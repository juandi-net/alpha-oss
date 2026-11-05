"""Hugging Face likes (curated): the models, datasets, and Spaces the user liked on Hugging Face.

Signed in with the user's Hugging Face token: HF_TOKEN in .env, or the token `hf auth login` saved
(~/.cache/huggingface/token, or under HF_HOME). Read through the Hub API (checked 2026-11-04):
- `GET /api/whoami-v2` names the account;
- `GET /api/users/<user>/likes` lists every like, `[{createdAt, repo: {name, type}}]`, newest first;
- `GET /api/<models|datasets|spaces>/<id>?expand[]=...` gives a repo's metadata (task, license, size, use);
- `huggingface.co/[datasets/|spaces/]<id>/raw/main/README.md` is its card.

`collect` stores new likes, newest first, until it reaches a stored one, and ranks them like GitHub stars
(github_stars._rank): above every other item in the order liked; on the first import, likes older than RECENT_DAYS
go below every other item instead. `enrich` reads each new like's metadata and card and makes the item's text:
key metadata (task, license, size, downloads, likes), then the card without its YAML header, cleaned and capped.
Likes are kind model, dataset, or space: resources matched to projects, never part of a skill; curated, so no judge.

Every request goes through the Hub client of the datasets source (huggingface.Client): one at a time, at least
`request_delay_seconds` apart, at most `max_requests` per run, cached in `hf_likes_cache`; a 429 stops until the
next run. hf_following uses this module's helpers (token, metadata, cards) for its releases.
"""

import json
import os
import sqlite3
from functools import cache
from pathlib import Path
from urllib.parse import quote, urlencode

from ..config import Settings, source_config
from ..enrich import HEADERS
from ..judge import Stop
from ..storage import now, store_item
from . import github_stars, huggingface

NAME = "hf_likes"
TRUST = "curated"
KINDS = ("model", "dataset", "space")
# HF_TOKEN or the token file `hf auth login` writes (see missing()), so neither is required on its own.
SECRETS: tuple[str, ...] = ()
TOOLS: tuple[str, ...] = ()
CONFIG = {
    "enabled": False,
    "every_hours": 6,
    "request_delay_seconds": 1.0,  # at least this long (plus up to half again) between requests
    "max_requests": 100,           # per run: the likes list, then two per card (metadata and README)
    "cards": 40,                   # likes whose metadata and card are read per run (a backlog continues next run)
    "card_characters": 12_000,     # card text kept per repo
}

SITE = "https://huggingface.co"
# Per repo type: the API collection and the site path prefix.
TYPES = {"model": ("models", ""), "dataset": ("datasets", "datasets/"), "space": ("spaces", "spaces/")}
# What a listing or a repo's API page should return (the Hub returns only these, plus id).
EXPAND = {
    "model": ("author", "cardData", "createdAt", "lastModified", "downloads", "likes", "tags", "gated", "private",
              "disabled", "pipeline_tag", "library_name", "safetensors", "gguf", "baseModels"),
    "dataset": ("author", "cardData", "createdAt", "lastModified", "downloads", "likes", "tags", "gated", "private",
                "disabled", "description"),
    "space": ("author", "cardData", "createdAt", "lastModified", "likes", "tags", "private", "disabled", "sdk",
              "runtime", "models", "datasets"),
}
# Card keys worth keeping (model-index and widget data can run to megabytes).
CARD_KEYS = ("license", "license_name", "license_link", "language", "tags", "base_model", "pipeline_tag",
             "library_name", "pretty_name", "size_categories", "task_categories", "dataset_info", "language_creators",
             "annotations_creators", "title", "short_description", "sdk", "models", "datasets")
WHOAMI_MAX_AGE_DAYS = 7
INFO_MAX_AGE_DAYS = 1
CARD_MAX_AGE_DAYS = 30


def cache_schema(table: str) -> str:
    """A source's Hub response cache (huggingface.Client reads and writes it)."""
    return (f"CREATE TABLE IF NOT EXISTS {table} (url TEXT PRIMARY KEY, status INTEGER NOT NULL, body TEXT NOT NULL, "
            "fetched_at TEXT NOT NULL);")


SCHEMA = cache_schema("hf_likes_cache") + """
CREATE TABLE IF NOT EXISTS hf_likes_cards (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    status TEXT NOT NULL,         -- ok | none (no card text) | missing (the repo is gone or private)
    characters INTEGER NOT NULL,  -- card characters kept
    fetched_at TEXT NOT NULL
);
"""


