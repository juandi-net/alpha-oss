"""Papers (discovered): research papers that ship something usable — a code repository, or models or datasets on
Hugging Face. Papers that ship nothing are dropped before the shared judge (judge.py) sees them.

Read through Hugging Face's free public JSON endpoints (no key; checked 2026-11-04):
- `huggingface.co/api/daily_papers?limit=N` lists the community-picked Daily Papers, newest first; read once per
  pass (the source's cadence is daily). Each paper carries its arXiv id (`paper.id`), abstract (`paper.summary`),
  `ai_summary`, and, when known, `githubRepo` with `githubStars` and `projectPage`.
- `huggingface.co/api/papers/search?q=Q&limit=N` searches papers (semantic); used for the needs (brief queries,
  searched again after `requery_days`, and open agent requests).
- `huggingface.co/api/papers/<arxiv id>` adds `linkedModels`, `linkedDatasets`, `linkedSpaces` (repos on the Hub
  that cite the paper) and their totals. It is read only for papers whose listing has no `githubRepo`, and for
  survivors whose text the judge reads.
- `api.github.com/repos/<owner>/<repo>/readme` (raw) gives the README, whose opening goes into the text. GitHub
  allows 60 such requests an hour without a key; when it refuses, the text goes without the README.
PDFs are never downloaded.

Politeness: one request at a time, at least `request_delay_seconds` apart, at most `max_requests` per run (half for
discovery, the rest for reading survivors), every answer cached in `papers_cache`, and a 429 or 403 from Hugging
Face stops the source for the rest of the run.
"""

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from urllib.parse import urlencode, urlsplit

import httpx

from ..config import Settings, source_config
from ..judge import Candidate, Need, Stop
from ..storage import now

NAME = "papers"
TRUST = "discovered"
KINDS = ("paper",)
SECRETS: tuple[str, ...] = ()
TOOLS: tuple[str, ...] = ()
CONFIG = {
    "enabled": False,
    "every_hours": 20,              # one pass a day; each pass reads the Daily Papers list
    "request_delay_seconds": 1.0,   # at least this long between requests
    "max_requests": 150,            # Hugging Face and GitHub requests per run; the rest waits for the next run
    "daily_papers": 50,             # papers read from the Daily Papers list per pass
    "results_per_query": 8,         # search results per need
    "requery_days": 7,              # a query is searched again after this many days
}

