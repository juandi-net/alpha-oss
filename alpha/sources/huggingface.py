"""Hugging Face datasets (discovered): datasets for the concrete data needs of the user's projects. Every
candidate goes through the shared judge (judge.py) before it becomes an item; datasets never feed skills, they
show up as project resources.

Datasets answer data needs, not topics, so the brief's search queries are not searched as they are. Instead
Claude turns each focus brief (and each open request) into 0 to 3 concrete data needs ("labeled
profanity-in-context examples for a word-sense filter"), each with a few keyword searches. Most briefs need no
data; that is expected. The needs are written once per brief or request and kept in `huggingface_needs`.

Read through the public Hugging Face Hub API (no key; checked 2026-11-04):
- `huggingface.co/api/datasets?search=Q&sort=downloads&direction=-1&limit=N&full=true` lists datasets whose
  name contains the words of Q, with downloads (last 30 days), likes, tags (`license:...`), gated, the card's
  metadata (`cardData`: license, sizes, splits, columns) and its first ~500 characters (`description`).
- `huggingface.co/datasets/<id>/raw/main/README.md` is the whole dataset card.

Hard filters before the judge (no request needed): a stated license that allows use (none, `unknown`, or an
unnamed `other` is dropped), a real card (not empty or the auto-generated template), some use (downloads or
likes at a small floor), the need's language when the dataset names its languages, and no dump of another
model's outputs unless the need asks for one.

Politeness: one request at a time, at least `request_delay_seconds` apart, at most `max_requests` per run (half
for searching, the rest for reading cards), every response cached in `huggingface_cache`, and any 429 stops
Hugging Face for the rest of the run.
"""

import json
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel, Field

from .. import focus, writer
from ..config import Settings, source_config
from ..enrich import HEADERS
from ..judge import Candidate, Need, Stop
from ..pipeline import parallel
from ..storage import now

NAME = "huggingface"
TRUST = "discovered"
KINDS = ("dataset",)
SECRETS: tuple[str, ...] = ()
TOOLS: tuple[str, ...] = ()
CONFIG = {
    "enabled": False,
    "every_hours": 168,             # weekly: datasets change slowly
    "request_delay_seconds": 2.0,   # at least this long (plus up to half again) between requests
    "max_requests": 40,             # requests per run (half for searching, half for reading cards)
    "results_per_search": 20,       # datasets listed per keyword search, most downloaded first
    "requery_days": 30,             # a project's data needs are searched again after this many days
    "min_downloads": 20,            # some use: downloads in the last 30 days at least this ...
    "min_likes": 2,                 # ... or likes at least this
}

