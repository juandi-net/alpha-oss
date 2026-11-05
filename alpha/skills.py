"""Turn groups of items into agent skills, built and tested before anyone uses them. Only items of a skill
kind (storage.SKILL_KINDS: post, article, paper, repo) feed skills; resources (datasets, models, Spaces) never do,
and neither does an idea (a discovered item only one judge passed) until the user rates it α.

1. `propose`: Claude reads every distinct item and proposes topics, each a repeatable process
   backed by at least three items, skipping what an existing skill already covers. A topic can also
   come from the skill chat on an item's page (skill_chat.py), with the user's brief.
2. `approve` (the dashboard's Build button): the topic becomes active with a test target, Jev
   adds every other item that fits it, and version 1 is built. It goes live if it passes.
3. A build: Claude (`skill_model`) writes a typed spec, told which library skills overlap this one
   so its description is scoped against them; code repairs what needs no judgment (an approval flag the
   rules require, a description over the limit, shortened by Claude or trimmed at a sentence), validates
   it (skill_spec.py), drops the posts no step cites, and renders SKILL.md and scripts/; each script runs
   on the test target; a headless agent follows the skill in report-only mode and Claude grades its
   report. When a version is live, the live version is tested on the same target in the same build and
   both reports are graded in one blinded call; the new one must also match the live one
   (writer.regressions). A failed check sends the problems back to Claude for a revision, with every
   problem seen so far in the build and which are fixed, so a revision cannot bring one back; a build
   after a failed version starts from that version's problems. The version is a draft only if
   everything passed. A build that failed only on the rules or crashed is built once more by the next
   run (`retry_failed`); one that failed its test waits for the user.
4. Every run, Jev checks new items against active topics. When a live skill's items change,
   a new version is built as a draft; it replaces the live skill only when the user publishes it.
   An item the user removes from a skill (`remove`) stays out and is never checked again.
5. `overlap`: Claude lists groups of library skills whose triggers compete, with a scope for each.
   It only reports; tool-written skills are scoped by their next build.

Published skills go to <skills_directory>/<name>/ and are linked into the active skills folder.
A SKILL.md without the Alpha marker (one you wrote) is never overwritten.
"""

import difflib
import json
import re
import sqlite3
import tempfile
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

from . import skill_spec, skill_test, writer
from .config import Settings
from .jev import Jev
from .pipeline import jev_state
from .storage import SKILL_KINDS, content_of, get_metadata, now, set_metadata

# SQL condition: the item (alias i) may feed a skill: a skill kind, and not an idea the user has not rated α.
SKILL_KIND = (f"i.kind IN ({', '.join(repr(kind) for kind in SKILL_KINDS)}) "
              "AND (i.tier IS NULL OR i.tier != 'idea')")

# Per item and in total, so a few long articles cannot push a skill prompt past the model's context.
SOURCE_ITEM_CHARACTERS = 20_000
SOURCE_TOTAL_CHARACTERS = 400_000
# A rule violation is cheap to fix (one Opus call, no test run), a failing script or a test run is not:
# up to five spec writes, of which at most three pass the rules and at most two reach the test run.
MAX_SPEC_WRITES = 5
MAX_CHECKED_WRITES = 3
MAX_TEST_RUNS = 2
# Failures the next run retries on its own, once per failed version (see failure and retry_failed).
RETRY_FAILURES = ("rules", "crash")
# A build still marked "building" after this long crashed; another build may start. An update runs
# up to three test runs of 30 minutes (the live version's and two of its own).
STALE_BUILD = timedelta(hours=3)