@cache
def token() -> str | None:
    if value := os.environ.get("HF_TOKEN"):
        return value.strip()
    home = Path(os.environ.get("HF_HOME") or "~/.cache/huggingface").expanduser()
    path = Path(os.environ.get("HF_TOKEN_PATH") or home / "token").expanduser()
    return (path.read_text().strip() or None) if path.is_file() else None


def missing() -> list[str]:
    """What this source still needs: a Hugging Face token."""
    return [] if token() else ["HF_TOKEN (or a token saved by `hf auth login`)"]


def hub_client(connection: sqlite3.Connection, table: str, delay: float, max_requests: int) -> huggingface.Client:
    """A Hub client with the user's token, caching in `table`."""
    if not token():
        raise SystemExit(f"Hugging Face: no {missing()[0]}")
    connection.executescript(cache_schema(table))
    return huggingface.Client(connection, delay, max_requests, table=table,
                              headers=HEADERS | {"Authorization": f"Bearer {token()}"})


@cache
def _client(connection: sqlite3.Connection, delay: float, max_requests: int) -> huggingface.Client:
    """One client per run (process and database connection), so collect and enrich share one budget."""
    connection.executescript(SCHEMA)
    return hub_client(connection, "hf_likes_cache", delay, max_requests)


def client(connection: sqlite3.Connection, settings: Settings) -> huggingface.Client:
    options = source_config(settings, NAME, CONFIG)
    return _client(connection, float(options["request_delay_seconds"]), int(options["max_requests"]))


def get_json(hub: huggingface.Client, path: str, max_age_days: float) -> object | None:
    """The API's JSON at `path`, or None for 404. A refused token stops with what to do."""
    status, body = hub.get(f"{SITE}{path}", max_age_days)
    if status == 401:
        raise SystemExit("Hugging Face: the token was refused (replace HF_TOKEN, or run `hf auth login`)")
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"Hugging Face answered {status} for {path}")
    return json.loads(body)


def username(hub: huggingface.Client) -> str:
    return get_json(hub, "/api/whoami-v2", WHOAMI_MAX_AGE_DAYS)["name"]


def repo_url(kind: str, repo_id: str) -> str:
    return f"{SITE}/{TYPES[kind][1]}{repo_id}"


def item_id(source: str, kind: str, repo_id: str) -> str:
    """`<source>-<type>--<owner>--<name>`."""
    return f"{source}-{kind}--{repo_id.replace('/', '--')}"


# Sources whose items and candidates are Hugging Face repos with ids from item_id().
HUB_SOURCES = ("hf_likes", "hf_following", "hf_trending")


def stored(connection: sqlite3.Connection, kind: str, repo_id: str) -> bool:
    """Whether Alpha already has the repo: liked, or seen by a Hugging Face discovered source (the datasets source
    too), so the judge never reads one repo twice."""
    ids = [item_id(source, kind, repo_id) for source in HUB_SOURCES]
    if kind == "dataset":
        ids.append(huggingface.candidate_id(repo_id))
    marks = ",".join("?" * len(ids))
    return bool(connection.execute(f"SELECT 1 FROM items WHERE id IN ({marks}) UNION ALL "
                                   f"SELECT 1 FROM candidates WHERE id IN ({marks}) LIMIT 1", ids + ids).fetchone())


def expand_query(kind: str) -> list[tuple[str, str]]:
    return [("expand[]", field) for field in EXPAND[kind]]


def slim(info: dict) -> dict:
    """A listing or repo page without card keys nobody reads."""
    card = info.get("cardData") or {}
    return {key: value for key, value in info.items() if key not in ("_id", "cardData", "siblings")} | {
        "cardData": {key: card[key] for key in CARD_KEYS if key in card}}


def repo_info(hub: huggingface.Client, kind: str, repo_id: str) -> dict | None:
    """A repo's metadata with its storage size, or None when it is gone (or private)."""
    query = urlencode([*expand_query(kind), ("expand[]", "usedStorage")])
    info = get_json(hub, f"/api/{TYPES[kind][0]}/{quote(repo_id)}?{query}", INFO_MAX_AGE_DAYS)
    return slim(info) if isinstance(info, dict) else None


