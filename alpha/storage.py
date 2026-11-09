"""The warehouse: one SQLite database, the source of truth. Outputs (vault pages, skills, the dashboard) are
rebuilt from it, and agents read only it; they never call a source.

Sources write items through `store_item` and `add_content`; the pipeline (pipeline.py) fills in the processed
fields and keeps the full-text index `items_fts` in sync.
"""

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# What an item is. Outputs depend on it: every kind is matched to projects, but only the skill kinds feed skills;
# a resource (a dataset, a model, a Space) shows as a project resource and never becomes part of a skill.
KINDS = ("post", "article", "dataset", "model", "space", "paper", "repo")
SKILL_KINDS = ("post", "article", "paper", "repo")
RESOURCE_KINDS = tuple(kind for kind in KINDS if kind not in SKILL_KINDS)

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,             -- an X post's own id; <source>-<native id> for every other source
    source TEXT NOT NULL,            -- the source's name in the registry (alpha/sources)
    kind TEXT NOT NULL,              -- post | article | dataset | model | space | paper | repo
    trust TEXT NOT NULL,             -- curated (the user picked it) | discovered (found by Alpha, admitted by the judge)
    tier TEXT,                       -- discovered only: find (both judges said yes) | idea (one did; judge.py). An idea
                                     -- feeds no skill until the user rates it α, which makes it a find
    url TEXT NOT NULL,
    title_source TEXT,               -- the source's own title, if it has one
    author TEXT,                     -- username, publication, or owner
    created_at TEXT,                 -- when it was published
    found_at TEXT NOT NULL,          -- when Alpha stored it
    rank INTEGER,                    -- feed order, higher = newer (curated: the order the user saved them in)
    text TEXT NOT NULL,
    links TEXT NOT NULL DEFAULT '[]',  -- JSON list of {url, title, description}
    raw TEXT NOT NULL DEFAULT '{}',  -- the source's own JSON for the item
    title TEXT,                      -- written by Claude
    summary TEXT,                    -- written by Claude
    category TEXT,                   -- chosen by Jev
    category_confidence REAL,
    duplicate_of TEXT REFERENCES items(id),
    duplicate_probability REAL,
    classified_at TEXT,
    indexed_at TEXT                  -- NULL until items_fts holds its current text (pipeline.index_pending)
);

-- Everything an item points at, kept so it never has to be fetched again.
-- text is NULL until the content is processed; an empty string means there was nothing to keep.
CREATE TABLE IF NOT EXISTS content (
    item_id TEXT NOT NULL REFERENCES items(id),
    kind TEXT NOT NULL,              -- image | article | quoted | replied_to | thread | author_replies | link
    key TEXT NOT NULL,               -- media key, post id, conversation id, or URL
    text TEXT,                       -- image transcription, post or thread text, article text
    detail TEXT NOT NULL DEFAULT '{}',  -- JSON: source URL, media type, local file, error
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (item_id, kind, key)
);

-- Full-text search over each item's title, summary, text, and content text (rowid = items.rowid).
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    item_id UNINDEXED, title, summary, text, content, tokenize = 'porter unicode61'
);
-- A change to what is indexed marks the item; the pipeline's next run indexes it again.
CREATE TRIGGER IF NOT EXISTS items_changed AFTER UPDATE OF title, summary, text ON items
BEGIN UPDATE items SET indexed_at = NULL WHERE id = NEW.id; END;
CREATE TRIGGER IF NOT EXISTS content_added AFTER INSERT ON content
BEGIN UPDATE items SET indexed_at = NULL WHERE id = NEW.item_id; END;
CREATE TRIGGER IF NOT EXISTS content_changed AFTER UPDATE OF text ON content
BEGIN UPDATE items SET indexed_at = NULL WHERE id = NEW.item_id; END;

CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    source TEXT NOT NULL,            -- file or directory the spec was read from
    spec TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One row per canonical item and project, for the project's current spec.
CREATE TABLE IF NOT EXISTS relevance (
    item_id TEXT NOT NULL REFERENCES items(id),
    project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    score REAL NOT NULL,             -- Jev Score, 0 to 3
    confidence REAL NOT NULL,
    reason TEXT,                     -- written by Claude when score passes the threshold
    scored_at TEXT NOT NULL,
    PRIMARY KEY (item_id, project)
);