# Tables only this source uses.
SCHEMA = """
-- Every answer (200 or 404), so nothing is requested twice within its useful life.
CREATE TABLE IF NOT EXISTS papers_cache (
    url TEXT PRIMARY KEY,
    status INTEGER NOT NULL,
    body TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""

HF = "https://huggingface.co"
DAILY_URL = f"{HF}/api/daily_papers"
SEARCH_URL = f"{HF}/api/papers/search"
PAPER_URL = f"{HF}/api/papers/{{}}"
README_URL = "https://api.github.com/repos/{}/{}/readme"
HEADERS = {"User-Agent": "alpha-papers (https://github.com; polite research discovery)"}
README_HEADERS = HEADERS | {"Accept": "application/vnd.github.raw+json"}
# The Daily Papers list changes during the day; search results slowly; links to a paper grow as people release.
DAILY_MAX_AGE_DAYS = 0.5
SEARCH_MAX_AGE_DAYS = 3
PAPER_MAX_AGE_DAYS = 7
README_MAX_AGE_DAYS = 30
# Answers older than this are deleted after each pass (an admitted item keeps its text in items).
CACHE_DAYS = 30
PREVIEW_CHARACTERS = 1500
README_CHARACTERS = 4000
LISTED_RELEASES = 5             # models, datasets, spaces named per kind in the text


@dataclass
class Client:
    connection: sqlite3.Connection
    delay: float
    max_requests: int
    requests: int = 0
    cached: int = 0
    blocked: dict[str, str] = field(default_factory=dict)  # host -> why; no request goes to it after
    _last: float = 0.0

    def get(self, url: str, params: dict | None = None, max_age_days: float | None = None,
            headers: dict | None = None, optional: bool = False) -> str | None:
        """The body of a 200 answer; None for a 404 (or, `optional`, when the host refuses). max_age_days None: a
        cached answer never expires."""
        if params:
            url = f"{url}?{urlencode(params)}"
        host = urlsplit(url).hostname or ""
        row = self.connection.execute("SELECT status, body, fetched_at FROM papers_cache WHERE url = ?", (url,)).fetchone()
        if row:
            age = (datetime.now(UTC) - datetime.fromisoformat(row["fetched_at"])).total_seconds() / 86400
            if max_age_days is None or age < max_age_days:
                self.cached += 1
                return row["body"] if row["status"] == 200 else None
        if host in self.blocked:
            if optional:
                return None
            raise Stop(self.blocked[host])
        if self.requests >= self.max_requests:
            raise Stop(f"{self.max_requests} paper requests made this run")
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            response = httpx.get(url, headers=headers or HEADERS, follow_redirects=True, timeout=30)
        finally:
            self._last = time.monotonic()
            self.requests += 1
        if response.status_code in (403, 429):
            self.blocked[host] = f"{response.status_code} from {host}"
            if optional:
                return None
            raise Stop(self.blocked[host])
        if response.status_code not in (200, 404):
            response.raise_for_status()
        self.connection.execute(
            "INSERT INTO papers_cache (url, status, body, fetched_at) VALUES (?, ?, ?, ?) ON CONFLICT(url) DO UPDATE "
            "SET status = excluded.status, body = excluded.body, fetched_at = excluded.fetched_at",
            (url, response.status_code, response.text, now()),
        )
        self.connection.commit()
        return response.text if response.status_code == 200 else None

    def daily(self, limit: int) -> list[dict]:
        return json.loads(self.get(DAILY_URL, {"limit": limit}, DAILY_MAX_AGE_DAYS) or "[]")

    def search(self, query: str, limit: int) -> list[dict]:
        return json.loads(self.get(SEARCH_URL, {"q": query, "limit": limit}, SEARCH_MAX_AGE_DAYS) or "[]")

    def paper(self, arxiv_id: str) -> dict | None:
        body = self.get(PAPER_URL.format(arxiv_id), max_age_days=PAPER_MAX_AGE_DAYS)
        return json.loads(body) if body else None

    def readme(self, repo_url: str) -> str | None:
        repo = github_repo(repo_url)
        if not repo:
            return None
        return self.get(README_URL.format(*repo), max_age_days=README_MAX_AGE_DAYS, headers=README_HEADERS,
                        optional=True)


@cache
def _client(connection: sqlite3.Connection, delay: float, max_requests: int) -> Client:
    """One client per run (process and database connection), so discovery and reading share one budget."""
    connection.executescript(SCHEMA)
    return Client(connection, delay, max_requests)


def client(connection: sqlite3.Connection, settings: Settings) -> Client:
    options = source_config(settings, NAME, CONFIG)
    return _client(connection, float(options["request_delay_seconds"]), int(options["max_requests"]))


def discover(connection: sqlite3.Connection, settings: Settings, needs: list[Need]) -> list[Candidate]:
    """Searches for the open requests, the Daily Papers list, then searches for the brief queries, within half the
    request budget. Only papers that ship code, models or datasets become candidates."""
    options = source_config(settings, NAME, CONFIG)
    hub = client(connection, settings)
    budget = int(options["max_requests"]) // 2      # the other half reads survivors
    per_query = int(options["results_per_query"])
    known = {row[0] for row in connection.execute(
        "SELECT id FROM candidates WHERE source = ? UNION SELECT id FROM items WHERE source = ?", (NAME, NAME))}
    stats = {"listed": 0, "known": 0, "with code": 0, "with models or datasets": 0, "dropped": 0, "unchecked": 0}
    found: list[Candidate] = []
    searched = 0

    def keep(listing: list[dict], need: Need | None) -> None:
        """The listing's new papers that ship something, checking the Hub links of those without a repo."""
        for entry in listing:
            paper = entry.get("paper") or {}
            if not paper.get("id"):
                continue
            stats["listed"] += 1
            if candidate_id(paper["id"]) in known:
                stats["known"] += 1
                continue
            links = None
            if not paper.get("githubRepo"):
                if hub.requests >= budget:
                    stats["unchecked"] += 1
                    continue
                try:
                    links = hub.paper(paper["id"]) or {}
                except (httpx.HTTPError, ValueError):  # checked again when it is listed again
                    stats["unchecked"] += 1
                    continue
                if links.get("githubRepo"):
                    paper = paper | {key: links.get(key) for key in ("githubRepo", "githubStars", "projectPage")}
                elif not (links.get("numTotalModels") or links.get("numTotalDatasets")):
                    stats["dropped"] += 1
                    continue
            stats["with code" if paper.get("githubRepo") else "with models or datasets"] += 1
            known.add(candidate_id(paper["id"]))
            found.append(candidate(paper, links, need))

    # An agent waiting on a request comes first; None stands for the Daily Papers list.
    order = [*(need for need in needs if need.request_id), None, *(need for need in needs if not need.request_id)]
    try:
        for need in order:
            if need is None:
                try:
                    keep(hub.daily(int(options["daily_papers"])), None)
                except (httpx.HTTPError, ValueError) as error:  # read again next run
                    print(f"Papers: the Daily Papers list failed: {type(error).__name__}")
                continue
            if hub.requests + 1 + per_query > budget:
                break
            try:
                keep(hub.search(need.query, per_query), need)
            except (httpx.HTTPError, ValueError) as error:  # tried again next run
                print(f"Papers: a search failed: {type(error).__name__}")
                continue
            need.searched = True
            searched += 1
    except Stop as error:
        print(f"Papers: stopped searching: {error}")
    _prune(connection)
    print(f"Papers: Daily Papers and {searched} searches; " + ", ".join(f"{key} {value}" for key, value in stats.items())
          + f"; {hub.requests} requests so far, {hub.cached} answered from cache")
    return found