def card(hub: huggingface.Client, kind: str, repo_id: str, characters: int) -> str:
    """The card (README) without its YAML header, HTML, images, and badges, capped; "" when there is none."""
    status, body = hub.get(f"{repo_url(kind, quote(repo_id))}/raw/main/README.md", CARD_MAX_AGE_DAYS)
    if status != 200:
        return ""
    return github_stars._clean(huggingface.strip_front_matter(body))[:characters]


def _number(value: float, units: tuple[str, ...] = ("T", "B", "M", "K")) -> str:
    """1.2B style; with byte units (TB, GB, MB, KB) for sizes on disk."""
    for unit, size in zip(units, (1e12, 1e9, 1e6, 1e3)):
        if value >= size:
            return f"{value / size:.1f}{unit}"
    return str(int(value))


def parameters(info: dict) -> str | None:
    """"8.0B parameters (BF16)" from the safetensors or GGUF metadata."""
    weights = info.get("safetensors") or {}
    total = weights.get("total") or (info.get("gguf") or {}).get("total")
    if not total:
        return None
    types = ", ".join(weights.get("parameters") or {})
    return f"{_number(total)} parameters" + (f" ({types})" if types else "")


def license_text(info: dict) -> str:
    """The license(s), with the named or linked one for `other`, and a note when it rules out commercial use."""
    card = info.get("cardData") or {}
    names = huggingface.licenses(info) or ([card["license"]] if isinstance(card.get("license"), str) else [])
    text = ", ".join(names) or "none stated"
    if "other" in names and (card.get("license_name") or card.get("license_link")):
        text += f" ({card.get('license_name') or card.get('license_link')})"
    if any(huggingface.NONCOMMERCIAL.search(name) for name in names):
        text += "; non-commercial use only"
    return text


def _date(value: str | None) -> str:
    return (value or "?")[:10]


def metadata_text(kind: str, info: dict) -> str:
    """Key metadata for the judge, the summary, and the item: task, license, size, use, dates."""
    if kind == "dataset":
        lines = [huggingface.metadata_text(info), f"Created {_date(info.get('createdAt'))}"]
    else:
        card = info.get("cardData") or {}
        tags = info.get("tags") or []
        lines = [f"{'Model' if kind == 'model' else 'Space'}: {info['id']} ({repo_url(kind, info['id'])})"]
        if kind == "space":
            if card.get("title") and card["title"] != info["id"].split("/")[-1]:
                lines.append(f"Title: {card['title']}")
            if card.get("short_description"):
                lines.append(f"Description: {card['short_description']}")
            runtime = info.get("runtime") or {}
            hardware = (runtime.get("hardware") or {}).get("current")
            lines.append(f"App: {info.get('sdk') or card.get('sdk') or '?'}"
                         + (f" on {hardware}" if hardware else "") + (f", {runtime['stage'].lower()}" if runtime.get("stage") else ""))
            for label, key in (("Models used", "models"), ("Datasets used", "datasets")):
                if used := info.get(key) or card.get(key):
                    lines.append(f"{label}: {', '.join(map(str, used[:10]))}")
        lines.append(f"License: {license_text(info)}")
        if info.get("gated"):
            lines.append("Access: gated (you must accept the owner's terms on Hugging Face before downloading)")
        if kind == "model":
            if info.get("pipeline_tag"):
                lines.append(f"Task: {info['pipeline_tag']}")
            if info.get("library_name"):
                lines.append(f"Library: {info['library_name']}")
            if size := parameters(info):
                lines.append(f"Size: {size}")
            base = info.get("baseModels") or {}
            if base.get("models"):
                lines.append(f"Base model ({base.get('relation', '?')}): "
                             + ", ".join(model["id"] for model in base["models"][:5]))
        languages = [tag.removeprefix("language:") for tag in tags if tag.startswith("language:")]
        if languages:
            lines.append(f"Languages: {', '.join(languages[:20])}")
        plain = [tag for tag in tags if ":" not in tag and tag not in ("region", "endpoints_compatible")]
        if plain:
            lines.append(f"Tags: {', '.join(plain[:20])}")
        use = (f"{info.get('downloads', 0):,} downloads last month, " if kind == "model" else "") + f"{info.get('likes', 0):,} likes"
        lines.append(f"Use: {use}; created {_date(info.get('createdAt'))}, last modified {_date(info.get('lastModified'))}")
    if info.get("usedStorage"):
        lines.append(f"Storage: {_number(info['usedStorage'], ('TB', 'GB', 'MB', 'KB'))}")
    return "\n".join(lines)


