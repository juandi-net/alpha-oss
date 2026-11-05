"""Alpha as an MCP server (stdio): `alpha mcp`. Lets any agent chat "check Alpha for X".

Every tool reads the warehouse only, through a read-only SQLite connection; `request` is the one write (a row in
`requests`, which the next `alpha run` searches every enabled discovered source for). Agents never call sources.
Answers are short briefs an agent can use correctly, after Interface Lineage's MCP: every search result says what
the item is, who vouches for it (saved by you / found by Alpha), why it matters, the projects it fits and the
user's α / β rating; `get_item` and `project_context` open with a "Brief for your answer" (claim, why it matters,
a verbatim quote, the evidence, suggest next, and "mark anything you add from outside Alpha as your own").
β-rated items stay out unless asked for and α-rated items rank first. When a search finds nothing, it says so
and suggests `request` instead of returning weak matches.

Register it with an agent harness as a stdio server whose command is
`uv run --project <path to alpha> alpha mcp` (works from any folder).
"""

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import implement
from .config import TOOL_ROOT, Settings, load_settings
from .storage import KINDS, content_of, file_request

SEARCH_LIMIT = 25
RECENT_DAYS = 30
SUMMARY_CHARACTERS = 400
REASON_CHARACTERS = 300
SECTION_CHARACTERS = 8000
ITEM_CHARACTERS = 40_000
# Words that say what the agent wants rather than what it wants it about ("check Alpha for tips on local SEO").
FILLER = set("""a an the of for to in on and or with about how what which who is are was do does did i my me we us our
you your it its this that these those from by at as be can could should would any some anything something there
alpha check find look show me tips ideas idea posts post bookmarks items stuff things related regarding""".split())
CONTENT_LABELS = {"thread": "Thread (the author's follow-up posts)", "author_replies": "Author replies",
                  "quoted": "Quoted post", "replied_to": "Replied to", "article": "Article", "image": "Image text",
                  "link": "Linked page"}
TRUST = {"curated": "saved by you", "discovered": "found by Alpha"}
SUGGESTION_STATUSES = ("open", "done", "dismissed", "all")
# A suggestion's Ask about this conversation (suggestion_chat.py), selected beside the suggestion `s`.
CHAT_COLUMN = ("(SELECT c.messages FROM suggestion_chats c WHERE c.item_id = s.item_id AND c.project = s.project) "
               "AS chat")
RATINGS = {"alpha": "α (you keep it)", "beta": "β (you skipped it)"}
BETA_REASONS = {"slop": "slop", "not_interested": "not interested", "not_now": "not now", "knew_it": "knew it"}
# The item's own words, for the verbatim quote and the evidence (quoted posts, replies to, and linked pages are
# someone else's).
OWN_CONTENT = ("thread", "author_replies", "article")
STEP_WORDS = set("""step steps first then next set run install use add click filter enable disable configure create
build copy paste open export import upload deploy test measure track schedule send""".split())
# First person or a first-hand test verb; with a number in the same sentence, the author's own result.
OWN_RESULT = re.compile(r"\b(i|i'm|i've|we|we've|my|our|tested|measured|benchmarked)\b", re.IGNORECASE)
# The rule that ends every brief, after Interface Lineage's dossier brief
# (e.g. https://interfacelineage.com/grace-window-undo: Claim, Suggest next, then this line).
OWN_LINE = "Mark anything you add from outside Alpha as your own."

INSTRUCTIONS = """Alpha is the user's warehouse of curated items (their X bookmarks, GitHub stars, Hugging Face likes) \
and items Alpha discovered and judged (Substack articles, papers, datasets, models, repos), each summarized and \
matched to the user's projects, plus the agent skills built from them. Use `search` when the user says "check Alpha \
for ..."; `get_item` for the brief \
and full text of a result; `project_context` before working on a project; `request` when Alpha has nothing on a \
topic (discovery then runs on its next scheduled run). Answers are briefs: lean on the claim, quote and evidence \
they give, cite items by url, and mark anything you add from outside Alpha as your own."""

server = MCPServer("alpha", instructions=INSTRUCTIONS)


@contextmanager
def _warehouse() -> Iterator[tuple[Settings, sqlite3.Connection]]:
    """Settings and a read-only connection to the warehouse (read fresh on every call: config edits apply)."""
    settings = load_settings()
    if not settings.database_path.is_file():
        raise RuntimeError(f"No Alpha warehouse at {settings.database_path}: run `alpha run` first")
    connection = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield settings, connection
    finally:
        connection.close()


