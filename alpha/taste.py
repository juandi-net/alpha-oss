"""The user's taste: what they keep and what they call slop, written by Claude from everything they picked (X
bookmarks, GitHub stars, Hugging Face likes: every curated source, sampled so each is represented) and their α / β
ratings in the dashboard, and rewritten weekly or on the next run after a rating changes. The judge gives it, with
the three picked items and the three β items most like the candidate as examples and counter-examples, to both
stage-2 judges for feed sources (a source with TASTE = True, such as the X timelines and Hugging Face follows), and
stage 1 lets a candidate through when it fits the user's taste even if it fits no project's focus brief."""

import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from . import writer
from .config import Settings
from .jev import shortlist
from .storage import get_metadata, now, set_metadata

REFRESH_DAYS = 7
PICKED = 300                     # picked items the profile is written from, newest first from each source in turn
RATED = 100                      # newest α and newest β ratings the profile is written from
ITEM_CHARACTERS = 400
MIN_PICKED = 20
# How each curated source's items are named to Claude and the judges; a source not listed goes by its name.
PICKED_AS = {"x": "X bookmark", "github_stars": "GitHub star", "hf_likes": "Hugging Face like"}
EXAMPLES = 3
# α: a real edge, kept. β: no edge (generic, slop, or already known; in finance beta is what everyone gets).
RATINGS = ("alpha", "beta")
REASONS = {"slop": "slop", "not_interested": "not interested", "not_now": "not now", "knew_it": "knew it"}
# Metadata: when a rating last changed. A profile written before it is stale.
RATED_AT = "taste_rated_at"


def _picked(connection: sqlite3.Connection, limit: int | None = PICKED) -> list[sqlite3.Row]:
    """Distinct summarized items from every curated source. With a limit: newest first from each source in turn, so
    a small source (the user's Hugging Face likes) is not drowned by their bookmarks."""
    rows = connection.execute(
        "SELECT id, source, title, summary FROM items WHERE trust = 'curated' AND summary IS NOT NULL "
        "AND duplicate_of IS NULL ORDER BY rank DESC"
    ).fetchall()
    if limit is None:
        return rows
    by_source: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_source[row["source"]].append(row)
    queues = list(by_source.values())
    sample: list[sqlite3.Row] = []
    for turn in range(max(map(len, queues), default=0)):
        sample += [queue[turn] for queue in queues if turn < len(queue)]
    return sample[:limit]


def _named(row: sqlite3.Row) -> str:
    return f"[{PICKED_AS.get(row['source'], row['source'])}] {row['title']}: {(row['summary'] or '')[:ITEM_CHARACTERS]}"


def _rated(connection: sqlite3.Connection, rating: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT i.id, COALESCE(i.title, i.title_source, '') AS title, COALESCE(i.summary, substr(i.text, 1, ?)) AS summary, "
        "r.reason FROM ratings r JOIN items i ON i.id = r.item_id WHERE r.rating = ? ORDER BY r.rated_at DESC LIMIT ?",
        (ITEM_CHARACTERS, rating, RATED),
    ).fetchall()


def _described(row: sqlite3.Row) -> str:
    reason = f" (the user's reason: {REASONS[row['reason']]})" if row["reason"] in REASONS else ""
    return f"{row['title']}: {(row['summary'] or '')[:ITEM_CHARACTERS]}{reason}"


def rate(connection: sqlite3.Connection, item_id: str, rating: str | None, reason: str | None = None) -> None:
    """Save the user's α or β on an item (None clears it; a reason goes only with β) and mark the taste profile stale,
    so the next run writes it again. α on an idea makes it a find (it may feed skills); clearing that α makes it an
    idea again."""
    if rating is None:
        connection.execute("DELETE FROM ratings WHERE item_id = ?", (item_id,))
    else:
        connection.execute(
            "INSERT INTO ratings (item_id, rating, reason, rated_at) VALUES (?, ?, ?, ?) ON CONFLICT (item_id) "
            "DO UPDATE SET rating = excluded.rating, reason = excluded.reason, rated_at = excluded.rated_at",
            (item_id, rating, reason if rating == "beta" else None, now()),
        )
    connection.execute(
        "UPDATE items SET tier = CASE WHEN EXISTS (SELECT 1 FROM ratings r WHERE r.item_id = items.id "
        "AND r.rating = 'alpha') THEN 'find' ELSE 'idea' END "
        "WHERE id = ? AND id IN (SELECT id FROM candidates WHERE status = 'idea')",
        (item_id,),
    )
    set_metadata(connection, RATED_AT, now())


def _rated_suggestions(connection: sqlite3.Connection, rating: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT s.project, s.action, s.reason, COALESCE(i.title, i.title_source, '') AS title FROM suggestions s "
        "JOIN items i ON i.id = s.item_id WHERE s.rating = ? ORDER BY s.rated_at DESC LIMIT ?",
        (rating, RATED),
    ).fetchall()


