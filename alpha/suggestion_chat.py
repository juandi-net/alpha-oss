"""Ask about a suggestion before acting on it: the dashboard's Ask about this, under a suggestion on an item's page and
in a project's list. Claude (`claude_model`) answers each question grounded in the suggestion (headline, outcome,
how), the source item's full text and content, the project's spec and current focus brief, and the conversation so
far. One conversation per suggestion (item and project); Start over clears it. Implement gives it to its agent, and
the user's decisions and constraints in it take precedence over the suggestion.
"""

import json
import sqlite3

from . import focus, writer
from .config import Settings
from .storage import content_of, now

# Per piece of content, and for the whole item, so one long article cannot crowd out the rest of the prompt.
CONTENT_CHARACTERS = 8000
ITEM_CHARACTERS = 40_000
SPEC_CHARACTERS = 6000


def get(connection: sqlite3.Connection, item_id: str, project: str) -> list[dict]:
    """The conversation, oldest first ([] when none)."""
    row = connection.execute("SELECT messages FROM suggestion_chats WHERE item_id = ? AND project = ?",
                             (item_id, project)).fetchone()
    return json.loads(row["messages"]) if row else []


def all_for(connection: sqlite3.Connection, item_ids: list[str]) -> dict[tuple[str, str], list[dict]]:
    """Every conversation about these items' suggestions, by (item, project)."""
    if not item_ids:
        return {}
    return {(row["item_id"], row["project"]): json.loads(row["messages"]) for row in connection.execute(
        f"SELECT * FROM suggestion_chats WHERE item_id IN ({','.join('?' * len(item_ids))})", item_ids)}


def transcript(messages: list[dict]) -> str:
    """The conversation as plain text, for Implement's agent and its task file."""
    return "\n\n".join(f"{'The user asked' if m['role'] == 'user' else 'Answer'}: {m['content']}" for m in messages)


def _context(connection: sqlite3.Connection, suggestion: sqlite3.Row, item: sqlite3.Row) -> str:
    project = suggestion["project"]
    spec = connection.execute("SELECT spec FROM projects WHERE name = ?", (project,)).fetchone()
    brief = focus.latest_briefs(connection).get(project)
    text = writer.item_text(item, content_of(connection, item["id"]), CONTENT_CHARACTERS)[:ITEM_CHARACTERS]
    return (
        f"# The suggestion, for {project}\nHeadline: {suggestion['headline'] or '(none)'}\n"
        f"Outcome: {suggestion['outcome'] or '(none)'}\nHow: {suggestion['action']}\n\n"
        f"# The project: {project}\n{(spec['spec'] if spec else '(no description in Alpha)')[:SPEC_CHARACTERS]}\n\n"
        f"# What the user is working on now (focus brief)\n{brief['brief'] if brief else '(none yet)'}\n\n"
        f"# The source item ({item['kind']}): {item['title'] or item['title_source'] or item['url']}\n{item['url']}\n"
        f"{item['summary'] or ''}\n\n{text}"
    )


def send(connection: sqlite3.Connection, settings: Settings, item_id: str, project: str, question: str) -> None:
    """Add the user's question and Claude's answer."""
    question = question.strip()
    if not question:
        return
    suggestion = connection.execute("SELECT * FROM suggestions WHERE item_id = ? AND project = ?",
                                    (item_id, project)).fetchone()
    item = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if suggestion is None or item is None:
        raise ValueError(f"no suggestion for {item_id} in {project}")
    messages = [*get(connection, item_id, project), {"role": "user", "content": question}]
    answer = writer.answer_suggestion(settings.claude_model, _context(connection, suggestion, item), messages)
    messages.append({"role": "assistant", "content": answer})
    connection.execute(
        "INSERT INTO suggestion_chats (item_id, project, messages, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(item_id, project) DO UPDATE SET messages = excluded.messages, updated_at = excluded.updated_at",
        (item_id, project, json.dumps(messages, ensure_ascii=False), now()),
    )
    connection.commit()


def reset(connection: sqlite3.Connection, item_id: str, project: str) -> None:
    connection.execute("DELETE FROM suggestion_chats WHERE item_id = ? AND project = ?", (item_id, project))
    connection.commit()
