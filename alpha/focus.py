"""What the user is working on now (context): one focus brief per active project, written by Claude from local
activity. Briefs drive discovery (search queries) and the judge (judge.py); they are not a source of items.

Active projects are the configured ones (the `projects` table: vault notes and config.toml repositories) plus
every repository under `[context] projects_directory` with commits or agent prompts in the last `days`.

Activity, all read locally, each feed only when its `[context.<feed>]` table says `enabled = true`:
- agents: prompts the user typed to coding agents: omp's prompt history (~/.omp/agent/history.db, with the
  folder each was typed in) and Claude Code sessions (~/.claude/projects/*/*.jsonl, interactive `cli` sessions
  only; the headless test runs of skills are `sdk-cli` and skipped). A prompt belongs to the project whose
  folder it was typed in.
- git: `git log` per project repository: commit subjects and changed paths.
- superwhisper: dictations (~/Documents/superwhisper/recordings/*/meta.json), except into `excluded_apps`.
- anarlog: meetings (~/Library/Application Support/hyprnote/app.db, opened read-only): the session's summary
  and notes, or its transcript, from the last `days`; sessions the user excluded on the Focus page are never
  read. Names of the people Anarlog knows are removed from every brief.
- alpha: Alpha's own loop, from its database, always on: the user's idea chats (their words and the distilled
  core, own_idea.py) and the suggestions implemented or marked done (headline, how, and the agent's report:
  what changed and why), each tied to its project. What the user asked to build, and built, is the plainest
  statement of what they are working on.
Dictations, meetings, and prompts typed outside any project folder carry no project; Claude assigns the
relevant ones.

Privacy: this activity goes only to Claude (writer.py). The brief Claude writes has no personal details or names
of people; known meeting participants, email addresses, and phone numbers are also removed from it in code.
Only briefs leave the machine to third parties (Jev, and as search queries). Logs carry counts only.

A brief is rewritten every `refresh_days`, or sooner once the project has at least half again as much new
activity as the brief was written from (and at least MIN_NEW_ITEMS). The user's latest edit of a project's
brief (Focus page) is given to every later rewrite as the user's correction.
"""

import json
import re
import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import writer
from .config import Settings
from .pipeline import parallel
from .projects import _local_file, _repository_spec
from .storage import get_metadata, now, set_metadata

HOME = Path.home()
OMP_HISTORY = HOME / ".omp" / "agent" / "history.db"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
SUPERWHISPER = HOME / "Documents" / "superwhisper" / "recordings"
ANARLOG = HOME / "Library" / "Application Support" / "hyprnote" / "app.db"
FEEDS = {"agent": "agent prompts", "git": "commits", "dictation": "dictations", "meeting": "meetings",
         "alpha": "ideas and implementations"}

MIN_NEW_ITEMS = 10
# Metadata: when the user last excluded a meeting or let one back; briefs written from meetings before it are due.
EXCLUSIONS_CHANGED = "focus_exclusions_changed_at"
REPOSITORY_DEPTH = 3
SPEC_CHARACTERS = 3000
ITEM_CHARACTERS = 1200
MEETING_CHARACTERS = 8000
# Per source in one brief prompt, newest items first.
SOURCE_CHARACTERS = {"agent": 40_000, "git": 12_000, "dictation": 20_000, "meeting": 24_000, "alpha": 16_000}
ALPHA_ITEM_CHARACTERS = 2500  # an idea chat or an implementation report says more than a prompt
REPORT_CHARACTERS = 1500      # of the agent's report: what it changed and why, before how it checked
ASSIGN_ITEM_CHARACTERS = 600
ASSIGN_CHARACTERS = 150_000
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"\+?\d[\d ().-]{7,}\d")


@dataclass
class Item:
    source: str                      # agent | git | dictation | meeting | alpha
    at: datetime
    text: str
    path: Path | None = None         # the folder an agent prompt was typed in, or a commit's repository
    project: str | None = None
    key: str = ""                    # Anarlog session id for meetings


