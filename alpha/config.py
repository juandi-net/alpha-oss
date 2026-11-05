"""Configuration from config.toml (personal, git-ignored; config.example.toml is the template) and secrets
from .env (also git-ignored; .env.example lists every name)."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

TOOL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = TOOL_ROOT / "config.toml"

# Secrets every install needs for processing. A source declares its own (alpha/sources).
SECRET_NAMES = {
    "jev": ["TYPESAFE_API_KEY"],
    "claude": ["ALPHA_CLAUDE_API_KEY"],
}


@dataclass(frozen=True)
class ContextSettings:
    """The `[context]` table and its feeds: what the user is working on (focus.py). Every feed is off
    unless its table says `enabled = true`."""

    projects_directory: Path | None = None  # repositories here with recent commits or agent prompts are active
    days: int = 14                    # activity window for agent prompts, commits, and dictations
    refresh_days: int = 3             # briefs are rewritten this often, or sooner when activity changes
    agents: bool = False              # [context.agents]: prompts typed to omp and Claude Code
    git: bool = False                 # [context.git]: commit subjects and changed paths
    superwhisper: bool = False        # [context.superwhisper]: dictations
    excluded_dictation_apps: tuple[str, ...] = ("Messages",)
    anarlog: bool = False             # [context.anarlog]: meeting summaries, notes, or transcripts
    meeting_days: int = 30


@dataclass(frozen=True)
class JudgeSettings:
    """The `[judge]` table: the shared judge every discovered candidate goes through (judge.py)."""

    fit_threshold: float = 0.4        # stage 1: candidates whose title and preview fit no brief this well are dropped
    model: str = "haiku"              # stage 2: the Claude judge beside Jev
    novelty: float = 0.6              # a find must be at least this likely not to repeat an item or skill


@dataclass(frozen=True)
class Settings:
    database_path: Path
    project_notes_directory: Path | None
    output_directory: Path | None     # vault pages; None turns them off
    project_repositories: dict[str, Path]
    project_github: dict[str, str]
    categories: dict[str, str]
    claude_model: str
    summary_model: str
    skill_model: str
    agent_model: str
    implement_minutes: int
    jev_model: str
    relevance_threshold: float
    relevance_guidance: str
    duplicate_threshold: float
    skills_directory: Path
    active_skills_directory: Path | None
    topic_threshold: float
    backup_directory: Path | None
    jev_price_per_million_input_tokens: float
    judge: JudgeSettings = field(default_factory=JudgeSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    sources: dict[str, dict] = field(default_factory=dict)  # [sources.<name>] tables, read by alpha.sources.config
    let_there_url: str = ""           # Let There's server (its own tool): its overlay loads on the Feed; empty turns it off
    let_there_implement: bool = True  # a note made on an app starts Implement at once (else it waits as a kept idea)


def resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else TOOL_ROOT / path


def _optional(raw: dict, key: str) -> Path | None:
    return resolve(raw[key]) if raw.get(key) else None


def load_settings(config_path: Path = CONFIG_PATH) -> Settings:
    load_dotenv(TOOL_ROOT / ".env")
    if not config_path.is_file():
        raise SystemExit(f"No {config_path.name}: run `uv run alpha setup` (it copies config.example.toml)")
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    categories = raw["categories"]
    if len(categories) < 2:
        raise ValueError("config.toml [categories] needs at least two entries")
    return Settings(
        database_path=resolve(raw.get("database_path", "var/alpha.db")),
        project_notes_directory=_optional(raw, "project_notes_directory"),
        output_directory=_optional(raw, "output_directory"),
        project_repositories={name: resolve(path) for name, path in raw.get("project_repositories", {}).items()},
        project_github=dict(raw.get("project_github", {})),
        categories=categories,
        claude_model=raw.get("claude_model", "sonnet"),
        summary_model=raw.get("summary_model", "opus"),
        skill_model=raw.get("skill_model", "opus"),
        agent_model=raw.get("agent_model", "opus"),
        implement_minutes=int(raw.get("implement_minutes", 120)),
        jev_model=raw.get("jev_model", "jev-latest"),
        relevance_threshold=float(raw.get("relevance_threshold", 1.5)),
        relevance_guidance=raw.get("relevance_guidance", "").strip(),
        duplicate_threshold=float(raw.get("duplicate_threshold", 0.85)),
        skills_directory=resolve(raw.get("skills_directory", "~/.claude/skills")),
        active_skills_directory=_optional(raw, "active_skills_directory"),
        topic_threshold=float(raw.get("topic_threshold", 0.7)),
        backup_directory=_optional(raw, "backup_directory"),
        jev_price_per_million_input_tokens=float(raw.get("jev_price_per_million_input_tokens", 0.042)),
        judge=_table(JudgeSettings, "judge", raw.get("judge", {})),
        context=_context(raw.get("context", {})),
        sources={name: dict(table) for name, table in raw.get("sources", {}).items()},
        let_there_url=str(raw.get("let_there_url", "")).strip().rstrip("/"),
        let_there_implement=bool(raw.get("let_there_implement", True)),
    )


def _table(kind: type, name: str, values: dict):
    unknown = values.keys() - kind.__dataclass_fields__.keys()
    if unknown:
        raise ValueError(f"config.toml [{name}] has unknown settings: {', '.join(sorted(unknown))}")
    return kind(**values)


def _context(raw: dict) -> ContextSettings:
    feeds = {"agents": {"enabled"}, "git": {"enabled"}, "superwhisper": {"enabled", "excluded_apps"},
             "anarlog": {"enabled", "days"}}
    values = {key: value for key, value in raw.items() if key not in feeds}
    if "projects_directory" in values:
        values["projects_directory"] = resolve(values["projects_directory"]) if values["projects_directory"] else None
    for feed, allowed in feeds.items():
        table = raw.get(feed, {})
        unknown = table.keys() - allowed
        if unknown:
            raise ValueError(f"config.toml [context.{feed}] has unknown settings: {', '.join(sorted(unknown))}")
        values[feed] = bool(table.get("enabled", False))
    if "excluded_apps" in raw.get("superwhisper", {}):
        values["excluded_dictation_apps"] = tuple(raw["superwhisper"]["excluded_apps"])
    if "days" in raw.get("anarlog", {}):
        values["meeting_days"] = int(raw["anarlog"]["days"])
    return _table(ContextSettings, "context", values)


def source_config(settings: Settings, name: str, defaults: dict) -> dict:
    """A source's `[sources.<name>]` table over its defaults (every source has `enabled`, off by default, and
    `every_hours`, its cadence)."""
    values = settings.sources.get(name, {})
    unknown = values.keys() - defaults.keys()
    if unknown:
        raise ValueError(f"config.toml [sources.{name}] has unknown settings: {', '.join(sorted(unknown))}")
    return defaults | values


def missing_secrets(service: str) -> list[str]:
    return [name for name in SECRET_NAMES[service] if not os.environ.get(name)]


def require_secrets(service: str) -> None:
    missing = missing_secrets(service)
    if missing:
        raise SystemExit(f"Missing in {TOOL_ROOT / '.env'}: {', '.join(missing)}")
