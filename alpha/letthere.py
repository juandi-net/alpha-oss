"""Alpha as Let There's coordinator: a note made on an app becomes a kept idea for that app's project and, by
default, starts building at once.

Let There (its own tool) is a layer over the apps the user builds: point at something, press ], say what
should change. Its server keeps each note as a markdown file with a picture of the element and tells Alpha about
it (`--notify http://localhost:<port>/letthere`); `alpha run` also asks it for notes still `new`, so none is missed.

For each note whose app is one of the user's projects (the script tag's data-app, matched to a project name):
- the note becomes an item (source letthere, kind note; it is the user's own words about their app, so it is
  summarized and matched by hand here and the pipeline leaves it alone; the Feed hides the source by default),
  with the element's picture kept under var/media/letthere;
- and a suggestion for that project (basis_kind note, rated α, the words as its basis), the same card as a Your
  idea, so Ask about this, Implement, Review, Mark done, the Ideas tab, and MCP all apply;
- with `let_there_implement` (default on) Implement starts right away, as the dashboard's Confirm would.
The note's status line follows: taken, implementing, implemented or failed (implement.py), done (Mark done).
"""

import json
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .config import TOOL_ROOT, Settings
from .pipeline import project_texts
from .storage import now, set_metadata
from .taste import RATED_AT

SOURCE = "letthere"
BASIS_KIND = "note"
HEADLINE_WORDS = 12
TIMEOUT = 10


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
    """Tell Let There where a note's work is; nothing when the item is not a note or the server is away."""
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


def take(connection: sqlite3.Connection, settings: Settings, note: dict) -> tuple[str, str] | None:
    """Store the note as an item and an α-rated suggestion for its app's project; returns (item id, project), or
    None when the app is no project of the user's, or the note is already in. Does not start Implement."""
    app, said = str(note.get("app") or ""), str(note.get("said") or "").strip()
    if not app or not said or not note.get("id"):
        return None
    project = _project_for(connection, app)
    if project is None:
        print(f"Let There: note {note.get('id')} is for {app}, which is no project here; left as new", flush=True)
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
        where.append(f"A picture of the element: {settings.database_path.parent / 'media' / picture} (read it to see "
                     "what the user saw)")
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
    # Matched by hand: the note is about its own app and no other project; the pipeline then leaves it alone.
    # (relevance rows need a projects row; a project known only by its brief gets the suggestion alone.)
    connection.executemany(
        "INSERT OR IGNORE INTO relevance (item_id, project, score, confidence, reason, scored_at) VALUES (?, ?, ?, 1, ?, ?)",
        [(item_id, row["name"], 3.0 if row["name"] == project else 0.0,
          "A Let There note on this app" if row["name"] == project else None, when)
         for row in connection.execute("SELECT name FROM projects")],
    )
    action = (f"In {project}: {said} It is about the element `{selector}` on {page}; first step: make that change "
              "there, as small as it can be.")
    connection.execute(
        "INSERT OR IGNORE INTO suggestions (item_id, project, action, headline, outcome, effort, basis, basis_kind, "
        "model, status, created_at, rating, rated_at) VALUES (?, ?, ?, ?, ?, 'small', ?, ?, 'letthere', 'open', ?, "
        "'alpha', ?)",
        (item_id, project, action, _headline(said), "", said, BASIS_KIND, when, when),
    )
    set_metadata(connection, RATED_AT, when)
    connection.commit()
    return item_id, project


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


def start(connection: sqlite3.Connection, settings: Settings, item_id: str, project: str) -> None:
    """Start Implement on the note's suggestion in the background (as the dashboard's Confirm does) when
    `let_there_implement` is on; the note's status says taken, then implementing."""
    from . import implement  # here: implement.py reports a note's status through this module
    if not settings.let_there_implement:
        set_status(settings, item_id, "taken")
        return
    suggestion = connection.execute("SELECT * FROM suggestions WHERE item_id = ? AND project = ?",
                                    (item_id, project)).fetchone()
    if suggestion is None or not implement.can_start(suggestion):
        return
    implement.queue(connection, item_id, project)
    connection.commit()
    set_status(settings, item_id, "implementing")
    log_path = TOOL_ROOT / "var" / "logs" / "implement.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log:
        subprocess.Popen([str(Path(sys.executable).parent / "alpha"), "implement", item_id, project],
                         cwd=TOOL_ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def handle(connection: sqlite3.Connection, settings: Settings, note: dict) -> tuple[str, str] | None:
    """One note, as Let There posts it: take it and start it."""
    taken = take(connection, settings, note)
    if taken:
        start(connection, settings, *taken)
    return taken


def sync(connection: sqlite3.Connection, settings: Settings) -> int:
    """Every note still `new` on the server (missed while Alpha was down); returns how many were taken."""
    listed = _request(settings, "/notes?status=new")
    if not isinstance(listed, list):
        return 0
    count = 0
    for note in listed:
        if isinstance(note, dict) and handle(connection, settings, note):
            count += 1
    if count:
        print(f"Let There: {count} note{'' if count == 1 else 's'} taken", flush=True)
    return count