-- Groups of items that teach one process; an active topic is written out as an agent skill.
CREATE TABLE IF NOT EXISTS topics (
    name TEXT PRIMARY KEY,           -- skill folder name, lowercase-with-hyphens
    title TEXT NOT NULL,
    description TEXT NOT NULL,       -- what an agent does with the skill
    status TEXT NOT NULL,            -- proposed | active (the user asked for the skill)
    test_target TEXT,                -- what the test run works on, e.g. a website URL
    brief TEXT,                      -- what the user asked for in the skill chat; given to the skill writer
    created_at TEXT NOT NULL
);

-- Every build of a skill. At most one version per topic is live; a draft waits for the user.
CREATE TABLE IF NOT EXISTS skill_versions (
    topic TEXT NOT NULL REFERENCES topics(name) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,            -- building | draft | failed | live | superseded | rejected
    members TEXT NOT NULL,           -- JSON list of the item ids its steps cite, in Sources order (all offered ones until written)
    model TEXT,                      -- Claude model that wrote the spec
    spec TEXT,                       -- JSON skill spec (see skill_spec.py)
    skill_md TEXT,                   -- SKILL.md rendered from the spec
    checks TEXT NOT NULL DEFAULT '{}',  -- JSON: attempts (validation, unused items, script runs, grade, and for an
                                        -- update the live version's grade and the comparison), posts (offered,
                                        -- unused, added/dropped against live), neighbors, live_test
    report TEXT,                     -- the test run's report
    created_at TEXT NOT NULL,
    published_at TEXT,
    PRIMARY KEY (topic, version)
);

-- One row per topic and canonical item once checked (or any item the user added by hand, at 1.0);
-- members have probability >= the topic threshold and are not excluded. The row of an item the user
-- removed from a skill stays (excluded = 1), so Jev never checks it against the topic again.
CREATE TABLE IF NOT EXISTS topic_scores (
    topic TEXT NOT NULL REFERENCES topics(name) ON DELETE CASCADE,
    item_id TEXT NOT NULL REFERENCES items(id),
    probability REAL NOT NULL,
    excluded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (topic, item_id)
);

-- A conversation planning a skill from one item (the dashboard's Make a skill).
CREATE TABLE IF NOT EXISTS skill_chats (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    messages TEXT NOT NULL,          -- JSON list of {role, content}; an assistant content is a SkillPlan as JSON
    plan TEXT,                       -- the latest SkillPlan as JSON
    topic TEXT REFERENCES topics(name) ON DELETE SET NULL,  -- set once the plan was built
    updated_at TEXT NOT NULL
);

-- A conversation shaping the user's own idea for an item in a project (the dashboard's Have an idea, own_idea.py),
-- one omp session (omp_agent.py), kept after it became a suggestion so Implement can read how it was shaped; Start
-- over deletes it.
CREATE TABLE IF NOT EXISTS idea_chats (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    project TEXT NOT NULL,
    messages TEXT NOT NULL,          -- JSON list of {role, content}; an assistant content is an IdeaPlan as JSON
    plan TEXT NOT NULL,              -- the latest IdeaPlan as JSON
    session TEXT,                    -- the omp session the chat goes on in (NULL: written before turns were omp sessions)
    suggested_at TEXT,               -- when the plan became the (item, project) suggestion
    updated_at TEXT NOT NULL
);

-- What the user is working on, per active project (focus.py). Every version is kept; the newest is current.
-- Claude writes them from local activity; a row by the user is their edit, given to the next refresh.
CREATE TABLE IF NOT EXISTS focus_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    brief TEXT NOT NULL,             -- no personal details or names of people; Jev and query writing read it
    author TEXT NOT NULL,            -- claude | user
    sources TEXT NOT NULL DEFAULT '{}',  -- JSON: activity items used, counted per context feed (never their text)
    activity INTEGER NOT NULL DEFAULT 0, -- activity items it was written from; a refresh compares against it
    created_at TEXT NOT NULL
);

-- Anarlog sessions the user keeps out of the focus briefs.
CREATE TABLE IF NOT EXISTS focus_exclusions (
    session_id TEXT PRIMARY KEY,
    excluded_at TEXT NOT NULL
);