def fetch_text(connection: sqlite3.Connection, settings: Settings, candidate: sqlite3.Row) -> str:
    """Title, abstract, what was released (repo, models, datasets, spaces), and the opening of the repo's README."""
    raw = json.loads(candidate["raw"])
    hub = client(connection, settings)
    paper = hub.paper(raw["arxiv_id"]) or {}
    repo_url = paper.get("githubRepo") or raw.get("github_repo")
    parts = [
        candidate["title"],
        f"arXiv {raw['arxiv_id']} · {raw['hf_url']}",
        f"Authors: {', '.join(raw.get('authors') or [])}" if raw.get("authors") else "",
        f"Abstract:\n{(paper.get('summary') or raw.get('abstract') or '').strip()}",
        "Released:\n" + "\n".join(f"- {line}" for line in releases(paper, raw)),
    ]
    readme = hub.readme(repo_url) if repo_url else None
    if readme:
        parts.append(f"README of {'/'.join(github_repo(repo_url))} (opening):\n{readme_opening(readme)}")
    elif repo_url:
        parts.append("[The repository's README could not be read.]")
    return "\n\n".join(part for part in parts if part)


def _prune(connection: sqlite3.Connection) -> None:
    """Forget old answers, so the database and its daily backup stay small."""
    cutoff = datetime.fromtimestamp(time.time() - CACHE_DAYS * 86400, UTC).isoformat(timespec="seconds")
    connection.execute("DELETE FROM papers_cache WHERE fetched_at < ?", (cutoff,))
    connection.commit()


def candidate_id(arxiv_id: str) -> str:
    return f"{NAME}-{arxiv_id.replace('/', '--')}"


def github_repo(url: str | None) -> tuple[str, str] | None:
    """(owner, name) of a github.com repository URL."""
    parts = urlsplit(url or "")
    if parts.hostname not in ("github.com", "www.github.com"):
        return None
    path = [piece for piece in parts.path.split("/") if piece]
    if len(path) < 2:
        return None
    return path[0], path[1].removesuffix(".git")


def released(paper: dict, links: dict | None) -> str:
    """One line on what ships with the paper, for the preview Jev fits against the briefs."""
    shipped = []
    if paper.get("githubRepo"):
        shipped.append(f"code {paper['githubRepo']} ({paper.get('githubStars') or 0} stars)")
    for key, label in (("numTotalModels", "models"), ("numTotalDatasets", "datasets")):
        if (links or {}).get(key):
            shipped.append(f"{links[key]} {label} on Hugging Face")
    return "Released: " + "; ".join(shipped) if shipped else ""


