"""Substack (discovered): posts found by searching Substack for the focus briefs' queries and open requests,
plus one more archive page per run from publications that produced a find. Every candidate goes through the
shared judge (judge.py) before it becomes an item.

Read slowly through Substack's public JSON endpoints (checked 2026-11-04):
- `substack.com/api/v1/top/search?query=Q` searches posts across Substack; each page holds about five posts
  among notes and profiles, and `nextCursor` (passed back as `cursor`) gives the next page.
- `<publication>/api/v1/archive?sort=new&offset=N&limit=M` lists a publication's posts, newest first.
- `<publication>/api/v1/posts/<slug>` returns one post with `body_html`: the full text when `audience` is
  `everyone` (free); for paid posts only the preview Substack shows everyone. Paywalls are never worked around.

Politeness: one request at a time, at least `request_delay_seconds` apart (plus a random part), at most
`max_requests` per run (half for searching, the rest for reading survivors), every response cached in
`substack_cache`, and any 429 or 403 stops Substack for the rest of the run.
"""

import json
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from urllib.parse import urlencode, urlsplit

import httpx
import trafilatura

from ..config import Settings, source_config
from ..enrich import HEADERS
from ..judge import Candidate, Need, Stop
from ..storage import now

NAME = "substack"
TRUST = "discovered"
KINDS = ("article",)
SECRETS: tuple[str, ...] = ()
TOOLS: tuple[str, ...] = ()
CONFIG = {
    "enabled": False,
    "every_hours": 20,              # at most one pass per this many hours
    "request_delay_seconds": 3.0,   # at least this long (plus up to half again) between requests
    "max_requests": 120,            # requests per run; the rest waits for the next run
    "search_pages": 2,              # result pages per query (about 5 posts each)
    "requery_days": 14,             # a query is searched again after this many days
    "archive_publications": 3,      # publications with finds whose archive gets one more page read per run
}

# Tables only this source uses.
SCHEMA = """
-- Publications that produced a find; their archives are read a page per run.
CREATE TABLE IF NOT EXISTS substack_publications (
    base_url TEXT PRIMARY KEY,
    name TEXT,
    archive_offset INTEGER NOT NULL DEFAULT 0,
    archive_done INTEGER NOT NULL DEFAULT 0,
    archive_read_at TEXT
);
-- Every Substack response, so nothing is requested twice within its useful life.
CREATE TABLE IF NOT EXISTS substack_cache (
    url TEXT PRIMARY KEY,
    status INTEGER NOT NULL,
    body TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""

SEARCH_URL = "https://substack.com/api/v1/top/search"
# Search results change; archive pages grow at the top; a published post does not change.
SEARCH_MAX_AGE_DAYS = 1
ARCHIVE_MAX_AGE_DAYS = 7
ARCHIVE_PAGE = 25
# Kept as item text and given to Jev, whose requests must stay far below its 64k-token limit.
TEXT_CHARACTERS = 30_000
# Responses older than this are deleted after each pass (an admitted item keeps its text in items).
CACHE_DAYS = 30
PAID_NOTE = "[Paid post: only the public preview was read.]"


@dataclass
class Client:
    connection: sqlite3.Connection
    delay: float
    max_requests: int
    requests: int = 0
    cached: int = 0
    blocked: str | None = None       # set by the first 429 or 403; no request is made after it
    _last: float = 0.0

    def get_json(self, url: str, params: dict | None = None, max_age_days: float | None = None):
        """max_age_days None: a cached answer never expires."""
        if params:
            url = f"{url}?{urlencode(params)}"
        row = self.connection.execute("SELECT status, body, fetched_at FROM substack_cache WHERE url = ?", (url,)).fetchone()
        if row and row["status"] == 200:
            age = (datetime.now(UTC) - datetime.fromisoformat(row["fetched_at"])).total_seconds() / 86400
            if max_age_days is None or age < max_age_days:
                self.cached += 1
                return json.loads(row["body"])
        if self.blocked:
            raise Stop(self.blocked)
        if self.requests >= self.max_requests:
            raise Stop(f"{self.max_requests} Substack requests made this run")
        wait = self._last + self.delay * random.uniform(1, 1.5) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            response = httpx.get(url, headers=HEADERS, follow_redirects=True, timeout=30)
        finally:
            self._last = time.monotonic()
            self.requests += 1
        self.connection.execute(
            "INSERT INTO substack_cache (url, status, body, fetched_at) VALUES (?, ?, ?, ?) ON CONFLICT(url) DO UPDATE "
            "SET status = excluded.status, body = excluded.body, fetched_at = excluded.fetched_at",
            (url, response.status_code, response.text, now()),
        )
        self.connection.commit()
        if response.status_code in (403, 429):
            self.blocked = f"{response.status_code} from {urlsplit(url).hostname}"
            raise Stop(self.blocked)
        response.raise_for_status()
        return response.json()

    def search(self, query: str, pages: int) -> list[dict]:
        """Posts (search items of type post, each with its publication) for a query, first `pages` pages."""
        items, cursor = [], None
        for _ in range(pages):
            params = {"query": query, **({"cursor": cursor} if cursor else {"page": 0})}
            answer = self.get_json(SEARCH_URL, params, SEARCH_MAX_AGE_DAYS)
            items += [item for item in answer.get("items") or [] if item.get("type") == "post" and item.get("post")]
            cursor = answer.get("nextCursor")
            if not cursor:
                break
        return items

    def archive(self, base_url: str, offset: int) -> list[dict]:
        params = {"sort": "new", "offset": offset, "limit": ARCHIVE_PAGE}
        return self.get_json(f"{base_url}/api/v1/archive", params, ARCHIVE_MAX_AGE_DAYS) or []

    def post(self, url: str, slug: str) -> dict:
        return self.get_json(f"{base_of(url)}/api/v1/posts/{slug}")


@cache
def _client(connection: sqlite3.Connection, delay: float, max_requests: int) -> Client:
    """One client per run (process and database connection), so discovery and reading share one budget."""
    connection.executescript(SCHEMA)
    return Client(connection, delay, max_requests)


def client(connection: sqlite3.Connection, settings: Settings) -> Client:
    options = source_config(settings, NAME, CONFIG)
    return _client(connection, float(options["request_delay_seconds"]), int(options["max_requests"]))


def discover(connection: sqlite3.Connection, settings: Settings, needs: list[Need]) -> list[Candidate]:
    """Search the needs within half the request budget, then read one more archive page from each of the
    publications with the most finds."""
    options = source_config(settings, NAME, CONFIG)
    substack = client(connection, settings)
    found: list[Candidate] = []
    searched = archived = 0
    try:
        for need in needs:
            if substack.requests + options["search_pages"] > options["max_requests"] // 2:
                break
            try:
                items = substack.search(need.query, options["search_pages"])
            except (httpx.HTTPError, ValueError) as error:  # tried again next run
                print(f"Substack: a search failed: {type(error).__name__}")
                continue
            found += [candidate(item["post"], item.get("publication"), need)
                      for item in items if item["post"].get("canonical_url")]
            need.searched = True
            searched += 1
        connection.execute(
            "INSERT OR IGNORE INTO substack_publications (base_url, name) SELECT author_url, author FROM candidates "
            "WHERE source = ? AND status = 'find' AND author_url IS NOT NULL",
            (NAME,),
        )
        publications = connection.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM candidates c WHERE c.author_url = p.base_url AND c.status = 'find') "
            "AS finds FROM substack_publications p WHERE archive_done = 0 ORDER BY finds DESC, archive_read_at LIMIT ?",
            (options["archive_publications"],),
        ).fetchall()
        for publication in publications:
            if substack.requests >= options["max_requests"] // 2:  # the other half reads survivors
                break
            try:
                posts = substack.archive(publication["base_url"], publication["archive_offset"])
            except (httpx.HTTPError, ValueError) as error:
                print(f"Substack: an archive page failed: {type(error).__name__}")
                continue
            details = {"name": publication["name"], "base_url": publication["base_url"]}
            found += [candidate(post, details, None) for post in posts if post.get("canonical_url")]
            connection.execute(
                "UPDATE substack_publications SET archive_offset = ?, archive_done = ?, archive_read_at = ? "
                "WHERE base_url = ?",
                (publication["archive_offset"] + len(posts), int(len(posts) < ARCHIVE_PAGE), now(),
                 publication["base_url"]),
            )
            connection.commit()
            archived += 1
    except Stop as error:
        print(f"Substack: stopped searching: {error}")
    _prune(connection)
    print(f"Substack: {searched} searches, {archived} archive pages, {len(found)} posts listed; "
          f"{substack.requests} requests so far, {substack.cached} answered from cache")
    return found


def fetch_text(connection: sqlite3.Connection, settings: Settings, candidate: sqlite3.Row) -> str:
    """The post's text: the whole post when free, the public preview (marked as such) when paid."""
    raw = json.loads(candidate["raw"])
    post = client(connection, settings).post(candidate["url"], raw["slug"])
    text = text_of(post) or candidate["preview"] or ""
    return text if is_free(post) else f"{PAID_NOTE}\n\n{text}"


