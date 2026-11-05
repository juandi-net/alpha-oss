"""GitHub stars (curated): the repositories the user starred, read from the GitHub REST API.

Signed in with the GitHub CLI's token (`gh auth login` once; `gh auth token` hands it over) or, without gh, a
GITHUB_TOKEN in .env (a fine-grained token with no extra permissions reads public stars). Both make the same
REST calls: `GET /user/starred` with the star media type (it adds `starred_at`), and `GET /repos/<repo>/readme`
raw.

`collect` reads stars newest first, 100 per call, until it reaches a stored one (a first run reads them all)
and ranks the new ones above every other item, in the order they were starred (on the first import, stars
older than RECENT_DAYS go below every other item instead). `enrich` fetches each new
repo's README and adds it, cleaned and capped, to the item's text after the description and topics.
Repos are kind "repo", so the tools and techniques the user starred can feed skills; curated, so no judge.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta
from functools import cache

import httpx

from ..config import Settings, source_config
from ..storage import now, store_item

NAME = "github_stars"
TRUST = "curated"
KINDS = ("repo",)
# Either one signs in (see missing()), so neither is required on its own.
SECRETS: tuple[str, ...] = ()
TOOLS: tuple[str, ...] = ()
CONFIG = {
    "enabled": False,
    "every_hours": 6,
    "readmes": 300,               # READMEs fetched per run (a larger backlog continues next run)
    "readme_characters": 12_000,  # README text kept per repo
}

API = "https://api.github.com"
PER_PAGE = 100
PAUSE_SECONDS = 0.5  # between calls; GitHub allows 5,000 an hour, a run makes a few hundred at most
RECENT_DAYS = 7  # on the first import, stars this recent go to the top of the feed; older ones to the bottom

SCHEMA = """
CREATE TABLE IF NOT EXISTS github_stars_readmes (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    status TEXT NOT NULL,         -- ok | none (the repo has no README)
    characters INTEGER NOT NULL,  -- README characters kept
    fetched_at TEXT NOT NULL
);
"""


class Stopped(Exception):
    """GitHub refused (rate limit, abuse limit); everything stored so far is kept and the next run continues."""


@cache
def _token() -> str | None:
    if token := os.environ.get("GITHUB_TOKEN"):
        return token
    if shutil.which("gh"):
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def missing() -> list[str]:
    """What this source still needs: a signed-in GitHub CLI or a GITHUB_TOKEN."""
    return [] if _token() else ["GITHUB_TOKEN (or gh on PATH, signed in with `gh auth login`)"]


class _GitHub:
    """The REST API with the user's token, one call at a time, counting calls."""

    def __init__(self) -> None:
        token = _token()
        if not token:
            raise SystemExit(f"GitHub stars: no {missing()[0]}")
        self.calls = 0
        self.client = httpx.Client(
            base_url=API, timeout=30, follow_redirects=True,
            headers={"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "alpha"},
        )

    def get(self, path: str, accept: str, params: dict | None = None) -> httpx.Response | None:
        """The response, or None for 404. Raises Stopped when GitHub limits the account."""
        if self.calls:
            time.sleep(PAUSE_SECONDS)
        self.calls += 1
        response = self.client.get(path, params=params, headers={"Accept": accept})
        if response.status_code == 404:
            return None
        if response.status_code == 401:
            raise SystemExit("GitHub stars: the token was refused (run `gh auth login` or replace GITHUB_TOKEN)")
        if response.status_code in (403, 429) and (
            response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers
        ):
            raise Stopped(f"rate limited (HTTP {response.status_code}, resets {response.headers.get('x-ratelimit-reset')})")
        response.raise_for_status()
        return response

    def close(self) -> None:
        self.client.close()


def _item_id(repo: dict) -> str:
    """The numeric repo id: it survives renames and transfers."""
    return f"{NAME}-{repo['id']}"


def _raw(star: dict) -> dict:
    """The star without the API's URL templates and owner profile."""
    repo = star["repo"]
    keep = ("id", "full_name", "description", "topics", "language", "stargazers_count", "html_url", "homepage",
            "created_at", "pushed_at", "archived", "fork", "default_branch")
    return {"starred_at": star["starred_at"], "repo": {key: repo.get(key) for key in keep}
            | {"owner": (repo.get("owner") or {}).get("login"), "license": (repo.get("license") or {}).get("spdx_id")}}


def _about(raw: dict) -> str:
    """Description, topics, language, and activity: the item's text until the README is added."""
    repo = raw["repo"]
    lines = [f"{repo['full_name']}: {repo.get('description') or 'no description'}"]
    if repo.get("topics"):
        lines.append("Topics: " + ", ".join(repo["topics"]))
    facts = [repo.get("language"), f"{repo.get('stargazers_count') or 0} stars",
             f"last push {(repo.get('pushed_at') or '')[:10]}" if repo.get("pushed_at") else None,
             "archived" if repo.get("archived") else None]
    lines.append(" · ".join(fact for fact in facts if fact))
    return "\n".join(lines)


def _rank(connection: sqlite3.Connection, newest_first: list[tuple[str, str]], first_import: bool) -> None:
    """New stars, given as (item id, starred_at) newest first, go above every other item in the order starred:
    they were starred since the last run. On the first import only the last RECENT_DAYS of stars are new;
    older ones go below every other item, still in the order starred, so a backlog does not bury the feed."""
    cutoff = datetime.now(UTC) - timedelta(days=RECENT_DAYS)
    top = [item_id for item_id, starred_at in newest_first
           if not first_import or datetime.fromisoformat(starred_at) >= cutoff]
    kept = set(top)
    below = [item_id for item_id, _ in newest_first if item_id not in kept]
    ids = top + below
    highest, lowest = connection.execute(
        f"SELECT COALESCE(MAX(rank), 0), COALESCE(MIN(rank), 0) FROM items WHERE id NOT IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchone()
    connection.executemany(
        "UPDATE items SET rank = ? WHERE id = ?",
        [(highest + len(top) - index, item_id) for index, item_id in enumerate(top)]
        + [(lowest - 1 - index, item_id) for index, item_id in enumerate(below)],
    )


def fetch_stars(connection: sqlite3.Connection, github: _GitHub) -> tuple[int, int]:
    """Store new stars, newest first, until a stored one. Returns (read, new)."""
    read = 0
    first_import = not connection.execute("SELECT 1 FROM items WHERE source = ? LIMIT 1", (NAME,)).fetchone()
    newest_first: list[tuple[str, str]] = []
    page = 1
    known = False
    while not known:
        response = github.get("/user/starred", "application/vnd.github.star+json",
                              {"per_page": PER_PAGE, "page": page, "sort": "created", "direction": "desc"})
        stars = response.json() if response else []
        for star in stars:
            read += 1
            raw = _raw(star)
            repo = raw["repo"]
            if not store_item(connection, {
                "id": _item_id(repo), "source": NAME, "kind": "repo", "trust": TRUST, "url": repo["html_url"],
                "title_source": repo["full_name"], "author": repo["owner"], "created_at": star["starred_at"],
                "text": _about(raw), "raw": raw,
            }):
                known = True
                break
            newest_first.append((_item_id(repo), star["starred_at"]))
        if len(stars) < PER_PAGE:
            break
        page += 1
    if newest_first:
        _rank(connection, newest_first, first_import)
    connection.commit()
    return read, len(newest_first)


def _clean(readme: str) -> str:
    """Markdown without HTML, images, and badge clutter."""
    text = re.sub(r"<!--.*?-->", "", readme, flags=re.S)
    text = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", "", text)  # linked badges
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)               # images
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_readmes(connection: sqlite3.Connection, github: _GitHub, *, limit: int, characters: int) -> tuple[int, int]:
    """README of each star not yet checked, newest first, at most `limit`. Returns (fetched, still waiting)."""
    connection.executescript(SCHEMA)
    rows = connection.execute(
        "SELECT id, raw FROM items i WHERE source = ? AND NOT EXISTS "
        "(SELECT 1 FROM github_stars_readmes r WHERE r.item_id = i.id) ORDER BY rank DESC",
        (NAME,),
    ).fetchall()
    fetched = 0
    for row in rows[:limit]:
        raw = json.loads(row["raw"])
        response = github.get(f"/repos/{raw['repo']['full_name']}/readme", "application/vnd.github.raw+json")
        readme = _clean(response.text)[:characters] if response else ""
        if readme:
            connection.execute("UPDATE items SET text = ? WHERE id = ?", (f"{_about(raw)}\n\nREADME:\n{readme}", row["id"]))
            fetched += 1
        connection.execute(
            "INSERT OR REPLACE INTO github_stars_readmes (item_id, status, characters, fetched_at) VALUES (?, ?, ?, ?)",
            (row["id"], "ok" if readme else "none", len(readme), now()),
        )
        connection.commit()
    return fetched, max(len(rows) - limit, 0)


def collect(connection: sqlite3.Connection, settings: Settings) -> dict:
    """Read new stars. A rate limit stops the run (`stopped` in the stats); the next run continues."""
    github = _GitHub()
    try:
        read, new = fetch_stars(connection, github)
    except Stopped as error:
        print(f"GitHub stars: {error}; stopped until the next run")
        return {"stopped": str(error)}
    finally:
        github.close()
    print(f"GitHub stars: read {read} stars, {new} new ({github.calls} API calls)")
    return {"read": read, "new": new}


def enrich(connection: sqlite3.Connection, settings: Settings) -> None:
    """READMEs of up to `readmes` new stars."""
    options = source_config(settings, NAME, CONFIG)
    github = _GitHub()
    try:
        fetched, waiting = fetch_readmes(connection, github, limit=options["readmes"],
                                         characters=options["readme_characters"])
        print(f"GitHub stars: {fetched} READMEs ({github.calls} API calls)"
              + (f"; {waiting} more next run" if waiting else ""))
    except Stopped as error:
        print(f"GitHub stars: {error}; READMEs continue next run")
    finally:
        github.close()
