"""Headless omp agents (`omp -p`): the Have an idea chat (own_idea.py) and Implement (implement.py) run as omp
sessions, so they have what the user's own omp has: every MCP server in ~/.omp/agent/mcp.json (Alpha's own search,
Interface Lineage, browsers, ...) and every skill, found by omp itself. Sessions are kept under var/omp-sessions so a
chat can go on with --resume. One call is one turn: the prompt, the final assistant text, and the session id.
"""

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .config import TOOL_ROOT
from .skill_test import OUTPUT_TAIL_CHARACTERS

SESSIONS = TOOL_ROOT / "var" / "omp-sessions"
# Where omp lives when it is not on the launchd PATH (install.py adds its folder once `omp` is found).
KNOWN = (Path.home() / "Library/Application Support/Tern/omp/bin/omp", Path.home() / ".local/bin/omp")
GRACE_SECONDS = 60  # after omp's own --max-time, before the process group is killed
LIMIT_SLACK_SECONDS = 5  # a run that ended this close to its limit without a final message was stopped by it


class AgentError(RuntimeError):
    pass


class TimedOut(AgentError):
    """The run hit its time limit. `text` is the agent's last message so far ('' when it said nothing yet); the
    caller decides what to keep of the work it did."""

    def __init__(self, time_limit: int, text: str) -> None:
        super().__init__(f"The agent did not finish within {time_limit // 60} minutes")
        self.text = text


def binary() -> str:
    found = shutil.which("omp") or shutil.which("omp", path=_environment("omp")["PATH"])
    if found:
        return found
    for path in KNOWN:
        if path.is_file():
            return str(path)
    raise AgentError("omp is not installed (https://omp.sh), so no agent can run")


def resolve_model(omp: str, model: str) -> str:
    """A family name (opus, sonnet, haiku) is the newest model of that family: `omp-latest-model <family>` says
    which (omp's own fuzzy match can pick an older release). Without that tool, or for a full id, `model` as given."""
    if any(character.isdigit() for character in model) or "/" in model:
        return model
    tool = shutil.which("omp-latest-model", path=_environment(omp)["PATH"])
    if not tool:
        return model
    found = subprocess.run([tool, model], capture_output=True, text=True, env=_environment(omp), check=False)
    return found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else model


def _environment(omp: str) -> dict[str, str]:
    """A clean environment, as skill_test.environment: variables inherited from an agent session (Alpha may run
    under one) make a nested agent hang, and Alpha's Anthropic key is left out so omp uses its own login. PATH:
    the usual folders, omp's own, and the current PATH after them (the user's MCP servers live there)."""
    keep = ("HOME", "USER", "LANG", "TMPDIR")
    path = [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
            str(Path(omp).parent), *os.environ.get("PATH", "").split(":")]
    return {key: os.environ[key] for key in keep if key in os.environ} | {
        "PATH": ":".join(dict.fromkeys(p for p in path if p))}


# A conversation's tools: reading, searching, the web, and (through the `write` omp keeps for xd:// devices and
# session scratch only, which refuses real paths) every MCP server. No bash, edit, eval, or task: it cannot change
# a file or run a command. Checked 2026-11-05 on omp 18.16: a write to a real path is refused.
READ_ONLY_TOOLS = "read,grep,glob,web_search"


def run(prompt: str, *, cwd: Path, model: str, time_limit: int, system: str | None = None,
        resume: str | None = None, read_only: bool = False) -> tuple[str, str]:
    """One turn: (the final assistant text, the session id). `system` is appended to omp's own system prompt, so
    its tools stay usable; `resume` continues an earlier session; `read_only` keeps READ_ONLY_TOOLS. Stopped at
    `time_limit` seconds (omp's --max-time; the process group is killed GRACE_SECONDS later)."""
    omp = binary()
    SESSIONS.mkdir(parents=True, exist_ok=True)
    command = [omp, "-p", prompt, "--mode", "json", "--model", resolve_model(omp, model), "--cwd", str(cwd),
               "--session-dir", str(SESSIONS), "--approval-mode", "yolo", "--max-time", str(time_limit)]
    if read_only:
        command += ["--tools", READ_ONLY_TOOLS]
    if system:
        command += ["--append-system-prompt", system]
    if resume:
        command += ["--resume", resume]
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env=_environment(omp), stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        printed, errors = process.communicate(timeout=time_limit + GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        printed, errors = process.communicate()
        raise TimedOut(time_limit, _parse(printed, resume)[0]) from None
    text, session, aborted = _parse(printed, resume)
    if process.returncode != 0 or not text:
        # omp stops the session at --max-time in two shapes: the last message aborted (exit 1), or a tool call
        # cut off ("Command aborted", exit 0) with no final text. Either way the run reached the limit.
        if aborted or time.monotonic() - started >= time_limit - LIMIT_SLACK_SECONDS:
            raise TimedOut(time_limit, text)
        tail = (errors or printed).strip()[-OUTPUT_TAIL_CHARACTERS:]
        raise AgentError(f"The agent stopped (exit {process.returncode}): {tail}")
    return text, session


def _parse(printed: str, resume: str | None) -> tuple[str, str, bool]:
    """(the last assistant text, the session id, whether omp stopped the session at --max-time: the last assistant
    message's stop reason is aborted) from omp's JSON lines."""
    session, text, aborted = resume or "", "", False
    for line in printed.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "session":
            session = event.get("id") or session
        elif event.get("type") == "message_end" and event["message"].get("role") == "assistant":
            message = event["message"]
            aborted = message.get("stopReason") == "aborted"
            text = "".join(block.get("text", "") for block in message.get("content", [])
                           if block.get("type") == "text").strip() or text
    return text, session, aborted
