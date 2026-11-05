"""The shared judge: every candidate a discovered source offers goes through it before it becomes an item.

1. Needs: Claude writes 3 to 6 search queries per project from its focus brief (again whenever the brief
   changes). A source gets the queries it has not searched within its `requery_days`, after the open requests
   (discovery an agent asked for in the `requests` table).
2. The source searches them (`discover`) and returns candidates; each is stored once in `candidates`, so
   nothing is judged twice.
3. Stage 1 (cheap): Jev scores each new candidate's title and preview against every focus brief (a requested
   candidate: against its request); candidates that fit none by `fit_threshold` are dropped.
4. Stage 2 (survivors, requested ones first, then best fit): the source reads the full text (`fetch_text`), which goes to two
   independent yes/no judges with the same definition of sauce for its kind (jev.sauce_definition) and the brief it fits
   best: Jev (a probability) and Claude (`[judge] model`, a yes or no with a one-sentence why). Jev also checks
   whether it repeats an item or skill the user already has (novelty is 1 minus the closest repeat). The
   author's track record (share of their earlier judged candidates that were finds) is kept alongside. A feed
   source (TASTE) also gives both judges the user's taste with the three items they picked (any curated source) and
   the three they rated β most like it (taste.context); their ids are kept on the candidate (`examples`) for the how?.
5. A find is a candidate both judges call sauce (Jev at SAUCE_PROBABILITY or more) that is new (`novelty`); an idea
   is a new candidate exactly one judge calls sauce (Jev is strict about "usable now on the current work"; Claude
   often says yes to what one could act on later), with that judge kept in `idea_judge`. Both are admitted into items
   (trust discovered, `tier` find or idea), so the pipeline summarizes, classifies, and matches them like any other
   item; an idea feeds no skill until the user rates it α (taste.rate makes it a find). Everything else stays in
   `candidates`.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import ModuleType

from . import focus, taste, writer
from .config import Settings
from .jev import Jev, shortlist
from .pipeline import parallel
from .storage import now, store_item

NOVELTY_TEXT_CHARACTERS = 2000
SAUCE_PROBABILITY = 0.5
# The context name for "fits the user's taste" (feed sources: TASTE = True), beside project names and request:<id>.
TASTE = "taste"
FEED_PREVIEW_CHARACTERS = 600


@dataclass
class Need:
    """Something to search for: a query from a project's brief, or an open request."""

    query: str
    project: str | None = None
    request_id: int | None = None
    searched: bool = False           # the source sets it once it has searched the query


@dataclass
class Candidate:
    """What a discovered source returns for each result. `id` is `<source>-<native id>`."""

    id: str
    source: str
    kind: str
    url: str
    title: str
    subtitle: str = ""
    author: str | None = None
    author_url: str | None = None
    posted_at: str | None = None
    preview: str = ""
    paid: bool | None = None         # only a public preview can be read
    raw: dict = field(default_factory=dict)
    need: Need | None = None


class Stop(Exception):
    """A source cannot answer more this run (its request budget is spent, or it refused). What it already
    returned is kept; the rest waits for the next run."""


def write_queries(connection: sqlite3.Connection, settings: Settings, briefs: dict[str, sqlite3.Row]) -> None:
    """Queries for every brief that has none yet (a new or edited brief gets new ones)."""
    pending = [
        brief for brief in briefs.values()
        if not connection.execute("SELECT 1 FROM queries WHERE brief_id = ?", (brief["id"],)).fetchone()
    ]

    def task(brief: sqlite3.Row) -> list[str]:
        return writer.search_queries(settings.claude_model, brief["project"], brief["brief"])

    def store(brief: sqlite3.Row, queries: list[str]) -> None:
        connection.executemany(
            "INSERT INTO queries (project, query, brief_id) VALUES (?, ?, ?) "
            "ON CONFLICT(project, query) DO UPDATE SET brief_id = excluded.brief_id",
            [(brief["project"], query, brief["id"]) for query in queries],
        )
        connection.commit()

    if pending:
        parallel(pending, task, store, "Search queries (Claude)")


