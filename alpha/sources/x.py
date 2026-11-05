"""X bookmarks (curated): the posts the user saved on X, read through X's own web app, free, in Lightpanda
(https://github.com/lightpanda-io/browser), a light headless browser driven over CDP by Playwright.

The browser is logged in with the user's session cookies (X_AUTH_TOKEN and X_CT0 in .env: the
`auth_token` and `ct0` cookies of a logged-in x.com tab). Pages load X's GraphQL JSON (Bookmarks for
the list, TweetDetail for a post's conversation, HomeLatestTimeline and HomeTimeline for the Following and
For You tabs); this module reads those responses, not the page's HTML, and stores each post as an item whose
raw JSON is the post in X API v2 shape (converted here). Lightpanda must be on PATH:
`brew install lightpanda-io/browser/lightpanda`.

`collect` reads new bookmarks (the list, newest first, until it reaches known ones); `enrich` opens up to
`post_pages` post pages per run (bookmarks and posts admitted from the timelines, x_timeline.py) for the rest
of the author's thread, their replies to comments, the post it replies to, and an X article's full text.
Everything X is read in one session per run (`page()`, ended by `close()`), and the first refusal stops all
X reading until the next run.

Two X states need the user and show as a banner on every dashboard page until they clear (metadata, JSON
{"at", "reason"}): SESSION_EXPIRED_KEY when the bookmarks page loads none (the session cookies expired), set
where that is found; REFUSED_KEY when X refused or throttled a read (the Throttled stop), saved with
`record_status()` at the end of a run. The next successful X read clears both.
"""

import json
import os
import random
import socket
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Response, sync_playwright

from ..config import Settings, source_config
from ..storage import add_content, now, set_metadata, store_item

NAME = "x"
TRUST = "curated"
KINDS = ("post",)
SECRETS = ("X_AUTH_TOKEN", "X_CT0")
TOOLS = ("lightpanda",)
CONFIG = {
    "enabled": False,
    "every_hours": 20,      # X is read at most once per this many hours
    "post_pages": 40,       # bookmark pages opened per run for threads and replies (a backlog takes several days)
    "limit": 0,             # bookmarks read per run; 0 reads until known ones
    "full_history": False,  # true: scroll through every bookmark instead of stopping at known ones
}

BOOKMARKS_URL = "https://x.com/i/bookmarks"
# How long to wait for the next GraphQL response after opening a page or scrolling.
RESPONSE_SECONDS = 20
# A timeline's next batch can be slow on a long page; wait this long before counting a scroll as empty.
TIMELINE_BATCH_SECONDS = 30
# Scrolls in a row that load nothing before the bookmark list counts as finished.
STALE_SCROLLS = 3
# GraphQL batches read on one timeline page before the tab ends. Lightpanda keeps every loaded post in its own
# 1.4 GB JS heap, and a For You page hit that limit after 99 batches, which closed the page mid-scroll. Below
# that, the scroll caps (following_scrolls, for_you_scrolls) decide.
BATCHES_PER_PAGE = 85
# Pacing, so the account reads like a person and X never has a reason to throttle it: a random pause
# before each next batch of bookmarks and between post pages, and at most `post_pages` post pages per
# run (a backlog of new bookmarks is checked over several daily runs, newest first).
SCROLL_PAUSE_SECONDS = (4.0, 9.0)
POST_PAUSE_SECONDS = (12.0, 25.0)
# A post's page lists replies in batches; scroll this many more batches for the author's replies.
REPLY_BATCHES = 1
REPLY_BATCH_SECONDS = 6
SCROLL = """() => {
  const last = [...document.querySelectorAll('article')].pop();
  if (last) last.scrollIntoView();
  window.scrollTo(0, document.body.scrollHeight);
}"""
HOME_URL = "https://x.com/home"
# GraphQL operations of the two home tabs, and the tab labels on the page.
TIMELINES = {"HomeLatestTimeline": ("following", "Following"), "HomeTimeline": ("for_you", "For you")}
CLICK_TAB = """(label) => {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const tab = tabs.find(t => t.textContent.trim() === label) || tabs.find(t => t.textContent.trim().startsWith(label));
  if (!tab) return false;
  tab.click();
  return true;
}"""