def _suggested(row: sqlite3.Row) -> str:
    reason = f" (the user's reason: {REASONS[row['reason']]})" if row["reason"] in REASONS else ""
    return f"from “{row['title']}”: {row['action']}{reason}"


def rate_suggestion(connection: sqlite3.Connection, item_id: str, project: str, rating: str | None,
                    reason: str | None = None) -> None:
    """Save the user's α or β on a suggestion (None clears it; a reason only with β) and mark the taste profile stale:
    what they keep tells the next profile what kind of application they value."""
    connection.execute(
        "UPDATE suggestions SET rating = ?, reason = ?, rated_at = ? WHERE item_id = ? AND project = ?",
        (rating, reason if rating == "beta" else None, now() if rating else None, item_id, project),
    )
    set_metadata(connection, RATED_AT, now())


def profile(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT profile FROM taste_profiles ORDER BY id DESC LIMIT 1").fetchone()
    return row["profile"] if row else None


def stale(connection: sqlite3.Connection, row: sqlite3.Row | None) -> bool:
    """The profile `row` is older than REFRESH_DAYS, was written from X bookmarks alone (no `sources`), or a rating
    changed after it was written."""
    if not row or row["sources"] is None:
        return True
    rated = get_metadata(connection, RATED_AT)
    written = datetime.fromisoformat(row["created_at"])
    return (datetime.now(UTC) - written >= timedelta(days=REFRESH_DAYS)
            or (rated is not None and datetime.fromisoformat(rated) > written))


def refresh(connection: sqlite3.Connection, settings: Settings, force: bool = False) -> str | None:
    """The current profile, written again when it is stale (or with force)."""
    row = connection.execute("SELECT * FROM taste_profiles ORDER BY id DESC LIMIT 1").fetchone()
    if not force and not stale(connection, row):
        return row["profile"]
    picked = _picked(connection)
    if len(picked) < MIN_PICKED:
        return row["profile"] if row else None
    listing = "\n".join(f"- {_named(item)}" for item in picked)
    sources: dict[str, int] = defaultdict(int)
    for item in picked:
        sources[item["source"]] += 1
    kept, rejected = _rated(connection, "alpha"), _rated(connection, "beta")
    if kept:
        listing += ("\n\nItems the user rated α in their Alpha feed (a real edge: they keep these):\n"
                    + "\n".join(f"- {_described(r)}" for r in kept))
    if rejected:
        listing += ("\n\nItems they rated β in their Alpha feed (no edge: generic, slop, or something they already "
                    "knew); count them as what they reject:\n" + "\n".join(f"- {_described(r)}" for r in rejected))
    for rating, heading in (("alpha", "Suggestions Alpha made that they rated α (the kind of application they value)"),
                            ("beta", "Suggestions Alpha made that the user rated β (applications they do not want)")):
        rows = _rated_suggestions(connection, rating)
        if rows:
            listing += f"\n\n{heading}:\n" + "\n".join(f"- {_suggested(r)}" for r in rows)
    text = writer.write_taste(settings.claude_model, listing)
    connection.execute("INSERT INTO taste_profiles (profile, items, sources, created_at) VALUES (?, ?, ?, ?)",
                       (text, len(picked), json.dumps(sources), now()))
    connection.commit()
    print(f"Taste: profile written from {len(picked)} picked items ({', '.join(f'{n} {s}' for s, n in sources.items())}), "
          f"{len(kept)} α and {len(rejected)} β ratings")
    return text


def context(connection: sqlite3.Connection, text: str) -> tuple[str, dict[str, list[str]]]:
    """The profile plus the picked items (any curated source) and the β items most like `text` (word overlap of title
    and summary), for the judges, and the ids of those examples ({kept, rejected}, stored on the candidate for the
    dashboard's how?)."""
    current = profile(connection)
    if not current:
        return "", {}
    kept = shortlist(text, [(row["id"], _named(row)) for row in _picked(connection, None)], EXAMPLES)
    rejected = shortlist(text, [(row["id"], _described(row)) for row in _rated(connection, "beta")], EXAMPLES)
    result = (f"The user's taste.\n{current}\n\nWhat they picked (bookmarked, starred, liked) most like this "
              "one:\n" + "\n".join(f"- {description}" for _, description in kept))
    if rejected:
        result += ("\n\nItems they rated β (no edge) most like this one; do not pass it for the same "
                   "reasons:\n" + "\n".join(f"- {description}" for _, description in rejected))
    return result, {"kept": [item_id for item_id, _ in kept], "rejected": [item_id for item_id, _ in rejected]}