SCHEMA = """
-- Claude's data needs per brief (`brief:<focus_briefs.id>`) or request (`request:<requests.id>`); [] = none.
CREATE TABLE IF NOT EXISTS huggingface_needs (
    key TEXT PRIMARY KEY,
    needs TEXT NOT NULL,
    written_at TEXT NOT NULL
);
-- Every Hugging Face response, so nothing is requested twice within its useful life.
CREATE TABLE IF NOT EXISTS huggingface_cache (
    url TEXT PRIMARY KEY,
    status INTEGER NOT NULL,
    body TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""

API = "https://huggingface.co/api/datasets"
SITE = "https://huggingface.co/datasets"
SEARCH_MAX_AGE_DAYS = 6          # fresh for each weekly run
CARD_MAX_AGE_DAYS = 30
CACHE_DAYS = 60                  # older responses are deleted after each pass
CARD_CHARACTERS = 20_000         # card text given to the judge and kept as item text
MIN_CARD_CHARACTERS = 80         # less real text than this (after the template's filler) is no card

NEEDS_SYSTEM = (
    "You find out whether a project needs an outside dataset, and which. You get a project's focus brief (or a "
    "request an agent made). Write 0 to 3 data needs: concrete datasets the project could use now to train, "
    "fine-tune, or evaluate a model it is building, each one line naming the content, labels, and use (e.g. "
    "'labeled profanity-in-context examples for a word-sense filter').\n\n"
    "Write a need only when the brief itself shows model or data work: training, fine-tuning, or evaluating a "
    "model or classifier, or looking for data. Not for testing a parser or a website, seed content, placeholder "
    "media, or examples the user could make themselves in an afternoon. Most projects need no dataset (a website, "
    "a business, an app without a model, a plan), and then the right answer is none. When in doubt, none.\n\n"
    "For each need write 1 to 3 searches of 1 or 2 plain words that would appear in a Hugging Face dataset's "
    "name (e.g. 'profanity', 'toxic', 'offensive speech', 'keyword spotting'); the search only matches names, "
    "so no sentences. List the languages the data must be in as ISO 639-1 codes (e.g. ['en']), or none when any "
    "language works. Set model_outputs_ok only when the need is for text another model generated (synthetic "
    "data, distillation, preference pairs)."
)

LICENSE_UNUSABLE = {"unknown", "other", ""}
NONCOMMERCIAL = re.compile(r"(^|-)nc(-|$)|non-?commercial|research-only", re.IGNORECASE)
MODEL_DUMP = re.compile(
    r"rollouts?|generations|completions|distill|gpt-?[345o]|gpt-oss|claude|llama|qwen|gemini|deepseek|mistral|"
    r"mixtral|gemma|phi-?[234]", re.IGNORECASE,
)
TEMPLATE_FILLER = re.compile(r"\[?more information needed\]?|dataset card for\s*\S*|see the full description on "
                             r"the dataset page:?\s*\S*", re.IGNORECASE)


class DataNeed(BaseModel):
    need: str = Field(description="One line: the content, labels, and use of the dataset")
    searches: list[str] = Field(description="1 to 3 searches of 1 or 2 words found in dataset names")
    languages: list[str] = Field(default_factory=list, description="ISO 639-1 codes the data must be in; empty = any")
    model_outputs_ok: bool = Field(description="True only if the need is for text generated by another model")


class DataNeeds(BaseModel):
    needs: list[DataNeed]


def data_needs(model: str, context: str) -> list[DataNeed]:
    """0 to 3 data needs for a focus brief or request (most have none)."""
    needs = writer._stream_parsed(model, NEEDS_SYSTEM, context, DataNeeds, writer.MAX_TOKENS).needs
    for need in needs:
        need.searches = [search.strip() for search in dict.fromkeys(need.searches) if search.strip()][:3]
        need.languages = [code.strip().lower() for code in need.languages if code.strip()]
    return [need for need in needs if need.need.strip() and need.searches][:3]


@dataclass
class Client:
    """Polite, cached Hub reads; hf_likes and hf_following use it too, each with its own cache table (same columns
    as huggingface_cache) and the user's token in `headers`."""

    connection: sqlite3.Connection
    delay: float
    max_requests: int
    table: str = "huggingface_cache"
    headers: dict = field(default_factory=lambda: dict(HEADERS))
    requests: int = 0
    cached: int = 0
    blocked: str | None = None       # set by the first 429; no request is made after it
    _last: float = 0.0

    def get(self, url: str, max_age_days: float) -> tuple[int, str]:
        """(status, body), from the cache while younger than max_age_days. A 429 stops the run."""
        row = self.connection.execute(f"SELECT status, body, fetched_at FROM {self.table} WHERE url = ?", (url,)).fetchone()
        if row and row["status"] in (200, 401, 403, 404):
            age = (datetime.now(UTC) - datetime.fromisoformat(row["fetched_at"])).total_seconds() / 86400
            if age < max_age_days:
                self.cached += 1
                return row["status"], row["body"]
        if self.blocked:
            raise Stop(self.blocked)
        if self.requests >= self.max_requests:
            raise Stop(f"{self.max_requests} Hugging Face requests made this run")
        wait = self._last + self.delay * random.uniform(1, 1.5) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            response = httpx.get(url, headers=self.headers, follow_redirects=True, timeout=30)
        finally:
            self._last = time.monotonic()
            self.requests += 1
        if response.status_code == 429:
            self.blocked = "429 from huggingface.co"
            raise Stop(self.blocked)
        if response.status_code >= 500:
            response.raise_for_status()
        self.connection.execute(
            f"INSERT INTO {self.table} (url, status, body, fetched_at) VALUES (?, ?, ?, ?) ON CONFLICT(url) DO UPDATE "
            "SET status = excluded.status, body = excluded.body, fetched_at = excluded.fetched_at",
            (url, response.status_code, response.text, now()),
        )
        self.connection.commit()
        return response.status_code, response.text

    def search(self, query: str, limit: int) -> list[dict]:
        params = {"search": query, "sort": "downloads", "direction": -1, "limit": limit, "full": "true"}
        status, body = self.get(f"{API}?{urlencode(params)}", SEARCH_MAX_AGE_DAYS)
        if status != 200:
            raise httpx.HTTPError(f"search answered {status}")
        return [dataset for dataset in json.loads(body) if isinstance(dataset, dict) and dataset.get("id")]

    def card(self, dataset_id: str) -> str | None:
        """The whole README card, or None when it cannot be read (missing, or gated)."""
        status, body = self.get(f"{SITE}/{quote(dataset_id)}/raw/main/README.md", CARD_MAX_AGE_DAYS)
        return body if status == 200 else None

    def prune(self) -> None:
        """Forget old responses, so the database and its daily backup stay small."""
        cutoff = datetime.fromtimestamp(time.time() - CACHE_DAYS * 86400, UTC).isoformat(timespec="seconds")
        self.connection.execute(f"DELETE FROM {self.table} WHERE fetched_at < ?", (cutoff,))
        self.connection.commit()