@dataclass
class Project:
    name: str
    spec: str
    folder: Path | None
    configured: str | None           # vault note | config.toml | GitHub, or None for a repository found by activity
    items: list[Item] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        found = Counter(item.source for item in self.items)
        return {source: found[source] for source in FEEDS if found[source]}

    def reason(self, settings: Settings) -> str:
        counts = self.counts()
        activity = ", ".join(f"{n} {FEEDS[source]}" for source, n in counts.items())
        activity = f"{activity} in the last {settings.context.days} days" if activity else "no recent activity"
        return f"configured ({self.configured}); {activity}" if self.configured else activity


def _normal(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


# --- Activity sources -------------------------------------------------------------------------------------


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def agent_prompts(since: datetime) -> list[Item]:
    items: list[Item] = []
    if OMP_HISTORY.is_file():
        connection = _read_only(OMP_HISTORY)
        try:
            for row in connection.execute(
                "SELECT prompt, created_at, cwd FROM history WHERE created_at >= ?", (int(since.timestamp()),)
            ):
                items.append(Item("agent", datetime.fromtimestamp(row["created_at"], UTC), row["prompt"],
                                  Path(row["cwd"]) if row["cwd"] else None))
        finally:
            connection.close()
    for path in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        if datetime.fromtimestamp(path.stat().st_mtime, UTC) < since:
            continue
        with path.open(errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if (entry.get("type") != "user" or entry.get("entrypoint") != "cli" or entry.get("isMeta")
                        or entry.get("isSidechain") or not entry.get("timestamp")):
                    continue
                content = (entry.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = "\n".join(part.get("text", "") for part in content if part.get("type") == "text")
                # Slash commands, command output, and injected reminders are wrapped in tags, not typed prose.
                if not isinstance(content, str) or content.lstrip().startswith("<"):
                    continue
                at = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                if at >= since:
                    items.append(Item("agent", at, content, Path(entry["cwd"]) if entry.get("cwd") else None))
    unique: dict[str, Item] = {}
    for item in sorted(items, key=lambda item: item.at):
        if len(item.text.strip()) >= 3:
            unique.setdefault(item.text.strip(), item)
    return list(unique.values())


def repositories(root: Path) -> list[Path]:
    """Git repositories under root, a few levels deep; hidden folders (worktrees) and dependencies skipped."""
    found: list[Path] = []

    def walk(folder: Path, depth: int) -> None:
        if (folder / ".git").exists():
            found.append(folder)
        if depth == REPOSITORY_DEPTH:
            return
        try:
            children = sorted(child for child in folder.iterdir() if child.is_dir())
        except OSError:
            return
        for child in children:
            if not child.name.startswith(".") and child.name != "node_modules":
                walk(child, depth + 1)

    if root.is_dir():
        walk(root, 0)
    return found


def commits(repository: Path, since: datetime) -> list[Item]:
    result = subprocess.run(
        ["git", "-C", str(repository), "log", f"--since={since.isoformat()}", "--no-merges",
         "--format=%x1e%aI%x1f%s", "--name-only"],
        capture_output=True, text=True,
    )
    items = []
    for record in result.stdout.split("\x1e")[1:]:
        header, _, names = record.partition("\n")
        at, _, subject = header.partition("\x1f")
        paths = [name for name in names.splitlines() if name.strip()]
        shown = ", ".join(paths[:6]) + (f" and {len(paths) - 6} more" if len(paths) > 6 else "")
        items.append(Item("git", datetime.fromisoformat(at), f"{subject} [{shown}]" if shown else subject, repository))
    return items


def dictations(since: datetime, excluded_apps: tuple[str, ...]) -> list[Item]:
    items = []
    if not SUPERWHISPER.is_dir():
        return items
    floor = since.timestamp() - 86400  # a folder is written when its recording is, so older folders are skipped
    for folder in SUPERWHISPER.iterdir():
        meta = folder / "meta.json"
        try:
            if folder.stat().st_mtime < floor:
                continue
            data = json.loads(meta.read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        app = ((data.get("promptContext") or {}).get("applicationContext") or {}).get("name") or ""
        text = (data.get("llmResult") or data.get("result") or "").strip()
        if not text or not data.get("datetime") or app in excluded_apps:
            continue
        at = datetime.fromisoformat(data["datetime"]).astimezone(UTC)  # local time without an offset
        if at >= since:
            items.append(Item("dictation", at, text))
    return items


def _prosemirror_text(node) -> str:
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        inner = "".join(_prosemirror_text(child) for child in node.get("content") or [])
        return inner + ("\n" if node.get("type") in ("paragraph", "heading", "listItem", "blockquote") else "")
    if isinstance(node, list):
        return "".join(_prosemirror_text(child) for child in node)
    return ""


def _document_text(body_format: str, body: str) -> str:
    if body_format == "prosemirror_json":
        try:
            return _prosemirror_text(json.loads(body)).strip()
        except ValueError:
            return ""
    return body.strip()


def meetings(since: datetime) -> list[tuple[str, str, str, str]]:
    """(session id, title, date, text) for every Anarlog meeting since `since`, newest first."""
    if not ANARLOG.is_file():
        return []
    connection = _read_only(ANARLOG)
    try:
        sessions = connection.execute(
            "SELECT id, title, COALESCE(NULLIF(started_at, ''), created_at) AS at FROM sessions "
            "WHERE deleted_at IS NULL AND COALESCE(NULLIF(started_at, ''), created_at) >= ? ORDER BY at DESC",
            (since.isoformat(),),
        ).fetchall()
        result = []
        for session in sessions:
            documents = connection.execute(
                "SELECT kind, body_format, body FROM session_documents WHERE session_id = ? AND deleted_at IS NULL "
                "AND kind IN ('summary', 'note') ORDER BY kind = 'note', sort_order",
                (session["id"],),
            ).fetchall()
            text = "\n\n".join(filter(None, (_document_text(d["body_format"], d["body"]) for d in documents)))
            if not text:
                words = []
                for transcript in connection.execute(
                    "SELECT words_json FROM transcripts WHERE session_id = ? AND deleted_at IS NULL ORDER BY started_at_ms",
                    (session["id"],),
                ):
                    words += [word.get("text", "") for word in json.loads(transcript["words_json"] or "[]")]
                text = re.sub(r"\s+", " ", " ".join(words)).strip()
            if text:
                result.append((session["id"], session["title"] or "Untitled", session["at"][:10], text))
        return result
    finally:
        connection.close()


def alpha_loop(connection: sqlite3.Connection, since: datetime) -> list[Item]:
    """Alpha's own loop since `since`, each tied to its project: the user's idea chats (their words and the
    distilled core) and the suggestions implemented or marked done (headline, how, who wrote it, and the agent's
    report: what changed and why, cut before how it checked)."""
    items: list[Item] = []
    for row in connection.execute("SELECT * FROM idea_chats WHERE updated_at > ?", (since.isoformat(),)):
        messages = json.loads(row["messages"])
        said = " / ".join(m["content"] for m in messages if m["role"] == "user")
        plan = writer.IdeaPlan.model_validate_json(row["plan"])
        kept = "kept as a suggestion" if row["suggested_at"] else "still being shaped"
        items.append(Item("alpha", datetime.fromisoformat(row["updated_at"]),
                          f"Idea ({kept}). The user said: {said}\nThe core: {plan.core}\nPlanned: {plan.action}",
                          project=row["project"]))
    for row in connection.execute(
        "SELECT s.*, i.title FROM suggestions s JOIN items i ON i.id = s.item_id WHERE s.basis_kind != 'match' "
        "OR s.implement_status IS NOT NULL OR s.status = 'done'"
    ):
        at = row["implement_finished_at"] or row["implement_started_at"] or row["rated_at"] or row["created_at"]
        if not at or at <= since.isoformat() or row["implement_status"] not in ("implemented", "handoff") and row["status"] != "done":
            continue
        who = {"user": "the user's own idea", "note": "a note the user made on the app itself"}.get(
            row["basis_kind"], f"Alpha's suggestion from “{row['title']}”")
        report = (row["implement_report"] or "").split("## How I checked", 1)[0].strip()[:REPORT_CHARACTERS]
        state = ("marked done by the user" if row["status"] == "done" else
                 f"implemented on branch {row['implement_branch']}, waiting for review" if row["implement_status"] == "implemented"
                 else "written as a task file")
        text = f"Built ({who}; {state}): {row['headline'] or row['action']}\nHow: {row['action']}"
        if row["basis_kind"] in ("user", "note"):
            text += f"\nIn the user's words: {row['basis']}"
        if report:
            text += f"\nThe agent's report: {report}"
        items.append(Item("alpha", datetime.fromisoformat(at), text, project=row["project"]))
    return items


def person_names() -> set[str]:
    """Names of people Anarlog knows (meeting participants and contacts), removed from every brief. A single
    name part that is also an ordinary word (Will, Bill) is left alone, so sentences survive."""
    if not ANARLOG.is_file():
        return set()
    connection = _read_only(ANARLOG)
    try:
        rows = connection.execute(
            "SELECT display_name AS name FROM session_participants UNION SELECT name FROM humans"
        ).fetchall()
    finally:
        connection.close()
    dictionary = Path("/usr/share/dict/words")
    words = {w for w in dictionary.read_text(errors="replace").split() if w.islower()} if dictionary.is_file() else set()
    names = set()
    for row in rows:
        name = (row["name"] or "").strip()
        if len(name) >= 3:
            names.add(name)
            names.update(part for part in name.split()
                         if len(part) >= 3 and part[0].isupper() and part.lower() not in words)
    return names


def redact(text: str, names: set[str]) -> str:
    text = PHONE.sub("[number]", EMAIL.sub("[email]", text))
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(name)}\b", "a person", text)
    return text


# --- Projects and briefs ----------------------------------------------------------------------------------


def _configured(connection: sqlite3.Connection, repos: list[Path]) -> list[Project]:
    by_name = {_normal(repo.name): repo for repo in repos}
    projects = []
    for row in connection.execute("SELECT name, source, spec FROM projects ORDER BY name"):
        source = Path(row["source"])
        if row["source"].startswith("github.com/"):
            kind, folder = "GitHub", by_name.get(_normal(row["source"].rsplit("/", 1)[-1]))
        elif source.suffix == ".md":
            kind, folder = "vault note", by_name.get(_normal(row["name"]))
        else:
            kind, folder = "config.toml", source if source.is_dir() else by_name.get(_normal(row["name"]))
        projects.append(Project(row["name"], row["spec"], folder, kind))
    return projects


def _owner(path: Path | None, projects: list[Project]) -> Project | None:
    """The project a folder belongs to: the deepest project folder containing it, else a project whose
    name is one of the folder's own path parts (a prompt typed in the parent of the project's repository)."""
    if path is None:
        return None
    containing = [p for p in projects if p.folder and _inside(path, p.folder)]
    if containing:
        return max(containing, key=lambda p: len(p.folder.parts))
    parts = {_normal(part) for part in path.relative_to(HOME).parts} if _inside(path, HOME) else set()
    return next((p for p in projects if _normal(p.name) in parts), None)


def scan(connection: sqlite3.Connection, settings: Settings) -> tuple[list[Project], list[Item], list[dict]]:
    """Active projects with the activity already tied to them, the activity Claude still has to assign,
    and every Anarlog meeting in the window (id, title, date, excluded) for the Focus page."""
    context = settings.context
    since = datetime.now(UTC) - timedelta(days=context.days)
    repos = repositories(context.projects_directory) if context.projects_directory else []
    projects = _configured(connection, repos)
    configured_folders = [p.folder for p in projects if p.folder]
    for repo in repos:
        if any(_inside(repo, folder) or _inside(folder, repo) for folder in configured_folders):
            continue  # part of a configured project (or its parent folder)
        if _normal(repo.name) in {_normal(p.name) for p in projects}:
            continue
        spec = _repository_spec(_local_file(repo))
        projects.append(Project(repo.name, spec, repo, None))
    if context.git:
        for project in projects:
            if project.folder:
                project.items += commits(project.folder, since)
    by_name = {_normal(p.name): p for p in projects}
    for item in alpha_loop(connection, since):  # already tied to a project; one not active yet becomes active
        owner = by_name.get(_normal(item.project or ""))
        if owner is None:
            owner = Project(item.project, "", None, None)
            projects.append(owner)
            by_name[_normal(owner.name)] = owner
        owner.items.append(item)
    unassigned: list[Item] = []
    prompts = agent_prompts(since) if context.agents else []
    for item in prompts:
        owner = _owner(item.path, projects)
        if owner:
            item.project = owner.name
            owner.items.append(item)
        else:
            unassigned.append(item)
    typed = {_normal(item.text)[:60] for item in prompts}
    # A dictation into a terminal is usually a prompt already read above.
    if context.superwhisper:
        unassigned += [item for item in dictations(since, context.excluded_dictation_apps)
                       if _normal(item.text)[:60] not in typed]
    excluded = {row["session_id"] for row in connection.execute("SELECT session_id FROM focus_exclusions")}
    meeting_since = datetime.now(UTC) - timedelta(days=context.meeting_days)
    listed = []
    for session_id, title, date, text in meetings(meeting_since) if context.anarlog else []:
        listed.append({"id": session_id, "title": title, "date": date, "excluded": session_id in excluded})
        if session_id not in excluded:
            unassigned.append(Item("meeting", datetime.fromisoformat(date).replace(tzinfo=UTC),
                                   f"Meeting: {title}\n{text[:MEETING_CHARACTERS]}", key=session_id))
    active = [p for p in projects if p.configured or p.items]
    return active, unassigned, listed


def _assign(settings: Settings, projects: list[Project], unassigned: list[Item]) -> None:
    """Claude ties relevant dictations, meetings, and loose prompts to projects."""
    listing = "\n".join(f"- {p.name}: {' '.join(p.spec.split())[:300] or 'a repository'}" for p in projects)
    numbered, size = [], 0
    for index, item in enumerate(sorted(unassigned, key=lambda item: item.at, reverse=True)):
        limit = MEETING_CHARACTERS if item.source == "meeting" else ASSIGN_ITEM_CHARACTERS
        line = f"[{index}] ({item.source}, {item.at:%Y-%m-%d}) {item.text[:limit]}"
        if size + len(line) > ASSIGN_CHARACTERS:
            break
        numbered.append((index, item, line))
        size += len(line)
    if not numbered:
        return
    by_index = {index: item for index, item, _ in numbered}
    by_name = {p.name: p for p in projects}
    for assignment in writer.assign_activity(settings.claude_model, listing, "\n".join(line for *_, line in numbered)):
        item, project = by_index.get(assignment.item), by_name.get(assignment.project)
        if item and project and item.project is None:
            item.project = project.name
            project.items.append(item)


def _activity(project: Project) -> str:
    sections = []
    for source, label in FEEDS.items():
        lines, size = [], 0
        for item in sorted((i for i in project.items if i.source == source), key=lambda i: i.at, reverse=True):
            limit = {"meeting": MEETING_CHARACTERS, "alpha": ALPHA_ITEM_CHARACTERS}.get(source, ITEM_CHARACTERS)
            line = f"- {item.at:%Y-%m-%d}: {' '.join(item.text.split())[:limit]}"
            if size + len(line) > SOURCE_CHARACTERS[source]:
                break
            lines.append(line)
            size += len(line)
        if lines:
            sections.append(f"{label.capitalize()} (newest first):\n" + "\n".join(lines))
    return "\n\n".join(sections)


def latest_briefs(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """project -> its newest brief (the user's edit when that is newest)."""
    return {
        row["project"]: row
        for row in connection.execute(
            "SELECT * FROM focus_briefs WHERE id IN (SELECT MAX(id) FROM focus_briefs GROUP BY project) ORDER BY project"
        )
    }


def _due(project: Project, latest: sqlite3.Row | None, settings: Settings, exclusions_changed: str | None) -> bool:
    if latest is None:
        return True
    # A brief written from meetings before the user excluded (or let back) a meeting is rewritten without it.
    if exclusions_changed and latest["created_at"] < exclusions_changed and json.loads(latest["sources"]).get("meeting"):
        return True
    written = datetime.fromisoformat(latest["created_at"])
    if datetime.now(UTC) - written >= timedelta(days=settings.context.refresh_days):
        return True
    new = sum(1 for item in project.items if item.at > written)
    return new >= max(MIN_NEW_ITEMS, latest["activity"] // 2)


def refresh(connection: sqlite3.Connection, settings: Settings, force: bool = False) -> int:
    """Write the briefs that are due (all with force). Returns how many were written."""
    projects, unassigned, listed = scan(connection, settings)
    latest = latest_briefs(connection)
    exclusions_changed = get_metadata(connection, EXCLUSIONS_CHANGED)
    due = [p for p in projects if force or _due(p, latest.get(p.name), settings, exclusions_changed)]
    if due:
        _assign(settings, projects, unassigned)
    set_metadata(connection, "focus_scan", json.dumps({
        "scanned_at": now(),
        "projects": [{"name": p.name, "folder": str(p.folder) if p.folder else None, "reason": p.reason(settings),
                      "counts": p.counts()} for p in projects],
        "meetings": listed,
        "unassigned": dict(Counter(item.source for item in unassigned if item.project is None)) if due else None,
    }))
    if not due:
        print(f"Focus: {len(projects)} active projects, no brief due")
        return 0
    names = person_names() if settings.context.anarlog else set()
    corrections = {
        row["project"]: row["brief"]
        for row in connection.execute(
            "SELECT project, brief FROM focus_briefs WHERE id IN "
            "(SELECT MAX(id) FROM focus_briefs WHERE author = 'user' GROUP BY project)"
        )
    }
    previous = {
        row["project"]: row["brief"]
        for row in connection.execute(
            "SELECT project, brief FROM focus_briefs WHERE id IN "
            "(SELECT MAX(id) FROM focus_briefs WHERE author = 'claude' GROUP BY project)"
        )
    }

    def task(project: Project) -> str:
        return writer.write_brief(settings.claude_model, project.name, project.spec[:SPEC_CHARACTERS],
                                  _activity(project), previous.get(project.name), corrections.get(project.name))

    def store(project: Project, brief: str) -> None:
        connection.execute(
            "INSERT INTO focus_briefs (project, brief, author, sources, activity, created_at) VALUES (?, ?, 'claude', ?, ?, ?)",
            (project.name, redact(brief, names), json.dumps(project.counts()), len(project.items), now()),
        )
        connection.commit()

    parallel(due, task, store, f"Focus briefs (Claude) for {len(due)} of {len(projects)} active projects")
    return len(due)


def save_edit(connection: sqlite3.Connection, project: str, brief: str) -> None:
    """The user's own version of a brief: current until the next rewrite, and given to every later one."""
    latest = latest_briefs(connection).get(project)
    if latest is None or not brief.strip() or brief.strip() == latest["brief"].strip():
        return
    connection.execute(
        "INSERT INTO focus_briefs (project, brief, author, sources, activity, created_at) VALUES (?, ?, 'user', ?, ?, ?)",
        (project, brief.strip(), latest["sources"], latest["activity"], now()),
    )
    connection.commit()


def set_excluded(connection: sqlite3.Connection, session_id: str, excluded: bool) -> None:
    if excluded:
        connection.execute("INSERT OR IGNORE INTO focus_exclusions (session_id, excluded_at) VALUES (?, ?)",
                           (session_id, now()))
    else:
        connection.execute("DELETE FROM focus_exclusions WHERE session_id = ?", (session_id,))
    connection.commit()
    set_metadata(connection, EXCLUSIONS_CHANGED, now())


def excluded_meetings(connection: sqlite3.Connection) -> set[str]:
    return {row["session_id"] for row in connection.execute("SELECT session_id FROM focus_exclusions")}