def open_requests(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM requests WHERE status = 'open' ORDER BY id").fetchall()


def needs(connection: sqlite3.Connection, settings: Settings, source: str, requery_days: float,
          requests: list[sqlite3.Row], briefs: dict[str, sqlite3.Row] | None) -> list[Need]:
    """The open requests first, then (with briefs) the brief queries this source has not searched lately,
    never-searched first."""
    found = [Need(row["query"], row["project"], row["id"]) for row in requests]
    if briefs:
        write_queries(connection, settings, briefs)
        since = (datetime.now(UTC) - timedelta(days=requery_days)).isoformat()
        current = [brief["id"] for brief in briefs.values()]
        found += [
            Need(row["query"], row["project"])
            for row in connection.execute(
                f"SELECT q.project, q.query, s.searched_at FROM queries q LEFT JOIN searches s "
                f"ON s.source = ? AND s.query = q.query WHERE q.brief_id IN ({','.join('?' * len(current))}) "
                "AND (s.searched_at IS NULL OR s.searched_at < ?) ORDER BY s.searched_at IS NOT NULL, s.searched_at",
                [source, *current, since],
            )
        ]
    return found


def remember(connection: sqlite3.Connection, candidates: list[Candidate]) -> set[str]:
    """Store candidates the first time they are seen. Returns the ids that were new."""
    new = set()
    for candidate in candidates:
        need = candidate.need or Need("")
        cursor = connection.execute(
            "INSERT OR IGNORE INTO candidates (id, source, kind, url, title, subtitle, author, author_url, posted_at, "
            "preview, paid, raw, project, query, request_id, status, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'seen', ?)",
            (candidate.id, candidate.source, candidate.kind, candidate.url, candidate.title, candidate.subtitle,
             candidate.author, candidate.author_url, candidate.posted_at, candidate.preview,
             None if candidate.paid is None else int(candidate.paid), json.dumps(candidate.raw, ensure_ascii=False),
             need.project, need.query or None, need.request_id, now()),
        )
        if cursor.rowcount == 1:
            new.add(candidate.id)
    connection.commit()
    return new


def contexts(connection: sqlite3.Connection) -> dict[str, str]:
    """What candidates are judged against: each project's current brief, `request:<id>` for every request (the
    query, with its project's brief when it names one), and `taste` (the user's taste profile, for feed sources)."""
    briefs = {project: row["brief"] for project, row in focus.latest_briefs(connection).items()}
    result = dict(briefs)
    for row in connection.execute("SELECT id, query, project FROM requests"):
        text = f"An agent asked for: {row['query']}"
        if row["project"] in briefs:
            text += f"\n\nIt is for the project {row['project']}. Focus brief:\n{briefs[row['project']]}"
        result[f"request:{row['id']}"] = text
    if profile := taste.profile(connection):
        result[TASTE] = profile
    return result


def stage_one(connection: sqlite3.Connection, settings: Settings, jev: Jev, source: ModuleType,
              against: dict[str, str]) -> int:
    """Fit each new candidate to the briefs (a requested one: to its request). A feed source (TASTE) is fitted to
    the briefs or the user's taste, many posts per Jev request, so a post the user would keep passes even when it fits
    no project."""
    projects = {name: brief for name, brief in against.items() if not name.startswith("request:") and name != TASTE}
    rows = connection.execute("SELECT * FROM candidates WHERE source = ? AND status = 'seen'", (source.NAME,)).fetchall()
    fitted: list[tuple[sqlite3.Row, float, str]] = []
    if getattr(source, "TASTE", False):
        feed = projects | ({TASTE: against[TASTE]} if TASTE in against else {})
        if feed and rows:
            posts = ["\n".join(filter(None, (row["title"], row["subtitle"], row["preview"])))[:FEED_PREVIEW_CHARACTERS]
                     for row in rows]
            fitted = [(row, score, best) for row, (score, best) in zip(rows, jev.fit_feed(feed, posts))]
    else:
        for row in rows:
            request = f"request:{row['request_id']}"
            pairs = [(request, against[request])] if request in against else list(projects.items())
            if pairs:
                fits = jev.fit("\n".join(filter(None, (row["title"], row["subtitle"], row["preview"]))), pairs)
                best = max(fits, key=fits.get)
                fitted.append((row, fits[best], best))
    passed = 0
    for row, score, best in fitted:
        ok = score >= settings.judge.fit_threshold
        connection.execute("UPDATE candidates SET fit = ?, fit_project = ?, status = ? WHERE id = ?",
                           (score, best, "passed" if ok else "dropped", row["id"]))
        passed += ok
    connection.commit()
    return passed


def novelty_candidates(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """What the user already has: each distinct item idea and each skill topic."""
    candidates = [
        (row["id"], f"{row['title'] or ''}: {row['summary'] or row['text'][:300]}")
        for row in connection.execute("SELECT id, title, summary, text FROM items WHERE duplicate_of IS NULL")
    ]
    candidates += [
        (f"skill:{row['name']}", f"Skill {row['title']}: {row['description']}")
        for row in connection.execute("SELECT name, title, description FROM topics WHERE status = 'active'")
    ]
    return candidates


def _track_record(connection: sqlite3.Connection, author_url: str | None) -> float | None:
    row = connection.execute(
        "SELECT COUNT(*) AS judged, SUM(status = 'find') AS finds FROM candidates "
        "WHERE author_url = ? AND status IN ('find', 'idea', 'below')",
        (author_url,),
    ).fetchone()
    return row["finds"] / row["judged"] if row["judged"] else None


def stage_two(connection: sqlite3.Connection, settings: Settings, jev: Jev, source: ModuleType,
              against: dict[str, str]) -> tuple[int, list[str], str | None]:
    """Survivors, requested ones first, then best stage-1 fit, while the source can read them. Returns (judged,
    finds and ideas as one line each, why it stopped early)."""
    known = novelty_candidates(connection)
    judged, finds = 0, []
    rows = connection.execute(
        "SELECT * FROM candidates WHERE source = ? AND status = 'passed' ORDER BY request_id IS NULL, fit DESC",
        (source.NAME,),
    ).fetchall()
    for row in rows:
        context = row["fit_project"]
        if context not in against:  # the project is no longer active: fit it to the current briefs again
            connection.execute("UPDATE candidates SET status = 'seen' WHERE id = ?", (row["id"],))
            connection.commit()
            continue
        try:
            text = source.fetch_text(connection, settings, row)
        except Stop as error:
            return judged, finds, str(error)
        except Exception as error:  # a deleted page or an answer that does not parse: not tried again
            connection.execute("UPDATE candidates SET status = 'failed', why = ? WHERE id = ?",
                               (f"{type(error).__name__}: {error}"[:300], row["id"]))
            connection.commit()
            continue
        state = f"{row['title']}\n{row['subtitle'] or ''}\n\n{text}"
        brief = against[context]
        label, examples = context, None
        if getattr(source, "TASTE", False):  # user taste, similar bookmarks as examples, β items as counter-examples
            liked, examples = taste.context(connection, f"{row['title']} {text[:NOVELTY_TEXT_CHARACTERS]}")
            if context == TASTE:
                label, brief = "none in particular: judge it by the user's taste below", liked
            elif liked:
                brief = f"{brief}\n\n{liked}"
        try:
            verdict = writer.judge_sauce(settings.judge.model, label, brief, state, row["kind"])
        except Exception as error:  # the Claude endpoint is down or the answer did not parse: judged next run
            print(f"Judge: Claude failed: {type(error).__name__}")
            continue
        others = [candidate for candidate in known if candidate[0] != row["id"]]
        scores = jev.sauce(state, label, brief, shortlist(f"{row['title']} {state[:NOVELTY_TEXT_CHARACTERS]}", others),
                           row["kind"])
        status, idea_judge = verdict_of(settings, scores.sauce, verdict.sauce, scores.novelty)
        if status != "below":  # a second candidate with the same idea in this run is not new
            known.append((row["id"], f"{row['title']}: {row['subtitle'] or row['preview'] or ''}"))
            finds.append(f"{status}{f' ({idea_judge} said yes)' if idea_judge else ''}: {row['title']} "
                         f"({row['author']}): {verdict.why}")
        connection.execute(
            "UPDATE candidates SET jev_sauce = ?, judge_sauce = ?, why = ?, novelty = ?, track_record = ?, "
            "similar_to = ?, examples = ?, status = ?, idea_judge = ?, scored_at = ? WHERE id = ?",
            (scores.sauce, int(verdict.sauce), verdict.why, scores.novelty, _track_record(connection, row["author_url"]),
             scores.similar_to, json.dumps(examples) if examples else None, status, idea_judge, now(), row["id"]),
        )
        connection.commit()
        judged += 1
    return judged, finds, None


def verdict_of(settings: Settings, jev_sauce: float, judge_sauce: bool, novelty: float) -> tuple[str, str | None]:
    """(find | idea | below, the judge that said yes for an idea): a repeat is below the bar whatever the judges say."""
    jev = jev_sauce >= SAUCE_PROBABILITY
    if novelty < settings.judge.novelty or not (jev or judge_sauce):
        return "below", None
    if jev and judge_sauce:
        return "find", None
    return "idea", "jev" if jev else "claude"


def settle(connection: sqlite3.Connection, settings: Settings, source: str) -> None:
    """Apply the current bar to every judged candidate not yet admitted (find, idea, or below)."""
    connection.execute(
        "UPDATE candidates SET status = CASE WHEN novelty < ? THEN 'below' "
        "WHEN jev_sauce >= ? AND judge_sauce = 1 THEN 'find' WHEN jev_sauce >= ? OR judge_sauce = 1 THEN 'idea' "
        "ELSE 'below' END WHERE source = ? AND status IN ('find', 'idea', 'below') AND id NOT IN (SELECT id FROM items)",
        (settings.judge.novelty, SAUCE_PROBABILITY, SAUCE_PROBABILITY, source),
    )
    connection.execute(
        "UPDATE candidates SET idea_judge = CASE WHEN status != 'idea' THEN NULL WHEN judge_sauce = 1 THEN 'claude' "
        "ELSE 'jev' END WHERE source = ? AND status IN ('find', 'idea', 'below') AND id NOT IN (SELECT id FROM items)",
        (source,),
    )
    connection.commit()


def admit(connection: sqlite3.Connection, settings: Settings, source: ModuleType) -> int:
    """Store finds and ideas as items (`tier`); the pipeline's next step summarizes, classifies, and matches them. A
    source with its own `admit(connection, settings, row, rank)` stores the item itself (X posts keep their images and
    quotes)."""
    rows = connection.execute(
        "SELECT * FROM candidates WHERE source = ? AND status IN ('find', 'idea') AND id NOT IN (SELECT id FROM items) "
        "ORDER BY status = 'idea'",
        (source.NAME,),
    ).fetchall()
    admitted = 0
    for row in rows:
        rank = connection.execute("SELECT COALESCE(MAX(rank), 0) + 1 FROM items").fetchone()[0]
        if hasattr(source, "admit"):
            source.admit(connection, settings, row, rank)
        else:
            try:
                text = source.fetch_text(connection, settings, row)  # the source's cache answers when stage 2 read it
            except Stop:
                break
            store_item(connection, {
                "id": row["id"], "source": row["source"], "kind": row["kind"], "trust": "discovered", "url": row["url"],
                "title_source": row["title"], "author": row["author"], "created_at": row["posted_at"], "rank": rank,
                "text": "\n\n".join(filter(None, (row["title"], row["subtitle"], text))), "raw": json.loads(row["raw"]),
            })
        connection.execute("UPDATE items SET tier = ? WHERE id = ?", (row["status"], row["id"]))
        connection.commit()
        admitted += 1
    return admitted


def run(connection: sqlite3.Connection, settings: Settings, jev: Jev, source: ModuleType, wanted: list[Need]) -> str:
    """One discovered source: discover, remember, judge, admit. Returns a one-line result."""
    found = source.discover(connection, settings, wanted)
    new = remember(connection, found)
    for need in wanted:
        if not need.searched:
            continue
        count = sum(1 for candidate in found if candidate.need is need and candidate.id in new)
        if need.request_id:
            connection.execute("UPDATE requests SET status = 'judging' WHERE id = ? AND status = 'open'",
                               (need.request_id,))
        else:
            connection.execute(
                "INSERT INTO searches (source, query, searched_at, found) VALUES (?, ?, ?, ?) ON CONFLICT(source, query) "
                "DO UPDATE SET searched_at = excluded.searched_at, found = excluded.found",
                (source.NAME, need.query, now(), count),
            )
    connection.commit()
    if getattr(source, "TASTE", False):
        taste.refresh(connection, settings)
    against = contexts(connection)
    passed = stage_one(connection, settings, jev, source, against)
    judged, finds, stopped = stage_two(connection, settings, jev, source, against)
    settle(connection, settings, source.NAME)
    admitted = admit(connection, settings, source)
    waiting = connection.execute(
        "SELECT COUNT(*) FROM candidates WHERE source = ? AND status = 'passed'", (source.NAME,)
    ).fetchone()[0]
    ideas = sum(line.startswith("idea") for line in finds)
    result = (f"{sum(n.searched for n in wanted)} searches, {len(new)} new candidates, {passed} passed stage 1, "
              f"{judged} judged, {len(finds) - ideas} finds, {ideas} ideas, {admitted} admitted"
              + (f"; {waiting} wait for the next run" if waiting else "") + (f"; stopped: {stopped}" if stopped else ""))
    print(f"Judge ({source.NAME}): {result}")
    for find in finds:
        print(f"  {find}")
    return result


def close_requests(connection: sqlite3.Connection) -> None:
    """A searched request is done once none of its candidates waits to be judged."""
    for row in connection.execute("SELECT id FROM requests WHERE status = 'judging'").fetchall():
        pending = connection.execute(
            "SELECT COUNT(*) FROM candidates WHERE request_id = ? AND status IN ('seen', 'passed')", (row["id"],)
        ).fetchone()[0]
        if pending:
            continue
        admitted = connection.execute(
            "SELECT COUNT(*) FROM candidates c JOIN items i ON i.id = c.id WHERE c.request_id = ?", (row["id"],)
        ).fetchone()[0]
        connection.execute("UPDATE requests SET status = 'done', handled_at = ?, result_count = ? WHERE id = ?",
                           (now(), admitted, row["id"]))
        print(f"Requests: request {row['id']} done, {admitted} items admitted")
    connection.commit()
