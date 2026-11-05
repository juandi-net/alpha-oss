"""The user's own idea for an item: the dashboard's Your idea, at the foot of an item's Fits your projects panel.

Alpha writes suggestions (what to do with an item in a project) only for the pairs its matching and judging reach.
When the user sees a use the system did not, they name the project and write the idea in their words; Claude
(`claude_model`) writes it up as a suggestion that keeps their intent (writer.suggest_from_idea), and it is stored
rated α with basis_kind 'user' and their words as its basis. From there it is a suggestion like any other: Ask
about this, Implement, Mark done, and agents read it over MCP. It is the strongest signal of the applications the
user wants: taste.py lists these under their own heading in the next taste profile, and Implement's agent is given
the user's words.
"""

import sqlite3

from . import writer
from .config import Settings
from .pipeline import project_texts
from .storage import now, set_metadata
from .taste import RATED_AT

BASIS_KIND = "user"
MIN_CHARACTERS = 3
PROJECT_CHARACTERS = 4000


def projects(connection: sqlite3.Connection) -> list[str]:
    """The projects an idea can name: those with a spec or a focus brief, so Claude has something to write against."""
    return sorted(project_texts(connection))


def add(connection: sqlite3.Connection, settings: Settings, item_id: str, project: str, idea: str) -> None:
    """Store the user's idea for the item in the project as an α-rated suggestion. A pair Alpha skipped (dismissed)
    is replaced; a pair with a live suggestion is left alone (ValueError), as is an empty idea or a project Alpha has
    no spec or brief for."""
    idea = " ".join(idea.split())
    if len(idea) < MIN_CHARACTERS:
        raise ValueError("say what you would do with it")
    texts = project_texts(connection)
    if project not in texts:
        raise ValueError(f"no project named {project} with a spec or a brief")
    item = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise ValueError(f"no item {item_id}")
    existing = connection.execute("SELECT status FROM suggestions WHERE item_id = ? AND project = ?",
                                  (item_id, project)).fetchone()
    if existing and existing["status"] != "dismissed":
        raise ValueError(f"{project} already has a suggestion for this item")
    suggestion = writer.suggest_from_idea(settings.claude_model, item, project, texts[project][:PROJECT_CHARACTERS],
                                          idea)
    written = now()
    connection.execute(
        "INSERT INTO suggestions (item_id, project, action, headline, outcome, effort, basis, basis_kind, model, "
        "status, created_at, rating, rated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 'alpha', ?) "
        "ON CONFLICT(item_id, project) DO UPDATE SET action = excluded.action, headline = excluded.headline, "
        "outcome = excluded.outcome, effort = excluded.effort, basis = excluded.basis, basis_kind = excluded.basis_kind, "
        "model = excluded.model, status = excluded.status, created_at = excluded.created_at, rating = excluded.rating, "
        "reason = NULL, rated_at = excluded.rated_at",
        (item_id, project, suggestion.action, suggestion.headline, suggestion.outcome, suggestion.effort, idea,
         BASIS_KIND, settings.claude_model, written, written),
    )
    set_metadata(connection, RATED_AT, written)
    connection.commit()