class Throttled(Exception):
    """X refused or limited a request. The run stops reading X at once (bookmarks, timelines, post pages);
    the next run continues."""


# One X session per run, shared by everything that reads X (bookmarks, post pages, timelines): opened on first
# use by page(), closed by close() at the end of the run. The first non-200 sets _stopped, and every later read
# in the run raises Throttled at once.
_shared: tuple[ExitStack, "_XPage"] | None = None
_stopped: str | None = None
# Why X itself stopped this run's reads (a refusal or a page that would not load), as against the browser failing;
# and whether any X read answered 200 with data. record_status() saves both.
_refused: str | None = None
_read_ok = False
loads = {"pages": 0, "scrolls": 0}
SESSION_EXPIRED_KEY = "x_session_expired"
REFUSED_KEY = "x_refused"


def _pause(bounds: tuple[float, float]) -> None:
    time.sleep(random.uniform(*bounds))


@contextmanager
def _lightpanda() -> Iterator[str]:
    """A Lightpanda CDP server on a free local port, stopped afterwards."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    try:
        process = subprocess.Popen(
            ["lightpanda", "serve", "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
            stdout=subprocess.DEVNULL, env=os.environ | {"LIGHTPANDA_DISABLE_TELEMETRY": "true"},
        )
    except FileNotFoundError:
        raise SystemExit("X: Lightpanda is not installed; run `brew install lightpanda-io/browser/lightpanda`") from None
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise SystemExit("X: Lightpanda did not start") from None
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(timeout=10)


class _XPage:
    """A logged-in x.com page that keeps the GraphQL responses it loads, by operation name."""

    def __init__(self, page: Page):
        self.page = page
        self.pending: list[tuple[str, Response]] = []
        page.on("response", self._keep)

    def _keep(self, response: Response) -> None:
        path = urlparse(response.url).path
        if "/graphql/" in path:
            self.pending.append((path.rsplit("/", 1)[-1], response))

    def open(self, url: str) -> None:
        if _stopped:
            raise Throttled(_stopped)
        self.pending.clear()
        loads["pages"] += 1
        self.page.goto(url, wait_until="commit", timeout=60_000)

    def scroll(self) -> None:
        if _stopped:
            raise Throttled(_stopped)
        loads["scrolls"] += 1
        self.page.evaluate(SCROLL)

    def click_tab(self, label: str) -> bool:
        """Click the tab with this text (Following, For you); False when the page has none."""
        return self.page.evaluate(CLICK_TAB, label)

    def first_of(self, operations: tuple[str, ...], seconds: float) -> str | None:
        """The first of these operations to answer since the last read, waiting up to `seconds`."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if found := next((name for name, _ in self.pending if name in operations), None):
                return found
            self.page.wait_for_timeout(250)
        return next((name for name, _ in self.pending if name in operations), None)

    def wait_for(self, operation: str, seconds: float) -> list[tuple[int, dict]]:
        """(HTTP status, JSON body) of every `operation` response since the last call, waiting up to
        `seconds` for the first one. Any refusal stops all X reading for the rest of the run."""
        global _stopped, _refused, _read_ok
        deadline = time.monotonic() + seconds
        while not any(name == operation for name, _ in self.pending) and time.monotonic() < deadline:
            self.page.wait_for_timeout(250)
        found = [response for name, response in self.pending if name == operation]
        self.pending = [(name, response) for name, response in self.pending if name != operation]
        bodies = []
        for response in found:
            try:
                bodies.append((response.status, response.json()))
            except (PlaywrightError, ValueError):
                bodies.append((response.status, {}))
        for status, body in bodies:
            if status != 200 or (body.get("errors") and not body.get("data")):
                _stopped = _refused = f"X answered {status} to {operation}"
                raise Throttled(_stopped)
        _read_ok = _read_ok or bool(bodies)
        return bodies


@contextmanager
def _session() -> Iterator[_XPage]:
    with _lightpanda() as endpoint, sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint)
        context = browser.new_context()
        context.add_cookies([
            {"name": "auth_token", "value": os.environ["X_AUTH_TOKEN"], "domain": ".x.com", "path": "/",
             "secure": True, "httpOnly": True},
            {"name": "ct0", "value": os.environ["X_CT0"], "domain": ".x.com", "path": "/", "secure": True},
        ])
        try:
            yield _XPage(context.new_page())
        finally:
            context.close()
            browser.close()


