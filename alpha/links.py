"""Links you add by hand: a page you found that has alpha, given to Alpha so it can lean on it (`alpha add <url>`,
the dashboard's Add a link, the MCP `add` tool).

It is the strongest curated signal there is (you went and brought it), so it is stored as a curated item of source
`link` at the top of the Feed, rated α at once (the taste profile learns from it; suggestions for it come first), and
pinned to the project you name with a full-score match, so Jev never gets to disagree. The page is read here, now
(its text under the item as content, like a star's README), and then the pipeline does the rest: summary, category,
matches to every other project, a suggestion per match. A note (why it is alpha) becomes the item's own words and,
with a project, the match reason the suggestion is written from.
"""

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import trafilatura

from .enrich import HEADERS, LINK_CHARACTERS, host_of
from .storage import add_content, now, store_item
from .taste import rate

SOURCE = "link"
PINNED_SCORE = 3.0  # relevance.score runs 0 to 3
# Query parameters that only track where a link was clicked; two copies of a page should be one item.
TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|ref|ref_src|igshid|mc_cid|mc_eid|s|t)$")
# X posts: the X source reads a post fully (thread, replies, images); a page fetch gets a login wall.
POST_HOSTS = ("x.com", "twitter.com")
HF_SECTIONS = {"datasets": "dataset", "spaces": "space", "papers": "paper"}
HF_PAGES = {"blog", "docs", "learn", "collections", "models", "tasks", "posts", "pricing", "settings"}


@dataclass
class Page:
    title: str | None
    author: str | None
    date: str | None
    description: str | None
    text: str
    final_url: str
    error: str | None = None


@dataclass
class Added:
    item_id: str
    title: str
    kind: str
    new: bool                    # False: the URL was already an item (any source); it is now rated α and pinned
    text_characters: int
    error: str | None = None     # the page could not be read; the item holds the note and the URL


def normalize(url: str) -> str:
    """One address per page: scheme and host in lower case, no fragment, no tracking parameters."""
    url = url.strip()
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "https://" + url
    parts = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(key)]
    return urlunparse((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.params,
                       urlencode(query), ""))


def kind_of(url: str) -> str:
    """What the page is from its address: a GitHub repository, an arXiv paper, a Hugging Face dataset, model, Space,
    or paper; everything else an article."""
    host, path = host_of(url), [part for part in urlparse(url).path.split("/") if part]
    if host == "github.com" and len(path) >= 2 and path[0] not in ("orgs", "topics", "trending", "features"):
        return "repo"
    if host == "arxiv.org" and path and path[0] in ("abs", "pdf", "html"):
        return "paper"
    if host == "huggingface.co" and path:
        if path[0] in HF_SECTIONS:
            return HF_SECTIONS[path[0]] if len(path) >= 2 else "article"
        if path[0] not in HF_PAGES and len(path) == 2:
            return "model"
    return "article"


def item_id_of(url: str) -> str:
    return f"{SOURCE}-{hashlib.sha1(url.encode()).hexdigest()[:16]}"


def read_page(url: str) -> Page:
    """The page's text and metadata (trafilatura); a failure is kept in `error`, with what was read."""
    try:
        response = httpx.get(url, headers=HEADERS, follow_redirects=True, timeout=30)
        response.raise_for_status()
    except httpx.HTTPError as error:
        return Page(None, None, None, None, "", url, f"{type(error).__name__}: {error}"[:300])
    final_url = str(response.url)
    kind = response.headers.get("content-type", "")
    if "html" not in kind:
        return Page(None, None, None, None, "", final_url, f"not a web page ({kind or 'unknown type'})")
    document = trafilatura.bare_extraction(response.text, url=final_url, with_metadata=True, include_tables=True,
                                           include_comments=False)
    if document is None:
        return Page(None, None, None, None, "", final_url, "no readable text on the page")
    return Page(document.title or None, document.author or None, document.date or None,
                document.description or None, (document.text or "")[:LINK_CHARACTERS], final_url)


def project_named(connection: sqlite3.Connection, name: str | None) -> str | None:
    """The project with a spec spelled like `name` (a match is pinned to it, so it must take a relevance row)."""
    if not name:
        return None
    rows = connection.execute("SELECT name FROM projects").fetchall()
    return next((row["name"] for row in rows if row["name"].lower() == name.strip().lower()), None)


def _pin(connection: sqlite3.Connection, item_id: str, project: str, note: str | None) -> None:
    """A full-score match to `project`. With a note the note is the match reason (the suggestion is written from
    it); without one Claude writes the reason on the next run, as for any match."""
    reason = f"You added this link for {project}: {note}" if note else None
    connection.execute(
        "INSERT INTO relevance (item_id, project, score, confidence, reason, scored_at) VALUES (?, ?, ?, 1.0, ?, ?) "
        "ON CONFLICT (item_id, project) DO UPDATE SET score = excluded.score, confidence = excluded.confidence, "
        "reason = COALESCE(excluded.reason, reason), scored_at = excluded.scored_at",
        (item_id, project, PINNED_SCORE, reason, now()),
    )


def add(connection: sqlite3.Connection, url: str, note: str | None = None, project: str | None = None) -> Added:
    """Store the page at `url` as a curated item at the top of the Feed, rated α, with its text as content; pin it to
    `project` (a name from `projects`; checked by the caller with project_named). A URL Alpha already holds (from
    any source) is not stored twice: that item is rated α and pinned instead."""
    url = normalize(url)
    note = " ".join(note.split()) if note else None
    if host_of(url) in POST_HOSTS:
        raise ValueError("That is an X post: bookmark it on X and the X source reads it fully (thread, replies, images).")
    page = read_page(url)
    if page.error and not note:
        raise ValueError(f"Could not read {url} ({page.error}). Add a note saying what is there, or try again later.")
    existing = connection.execute("SELECT id, title, title_source FROM items WHERE url IN (?, ?)",
                                  (url, page.final_url)).fetchone()
    if existing:
        item_id = existing["id"]
        if note:
            connection.execute("UPDATE items SET raw = json_set(raw, '$.note', ?) WHERE id = ?", (note, item_id))
    else:
        item_id = item_id_of(url)
        rank = connection.execute("SELECT COALESCE(MAX(rank), 0) + 1 FROM items").fetchone()[0]
        store_item(connection, {
            "id": item_id, "source": SOURCE, "kind": kind_of(page.final_url), "trust": "curated", "url": url,
            "title_source": page.title, "author": page.author, "created_at": page.date, "rank": rank,
            "text": note or page.description or page.title or url,
            "raw": {"note": note, "project": project, "final_url": page.final_url, "description": page.description,
                    "added_at": now()},
        })
        add_content(connection, item_id, "article", page.final_url, page.text,
                    {"final_url": page.final_url} | ({"error": page.error} if page.error else {}))
    rate(connection, item_id, "alpha")
    if project:
        _pin(connection, item_id, project, note)
    connection.commit()
    row = connection.execute("SELECT kind, title, title_source FROM items WHERE id = ?", (item_id,)).fetchone()
    return Added(item_id, row["title"] or row["title_source"] or url, row["kind"], existing is None, len(page.text),
                 page.error)
