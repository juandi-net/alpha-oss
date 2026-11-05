"""Alpha learns from Let There: every note the user makes on one of their apps, and what Let There built from it.

Let There (its own tool) is a layer over the apps the user builds: rest the pointer on something, press the key, say
what should change. Its own server keeps the note as a markdown file with a picture of the element and, with its
--build, turns it into a branch of the app's repository by itself; it tells Alpha about each note
(`--notify http://localhost:<port>/letthere`), and `alpha run` asks it for every note as well, so Alpha sees the
outcome (branch, files, the agent's report) once a build ends.

For each note whose app is one of the user's projects (the script tag's data-app, matched to a project name):
- the note becomes an item (source letthere, kind note; the user's own words about their app, so it is summarized
  and matched by hand here and the pipeline leaves it alone; the Feed hides the source) with the element's picture
  under var/media/letthere;
- and a suggestion for that project (basis_kind note, rated α, the words as its basis), the same card as a Your idea
  marked Let There, whose implement_* columns mirror the note's build (branch, worktree, report, status), so the
  Ideas tab, Review changes, Ask about this, Mark done, and MCP all apply; Mark done sets the note's status to
  done. Alpha never builds a note itself: Let There does, where the app lives.
From there the note and its build feed the focus brief, the taste profile, and the context new suggestions are
written from (focus.alpha_loop, taste.py, pipeline.project_loop).
"""

import json
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from .config import Settings
from .pipeline import project_texts
from .storage import now, set_metadata
from .taste import RATED_AT

SOURCE = "letthere"
BASIS_KIND = "note"
HEADLINE_WORDS = 12
TIMEOUT = 10
# A note's status (Let There) as Alpha's implement_status.
BUILD_STATUS = {"implementing": "implementing", "implemented": "implemented", "failed": "failed"}


def item_id_of(note: dict) -> str:
    return f"{SOURCE}-{note['app']}-{note['id']}"


def note_of(item_id: str) -> tuple[str, str] | None:
    """(app, note id) of a Let There item, else None."""
    match = re.fullmatch(rf"{SOURCE}-([\w.-]+)-(\d{{4}}-\d{{2}}-\d{{2}}-\d{{6}}.*)", item_id)
    return (match.group(1), match.group(2)) if match else None


def _request(settings: Settings, path: str, body: dict | None = None) -> dict | list | bytes | None:
    if not settings.let_there_url:
        return None
    request = urllib.request.Request(f"{settings.let_there_url}{path}", method="POST" if body else "GET",
                                     data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = response.read()
            return json.loads(data) if response.headers.get_content_type() == "application/json" else data
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"Let There: {path}: {error}", flush=True)
        return None


def set_status(settings: Settings, item_id: str, status: str) -> None:
    """Tell Let There the user's word on a note (done); nothing when the item is not a note or the server is away."""
    if found := note_of(item_id):
        app, note_id = found
        _request(settings, f"/notes/{app}/{note_id}/status", {"status": status})


def _project_for(connection: sqlite3.Connection, app: str) -> str | None:
    """The project the app's name means: one with a spec or a focus brief (own_idea.projects), spelled loosely."""
    normal = lambda name: re.sub(r"[^a-z0-9]", "", name.lower())  # noqa: E731
    return next((name for name in project_texts(connection) if normal(name) == normal(app)), None)


def _headline(said: str) -> str:
    first = " ".join(said.strip().splitlines()[0].split()).rstrip(".")
    words = first.split()
    return " ".join(words[:HEADLINE_WORDS]) + ("…" if len(words) > HEADLINE_WORDS else "")


def _picture(settings: Settings, note: dict) -> str | None:
    """The note's picture, copied under var/media/letthere/<app>/<id>.png; its media path, or None."""
    if not note.get("picture"):
        return None
    data = _request(settings, note["picture"])
    if not isinstance(data, bytes) or not data.startswith(b"\x89PNG"):
        return None
    relative = Path(SOURCE) / note["app"] / f"{note['id']}.png"
    path = settings.database_path.parent / "media" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(relative)