def page() -> _XPage:
    """The run's X session, opened on first use."""
    global _shared
    if _stopped:
        raise Throttled(_stopped)
    if _shared is None:
        stack = ExitStack()
        try:
            _shared = (stack, stack.enter_context(_session()))
        except BaseException:
            stack.close()
            raise
    return _shared[1]


def close() -> None:
    """End the run's X session (Lightpanda stops)."""
    global _shared
    if _shared is not None:
        stack, _shared = _shared[0], None
        stack.close()


def stop(reason: str, refused: bool = True) -> None:
    """Stop all X reading for the rest of the run; `refused` when X caused it (not the browser failing)."""
    global _stopped, _refused
    _stopped = _stopped or reason
    if refused:
        _refused = _refused or reason


def _note(connection: sqlite3.Connection, key: str, reason: str) -> None:
    set_metadata(connection, key, json.dumps({"at": now(), "reason": reason}))
    connection.commit()


def record_status(connection: sqlite3.Connection) -> None:
    """At the end of a run: save X's refusal (REFUSED_KEY) if it refused, and clear what a successful X read
    resolved (the expired session; a refusal from an earlier run). A run that read no X changes nothing."""
    if _read_ok:
        connection.execute("DELETE FROM metadata WHERE key = ?", (SESSION_EXPIRED_KEY,))
    if _refused:
        _note(connection, REFUSED_KEY, _refused)
    elif _read_ok:
        connection.execute("DELETE FROM metadata WHERE key = ?", (REFUSED_KEY,))
    connection.commit()


def _logged_out(connection: sqlite3.Connection, page: _XPage) -> SystemExit:
    """The session expired: noted for the dashboard's banner (SESSION_EXPIRED_KEY), and the run's X source fails."""
    _note(connection, SESSION_EXPIRED_KEY, f"no bookmarks loaded (page: {page.page.url})")
    return SystemExit(
        f"X: no bookmarks loaded (page: {page.page.url}). The session in .env has probably expired: copy the "
        "auth_token and ct0 cookies of a logged-in x.com tab into X_AUTH_TOKEN and X_CT0."
    )


def _tweet(result: dict | None) -> dict | None:
    """The post inside a GraphQL result, unwrapping visibility wrappers; None for deleted or withheld posts."""
    if result and result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet")
    return result if result and result.get("legacy") else None


