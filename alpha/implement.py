"""Implement a suggestion: a headless omp agent (omp_agent.py) carries out its how in the project's own repository,
on a new branch in a separate worktree, and reports; Alpha then commits the change on that branch itself.

The user's working folder is never touched: the branch alpha/<slug> starts from the repository's current HEAD
(uncommitted changes there are not included) in var/worktrees/<project>-<slug>. The agent is an omp session with
`agent_model`, so it has the user's MCP servers (Alpha's own search, Interface Lineage, browsers, ...) and skills;
it is told to read, edit files in the worktree, and run the project's tests, checks, and dependency installs, and
not to commit, push, deploy, send, or buy (omp has no hard deny-list for commands: the rules, the worktree, and the
time limit are the guard). Alpha commits as the user (their git identity and hooks, no trailers) with
"Alpha: <headline>"; nothing is pushed, deployed, published, sent, or bought by Alpha.

The agent is given the suggestion, the source item, the project's spec and focus brief, and the user's Ask about
this conversation (suggestion_chat.py), whose decisions and constraints take precedence over the suggestion.

A project with no local repository (only a GitHub repository or a vault note) gets a ready-to-run task file
instead, var/handoffs/<project>/<slug>.md; nothing runs.

One implementation at a time: each waits for var/implement.lock (the Mac is short on memory).
"""

import fcntl
import re
import shutil
import sqlite3
import subprocess
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import focus, omp_agent, own_idea, suggestion_chat, writer
from .config import TOOL_ROOT, Settings, load_settings
from .projects import _local_file, _repository_spec
from .skills import slug
from .storage import content_of, now

WORKTREES = TOOL_ROOT / "var" / "worktrees"
HANDOFFS = TOOL_ROOT / "var" / "handoffs"
LOCK = TOOL_ROOT / "var" / "implement.lock"
# A run still marked implementing past settings.implement_minutes plus this grace stopped without saying so; it may
# start again. A queued run past QUEUED_STALE did too.
STALE_GRACE = timedelta(minutes=15)
QUEUED_STALE = timedelta(hours=3)
BRANCH_WORDS = 6
BRANCH_CHARACTERS = 40
ITEM_CHARACTERS = 20_000
SPEC_CHARACTERS = 6000
# Commands a project's tests, checks, and dependency installs usually need; named to the agent as examples.
CHECK_COMMANDS = (
    "uv run pytest", "uv run ruff", "uv sync", "pytest", "npm test", "npm run lint", "npm install", "npx tsc",
    "bun test", "bun install", "cargo test", "cargo check", "go test", "make test",
)
AGENT_PROMPT = """You are carrying out one suggestion in the user's project {project}. You are in a git worktree of \
its repository, on the new branch {branch}; the user's own working folder is elsewhere and stays untouched.

# The suggestion
{headline}
Outcome the user expects: {outcome}

How (start here): {action}
{own}{questions}
# Rules
- Start with the first step. Keep the change small and reviewable: the smallest change that carries out the first \
step well, in the project's own style and structure, in as few files as you can. Read only what you need (search, \
then read the parts that matter): your context is limited.
- Work only in this folder: read, edit, and create files here.
- Run the project's existing tests or checks, if any (see package.json, pyproject.toml, Makefile, the README), and \
install its dependencies in this folder if they are missing; for example {commands}. Use your MCP servers and skills \
when they help (Alpha's own search and get_item for the source item and what else the user collected, a browser to \
look at the result, Interface Lineage for interaction design); never to send, post, publish, or buy.
- Do not commit, push, deploy, publish, send, sign up, or buy anything, and change nothing outside this folder. \
Alpha commits your changes on this branch after you finish, and the user reviews them.
- Do not ask questions: make sensible choices and say what you assumed.
- If the suggestion does not fit this repository, or needs something you do not have (an account, a paid tool, \
data), change only what helps, or nothing, and say so.

Finish with your report as your final message, in Markdown, with exactly these sections:
## What I changed
## Why
## How I checked it
## What's left

# The project
{spec}

# What the user is working on now (focus brief)
{brief}

# The source item: {title}
{url}

{summary}

{text}
"""
HANDOFF = """# {headline}

Project: {project} (no local repository Alpha could find, so nothing ran; run this task where the code lives)
Outcome: {outcome}
Source: {title}, {url}

## Task
{action}
{own}{questions}
## First step
{first_step}

## Acceptance
- The first step above is done, as a small, reviewable change in the project's own style.
- The project's existing tests or checks pass (or the report says why one could not run).
- Nothing is pushed, deployed, published, sent, or bought; the change waits on its own branch for review.
- The work ends with a report: what changed, why, how it was checked, what's left.

## The project
{spec}

## What the user is working on now (focus brief)
{brief}

## The source item: {title}
{url}

{summary}

{text}
"""


