"""Hugging Face trending (discovered): the models, datasets, and Spaces trending on the Hub, admitted only when the
shared judge finds them worth it against the user's focus briefs and their taste.

Each run reads the three trending lists (`GET /api/<models|datasets|spaces>?sort=trendingScore&direction=-1`, with
the metadata the judge needs expanded into the listing; checked 2026-11-04), `per_listing` repos each. Trending
Spaces carry junk (adult and "uncensored" image apps, face swaps, test Spaces): a cheap filter on the Space's name,
title, description, and tags drops the obvious ones, and the judge handles the rest. Repos Alpha already has are
skipped (hf_likes.stored: liked, or seen by hf_following, this source, or the datasets source), and obvious
variants of one release (a model and its GGUF or quantized copies) are collapsed as hf_following does, so the judge
sees each release once; a release this source judged before is skipped.

The shared judge (judge.py) decides as for the X timelines (TASTE): stage 1 reads the name, the facts line, and the
listing's description; stage 2 reads the metadata and the card (hf_following.release_text, one request). Admitted
releases are items of kind model, dataset, or space: project resources that never feed skills.

Politeness: the Hub client of the datasets source (huggingface.Client) with the user's token (hf_likes): one request
at a time, at least `request_delay_seconds` apart, at most `max_requests` per run, listings cached for
LIST_MAX_AGE_DAYS and cards for 30 days in `hf_trending_cache`; a 429 stops until the next run.
"""

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from ..config import Settings, source_config
from ..judge import Candidate, Need, Stop
from . import hf_following, hf_likes, huggingface

NAME = "hf_trending"
TRUST = "discovered"
KINDS = ("model", "dataset", "space")
SECRETS: tuple[str, ...] = ()
TOOLS: tuple[str, ...] = ()
TASTE = True        # judged against the user's taste as well as the focus briefs
SEARCHES = False    # reads the trending lists, not the needs: open requests never start it
CONFIG = {
    "enabled": False,
    "every_hours": 20,
    "request_delay_seconds": 1.0,   # at least this long (plus up to half again) between requests
    "max_requests": 60,             # per run: three listings, then one card per release judged
    "per_listing": 50,              # repos read from each trending list
    "card_characters": 12_000,      # card text given to the judge and kept as item text
}

TABLE = "hf_trending_cache"
LIST_MAX_AGE_DAYS = 0.5
NEW_DAYS = 14                       # the facts line calls a repo created this recently "new"
# Obvious junk among trending Spaces: adult or "uncensored" apps and face swaps (name, title, description, tags) ...
ADULT = re.compile(r"uncensored|nsfw|porn|nude|naked|undress|hentai|erotic|(?<![a-z])eros(?![a-z])|sexy|lewd|"
                   r"face-?swap|deepfake|not-for-all-audiences", re.IGNORECASE)
# ... and test or scratch Spaces (by name only).
SCRATCH = re.compile(r"(?:^|[-_/.])(?:test|testing|tmp|temp|untitled)(?:[-_/.]|$)", re.IGNORECASE)

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
        print(f"Hugging Face trending: {_hub.requests} requests this run, {_hub.cached} answered from cache")
        _hub.prune()
        _hub = None


def trending(hub: huggingface.Client, kind: str, limit: int) -> list[dict]:
    """The public repos of this type trending now, most trending first."""
    query = urlencode([("sort", "trendingScore"), ("direction", "-1"), ("limit", str(limit)),
                       *hf_likes.expand_query(kind), ("expand[]", "trendingScore")])
    repos = hf_likes.get_json(hub, f"/api/{hf_likes.TYPES[kind][0]}?{query}", LIST_MAX_AGE_DAYS) or []
    return [hf_likes.slim(repo) for repo in repos if not repo.get("private") and not repo.get("disabled")]


def junk(kind: str, repo: dict) -> bool:
    """An obvious adult, face-swap, or test Space."""
    if kind != "space":
        return False
    card = repo.get("cardData") or {}
    words = " ".join(map(str, [repo["id"], card.get("title") or "", card.get("short_description") or "",
                               *(repo.get("tags") or []), *(card.get("tags") or [])]))
    return bool(ADULT.search(words) or SCRATCH.search(repo["id"]))


def discover(connection: sqlite3.Connection, settings: Settings, needs: list[Need]) -> list[Candidate]:
    options = source_config(settings, NAME, CONFIG)
    hub = client(connection, settings)
    listed: list[tuple[str, str, dict]] = []
    counts = {kind: 0 for kind in hf_likes.TYPES}
    filtered = 0
    try:
        for kind in hf_likes.TYPES:
            repos = trending(hub, kind, int(options["per_listing"]))
            counts[kind] = len(repos)
            for place, repo in enumerate(repos, 1):
                if junk(kind, repo):
                    filtered += 1
                else:
                    listed.append((kind, f"trending {kind}s", repo | {"trending_place": place}))
    except Stop as error:
        print(f"Hugging Face trending: stopped listing: {error}")
    fresh = [(kind, need, repo) for kind, need, repo in listed if not hf_likes.stored(connection, kind, repo["id"])]
    releases = hf_following.collapse(fresh)
    judged_before = hf_following.judged_groups(connection, NAME, releases)
    since = datetime.now(UTC) - timedelta(days=NEW_DAYS)
    found = []
    for kind, need, repo, variants, key in releases:
        if key in judged_before:
            continue
        owner = repo["id"].split("/")[0]
        found.append(Candidate(
            id=hf_likes.item_id(NAME, kind, repo["id"]), source=NAME, kind=kind, url=hf_likes.repo_url(kind, repo["id"]),
            title=repo["id"],
            subtitle=f"{hf_following.facts_line(kind, repo, since)} · #{repo['trending_place']} of trending {kind}s",
            author=owner, author_url=f"{hf_likes.SITE}/{owner}",
            posted_at=repo.get("createdAt") or repo.get("lastModified"),
            preview=hf_following.preview(kind, repo, variants),
            raw=repo | {"type": kind, "group": key, "variants": variants},
            need=Need(need),
        ))
    kinds = ", ".join(f"{sum(c.kind == kind for c in found)} {kind}s" for kind in hf_likes.TYPES)
    print(f"Hugging Face trending: listed {', '.join(f'{n} {kind}s' for kind, n in counts.items())}; "
          f"{filtered} junk Spaces filtered, {len(listed) - len(fresh)} already known, "
          f"{len(fresh) - len(releases)} variants collapsed into {len(releases)} releases, "
          f"{len(judged_before)} releases judged before, {len(found)} candidates ({kinds}); "
          f"{hub.requests} requests so far, {hub.cached} answered from cache")
    return found


def fetch_text(connection: sqlite3.Connection, settings: Settings, candidate: sqlite3.Row) -> str:
    return hf_following.release_text(client(connection, settings), json.loads(candidate["raw"]),
                                     int(source_config(settings, NAME, CONFIG)["card_characters"]))