@cache
def _client(connection: sqlite3.Connection, delay: float, max_requests: int) -> Client:
    """One client per run (process and database connection), so discovery and reading share one budget."""
    connection.executescript(SCHEMA)
    return Client(connection, delay, max_requests)


def client(connection: sqlite3.Connection, settings: Settings) -> Client:
    options = source_config(settings, NAME, CONFIG)
    return _client(connection, float(options["request_delay_seconds"]), int(options["max_requests"]))


def needs_for(connection: sqlite3.Connection, settings: Settings, contexts: dict[str, str]) -> dict[str, list[DataNeed]]:
    """Data needs for each context key (`brief:<id>` / `request:<id>`), asking Claude only for keys not seen yet."""
    connection.executescript(SCHEMA)
    pending = [key for key in contexts
               if not connection.execute("SELECT 1 FROM huggingface_needs WHERE key = ?", (key,)).fetchone()]

    def task(key: str) -> list[DataNeed]:
        return data_needs(settings.claude_model, contexts[key])

    def store(key: str, needs: list[DataNeed]) -> None:
        connection.execute(
            "INSERT INTO huggingface_needs (key, needs, written_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
            "SET needs = excluded.needs, written_at = excluded.written_at",
            (key, json.dumps([need.model_dump() for need in needs], ensure_ascii=False), now()),
        )
        connection.commit()

    if pending:
        parallel(pending, task, store, "Data needs (Claude)")
    result = {}
    for key in contexts:
        row = connection.execute("SELECT needs FROM huggingface_needs WHERE key = ?", (key,)).fetchone()
        if row:  # a failed key is asked again next run
            result[key] = [DataNeed(**need) for need in json.loads(row["needs"])]
    return result


def discover(connection: sqlite3.Connection, settings: Settings, needs: list[Need]) -> list[Candidate]:
    """Turn each open request and each brief with unsearched queries into data needs, search their keywords
    within half the request budget, and return the datasets that pass the hard filters."""
    options = source_config(settings, NAME, CONFIG)
    hub = client(connection, settings)
    briefs = focus.latest_briefs(connection)
    # One unit per request and per project: (context key, the judge's needs it answers, context text).
    units: dict[str, tuple[list[Need], str]] = {}
    for need in needs:
        if need.request_id:
            text = f"Request from an agent: {need.query}"
            if need.project in briefs:
                text += f"\n\nIt is for the project {need.project}. Focus brief:\n{briefs[need.project]['brief']}"
            units.setdefault(f"request:{need.request_id}", ([], text))[0].append(need)
        elif need.project in briefs:
            brief = briefs[need.project]
            units.setdefault(f"brief:{brief['id']}", ([], f"Project: {need.project}\n\nFocus brief:\n{brief['brief']}"))[0].append(need)
    written = needs_for(connection, settings, {key: text for key, (_, text) in units.items()})

    found: dict[str, Candidate] = {}
    dropped: dict[str, int] = {}
    seen = searches = 0
    try:
        for key, (answered, _) in units.items():
            if key not in written:
                continue
            data = written[key]
            total = sum(len(need.searches) for need in data)
            if data and hub.requests + total > options["max_requests"] // 2:
                break
            for data_need in data:
                first = answered[0]
                attach = first if first.request_id else Need(data_need.need, first.project)
                for query in data_need.searches:
                    try:
                        datasets = hub.search(query, int(options["results_per_search"]))
                    except (httpx.HTTPError, ValueError) as error:  # tried again next run
                        print(f"Hugging Face: a search failed: {type(error).__name__}: {error}")
                        continue
                    searches += 1
                    for dataset in datasets:
                        seen += 1
                        reason = rejection(dataset, data_need, options)
                        if reason:
                            dropped[reason] = dropped.get(reason, 0) + 1
                        else:
                            found.setdefault(candidate_id(dataset["id"]), candidate(dataset, attach))
            for need in answered:  # searched (a context with no data needs is answered by searching nothing)
                need.searched = True
    except Stop as error:
        print(f"Hugging Face: stopped searching: {error}")
    hub.prune()
    reasons = ", ".join(f"{count} {reason}" for reason, count in sorted(dropped.items(), key=lambda item: -item[1]))
    print(f"Hugging Face: {sum(len(v) for v in written.values())} data needs for {len(written)} briefs/requests, "
          f"{searches} searches, {seen} datasets listed, {sum(dropped.values())} filtered out"
          + (f" ({reasons})" if reasons else "") + f", {len(found)} candidates; "
          f"{hub.requests} requests so far, {hub.cached} answered from cache")
    return list(found.values())


