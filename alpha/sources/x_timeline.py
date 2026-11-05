"""Your X timelines (discovered): Alpha scrolls the Following and For You tabs for you and keeps only the real
alpha, so you scroll Alpha instead of X.

Each run (in the same X session as the bookmarks, x.py): Following, newest first, until it reaches posts seen in
an earlier run (at most `following_scrolls` scrolls), then For You for `for_you_scrolls` scrolls, with the same
random 4 to 9 s pauses as the bookmarks. Promoted posts and plain reposts are skipped (quote posts are kept),
and so are posts already bookmarked. Every post seen is stored as a candidate once (author, metrics, and the tab
it came from in `query`), so nothing is judged twice. The first refusal from X stops all X reading for the run.

The shared judge (judge.py) decides: stage 1 fits each post to the focus briefs or to the user's taste (taste.py,
written from everything they picked), many posts per Jev request; stage 2 gives both judges the taste profile and the
three picked items most like the post as examples. Admitted posts become items like bookmarks (with images and quoted
posts); the next run's post-page pass (x.enrich) adds their threads and the author's replies.
"""

import json
import sqlite3
import time

from ..config import Settings, source_config
from ..judge import Candidate, Need
from . import x

NAME = "x_timeline"
TRUST = "discovered"
KINDS = ("post",)
SECRETS = x.SECRETS
TOOLS = x.TOOLS
TASTE = True        # judged against the user's taste as well as the focus briefs
SEARCHES = False    # reads a feed, not the needs: open requests never start it
CONFIG = {
    "enabled": False,
    "every_hours": 20,          # the timelines are read at most once per this many hours
    "following_scrolls": 60,    # safety cap; Following stops earlier at posts seen in an earlier run
    "for_you_scrolls": 30,
}
TITLE_CHARACTERS = 120
PREVIEW_CHARACTERS = 2000
TABS = {"following": "your Following", "for_you": "For You"}


def _text(entry: dict) -> str:
    """The post's text with its quoted post and links: what the judges read."""
    post, users = entry["post"], entry["users"]
    parts = [x._text(post)]
    for quoted in entry["referenced"].values():
        parts.append(f"Quoting @{users.get(str(quoted.get('author_id')), 'someone')}: {x._text(quoted)}")
    if links := [link["url"] for link in x._links(post)]:
        parts.append("Links: " + " ".join(links))
    return "\n\n".join(part for part in parts if part)


def discover(connection: sqlite3.Connection, settings: Settings, needs: list[Need]) -> list[Candidate]:
    options = source_config(settings, NAME, CONFIG)

    def known(post_id: str) -> str | None:
        if connection.execute("SELECT 1 FROM candidates WHERE id = ?", (f"{NAME}-{post_id}",)).fetchone():
            return "seen"
        if connection.execute("SELECT 1 FROM items WHERE id = ?", (post_id,)).fetchone():
            return "bookmarked"
        return None

    started, before = time.monotonic(), dict(x.loads)
    try:
        entries, stats = x.fetch_timelines(following_scrolls=int(options["following_scrolls"]),
                                           for_you_scrolls=int(options["for_you_scrolls"]), known=known)
    except x.Throttled as error:
        print(f"X timelines: {error}; stopped reading X until the next run")
        return []
    finally:  # the timelines are the run's last X reads (bookmarks and post pages come first)
        x.close()
    found = []
    for entry in entries:
        post = entry["post"]
        username = entry["users"].get(str(post.get("author_id")))
        text = _text(entry)
        first = " ".join(text.split("\n", 1)[0].split())
        found.append(Candidate(
            id=f"{NAME}-{post['id']}", source=NAME, kind="post",
            url=f"https://x.com/{username or 'i/web'}/status/{post['id']}",
            title=(first[:TITLE_CHARACTERS].rstrip() + ("…" if len(first) > TITLE_CHARACTERS else "")) or "Post",
            author=username, author_url=f"https://x.com/{username}" if username else None,
            posted_at=post.get("created_at"), preview=text[:PREVIEW_CHARACTERS],
            raw=entry | {"text": text}, need=Need(entry["tab"]),
        ))
    pages, scrolls = x.loads["pages"] - before["pages"], x.loads["scrolls"] - before["scrolls"]
    print(f"X timelines: {stats['following']} new from Following, {stats['for_you']} new from For You; skipped "
          f"{stats['promoted']} promoted, {stats['reposts']} reposts, {stats['seen before']} seen before, "
          f"{stats['bookmarked']} already bookmarked; {pages} page loads, {scrolls} scrolls, "
          f"{time.monotonic() - started:.0f} s"
          + (f"; stopped reading X until the next run: {stats['stopped']}" if "stopped" in stats else "")
          + "".join(f"\n  {key}: {value}" for key, value in stats.items() if key.endswith((" tab", " diagnostics"))))
    return found


def fetch_text(connection: sqlite3.Connection, settings: Settings, candidate: sqlite3.Row) -> str:
    """Kept from the timeline when it was read: no request to X."""
    return json.loads(candidate["raw"])["text"]


def admit(connection: sqlite3.Connection, settings: Settings, candidate: sqlite3.Row, rank: int) -> None:
    """Store a find like a bookmark (images, quoted post, article), under this source's id and trust."""
    raw = json.loads(candidate["raw"])
    x.store_post(connection, raw["post"], raw["media"], raw["referenced"], raw["users"],
                 {"id": candidate["id"], "source": NAME, "trust": TRUST, "url": candidate["url"], "rank": rank})
