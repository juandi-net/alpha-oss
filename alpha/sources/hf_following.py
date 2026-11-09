"""Hugging Face follows (discovered): what the organizations and people the user follows on Hugging Face release,
judged against the user's focus briefs and taste, so a release they would want shows up without their checking the Hub.

Each run lists, for every account the user follows (`GET /api/users/<user>/following/orgs` and `/following` for people),
its models, datasets, and Spaces created or updated since the last complete listing (the first run: the last
`first_days` days), newest change first (`GET /api/<models|datasets|spaces>?author=<account>&sort=lastModified`,
with the metadata the judge needs expanded into the listing). Repos Alpha already has are skipped (hf_likes.stored:
liked, or seen by this source, hf_trending, or the datasets source).

The big accounts publish many copies of one release (quantizations, GGUF and MLX conversions, precision and
instruct/base variants, checkpoints), so obvious variants are collapsed before the judge: repos of one type whose
names match once those suffixes are stripped, or which are tagged as a quantization of the same base model, form
one release. The main repo (unquantized, fewest suffixes, then most liked) is the candidate; the other names are
listed in its text, so the judge sees each release once. A release whose group an earlier run judged is skipped.

The shared judge (judge.py) decides, as for the X timelines (TASTE): stage 1 reads the name, the facts line, and
the listing's description; stage 2 reads the metadata and the card (`fetch_text`, one request). Models and Spaces
are judged by jev.RELEASE_SAUCE_DEFINITION, datasets by the dataset definition. Admitted releases are items of kind
model, dataset, or space: project resources that never feed skills.

Politeness: every request goes through the Hub client of the datasets source (huggingface.Client), with the user's
token (hf_likes): one at a time, at least `request_delay_seconds` apart, at most `max_requests` per run (listing and
cards share it), listings cached for LIST_MAX_AGE_DAYS and cards for 30 days in `hf_following_cache`; a 429 stops
until the next run.
"""

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from ..config import Settings, source_config
from ..judge import Candidate, Need, Stop
from ..storage import get_metadata, now, set_metadata
from . import hf_likes, huggingface

NAME = "hf_following"
TRUST = "discovered"
KINDS = ("model", "dataset", "space")
SECRETS: tuple[str, ...] = ()
TOOLS: tuple[str, ...] = ()
TASTE = True        # judged against the user's taste as well as the focus briefs
SEARCHES = False    # reads what followed accounts release, not the needs: open requests never start it
CONFIG = {
    "enabled": False,
    "every_hours": 20,
    "request_delay_seconds": 1.0,   # at least this long (plus up to half again) between requests
    "max_requests": 150,            # per run: about 3 listings per followed account, then one card per stage-2 read
    "first_days": 14,               # the first run looks back this far
    "per_listing": 100,             # repos listed per account and type, most recently changed first
    "card_characters": 12_000,      # card text given to the judge and kept as item text
}

TABLE = "hf_following_cache"
LIST_MAX_AGE_DAYS = 0.5
FOLLOWING_MAX_AGE_DAYS = 1
OVERLAP = timedelta(hours=1)        # each window starts this long before the last complete listing ended
LISTED_KEY = f"{NAME}:listed_through"
PREVIEW_CHARACTERS = 1000

# Variant suffixes, stripped from the end of a repo name (repeatedly) to find the release it belongs to.
QUANTIZED = (r"gguf|ggml|mlx|onnx|openvino|awq|gptq|exl[23]|bnb|nf4|nvfp4|mxfp[468]|fp[48]|int[48]|w[48]a(?:8|16)"
             r"|q[2-8](?:_k)?(?:_[a-z0-9]+)?|[2-8]-?bits?|quantized|dynamic|dflash\d*|eagle\d*")
OTHER = r"bf16|fp16|fp32|diffusers|comfy(?:ui)?|dev|instruct|it|chat|base|hf|checkpoints?|ckpt|step-?\d+|\d+k?-?steps?"
SUFFIX = re.compile(rf"[-_.](?:(?P<quantized>{QUANTIZED})|(?P<other>{OTHER}))$", re.IGNORECASE)

_hub: huggingface.Client | None = None


def missing() -> list[str]:
    return hf_likes.missing()


def client(connection: sqlite3.Connection, settings: Settings) -> huggingface.Client:
    """One client per run, so listing and stage 2 share one budget (close() reports it)."""
    global _hub
    if _hub is None or _hub.connection is not connection:
        options = source_config(settings, NAME, CONFIG)
        _hub = hf_likes.hub_client(connection, TABLE, float(options["request_delay_seconds"]),
                                   int(options["max_requests"]))
    return _hub


def close() -> None:
    global _hub
    if _hub is not None:
        print(f"Hugging Face follows: {_hub.requests} requests this run, {_hub.cached} answered from cache")
        _hub.prune()
        _hub = None