def fetch_text(connection: sqlite3.Connection, settings: Settings, candidate: sqlite3.Row) -> str:
    """Key metadata (license, size, splits, columns, use), then the card without its YAML header."""
    raw = json.loads(candidate["raw"])
    card = client(connection, settings).card(raw["id"])
    body = strip_front_matter(card) if card else (raw.get("description") or "")
    return f"{metadata_text(raw)}\n\nDataset card:\n{body.strip()[:CARD_CHARACTERS]}"


def rejection(dataset: dict, need: DataNeed, options: dict) -> str | None:
    """Why a listed dataset fails the hard filters, or None when it passes."""
    if dataset.get("private") or dataset.get("disabled"):
        return "not public"
    if not usable_license(dataset):
        return "no usable license"
    if len(card_summary(dataset)) < MIN_CARD_CHARACTERS:
        return "no real card"
    if (dataset.get("downloads") or 0) < options["min_downloads"] and (dataset.get("likes") or 0) < options["min_likes"]:
        return "too little use"
    languages = {tag.removeprefix("language:").lower() for tag in dataset.get("tags") or [] if tag.startswith("language:")}
    if need.languages and languages and not languages & set(need.languages):
        return "other language"
    if not need.model_outputs_ok and model_dump(dataset):
        return "model outputs"
    return None


def licenses(dataset: dict) -> list[str]:
    return [tag.removeprefix("license:") for tag in dataset.get("tags") or [] if tag.startswith("license:")]


def usable_license(dataset: dict) -> bool:
    """A named license; `other` counts only when the card names or links the license."""
    card = dataset.get("cardData") or {}
    named = [name for name in licenses(dataset) if name.lower() not in LICENSE_UNUSABLE]
    return bool(named) or ("other" in licenses(dataset) and bool(card.get("license_name") or card.get("license_link")))


def card_summary(dataset: dict) -> str:
    """The card's opening text without the auto-generated template's filler."""
    text = TEMPLATE_FILLER.sub(" ", dataset.get("description") or "")
    return " ".join(text.split())


def model_dump(dataset: dict) -> bool:
    """Text another model wrote: named after a model or its outputs, tagged synthetic, or machine-written."""
    card = dataset.get("cardData") or {}
    creators = card.get("language_creators") or []
    tags = [str(tag).lower() for tag in (dataset.get("tags") or []) + (card.get("tags") or [])]
    return (bool(MODEL_DUMP.search(f"{dataset['id']} {card.get('pretty_name') or ''}"))
            or "synthetic" in tags or "machine-generated" in creators)


def candidate_id(dataset_id: str) -> str:
    return f"{NAME}-{dataset_id.replace('/', '--')}"