class Failure(Exception):
    """A run that cannot go on; its message is the reason shown on the suggestion."""


@dataclass(frozen=True)
class Plan:
    """What Implement will do for one suggestion, read back before it starts."""
    item: sqlite3.Row
    suggestion: sqlite3.Row
    headline: str
    repository: Path | None   # None: no local repository, a handoff file instead
    head: str                 # the repository's current branch (or commit) the new branch starts from
    branch: str
    worktree: Path
    handoff: Path


def _git(folder: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    """git in `folder` with the user's own environment: their identity, config, and hooks."""
    completed = subprocess.run(["git", "-C", str(folder), *arguments], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL)
    if check and completed.returncode != 0:
        raise Failure(f"git {' '.join(arguments[:2])} failed: {(completed.stderr or completed.stdout).strip()[-600:]}")
    return completed


def repository(settings: Settings, project: str) -> Path | None:
    """The project's local git repository: its config.toml folder, else a repository under `[context]
    projects_directory` with its name (or its GitHub repository's name)."""
    folders = [settings.project_repositories[project]] if project in settings.project_repositories else []
    names = {focus._normal(project)}
    if project in settings.project_github:
        names.add(focus._normal(settings.project_github[project].rsplit("/", 1)[-1]))
    if settings.context.projects_directory:
        folders += [repo for repo in focus.repositories(settings.context.projects_directory)
                    if focus._normal(repo.name) in names and not repo.is_relative_to(WORKTREES)]
    for folder in folders:
        if folder.is_dir() and (top := _git(folder, "rev-parse", "--show-toplevel", check=False)).returncode == 0:
            return Path(top.stdout.strip())
    return None


def headline_of(suggestion: sqlite3.Row) -> str:
    """The headline, or (before it was written) the how's first clause."""
    return suggestion["headline"] or re.split(r"[;:.]", suggestion["action"].split(":", 1)[-1])[0].strip()


def first_step(action: str) -> str:
    match = re.search(r"first step:?\s*(.+)$", action, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else action


def state(suggestion: sqlite3.Row) -> str | None:
    """The implementation's state; a queued or implementing run past its stale limit (implement_minutes plus
    STALE_GRACE, or QUEUED_STALE) shows as stale."""
    status = suggestion["implement_status"]
    if status in ("queued", "implementing") and suggestion["implement_started_at"]:
        limit = (QUEUED_STALE if status == "queued"
                 else timedelta(minutes=load_settings().implement_minutes) + STALE_GRACE)
        if datetime.now(UTC) - datetime.fromisoformat(suggestion["implement_started_at"]) > limit:
            return "stale"
    return status


def can_start(suggestion: sqlite3.Row) -> bool:
    return state(suggestion) in (None, "failed", "handoff", "stale")


def _row(connection: sqlite3.Connection, item_id: str, project: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM suggestions WHERE item_id = ? AND project = ?",
                              (item_id, project)).fetchone()


def plan(connection: sqlite3.Connection, settings: Settings, item_id: str, project: str) -> Plan | None:
    suggestion = _row(connection, item_id, project)
    item = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if suggestion is None or item is None:
        return None
    headline = headline_of(suggestion)
    name = slug(" ".join(headline.split()[:BRANCH_WORDS]))
    if len(name) > BRANCH_CHARACTERS:  # cut at a word, not inside one
        name = name[:BRANCH_CHARACTERS + 1].rsplit("-", 1)[0]
    name = name.strip("-") or "suggestion"
    repo = repository(settings, project)
    head = ""
    branch, worktree = f"alpha/{name}", WORKTREES / f"{slug(project)}-{name}"
    if repo:
        current = _git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
        commit = _git(repo, "rev-parse", "--short", "HEAD", check=False).stdout.strip()
        head = f"{current} ({commit})" if current else commit
        # An earlier run of this suggestion is replaced; another suggestion's branch or folder is left alone.
        own = {suggestion["implement_branch"], suggestion["implement_path"]}
        number = 1
        while ((branch not in own and _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}",
                                           check=False).returncode == 0)
               or (str(worktree) not in own and worktree.exists())):
            number += 1
            branch, worktree = f"alpha/{name}-{number}", WORKTREES / f"{slug(project)}-{name}-{number}"
    return Plan(item, suggestion, headline, repo, head, branch, worktree, HANDOFFS / slug(project) / f"{name}.md")


