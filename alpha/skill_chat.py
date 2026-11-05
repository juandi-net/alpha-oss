"""Plan a skill from one item in a conversation, then build it.

On an item's page the user presses Make a skill. Claude (`claude_model`) answers with a plan
(name, description, the user's brief, other items to include, a test target) and asks what it
needs to know; every answer updates the plan. Build turns the plan into an active topic whose
members are the opened item and the chosen ones, then the usual approve or build runs
(skills.py). The brief goes into the prompt that writes the skill. Building again from the same
chat updates that topic and builds a new version. Only items of a skill kind (no datasets, models, or Spaces) take
part.

An item the user picks (the opened one, or one added to a skill) is a member as itself, even
when it repeats an earlier idea: its own thread and images are what the user chose.
"""

import json
import sqlite3

from . import writer
from .config import Settings
from .skills import SKILL_KIND, canonical, library_skills, slug
from .storage import SKILL_KINDS, content_of, now

FIRST_MESSAGE = "Make a skill from this post."
# Per item, so one long article cannot crowd the catalogue out of the chat prompt.
POST_ITEM_CHARACTERS = 8000


def get(connection: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM skill_chats WHERE item_id = ?", (item_id,)).fetchone()


def plan_of(chat: sqlite3.Row | None) -> writer.SkillPlan | None:
    return writer.SkillPlan.model_validate_json(chat["plan"]) if chat and chat["plan"] else None


def send(connection: sqlite3.Connection, settings: Settings, item_id: str, text: str) -> None:
    """Add the user's message (the opening request when empty and the chat is new) and Claude's answer."""
    item = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item["kind"] not in SKILL_KINDS:
        raise ValueError(f"a {item['kind']} does not feed skills")
    if item["tier"] == "idea":
        raise ValueError("an idea feeds skills once you rate it α")
    chat = get(connection, item_id)
    messages = json.loads(chat["messages"]) if chat else []
    if not text and messages:
        return
    messages.append({"role": "user", "content": text or FIRST_MESSAGE})
    post = (
        f"Title: {item['title']}\nSummary: {item['summary']}\n\n"
        f"{writer.item_text(item, content_of(connection, item_id), POST_ITEM_CHARACTERS)}"
    )
    catalogue = "\n".join(
        f"{row['id']} | {row['category']} | {row['title']} | {row['summary']}"
        for row in connection.execute(
            f"SELECT * FROM items i WHERE classified_at IS NOT NULL AND duplicate_of IS NULL AND {SKILL_KIND} "
            "AND id != ? ORDER BY created_at",
            (canonical(connection, item_id),),
        )
    )
    active = connection.execute("SELECT name, description FROM topics WHERE status = 'active'").fetchall()
    existing = "\n".join([*(f"- {row['name']}: {row['description']}" for row in active), library_skills(settings)]).strip()
    plan = writer.plan_skill(settings.claude_model, post, catalogue, existing, messages)
    messages.append({"role": "assistant", "content": plan.model_dump_json()})
    connection.execute(
        "INSERT INTO skill_chats (item_id, messages, plan, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(item_id) "
        "DO UPDATE SET messages = excluded.messages, plan = excluded.plan, updated_at = excluded.updated_at",
        (item_id, json.dumps(messages, ensure_ascii=False), plan.model_dump_json(), now()),
    )
    connection.commit()


def reset(connection: sqlite3.Connection, item_id: str) -> None:
    connection.execute("DELETE FROM skill_chats WHERE item_id = ?", (item_id,))
    connection.commit()


def _free_name(connection: sqlite3.Connection, settings: Settings, wanted: str) -> str:
    taken = {row["name"] for row in connection.execute("SELECT name FROM topics")}
    if settings.skills_directory.is_dir():
        taken |= {path.name for path in settings.skills_directory.iterdir()}
    name, number = wanted, 2
    while name in taken:
        name, number = f"{wanted[:60]}-{number}", number + 1
    return name


def prepare_build(connection: sqlite3.Connection, settings: Settings, item_id: str, target: str) -> tuple[str, str]:
    """Write the chat's plan into its topic (created on the first build) with the opened item and
    the chosen ones as members. Returns the `skills` action to run (approve or build) and the topic name."""
    chat = get(connection, item_id)
    plan = plan_of(chat)
    if plan is None:
        raise ValueError("no plan to build yet")
    name = chat["topic"]
    if name:
        connection.execute(
            "UPDATE topics SET title = ?, description = ?, brief = ?, test_target = ?, status = 'active' WHERE name = ?",
            (plan.title, plan.description, plan.brief, target, name),
        )
        action = "build"
    else:
        name = _free_name(connection, settings, slug(plan.name) or slug(plan.title) or f"item-{slug(item_id)}")
        connection.execute(
            "INSERT INTO topics (name, title, description, status, test_target, brief, created_at) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (name, plan.title, plan.description, target, plan.brief, now()),
        )
        connection.execute("UPDATE skill_chats SET topic = ? WHERE item_id = ?", (name, item_id))
        action = "approve"
    chosen = {item_id} | {found for p in plan.post_ids if (found := canonical(connection, p.strip()))}
    # An item the user removed from the skill on the Skills tab stays removed (excluded is kept).
    connection.executemany(
        "INSERT INTO topic_scores (topic, item_id, probability) VALUES (?, ?, 1.0) "
        "ON CONFLICT (topic, item_id) DO UPDATE SET probability = 1.0",
        [(name, member) for member in sorted(chosen)],
    )
    connection.commit()
    return action, name


def add_to_topic(connection: sqlite3.Connection, item_id: str, topic: str) -> bool:
    """Make the item a member of an existing active topic, also one removed from it before. A live
    skill gets a new draft on the next run. Datasets never join a skill."""
    if not connection.execute("SELECT 1 FROM topics WHERE name = ? AND status = 'active'", (topic,)).fetchone():
        return False
    if not connection.execute(f"SELECT 1 FROM items i WHERE id = ? AND {SKILL_KIND}", (item_id,)).fetchone():
        return False
    connection.execute(
        "INSERT OR REPLACE INTO topic_scores (topic, item_id, probability) VALUES (?, ?, 1.0)", (topic, item_id)
    )
    connection.commit()
    return True