def _clip(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _date(value: str | None) -> str:
    return (value or "")[:10] or "undated"


def _projects(connection: sqlite3.Connection) -> list[str]:
    """Projects items are scored against, and projects with a focus brief."""
    return [row[0] for row in connection.execute(
        "SELECT name FROM projects UNION SELECT project FROM focus_briefs ORDER BY 1")]


def _project(connection: sqlite3.Connection, name: str) -> str | None:
    by_lower = {project.lower(): project for project in _projects(connection)}
    return by_lower.get(name.strip().lower())


def _unknown_project(connection: sqlite3.Connection, name: str) -> str:
    return f"Alpha has no project named '{name}'. Projects: {', '.join(_projects(connection)) or 'none yet'}."


def _matches(connection: sqlite3.Connection, settings: Settings, item_id: str, limit: int = 3) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT project, score, reason FROM relevance WHERE item_id = ? AND score >= ? ORDER BY score DESC LIMIT ?",
        (item_id, settings.relevance_threshold, limit),
    ).fetchall()


def _judge(connection: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT why, fit_project, query, request_id, jev_sauce, judge_sauce, novelty, paid FROM candidates "
        "WHERE id = ?", (item_id,),
    ).fetchone()


def _provenance(item: sqlite3.Row) -> str:
    author = f" by {item['author']}" if item["author"] else ""
    trust = "idea (one judge)" if item["tier"] == "idea" else TRUST.get(item["trust"], item["trust"])
    return f"{item['source']} {item['kind']}{author}, {trust}, {_date(item['created_at'] or item['found_at'])}"


def _title(item: sqlite3.Row) -> str:
    return item["title"] or item["title_source"] or _clip(item["text"], 80) or item["id"]