def _context(connection: sqlite3.Connection, plan: Plan) -> dict[str, str]:
    """What the agent (or the handoff file) is given: the suggestion, the project's spec and brief, the item."""
    project = plan.suggestion["project"]
    spec = connection.execute("SELECT spec FROM projects WHERE name = ?", (project,)).fetchone()
    spec = spec["spec"] if spec else (_repository_spec(_local_file(plan.repository)) if plan.repository else "")
    brief = focus.latest_briefs(connection).get(project)
    item = plan.item
    return {
        "project": project, "branch": plan.branch, "headline": plan.headline, "commands": ", ".join(CHECK_COMMANDS),
        "questions": questions(connection, plan.item["id"], project), "own": own_idea_of(connection, plan.suggestion),
        "outcome": plan.suggestion["outcome"] or "(not written)", "action": plan.suggestion["action"],
        "first_step": first_step(plan.suggestion["action"]),
        "spec": (spec or "(no description)")[:SPEC_CHARACTERS], "brief": brief["brief"] if brief else "(none yet)",
        "title": item["title"] or item["title_source"] or item["url"], "url": item["url"],
        "summary": item["summary"] or "",
        "text": writer.item_text(item, content_of(connection, item["id"]))[:ITEM_CHARACTERS],
    }


def questions(connection: sqlite3.Connection, item_id: str, project: str) -> str:
    """The user's Ask about this conversation, as a section of the agent's prompt ('' when there is none)."""
    messages = suggestion_chat.get(connection, item_id, project)
    if not messages:
        return ""
    return ("\n# The user's questions and answers about it\nThe user asked about this suggestion before implementing. "
            "Their decisions and constraints here take precedence over the suggestion.\n\n"
            f"{suggestion_chat.transcript(messages)}\n")


def own_idea_of(connection: sqlite3.Connection, suggestion: sqlite3.Row) -> str:
    """The user's own words when they wrote the suggestion themselves (own_idea.py, with the chat that shaped it
    while it is still there), as a section of the agent's prompt; '' for one Alpha wrote."""
    if suggestion["basis_kind"] != "user":
        return ""
    section = ("\n# The user's own idea\nThe user wrote this suggestion themselves; the how above is Alpha's write-up "
               f"of it. Their words, which take precedence:\n\n{suggestion['basis']}\n")
    chat = own_idea.get(connection, suggestion["item_id"])
    if chat and chat["project"] == suggestion["project"] and chat["suggested_at"]:
        section += f"\nHow the idea was shaped with Alpha:\n\n{own_idea.transcript(own_idea.messages_of(chat))}\n"
    return section


def _set(connection: sqlite3.Connection, item_id: str, project: str, **values) -> None:
    columns = ", ".join(f"implement_{name} = ?" for name in values)
    connection.execute(f"UPDATE suggestions SET {columns} WHERE item_id = ? AND project = ?",
                       (*values.values(), item_id, project))
    connection.commit()


def _clear(connection: sqlite3.Connection, item_id: str, project: str, status: str | None) -> None:
    _set(connection, item_id, project, status=status, branch=None, base=None, path=None, report=None, error=None,
         files=None, started_at=now() if status else None, finished_at=None)


def handoff(connection: sqlite3.Connection, plan: Plan) -> Path:
    """The task file for a project with no local repository; nothing runs."""
    plan.handoff.parent.mkdir(parents=True, exist_ok=True)
    plan.handoff.write_text(HANDOFF.format(**_context(connection, plan)))
    item_id, project = plan.item["id"], plan.suggestion["project"]
    _clear(connection, item_id, project, "handoff")
    _set(connection, item_id, project, path=str(plan.handoff), finished_at=now())
    return plan.handoff


def queue(connection: sqlite3.Connection, item_id: str, project: str) -> None:
    """Marked before the background run starts, so the dashboard shows it at once. An earlier run's branch and
    worktree stay recorded, so the run can delete them before it starts again."""
    _set(connection, item_id, project, status="queued", error=None, started_at=now(), finished_at=None)


def repository_of(worktree: Path) -> Path | None:
    """The repository a worktree belongs to."""
    if not worktree.is_dir():
        return None
    common = _git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    return Path(common.stdout.strip()).parent if common.returncode == 0 else None


def _remove_worktree(settings: Settings, suggestion: sqlite3.Row) -> None:
    """Delete a run's worktree and branch, from the repository they belong to: Alpha's own (under var/worktrees,
    named alpha/...), or a Let There build's (named letthere/..., in Let There's worktrees folder)."""
    folder = Path(suggestion["implement_path"]) if suggestion["implement_path"] else None
    branch = suggestion["implement_branch"] or ""
    ours = folder is not None and folder.is_relative_to(WORKTREES)
    theirs = folder is not None and branch.startswith("letthere/") and folder.parent.name == "worktrees"
    if not (ours or theirs):
        return
    repo = repository_of(folder) or repository(settings, suggestion["project"])
    if repo:
        _git(repo, "worktree", "remove", "--force", str(folder), check=False)
        if branch.startswith(("alpha/", "letthere/")):
            _git(repo, "branch", "-D", branch, check=False)
        _git(repo, "worktree", "prune", check=False)
    shutil.rmtree(folder, ignore_errors=True)