def candidate(dataset: dict, need: Need) -> Candidate:
    """A listed dataset as a candidate; its listing (without the file list) is kept as raw."""
    card = dataset.get("cardData") or {}
    keep = ("id", "author", "downloads", "likes", "tags", "gated", "lastModified", "createdAt", "description")
    raw = {key: dataset[key] for key in keep if key in dataset}
    raw["cardData"] = {key: card[key] for key in ("license", "license_name", "license_link", "pretty_name",
                                                  "language", "size_categories", "task_categories",
                                                  "dataset_info", "configs", "language_creators",
                                                  "annotations_creators") if key in card}
    return Candidate(
        id=candidate_id(dataset["id"]),
        source=NAME,
        kind="dataset",
        url=f"{SITE}/{dataset['id']}",
        title=(card.get("pretty_name") or dataset["id"]).strip(),
        subtitle=facts_line(raw),
        author=dataset.get("author"),
        author_url=f"https://huggingface.co/{dataset['author']}" if dataset.get("author") else None,
        posted_at=dataset.get("createdAt") or dataset.get("lastModified"),
        preview=card_summary(dataset)[:1000],
        paid=None,
        raw=raw,
        need=need,
    )


def facts_line(raw: dict) -> str:
    """One line for stage 1: license, size, tasks, use."""
    tags = raw.get("tags") or []
    sizes = [tag.removeprefix("size_categories:") for tag in tags if tag.startswith("size_categories:")]
    tasks = [tag.removeprefix("task_categories:") for tag in tags if tag.startswith("task_categories:")]
    parts = [f"Hugging Face dataset {raw['id']}", f"license {', '.join(licenses(raw))}"]
    if sizes:
        parts.append(f"{', '.join(sizes)} rows")
    if tasks:
        parts.append(", ".join(tasks))
    parts.append(f"{raw.get('downloads', 0)} downloads last month, {raw.get('likes', 0)} likes")
    return " · ".join(parts)


def metadata_text(raw: dict) -> str:
    """License, size, splits, columns, languages, and use, for the judge and the summary."""
    card = raw.get("cardData") or {}
    tags = raw.get("tags") or []
    names = licenses(raw)
    lines = [f"Dataset: {raw['id']} ({SITE}/{raw['id']})", f"License: {', '.join(names)}"
             + (f" ({card.get('license_name') or card.get('license_link')})" if "other" in names else "")]
    if any(NONCOMMERCIAL.search(name) for name in names):
        lines.append("License note: non-commercial use only")
    if raw.get("gated"):
        lines.append("Access: gated (you must accept the owner's terms on Hugging Face before downloading)")
    for label, prefix in (("Size", "size_categories:"), ("Tasks", "task_categories:"), ("Languages", "language:"),
                          ("Modalities", "modality:"), ("Formats", "format:")):
        values = [tag.removeprefix(prefix) for tag in tags if tag.startswith(prefix)]
        if values:
            lines.append(f"{label}: {', '.join(values)}")
    infos = card.get("dataset_info") or []
    for info in infos if isinstance(infos, list) else [infos]:
        if not isinstance(info, dict):
            continue
        config = f" ({info['config_name']})" if info.get("config_name") else ""
        splits = [f"{split.get('name')} {split.get('num_examples', '?')}" for split in info.get("splits") or []
                  if isinstance(split, dict)]
        columns = [f"{feature.get('name')}: {dtype_text(feature)}" for feature in info.get("features") or []
                   if isinstance(feature, dict)]
        if splits:
            lines.append(f"Splits{config}: {', '.join(splits)} examples")
        if columns:
            lines.append(f"Columns{config}: {'; '.join(columns)}")
        if info.get("dataset_size"):
            lines.append(f"Size on disk{config}: {info['dataset_size'] / 1e6:.1f} MB")
    lines.append(f"Use: {raw.get('downloads', 0)} downloads last month, {raw.get('likes', 0)} likes; "
                 f"last modified {(raw.get('lastModified') or '?')[:10]}")
    return "\n".join(lines)


def dtype_text(feature: dict) -> str:
    """A column's type; class labels list their names."""
    dtype = feature.get("dtype")
    if isinstance(dtype, dict) and isinstance(dtype.get("class_label"), dict):
        names = dtype["class_label"].get("names") or {}
        return f"label ({', '.join(map(str, names.values() if isinstance(names, dict) else names))})"
    if dtype is None:
        return "nested" if feature.get("sequence") or feature.get("struct") or feature.get("list") else "?"
    return dtype if isinstance(dtype, str) else "nested"


def strip_front_matter(card: str) -> str:
    """The card without its YAML header (its metadata is in metadata_text already)."""
    if card.startswith("---"):
        end = card.find("\n---", 3)
        if end != -1:
            return card[end + 4:]
    return card
