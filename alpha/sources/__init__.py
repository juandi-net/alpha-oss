"""Sources: where items come from. A source is a module with a few constants and functions, listed in SOURCES.

Every source declares:
    NAME     registry name; also the `source` of its items and the prefix of their ids (`<name>-<native id>`;
             X posts keep their numeric ids)
    TRUST    "curated" (items the user picked; stored as they are) or "discovered" (items Alpha finds; each
             candidate goes through the shared judge, judge.py, before it becomes an item)
    KINDS    the item kinds it produces: post, article, dataset, model, space, paper, repo
    SECRETS  names it needs in .env
    TOOLS    executables it needs on PATH
    CONFIG   its `[sources.<name>]` settings with defaults; always `enabled` (False) and `every_hours` (cadence)
    missing() -> list[str]   optional: what else it still needs, when SECRETS and TOOLS cannot say (either of two
             ways to sign in, say)

A curated source exposes
    collect(connection, settings) -> stats    store new items with storage.store_item (and add_content);
                                              stats is a dict of counts; a `stopped` entry skips enrich
    enrich(connection, settings)              optional: fetch more for stored items (threads, comments)
A discovered source exposes
    discover(connection, settings, needs) -> candidates
                                              search each judge.Need (a query from a focus brief or an open
                                              request), set need.searched on the ones it searched, and return
                                              judge.Candidate objects; raise nothing for a spent budget, just stop
    fetch_text(connection, settings, candidate) -> text
                                              the full text of one stored candidate row, for stage 2 and the item;
                                              raise judge.Stop when the source cannot answer more this run
  optional for a discovered source:
    TASTE = True        judge it against the user's taste (taste.py) as well as the briefs; stage 1 is batched
    SEARCHES = False    it reads a feed instead of searching needs, so open requests never start it
    admit(connection, settings, candidate, rank)   store an admitted candidate itself (default: storage.store_item)
Any source may expose close(), called once at the end of a run (x.py ends its shared X session there).
"""

import os
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from types import ModuleType

from ..config import SECRET_NAMES, Settings, source_config
from ..storage import get_metadata, set_metadata
from . import (github_stars, github_trending, hf_following, hf_likes, hf_trending, huggingface, papers, substack,
               substack_following, x, x_timeline)

SOURCES: dict[str, ModuleType] = {
    module.NAME: module
    for module in (x, github_stars, hf_likes, x_timeline, substack, substack_following, papers, huggingface,
                   hf_following, github_trending, hf_trending)
}


def close_all() -> None:
    for source in SOURCES.values():
        if hasattr(source, "close"):
            source.close()


def options(settings: Settings, source: ModuleType) -> dict:
    return source_config(settings, source.NAME, source.CONFIG)


def enabled(settings: Settings, trust: str | None = None) -> list[ModuleType]:
    return [source for source in SOURCES.values()
            if options(settings, source)["enabled"] and trust in (None, source.TRUST)]


def missing(source: ModuleType) -> list[str]:
    """What the source still needs: secrets not in .env, tools not on PATH, and whatever its own `missing()`
    reports. A discovered source also needs the judge's Claude and Jev keys."""
    secrets = list(source.SECRETS)
    if source.TRUST == "discovered":
        secrets += SECRET_NAMES["claude"] + SECRET_NAMES["jev"]
    return ([name for name in secrets if not os.environ.get(name)]
            + [f"{tool} (not on PATH)" for tool in source.TOOLS if not shutil.which(tool)]
            + (source.missing() if hasattr(source, "missing") else []))


def last_run(connection: sqlite3.Connection, source: ModuleType) -> tuple[str | None, str | None]:
    """(when the last run started, its one-line result)."""
    return get_metadata(connection, f"ran:{source.NAME}"), get_metadata(connection, f"result:{source.NAME}")


def due(connection: sqlite3.Connection, settings: Settings, source: ModuleType) -> bool:
    ran, _ = last_run(connection, source)
    hours = float(options(settings, source)["every_hours"])
    return ran is None or datetime.now(UTC) - datetime.fromisoformat(ran) >= timedelta(hours=hours)


def mark_run(connection: sqlite3.Connection, source: ModuleType, started: str, result: str) -> None:
    """A run counts from when it started, also when it failed, so a source is never asked again sooner."""
    set_metadata(connection, f"ran:{source.NAME}", started)
    set_metadata(connection, f"result:{source.NAME}", result)
