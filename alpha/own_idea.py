"""The user's own idea for an item: the dashboard's Have an idea card on an item's page.

Alpha writes suggestions (what to do with an item in a project) only for the pairs its matching and judging reach.
When the user sees a use the system did not, they name the project and write the idea in their words; Claude
(`claude_model`) shapes it with them in a chat (writer.plan_idea): every answer carries the current plan (headline,
outcome, how, effort) and asks what would change the how. When they are happy, Make it a suggestion stores the plan
as the (item, project) suggestion, or Implement stores it and opens Implement's readback. Either way it is stored
rated α with basis_kind 'user' and the user's own messages as its basis, and from there it is a suggestion like any
other: Ask about this, Implement, Mark done, and agents read it over MCP. It is the strongest signal of the
applications the user wants: taste.py lists these under their own heading in the next taste profile, and Implement's
agent is given the user's words and how the idea was shaped.

One chat per item; Start over clears it (the suggestion it made stays).
"""

import json
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
    """The projects an idea can name: those with a spec or a focus brief, so Claude has something to shape against."""
    return sorted(project_texts(connection))


def get(connection: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM idea_chats WHERE item_id = ?", (item_id,)).fetchone()


def plan_of(chat: sqlite3.Row | None) -> writer.IdeaPlan | None:
    return writer.IdeaPlan.model_validate_json(chat["plan"]) if chat else None


def messages_of(chat: sqlite3.Row | None) -> list[dict]:
    return json.loads(chat["messages"]) if chat else []


def transcript(messages: list[dict]) -> str:
    """The conversation as plain text: the user's messages and Claude's replies (not the plans)."""
    return "\n\n".join(
        f"The user said: {m['content']}" if m["role"] == "user"
        else f"Alpha: {writer.IdeaPlan.model_validate_json(m['content']).reply}"
        for m in messages
    )


def own_words(messages: list[dict]) -> str:
    """Everything the user wrote in the chat, in order: the suggestion's basis."""
    return "\n".join(m["content"] for m in messages if m["role"] == "user")


def send(connection: sqlite3.Connection, settings: Settings, item_id: str, project: str, text: str) -> None:
    """Add the user's message (their idea, with the project, when the chat is new) and Claude's answer with the
    updated plan. A new chat refuses an empty idea, a project Alpha has no spec or brief for, and a project that
    already has a live suggestion for the item (rate or ask about that one)."""
    text = " ".join(text.split())
    chat = get(connection, item_id)
    messages = messages_of(chat)
    if chat:
        project = chat["project"]
        if not text:
            return
    else:
        if len(text) < MIN_CHARACTERS:
            raise ValueError("say what you would do with it")
        if project not in project_texts(connection):
            raise ValueError(f"no project named {project} with a spec or a brief")
        existing = connection.execute("SELECT status FROM suggestions WHERE item_id = ? AND project = ?",
                                      (item_id, project)).fetchone()
        if existing and existing["status"] != "dismissed":
            raise ValueError(f"{project} already has a suggestion for this item: rate or ask about that one")
    item = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise ValueError(f"no item {item_id}")
    messages.append({"role": "user", "content": text})
    plan = writer.plan_idea(settings.claude_model, item, project,
                            project_texts(connection).get(project, "(no description)")[:PROJECT_CHARACTERS], messages)
    messages.append({"role": "assistant", "content": plan.model_dump_json()})
    connection.execute(
        "INSERT INTO idea_chats (item_id, project, messages, plan, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET messages = excluded.messages, plan = excluded.plan, "
        "updated_at = excluded.updated_at",
        (item_id, project, json.dumps(messages, ensure_ascii=False), plan.model_dump_json(), now()),
    )
    connection.commit()


def keep(connection: sqlite3.Connection, settings: Settings, item_id: str) -> str:
    """Store the chat's current plan as the item's suggestion for its project, rated α, basis the user's own words;
    returns the project. A pair Alpha skipped (dismissed) is replaced; one with a live suggestion is left alone
    (ValueError) unless this chat made it, which updates it with the latest plan."""
    chat = get(connection, item_id)
    plan = plan_of(chat)
    if chat is None or plan is None:
        raise ValueError("no idea to keep yet")
    project = chat["project"]
    existing = connection.execute("SELECT status FROM suggestions WHERE item_id = ? AND project = ?",
                                  (item_id, project)).fetchone()
    if existing and existing["status"] != "dismissed" and not chat["suggested_at"]:
        raise ValueError(f"{project} already has a suggestion for this item")
    written = now()
    connection.execute(
        "INSERT INTO suggestions (item_id, project, action, headline, outcome, effort, basis, basis_kind, model, "
        "status, created_at, rating, rated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 'alpha', ?) "
        "ON CONFLICT(item_id, project) DO UPDATE SET action = excluded.action, headline = excluded.headline, "
        "outcome = excluded.outcome, effort = excluded.effort, basis = excluded.basis, basis_kind = excluded.basis_kind, "
        "model = excluded.model, status = excluded.status, created_at = excluded.created_at, rating = excluded.rating, "
        "reason = NULL, rated_at = excluded.rated_at",
        (item_id, project, plan.action, plan.headline, plan.outcome, plan.effort, own_words(messages_of(chat)),
         BASIS_KIND, settings.claude_model, written, written),
    )
    connection.execute("UPDATE idea_chats SET suggested_at = ? WHERE item_id = ?", (written, item_id))
    set_metadata(connection, RATED_AT, written)
    connection.commit()
    return project


def reset(connection: sqlite3.Connection, item_id: str) -> None:
    connection.execute("DELETE FROM idea_chats WHERE item_id = ?", (item_id,))
    connection.commit()
