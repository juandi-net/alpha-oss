"""Test a built skill before anyone uses it: run each shipped script on the test task, then have
a headless Claude Code agent follow the skill in report-only mode and write a report, which
writer.grade_run scores against a fixed rubric.

The test agent runs with a clean environment that uses the same Claude account as the rest of Alpha
(ALPHA_CLAUDE_API_KEY, and ALPHA_CLAUDE_BASE_URL when set, from .env, passed on as ANTHROPIC_API_KEY and
ANTHROPIC_BASE_URL), no MCP servers, and in don't-ask mode: it may only read, browse, search, run the skill's
scripts with `uv run`, clone a repository, and write in its own folder.
(`--bare` is not used: it removes the Write and WebSearch tools.)
"""

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .skill_spec import SkillSpec

SCRIPT_TIMEOUT_SECONDS = 180
AGENT_TIMEOUT_SECONDS = 30 * 60
OUTPUT_TAIL_CHARACTERS = 3000
# Bash only for the skill's scripts and cloning a target repository; reading, writing in the
# working folder, fetching pages, and web search have their own tools.
AGENT_TOOLS = "Read Glob Grep Write Edit WebFetch WebSearch Bash(uv run:*) Bash(git clone:*) Bash(gh repo clone:*)"
AGENT_PROMPT = """You are testing a newly built agent skill before it is published.

1. Read the skill at {skill}/SKILL.md and its scripts/ folder.
2. Follow it in REPORT-ONLY mode on this test task (a URL, a repository, or a scenario): {target}
3. Run each script as one plain command with its absolute path, no cd, pipes, or redirection:
   `uv run {skill}/scripts/<file> <arguments>`. Read its printed output directly; save what you
   need with the Write tool.
4. Do not ask questions. Where the skill asks for inputs you do not have (accounts, paid tools, data
   exports), use its fallbacks and say so.
5. Change nothing outside this working folder: no commits, pushes, form submissions, sign-ups,
   purchases, emails, posts, or paid API calls.
6. Write the report the skill's Output section describes to ./report.md with the Write tool. End it
   with a section "## Skill feedback": steps that were blocked, vague, or wrong, and why.
"""


def environment() -> dict[str, str]:
    """A clean environment: variables inherited from an agent session (this tool may run under one)
    make a nested `claude -p` hang, and scripts need nothing beyond these."""
    keep = ("HOME", "USER", "LANG", "TMPDIR")
    path = [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    base_url = os.environ.get("ALPHA_CLAUDE_BASE_URL")
    return {key: os.environ[key] for key in keep if key in os.environ} | {
        "PATH": ":".join(path),
        "ANTHROPIC_API_KEY": os.environ["ALPHA_CLAUDE_API_KEY"],
    } | ({"ANTHROPIC_BASE_URL": base_url} if base_url else {})


def _tail(text: str) -> str:
    return text[-OUTPUT_TAIL_CHARACTERS:]


def run_scripts(directory: Path, spec: SkillSpec, target: str) -> list[dict]:
    results = []
    for script in spec.scripts:
        # The writer sometimes quotes the placeholder itself ('{target}' or "{target}"); quote it once.
        bare = script.smoke_command.replace("'{target}'", "{target}").replace('"{target}"', "{target}")
        command = bare.replace("{target}", shlex.quote(target))
        started = time.monotonic()
        try:
            completed = subprocess.run(
                shlex.split(command), cwd=directory, capture_output=True, text=True,
                timeout=SCRIPT_TIMEOUT_SECONDS, env=environment(), stdin=subprocess.DEVNULL, check=False,
            )
            code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as error:
            code, stdout, stderr = None, error.stdout or "", f"timed out after {SCRIPT_TIMEOUT_SECONDS} s"
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        results.append({
            "script": script.filename,
            "command": command,
            "exit_code": code,
            "seconds": round(time.monotonic() - started, 1),
            "ok": code == 0 and bool(stdout.strip()),
            "stdout": _tail(stdout),
            "stderr": _tail(stderr),
        })
    return results


def run_agent(directory: Path, target: str, model: str) -> str:
    """Returns the agent's report (or what it printed, if it wrote none)."""
    workspace = directory.parent / f"{directory.name}-test-run"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True)
    command = [
        shutil.which("claude", path=environment()["PATH"]) or "claude",
        "-p", AGENT_PROMPT.format(skill=directory, target=target),
        "--model", model, "--strict-mcp-config", "--setting-sources", "project,local", "--permission-mode", "dontAsk",
        "--allowedTools", AGENT_TOOLS, "--add-dir", str(directory), "--output-format", "json",
    ]
    try:
        completed = subprocess.run(
            command, cwd=workspace, capture_output=True, text=True, timeout=AGENT_TIMEOUT_SECONDS,
            env=environment(), stdin=subprocess.DEVNULL, check=False,
        )
        printed = completed.stdout
    except subprocess.TimeoutExpired:
        printed = json.dumps({"result": f"The test run did not finish within {AGENT_TIMEOUT_SECONDS // 60} minutes."})
    report = workspace / "report.md"
    if report.is_file():
        return report.read_text(errors="replace")
    try:
        return json.loads(printed).get("result") or "The test agent wrote no report."
    except json.JSONDecodeError:
        return f"The test agent wrote no report. Output:\n{_tail(printed)}"
