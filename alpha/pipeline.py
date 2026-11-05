"""Processing steps. Each step selects only unfinished work and commits per item, so an
interrupted or partly failed run resumes where it stopped on the next run."""

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import writer
from .config import Settings
from .jev import Jev, shortlist
from .storage import content_of, get_metadata, now, set_metadata

CLAUDE_WORKERS = 4
# Suggestions written per run (Opus, one call per item and project): strongest first, the rest on later runs. The
# first run with none yet writes FIRST_SUGGESTIONS, so the backlog of matches does not go out in one burst.
SUGGESTIONS_PER_RUN = 30
FIRST_SUGGESTIONS = 150
# Headlines and outcomes for suggestions written before they had them, per Claude call.
FRAMES_PER_CALL = 40


def parallel(items: list, task: Callable, store: Callable, label: str) -> None:
    """Run `task(item)` on worker threads; `store(item, result)` runs on this thread (SQLite)."""
    failures = 0
    with ThreadPoolExecutor(CLAUDE_WORKERS) as pool:
        futures = {pool.submit(task, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                store(item, future.result())
            except Exception as error:  # reported and left unfinished; the next run retries it
                failures += 1
                print(f"{label}: failed: {type(error).__name__}: {error}")
    print(f"{label}: {len(items) - failures} done, {failures} failed")


def jev_state(row: sqlite3.Row) -> str:
    return f"{row['title']}\n\n{row['summary']}\n\n{writer.item_text(row)}"


def sync_jev_version(connection: sqlite3.Connection, jev: Jev) -> None:
    """Scores from two Jev versions are not comparable against one threshold. When the version behind
    the configured model changes, drop project and skill-topic scores so this run makes them again.
    Claude's own topic picks (probability 1.0) and items the user removed from a skill are kept."""
    version = jev.version()
    stored = get_metadata(connection, "jev_version")
    if stored and stored != version:
        connection.execute("DELETE FROM relevance")
        connection.execute("DELETE FROM topic_scores WHERE probability < 1.0 AND excluded = 0")
        print(f"Jev: {stored} -> {version}, every item will be scored again")
    set_metadata(connection, "jev_version", version)


def summarize_pending(connection: sqlite3.Connection, settings: Settings) -> None:
    rows = connection.execute("SELECT * FROM items WHERE summary IS NULL").fetchall()

    def store(row: sqlite3.Row, result: writer.Summary) -> None:
        connection.execute("UPDATE items SET title = ?, summary = ? WHERE id = ?", (result.title, result.summary, row["id"]))
        connection.commit()

    # Read here: the SQLite connection belongs to this thread, not the workers.
    content = {row["id"]: content_of(connection, row["id"]) for row in rows}
    parallel(
        rows,
        lambda row: writer.summarize(settings.summary_model, row, content[row["id"]]),
        store,
        "Summaries (Claude)",
    )


def classify_pending(connection: sqlite3.Connection, settings: Settings, jev: Jev) -> None:
    """Oldest first, so the earliest item of an idea becomes the canonical one."""
    rows = connection.execute(
        "SELECT * FROM items WHERE summary IS NOT NULL AND classified_at IS NULL ORDER BY created_at, id"
    ).fetchall()
    earlier = [
        (row["id"], f"{row['title']}: {row['summary']}")
        for row in connection.execute(
            "SELECT id, title, summary FROM items WHERE classified_at IS NOT NULL AND duplicate_of IS NULL"
        )
    ]
    duplicates = 0
    for row in rows:
        description = f"{row['title']}: {row['summary']}"
        result = jev.classify(
            jev_state(row), settings.categories, shortlist(description, earlier), settings.duplicate_threshold
        )
        connection.execute(
            "UPDATE items SET category = ?, category_confidence = ?, duplicate_of = ?, "
            "duplicate_probability = ?, classified_at = ? WHERE id = ?",
            (result.category, result.category_confidence, result.duplicate_of, result.duplicate_probability, now(),
             row["id"]),
        )
        connection.commit()
        if result.duplicate_of:
            duplicates += 1
        else:
            earlier.append((row["id"], description))
    print(f"Categories and duplicates (Jev): {len(rows)} classified, {duplicates} duplicates of earlier ideas")


def score_pending(connection: sqlite3.Connection, settings: Settings, jev: Jev) -> None:
    """Every kind is matched to projects (a dataset, model, or Space becomes a project resource)."""
    specs = {row["name"]: row["spec"] for row in connection.execute("SELECT name, spec FROM projects")}
    missing: dict[str, list[str]] = defaultdict(list)
    for row in connection.execute(
        "SELECT i.id, p.name FROM items i CROSS JOIN projects p "
        "WHERE i.classified_at IS NOT NULL AND i.duplicate_of IS NULL AND NOT EXISTS "
        "(SELECT 1 FROM relevance r WHERE r.item_id = i.id AND r.project = p.name)"
    ):
        missing[row["id"]].append(row["name"])
    pairs = 0
    for item_id, names in missing.items():
        item = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        scores = jev.relevance(jev_state(item), [(name, specs[name]) for name in names], settings.relevance_guidance)
        connection.executemany(
            "INSERT INTO relevance (item_id, project, score, confidence, scored_at) VALUES (?, ?, ?, ?, ?)",
            [(item_id, name, score, confidence, now()) for name, (score, confidence) in scores.items()],
        )
        connection.commit()
        pairs += len(scores)
    print(f"Project relevance (Jev): {pairs} item-project pairs scored")


def explain_pending(connection: sqlite3.Connection, settings: Settings) -> None:
    rows = connection.execute(
        "SELECT i.*, r.project, p.spec FROM relevance r "
        "JOIN projects p ON p.name = r.project JOIN items i ON i.id = r.item_id "
        "WHERE r.score >= ? AND r.reason IS NULL",
        (settings.relevance_threshold,),
    ).fetchall()

    def task(row: sqlite3.Row) -> str:
        return writer.explain(settings.claude_model, row, row["project"], row["spec"])

    def store(row: sqlite3.Row, reason: str) -> None:
        connection.execute(
            "UPDATE relevance SET reason = ? WHERE item_id = ? AND project = ?", (reason, row["id"], row["project"])
        )
        connection.commit()

    parallel(rows, task, store, "Match reasons (Claude)")


def project_texts(connection: sqlite3.Connection) -> dict[str, str]:
    """What each project is, for writing a suggestion: its latest focus brief, else its spec."""
    texts = {row["name"]: row["spec"] for row in connection.execute("SELECT name, spec FROM projects")}
    texts |= {row["project"]: row["brief"] for row in connection.execute(
        "SELECT project, brief FROM focus_briefs WHERE id IN (SELECT MAX(id) FROM focus_briefs GROUP BY project)")}
    return texts


LOOP_PER_PROJECT = 8


def project_loop(connection: sqlite3.Connection) -> dict[str, str]:
    """What the user already asked for and built in each project, for writing a suggestion that adds to it rather
    than repeating it: their own ideas and notes, and the suggestions implemented, handed off, or done (headline and
    the first line of the agent's report), newest first, LOOP_PER_PROJECT each."""
    rows = connection.execute(
        "SELECT project, headline, action, basis, basis_kind, status, implement_status, "
        "substr(implement_report, 1, 300) AS report FROM suggestions WHERE basis_kind IN ('user', 'note') "
        "OR implement_status IN ('implemented', 'handoff') OR status = 'done' "
        "ORDER BY COALESCE(implement_finished_at, rated_at, created_at) DESC"
    ).fetchall()
    lines: dict[str, list[str]] = {}
    for row in rows:
        if len(lines.setdefault(row["project"], [])) >= LOOP_PER_PROJECT:
            continue
        what = row["headline"] or row["action"]
        if row["basis_kind"] in ("user", "note"):
            line = f"- The user asked for: {what} (their words: {' '.join(row['basis'].split())[:200]})"
        else:
            line = f"- {what}"
        if row["implement_status"] in ("implemented", "handoff") or row["status"] == "done":
            state = "done" if row["status"] == "done" else "built, awaiting review" if row["implement_status"] == "implemented" else "handed off as a task"
            line += f" [{state}]" + (f": {' '.join(row['report'].split())[:200]}" if row["report"] else "")
        lines[row["project"]].append(line)
    return {project: "\n\nAlready asked for or built in this project (add to it, do not repeat it):\n" + "\n".join(entries)
            for project, entries in lines.items()}


def suggest_pending(connection: sqlite3.Connection, settings: Settings) -> None:
    """One suggestion (a concrete action, writer.suggest) per item and project without one: every match above the
    threshold that has its reason, and each find or idea for the project it was found for (from the judge's why).
    Items the user rated α first, then finds and ideas, then the strongest matches; items they rated β are skipped."""
    limit = SUGGESTIONS_PER_RUN if connection.execute("SELECT 1 FROM suggestions LIMIT 1").fetchone() else FIRST_SUGGESTIONS
    pairs = connection.execute(
        "WITH pairs AS ("
        " SELECT r.item_id, r.project, r.reason, NULL AS why, r.score FROM relevance r"
        " WHERE r.score >= ? AND r.reason IS NOT NULL"
        " UNION ALL"
        " SELECT c.id, COALESCE(c.project, (SELECT q.project FROM requests q WHERE q.id = c.request_id),"
        "  CASE WHEN c.fit_project != 'taste' AND c.fit_project NOT LIKE 'request:%' THEN c.fit_project END),"
        "  NULL, c.why, NULL FROM candidates c WHERE c.status IN ('find', 'idea') AND c.why IS NOT NULL)"
        "SELECT p.item_id, p.project, MAX(p.reason) AS reason, MAX(p.why) AS why FROM pairs p "
        "JOIN items i ON i.id = p.item_id WHERE p.project IS NOT NULL AND i.summary IS NOT NULL "
        "AND i.duplicate_of IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM suggestions s WHERE s.item_id = p.item_id AND s.project = p.project) "
        "AND NOT EXISTS (SELECT 1 FROM ratings ra WHERE ra.item_id = p.item_id AND ra.rating = 'beta') "
        "GROUP BY p.item_id, p.project "
        "ORDER BY EXISTS (SELECT 1 FROM ratings ra WHERE ra.item_id = p.item_id AND ra.rating = 'alpha') DESC, "
        "MAX(p.why) IS NOT NULL DESC, COALESCE(MAX(p.score), 0) DESC LIMIT ?",
        (settings.relevance_threshold, limit),
    ).fetchall()
    if not pairs:
        return
    texts = project_texts(connection)
    loop = project_loop(connection)
    work = []
    for pair in pairs:
        basis = "\n".join(filter(None, (f"The match reason: {pair['reason']}" if pair["reason"] else "",
                                        f"The judge's why (it was found for this project): {pair['why']}"
                                        if pair["why"] else "")))
        kind = "both" if pair["reason"] and pair["why"] else "match" if pair["reason"] else "judge"
        item = connection.execute("SELECT * FROM items WHERE id = ?", (pair["item_id"],)).fetchone()
        text = texts.get(pair["project"], "(no description)")[:4000] + loop.get(pair["project"], "")
        work.append((item, pair["project"], text, basis, kind))

    def task(entry: tuple) -> writer.Suggestion:
        item, project, text, basis, _ = entry
        return writer.suggest(settings.skill_model, item, project, text, basis)

    def store(entry: tuple, suggestion: writer.Suggestion) -> None:
        item, project, _, basis, kind = entry
        # A skipped one is kept as dismissed, so the same pair is not asked again every run.
        action = f"Skipped by Alpha: {suggestion.skip_reason}" if suggestion.skip else suggestion.action
        connection.execute(
            "INSERT OR IGNORE INTO suggestions (item_id, project, action, headline, outcome, effort, basis, basis_kind, "
            "model, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["id"], project, action, suggestion.headline, suggestion.outcome, suggestion.effort, basis, kind,
             settings.skill_model, "dismissed" if suggestion.skip else "open", now()),
        )
        connection.commit()

    parallel(work, task, store, f"Suggestions ({settings.skill_model})")


def frame_pending(connection: sqlite3.Connection, settings: Settings) -> None:
    """A headline and an outcome (the benefit, plainly) for every suggestion written before suggestions had them,
    rewritten from its own how: many per call (`claude_model`), no item read again."""
    rows = connection.execute("SELECT item_id, project, action FROM suggestions "
                              "WHERE headline IS NULL OR outcome IS NULL ORDER BY created_at").fetchall()
    if not rows:
        return
    batches = [rows[start:start + FRAMES_PER_CALL] for start in range(0, len(rows), FRAMES_PER_CALL)]

    def task(batch: list[sqlite3.Row]) -> list[writer.Framing | None]:
        return writer.frame(settings.claude_model, [(row["project"], row["action"]) for row in batch])

    def store(batch: list[sqlite3.Row], written: list[writer.Framing | None]) -> None:
        connection.executemany(
            "UPDATE suggestions SET headline = ?, outcome = ? WHERE item_id = ? AND project = ?",
            [(framing.headline, framing.outcome, row["item_id"], row["project"])
             for row, framing in zip(batch, written) if framing],
        )
        connection.commit()

    parallel(batches, task, store, f"Suggestion headlines ({settings.claude_model}, {len(rows)} in "
                                   f"{len(batches)} call{'s' if len(batches) != 1 else ''})")


def index_pending(connection: sqlite3.Connection) -> None:
    """Keep the full-text index (items_fts) in step with each item's title, summary, text, and content.
    Triggers mark an item when any of those change; this indexes every marked item again."""
    rows = connection.execute("SELECT rowid, id, title, summary, text FROM items WHERE indexed_at IS NULL").fetchall()
    for row in rows:
        content = "\n\n".join(item["text"] for item in content_of(connection, row["id"]))
        connection.execute("DELETE FROM items_fts WHERE rowid = ?", (row["rowid"],))
        connection.execute(
            "INSERT INTO items_fts (rowid, item_id, title, summary, text, content) VALUES (?, ?, ?, ?, ?, ?)",
            (row["rowid"], row["id"], row["title"] or "", row["summary"] or "", row["text"], content),
        )
        connection.execute("UPDATE items SET indexed_at = ? WHERE rowid = ?", (now(), row["rowid"]))
    connection.commit()
    print(f"Search index: {len(rows)} items indexed")
