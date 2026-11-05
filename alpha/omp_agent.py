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
from pathlib import Path

from .config import TOOL_ROOT
from .skill_test import OUTPUT_TAIL_CHARACTERS, environment

SESSIONS = TOOL_ROOT / "var" / "omp-sessions"
# Where omp lives when it is not on the launchd PATH (install.py adds its folder once `omp` is found).
KNOWN = (Path.home() / "Library/Application Support/Tern/omp/bin/omp", Path.home() / ".local/bin/omp")
GRACE_SECONDS = 60  # after omp's own --max-time, before the process group is killed


class AgentError(RuntimeError):
    pass


def binary() -> str:
    found = shutil.which("omp") or shutil.which("omp", path=environment()["PATH"])
    if found:
        return found
    for path in KNOWN:
        if path.is_file():
            return str(path)
    raise AgentError("omp is not installed (https://omp.sh), so no agent can run")


def _environment(omp: str) -> dict[str, str]:
    """skill_test.environment (a clean environment: a nested agent's variables make it hang) without Alpha's
    Anthropic key and base URL, which omp would take over its own login, plus the folders omp and the user's MCP
    servers are found in."""
    env = {key: value for key, value in environment().items() if not key.startswith("ANTHROPIC_")}
    path = [*env["PATH"].split(":"), str(Path(omp).parent), *os.environ.get("PATH", "").split(":")]
    return env | {"PATH": ":".join(dict.fromkeys(p for p in path if p))}


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
    command = [omp, "-p", prompt, "--mode", "json", "--model", model, "--cwd", str(cwd), "--session-dir",
               str(SESSIONS), "--approval-mode", "yolo", "--max-time", str(time_limit)]
    if read_only:
        command += ["--tools", READ_ONLY_TOOLS]
    if system:
        command += ["--append-system-prompt", system]
    if resume:
        command += ["--resume", resume]
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env=_environment(omp), stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        printed, errors = process.communicate(timeout=time_limit + GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate()
        raise AgentError(f"The agent did not finish within {time_limit // 60} minutes") from None
    session, text = resume or "", ""
    for line in printed.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "session":
            session = event.get("id") or session
        elif event.get("type") == "message_end" and event["message"].get("role") == "assistant":
            text = "".join(block.get("text", "") for block in event["message"].get("content", [])
                           if block.get("type") == "text").strip() or text
    if process.returncode != 0 or not text:
        tail = (errors or printed).strip()[-OUTPUT_TAIL_CHARACTERS:]
        raise AgentError(f"The agent stopped (exit {process.returncode}): {tail}")
    return text, session