def following(hub: huggingface.Client, user: str) -> list[str]:
    """The organizations, then the people, the user follows."""
    accounts = []
    for path in (f"/api/users/{quote(user)}/following/orgs", f"/api/users/{quote(user)}/following"):
        for entry in hf_likes.get_json(hub, path, FOLLOWING_MAX_AGE_DAYS) or []:
            name = entry.get("name") or entry.get("user")
            if name and name not in accounts:
                accounts.append(name)
    return accounts


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def changed_since(hub: huggingface.Client, account: str, kind: str, since: datetime, limit: int) -> tuple[list[dict], bool]:
    """The account's repos of this type changed since `since`, newest change first. Returns (repos, whether the
    listing ran out before reaching `since`: more changed than `limit`)."""
    query = urlencode([("author", account), ("sort", "lastModified"), ("direction", "-1"), ("limit", str(limit)),
                       *hf_likes.expand_query(kind)])
    repos = hf_likes.get_json(hub, f"/api/{hf_likes.TYPES[kind][0]}?{query}", LIST_MAX_AGE_DAYS) or []
    recent = []
    for repo in repos:
        changed = _time(repo.get("lastModified"))
        if changed is None or changed < since:
            return recent, False
        if not repo.get("private") and not repo.get("disabled"):
            recent.append(hf_likes.slim(repo))
    return recent, len(repos) >= limit


def base_name(name: str) -> tuple[str, int, int]:
    """(the name without variant suffixes, lowercased; quantization suffixes stripped; other suffixes stripped)."""
    quantized = other = 0
    while (match := SUFFIX.search(name)) and match.start() > 0:
        quantized += bool(match["quantized"])
        other += not match["quantized"]
        name = name[:match.start()]
    return name.lower(), quantized, other


def quantized_from(repo: dict) -> str | None:
    """The base model a quantized repo was made from."""
    base = repo.get("baseModels") or {}
    if base.get("relation") == "quantized" and base.get("models"):
        return base["models"][0]["id"]
    return next((tag.split(":", 2)[2] for tag in repo.get("tags") or [] if tag.startswith("base_model:quantized:")), None)


def group_key(kind: str, repo: dict) -> str:
    """`<type>:<owner>/<base name>`: the release a repo belongs to (a quantization: its base model's)."""
    owner, name = (quantized_from(repo) or repo["id"]).split("/", 1)
    return f"{kind}:{owner.lower()}/{base_name(name)[0]}"


def _preference(repo: dict) -> tuple:
    """Lower is a better main repo: not a quantized copy, fewest suffixes, most liked, most downloaded."""
    _, quantized, other = base_name(repo["id"].split("/", 1)[1])
    return (quantized_from(repo) is not None, quantized, other, -(repo.get("likes") or 0), -(repo.get("downloads") or 0))


def collapse(listed: list[tuple[str, str, dict]]) -> list[tuple[str, str, dict, list[str], str]]:
    """Group (type, account, repo) into releases. Returns (type, account, main repo, the variants' ids, group key)."""
    groups: dict[str, list[tuple[str, str, dict]]] = {}
    for kind, account, repo in listed:
        groups.setdefault(group_key(kind, repo), []).append((kind, account, repo))
    releases = []
    for key, members in groups.items():
        members.sort(key=lambda member: _preference(member[2]))
        kind, account, main = members[0]
        releases.append((kind, account, main, [repo["id"] for _, _, repo in members[1:]], key))
    return releases


def facts_line(kind: str, repo: dict, since: datetime) -> str:
    """One line for stage 1: type, task, size, license, use, and whether it is new or an update."""
    card = repo.get("cardData") or {}
    parts = [f"Hugging Face {'Space' if kind == 'space' else kind} by {repo['id'].split('/')[0]}"]
    if kind == "model":
        parts += [part for part in (repo.get("pipeline_tag"), hf_likes.parameters(repo)) if part]
    elif kind == "dataset":
        tags = repo.get("tags") or []
        parts += [", ".join(tag.split(":", 1)[1] for tag in tags if tag.startswith(prefix))
                  for prefix in ("size_categories:", "task_categories:") if any(tag.startswith(prefix) for tag in tags)]
    else:
        parts.append(f"{repo.get('sdk') or card.get('sdk') or '?'} app")
    parts.append(f"license {hf_likes.license_text(repo)}")
    parts.append((f"{repo.get('downloads') or 0:,} downloads last month, " if kind != "space" else "")
                 + f"{repo.get('likes') or 0:,} likes")
    created = _time(repo.get("createdAt"))
    parts.append(f"new {created:%Y-%m-%d}" if created and created >= since
                 else f"updated {(repo.get('lastModified') or '?')[:10]} (created {(repo.get('createdAt') or '?')[:10]})")
    return " · ".join(parts)