def _iso(created_at: str) -> str:
    return datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y").astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _convert(tweet: dict) -> tuple[dict, dict[str, dict], dict[str, str]]:
    """The post in X API v2 shape, its images by media key, and its author's id -> username."""
    legacy = tweet["legacy"]
    note = ((tweet.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    urls = (note.get("entity_set") or legacy.get("entities") or {}).get("urls") or []
    post = {
        "id": tweet["rest_id"],
        "text": legacy["full_text"],
        "created_at": _iso(legacy["created_at"]),
        "author_id": legacy["user_id_str"],
        "conversation_id": legacy["conversation_id_str"],
        "lang": legacy.get("lang"),
        "entities": {"urls": [{"url": u["url"], "expanded_url": u.get("expanded_url"), "display_url": u.get("display_url")}
                              for u in urls]},
        "public_metrics": {
            "like_count": legacy.get("favorite_count"), "retweet_count": legacy.get("retweet_count"),
            "reply_count": legacy.get("reply_count"), "quote_count": legacy.get("quote_count"),
            "bookmark_count": legacy.get("bookmark_count"),
            "impression_count": int(views) if (views := (tweet.get("views") or {}).get("count")) else None,
        },
    }
    if note.get("text"):
        post["note_post"] = {"text": note["text"]}
    media: dict[str, dict] = {}
    for item in (legacy.get("extended_entities") or {}).get("media") or []:
        image = item["media_url_https"]
        media[item["media_key"]] = {"media_key": item["media_key"], "type": item["type"], "alt_text": item.get("ext_alt_text"),
                                    **({"url": image} if item["type"] == "photo" else {"preview_image_url": image})}
    if media:
        post["attachments"] = {"media_keys": list(media)}
    article = ((tweet.get("article") or {}).get("article_results") or {}).get("result")
    if article:
        post["article_title"] = article.get("title")
        post["article"] = {"title": article.get("title"), "media_entities": []}
        # The full text (content_state) comes only on the post's own page, not in the bookmark list.
        if blocks := (article.get("content_state") or {}).get("blocks"):
            post["article"]["plain_text"] = "\n".join(block.get("text", "") for block in blocks)
        for item in [article.get("cover_media"), *(article.get("media_entities") or [])]:
            if url := ((item or {}).get("media_info") or {}).get("original_img_url"):
                key = f"3_{item['media_id']}"
                media[key] = {"media_key": key, "type": "photo", "url": url}
                post["article"]["media_entities"].append(key)
    references = []
    if parent := legacy.get("in_reply_to_status_id_str"):
        references.append({"type": "replied_to", "id": parent})
    if quoted := _tweet((tweet.get("quoted_status_result") or {}).get("result")):
        references.append({"type": "quoted", "id": quoted["rest_id"]})
    if references:
        post["referenced_tweets"] = references
    user = ((tweet.get("core") or {}).get("user_results") or {}).get("result") or {}
    username = (user.get("core") or {}).get("screen_name") or (user.get("legacy") or {}).get("screen_name")
    return post, media, ({post["author_id"]: username} if username else {})


def _collect(tweet: dict) -> tuple[dict, dict[str, dict], dict[str, dict], dict[str, str]]:
    """The converted post plus its quoted post: (post, media of both, {quoted id: quoted post}, usernames)."""
    post, media, users = _convert(tweet)
    referenced = {}
    if quoted := _tweet((tweet.get("quoted_status_result") or {}).get("result")):
        quoted_post, quoted_media, quoted_users = _convert(quoted)
        referenced[quoted_post["id"]] = quoted_post
        media |= quoted_media
        users |= quoted_users
    return post, media, referenced, users



def _bookmark_tweets(body: dict) -> list[dict]:
    instructions = (((body.get("data") or {}).get("bookmark_timeline_v2") or {}).get("timeline") or {}).get("instructions") or []
    return [
        tweet
        for instruction in instructions
        for entry in instruction.get("entries") or []
        if entry.get("entryId", "").startswith("tweet-")
        and (tweet := _tweet(((entry.get("content") or {}).get("itemContent") or {}).get("tweet_results", {}).get("result")))
    ]


def _results(value) -> Iterator[dict]:
    """Every post result in a GraphQL body, in document order, wherever the timeline nests it."""
    if isinstance(value, dict):
        if "tweet_results" in value:
            if tweet := _tweet(value["tweet_results"].get("result")):
                yield tweet
        for key, child in value.items():
            if key != "tweet_results":
                yield from _results(child)
    elif isinstance(value, list):
        for child in value:
            yield from _results(child)


def _links(post: dict) -> list[dict]:
    links = []
    for entity in (post.get("entities") or {}).get("urls") or []:
        url = entity.get("unwound_url") or entity.get("expanded_url") or entity.get("url")
        # Links to other posts and attached media add nothing the text does not already say.
        if not url or entity.get("media_key"):
            continue
        links.append({"url": url, "title": entity.get("title"), "description": entity.get("description")})
    return links


def _text(post: dict) -> str:
    note = post.get("note_post") or {}
    return note.get("text") or post.get("text") or ""


def _media_keys(post: dict) -> list[str]:
    keys = list((post.get("attachments") or {}).get("media_keys") or [])
    keys += [key for key in (post.get("article") or {}).get("media_entities") or [] if isinstance(key, str)]
    return keys


def _store_media(connection: sqlite3.Connection, post_id: str, post: dict, media: dict[str, dict]) -> None:
    for key in _media_keys(post):
        item = media.get(key)
        url = item and (item.get("url") or item.get("preview_image_url"))
        if url:
            detail = {"url": url, "type": item.get("type"), "alt_text": item.get("alt_text")}
            add_content(connection, post_id, "image", key, None, detail)


def store_post(connection: sqlite3.Connection, post: dict, media: dict[str, dict], posts: dict[str, dict],
               users: dict[str, str], item: dict | None = None) -> bool:
    """Store one post as an item (raw: the post in X API shape) with its images, article, and referenced
    posts. `item` overrides item fields (a timeline find: its own id, source, trust, rank). Returns False when it
    was already stored; its raw JSON is then refreshed (newer fields)."""
    post_id = str(post["id"])
    fields = {
        "id": post_id, "source": NAME, "kind": "post", "trust": TRUST, "url": f"https://x.com/i/web/status/{post_id}",
        "title_source": post.get("article_title"), "author": users.get(str(post.get("author_id"))),
        "created_at": post.get("created_at"), "text": _text(post), "links": _links(post), "raw": post,
    } | (item or {})
    item_id = fields["id"]
    is_new = store_item(connection, fields)
    _store_media(connection, item_id, post, media)
    article = post.get("article") or {}
    if article.get("plain_text"):
        add_content(connection, item_id, "article", post_id, article["plain_text"], {"title": article.get("title")})
    for reference in post.get("referenced_tweets") or []:
        referenced = posts.get(str(reference["id"]))
        if referenced and reference["type"] in ("quoted", "replied_to"):
            add_content(connection, item_id, reference["type"], str(reference["id"]), _text(referenced))
            _store_media(connection, item_id, referenced, media)
    return is_new


def _rank(connection: sqlite3.Connection, newest_first: list[str]) -> None:
    """A run reads from the top of the bookmark list, so the posts it read are, in that order, above
    every other stored item (those are older, or no longer bookmarked)."""
    others = connection.execute(
        f"SELECT COALESCE(MAX(rank), 0) FROM items WHERE id NOT IN ({','.join('?' * len(newest_first))})",
        newest_first,
    ).fetchone()[0]
    connection.executemany(
        "UPDATE items SET rank = ? WHERE id = ?",
        [(others + len(newest_first) - index, post_id) for index, post_id in enumerate(newest_first)],
    )


def fetch_bookmarks(connection: sqlite3.Connection, *, limit: int | None, full_history: bool) -> tuple[int, int]:
    """Store new bookmarks, newest first, and rank every post read by its place in the list (X gives
    no time a post was bookmarked). Scrolls the bookmarks page until a batch contains a stored
    bookmark (unless full_history), the list ends, or `limit` posts have been read. Returns (read, new)."""
    read = new = 0
    newest_first: list[str] = []
    xpage = page()
    xpage.open(BOOKMARKS_URL)
    stale = 0
    while True:
        batches = xpage.wait_for("Bookmarks", RESPONSE_SECONDS)
        if not batches and not newest_first:
            raise _logged_out(connection, xpage)
        loaded = [t for _, body in batches for t in _bookmark_tweets(body)]
        if batches and not loaded:  # only cursors: the end of the list
            break
        seen = set(newest_first)
        tweets = list({t["rest_id"]: t for t in loaded if t["rest_id"] not in seen}.values())
        if not tweets:  # nothing loaded, or a batch X sent again
            stale += 1
            if stale >= STALE_SCROLLS:
                break
            _pause(SCROLL_PAUSE_SECONDS)
            xpage.scroll()
            continue
        stale = 0
        if limit is not None:
            tweets = tweets[: limit - read]
        batch_new = 0
        for tweet in tweets:
            post, media, referenced, users = _collect(tweet)
            batch_new += store_post(connection, post, media, referenced, users)
            newest_first.append(post["id"])
        _rank(connection, newest_first)
        connection.commit()
        read += len(tweets)
        new += batch_new
        if (limit is not None and read >= limit) or (batch_new < len(tweets) and not full_history):
            break
        _pause(SCROLL_PAUSE_SECONDS)
        xpage.scroll()
    print(f"X: read {read} bookmarks, {new} new")
    return read, new


def _entries(value) -> Iterator[dict]:
    """Every timeline entry (a dict with an entryId) in a GraphQL body, in order."""
    if isinstance(value, dict):
        if "entryId" in value:
            yield value
            return
        for child in value.values():
            yield from _entries(child)
    elif isinstance(value, list):
        for child in value:
            yield from _entries(child)


def _promoted(value) -> bool:
    """An ad: some part of the entry has a non-null promotedMetadata (ordinary posts have none, or null)."""
    if isinstance(value, dict):
        return bool(value.get("promotedMetadata")) or any(_promoted(child) for child in value.values())
    return isinstance(value, list) and any(_promoted(child) for child in value)


def fetch_timelines(*, following_scrolls: int, for_you_scrolls: int,
                    known: Callable[[str], str | None]) -> tuple[list[dict], dict]:
    """The home timelines in the run's X session: Following (newest first) until it reaches a post `known`
    returns "seen" for, at most `following_scrolls` scrolls; For You for `for_you_scrolls` scrolls. A scroll that
    brings no batch within TIMELINE_BATCH_SECONDS is retried (the last post scrolled into view again) up to
    STALE_SCROLLS times in a row before the tab ends, and a page ends after BATCHES_PER_PAGE batches. Promoted
    posts and plain reposts are skipped; quote posts are kept. Returns ({tab, post, media, referenced, users} per
    new post, counts and per-tab diagnostics)."""
    stats: dict = {"following": 0, "for_you": 0, "promoted": 0, "reposts": 0, "seen before": 0, "bookmarked": 0}
    posts: list[dict] = []
    taken: set[str] = set()

    def take(tab: str, batches: list[tuple[int, dict]]) -> tuple[bool, list[int]]:
        """Keep the new posts of these batches. Returns (a post seen in an earlier run came up, new posts per batch)."""
        reached, counts = False, []
        for _, body in batches:
            before = stats[tab]
            for entry in _entries(body):
                if entry.get("entryId", "").startswith("promoted") or _promoted(entry):
                    stats["promoted"] += 1
                    continue
                for tweet in _results(entry):
                    if tweet["legacy"].get("retweeted_status_result"):
                        stats["reposts"] += 1
                        continue
                    post_id = tweet["rest_id"]
                    if post_id in taken:
                        continue
                    taken.add(post_id)
                    if reason := known(post_id):
                        stats["seen before" if reason == "seen" else "bookmarked"] += 1
                        # Only a post of its own entry counts: a conversation repeats older posts above a new reply.
                        reached = reached or (reason == "seen" and entry.get("entryId", "").startswith("tweet-"))
                        continue
                    post, media, referenced, users = _collect(tweet)
                    posts.append({"tab": tab, "post": post, "media": media, "referenced": referenced, "users": users})
                    stats[tab] += 1
            counts.append(stats[tab] - before)
        return reached, counts

    xpage = page()
    for operation in TIMELINES:  # Following first: it stops at posts seen in an earlier run
        tab, label = TIMELINES[operation]
        cap = following_scrolls if tab == "following" else for_you_scrolls
        scrolls = stale = 0
        per_batch: list[int] = []
        end = f"cap of {cap} scrolls"
        try:
            # A fresh page per tab: Lightpanda keeps every loaded post in memory, and a long page runs out of heap.
            if loads["pages"]:
                _pause(POST_PAUSE_SECONDS)
            xpage.open(HOME_URL)
            first = xpage.first_of(tuple(TIMELINES), RESPONSE_SECONDS)
            if first is None:
                stop(f"the home timeline did not load (page: {xpage.page.url})")
                raise Throttled(_stopped)
            if first != operation:
                _pause(SCROLL_PAUSE_SECONDS)
                if not xpage.click_tab(label):
                    stats[f"{tab} tab"] = "not found on the page"
                    continue
            batches = xpage.wait_for(operation, RESPONSE_SECONDS)
            if not batches:
                stats[f"{tab} tab"] = "did not load"
                continue
            reached, per_batch = take(tab, batches)
            while scrolls < cap:
                if reached and tab == "following":
                    end = "reached posts seen in an earlier run"
                    break
                if len(per_batch) >= BATCHES_PER_PAGE:
                    end = f"{BATCHES_PER_PAGE} batches on one page (memory ceiling)"
                    break
                _pause(SCROLL_PAUSE_SECONDS)
                xpage.scroll()
                scrolls += 1
                more = xpage.wait_for(operation, TIMELINE_BATCH_SECONDS)
                if not more:
                    stale += 1
                    if stale >= STALE_SCROLLS:
                        end = f"no new batch after {STALE_SCROLLS} scrolls in a row"
                        break
                    continue
                stale = 0
                reached, counts = take(tab, more)
                per_batch += counts
        except Throttled as error:  # X refused: keep what was read; _stopped ends all X reading this run
            end = f"X refused: {error}"
        except PlaywrightError as error:  # the browser page died: keep what was read, read no more X this run
            end = f"the page failed: {str(error).splitlines()[0][:120]}"
            stop(end, refused=False)
        finally:
            stats[f"{tab} diagnostics"] = (f"{scrolls} scrolls, {len(per_batch)} batches, new posts per batch "
                                           f"{per_batch}, stopped: {end}")
        if _stopped:
            stats["stopped"] = _stopped
            break
    return posts, stats


def _self_thread(found: list[dict], post_id: str, conversation: str) -> list[dict]:
    """The author's posts (`found`) that continue their own chain from the conversation root or the
    bookmark, oldest first. Drops a reply the author made to themselves under someone else's comment."""
    # The root counts only when the author wrote it; otherwise the bookmark is a reply in someone else's conversation.
    author_root = any(str(p["id"]) == conversation for p in found)
    chain = {post_id, conversation} if author_root else {post_id}
    kept = []
    for post in sorted(found, key=lambda p: p.get("created_at", "")):
        replied_to = {str(r["id"]) for r in post.get("referenced_tweets") or [] if r["type"] == "replied_to"}
        if str(post["id"]) == conversation or replied_to & chain:
            chain.add(str(post["id"]))
            if str(post["id"]) != post_id:
                kept.append(post)
    return kept


def _author_replies(posts: dict[str, dict], users: dict[str, str], author: str, thread: set[str]) -> list[tuple[dict, dict]]:
    """(comment, the author's reply) for every reply the author made to someone else's comment, oldest first."""
    pairs = []
    for post in posts.values():
        parent_id = next((r["id"] for r in post.get("referenced_tweets") or [] if r["type"] == "replied_to"), None)
        parent = posts.get(parent_id or "")
        if post["author_id"] == author and post["id"] not in thread and parent and parent["author_id"] != author:
            pairs.append((parent, post))
    return sorted(pairs, key=lambda pair: pair[1]["created_at"])


def _conversation(page: _XPage, post_id: str) -> list[dict] | None:
    """Every post on the bookmark's page, after loading one more batch of replies; None when the page
    did not load."""
    page.open(f"https://x.com/i/status/{post_id}")
    batches = page.wait_for("TweetDetail", RESPONSE_SECONDS)
    if not batches:
        return None
    for _ in range(REPLY_BATCHES):
        _pause(SCROLL_PAUSE_SECONDS)
        page.scroll()
        more = page.wait_for("TweetDetail", REPLY_BATCH_SECONDS)
        if not more:
            break
        batches += more
    return [tweet for _, body in batches for tweet in _results(body)]


def _store_thread(connection: sqlite3.Connection, post_id: str, conversation: str, text: str, detail: dict) -> None:
    """The thread row marks a bookmark as checked; `source: web` replaces a row the old X API search wrote."""
    connection.execute(
        "INSERT OR REPLACE INTO content (item_id, kind, key, text, detail, fetched_at) VALUES (?, 'thread', ?, ?, ?, ?)",
        (post_id, conversation, text, json.dumps(detail | {"source": "web"}, ensure_ascii=False), now()),
    )


def fetch_threads(connection: sqlite3.Connection, *, limit: int) -> None:
    """For each X item (a bookmark, or a post admitted from the timelines) not yet checked (at most `limit`
    per run), keep from its page: the rest of the original poster's thread, the author's replies to comments
    (each with the comment), the post it replies to, and an X article's full text, with their images. Items
    with no thread row come first, newest first; then ones whose thread the old X API search read, for the
    author's replies it never collected. A page that does not load is tried again next run."""
    rows = connection.execute(
        "SELECT id, raw FROM items i WHERE i.source IN ('x', 'x_timeline') AND NOT EXISTS (SELECT 1 FROM content c "
        "WHERE c.item_id = i.id AND c.kind = 'thread' AND json_extract(c.detail, '$.source') = 'web') "
        "ORDER BY EXISTS (SELECT 1 FROM content c WHERE c.item_id = i.id AND c.kind = 'thread'), i.rank DESC"
    ).fetchall()
    waiting = max(len(rows) - limit, 0)
    rows = rows[:limit]
    if not rows:
        print("X: no new posts to check for threads")
        return
    threads = with_replies = failed = misses = 0
    xpage = page()
    for index, row in enumerate(rows):
        if index or loads["pages"]:
            _pause(POST_PAUSE_SECONDS)
        # item_id is where content is kept; post_id is X's own id (a timeline find's item id has a prefix).
        item_id, bookmark = row["id"], json.loads(row["raw"])
        post_id = str(bookmark.get("id") or item_id)
        author, conversation = str(bookmark.get("author_id")), str(bookmark.get("conversation_id") or post_id)
        tweets = _conversation(xpage, post_id)
        if tweets is None:
            failed += 1
            misses += 1
            if misses >= 3:
                stop("3 post pages in a row did not load")
                raise Throttled(_stopped)
            continue
        misses = 0
        posts: dict[str, dict] = {}
        media: dict[str, dict] = {}
        users: dict[str, str] = {}
        for tweet in tweets:
            post, post_media, post_users = _convert(tweet)
            if post["conversation_id"] == conversation or post["id"] == post_id:
                posts[post["id"]] = post
                media |= post_media
                users |= post_users
        if users.get(author):
            connection.execute("UPDATE items SET author = COALESCE(author, ?) WHERE id = ?", (users[author], item_id))
        if post_id not in posts:
            _store_thread(connection, item_id, conversation, "", {"skipped": "post not available"})
            connection.commit()
            continue
        focal = posts[post_id]
        others = _self_thread([p for p in posts.values() if p["author_id"] == author], post_id, conversation)
        _store_thread(connection, item_id, conversation, "\n\n".join(_text(p) for p in others),
                      {"post_ids": [p["id"] for p in others]})
        for other in others:
            _store_media(connection, item_id, other, media)
        thread = {post_id, conversation} | {p["id"] for p in others}
        if pairs := _author_replies(posts, users, author, thread):
            text = "\n\n".join(
                f"@{users.get(comment['author_id'], comment['author_id'])}: {_text(comment)}\n"
                f"@{users.get(author, author)}: {_text(reply)}"
                for comment, reply in pairs
            )
            add_content(connection, item_id, "author_replies", conversation, text,
                        {"post_ids": [reply["id"] for _, reply in pairs]})
            with_replies += 1
        parent_id = next((r["id"] for r in focal.get("referenced_tweets") or [] if r["type"] == "replied_to"), None)
        if parent := posts.get(parent_id or ""):
            add_content(connection, item_id, "replied_to", parent["id"], _text(parent))
            _store_media(connection, item_id, parent, media)
        if plain := (focal.get("article") or {}).get("plain_text"):
            add_content(connection, item_id, "article", post_id, plain, {"title": focal["article"].get("title")})
            _store_media(connection, item_id, focal, media)
        connection.commit()
        threads += bool(others)
    print(f"X: checked {len(rows) - failed} posts: {threads} threads, {with_replies} with the author's replies to "
          f"comments" + (f"; {failed} pages did not load, tried again next run" if failed else "")
          + (f"; {waiting} more next run" if waiting else ""))


def collect(connection: sqlite3.Connection, settings: Settings) -> dict:
    """Read new bookmarks. Stops at the first sign of throttling (`stopped` in the stats); the next run continues."""
    options = source_config(settings, NAME, CONFIG)
    try:
        read, new = fetch_bookmarks(connection, limit=options["limit"] or None, full_history=options["full_history"])
    except Throttled as error:  # everything read so far is kept; reading again now would only make it worse
        print(f"X: {error}; stopped reading X until the next run")
        return {"stopped": str(error)}
    return {"read": read, "new": new}


def enrich(connection: sqlite3.Connection, settings: Settings) -> None:
    """Threads, the author's replies, and articles from up to `post_pages` bookmark pages."""
    try:
        fetch_threads(connection, limit=source_config(settings, NAME, CONFIG)["post_pages"])
    except Throttled as error:
        print(f"X: {error}; stopped reading X until the next run")
