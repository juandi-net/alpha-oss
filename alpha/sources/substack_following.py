"""Substack subscriptions (discovered): new posts from the publications the user subscribes to on Substack, judged
against the user's focus briefs and their taste, so a post they would want shows up without their reading the inbox.

Signed in with the user's Substack session: SUBSTACK_SID in .env, the `substack.sid` cookie of a logged-in
substack.com tab. The cookie is sent in exactly one request per run, to substack.com only (no redirects followed):
- `GET substack.com/api/v1/subscriptions?tvOnly=false` lists the publications the account subscribes to
  (`publications`: name, subdomain, custom domain). 401 without a session (checked 2026-11-04).
Everything else is read anonymously through the substack source's client and public endpoints:
- `<publication>/api/v1/archive?sort=new&offset=0&limit=per_publication` lists each publication's newest posts;
- `<publication>/api/v1/posts/<slug>` gives a post's text for stage 2 (substack.read_post: the whole post when
  free, the public preview, marked, when paid; paywalls are never worked around, even ones the user pays for).

Each run reads the posts published since the last complete listing (the first run: the last `first_days` days).
Posts Alpha has already seen from any source (same URL as a candidate or an item) are skipped. The shared judge
(judge.py) decides as for the X timelines (TASTE): stage 1 reads the title, subtitle, and preview; stage 2 the post.
Admitted posts are items of kind article, which can feed skills.

Politeness: the substack source's client (one request at a time, at least `request_delay_seconds` apart, at most
`max_requests` per run, responses cached in `substack_cache`, and any 429 or 403 stops Substack for the run).
Listing takes at most two thirds of the budget, publications read longest ago first; the rest reads survivors.
"""

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from ..config import Settings, source_config
from ..enrich import HEADERS
from ..judge import Candidate, Need, Stop
from ..storage import get_metadata, now, set_metadata
from . import substack

NAME = "substack_following"
TRUST = "discovered"
KINDS = ("article",)
SECRETS = ("SUBSTACK_SID",)
TOOLS: tuple[str, ...] = ()
TASTE = True        # judged against the user's taste as well as the focus briefs
SEARCHES = False    # reads the user's subscriptions, not the needs: open requests never start it
CONFIG = {
    "enabled": False,
    "every_hours": 20,
    "request_delay_seconds": 2.0,   # at least this long (plus up to half again) between requests
    "max_requests": 300,            # per run: one archive page per publication, then one read per stage-2 survivor
    "first_days": 7,                # the first run looks back this far
    "per_publication": 10,          # newest posts listed per publication and run
}

SUBSCRIPTIONS_URL = "https://substack.com/api/v1/subscriptions"
ARCHIVE_MAX_AGE_DAYS = 0.5
OVERLAP = timedelta(hours=1)        # each window starts this long before the last complete listing ended
LISTED_KEY = f"{NAME}:listed_through"
LISTING_SHARE = 2 / 3               # of max_requests; the rest reads stage-2 survivors


def client(connection: sqlite3.Connection, settings: Settings) -> substack.Client:
    options = source_config(settings, NAME, CONFIG)
    return substack._client(connection, float(options["request_delay_seconds"]), int(options["max_requests"]))


def base_url(publication: dict) -> str | None:
    """A publication's address: its custom domain when it uses one, else its substack.com subdomain."""
    if publication.get("custom_domain") and not publication.get("custom_domain_optional"):
        return f"https://{publication['custom_domain']}"
    if publication.get("subdomain"):
        return f"https://{publication['subdomain']}.substack.com"
    return None


def subscriptions() -> list[dict]:
    """The publications the user subscribes to, as {name, base_url}. The one request that carries the session."""
    sid = os.environ.get("SUBSTACK_SID", "").strip()
    response = httpx.get(
        SUBSCRIPTIONS_URL, params={"tvOnly": "false"}, headers=HEADERS | {"Accept": "application/json"},
        cookies={"substack.sid": sid, "connect.sid": sid}, follow_redirects=False, timeout=30,
    )
    if response.status_code in (401, 403) or response.is_redirect:
        raise RuntimeError(f"Substack refused the session ({response.status_code}): copy the substack.sid cookie of a "
                           "logged-in substack.com tab into SUBSTACK_SID in .env again")
    response.raise_for_status()
    publications, seen = [], set()
    for publication in response.json().get("publications") or []:
        url = base_url(publication)
        if url and url not in seen:
            seen.add(url)
            publications.append({"name": publication.get("name") or url, "base_url": url})
    return publications


