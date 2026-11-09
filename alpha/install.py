"""Machine setup: `alpha setup` (check tools, create .env and config.toml, say what is missing), and `alpha install` /
`alpha uninstall` (the two launchd jobs for this folder, macOS)."""

import getpass
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import SECRET_NAMES, TOOL_ROOT, load_settings
from .sources import SOURCES, missing, options

TOOLS = {
    "uv": ("runs Alpha", "curl -LsSf https://astral.sh/uv/install.sh | sh"),
    "git": ("context feed git (commits)", "xcode-select --install"),
    "lightpanda": ("source x (headless browser)", "brew install lightpanda-io/browser/lightpanda"),
    "gh": ("[project_github] specs read from GitHub", "brew install gh && gh auth login"),
    "claude": ("skill test runs (Claude Code)", "npm install -g @anthropic-ai/claude-code"),
    "omp": ("the Have an idea chat and Implement (omp agents with your MCP servers and skills)", "https://omp.sh"),
}
CONTEXT_FEEDS = {
    "agents": [Path.home() / ".omp" / "agent" / "history.db", Path.home() / ".claude" / "projects"],
    "git": [],
    "superwhisper": [Path.home() / "Documents" / "superwhisper" / "recordings"],
    "anarlog": [Path.home() / "Library" / "Application Support" / "hyprnote" / "app.db"],
}
RUN_INTERVAL_SECONDS = 6 * 3600  # each source keeps its own cadence (`every_hours`); this only wakes the run
DASHBOARD_PORT = 3460


def setup() -> None:
    print(f"Alpha in {TOOL_ROOT}\n\nTools:")
    for tool, (purpose, how) in TOOLS.items():
        found = shutil.which(tool)
        print(f"  {tool:<11} {'ok' if found else 'missing'}  ({purpose})" + ("" if found else f"\n{'':15}install: {how}"))
    for name in (".env", "config.toml"):
        target = TOOL_ROOT / name
        template = TOOL_ROOT / (".env.example" if name == ".env" else "config.example.toml")
        if target.exists():
            print(f"\n{name}: exists")
        else:
            shutil.copyfile(template, target)
            target.chmod(0o600 if name == ".env" else 0o644)
            print(f"\n{name}: created from {template.name}; fill it in")
    settings = load_settings()
    keys = {service: [name for name in names if not os.environ.get(name)] for service, names in SECRET_NAMES.items()}
    print("\nProcessing (every install):")
    print(f"  Claude: {'ready' if not keys['claude'] else 'missing ' + ', '.join(keys['claude']) + ' in .env'}"
          f" ({os.environ.get('ALPHA_CLAUDE_BASE_URL') or 'api.anthropic.com'})")
    print(f"  Jev: {'ready' if not keys['jev'] else 'missing ' + ', '.join(keys['jev']) + ' in .env'}")
    print("\nSources ([sources.<name>] in config.toml):")
    for source in SOURCES.values():
        enabled = options(settings, source)["enabled"]
        lacking = missing(source)
        state = ("ready" if not lacking else "missing " + ", ".join(lacking)) if enabled else "off (enabled = false)"
        print(f"  {source.NAME} ({source.TRUST}): {state}")
    print("\nContext feeds ([context.<name>]):")
    context = settings.context
    for feed, paths in CONTEXT_FEEDS.items():
        enabled = getattr(context, feed)
        present = all(path.exists() for path in paths) if feed != "agents" else any(path.exists() for path in paths)
        state = ("ready" if present else "nothing found at " + ", ".join(str(p) for p in paths)) if enabled else "off"
        print(f"  {feed}: {state}")
    print(f"  projects_directory: {context.projects_directory or 'unset (only configured projects are active)'}")
    print("\nOutputs:")
    notes = settings.project_notes_directory
    print(f"  project specs: {len(settings.project_repositories) + len(settings.project_github)} repositories"
          f"{f', notes in {notes}' if notes else ''}")
    print(f"  vault pages: {settings.output_directory or 'off (output_directory unset)'}")
    print(f"  skills: {settings.skills_directory}" + ("" if shutil.which("claude") else " (test runs need `claude`)"))
    print(f"  backups: {settings.backup_directory or 'off (backup_directory unset)'}")
    print("\nNext: fill in .env and config.toml, then `uv run alpha run`, then `uv run alpha install`.")


def _label() -> str:
    return f"com.{getpass.getuser()}.alpha"


def _plists() -> dict[str, dict]:
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("Install: uv is not on PATH")
    path = list(dict.fromkeys([
        str(Path(uv).parent), "/opt/homebrew/bin", "/usr/local/bin",
        *(str(Path(found).parent) for tool in TOOLS if (found := shutil.which(tool))),
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]))
    logs = TOOL_ROOT / "var" / "logs"

    def job(name: str, command: list[str], extra: dict) -> dict:
        return {
            "Label": f"{_label()}.{name}",
            "ProgramArguments": [uv, "run", "alpha", *command],
            "EnvironmentVariables": {"PATH": ":".join(path)},
            "WorkingDirectory": str(TOOL_ROOT),
            "StandardOutPath": str(logs / f"{name}.log"),
            "StandardErrorPath": str(logs / f"{name}.log"),
            "RunAtLoad": True,
        } | extra

    return {
        "run": job("run", ["run"], {"StartInterval": RUN_INTERVAL_SECONDS}),
        "dashboard": job("dashboard", ["serve", "--port", str(DASHBOARD_PORT)], {"KeepAlive": True}),
    }


def _launchctl(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *arguments], capture_output=True, text=True, check=False)


def start_detached(command: str, *arguments: str) -> None:
    """Run a CLI command (`alpha skills ...`, `alpha implement ...`, `alpha process`) detached, so a build, an
    implementation, or the processing of a new item outlives the request that started it; output goes to
    var/logs/<command>.log."""
    log_path = TOOL_ROOT / "var" / "logs" / f"{command}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log:
        subprocess.Popen(
            [str(Path(sys.executable).parent / "alpha"), command, *arguments],
            cwd=TOOL_ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )


def _unload(service: str) -> None:
    """Unload a job and wait until launchd has let go of it (bootout returns before it has)."""
    _launchctl("bootout", service)
    for _ in range(50):
        if _launchctl("print", service).returncode:
            return
        time.sleep(0.2)


def install() -> None:
    """Write and load the run and dashboard jobs. A run starts at once (RunAtLoad); each source's cadence
    decides whether it is read."""
    (TOOL_ROOT / "var" / "logs").mkdir(parents=True, exist_ok=True)
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    for plist in _plists().values():
        path = agents / f"{plist['Label']}.plist"
        _unload(f"{domain}/{plist['Label']}")
        with path.open("wb") as handle:
            plistlib.dump(plist, handle)
        result = _launchctl("bootstrap", domain, str(path))
        if result.returncode:
            raise SystemExit(f"Install: launchctl bootstrap {path} failed: {result.stderr.strip()}")
        print(f"Install: {plist['Label']} loaded ({path})")
    print(f"Dashboard: http://localhost:{DASHBOARD_PORT} · logs in {TOOL_ROOT / 'var' / 'logs'}")


def uninstall() -> None:
    agents = Path.home() / "Library" / "LaunchAgents"
    for name in ("run", "dashboard"):
        label = f"{_label()}.{name}"
        _unload(f"gui/{os.getuid()}/{label}")
        (agents / f"{label}.plist").unlink(missing_ok=True)
        print(f"Uninstall: {label} removed")
