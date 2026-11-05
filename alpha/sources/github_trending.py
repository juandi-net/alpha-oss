"""GitHub trending (discovered): the repositories trending on GitHub, admitted only when the shared judge finds them
worth it against the user's focus briefs and their taste.

GitHub has no API for trending, so each run reads the pages a person would (checked 2026-11-04): github.com/trending
(today), `?since=weekly` (this week), and the daily page of each language in `languages`. Every repository row is
`<article class="Box-row">` with `<h2 class="h3 lh-condensed"><a href="/owner/repo">`, its numeric id in the
link's click data, a description, the language, total stars and forks, and "N stars today" (or "this week"). A
repository on several pages is one candidate that lists them all. Repositories Alpha already has are skipped: seen by
an earlier run, or starred (github_stars: the same numeric id, or the same address).

The shared judge (judge.py) decides as for the X timelines (TASTE): stage 1 reads the name, the facts line, and the
description; stage 2 reads the repository's description, topics, stars, the stars it gained, and its README through
the GitHub REST API (github_stars' token and README cleaning; two calls). Admitted repositories are items of kind
repo, so like stars they can feed skills.

Politeness: one request at a time, at least `request_delay_seconds` apart, at most `max_requests` per run (pages
and API calls together), the pages read with a browser's User-Agent, every response cached in
`github_trending_cache` (pages for PAGE_MAX_AGE_DAYS, API answers for API_MAX_AGE_DAYS); a 429 or 403 from a page,
or a rate limit from the API, stops GitHub for the rest of the run.
"""

import html
import json
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from ..config import Settings, source_config
from ..judge import Candidate, Need, Stop
from ..storage import now
from . import github_stars

NAME = "github_trending"
TRUST = "discovered"
KINDS = ("repo",)
# Either one signs in for the README calls (github_stars.missing), so neither is required on its own.
SECRETS: tuple[str, ...] = ()
TOOLS: tuple[str, ...] = ()
TASTE = True        # judged against the user's taste as well as the focus briefs
SEARCHES = False    # reads the trending pages, not the needs: open requests never start it
CONFIG = {
    "enabled": False,
    "every_hours": 20,
    "request_delay_seconds": 3.0,             # at least this long (plus up to half again) between requests
    "max_requests": 60,                       # per run: the trending pages, then two API calls per repo judged
    "languages": ["python", "typescript", "swift"],  # daily trending pages read besides the overall ones
    "readme_characters": 12_000,              # README text given to the judge and kept as item text
}