def _archive_params(limit: int) -> dict:
    return {"sort": "new", "offset": 0, "limit": limit}


def _read_at(connection: sqlite3.Connection, base: str, limit: int) -> str:
    """When this publication's archive page was last fetched ('' never), from the shared cache."""
    url = f"{base}/api/v1/archive?{urlencode(_archive_params(limit))}"
    row = connection.execute("SELECT fetched_at FROM substack_cache WHERE url = ?", (url,)).fetchone()
    return row["fetched_at"] if row else ""


def _seen(connection: sqlite3.Connection, url: str) -> bool:
    return bool(connection.execute(
        "SELECT 1 FROM candidates WHERE url = ? UNION ALL SELECT 1 FROM items WHERE url = ? LIMIT 1", (url, url),
    ).fetchone())


def _posted(post: dict) -> datetime | None:
    value = post.get("post_date")
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def discover(connection: sqlite3.Connection, settings: Settings, needs: list[Need]) -> list[Candidate]:
    options = source_config(settings, NAME, CONFIG)
    reader = client(connection, settings)
    limit = int(options["per_publication"])
    listed_through = get_metadata(connection, LISTED_KEY)
    since = (datetime.fromisoformat(listed_through) - OVERLAP if listed_through
             else datetime.now(UTC) - timedelta(days=float(options["first_days"])))
    started = now()
    publications = subscriptions()
    # Longest ago first, so a run cut short by the budget reaches the rest next time.
    publications.sort(key=lambda p: _read_at(connection, p["base_url"], limit))
    budget = int(int(options["max_requests"]) * LISTING_SHARE)
    found: list[Candidate] = []
    read = posts = seen = failed = 0
    complete = False
    try:
        for publication in publications:
            if reader.requests >= budget:
                raise Stop(f"listing used its share of max_requests ({budget})")
            try:
                page = reader.get_json(f"{publication['base_url']}/api/v1/archive", _archive_params(limit),
                                       ARCHIVE_MAX_AGE_DAYS) or []
            except (httpx.HTTPError, ValueError) as error:  # one publication failing leaves the others
                print(f"Substack subscriptions: {publication['name']}: {type(error).__name__}")
                failed += 1
                continue
            read += 1
            for post in page:
                url = post.get("canonical_url")
                posted = _posted(post)
                if not url or posted is None or posted < since:
                    continue
                posts += 1
                if _seen(connection, url):
                    seen += 1
                    continue
                found.append(_candidate(post, publication))
        complete = not failed
    except Stop as error:
        print(f"Substack subscriptions: stopped listing: {error}; the window is listed again next run")
    if complete:  # a partial listing leaves the window open, so the next run lists it again
        set_metadata(connection, LISTED_KEY, started)
        connection.commit()
    substack._prune(connection)
    print(f"Substack subscriptions: {len(publications)} publications, {read} listed since {since:%Y-%m-%d %H:%M} UTC"
          f"{f', {failed} failed' if failed else ''}, {posts} new posts, {seen} already seen, {len(found)} candidates; "
          f"{reader.requests} requests so far, {reader.cached} answered from cache")
    return found


def _candidate(post: dict, publication: dict) -> Candidate:
    """A post as the substack source lists it, under this source's name, found for the publication it came from."""
    listed = substack.candidate(post, publication, Need(publication["name"]))
    listed.id = f"{NAME}-{post['id']}"
    listed.source = NAME
    return listed


def fetch_text(connection: sqlite3.Connection, settings: Settings, candidate: sqlite3.Row) -> str:
    return substack.read_post(client(connection, settings), candidate)