def text(kind: str, info: dict, card_text: str) -> str:
    """The item's text: metadata, then the card."""
    return metadata_text(kind, info) + (f"\n\nCard:\n{card_text}" if card_text else "\n\nNo card.")


def fetch_likes(connection: sqlite3.Connection, hub: huggingface.Client) -> tuple[int, dict[str, int]]:
    """Store new likes, newest first, until a stored one. Returns (read, new per kind)."""
    likes = get_json(hub, f"/api/users/{quote(username(hub))}/likes", 0) or []
    likes.sort(key=lambda like: like["createdAt"], reverse=True)
    first_import = not connection.execute("SELECT 1 FROM items WHERE source = ? LIMIT 1", (NAME,)).fetchone()
    newest_first: list[tuple[str, str]] = []
    new: dict[str, int] = {}
    read = 0
    for like in likes:
        read += 1
        kind, repo_id = like["repo"]["type"], like["repo"]["name"]
        if kind not in TYPES:
            continue
        identifier = item_id(NAME, kind, repo_id)
        if not store_item(connection, {
            "id": identifier, "source": NAME, "kind": kind, "trust": TRUST, "url": repo_url(kind, repo_id),
            "title_source": repo_id, "author": repo_id.split("/")[0], "created_at": like["createdAt"],
            "text": f"Hugging Face {kind} {repo_id}, liked {_date(like['createdAt'])}", "raw": like,
        }):
            break
        newest_first.append((identifier, like["createdAt"]))
        new[kind] = new.get(kind, 0) + 1
    if newest_first:
        github_stars._rank(connection, newest_first, first_import)
    connection.commit()
    return read, new


def fetch_cards(connection: sqlite3.Connection, hub: huggingface.Client, *, limit: int,
                characters: int) -> tuple[int, int, int]:
    """Metadata and card of each like not yet read, newest first, at most `limit`. Returns (with a card, read,
    still waiting)."""
    rows = connection.execute(
        "SELECT id, raw FROM items i WHERE source = ? AND NOT EXISTS "
        "(SELECT 1 FROM hf_likes_cards c WHERE c.item_id = i.id) ORDER BY rank DESC",
        (NAME,),
    ).fetchall()
    with_card = 0
    for row in rows[:limit]:
        repo = json.loads(row["raw"])["repo"]
        kind = repo["type"]
        info = repo_info(hub, kind, repo["name"])
        card_text = card(hub, kind, repo["name"], characters) if info else ""
        if info:
            connection.execute("UPDATE items SET text = ?, raw = ? WHERE id = ?",
                               (text(kind, info, card_text),
                                json.dumps(json.loads(row["raw"]) | {"info": info}, ensure_ascii=False), row["id"]))
        with_card += bool(card_text)
        connection.execute(
            "INSERT OR REPLACE INTO hf_likes_cards (item_id, status, characters, fetched_at) VALUES (?, ?, ?, ?)",
            (row["id"], "ok" if card_text else "none" if info else "missing", len(card_text), now()),
        )
        connection.commit()
    return with_card, min(len(rows), limit), max(len(rows) - limit, 0)


def collect(connection: sqlite3.Connection, settings: Settings) -> dict:
    """Read new likes. A 429 or a spent budget stops the run (`stopped` in the stats); the next run continues."""
    hub = client(connection, settings)
    try:
        read, new = fetch_likes(connection, hub)
    except Stop as error:
        print(f"Hugging Face likes: {error}; stopped until the next run")
        return {"stopped": str(error)}
    kinds = ", ".join(f"{count} {kind}s" for kind, count in new.items())
    print(f"Hugging Face likes: read {read} likes, {sum(new.values())} new" + (f" ({kinds})" if kinds else "")
          + f" ({hub.requests} requests)")
    return {"read": read, "new": sum(new.values())} | {kind: count for kind, count in new.items()}


def enrich(connection: sqlite3.Connection, settings: Settings) -> None:
    """Metadata and cards of up to `cards` new likes."""
    options = source_config(settings, NAME, CONFIG)
    hub = client(connection, settings)
    try:
        with_card, read, waiting = fetch_cards(connection, hub, limit=int(options["cards"]),
                                               characters=int(options["card_characters"]))
        print(f"Hugging Face likes: metadata for {read} likes, {with_card} with a card "
              f"({hub.requests} requests this run, {hub.cached} from cache)" + (f"; {waiting} more next run" if waiting else ""))
    except Stop as error:
        print(f"Hugging Face likes: {error}; cards continue next run")
    hub.prune()
