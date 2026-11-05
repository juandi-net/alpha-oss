"""Active projects and their specs. A vault note takes precedence over a repository of the same name.

When a spec or the relevance guidance changes, the project's stored relevance rows are deleted so
every canonical item is scored again on the next run.
"""

import hashlib
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .storage import now

REPOSITORY_SPEC_FILES = ["README.md", "AGENTS.md", "CLAUDE.md"]
# Keeps each Jev Score question far below the 32k-token per-question limit.
MAX_SPEC_CHARACTERS = 6000
# Repository files shorter than this (e.g. a lone "@AGENTS.md" include) say nothing about the project.
MIN_REPOSITORY_SPEC_CHARACTERS = 300


@dataclass(frozen=True)
class Project:
    name: str
    source: str
    spec: str


def _repository_spec(read: Callable[[str], str | None]) -> str:
    parts = []
    for filename in REPOSITORY_SPEC_FILES:
        text = read(filename)
        if text is not None:
            parts.append(f"# {filename}\n\n{text.strip()}")
    return "\n\n".join(parts)


def _local_file(directory: Path) -> Callable[[str], str | None]:
    def read(filename: str) -> str | None:
        path = directory / filename
        return path.read_text(errors="replace") if path.is_file() else None

    return read


def _github_file(repository: str) -> Callable[[str], str | None]:
    """Reads through the `gh` CLI. A missing file is skipped; any other failure stops the run,
    because dropping the project would delete its stored scores."""

    def read(filename: str) -> str | None:
        result = subprocess.run(
            ["gh", "api", f"repos/{repository}/contents/{filename}", "-H", "Accept: application/vnd.github.raw"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout
        if "HTTP 404" in result.stderr:
            return None
        raise SystemExit(f"Projects: could not read {filename} from github.com/{repository}: {result.stderr.strip()}")

    return read


def discover_projects(settings: Settings) -> list[Project]:
    projects: dict[str, Project] = {}
    notes = settings.project_notes_directory
    if notes and notes.is_dir():
        for path in sorted(settings.project_notes_directory.glob("*.md")):
            spec = path.read_text(errors="replace").strip()
            if spec:
                projects[path.stem] = Project(path.stem, str(path), spec[:MAX_SPEC_CHARACTERS])
    sources = [(name, str(directory), _local_file(directory)) for name, directory in settings.project_repositories.items()]
    sources += [
        (name, f"github.com/{repository}", _github_file(repository))
        for name, repository in settings.project_github.items()
    ]
    for name, source, read in sources:
        if name in projects:
            continue
        spec = _repository_spec(read)
        if len(spec) >= MIN_REPOSITORY_SPEC_CHARACTERS:
            projects[name] = Project(name, source, spec[:MAX_SPEC_CHARACTERS])
        else:
            instead = f"write {notes / (name + '.md')} instead" if notes else "add a README.md, AGENTS.md, or CLAUDE.md"
            print(f"Projects: skipped {name}, {source} has no usable {', '.join(REPOSITORY_SPEC_FILES)}; {instead}")
    return sorted(projects.values(), key=lambda project: project.name)


def synchronize_projects(connection: sqlite3.Connection, settings: Settings) -> list[Project]:
    projects = discover_projects(settings)
    stored = {row["name"]: row["spec_hash"] for row in connection.execute("SELECT name, spec_hash FROM projects")}
    current = {project.name for project in projects}
    for name in stored.keys() - current:
        connection.execute("DELETE FROM projects WHERE name = ?", (name,))
        print(f"Projects: removed {name}")
    for project in projects:
        spec_hash = hashlib.sha256(f"{settings.relevance_guidance}\n\n{project.spec}".encode()).hexdigest()
        if stored.get(project.name) == spec_hash:
            continue
        if project.name in stored:
            connection.execute("DELETE FROM relevance WHERE project = ?", (project.name,))
            print(f"Projects: {project.name} spec or relevance guidance changed, all items will be scored again")
        else:
            print(f"Projects: added {project.name}, all items will be scored against it")
        connection.execute(
            "INSERT INTO projects (name, source, spec, spec_hash, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET source = excluded.source, spec = excluded.spec, "
            "spec_hash = excluded.spec_hash, updated_at = excluded.updated_at",
            (project.name, project.source, project.spec, spec_hash, now()),
        )
    connection.commit()
    return projects