SITE = "https://github.com"
PAGE_MAX_AGE_DAYS = 0.5
API_MAX_AGE_DAYS = 7
CACHE_DAYS = 30                               # older responses are deleted at the end of each run
PREVIEW_CHARACTERS = 1000
BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SCHEMA = """
-- Every trending page and API answer, so nothing is requested twice within its useful life.
CREATE TABLE IF NOT EXISTS github_trending_cache (
    url TEXT PRIMARY KEY,
    status INTEGER NOT NULL,
    body TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""

ROW = '<article class="Box-row">'
HEADING = re.compile(r'<h2 class="h3 lh-condensed">.*?href="/([^"/]+/[^"/]+)"', re.S)
RECORD = re.compile(r"(?:record_id|repository_id)&quot;:(\d+)")
DESCRIPTION = re.compile(r'<p class="col-9[^"]*">(.*?)</p>', re.S)
LANGUAGE = re.compile(r'itemprop="programmingLanguage">([^<]+)<')
GAINED = re.compile(r"([\d,]+) stars? (today|this week|this month)")


@dataclass
class Reader:
    """Trending pages and GitHub API answers, one request at a time, cached, counting requests."""

    connection: sqlite3.Connection
    delay: float
    max_requests: int
    requests: int = 0
    cached: int = 0
    blocked: str | None = None
    github: github_stars._GitHub | None = None
    _last: float = 0.0

    def _cache(self, url: str, max_age_days: float) -> sqlite3.Row | None:
        row = self.connection.execute("SELECT status, body, fetched_at FROM github_trending_cache WHERE url = ?",
                                      (url,)).fetchone()
        if row and (datetime.now(UTC) - datetime.fromisoformat(row["fetched_at"])).total_seconds() / 86400 < max_age_days:
            self.cached += 1
            return row
        return None

    def _store(self, url: str, status: int, body: str) -> None:
        self.connection.execute(
            "INSERT INTO github_trending_cache (url, status, body, fetched_at) VALUES (?, ?, ?, ?) ON CONFLICT(url) "
            "DO UPDATE SET status = excluded.status, body = excluded.body, fetched_at = excluded.fetched_at",
            (url, status, body, now()),
        )
        self.connection.commit()

    def _turn(self) -> None:
        """Wait for this request's turn; stop when GitHub refused or the budget is spent."""
        if self.blocked:
            raise Stop(self.blocked)
        if self.requests >= self.max_requests:
            raise Stop(f"{self.max_requests} GitHub requests made this run")
        wait = self._last + self.delay * random.uniform(1, 1.5) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.requests += 1

    def page(self, path: str) -> str:
        """A trending page's HTML."""
        url = f"{SITE}{path}"
        if row := self._cache(url, PAGE_MAX_AGE_DAYS):
            return row["body"]
        self._turn()
        try:
            response = httpx.get(url, headers=BROWSER, follow_redirects=True, timeout=30)
        finally:
            self._last = time.monotonic()
        if response.status_code in (403, 429):
            self.blocked = f"github.com answered {response.status_code} for a trending page"
            raise Stop(self.blocked)
        response.raise_for_status()
        self._store(url, response.status_code, response.text)
        return response.text

    def api(self, path: str, accept: str) -> str | None:
        """An API answer's body, or None for 404 (a deleted or renamed repository, or no README)."""
        url = f"{github_stars.API}{path}"
        if row := self._cache(url, API_MAX_AGE_DAYS):
            return row["body"] if row["status"] == 200 else None
        self._turn()
        if self.github is None:
            self.github = github_stars._GitHub()
        try:
            response = self.github.get(path, accept)
        except github_stars.Stopped as error:
            self.blocked = f"GitHub API {error}"
            raise Stop(self.blocked) from error
        finally:
            self._last = time.monotonic()
        self._store(url, 200 if response else 404, response.text if response else "")
        return response.text if response else None


_reader: Reader | None = None


def missing() -> list[str]:
    return github_stars.missing()


def client(connection: sqlite3.Connection, settings: Settings) -> Reader:
    """One reader per run, so the pages and stage 2 share one budget (close() reports it)."""
    global _reader
    if _reader is None or _reader.connection is not connection:
        options = source_config(settings, NAME, CONFIG)
        connection.executescript(SCHEMA)
        _reader = Reader(connection, float(options["request_delay_seconds"]), int(options["max_requests"]))
    return _reader


def close() -> None:
    global _reader
    if _reader is not None:
        print(f"GitHub trending: {_reader.requests} requests this run, {_reader.cached} answered from cache")
        cutoff = datetime.fromtimestamp(time.time() - CACHE_DAYS * 86400, UTC).isoformat(timespec="seconds")
        _reader.connection.execute("DELETE FROM github_trending_cache WHERE fetched_at < ?", (cutoff,))
        _reader.connection.commit()
        if _reader.github:
            _reader.github.close()
        _reader = None