-- Search queries Claude wrote from a brief (judge.py); discovered sources search them.
CREATE TABLE IF NOT EXISTS queries (
    project TEXT NOT NULL,
    query TEXT NOT NULL,
    brief_id INTEGER NOT NULL REFERENCES focus_briefs(id),
    PRIMARY KEY (project, query)
);

-- When each discovered source last searched a query; it searches it again only after its requery_days.
CREATE TABLE IF NOT EXISTS searches (
    source TEXT NOT NULL,
    query TEXT NOT NULL,
    searched_at TEXT NOT NULL,
    found INTEGER NOT NULL DEFAULT 0,   -- candidates new to Alpha in the last search
    PRIMARY KEY (source, query)
);

-- Discovery asked for by an agent (or by hand). The next run (or `alpha discover`) searches every enabled
-- discovered source for the query, judges what it finds, and records how many items it admitted.
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    project TEXT,                    -- optional: judge against this project's focus brief as well
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',  -- open | judging (searched, candidates not all judged yet) | done
    handled_at TEXT,
    result_count INTEGER             -- items admitted from this request
);

-- Everything a discovered source has offered, so nothing is judged twice. A find or an idea is also stored in items
-- under the same id; everything below the bar stays here (judge.py).
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,             -- <source>-<native id>, the item id it gets when admitted
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    subtitle TEXT,
    author TEXT,                     -- publication, channel, or owner
    author_url TEXT,                 -- its address; the track record is kept per author_url
    posted_at TEXT,
    preview TEXT,                    -- what the listing shows (stage 1 reads only this)
    paid INTEGER,                    -- 1: only a public preview can be read; judged on it and marked so
    raw TEXT NOT NULL DEFAULT '{}',  -- the source's listing JSON; becomes the item's raw when admitted
    project TEXT,                    -- whose query surfaced it
    query TEXT,                      -- the search query, or the feed that surfaced it (x_timeline: following | for_you)
    request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL,
    fit REAL,                        -- stage 1: best fit to a focus brief (or the request), 0 to 1
    fit_project TEXT,                -- that project, request:<id>, or taste (fits the user's taste, no project)
    jev_sauce REAL,                  -- stage 2: Jev's probability that it is sauce (full text)
    judge_sauce INTEGER,             -- stage 2: the Claude judge's yes (1) or no (0)
    novelty REAL,                    -- 1 minus the probability it repeats an item or skill
    track_record REAL,               -- share of this author's earlier judged candidates that were finds
    similar_to TEXT,                 -- the item or skill it most resembles
    examples TEXT,                   -- JSON {kept: [item ids], rejected: [item ids]}: the bookmarks and β items shown to
                                     -- the judge as examples (taste-judged sources; NULL before it was kept)
    why TEXT,                        -- the Claude judge's one-sentence reason (for a failed candidate, the error)
    status TEXT NOT NULL,            -- seen | dropped | passed | below | idea | find | failed
    idea_judge TEXT,                 -- status idea: the one judge that said yes (jev | claude)
    seen_at TEXT NOT NULL,
    scored_at TEXT
);

-- What the user keeps and what they call slop, written by Claude from everything they picked (taste.py): X bookmarks,
-- GitHub stars, Hugging Face likes, and their α / β ratings. The judge reads the newest one for feed sources.
CREATE TABLE IF NOT EXISTS taste_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT NOT NULL,
    items INTEGER NOT NULL,          -- picked items it was written from
    sources TEXT,                    -- JSON {source: items}; NULL: written from X bookmarks only (before sources were mixed)
    created_at TEXT NOT NULL
);

-- The user's call on an item in the dashboard: alpha (real edge, keep) or beta (no edge: generic, slop, or already
-- known). taste.py writes both into the next taste profile; the judge shows similar beta items as counter-examples.
CREATE TABLE IF NOT EXISTS ratings (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    rating TEXT NOT NULL,            -- alpha | beta
    reason TEXT,                     -- beta only, optional: slop | not_interested | not_now | knew_it
    rated_at TEXT NOT NULL
);