def variants_line(variants: list[str]) -> str:
    return f"Variants of this release (not judged separately): {', '.join(variants)}" if variants else ""


def preview(kind: str, repo: dict, variants: list[str]) -> str:
    """The listing's own words (a dataset's card opening, a Space's description) and topic tags, then the variants."""
    card = repo.get("cardData") or {}
    words = huggingface.card_summary(repo) if kind == "dataset" else card.get("short_description") or ""
    tags = [tag for tag in repo.get("tags") or [] if ":" not in tag and tag not in ("region", "endpoints_compatible")]
    parts = [words[:PREVIEW_CHARACTERS], f"Tags: {', '.join(tags[:15])}" if tags else "", variants_line(variants)]
    return "\n".join(part for part in parts if part)


def discover(connection: sqlite3.Connection, settings: Settings, needs: list[Need]) -> list[Candidate]:
    options = source_config(settings, NAME, CONFIG)
    hub = client(connection, settings)
    listed_through = get_metadata(connection, LISTED_KEY)
    since = (datetime.fromisoformat(listed_through) - OVERLAP if listed_through
             else datetime.now(UTC) - timedelta(days=float(options["first_days"])))
    started = now()
    listed: list[tuple[str, str, dict]] = []
    accounts: list[str] = []
    listings = truncated = 0
    complete = False
    try:
        accounts = following(hub, hf_likes.username(hub))
        for account in accounts:
            for kind in hf_likes.TYPES:
                repos, more = changed_since(hub, account, kind, since, int(options["per_listing"]))
                listings += 1
                truncated += more
                listed += [(kind, account, repo) for repo in repos]
        complete = True
    except Stop as error:
        print(f"Hugging Face follows: stopped listing: {error}; the window is listed again next run")
    fresh = [(kind, account, repo) for kind, account, repo in listed if not hf_likes.stored(connection, kind, repo["id"])]
    releases = collapse(fresh)
    judged_before = judged_groups(connection, NAME, releases)
    found = []
    for kind, account, repo, variants, key in releases:
        if key in judged_before:
            continue
        owner = repo["id"].split("/")[0]
        found.append(Candidate(
            id=hf_likes.item_id(NAME, kind, repo["id"]), source=NAME, kind=kind, url=hf_likes.repo_url(kind, repo["id"]),
            title=repo["id"], subtitle=facts_line(kind, repo, since), author=owner,
            author_url=f"{hf_likes.SITE}/{owner}", posted_at=repo.get("createdAt") or repo.get("lastModified"),
            preview=preview(kind, repo, variants),
            raw=repo | {"type": kind, "account": account, "group": key, "variants": variants},
            need=Need(account),
        ))
    if complete:  # a partial listing leaves the window open, so the next run lists it again
        set_metadata(connection, LISTED_KEY, started)
        connection.commit()
    kinds = ", ".join(f"{sum(c.kind == kind for c in found)} {kind}s" for kind in hf_likes.TYPES)
    print(f"Hugging Face follows: {len(accounts)} accounts, {listings} listings since {since:%Y-%m-%d %H:%M} UTC, "
          f"{len(listed)} repos changed, {len(listed) - len(fresh)} already stored, "
          f"{len(fresh) - len(releases)} variants collapsed into {len(releases)} releases, "
          f"{len(judged_before)} releases judged before, {len(found)} candidates ({kinds}); "
          f"{hub.requests} requests so far, {hub.cached} answered from cache"
          + (f"; {truncated} listings hit per_listing ({options['per_listing']}), older changes there were not read"
             if truncated else ""))
    return found


def judged_groups(connection: sqlite3.Connection, source: str, releases: list[tuple]) -> set[str]:
    """The group keys (collapse) of releases this source has seen as a candidate before, by any of its repos."""
    return {key for *_, key in releases if connection.execute(
        "SELECT 1 FROM candidates WHERE source = ? AND json_extract(raw, '$.group') = ?", (source, key)
    ).fetchone()}


def release_text(hub: huggingface.Client, raw: dict, characters: int) -> str:
    """Metadata from the listing, the variants, then the card (one request, cached)."""
    kind = raw["type"]
    card = hf_likes.card(hub, kind, raw["id"], characters)
    parts = [hf_likes.metadata_text(kind, raw), variants_line(raw.get("variants") or []),
             f"Card:\n{card}" if card else "No card."]
    return "\n\n".join(part for part in parts if part)


def fetch_text(connection: sqlite3.Connection, settings: Settings, candidate: sqlite3.Row) -> str:
    return release_text(client(connection, settings), json.loads(candidate["raw"]),
                        int(source_config(settings, NAME, CONFIG)["card_characters"]))
