"""Keep what an item points at: the text of linked pages and the images, saved locally and
transcribed by Claude. Each piece is processed once; failures are recorded and not retried
(an empty text with the error in `detail`), so a dead link costs one attempt."""

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import httpx
import trafilatura

from . import writer
from .config import Settings
from .pipeline import parallel
from .storage import add_content

# Posts, media, and video pages: their content is already stored or cannot be read as text.
SKIPPED_HOSTS = {"x.com", "twitter.com", "t.co", "youtube.com", "youtu.be", "instagram.com", "tiktok.com"}
LINK_CHARACTERS = 60_000
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko)"}


def media_directory(settings: Settings) -> Path:
    return settings.database_path.parent / "media"


def _host(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.removeprefix("www.").removeprefix("m.")


def _page_text(url: str) -> tuple[str, dict]:
    response = httpx.get(url, headers=HEADERS, follow_redirects=True, timeout=30)
    response.raise_for_status()
    kind = response.headers.get("content-type", "")
    if "html" not in kind:
        return "", {"skipped": kind or "unknown type", "final_url": str(response.url)}
    text = trafilatura.extract(response.text, include_tables=True, include_comments=False) or ""
    return text[:LINK_CHARACTERS], {"final_url": str(response.url)}


def fetch_links(connection: sqlite3.Connection) -> None:
    pending = []
    for row in connection.execute("SELECT id, links FROM items"):
        for link in json.loads(row["links"]):
            url = link["url"]
            if _host(url) in SKIPPED_HOSTS:
                continue
            exists = connection.execute(
                "SELECT 1 FROM content WHERE item_id = ? AND kind = 'link' AND key = ?", (row["id"], url)
            ).fetchone()
            if not exists:
                pending.append((row["id"], url))

    def task(item: tuple[str, str]) -> tuple[str, dict]:
        try:
            return _page_text(item[1])
        except (httpx.HTTPError, ValueError) as error:
            return "", {"error": f"{type(error).__name__}: {error}"[:300]}

    def store(item: tuple[str, str], result: tuple[str, dict]) -> None:
        add_content(connection, item[0], "link", item[1], *result)
        connection.commit()

    parallel(pending, task, store, "Linked pages")


def _download(url: str, directory: Path, key: str) -> tuple[Path, str]:
    source = url if "?" in url else f"{url}?name=large"
    response = httpx.get(source, headers=HEADERS, follow_redirects=True, timeout=60)
    response.raise_for_status()
    media_type = response.headers.get("content-type", "image/jpeg").split(";")[0]
    path = directory / f"{key}.{media_type.split('/')[-1].replace('jpeg', 'jpg')}"
    path.write_bytes(response.content)
    return path, media_type


def transcribe_images(connection: sqlite3.Connection, settings: Settings) -> None:
    directory = media_directory(settings)
    directory.mkdir(parents=True, exist_ok=True)
    rows = connection.execute(
        "SELECT c.item_id, c.key, c.detail, i.text AS post_text FROM content c "
        "JOIN items i ON i.id = c.item_id WHERE c.kind = 'image' AND c.text IS NULL"
    ).fetchall()

    def task(row: sqlite3.Row) -> tuple[str, dict]:
        detail = json.loads(row["detail"])
        existing = next(directory.glob(f"{row['key']}.*"), None)
        try:
            if existing:
                path, media_type = existing, f"image/{existing.suffix[1:].replace('jpg', 'jpeg')}"
            else:
                path, media_type = _download(detail["url"], directory, row["key"])
        except httpx.HTTPError as error:
            return "", detail | {"error": f"{type(error).__name__}: {error}"[:300]}
        text = writer.transcribe_image(settings.claude_model, path.read_bytes(), media_type, row["post_text"])
        return text, detail | {"file": path.name}

    def store(row: sqlite3.Row, result: tuple[str, dict]) -> None:
        text, detail = result
        connection.execute(
            "UPDATE content SET text = ?, detail = ? WHERE item_id = ? AND kind = 'image' AND key = ?",
            (text, json.dumps(detail, ensure_ascii=False), row["item_id"], row["key"]),
        )
        connection.commit()

    parallel(rows, task, store, "Images (Claude)")