-- What to do with an item in a project: one concrete action per item and project, written by Claude from the match
-- reason (relevance) or the judge's why (a find or idea, for the project it was found for); pipeline.suggest_pending.
-- Or the user's own idea (own_idea.py): basis_kind user, basis their words, rated α when written.
-- The user rates them α / β like items (taste.py learns from both) and marks them done once they have acted on one.
CREATE TABLE IF NOT EXISTS suggestions (
    item_id TEXT NOT NULL REFERENCES items(id),
    project TEXT NOT NULL,
    action TEXT NOT NULL,            -- how: "In <project>: do X with this, because Y; first step: Z" (1 to 3 sentences)
    headline TEXT,                   -- 6 to 12 plain words: the benefit for the project (the Feed's title for it)
    outcome TEXT,                    -- one plain sentence: the outcome the user can expect
    effort TEXT,                     -- small (under an hour) | medium (about a day) | large (days, new infrastructure)
    basis TEXT NOT NULL,             -- what it was written from: the match reason and/or the judge's why, or the user's idea
    basis_kind TEXT NOT NULL,        -- match | judge | both | user
    model TEXT NOT NULL,             -- Claude model that wrote it
    created_at TEXT NOT NULL,
    rating TEXT,                     -- alpha | beta | NULL
    reason TEXT,                     -- beta only, optional: slop | not_interested | not_now | knew_it
    rated_at TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- open | done | dismissed
    -- Implement (implement.py): a headless agent carries it out on a new branch in a worktree, or, for a project
    -- with no local repository, Alpha writes a task file to hand off.
    implement_status TEXT,           -- queued | implementing | implemented | failed | handoff | NULL (never started)
    implement_branch TEXT,           -- alpha/<slug>, in the project's repository
    implement_base TEXT,             -- the commit the branch started from (the diff's base)
    implement_path TEXT,             -- the worktree folder, or the handoff task file
    implement_report TEXT,           -- the agent's report: what it changed, why, how it checked it, what's left
    implement_error TEXT,            -- failed: why
    implement_files INTEGER,         -- files the commit changed (0: nothing changed, no commit)
    implement_model TEXT,            -- the model the agent ran with (implement.route)
    implement_route TEXT,            -- JSON: how it was chosen (Jev's size, behavior and design odds, any escalation)
    implement_started_at TEXT,
    implement_finished_at TEXT,
    PRIMARY KEY (item_id, project)
);

-- Questions the user asked about a suggestion before acting on it (the dashboard's Ask about this,
-- suggestion_chat.py), answered by Claude; Implement gives the conversation to its agent.
CREATE TABLE IF NOT EXISTS suggestion_chats (
    item_id TEXT NOT NULL,
    project TEXT NOT NULL,
    messages TEXT NOT NULL,          -- JSON list of {role, content}, user first
    updated_at TEXT NOT NULL,
    PRIMARY KEY (item_id, project),
    FOREIGN KEY (item_id, project) REFERENCES suggestions(item_id, project) ON DELETE CASCADE
);
"""

# The database before it was source-agnostic (var/bookmarks.db): `bookmarks` and its post_id foreign keys
# become `items` and item_id, scout_posts becomes the generic `candidates`, scout_queries splits into
# `queries` and per-source `searches`, scout_publications stays with the Substack source, and the X-only
# `authors` table folds into items.author.
MIGRATION = """
PRAGMA legacy_alter_table = ON;
BEGIN;
ALTER TABLE bookmarks RENAME TO old_bookmarks;
ALTER TABLE content RENAME TO old_content;
ALTER TABLE relevance RENAME TO old_relevance;
ALTER TABLE topic_scores RENAME TO old_topic_scores;
ALTER TABLE skill_chats RENAME TO old_skill_chats;
ALTER TABLE scout_posts RENAME TO old_scout_posts;
ALTER TABLE scout_queries RENAME TO old_scout_queries;
ALTER TABLE scout_publications RENAME TO substack_publications;
{schema}
INSERT INTO items (id, source, kind, trust, url, title_source, author, created_at, found_at, rank, text, links, raw,
                   title, summary, category, category_confidence, duplicate_of, duplicate_probability, classified_at)
SELECT b.post_id, b.source,
       CASE b.source WHEN 'x' THEN 'post' ELSE 'article' END,
       CASE b.source WHEN 'x' THEN 'curated' ELSE 'discovered' END,
       COALESCE(b.url, 'https://x.com/i/web/status/' || b.post_id),
       CASE b.source WHEN 'x' THEN json_extract(b.raw_json, '$.article_title') ELSE json_extract(b.raw_json, '$.title') END,
       CASE b.source WHEN 'x' THEN (SELECT username FROM authors a WHERE a.id = json_extract(b.raw_json, '$.author_id'))
                     ELSE (SELECT publication FROM old_scout_posts s WHERE s.post_id = b.post_id) END,
       b.created_at, b.fetched_at, b.saved_rank, b.text, b.links, b.raw_json,
       b.title, b.summary, b.category, b.category_confidence, b.duplicate_of, b.duplicate_probability, b.classified_at
FROM old_bookmarks b;
INSERT INTO content (item_id, kind, key, text, detail, fetched_at)
SELECT post_id, kind, key, text, detail, fetched_at FROM old_content ORDER BY rowid;
INSERT INTO relevance (item_id, project, score, confidence, reason, scored_at)
SELECT post_id, project, score, confidence, reason, scored_at FROM old_relevance;
INSERT INTO topic_scores (topic, item_id, probability, excluded)
SELECT topic, post_id, probability, excluded FROM old_topic_scores;
INSERT INTO skill_chats (item_id, messages, plan, topic, updated_at)
SELECT post_id, messages, plan, topic, updated_at FROM old_skill_chats;
INSERT INTO candidates (id, source, kind, url, title, subtitle, author, author_url, posted_at, preview, paid, raw,
                        project, query, fit, fit_project, jev_sauce, judge_sauce, novelty, track_record, similar_to,
                        why, status, seen_at, scored_at)
SELECT post_id, 'substack', 'article', url, title, subtitle, publication, publication_url, post_date, preview,
       CASE WHEN audience IS NULL THEN NULL WHEN audience = 'everyone' THEN 0 ELSE 1 END,
       json_object('canonical_url', url, 'slug', slug, 'audience', audience),
       project, query, stage1, stage1_project, jev_sauce, judge_sauce, novelty, track_record, similar_to,
       why, status, seen_at, scored_at
FROM old_scout_posts;
INSERT INTO queries (project, query, brief_id) SELECT project, query, brief_id FROM old_scout_queries;
INSERT INTO searches (source, query, searched_at, found)
SELECT 'substack', query, MAX(searched_at), SUM(COALESCE(posts, 0)) FROM old_scout_queries
WHERE searched_at IS NOT NULL GROUP BY query;
INSERT OR REPLACE INTO metadata (key, value) SELECT 'ran:x', value FROM metadata WHERE key = 'last_run';
INSERT OR REPLACE INTO metadata (key, value)
SELECT 'ran:substack', MAX(at) FROM (SELECT MAX(searched_at) AS at FROM old_scout_queries
                                     UNION ALL SELECT MAX(scored_at) FROM old_scout_posts) HAVING MAX(at) IS NOT NULL;
DELETE FROM metadata WHERE key IN ('last_run', 'x_user_id');
DROP TABLE old_skill_chats;
DROP TABLE old_topic_scores;
DROP TABLE old_relevance;
DROP TABLE old_content;
DROP TABLE old_scout_posts;
DROP TABLE old_scout_queries;
DROP TABLE old_bookmarks;
DROP TABLE authors;
COMMIT;
PRAGMA legacy_alter_table = OFF;
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'bookmarks'").fetchone():
        _migrate(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    _add_columns(connection)
    return connection


# Columns added after their table was first created: CREATE TABLE IF NOT EXISTS leaves an existing table alone.
# Each: (type, statement that fills existing rows once the column is added, or None).
ADDED_COLUMNS = {
    "candidates": {"examples": ("TEXT", None), "idea_judge": ("TEXT", None)},
    "items": {"tier": ("TEXT", "UPDATE items SET tier = 'find' WHERE trust = 'discovered'")},
    "taste_profiles": {"sources": ("TEXT", None)},
    "idea_chats": {"session": ("TEXT", None)},
    "suggestions": {"headline": ("TEXT", None), "outcome": ("TEXT", None), "effort": ("TEXT", None),
                    **{name: (kind, None) for name, kind in (
                        ("implement_status", "TEXT"), ("implement_branch", "TEXT"), ("implement_base", "TEXT"),
                        ("implement_path", "TEXT"), ("implement_report", "TEXT"), ("implement_error", "TEXT"),
                        ("implement_files", "INTEGER"), ("implement_started_at", "TEXT"),
                        ("implement_finished_at", "TEXT"), ("implement_model", "TEXT"), ("implement_route", "TEXT"))}},
}


def _add_columns(connection: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        present = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, (kind, fill) in columns.items():
            if name not in present:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                if fill:
                    connection.execute(fill)
    connection.commit()


def _migrate(connection: sqlite3.Connection) -> None:
    """One transaction with foreign keys off (tables are rebuilt); checked before it is used."""
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript(MIGRATION.format(schema=SCHEMA))
    except sqlite3.Error:
        if connection.in_transaction:
            connection.rollback()
        raise
    problems = connection.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise SystemExit(f"Migration: {len(problems)} foreign key problems, e.g. {dict(problems[0])}")
    print("Migration: bookmarks are now items (database schema updated)")


def get_metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO metadata (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    connection.commit()


def file_request(connection: sqlite3.Connection, query: str, project: str | None) -> tuple[int, str | None]:
    """Ask the next run to discover items about `query` (for `project`, or any). Returns the request id and,
    when the same query is already waiting, its status (None for a new request)."""
    query = " ".join(query.split())
    existing = connection.execute(
        "SELECT id, status FROM requests WHERE lower(query) = lower(?) AND project IS ? AND status != 'done'",
        (query, project),
    ).fetchone()
    if existing:
        return existing[0], existing[1]
    with connection:
        cursor = connection.execute("INSERT INTO requests (query, project, created_at) VALUES (?, ?, ?)",
                                    (query, project, now()))
    return cursor.lastrowid, None


ITEM_FIELDS = ("id", "source", "kind", "trust", "url", "title_source", "author", "created_at", "rank", "text",
               "links", "raw")


def store_item(connection: sqlite3.Connection, item: dict) -> bool:
    """Store an item from a source. `item` has id, source, kind, trust, url, and text; optionally
    title_source, author, created_at, rank, links (list of {url, title, description}), and raw (the
    source's JSON, a dict). Returns False when it was already stored: its raw JSON, author, and own
    title are then refreshed (a source may return newer fields), everything else is kept."""
    unknown = item.keys() - set(ITEM_FIELDS)
    if unknown or item.get("kind") not in KINDS or item.get("trust") not in ("curated", "discovered"):
        raise ValueError(f"store_item: bad item {item.get('id')}: unknown fields {sorted(unknown)} or kind/trust")
    values = {field: item.get(field) for field in ITEM_FIELDS}
    values["links"] = json.dumps(item.get("links") or [], ensure_ascii=False)
    values["raw"] = json.dumps(item.get("raw") or {}, ensure_ascii=False)
    if connection.execute("SELECT 1 FROM items WHERE id = ?", (values["id"],)).fetchone():
        connection.execute(
            "UPDATE items SET raw = :raw, author = COALESCE(:author, author), "
            "title_source = COALESCE(:title_source, title_source) WHERE id = :id",
            values,
        )
        return False
    connection.execute(
        f"INSERT INTO items ({', '.join(ITEM_FIELDS)}, found_at) VALUES ({', '.join(':' + f for f in ITEM_FIELDS)}, :found_at)",
        values | {"found_at": now()},
    )
    return True


def add_content(
    connection: sqlite3.Connection, item_id: str, kind: str, key: str, text: str | None, detail: dict | None = None
) -> None:
    """Keep the first copy: content already stored (and possibly already processed) is left alone."""
    connection.execute(
        "INSERT OR IGNORE INTO content (item_id, kind, key, text, detail, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, kind, key, text, json.dumps(detail or {}, ensure_ascii=False), now()),
    )


def content_of(connection: sqlite3.Connection, item_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM content WHERE item_id = ? AND text IS NOT NULL AND text != '' ORDER BY rowid", (item_id,)
    ).fetchall()


def search(connection: sqlite3.Connection, text: str, limit: int = 20) -> list[sqlite3.Row]:
    """Items whose title, summary, text, or content contain every word of `text`, best match first."""
    words = re.findall(r"\w+", text)
    if not words:
        return []
    return connection.execute(
        "SELECT i.* FROM items_fts JOIN items i ON i.rowid = items_fts.rowid WHERE items_fts MATCH ? "
        "ORDER BY items_fts.rank LIMIT ?",
        (" ".join(f'"{word}"' for word in words), limit),
    ).fetchall()