def _plain(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def _count(text: str | None) -> int:
    return int(text.replace(",", "")) if text else 0


def parse(page: str) -> list[dict]:
    """The repositories on a trending page, in its order."""
    repos = []
    for block in page.split(ROW)[1:]:
        heading = HEADING.search(block)
        if not heading:
            continue
        full_name = heading[1]
        record = RECORD.search(block)
        description = DESCRIPTION.search(block)
        language = LANGUAGE.search(block)
        counts = {kind: re.search(rf'href="/{re.escape(full_name)}/{kind}"[^>]*>.*?([\d,]+)\s*</a>', block, re.S)
                  for kind in ("stargazers", "forks")}
        gained = GAINED.search(block)
        repos.append({
            "id": int(record[1]) if record else None, "full_name": full_name,
            "description": _plain(description[1]) if description else "",
            "language": language[1].strip() if language else None,
            "stars": _count(counts["stargazers"][1] if counts["stargazers"] else None),
            "forks": _count(counts["forks"][1] if counts["forks"] else None),
            "gained": {gained[2]: _count(gained[1])} if gained else {},
        })
    return repos


def pages(options: dict) -> list[tuple[str, str]]:
    """(which list, path): today, this week, then each language's daily page."""
    return ([("today", "/trending"), ("this week", "/trending?since=weekly")]
            + [(language, f"/trending/{quote(str(language).lower())}") for language in options["languages"]])


def candidate_id(repo: dict) -> str:
    """The numeric repo id, as github_stars uses it (it survives renames)."""
    return f"{NAME}-{repo['id']}" if repo["id"] else f"{NAME}-{repo['full_name'].replace('/', '--')}"


def known(connection: sqlite3.Connection, repo: dict) -> bool:
    """Seen by an earlier run, or starred (same numeric id, or same address)."""
    if connection.execute("SELECT 1 FROM candidates WHERE id = ?", (candidate_id(repo),)).fetchone():
        return True
    return bool(connection.execute(
        "SELECT 1 FROM items WHERE id IN (?, ?) OR lower(url) = ?",
        (candidate_id(repo), f"{github_stars.NAME}-{repo['id']}", f"{SITE}/{repo['full_name']}".lower()),
    ).fetchone())


def trend_line(raw: dict) -> str:
    gained = ", ".join(f"{count:,} stars {period}" for period, count in raw["gained"].items())
    lists = ", ".join(raw["lists"])
    return f"Trending on GitHub ({lists})" + (f": {gained}" if gained else "")


def discover(connection: sqlite3.Connection, settings: Settings, needs: list[Need]) -> list[Candidate]:
    options = source_config(settings, NAME, CONFIG)
    reader = client(connection, settings)
    merged: dict[str, dict] = {}
    listed = read = 0
    try:
        for name, path in pages(options):
            repos = parse(reader.page(path))
            read += 1
            if not repos:
                print(f"GitHub trending: no repositories found on {path} (the page layout may have changed)")
            for repo in repos:
                listed += 1
                entry = merged.setdefault(repo["full_name"], repo | {"lists": [], "gained": {}})
                entry["lists"].append(name)
                for period, count in repo["gained"].items():
                    entry["gained"][period] = max(count, entry["gained"].get(period, 0))
    except Stop as error:
        print(f"GitHub trending: stopped reading: {error}")
    fresh = [repo for repo in merged.values() if not known(connection, repo)]
    found = []
    for repo in fresh:
        owner = repo["full_name"].split("/")[0]
        facts = [f"GitHub repo {repo['full_name']}", repo["language"], f"{repo['stars']:,} stars",
                 trend_line(repo)]
        found.append(Candidate(
            id=candidate_id(repo), source=NAME, kind="repo", url=f"{SITE}/{repo['full_name']}",
            title=repo["full_name"], subtitle=" · ".join(fact for fact in facts if fact), author=owner,
            author_url=f"{SITE}/{owner}", posted_at=now(), preview=repo["description"][:PREVIEW_CHARACTERS],
            raw=repo, need=Need(f"trending {repo['lists'][0]}"),
        ))
    print(f"GitHub trending: {read} pages, {listed} rows, {len(merged)} repositories, "
          f"{len(merged) - len(fresh)} already known, {len(found)} candidates; "
          f"{reader.requests} requests so far, {reader.cached} answered from cache")
    return found


def fetch_text(connection: sqlite3.Connection, settings: Settings, candidate: sqlite3.Row) -> str:
    """Description, topics, language, stars, and what it gained on the trending lists, then the README."""
    raw = json.loads(candidate["raw"])
    reader = client(connection, settings)
    full_name = raw["full_name"]
    body = reader.api(f"/repos/{full_name}", "application/vnd.github+json")
    repo = json.loads(body) if body else {}
    about = github_stars._about({"repo": {
        "full_name": full_name, "description": repo.get("description") or raw["description"],
        "topics": repo.get("topics"), "language": repo.get("language") or raw["language"],
        "stargazers_count": repo.get("stargazers_count") or raw["stars"], "pushed_at": repo.get("pushed_at"),
        "archived": repo.get("archived"),
    }})
    details = [trend_line(raw)]
    if (repo.get("license") or {}).get("spdx_id"):
        details.append(f"License: {repo['license']['spdx_id']}")
    if repo.get("created_at"):
        details.append(f"Created {repo['created_at'][:10]}")
    if repo.get("homepage"):
        details.append(f"Homepage: {repo['homepage']}")
    readme = reader.api(f"/repos/{full_name}/readme", "application/vnd.github.raw+json")
    characters = int(source_config(settings, NAME, CONFIG)["readme_characters"])
    text = "\n".join([about, *details])
    return text + (f"\n\nREADME:\n{github_stars._clean(readme)[:characters]}" if readme else "\n\nNo README.")
