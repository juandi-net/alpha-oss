"""Let There's local server: it serves the overlay (overlay.js) and keeps each note an app sends.

Let There is a layer over the apps you build: point at something, press \\, say what should change. An app takes
part with one script tag,

    <script src="http://localhost:3470/overlay.js" data-app="NAME" defer></script>

and each note becomes NOTES/NAME/<id>.md (what you said, where the element is, its text, an HTML excerpt) with a
picture of the element beside it as <id>.png. NOTES is letthere/var/notes unless --notes names another folder.

Run: uv run letthere/server.py [--port 3470] [--notes DIR]. It uses only the standard library, listens on
127.0.0.1, and takes notes only from pages served by localhost or 127.0.0.1.
"""

import argparse
import base64
import binascii
import json
import re
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
OVERLAY = HERE / "overlay.js"
NOTES = HERE / "var" / "notes"
PORT = 3470
APP = re.compile(r"[\w.-]{1,64}")
BODY_LIMIT = 12 * 1024 * 1024  # a note with its picture
PNG = b"\x89PNG\r\n\x1a\n"


def slug(text: str, words: int = 6) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", text.lower())[:words]) or "note"


def save(notes: Path, note: dict) -> str:
    """Write the note (and its picture) under notes/<app>/ and return its id: the time it was saved and the
    first words said."""
    app, said = note.get("app"), str(note.get("note") or "").strip()
    if not isinstance(app, str) or not APP.fullmatch(app) or app.startswith("."):
        raise ValueError("The script tag's data-app must be a plain name (letters, digits, . _ -)")
    if not said:
        raise ValueError("The note is empty")
    picture = _picture(note.get("picture"))
    folder = notes / app
    folder.mkdir(parents=True, exist_ok=True)
    created = datetime.now(UTC).replace(microsecond=0)
    stem = f"{created:%Y-%m-%d-%H%M%S}-{slug(said)}"
    for count in range(1, 100):
        note_id = stem if count == 1 else f"{stem}-{count}"
        try:
            with (folder / f"{note_id}.md").open("x", encoding="utf-8") as handle:
                handle.write(markdown(note_id, created, note, said, picture is not None))
            break
        except FileExistsError:
            continue
    else:
        raise ValueError("Too many notes saved this second")
    if picture:
        (folder / f"{note_id}.png").write_bytes(picture)
    return note_id


def _picture(data_url: object) -> bytes | None:
    if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
        return None
    try:
        picture = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except binascii.Error:
        return None
    return picture if picture.startswith(PNG) else None


def _number(values: object, key: str) -> int:
    try:
        return round(float(values.get(key) or 0)) if isinstance(values, dict) else 0
    except (TypeError, ValueError):
        return 0


def markdown(note_id: str, created: datetime, note: dict, said: str, has_picture: bool) -> str:
    """The note file: front matter for whatever picks the note up next, then what was said and where."""
    def text(key: str, limit: int) -> str:
        return str(note.get(key) or "")[:limit]

    box, viewport = note.get("box"), note.get("viewport")
    front = {
        "id": note_id,
        "app": note["app"],
        "page": text("page", 2000),
        "title": text("title", 300),
        "selector": text("selector", 2000),
        "created": created.isoformat(),
        "status": "new",
    }
    place = (f"{_number(box, 'width')} × {_number(box, 'height')} at ({_number(box, 'left')}, {_number(box, 'top')}) "
             f"in the page, seen in a {_number(viewport, 'width')} × {_number(viewport, 'height')} window")
    lines = ["---", *(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in front.items()), "---", "",
             f"# {said.splitlines()[0][:100]}", "", said, "", "## Where", "",
             f"- Page: {front['page']}", f"- Element: `{front['selector']}`", f"- Box: {place}"]
    lines += ["", f"![The element]({note_id}.png)"] if has_picture else ["", "(No picture: the page could not draw one.)"]
    element_text = text("text", 4000).strip()
    if element_text:
        lines += ["", "## Its text", "", *(f"> {line}" if line else ">" for line in element_text.splitlines())]
    html = text("html", 6000).strip()
    if html:
        fence = "````" if "```" in html else "```"
        lines += ["", "## Its HTML (excerpt)", "", f"{fence}html", html, fence]
    return "\n".join(lines) + "\n"


def _local(origin: str) -> bool:
    return urlparse(origin).hostname in ("localhost", "127.0.0.1")


class Handler(BaseHTTPRequestHandler):
    notes = NOTES

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        origin = self.headers.get("Origin") or ""
        if _local(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict) -> None:
        self._send(status, json.dumps(value).encode())

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/overlay.js":
            self._send(200, OVERLAY.read_bytes(), "text/javascript; charset=utf-8")
        else:
            self._json(404, {"error": "Not found"})

    def do_OPTIONS(self) -> None:
        if not _local(self.headers.get("Origin") or ""):
            self._json(403, {"error": "Only pages on localhost may send notes"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_POST(self) -> None:
        origin = self.headers.get("Origin") or ""
        if origin and not _local(origin):
            self._json(403, {"error": "Only pages on localhost may send notes"})
            return
        if urlparse(self.path).path != "/notes":
            self._json(404, {"error": "Not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= BODY_LIMIT:
            self._json(413, {"error": "The note is empty or too large"})
            return
        try:
            note = json.loads(self.rfile.read(length))
        except ValueError:
            self._json(400, {"error": "A note is JSON"})
            return
        if not isinstance(note, dict):
            self._json(400, {"error": "A note is a JSON object"})
            return
        try:
            note_id = save(self.notes, note)
        except ValueError as error:
            self._json(400, {"error": str(error)})
            return
        self._json(201, {"id": note_id, "path": f"{note['app']}/{note_id}.md"})

    def log_message(self, format: str, *args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Let There: keep the notes apps send from their overlay.")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--notes", type=Path, default=NOTES, help=f"folder for the notes (default {NOTES})")
    arguments = parser.parse_args()
    Handler.notes = arguments.notes.expanduser().resolve()
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), Handler)
    print(f"Let There: http://localhost:{arguments.port}/overlay.js, notes in {Handler.notes}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