def _rating(connection: sqlite3.Connection, item_id: str) -> str | None:
    """The user's α / β on the item, in words; None when unrated."""
    row = connection.execute("SELECT rating, reason FROM ratings WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    reason = BETA_REASONS.get(row["reason"] or "", row["reason"])
    return RATINGS.get(row["rating"], row["rating"]) + (f": {reason}" if reason else "")


def _why(connection: sqlite3.Connection, item: sqlite3.Row, project: str | None,
         matches: list[sqlite3.Row]) -> str | None:
    """One line on why the item matters: the project asked about, else the judge's why for a find, else the best
    project match's reason."""
    own = next((row for row in matches if row["project"] == project and row["reason"]), None)
    if own:
        return f"for {own['project']}: {own['reason']}"
    if item["trust"] == "discovered":
        judged = _judge(connection, item["id"])
        if judged and judged["why"]:
            fit = judged["fit_project"] or "no project"
            fit = "your taste profile" if fit == "taste" else fit.replace("request:", "request ")
            return f"the judge, for {fit}: {judged['why']}"
    best = next((row for row in matches if row["reason"]), None)
    return f"for {best['project']}: {best['reason']}" if best else None


def _project_matches(connection: sqlite3.Connection, settings: Settings, item_id: str,
                     project: str | None) -> list[sqlite3.Row]:
    """The item's three best project matches, with `project`'s own match first when it has one."""
    matches = _matches(connection, settings, item_id)
    if project:
        own = connection.execute("SELECT project, score, reason FROM relevance WHERE item_id = ? AND project = ?",
                                 (item_id, project)).fetchone()
        if own:
            matches = [own, *(row for row in matches if row["project"] != project)][:3]
    return matches


def _brief_result(connection: sqlite3.Connection, settings: Settings, item: sqlite3.Row, number: int,
                  project: str | None) -> str:
    """A search result as a compact brief: title, provenance and url, why it matters, fits, rating, id."""
    matches = _project_matches(connection, settings, item["id"], project)
    why = _why(connection, item, project, matches)
    lines = [f"{number}. {_title(item)}", f"   {_provenance(item)} · {item['url']}",
             f"   Why it matters: {_clip(why, REASON_CHARACTERS)}" if why else
             f"   What it says (no project reason yet): {_clip(item['summary'] or item['text'], REASON_CHARACTERS)}"]
    tail = [f"Fits: {', '.join(f'{row['project']} {row['score']:.1f}' for row in matches)}" if matches
            else "Fits no project"]
    rating = _rating(connection, item["id"])
    if rating:
        tail.append(f"Rated {rating}")
    lines.append("   " + " · ".join([*tail, f"id {item['id']}"]))
    return "\n".join(lines)


def _words(query: str) -> list[str]:
    words = re.findall(r"\w+", query.lower())
    meaningful = [word for word in words if word not in FILLER]
    return list(dict.fromkeys(meaningful or words))


@server.tool()
def search(query: str, project: str | None = None, kind: str | None = None, source: str | None = None,
           limit: int = 10, include_skipped: bool = False) -> str:
    """Search the Alpha warehouse (full text of titles, summaries, posts, threads, articles, image text) for items
    about every word of `query`; items the user rated α come first, then best match, with ideas (found by Alpha, only
    one of the two judges said yes) after the rest. Optional filters: `project`
    (only items matched to that project), `kind` (post, article, dataset, model, space, paper, repo), `source` (x,
    substack, ...). Items the user rated β (skipped) are left out unless `include_skipped`. Each result is a short brief:
    title, provenance (saved by you, found by Alpha, or idea (one judge)), url, why it matters, the projects it fits,
    and the user's rating. When nothing matches, Alpha says so: then offer `request` to have Alpha go find it."""
    with _warehouse() as (settings, connection):
        words = _words(query)
        if not words:
            return "Give a few words to search for."
        conditions, values = ["items_fts MATCH ?", "i.duplicate_of IS NULL"], [" ".join(f'"{w}"' for w in words)]
        if project:
            name = _project(connection, project)
            if name is None:
                return _unknown_project(connection, project)
            project = name
            conditions.append("EXISTS (SELECT 1 FROM relevance r WHERE r.item_id = i.id AND r.project = ? "
                              "AND r.score >= ?)")
            values += [project, settings.relevance_threshold]
        if kind:
            if kind not in KINDS:
                return f"Unknown kind '{kind}'. Kinds: {', '.join(KINDS)}."
            conditions.append("i.kind = ?")
            values.append(kind)
        if source:
            conditions.append("i.source = ?")
            values.append(source.lower())
        limit = max(1, min(int(limit), SEARCH_LIMIT))
        joined = ("FROM items_fts JOIN items i ON i.rowid = items_fts.rowid LEFT JOIN ratings ra ON ra.item_id = i.id "
                  f"WHERE {' AND '.join(conditions)}")
        skipped = 0
        if not include_skipped:
            skipped = connection.execute(f"SELECT COUNT(*) {joined} AND ra.rating = 'beta'", values).fetchone()[0]
            joined += " AND (ra.rating IS NULL OR ra.rating != 'beta')"
        # Weights: title, summary, item text, then fetched content (item_id is unindexed). α-rated items first,
        # ideas (one judge) last.
        rows = connection.execute(
            f"SELECT i.* {joined} ORDER BY COALESCE(ra.rating = 'alpha', 0) DESC, COALESCE(i.tier = 'idea', 0), "
            "bm25(items_fts, 0.0, 8.0, 4.0, 2.0, 1.0) LIMIT ?",
            (*values, limit),
        ).fetchall()
        scope = " ".join(filter(None, [f"project {project}" if project else "", f"kind {kind}" if kind else "",
                                       f"source {source}" if source else ""]))
        searched = " ".join(words) + (f" ({scope})" if scope else "")
        hidden = (f" {skipped} item{'s' if skipped != 1 else ''} you rated β (skipped) left out; "
                  "include_skipped=True shows them.") if skipped else ""
        if not rows:
            pending = connection.execute(
                "SELECT id, status FROM requests WHERE lower(query) = lower(?) AND status != 'done' ORDER BY id DESC",
                (query.strip(),),
            ).fetchone()
            if skipped:
                answer = (f"Alpha's only items on '{searched}' are {skipped} you rated β (skipped); "
                          "include_skipped=True shows them.")
            else:
                total = connection.execute("SELECT COUNT(*) FROM items WHERE duplicate_of IS NULL").fetchone()[0]
                answer = f"Alpha has nothing on '{searched}' (no item among {total} contains all of these words)."
                if len(words) > 1:
                    counts = [(word, connection.execute("SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH ?",
                                                        (f'"{word}"',)).fetchone()[0]) for word in words]
                    answer += " Items with each word alone: " + ", ".join(f"{word} {count}" for word, count in counts) + "."
            if pending:
                return answer + f" Request {pending['id']} for it is already {pending['status']}; its finds will show up here."
            return answer + (" Rather than stretch weak matches, ask Alpha to go find it: "
                             f"request(query={query.strip()!r}{f', project={project!r}' if project else ''}).")
        header = (f"{len(rows)} item{'s' if len(rows) != 1 else ''} in Alpha on '{searched}', rated α first, then best "
                  "match, ideas (one judge) last.")
        footer = f"get_item(id) gives an item's brief and full text. Cite items by url. {OWN_LINE}"
        return "\n\n".join([header + hidden, *(_brief_result(connection, settings, row, number, project)
                                               for number, row in enumerate(rows, 1)), footer])


def _links(item: sqlite3.Row) -> list[str]:
    try:
        links = json.loads(item["links"] or "[]")
    except json.JSONDecodeError:
        return []
    return [f"- {link.get('title') or link.get('url')}: {link.get('url')}" for link in links if link.get("url")]


def _skills_with(connection: sqlite3.Connection, item_ids: set[str]) -> list[tuple[sqlite3.Row, list[str]]]:
    """Live skill versions written from any of `item_ids`, with the members among them."""
    found = []
    for row in connection.execute(
        "SELECT v.topic, v.version, v.members, v.published_at, t.title, t.description FROM skill_versions v "
        "JOIN topics t ON t.name = v.topic WHERE v.status = 'live' ORDER BY v.published_at DESC"
    ):
        members = [member for member in json.loads(row["members"] or "[]") if member in item_ids]
        if members:
            found.append((row, members))
    return found


def _skill_line(settings: Settings, row: sqlite3.Row) -> str:
    path = settings.skills_directory / row["topic"] / "SKILL.md"
    where = f" · {path}" if path.is_file() else ""
    return (f"- {row['topic']} (v{row['version']}, live since {_date(row['published_at'])}): {row['title']}. "
            f"{_clip(row['description'], 300)}{where}")


def _sentences(texts: list[str]) -> Iterator[str]:
    """Sentences exactly as written (split on lines and sentence ends, never rejoined), so a quote stays verbatim."""
    for text in texts:
        for line in text.splitlines():
            for sentence in re.split(r"(?<=[.!?])\s+", line.strip()):
                sentence = sentence.strip()
                if 40 <= len(sentence) <= 320 and "http" not in sentence:
                    yield sentence


def _specificity(sentence: str) -> int:
    """How much a sentence names: numbers count double, then named tools (capitalized or mixed-case words past the
    first) and step verbs."""
    score = sentence.count("`")
    for index, token in enumerate(re.findall(r"[\w.%$+-]+", sentence)):
        if any(character.isdigit() for character in token):
            score += 2
        elif index and (token[0].isupper() or any(character.isupper() for character in token[1:])):
            score += 1
        elif token.lower() in STEP_WORDS:
            score += 1
    return score


def _quote(texts: list[str]) -> str | None:
    """The item's most specific sentence (numbers, named tools, steps), verbatim; the first sentence when none is."""
    best, best_score = None, 0
    for sentence in _sentences(texts):
        score = _specificity(sentence)
        if best is None or score > best_score:
            best, best_score = sentence, score
    return best


def _evidence(item: sqlite3.Row, judged: sqlite3.Row | None, texts: list[str], quote: str | None) -> str:
    """What the item rests on: the stage-2 judges' verdict for a find (whose sauce definition asks for the author's
    own results), else Alpha's reading of the text: first-person sentences with numbers are the author's results."""
    own = [sentence for sentence in _sentences(texts) if OWN_RESULT.search(sentence) and re.search(r"\d", sentence)]
    if own:
        example = max(own, key=_specificity)
        shown = "the quote above" if example == quote else f"“{example}”"
        reading = (f"the author reports their own results in {len(own)} sentence{'s' if len(own) != 1 else ''} "
                   f"(e.g. {shown}); anything else it states is the author's claim")
    else:
        reading = "no sentence reports the author's own results, so treat its numbers and outcomes as claims"
    if not (judged and judged["jev_sauce"] is not None):
        source = "you saved it, so no judge read it" if item["trust"] == "curated" else "no judge verdict recorded"
        return f"{source}; Alpha's reading of the text: {reading}."
    asked = ("is it data whose content, labels, license and card fit a concrete need" if item["kind"] == "dataset" else
             "is it a release usable now: open weights or a working app, a usable license, a real card, a concrete "
             "capability" if item["kind"] in ("model", "space") else
             "is it specific (named tools, numbers, steps), backed by the author's own results or data, and not "
             "common knowledge")
    verdict = f"Jev {judged['jev_sauce']:.2f}, Claude {'yes' if judged['judge_sauce'] else 'no'}"
    preview = ", judged on its public preview only (paid)" if judged["paid"] else ""
    caveat = re.search(r"\b(?:though|although|but)\b\s+(.+?)\.?$", judged["why"] or "")
    caveat_text = f"; the judge's caveat: {caveat.group(1)}" if caveat else ""
    return (f"both judges were asked \"{asked}?\" ({verdict}{preview}){caveat_text}. Alpha's reading of the "
            f"text: {reading}.")


def _related(connection: sqlite3.Connection, item: sqlite3.Row, skills: list[tuple[sqlite3.Row, list[str]]],
             limit: int = 3) -> list[str]:
    """Up to `limit` items to read next: members of the same live skill first, then items whose title and summary
    share at least two of this item's title words (items_fts finds them; words in more than a fifth of the items
    say nothing about the topic), leaving out duplicates and β-rated items."""
    picks: list[str] = []
    seen = {item["id"]}
    usable = ("SELECT i.* FROM items i LEFT JOIN ratings ra ON ra.item_id = i.id WHERE i.duplicate_of IS NULL "
              "AND (ra.rating IS NULL OR ra.rating != 'beta') AND i.id = ?")

    def add(row: sqlite3.Row, how: str) -> None:
        seen.add(row["id"])
        picks.append(f"{_title(row)} (get_item('{row['id']}'), {row['url']}; {how})")

    for skill, _ in skills:
        for member in json.loads(skill["members"] or "[]"):
            if len(picks) >= min(2, limit):
                break
            row = connection.execute(usable, (member,)).fetchone() if member not in seen else None
            if row:
                add(row, f"also in the skill {skill['topic']}")
    total = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    topical = []
    for word in _words(_title(item)):
        if len(word) > 2 and not word.isdigit():
            count = connection.execute("SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH ?",
                                       (f'"{word}"',)).fetchone()[0]
            if 1 < count <= total / 5:
                topical.append(word)
    if len(topical) < 2 or len(picks) >= limit:
        return picks
    # Most title words in common first, bm25 order breaking ties; one word in common is a coincidence.
    scored = []
    for order, row in enumerate(connection.execute(
        "SELECT i.* FROM items_fts JOIN items i ON i.rowid = items_fts.rowid LEFT JOIN ratings ra ON "
        "ra.item_id = i.id WHERE items_fts MATCH ? AND i.duplicate_of IS NULL "
        "AND (ra.rating IS NULL OR ra.rating != 'beta') "
        "ORDER BY bm25(items_fts, 0.0, 8.0, 4.0, 2.0, 1.0) LIMIT 40",
        (" OR ".join(f'"{word}"' for word in topical),),
    )):
        if row["id"] not in seen:
            words = set(re.findall(r"\w+", f"{_title(row)} {row['summary'] or ''}".lower()))
            shared = [word for word in topical if word in words]
            if len(shared) > 1:
                scored.append((-len(shared), order, row, shared))
    for *_, row, shared in sorted(scored, key=lambda entry: entry[:2])[: limit - len(picks)]:
        add(row, f"shares {', '.join(shared[:3])}")
    return picks


@server.tool()
def get_item(id: str) -> str:
    """An Alpha item by id (or url) from a search result. It opens with a brief for your answer: the claim, why it
    matters to which project, one verbatim quote from the item's own text, the evidence it rests on, the user's
    rating, and 2 to 3 related items to read next. Then the full item: summary, the post or article text, links,
    project matches with reasons, what Alpha suggests doing with it per project (with the user's questions about a
    suggestion and the last answer, when the user asked), the judge's verdict for discovered items, the fetched content
    (thread, author replies, quoted post, article text, image text, linked pages), and the skills built from it."""
    with _warehouse() as (settings, connection):
        item = connection.execute("SELECT * FROM items WHERE id = ? OR url = ?", (id.strip(), id.strip())).fetchone()
        if item is None:
            return f"Alpha has no item '{id}'. Ids come from search results; `search` finds items by topic."
        content = content_of(connection, item["id"])
        own_texts = [item["text"] or "", *(row["text"] for row in content if row["kind"] in OWN_CONTENT)]
        judged = _judge(connection, item["id"]) if item["trust"] == "discovered" else None
        skills = _skills_with(connection, {item["id"]})
        top = _matches(connection, settings, item["id"])
        why = _why(connection, item, None, top)
        fits = ", ".join(f"{row['project']} {row['score']:.1f}" for row in top)
        quote = _quote(own_texts)
        related = _related(connection, item, skills)
        # After Interface Lineage's "Brief for your answer" (https://interfacelineage.com/grace-window-undo).
        lines = [f"# {_title(item)}", f"{_provenance(item)} · id {item['id']}", item["url"], "",
                 "**Brief for your answer** (Alpha's reading of this item):",
                 f"- Claim: {_clip(item['summary'] or item['text'], SUMMARY_CHARACTERS)}",
                 f"- Why it matters: {why or 'no project match or judge reason recorded'}"
                 + (f" (fits {fits})" if fits else " (fits no project)"),
                 f"- Quote (verbatim, from its own text): “{quote}”" if quote else
                 "- Quote: its own text has no full sentence to quote; see Text below.",
                 f"- Evidence: {_evidence(item, judged, own_texts, quote)}",
                 f"- Your rating: {_rating(connection, item['id']) or 'not rated yet'}",
                 f"- Suggest next: {'; '.join(related)}." if related else
                 "- Suggest next: nothing close in Alpha; request(query=...) has Alpha go find more.",
                 f"- {OWN_LINE}", "", "## The full item"]
        if item["category"]:
            lines.append(f"Category: {item['category']}")
        if item["duplicate_of"]:
            lines.append(f"Repeats an earlier item: {item['duplicate_of']}")
        if item["summary"]:
            lines += ["", f"Summary: {item['summary']}"]
        if item["text"]:
            lines += ["", "Text:", item["text"][:SECTION_CHARACTERS]]
        links = _links(item)
        if links:
            lines += ["", "Links:", *links]
        matches = _matches(connection, settings, item["id"], limit=20)
        if matches:
            lines += ["", "Projects:", *(f"- {row['project']} ({row['score']:.1f}): {row['reason'] or 'no reason written'}"
                                         for row in matches)]
        suggested = connection.execute(
            f"SELECT s.*, i.title, i.title_source, i.text, i.url, {CHAT_COLUMN} FROM suggestions s "
            "JOIN items i ON i.id = s.item_id WHERE s.item_id = ? AND COALESCE(s.rating, '') != 'beta' "
            "AND s.status != 'dismissed' "
            "ORDER BY s.project", (item["duplicate_of"] or item["id"],)).fetchall()
        if suggested:
            lines += ["", "Suggestions (what to do with it, per project):",
                      *(f"- {row['project']}: " + _suggestion_line(row).removeprefix("- ") for row in suggested)]
        if judged:
            asked = f"request {judged['request_id']}" if judged["request_id"] else f"query '{judged['query']}'"
            sauce = f", Jev sauce {judged['jev_sauce']:.2f}" if judged["jev_sauce"] is not None else ""
            novelty = f", novelty {judged['novelty']:.2f}" if judged["novelty"] is not None else ""
            lines += ["", f"Judge (found for {judged['fit_project'] or 'no project'} by {asked}{sauce}{novelty}): "
                          f"{judged['why'] or 'no reason recorded'}"]
        used = len("\n".join(lines))
        for row in content:
            if used >= ITEM_CHARACTERS:
                lines += ["", "(more content stored; cut here to keep the answer short)"]
                break
            label = CONTENT_LABELS.get(row["kind"], row["kind"])
            heading = f"{label}: {row['key']}" if row["kind"] == "link" else label
            text = row["text"][: min(SECTION_CHARACTERS, ITEM_CHARACTERS - used)]
            lines += ["", f"## {heading}", text]
            used += len(text)
        if skills:
            lines += ["", "Skills built from it:", *(_skill_line(settings, row) for row, _ in skills)]
        return "\n".join(lines)


@server.tool()
def project_context(project: str) -> str:
    """What Alpha knows for one project, as a brief for your answer (its focus, what Alpha holds, what to lean on
    and read next), then its current focus brief (what the user is working on), the kept suggestions (actions they
    rated α) and others not rated yet, the top matched items (rated α first, then the strongest recent matches;
    β-rated items left out), and the live skills built from its matched items. Call it before working on a project."""
    with _warehouse() as (settings, connection):
        name = _project(connection, project)
        if name is None:
            return _unknown_project(connection, project)
        brief = connection.execute("SELECT brief, author, created_at FROM focus_briefs WHERE project = ? "
                                   "ORDER BY id DESC LIMIT 1", (name,)).fetchone()
        matched_from = ("FROM relevance r JOIN items i ON i.id = r.item_id LEFT JOIN ratings ra ON ra.item_id = i.id "
                        "WHERE r.project = ? AND r.score >= ? AND i.duplicate_of IS NULL")
        counts = connection.execute(
            f"SELECT COUNT(*), SUM(i.trust = 'curated'), SUM(i.trust = 'discovered'), SUM(ra.rating = 'alpha'), "
            f"SUM(ra.rating = 'beta') {matched_from}", (name, settings.relevance_threshold)).fetchone()
        total, curated, discovered, kept, skipped = (value or 0 for value in counts)
        # α-rated items come first whatever their date; the rest are the strongest recent matches.
        query = (f"SELECT i.* {matched_from} AND (ra.rating IS NULL OR ra.rating != 'beta') {{extra}} "
                 "ORDER BY COALESCE(ra.rating = 'alpha', 0) DESC, r.score DESC, "
                 "COALESCE(i.created_at, i.found_at) DESC LIMIT 8")
        since = (datetime.now(UTC) - timedelta(days=RECENT_DAYS)).isoformat()
        rows = connection.execute(
            query.format(extra="AND (ra.rating = 'alpha' OR COALESCE(i.created_at, i.found_at) >= ?)"),
            (name, settings.relevance_threshold, since)).fetchall()
        label = f"Top items: rated α first, then the strongest matches from the last {RECENT_DAYS} days"
        if not rows:
            rows = connection.execute(query.format(extra=""), (name, settings.relevance_threshold)).fetchall()
            label = f"Top items: no matches in the last {RECENT_DAYS} days, so the strongest overall"
        matched = {row[0] for row in connection.execute(
            "SELECT item_id FROM relevance WHERE project = ? AND score >= ?", (name, settings.relevance_threshold))}
        skills = _skills_with(connection, matched)
        focus = (f"{_clip(brief['brief'].strip().split('\n\n')[0], 360)} (focus brief of "
                 f"{_date(brief['created_at'])})") if brief else "no focus brief yet"
        holds = (f"{total} matched items ({curated} saved by you, {discovered} found by Alpha); you kept {kept} (α) "
                 f"and skipped {skipped} (β); {len(skills)} live skill{'s' if len(skills) != 1 else ''} built from them")
        lean = "; ".join(f"{_title(row)} ({row['url']})" for row in rows[:2])
        if rows:
            next_steps = (f"get_item('{rows[0]['id']}') for the top item's brief; search(query=..., project='{name}') "
                          f"for more; request(query=..., project='{name}') for what Alpha lacks")
        else:
            next_steps = f"request(query=..., project='{name}') to have Alpha go find items for it"
        lines = [f"# {name}", "", f"**Brief for your answer** (what Alpha holds for {name}):", f"- Focus: {focus}",
                 f"- Holds: {holds}", f"- Lean on: {lean}" if lean else "- Lean on: nothing yet; no item is matched to it",
                 f"- Suggest next: {next_steps}.", f"- {OWN_LINE}", ""]
        if brief:
            lines += [f"## Focus brief ({_date(brief['created_at'])}, by {brief['author']})", brief["brief"].strip()]
        kept_suggestions = _suggestion_rows(connection, name, "open", "s.rating = 'alpha'", 10)
        other_suggestions = _suggestion_rows(connection, name, "open", "s.rating IS NULL", 5)
        if kept_suggestions:
            lines += ["", "## Suggestions you kept (rated α, not done yet)", *map(_suggestion_line, kept_suggestions)]
        if other_suggestions:
            lines += ["", "## Other open suggestions (not rated yet)", *map(_suggestion_line, other_suggestions)]
        if rows:
            lines += ["", f"## {label} ({total} matched in all)"]
            lines += [_brief_result(connection, settings, row, number, name) for number, row in enumerate(rows, 1)]
        else:
            lines += ["", "No items are matched to this project yet."]
        lines += ["", "## Live skills built from its items" if skills else "No live skill is built from its items yet.",
                  *(_skill_line(settings, row) + f" ({len(members)} of its items)" for row, members in skills)]
        return "\n".join(lines)


def _suggestion_rows(connection: sqlite3.Connection, project: str | None, status: str, condition: str,
                     limit: int) -> list[sqlite3.Row]:
    clauses, values = [condition], []
    if project:
        clauses.append("s.project = ?")
        values.append(project)
    if status != "all":
        clauses.append("s.status = ?")
        values.append(status)
    return connection.execute(
        f"SELECT s.*, i.title, i.title_source, i.text, i.url, {CHAT_COLUMN} FROM suggestions s "
        f"JOIN items i ON i.id = s.item_id WHERE {' AND '.join(clauses)} "
        "ORDER BY COALESCE(s.rating = 'alpha', 0) DESC, chat IS NOT NULL DESC, s.created_at DESC LIMIT ?",
        (*values, limit),
    ).fetchall()


def _suggestion_line(row: sqlite3.Row) -> str:
    rating = {"alpha": "α kept", "beta": "β skipped"}.get(row["rating"] or "", "not rated")
    if row["basis_kind"] == "user":
        rating = f"the user's own idea, in their words: “{_clip(row['basis'], 300)}”; {rating}"
    state = "" if row["status"] == "open" else f", {row['status']}"
    benefit = f"{row['headline']}: {row['outcome']} How: " if row["headline"] and row["outcome"] else ""
    effort = f"{row['effort']} effort; " if row["effort"] else ""
    return (f"- {benefit}{row['action']} ({effort}{rating}{state}; {_implementation(row)}; from {_title(row)}, "
            f"{row['url']}, get_item('{row['item_id']}')){_asked(row)}")


def _asked(row: sqlite3.Row) -> str:
    """The user's Ask about this on a suggestion (row's `chat`): how many questions and the last answer, which
    Implement also gives its agent; '' when the user asked nothing."""
    messages = json.loads(row["chat"]) if row["chat"] else []
    answers = [m["content"] for m in messages if m["role"] == "assistant"]
    if not answers:
        return ""
    asked = len([m for m in messages if m["role"] == "user"])
    last = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    return (f"\n  The user asked about it ({asked} question{'' if asked == 1 else 's'}); last: “{_clip(last, 200)}” "
            f"Answer: {_clip(answers[-1], 600)}")


def _implementation(row: sqlite3.Row) -> str:
    """Whether Alpha implemented the suggestion, and where (implement.py)."""
    status = implement.state(row)
    branch = row["implement_branch"]
    reason = " ".join((row["implement_error"] or "no reason recorded").split())[:200]
    return {
        None: "not implemented",
        "queued": "implementation waiting to start",
        "implementing": f"being implemented on branch {branch}" if branch else "being implemented",
        "implemented": f"implemented on branch {branch} ({row['implement_files'] or 0} files changed, not merged; "
                       f"worktree {row['implement_path']})",
        "failed": f"implementation failed: {reason}",
        "stale": "implementation stopped without finishing",
        "handoff": f"no local repository; task file {row['implement_path']}",
    }.get(status, status)


@server.tool()
def suggestions(project: str | None = None, status: str = "open") -> str:
    """What Alpha suggests doing with its items in the user's projects: one concrete action per item and project,
    each as its benefit (headline), the outcome to expect, and how ("In <project>: do X with this, because Y; first
    step: Z"), with its implementation status (Alpha's Implement carries one out on a new branch alpha/<slug> of the
    project's repository and leaves it for review; the branch is named when there is one). The ones rated α (kept)
    come first, then those they asked about, then the rest not rated yet; ones they rated β are left out. `project`
    narrows to one project; `status` is open (default), done, dismissed, or all. When asked about a suggestion (the
    dashboard's Ask about this), it says how many questions and gives the last question and its answer: those decisions
    there take precedence over the suggestion. Treat an α suggestion as something the user wants done."""
    with _warehouse() as (_, connection):
        if status not in SUGGESTION_STATUSES:
            return f"Unknown status '{status}'. Statuses: {', '.join(SUGGESTION_STATUSES)}."
        name = None
        if project:
            name = _project(connection, project)
            if name is None:
                return _unknown_project(connection, project)
        rows = _suggestion_rows(connection, name, status, "COALESCE(s.rating, '') != 'beta'", SEARCH_LIMIT * 3)
        scope = f" for {name}" if name else ""
        if not rows:
            return f"Alpha has no suggestions{scope} ({status}) yet."
        by_project: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_project.setdefault(row["project"], []).append(row)
        lines = [f"{len(rows)} suggestion{'s' if len(rows) != 1 else ''}{scope} ({status}), kept (α) first."]
        for key, group in by_project.items():
            lines += ["", f"## {key}", *map(_suggestion_line, group)]
        lines += ["", f"get_item(id) gives the item behind a suggestion. {OWN_LINE}"]
        return "\n".join(lines)


@server.tool()
def list_projects() -> str:
    """The user's projects in Alpha: matched items, latest focus brief date, and live skills for each."""
    with _warehouse() as (settings, connection):
        lines, scored = [], {row[0] for row in connection.execute("SELECT name FROM projects")}
        for name in _projects(connection):
            matched = {row[0] for row in connection.execute(
                "SELECT item_id FROM relevance WHERE project = ? AND score >= ?", (name, settings.relevance_threshold))}
            brief = connection.execute("SELECT MAX(created_at) FROM focus_briefs WHERE project = ?", (name,)).fetchone()[0]
            live = len(_skills_with(connection, matched))
            items = (f"{len(matched)} matched items, {live} live skill{'' if live == 1 else 's'}"
                     if name in scored else "items not scored against it (no project spec), discovery only")
            lines.append(f"- {name}: {items}; {'brief ' + _date(brief) if brief else 'no focus brief'}")
        return ("Projects in Alpha:\n" + "\n".join(lines)) if lines else "Alpha has no projects yet."


@server.tool()
def skills(project: str | None = None) -> str:
    """The live agent skills Alpha built and tested from items (optionally only those built from a project's
    matched items), with where each SKILL.md is installed, plus the proposed topics not built yet."""
    with _warehouse() as (settings, connection):
        if project:
            name = _project(connection, project)
            if name is None:
                return _unknown_project(connection, project)
            ids = {row[0] for row in connection.execute(
                "SELECT item_id FROM relevance WHERE project = ? AND score >= ?", (name, settings.relevance_threshold))}
        else:
            name, ids = None, {row[0] for row in connection.execute("SELECT id FROM items")}
        live = _skills_with(connection, ids)
        lines = [f"Live skills{' for ' + name if name else ''}:" if live else
                 f"No live skills{' built from ' + name + ' items' if name else ''} yet."]
        lines += [_skill_line(settings, row) + f" ({len(members)} source items)" for row, members in live]
        proposed = [row[0] for row in connection.execute("SELECT name FROM topics WHERE status = 'proposed' ORDER BY name")]
        if proposed and not name:
            lines += ["", f"Proposed topics, not built yet ({len(proposed)}): {', '.join(proposed)}"]
        return "\n".join(lines)


def _next_run() -> str:
    """When the scheduled `alpha run` will next pick a request up."""
    from .install import RUN_INTERVAL_SECONDS, _label

    hours = RUN_INTERVAL_SECONDS // 3600
    if not (Path.home() / "Library" / "LaunchAgents" / f"{_label()}.run.plist").is_file():
        return "on the next `alpha run` (no scheduled run is installed: `alpha install`, or run `alpha discover` now)"
    log = TOOL_ROOT / "var" / "logs" / "run.log"
    if log.is_file():
        due = datetime.fromtimestamp(log.stat().st_mtime, UTC) + timedelta(seconds=RUN_INTERVAL_SECONDS)
        if due > datetime.now(UTC):
            return (f"on the next scheduled `alpha run` (every {hours} h), about "
                    f"{due.astimezone().strftime('%H:%M')} local time; `alpha discover` handles it now")
    return f"on the next scheduled `alpha run` (every {hours} h, due now); `alpha discover` handles it sooner"


@server.tool()
def request(query: str, project: str | None = None) -> str:
    """Ask Alpha to go find items about `query` (optionally for a project, so the judge also weighs that project's
    focus brief). The next pipeline run searches every enabled discovered source; what the judge admits shows up
    in `search`. Returns the request id and when it will run."""
    from .sources import enabled, missing

    query = " ".join(query.split())
    if len(query) < 3:
        return "Say what Alpha should look for (a few words)."
    with _warehouse() as (settings, connection):
        if project:
            name = _project(connection, project)
            if name is None:
                return _unknown_project(connection, project)
            project = name
    sources = [source for source in enabled(settings, "discovered") if not missing(source)]
    if not sources:
        return ("No discovered source is enabled and ready ([sources.<name>] enabled = true in config.toml, with its "
                "keys in .env), so Alpha cannot search for requests yet. Nothing was filed.")
    names = ", ".join(source.NAME for source in sources)
    connection = sqlite3.connect(settings.database_path, timeout=30)
    try:
        request_id, status = file_request(connection, query, project)
    finally:
        connection.close()
    if status:
        return f"Request {request_id} for '{query}' is already {status} ({names}); {_next_run()}."
    return (f"Filed request {request_id}: '{query}'{f' for {project}' if project else ''}. Alpha searches "
            f"{names} for it {_next_run()}. The judge admits only what is worth keeping; finds show up in `search`.")


def main() -> None:
    server.run("stdio")