def _prune(connection: sqlite3.Connection) -> None:
    """Forget old responses, so the database and its daily backup stay small."""
    cutoff = datetime.fromtimestamp(time.time() - CACHE_DAYS * 86400, UTC).isoformat(timespec="seconds")
    connection.execute("DELETE FROM substack_cache WHERE fetched_at < ?", (cutoff,))
    connection.commit()


def base_of(url: str) -> str:
    """Publication address from a post's canonical URL (works for custom domains too)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def is_free(post: dict) -> bool:
    return post.get("audience") == "everyone"


def candidate(post: dict, publication: dict | None, need: Need | None) -> Candidate:
    """A search or archive post as a candidate; its listing JSON is kept as raw."""
    url = post.get("canonical_url") or ""
    keep = ("id", "title", "subtitle", "description", "slug", "canonical_url", "post_date", "audience", "type",
            "wordcount", "reaction_count", "comment_count")
    author = (publication or {}).get("name")
    author_url = (publication or {}).get("base_url") or (base_of(url) if url else None)
    return Candidate(
        id=f"substack-{post['id']}",
        source=NAME,
        kind="article",
        url=url,
        title=(post.get("title") or "").strip() or "Untitled",
        subtitle=(post.get("subtitle") or post.get("description") or "").strip(),
        author=author,
        author_url=author_url,
        posted_at=post.get("post_date"),
        preview=(post.get("truncated_body_text") or post.get("description") or "").strip(),
        paid=None if post.get("audience") is None else post.get("audience") != "everyone",
        raw={key: post[key] for key in keep if key in post} | {"publication": {"name": author, "base_url": author_url}},
        need=need,
    )


def text_of(post: dict) -> str:
    """The post's text from its HTML: the whole post when free, the public preview when paid."""
    html = post.get("body_html") or ""
    if not html:
        return ""
    text = trafilatura.extract(f"<html><body><article>{html}</article></body></html>", include_tables=True,
                               include_comments=False) or ""
    return text[:TEXT_CHARACTERS]