def remove(connection: sqlite3.Connection, settings: Settings, item_id: str, project: str) -> None:
    """Remove: the worktree and its branch are deleted, and the suggestion can be implemented again."""
    suggestion = _row(connection, item_id, project)
    if suggestion is None:
        return
    _remove_worktree(settings, suggestion)
    _clear(connection, item_id, project, None)


def diff(suggestion: sqlite3.Row) -> tuple[str, str]:
    """The branch against its base: (stat, patch)."""
    folder = Path(suggestion["implement_path"] or "")
    if not suggestion["implement_base"] or not folder.is_dir():
        return "", ""
    span = f"{suggestion['implement_base']}..{suggestion['implement_branch']}"
    return (_git(folder, "diff", "--stat", span, check=False).stdout,
            _git(folder, "diff", "--find-renames", span, check=False).stdout)


def _agent(folder: Path, prompt: str, model: str, minutes: int) -> tuple[str, bool]:
    """(the agent's report, whether it was cut short) from one omp session in the worktree, stopped after
    `minutes`. A run stopped at the limit keeps what it did: its last message stands in for the report, and the
    caller commits the worktree as it is."""
    try:
        report, _ = omp_agent.run(prompt, cwd=folder, model=model, time_limit=minutes * 60)
    except omp_agent.TimedOut as stopped:
        last = stopped.text or "(it had not written anything yet)"
        return (f"## Stopped at the time limit\nThe agent did not finish within {minutes} minutes (implement_minutes "
                "in config.toml). What is on the branch is what it had done by then, committed as it was; its tests may "
                f"not have run, so review with care. Its last message:\n\n{last}"), True
    except omp_agent.AgentError as error:
        raise Failure(f"{error}; unfinished changes are left uncommitted in {folder}.") from None
    return report, False


def _commit(folder: Path, base: str, headline: str, item: sqlite3.Row) -> int:
    """Commit everything the agent changed, as the user (their identity and hooks; never --no-verify), with a plain
    message and no trailers. Returns how many files the branch changes (0: nothing changed, no commit)."""
    _git(folder, "add", "-A")
    if _git(folder, "diff", "--cached", "--quiet", check=False).returncode != 0:
        title = item["title"] or item["title_source"] or "an item"
        _git(folder, "commit", "-q", "-m", f"Alpha: {headline}",
             "-m", f"Suggested by Alpha from {title}\n{item['url']}")
    return len(_git(folder, "diff", "--name-only", f"{base}..HEAD").stdout.splitlines())


def run(connection: sqlite3.Connection, settings: Settings, item_id: str, project: str) -> None:
    """Carry out the suggestion (the `implement` command, started by the dashboard's Confirm)."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # one at a time: a second run waits here, shown as queued
        suggestion = _row(connection, item_id, project)
        if suggestion is None:
            raise SystemExit(f"Implement: no suggestion for {item_id} in {project}")
        _remove_worktree(settings, suggestion)  # an earlier failed run's worktree and branch
        _clear(connection, item_id, project, "implementing")
        try:
            chosen = plan(connection, settings, item_id, project)
            if chosen.repository is None:
                raise Failure(f"{project} has no local repository; use Implement again to write a task file instead.")
            base = _git(chosen.repository, "rev-parse", "HEAD").stdout.strip()
            WORKTREES.mkdir(parents=True, exist_ok=True)
            _git(chosen.repository, "worktree", "add", "-q", "-b", chosen.branch, str(chosen.worktree), base)
            _set(connection, item_id, project, branch=chosen.branch, base=base, path=str(chosen.worktree))
            print(f"Implement: {project}: {chosen.headline} on {chosen.branch} in {chosen.worktree}", flush=True)
            prompt = AGENT_PROMPT.format(**_context(connection, chosen))
            report, cut_short = _agent(chosen.worktree, prompt, settings.agent_model, settings.implement_minutes)
            _set(connection, item_id, project, report=report)
            files = _commit(chosen.worktree, base, chosen.headline, chosen.item)
            if cut_short and not files:
                raise Failure(f"The agent did not finish within {settings.implement_minutes} minutes and had changed "
                              "nothing yet.")
            _set(connection, item_id, project, status="implemented", files=files, finished_at=now())
            print(f"Implement: {'stopped at the time limit' if cut_short else 'done'}, {files} file"
                  f"{'' if files == 1 else 's'} changed on {chosen.branch}", flush=True)
        except Exception as error:  # a crashed run must not stay "implementing"
            reason = str(error) if isinstance(error, Failure) else f"{type(error).__name__}: {error}"
            if not isinstance(error, Failure):
                print(traceback.format_exc(), flush=True)
            _set(connection, item_id, project, status="failed", error=reason[:4000], finished_at=now())
            print(f"Implement: failed: {reason}", flush=True)