def candidate(paper: dict, links: dict | None, need: Need | None) -> Candidate:
    """A listed paper (with its Hub links when they were read) as a candidate; the facts are kept as raw."""
    arxiv_id = paper["id"]
    organization = paper.get("organization") or {}
    authors = [author["name"] for author in paper.get("authors") or [] if author.get("name")]
    author = organization.get("fullname") or (f"{authors[0]} et al." if len(authors) > 1 else (authors or [None])[0])
    abstract = re.sub(r"\s+", " ", paper.get("summary") or "").strip()
    line = released(paper, links)
    return Candidate(
        id=candidate_id(arxiv_id),
        source=NAME,
        kind="paper",
        url=f"https://arxiv.org/abs/{arxiv_id}",
        title=re.sub(r"\s+", " ", paper.get("title") or "").strip() or arxiv_id,
        subtitle=(paper.get("ai_summary") or "").strip(),
        author=author,
        author_url=f"{HF}/{organization['name']}" if organization.get("name") else None,
        posted_at=paper.get("publishedAt"),
        preview="\n\n".join(filter(None, (abstract[:PREVIEW_CHARACTERS], line))),
        raw={
            "arxiv_id": arxiv_id,
            "hf_url": f"{HF}/papers/{arxiv_id}",
            "authors": authors[:12],
            "abstract": abstract,
            "github_repo": paper.get("githubRepo"),
            "github_stars": paper.get("githubStars"),
            "project_page": paper.get("projectPage"),
            "upvotes": paper.get("upvotes"),
            "keywords": paper.get("ai_keywords") or [],
            "organization": organization.get("name"),
            "models": (links or {}).get("numTotalModels"),
            "datasets": (links or {}).get("numTotalDatasets"),
            "listed_in": "search" if need else "daily",
        },
        need=need,
    )


def releases(paper: dict, raw: dict) -> list[str]:
    """What ships with the paper, from its Hub page (the listing's facts when the page could not be read)."""
    lines = []
    repo = paper.get("githubRepo") or raw.get("github_repo")
    if repo:
        stars = paper.get("githubStars", raw.get("github_stars"))
        lines.append(f"Code: {repo}" + (f" ({stars} stars)" if stars is not None else ""))
    for key, total, label in (("linkedModels", "numTotalModels", "Models"), ("linkedDatasets", "numTotalDatasets", "Datasets"),
                              ("linkedSpaces", "numTotalSpaces", "Demos (Spaces)")):
        named = paper.get(key) or []
        count = paper.get(total, raw.get(total.removeprefix("numTotal").lower()))
        if not (count or named):
            continue
        listed = ", ".join(describe(repo) for repo in named[:LISTED_RELEASES])
        lines.append(f"{label} on Hugging Face ({count or len(named)}): {listed}".rstrip(": "))
    page = paper.get("projectPage") or raw.get("project_page")
    if page:
        lines.append(f"Project page: {page}")
    return lines or ["(nothing listed)"]


def describe(repo: dict) -> str:
    """A linked Hub repository as its address, with its task and downloads when known."""
    prefix = {"dataset": "datasets/", "space": "spaces/"}.get(repo.get("repoType"), "")
    details = [repo["pipeline_tag"]] if repo.get("pipeline_tag") else []
    if repo.get("downloads"):
        details.append(f"{repo['downloads']} downloads")
    return f"{HF}/{prefix}{repo.get('id')}" + (f" ({', '.join(details)})" if details else "")


def readme_opening(markdown: str) -> str:
    """The README's first prose, without badges, images and HTML, cut at a line near README_CHARACTERS."""
    lines, size = [], 0
    for line in markdown.splitlines():
        bare = line.strip()
        if bare.startswith(("<", "![", "[![")) or re.fullmatch(r"(\[!\[.*?\]\(.*?\)\]\(.*?\)\s*)+", bare):
            continue
        if not bare and (not lines or not lines[-1]):
            continue
        lines.append(line.rstrip())
        size += len(line) + 1
        if size >= README_CHARACTERS:
            break
    return "\n".join(lines).strip()