def take(connection: sqlite3.Connection, settings: Settings, note: dict) -> tuple[str, str] | None:
    """Store a new note as an item and an α-rated suggestion for its app's project; returns (item id, project),
    or None when the app is no project of the user's or the note is already in."""
    app, said = str(note.get("app") or ""), str(note.get("said") or "").strip()
    if not app or not said or not note.get("id"):
        return None
    project = _project_for(connection, app)
    if project is None:
        return None
    item_id = item_id_of(note)
    if connection.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
        return None
    selector, page = str(note.get("selector") or ""), str(note.get("page") or "")
    picture = _picture(settings, note)
    where = [f"The user pointed at `{selector}` on {page}" + (f" ({note['title']})" if note.get("title") else "")]
    if note.get("text"):
        where.append(f"The element's text:\n{str(note['text'])[:4000]}")
    if note.get("html"):
        where.append(f"The element's HTML (excerpt):\n{str(note['html'])[:6000]}")
    if picture:
        where.append(f"A picture of the element: {settings.database_path.parent / 'media' / picture}")
    text = f"{said}\n\n" + "\n\n".join(where)
    when = now()
    connection.execute(
        "INSERT INTO items (id, source, kind, trust, url, title_source, title, author, created_at, found_at, rank, "
        "text, links, raw, summary, category, classified_at) VALUES (?, ?, 'note', 'curated', ?, ?, ?, 'you', ?, ?, 0, "
        "?, '[]', ?, ?, 'note', ?)",
        (item_id, SOURCE, page, _headline(said), _headline(said), note.get("created") or when, when, text,
         json.dumps(note, ensure_ascii=False), said, when),
    )
    if picture:
        connection.execute(
            "INSERT OR IGNORE INTO content (item_id, kind, key, text, detail, fetched_at) VALUES (?, 'image', ?, ?, ?, ?)",
            (item_id, picture, "A picture of the element the note is about.", json.dumps({"file": picture}), when),
        )
    # Matched by hand: the note is about its own app and no other project; the pipeline then leaves it alone
    # (relevance rows need a projects row; a project known only by its brief gets the suggestion alone).
    connection.executemany(
        "INSERT OR IGNORE INTO relevance (item_id, project, score, confidence, reason, scored_at) VALUES (?, ?, ?, 1, ?, ?)",
        [(item_id, row["name"], 3.0 if row["name"] == project else 0.0,
          "A Let There note on this app" if row["name"] == project else None, when)
         for row in connection.execute("SELECT name FROM projects")],
    )
    action = f"In {project}: {said} It is about the element `{selector}` on {page}; Let There builds it where the app lives."
    connection.execute(
        "INSERT OR IGNORE INTO suggestions (item_id, project, action, headline, outcome, effort, basis, basis_kind, "
        "model, status, created_at, rating, rated_at) VALUES (?, ?, ?, ?, ?, 'small', ?, ?, 'letthere', 'open', ?, "
        "'alpha', ?)",
        (item_id, project, action, _headline(said), "", said, BASIS_KIND, when, when),
    )
    set_metadata(connection, RATED_AT, when)
    connection.commit()
    return item_id, project


def mirror(connection: sqlite3.Connection, note: dict) -> bool:
    """Copy the note's build (Let There's front matter: status, branch, base, worktree, files, report, error, times)
    onto its suggestion's implement_* columns, so the Ideas tab and Review changes show it. Returns whether the
    suggestion exists."""
    item_id = item_id_of(note)
    row = connection.execute("SELECT project, implement_status, status FROM suggestions WHERE item_id = ?",
                             (item_id,)).fetchone()
    if row is None:
        return False
    status = BUILD_STATUS.get(str(note.get("status") or ""))
    if note.get("status") == "done" and row["status"] != "done":
        connection.execute("UPDATE suggestions SET status = 'done' WHERE item_id = ?", (item_id,))
    if status or note.get("branch"):
        connection.execute(
            "UPDATE suggestions SET implement_status = ?, implement_branch = ?, implement_base = ?, implement_path = ?, "
            "implement_files = ?, implement_report = ?, implement_error = ?, implement_started_at = ?, "
            "implement_finished_at = ? WHERE item_id = ?",
            (status or row["implement_status"], note.get("branch"), note.get("base"), note.get("worktree"),
             note.get("files"), note.get("report"), note.get("error"), note.get("started"), note.get("finished"), item_id),
        )
    connection.commit()
    return True


def handle(connection: sqlite3.Connection, settings: Settings, note: dict) -> tuple[str, str] | None:
    """One note, as Let There posts it or lists it: take it if new to Alpha, then mirror its build."""
    taken = take(connection, settings, note)
    mirror(connection, note)
    return taken


def sync(connection: sqlite3.Connection, settings: Settings) -> int:
    """Every note on the server: new ones are taken, the rest have their builds mirrored. Returns how many were
    taken."""
    listed = _request(settings, "/notes")
    if not isinstance(listed, list):
        return 0
    count = sum(1 for note in listed if isinstance(note, dict) and handle(connection, settings, note))
    if count:
        print(f"Let There: {count} note{'' if count == 1 else 's'} taken", flush=True)
    return count