def slug(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")[:64]


def canonical(connection: sqlite3.Connection, item_id: str) -> str | None:
    row = connection.execute("SELECT id, duplicate_of FROM items WHERE id = ?", (item_id,)).fetchone()
    return row and (row["duplicate_of"] or row["id"])


def members(connection: sqlite3.Connection, settings: Settings, topic: str) -> list[str]:
    return [
        row["item_id"]
        for row in connection.execute(
            "SELECT item_id FROM topic_scores WHERE topic = ? AND probability >= ? AND excluded = 0 "
            "AND item_id NOT IN (SELECT id FROM items WHERE tier = 'idea') ORDER BY item_id",
            (topic, settings.topic_threshold),
        )
    ]


def remove(connection: sqlite3.Connection, topic: str, item_id: str) -> None:
    """Take an item out of a skill for good: its row stays with excluded = 1, so members() skips it and
    Jev never checks it against the topic again. Starts no build; the next build leaves it out."""
    connection.execute(
        "INSERT INTO topic_scores (topic, item_id, probability, excluded) VALUES (?, ?, 0, 1) "
        "ON CONFLICT (topic, item_id) DO UPDATE SET excluded = 1",
        (topic, item_id),
    )
    connection.commit()


def versions(connection: sqlite3.Connection, topic: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM skill_versions WHERE topic = ? ORDER BY version DESC", (topic,)
    ).fetchall()


def live_version(connection: sqlite3.Connection, topic: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM skill_versions WHERE topic = ? AND status = 'live'", (topic,)
    ).fetchone()


def library_files(settings: Settings) -> dict[str, Path]:
    """SKILL.md of every skill an agent could load, by name: the library, plus skills that sit only in
    the active folder (installed there directly)."""
    folders = [settings.skills_directory, *([settings.active_skills_directory] if settings.active_skills_directory else [])]
    files: dict[str, Path] = {}
    for folder in folders:
        for path in sorted(folder.glob("*/SKILL.md")):
            files.setdefault(path.parent.name, path)
    return dict(sorted(files.items()))


def written_by_tool(path: Path) -> bool:
    return skill_spec.MARKER in path.read_text(errors="replace")[:1000]


def library(settings: Settings, exclude: str | None = None) -> dict[str, str]:
    """Name and description of every skill an agent could load (see library_files)."""
    return {
        name: skill_spec.description_of(path.read_text(errors="replace"))
        for name, path in library_files(settings).items() if name != exclude
    }


def library_skills(settings: Settings) -> str:
    """Names and descriptions of skills already in the library, so proposals do not duplicate them.
    Skills this tool wrote are listed by propose as active topics instead."""
    files = library_files(settings)
    return "\n".join(
        f"- {name}: {description[:200]}"
        for name, description in library(settings).items() if not written_by_tool(files[name])
    )


def propose(connection: sqlite3.Connection, settings: Settings) -> None:
    """Replace pending proposals with new ones; active topics and library skills are shown to Claude."""
    items = "\n".join(
        f"{row['id']} | {row['source']} | {row['kind']} | {row['category']} | {row['title']} | {row['summary']}"
        for row in connection.execute(
            f"SELECT * FROM items i WHERE classified_at IS NOT NULL AND duplicate_of IS NULL AND {SKILL_KIND} "
            "ORDER BY created_at"
        )
    )
    active = connection.execute("SELECT name, title, description FROM topics WHERE status = 'active'").fetchall()
    existing = "\n".join(f"- {row['name']}: {row['description']}" for row in active)
    existing = "\n".join(part for part in (existing, library_skills(settings)) if part)
    projects = "\n".join(
        f"- {row['name']}: {' '.join(row['spec'].split())[:300]}" for row in connection.execute("SELECT * FROM projects")
    )
    proposals = writer.propose_topics(settings.skill_model, items, existing, projects)
    connection.execute("DELETE FROM topics WHERE status = 'proposed'")
    taken = {row["name"] for row in active}
    if settings.skills_directory.is_dir():
        taken |= {path.name for path in settings.skills_directory.iterdir()}
    for proposal in proposals:
        name = slug(proposal.name)
        item_ids = {found for p in proposal.post_ids if (found := canonical(connection, p.strip()))}
        if not name or name in taken or len(item_ids) < 3:
            continue
        taken.add(name)
        connection.execute(
            "INSERT INTO topics (name, title, description, status, test_target, created_at) "
            "VALUES (?, ?, ?, 'proposed', ?, ?)",
            (name, proposal.title, proposal.description, proposal.test_task.strip() or None, now()),
        )
        connection.executemany(
            "INSERT INTO topic_scores (topic, item_id, probability) VALUES (?, ?, 1.0)",
            [(name, item_id) for item_id in sorted(item_ids)],
        )
    connection.commit()
    set_metadata(connection, "proposed_at", now())
    show(connection, settings)


# Proposals are redone weekly, so items from every source (and new ones) can form topics.
PROPOSE_EVERY = timedelta(days=7)


def propose_if_due(connection: sqlite3.Connection, settings: Settings) -> None:
    last = get_metadata(connection, "proposed_at")
    if last and datetime.now(UTC) - datetime.fromisoformat(last) < PROPOSE_EVERY:
        return
    propose(connection, settings)


def show(connection: sqlite3.Connection, settings: Settings) -> None:
    for row in connection.execute("SELECT * FROM topics ORDER BY status, name"):
        latest = versions(connection, row["name"])
        live = live_version(connection, row["name"])
        state = row["status"]
        if latest:
            state = f"v{latest[0]['version']} {latest[0]['status']}"
            if live and live["version"] != latest[0]["version"]:
                state += f", live v{live['version']}"
        print(f"{row['name']} ({len(members(connection, settings, row['name']))} items, {state}): {row['description']}")


def approve(connection: sqlite3.Connection, settings: Settings, name: str, target: str | None) -> None:
    """Activate a topic, let Jev add every other item that fits it, then build and test version 1."""
    topic = connection.execute("SELECT * FROM topics WHERE name = ?", (name,)).fetchone()
    if not topic:
        raise SystemExit(f"Skills: no topic named {name}; run `alpha skills` to list them")
    target = target or topic["test_target"]
    if not target:
        raise SystemExit(f"Skills: {name} needs a test task (--target): a URL, a repository, or a one-line scenario")
    connection.execute("UPDATE topics SET status = 'active', test_target = ? WHERE name = ?", (target, name))
    connection.commit()
    assign_pending(connection, settings, Jev(settings.jev_model))
    build(connection, settings, name)


def assign_pending(connection: sqlite3.Connection, settings: Settings, jev: Jev) -> None:
    """Check every distinct item of a skill kind not yet scored against each active topic."""
    topics = {
        row["name"]: f"{row['title']}: {row['description']}"
        for row in connection.execute("SELECT * FROM topics WHERE status = 'active'")
    }
    if not topics:
        return
    pending: dict[str, list[str]] = {}
    for row in connection.execute(
        "SELECT i.id, t.name FROM items i CROSS JOIN topics t WHERE t.status = 'active' "
        f"AND i.classified_at IS NOT NULL AND i.duplicate_of IS NULL AND {SKILL_KIND} AND NOT EXISTS "
        "(SELECT 1 FROM topic_scores s WHERE s.topic = t.name AND s.item_id = i.id)"
    ):
        pending.setdefault(row["id"], []).append(row["name"])
    for item_id, names in pending.items():
        item = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        result = jev.topic_membership(jev_state(item), [(name, topics[name]) for name in names])
        connection.executemany(
            "INSERT INTO topic_scores (topic, item_id, probability) VALUES (?, ?, ?)",
            [(name, item_id, probability) for name, probability in result.items()],
        )
        connection.commit()
    if pending:
        print(f"Skills (Jev): {len(pending)} items checked against {len(topics)} active topics")


def _sources(connection: sqlite3.Connection, item_ids: list[str]) -> tuple[str, list[tuple[str, str]]]:
    """The numbered source text for Claude, and (title, URL) per number for the Sources list."""
    parts, listing, total = [], [], 0
    for number, item_id in enumerate(item_ids, 1):
        row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        text = writer.item_text(row, content_of(connection, item_id), SOURCE_ITEM_CHARACTERS)
        part = f"[{number}] {row['title']}\nURL: {row['url']}\nSummary: {row['summary']}\n\n{text}"
        if total + len(part) > SOURCE_TOTAL_CHARACTERS:
            part = part[: max(0, SOURCE_TOTAL_CHARACTERS - total)]
        parts.append(part)
        listing.append((row["title"] or "Untitled", row["url"]))
        total += len(part)
    return "\n\n---\n\n".join(parts), listing


def draft_directory(settings: Settings, topic: str, version: int) -> Path:
    return settings.database_path.parent / "skills" / topic / f"v{version}"


def _neighbors(settings: Settings, topic: sqlite3.Row, live: sqlite3.Row | None) -> dict[str, str]:
    """Library skills whose scope overlaps this one (picked by Claude), with their descriptions, so the
    spec's description can send their requests to them. The live description is what agents match
    against today, so it is what is compared."""
    others = library(settings, exclude=topic["name"])
    description = f"{topic['title']}: {topic['description']}"
    if live and live["spec"]:
        description = skill_spec.SkillSpec.model_validate_json(live["spec"]).description
    return {name: others[name] for name in writer.pick_neighbors(settings.skill_model, topic["name"], description, others)}


def _live_test(settings: Settings, topic: sqlite3.Row, live: sqlite3.Row) -> str:
    """The live version's report-only test on this build's target, run from its stored spec in a
    temporary folder, so the new version is judged against it under the same conditions."""
    with tempfile.TemporaryDirectory(prefix="alpha-live-") as temporary:
        directory = Path(temporary) / topic["name"]
        skill_spec.write_files(directory, live["skill_md"], skill_spec.SkillSpec.model_validate_json(live["spec"]))
        return skill_test.run_agent(directory, topic["test_target"], writer.resolve_model(settings.claude_model))


def _repair(settings: Settings, spec: skill_spec.SkillSpec) -> list[str]:
    """Fix what code can before validation counts it: the approval flag, and a description over the limit
    (shortened by Claude in one small call, else trimmed at sentence boundaries). One sentence per repair."""
    repairs = skill_spec.repair(spec)
    if len(spec.description) > skill_spec.DESCRIPTION_LIMIT:
        before = len(spec.description)
        shorter = writer.shorten_description(settings.skill_model, spec.description, skill_spec.DESCRIPTION_LIMIT)
        how = "Claude shortened it"
        if not skill_spec.fits(shorter):
            shorter, how = skill_spec.trim_description(spec.description), "trimmed at a sentence boundary"
        if skill_spec.fits(shorter):
            spec.description = shorter
            repairs.append(f"description shortened from {before} to {len(shorter)} characters ({how})")
    return repairs


def _attempt(settings: Settings, topic: sqlite3.Row, version: int, sources: str, listing: list[tuple[str, str]],
             post_ids: list[str], previous: str | None, ledger: dict[str, list[str]], neighbors: dict[str, str],
             live: sqlite3.Row | None, live_report: Callable[[], str] | None) -> dict:
    """One attempt: spec, repairs, validation, post review, scripts, test run, grade (beside the live
    version's when there is one). `ledger` is what the writer is told (see _ledger). Returns everything
    it produced; `members` are the posts the spec cites."""
    name, target = topic["name"], topic["test_target"]
    spec = writer.write_skill_spec(
        settings.skill_model, name, topic["title"], topic["description"], sources, previous,
        ledger["problems"] or None, topic["brief"], neighbors, target, ledger["fixed"] or None,
        ledger["repaired"] or None,
    )
    repairs = _repair(settings, spec)
    validation = skill_spec.validate(spec, name, len(listing))
    result = {"checks": {"repairs": repairs, "validation": validation}, "members": post_ids}
    if not validation:
        # A post no step cites taught the skill nothing (often off-topic): it leaves the sources.
        numbers = skill_spec.cited(spec)
        used = [post_id for number, post_id in enumerate(post_ids, 1) if number in numbers]
        spec = skill_spec.renumber(spec, post_ids, used)
        listing = [entry for number, entry in enumerate(listing, 1) if number in numbers]
        result["members"] = used
        result["checks"]["unused"] = [post_id for post_id in post_ids if post_id not in used]
    skill_md = skill_spec.render(spec, name, listing, version, now()[:10])
    directory = draft_directory(settings, name, version)
    skill_spec.write_files(directory, skill_md, spec)
    result |= {"spec": spec, "skill_md": skill_md}
    if validation:
        result["problems"] = validation
        return result
    scripts = skill_test.run_scripts(directory, spec, target)
    result["checks"]["scripts"] = scripts
    if failed := [s for s in scripts if not s["ok"]]:
        result["problems"] = [
            f"script {s['script']} failed its test `{s['command']}` (exit {s['exit_code']}): {s['stderr'][-800:]}"
            for s in failed
        ]
        return result
    report = skill_test.run_agent(directory, target, writer.resolve_model(settings.claude_model))
    result["report"] = report
    regressed: list[str] = []
    if live is None:
        grade = writer.grade_run(settings.skill_model, skill_md, target, report)
    else:
        pair = writer.grade_pair(settings.skill_model, target, (skill_md, report), (live["skill_md"], live_report()))
        grade = pair.new
        regressed = writer.regressions(pair.new, pair.live, live["version"])
        result["checks"]["live_grade"] = pair.live.model_dump()
        result["checks"]["comparison"] = {
            "live_version": live["version"], "new_mean": round(writer.mean(pair.new), 3),
            "live_mean": round(writer.mean(pair.live), 3), "regressions": regressed,
            "verdict": pair.comparison, "new_label": pair.new_label,
        }
    result["checks"]["grade"] = grade.model_dump()
    weak = [f"{c.name} scored {c.score:.2f}: {c.reason}" for c in grade.criteria if c.score < 0.75]
    result["problems"] = [] if writer.passed(grade) else weak + grade.problems
    if regressed:
        verdict = f"The grader compared both runs (this version was {pair.new_label}): {pair.comparison}"
        result["problems"] = regressed + [verdict] + (result["problems"] or grade.problems)
    return result


def _post_review(candidates: list[str], used: list[str], live: sqlite3.Row | None) -> dict:
    """Which posts the version was offered, which it left uncited, and (for an update) which posts it
    adds and drops against the live version."""
    review = {"candidates": candidates, "unused": [p for p in candidates if p not in used]}
    if live:
        before = json.loads(live["members"])
        review |= {
            "live_version": live["version"],
            "added": [p for p in candidates if p not in before],
            "dropped": [p for p in before if p not in used],
        }
    return review


def attempts_of(version: sqlite3.Row) -> list[dict]:
    return json.loads(version["checks"] or "{}").get("attempts") or []


def failure(version: sqlite3.Row) -> str:
    """Why a failed version failed: 'test' (its graded test run or the head-to-head), 'crash', 'scripts'
    (a script failed on the target), or 'rules' (it never passed the fixed rules)."""
    attempts = attempts_of(version)
    if any("grade" in attempt for attempt in attempts):
        return "test"
    if not attempts or attempts[-1].get("error"):
        return "crash"
    return "rules" if attempts[-1].get("validation") else "scripts"


def last_failed(connection: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """The newest version when it failed (nothing was built or went live after it): its problems feed the
    next build, and the next run may retry it."""
    row = connection.execute(
        "SELECT * FROM skill_versions WHERE topic = ? ORDER BY version DESC LIMIT 1", (name,)
    ).fetchone()
    return row if row and row["status"] == "failed" else None


def retry_due(connection: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """The failed version the next run builds again on its own: the skill is active with a test task, and
    the version failed only on the rules or crashed and was not itself an automatic retry, so each failure
    is retried once and a retry never loops."""
    topic = connection.execute("SELECT test_target FROM topics WHERE name = ? AND status = 'active'", (name,)).fetchone()
    row = last_failed(connection, name) if topic and topic["test_target"] else None
    if not row or failure(row) not in RETRY_FAILURES or json.loads(row["checks"] or "{}").get("retry_of"):
        return None
    return row


def retry_failed(connection: sqlite3.Connection, settings: Settings) -> None:
    """Build once more every active skill whose newest version failed only on the rules or crashed."""
    for topic in connection.execute("SELECT name FROM topics WHERE status = 'active'").fetchall():
        if failed := retry_due(connection, topic["name"]):
            print(f"Skills: {topic['name']} v{failed['version']} failed ({failure(failed)}); trying again once", flush=True)
            build(connection, settings, topic["name"], retry_of=failed["version"])


def _ledger(history: list[dict]) -> dict[str, list[str]]:
    """What the writer is told before the next attempt: the problems of the last attempt that found any,
    those an earlier attempt had and a later one fixed, and every repair code made, so a revision keeps
    earlier fixes."""
    found = [attempt for attempt in history if attempt.get("problems")]
    problems = found[-1]["problems"] if found else []
    fixed = [p for attempt in found[:-1] for p in attempt["problems"] if p not in problems]
    repaired = [r for attempt in history for r in attempt.get("repairs") or []]
    return {"problems": problems, "fixed": list(dict.fromkeys(fixed)), "repaired": list(dict.fromkeys(repaired))}


def last_problems(version: sqlite3.Row) -> list[str]:
    """The problems of a version's last attempt that found any: what its next build starts from."""
    return _ledger(attempts_of(version))["problems"]


def build(connection: sqlite3.Connection, settings: Settings, name: str, retry_of: int | None = None) -> sqlite3.Row | None:
    """Build and test a new version. With no live version it goes live when it passes; otherwise it
    must also match the live version, tested beside it, and waits as a draft for `publish`. After a
    failed version, its attempts feed the first spec. retry_of: the failed version this build retries on
    its own (recorded, so the retry is not retried)."""
    topic = connection.execute("SELECT * FROM topics WHERE name = ? AND status = 'active'", (name,)).fetchone()
    if not topic or not topic["test_target"]:
        raise SystemExit(f"Skills: {name} is not an active topic with a test task")
    running = connection.execute(
        "SELECT created_at FROM skill_versions WHERE topic = ? AND status = 'building'", (name,)
    ).fetchone()
    if running and datetime.now(UTC) - datetime.fromisoformat(running["created_at"]) < STALE_BUILD:
        print(f"Skills: {name} is already being built")
        return None
    connection.execute("UPDATE skill_versions SET status = 'failed' WHERE topic = ? AND status = 'building'", (name,))
    post_ids = members(connection, settings, name)
    version = (connection.execute(
        "SELECT MAX(version) FROM skill_versions WHERE topic = ?", (name,)
    ).fetchone()[0] or 0) + 1
    live = live_version(connection, name)
    failed = last_failed(connection, name)
    history = attempts_of(failed) if failed else []
    checks = {"retry_of": retry_of} if retry_of else {}
    if ledger := last_problems(failed) if failed else []:
        checks["fed_in"] = {"version": failed["version"], "problems": ledger}
    connection.execute(
        "INSERT INTO skill_versions (topic, version, status, members, model, checks, created_at) "
        "VALUES (?, ?, 'building', ?, ?, ?, ?)",
        (name, version, json.dumps(post_ids), writer.resolve_model(settings.skill_model), json.dumps(checks), now()),
    )
    connection.commit()
    against = f", against live v{live['version']}" if live else ""
    fed = f", starting from v{failed['version']}'s {len(ledger)} problems" if ledger else ""
    print(f"Skills: building {name} v{version} from {len(post_ids)} posts, testing on {topic['test_target']}{against}{fed}",
          flush=True)
    attempts, result, test_runs = [], None, 0
    # The live version's test runs once per build, when the first attempt reaches its own test run.
    live_report = cache(lambda: _live_test(settings, topic, live)) if live else None
    try:
        sources, listing = _sources(connection, post_ids)
        # The live spec cites posts by their place in its own source list; renumber it to this one.
        previous = skill_spec.renumber(
            skill_spec.SkillSpec.model_validate_json(live["spec"]), json.loads(live["members"]), post_ids
        ).model_dump_json() if live else None
        neighbors = _neighbors(settings, topic, live)
        checks["neighbors"] = list(neighbors)
        while True:
            result = _attempt(settings, topic, version, sources, listing, post_ids, previous,
                              _ledger(history + attempts), neighbors, live, live_report)
            attempts.append(result["checks"] | {"problems": result["problems"]})
            test_runs += "grade" in result["checks"]
            checked = sum(not attempt["validation"] for attempt in attempts)
            if (not result["problems"] or test_runs >= MAX_TEST_RUNS or checked >= MAX_CHECKED_WRITES
                    or len(attempts) >= MAX_SPEC_WRITES):
                break
            print(f"Skills: {name} v{version} attempt {len(attempts)} found {len(result['problems'])} problems", flush=True)
        status = "draft" if not result["problems"] else "failed"
        checks |= {"attempts": attempts, "posts": _post_review(post_ids, result["members"], live)}
        if live_report and live_report.cache_info().currsize:
            checks["live_test"] = {"version": live["version"], "report": live_report()}
        connection.execute(
            "UPDATE skill_versions SET status = ?, members = ?, spec = ?, skill_md = ?, checks = ?, report = ? "
            "WHERE topic = ? AND version = ?",
            (status, json.dumps(result["members"]), result["spec"].model_dump_json(), result["skill_md"],
             json.dumps(checks), result.get("report"), name, version),
        )
    except Exception as error:  # a crashed build must not stay "building" or stop the scheduled run
        attempts.append({"error": f"{type(error).__name__}: {error}", "trace": traceback.format_exc()[-3000:]})
        connection.execute(
            "UPDATE skill_versions SET status = 'failed', checks = ? WHERE topic = ? AND version = ?",
            (json.dumps(checks | {"attempts": attempts}), name, version),
        )
        status = "failed"
    connection.commit()
    outcome = "passed" if status == "draft" else "failed"
    if comparison := (attempts[-1].get("comparison") if attempts else None):
        outcome += f" (mean {comparison['new_mean']:.2f} against live v{comparison['live_version']} {comparison['live_mean']:.2f})"
    print(f"Skills: {name} v{version} {outcome}", flush=True)
    if status == "draft" and not live:
        publish(connection, settings, name, version)
    return connection.execute("SELECT * FROM skill_versions WHERE topic = ? AND version = ?", (name, version)).fetchone()


def _activate(settings: Settings, name: str) -> None:
    """Link the skill into the active set every agent reads (Claude Code, Codex, omp, ~/.agents)."""
    if settings.active_skills_directory is None:
        return
    link, target = settings.active_skills_directory / name, settings.skills_directory / name
    if link.is_symlink() and link.resolve() == target.resolve():
        return
    if link.exists() or link.is_symlink():
        print(f"Skills: {link} already exists and is not this skill; not linked")
        return
    link.symlink_to(target)


def publish(connection: sqlite3.Connection, settings: Settings, name: str, version: int | None = None) -> None:
    """Make a draft the live skill: write it to the library from its stored spec and link it."""
    query = "SELECT * FROM skill_versions WHERE topic = ? AND status = 'draft'"
    query += " AND version = ?" if version else " ORDER BY version DESC LIMIT 1"
    row = connection.execute(query, (name, version) if version else (name,)).fetchone()
    if not row:
        raise SystemExit(f"Skills: {name} has no draft{f' v{version}' if version else ''} that passed its checks")
    directory = settings.skills_directory / name
    existing = directory / "SKILL.md"
    if existing.exists() and skill_spec.MARKER not in existing.read_text(errors="replace")[:1000]:
        raise SystemExit(f"Skills: {existing} was not written by this tool; not replaced")
    skill_spec.write_files(directory, row["skill_md"], skill_spec.SkillSpec.model_validate_json(row["spec"]))
    connection.execute("UPDATE skill_versions SET status = 'superseded' WHERE topic = ? AND status = 'live'", (name,))
    connection.execute(
        "UPDATE skill_versions SET status = 'live', published_at = ? WHERE topic = ? AND version = ?",
        (now(), name, row["version"]),
    )
    connection.commit()
    _activate(settings, name)
    print(f"Skills: {name} v{row['version']} is live at {directory}")


def reject(connection: sqlite3.Connection, name: str, version: int | None = None) -> None:
    query = "UPDATE skill_versions SET status = 'rejected' WHERE topic = ? AND status IN ('draft', 'failed')"
    params: tuple = (name,)
    if version:
        query, params = query + " AND version = ?", (name, version)
    connection.execute(query, params)
    connection.commit()


def _covers(version: sqlite3.Row, current: set[str]) -> bool:
    """The version was built from these posts, or from more posts of which it cited only ones still
    here (removing a post a version did not use is no reason to build again)."""
    used = set(json.loads(version["members"]))
    offered = json.loads(version["checks"] or "{}").get("posts", {}).get("candidates")
    return used <= current <= (set(offered) if offered is not None else used)


def update_drafts(connection: sqlite3.Connection, settings: Settings) -> None:
    """For each live skill whose posts changed, build one new version as a draft. A post set that
    was already built (passed, failed, or rejected) is not built again."""
    for topic in connection.execute(
        "SELECT t.name FROM topics t JOIN skill_versions v ON v.topic = t.name AND v.status = 'live' WHERE t.status = 'active'"
    ).fetchall():
        current = set(members(connection, settings, topic["name"]))
        if not any(_covers(version, current) for version in versions(connection, topic["name"])):
            build(connection, settings, topic["name"])


def overlap(connection: sqlite3.Connection, settings: Settings) -> None:
    """Claude lists groups of skills an agent could confuse (overlapping triggers), with a proposed
    scope for each, and the result is kept for the dashboard. Nothing is changed: a SKILL.md without
    the marker is the user's to edit, and a tool-written one is scoped by its next build, whose
    writer is shown the skills that overlap it."""
    files = library_files(settings)
    groups = writer.find_overlaps(settings.skill_model, library(settings))
    active = set()
    if settings.active_skills_directory:
        active = {path.parent.name for path in settings.active_skills_directory.glob("*/SKILL.md")}
    result = {
        "checked_at": now(), "skills": len(files), "active": sorted(active),
        "tool_written": sorted(name for name, path in files.items() if written_by_tool(path)),
        "groups": [group.model_dump() for group in groups],
    }
    set_metadata(connection, "skill_overlaps", json.dumps(result))
    print(f"Skills: {len(groups)} groups of overlapping skills among {len(files)} ({len(active)} active)")
    for number, group in enumerate(groups, 1):
        names = ", ".join(_overlap_label(name, result) for name in group.skills)
        print(f"\n{number}. {names}\n   Overlap: {group.triggers}")
        for scope in group.scopes:
            print(f"   - {_overlap_label(scope.skill, result)}: {scope.scope}")


def _overlap_label(name: str, result: dict) -> str:
    notes = [note for note, names in (("active", result["active"]), ("tool-written", result["tool_written"])) if name in names]
    return f"{name} ({', '.join(notes)})" if notes else name


def overlaps(connection: sqlite3.Connection) -> dict | None:
    """The last overlap check, or None if none ran."""
    stored = get_metadata(connection, "skill_overlaps")
    return json.loads(stored) if stored else None


def diff(connection: sqlite3.Connection, name: str, version: int) -> str:
    """Unified diff of SKILL.md and every script from the live version to the given one."""
    live = live_version(connection, name)
    row = connection.execute("SELECT * FROM skill_versions WHERE topic = ? AND version = ?", (name, version)).fetchone()
    if not live or not row or not row["spec"]:
        return ""
    old, new = (skill_spec.SkillSpec.model_validate_json(r["spec"]) for r in (live, row))
    files = [("SKILL.md", live["skill_md"], row["skill_md"])]
    old_scripts = {s.filename: s.code for s in old.scripts}
    new_scripts = {s.filename: s.code for s in new.scripts}
    files += [
        (f"scripts/{filename}", old_scripts.get(filename, ""), new_scripts.get(filename, ""))
        for filename in sorted(old_scripts.keys() | new_scripts.keys())
    ]
    return "".join(
        "".join(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            f"v{live['version']}/{path}", f"v{version}/{path}",
        ))
        for path, before, after in files
    )
