"""Browser dashboard on localhost only. Pages: the Feed (every item but those rated β, filters, full-text search, day
dividers, and where the new items end), an item's page, Turned down (what the judge left out), Projects (focus
briefs, matches, resources: datasets, models, Spaces), Skills (with a skill update beside the live version,
/skills/<name>/compare/<n>), Requests (what agents asked discovery for), and an implemented suggestion's review
(/item/<id>/suggestion/<project>/review: the agent's report and the branch's diff). A build, a discovery request, and
Implement first show a readback of what they will do (/skills/<name>/confirm, /item/<id>/build, /requests/confirm,
/item/<id>/suggestion/<project>/implement) and start only on its Confirm; an expired X session or an X refusal shows
as a banner on every page until it clears.

Every request reads config.toml and the database again, so a scheduled run or a new threshold shows up on the
next page load without restarting the server. The pages are server-rendered HTML; alpha/static holds the one
stylesheet and the small script (live search, more results, waiting buttons, α / β saved in place, the live run
ledger, keyboard shortcuts, copy buttons, running clocks). An item's page holds the skill chat (skill_chat.py), Have
an idea (own_idea.py: a chat shaping the user's own suggestion for a project, kept rated α or sent to Implement),
and each suggestion its Ask about this (suggestion_chat.py); all three call Claude while the request waits. Builds,
proposals, publishing, rejecting, and the competing-skills check
start the `skills` CLI command in the background, and Implement the `implement` command (implement.py). Removing an
item from a skill, editing a brief, filing a discovery request, and rating an item α or β (taste.py; the next run
writes the taste profile again) are database writes and happen at once.

Two cookies mark what is new: everything Alpha stored after the last page view of your previous visit stays
marked new for the whole of this visit (a visit ends after VISIT_GAP of no page views), until Mark all seen.
"""

import difflib
import json
import mimetypes
import re
import shlex
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlparse

from . import focus, implement, own_idea, skill_chat, suggestion_chat, taste
from .config import TOOL_ROOT, Settings, load_settings
from .install import RUN_INTERVAL_SECONDS
from .render import duplicates_by_original
from .skills import (attempts_of, diff, failure, last_failed, last_problems, live_version, members, overlaps, remove,
                     retry_due, slug, versions)
from .sources import SOURCES, enabled, last_run, missing, options
from .sources import x as x_source
from .storage import KINDS, RESOURCE_KINDS, SKILL_KINDS, connect, file_request, get_metadata, now
from .writer import REGRESSION_TOLERANCE, IdeaPlan, SkillPlan

STATIC = Path(__file__).resolve().parent / "static"
# X post ids are numbers; every other source's ids are <source>-<native id>.
ITEM_ID = re.compile(r"[\w.-]+")
PAGE_SIZE = 50
# An implementation's diff on its review page stops here; the merge commands show the rest.
DIFF_LINES = 3000
# `author` filters the Feed (an item page's author link) without a facet group of its own: there are too many.
FACETS = ("source", "trust", "tier", "kind", "category", "project", "rating", "author")
TRUST_LABELS = {"curated": "Saved by you", "discovered": "Found by Alpha"}
KIND_LABELS = {"post": "Posts", "article": "Articles", "paper": "Papers", "repo": "Repos", "dataset": "Datasets",
               "model": "Models", "space": "Spaces"}
# β items leave the default feed and stay findable here.
RATING_LABELS = {"alpha": "Rated α", "beta": "Rated β", "unrated": "Unrated"}
# A find: both judges said yes. An idea: one did (judge.py); α makes it a find.
TIER_LABELS = {"find": "Finds", "idea": "Ideas"}
JUDGE_NAMES = {"jev": "Jev", "claude": "Claude"}
# α / β on an item: the glyph, its short word, what it means, and what a tap tells Alpha, which stays on as the
# button's title (https://interfacelineage.com/pandora-station-builder: a thumb shapes the stream).
RATE_BUTTONS = {"alpha": ("α", "keep", "α keep: a real edge", "Rated α: Alpha will look for more like this"),
                "beta": ("β", "skip", "β skip: no edge (generic, slop, or already known)",
                         "Rated β: Alpha will look for less like this")}
# The same two buttons on a suggestion (what to do with an item in a project).
SUGGESTION_BUTTONS = {"alpha": ("α", "keep", "α keep: worth doing", "Rated α: Alpha will suggest more like this"),
                      "beta": ("β", "skip", "β skip: not worth doing", "Rated β: Alpha will suggest less like this")}
SUGGESTION_VIEWS = {"": "To do", "beta": "Rated β", "done": "Done"}
SOURCE_LABELS = {"x": "X bookmarks", "x_timeline": "X timeline", "substack": "Substack", "github_stars": "GitHub stars",
                 "huggingface": "Hugging Face datasets", "hf_likes": "Hugging Face likes",
                 "hf_following": "Hugging Face follows", "papers": "Papers", "arxiv": "arXiv",
                 "github_trending": "GitHub trending", "hf_trending": "Hugging Face trending",
                 "substack_following": "Substack subscriptions"}
X_SOURCES = ("x", "x_timeline")
# Where on X a timeline find came from (candidates.query of an x_timeline candidate).
TIMELINE_TABS = {"following": "from your Following", "for_you": "from For You"}
CONTENT_HEADINGS = {"thread": "Rest of the thread", "author_replies": "The author's replies",
                    "quoted": "Quoted post", "replied_to": "Replying to"}
JUDGE_COLUMNS = ("c.why, c.jev_sauce, c.judge_sauce, c.novelty, c.track_record, c.paid, c.similar_to, c.examples, "
                 "c.idea_judge, COALESCE(c.project, c.fit_project) AS found_for, c.query AS searched, "
                 "(SELECT query FROM requests q WHERE q.id = c.request_id) AS request_query, "
                 "CASE WHEN c.source = 'x_timeline' THEN c.query END AS found_in")
# Markers FTS5 puts around matched words; HTML-escaping leaves them alone, then they become <mark>.
OPEN, CLOSE = "\x02", "\x03"
RUN_COMMAND = r"alpha (run|collect|discover|process|focus)( |$)"
VISIT_GAP = timedelta(minutes=30)
SEEN_COOKIE, ACTIVE_COOKIE = "alpha_seen", "alpha_active"
COOKIE_AGE = 400 * 24 * 3600
# Where `g` then a letter goes, and the keyboard sheet `?` opens.
GO_KEYS = (("f", "/", "Feed"), ("p", "/projects", "Projects"), ("s", "/skills", "Skills"), ("r", "/requests", "Requests"))
KEYS = (("j / k", "Next / previous item in a list"), ("o or Enter", "Open the selected item"),
        ("a / b", "Rate the selected item α or β (again to clear)"),
        ("/", "Search the Feed"), ("Esc", "Clear the search, close this sheet, leave a comparison or a readback"),
        *((f"g then {key}", f"Go to {label}") for key, _, label in GO_KEYS), ("?", "Show these shortcuts"))


def _connect(settings: Settings) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _ago(timestamp: str | None) -> str:
    if not timestamp:
        return "never"
    minutes = int((datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} days ago"


def _until(timestamp: datetime) -> str:
    minutes = int((timestamp - datetime.now(UTC)).total_seconds() // 60)
    if minutes <= 0:
        return "due now"
    if minutes < 60:
        return f"due in {minutes} min"
    return f"due in {round(minutes / 60)} h"


def _date(timestamp: str | None) -> str:
    if not timestamp:
        return ""
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return escape(timestamp[:10])
    year = "" if moment.year == datetime.now(UTC).year else f", {moment.year}"
    return f"{moment:%b} {moment.day}{year}"


def _moment(timestamp: str) -> str:
    local = datetime.fromisoformat(timestamp).astimezone()
    return f"{local:%b} {local.day}, {local:%H:%M}"


def _instant(text: str | None) -> str | None:
    """`text` when it is an ISO timestamp with a time zone (a cookie or a URL can hold anything), else None."""
    try:
        return text if text and datetime.fromisoformat(text).tzinfo else None
    except ValueError:
        return None


def _cookie(name: str, value: str) -> str:
    return f"{name}={value}; Max-Age={COOKIE_AGE}; Path=/; SameSite=Lax; HttpOnly"


def _visit(cookie_header: str | None) -> tuple[str, list[str]]:
    """Since when items count as new on this page view, and the Set-Cookie values that carry it to the next one.
    The first page view of a visit makes everything stored after the previous visit's last page view new."""
    cookies = SimpleCookie()
    try:
        cookies.load(cookie_header or "")
    except CookieError:
        pass
    moment = now()
    active, seen = (_instant(cookies[name].value) if name in cookies else None for name in (ACTIVE_COOKIE, SEEN_COOKIE))
    if active is None:
        seen = moment  # a first visit: nothing is new yet
    elif datetime.now(UTC) - datetime.fromisoformat(active) > VISIT_GAP:
        seen = active
    else:
        seen = seen or active
    return seen, [_cookie(SEEN_COOKIE, seen), _cookie(ACTIVE_COOKIE, moment)]


def _source_label(name: str) -> str:
    return SOURCE_LABELS.get(name, name.replace("_", " ").capitalize())


def _title(row: sqlite3.Row) -> str:
    """Claude's title, else the source's, else (until the summary lands) the first line of the item's own text."""
    line = next((line.strip() for line in (row["text"] or "").splitlines() if line.strip()), "")
    return row["title"] or row["title_source"] or (line[:117] + "…" if len(line) > 120 else line) or "Untitled"


def _text(text: str) -> str:
    return f'<div class="prose">{escape(text)}</div>'


def _marked(text: str | None) -> str:
    return escape(text or "").replace(OPEN, "<mark>").replace(CLOSE, "</mark>")


def _media_url(detail: dict, local_only: bool = False) -> str | None:
    if detail.get("file"):
        return f"/media/{quote(detail['file'])}"
    return None if local_only else detail.get("url")


def _static(name: str) -> str:
    """The file's URL with its modification time, so a changed file is fetched again while the browser
    caches it otherwise."""
    return f"/static/{name}?v={int((STATIC / name).stat().st_mtime)}"


# Shell: header, run ledger, tabs ----------------------------------------------------------------------


def _running() -> list[str]:
    """The Alpha commands running right now (run, collect, discover, process, focus), from the process list."""
    try:
        listing = subprocess.run(["pgrep", "-lf", RUN_COMMAND], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return sorted({match.group(1) for match in re.finditer(r"alpha (run|collect|discover|process|focus)\b", listing)})


def _ledger(connection: sqlite3.Connection, settings: Settings, since: str) -> str:
    """One status dot in the header (it pulses while a run or a build is going) that opens the run ledger: the run
    status, each source's last run and next due run, the warehouse totals, and the keyboard sheet. Beside the dot, how
    many items arrived after `since` (when the page was drawn; the script asks again every few seconds while a run is
    going, GET /ledger)."""
    building = connection.execute("SELECT COUNT(*) FROM skill_versions WHERE status = 'building'").fetchone()[0]
    running = _running()
    state = f"Running alpha {' + '.join(running)}" if running else "Idle"
    if building:
        state += f" · building {building} skill{'s' if building > 1 else ''}"
    sources = []
    for source in SOURCES.values():
        config = options(settings, source)
        ran, result = last_run(connection, source)
        lacking = missing(source)
        if not config["enabled"]:
            status, tone = "off", "off"
        elif lacking:
            status, tone = f"needs {', '.join(lacking)}", "bad"
        else:
            due = (datetime.fromisoformat(ran).timestamp() + float(config["every_hours"]) * 3600) if ran else None
            status = _until(datetime.fromtimestamp(due, UTC)) if due else "due now"
            tone = "bad" if (result or "").startswith("failed") else "ok"
        # A failed run says why in the panel itself, in red, not only in the tooltip (https://interfacelineage.com/show-every-state).
        error = (f'<span class="src-error">{escape(result.removeprefix("failed: "))}</span>'
                 if tone == "bad" and (result or "").startswith("failed") else "")
        sources.append(
            f'<span class="src {tone}" title="{escape(result or "No run yet")}"><b>{escape(_source_label(source.NAME))}</b> '
            f"{_ago(ran)} · {escape(status)}{error}</span>"
        )
    arrived = connection.execute("SELECT COUNT(*) FROM items WHERE found_at > ?", (since,)).fetchone()[0]
    link = (f'<a class="arrived" href="/">{arrived} new item{"s" if arrived != 1 else ""} arrived · show</a>'
            if arrived else "")
    return (
        f'<details class="ledger" id="ledger" data-since="{escape(since)}">'
        f'<summary class="pulse {"on" if running or building else ""}" title="{escape(state)}">'
        f'<span class="sr">Run status: {escape(state)}</span>{link}</summary>'
        f'<div class="ledger-panel" role="status"><p class="state">{escape(state)}</p>'
        f'<span class="sources">{"".join(sources)}</span>{_totals(connection, settings)}'
        '<button class="quiet" type="button" id="keys-open">Keyboard shortcuts <kbd>?</kbd></button></div></details>'
    )


def _fresh(connection: sqlite3.Connection, settings: Settings, seen: str, where: str = "1",
           parameters: list | tuple = ()) -> tuple[int, int]:
    """How many items (`i`, within `where`) Alpha stored after `seen`, and how many of those fit a project."""
    row = connection.execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM relevance r WHERE r.item_id = i.id "
        f"AND r.score >= ?)) FROM items i WHERE i.found_at > ? AND {where}",
        (settings.relevance_threshold, seen, *parameters),
    ).fetchone()
    return row[0], row[1]


def _totals(connection: sqlite3.Connection, settings: Settings) -> str:
    total = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    matches = connection.execute(
        "SELECT COUNT(*) FROM relevance WHERE score >= ?", (settings.relevance_threshold,)
    ).fetchone()[0]
    live = connection.execute("SELECT COUNT(*) FROM skill_versions WHERE status = 'live'").fetchone()[0]
    return (f'<p class="totals"><b>{total:,}</b> items · <b>{matches:,}</b> project matches · '
            f'<b>{live}</b> live skill{"s" if live != 1 else ""}</p>')


def _alerts(connection: sqlite3.Connection, settings: Settings) -> str:
    """The two states that need you, as a banner across the top of every page until they clear (x.py records and
    clears them): the X session expired, or X refused a read. Everything routine stays in the status dot
    (https://interfacelineage.com/stay-in-the-periphery: bring consequential changes into the center)."""
    if not any(options(settings, SOURCES[name])["enabled"] for name in X_SOURCES):
        return ""
    banners = []
    for key in (x_source.SESSION_EXPIRED_KEY, x_source.REFUSED_KEY):
        try:
            state = json.loads(get_metadata(connection, key) or "null")
        except ValueError:
            state = None
        if not isinstance(state, dict):
            continue
        when = f" ({escape(_moment(state['at']))})" if _instant(state.get("at")) else ""
        reason = escape(state.get("reason") or "")
        if key == x_source.SESSION_EXPIRED_KEY:
            banners.append(
                f'<p><span><b>Your X session expired{when}.</b> Alpha cannot read your bookmarks or timelines: {reason}. '
                "In a logged-in x.com tab, copy the <code>auth_token</code> and <code>ct0</code> cookies into "
                "<code>X_AUTH_TOKEN</code> and <code>X_CT0</code> in <code>.env</code>; this clears after the next "
                "run reads X.</span></p>")
        else:
            ran, _ = last_run(connection, SOURCES["x"])
            every = float(options(settings, SOURCES["x"])["every_hours"])
            again = (_until(datetime.fromisoformat(ran) + timedelta(hours=every)).replace("due", "next X read", 1)
                     if ran else "next X read on the next run")
            banners.append(
                f"<p><span><b>X refused a request{when}.</b> {reason}. Alpha stopped reading X for that run so as not "
                f"to make it worse and tries again when X is next due ({escape(again)}); this clears once X "
                "answers.</span></p>")
    return f'<div class="alert" role="alert">{"".join(banners)}</div>' if banners else ""


def _layout(connection: sqlite3.Connection, settings: Settings, view: str, title: str, main: str, seen: str) -> str:
    """The page around `main`: brand, tabs (`view` marks the active one; the Feed's says what is new since
    `seen`), the status dot with the run ledger, and the keyboard sheet."""
    # The header stays quiet: each tab's count is in its tooltip.
    count = (lambda sql: connection.execute(sql).fetchone()[0])
    counts = {
        "feed": f"{count('SELECT COUNT(*) FROM items'):,} items",
        "projects": f"{len(_project_names(connection)):,} projects",
        "skills": f"{count('SELECT COUNT(*) FROM topics'):,} skills",
        "requests": f"""{count("SELECT COUNT(*) FROM requests WHERE status != 'done'"):,} open requests""",
    }
    # Two tiers, as Slack marks unread: a number when new items fit a project, a quiet dot when they only arrived.
    fresh, fitting = _fresh(connection, settings, seen)
    said = f"{fresh:,} new since your last visit" + (f", {fitting:,} fit a project" if fitting else "")
    marks = {"feed": (f'<span class="fresh" title="{said}"><span class="sr">{said}</span>{fitting:,}</span>' if fitting
                      else f'<span class="fresh dot" title="{said}"><span class="sr">{said}</span></span>' if fresh
                      else "")}
    tabs = "".join(
        f'<a href="{href}" class="{"active" if view == name else ""}"{" aria-current=page" if view == name else ""} '
        f'title="{counts[name]}"><span class="glyph" aria-hidden="true">{glyph}</span>{label}{marks.get(name, "")}</a>'
        for name, href, label, glyph in (("feed", "/", "Feed", "≡"), ("projects", "/projects", "Projects", "◇"),
                                         ("skills", "/skills", "Skills", "✦"), ("requests", "/requests", "Requests", "?"))
    )
    keys = "".join(f"<dt><kbd>{escape(key)}</kbd></dt><dd>{escape(what)}</dd>" for key, what in KEYS)
    go = escape(json.dumps({key: href for key, href, _ in GO_KEYS}))
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0e1b19">
<title>{escape(title)} · Alpha</title>
<link rel="icon" type="image/svg+xml" href="{_static("logo.svg")}">
<link rel="stylesheet" href="{_static("dashboard.css")}">
</head>
<body class="view-{view}" data-go="{go}">
{_alerts(connection, settings)}
<header class="top"><a class="brand" href="/"><img class="mark" src="{_static("logo.svg")}" alt="" width="30" height="30">Alpha</a><nav class="tabs">{tabs}</nav>
{_ledger(connection, settings, now())}</header>
<main>{main}</main>
<dialog id="keys" aria-labelledby="keys-title"><h4 id="keys-title">Keyboard</h4><dl>{keys}</dl>
<form method="dialog"><button class="secondary">Close</button></form></dialog>
<script src="{_static("dashboard.js")}" defer></script>
</body></html>"""


# Shared pieces --------------------------------------------------------------------------------------


def _project_names(connection: sqlite3.Connection) -> list[str]:
    """Every project with a spec, a brief, or a place in the last focus scan."""
    names = {row["name"] for row in connection.execute("SELECT name FROM projects")}
    names |= {row["project"] for row in connection.execute("SELECT DISTINCT project FROM focus_briefs")}
    scan = json.loads(get_metadata(connection, "focus_scan") or "{}")
    names |= {project["name"] for project in scan.get("projects", [])}
    return sorted(names)

def _found_chip(row: sqlite3.Row) -> str:
    """'Found by Alpha for <project>': the project whose focus (or an agent's request, or the user's taste) the item
    was found for, and for a timeline find the tab it came from."""
    keys = row.keys()
    project = row["found_for"] if "found_for" in keys else None
    if project and project.startswith("request:"):
        target = " for an agent's request"
    elif project == "taste":
        target = " for your taste"
    elif project:
        target = f' for <a href="/project/{quote(project)}">{escape(project)}</a>'
    else:
        target = ""
    tab = TIMELINE_TABS.get(row["found_in"] or "") if "found_in" in keys else None
    return f'<span class="chip found">Found by Alpha{target}{f" · {tab}" if tab else ""}</span>'


def _examples(row: sqlite3.Row) -> dict[str, list[str]]:
    """The bookmarks (kept) and β items (rejected) the judge was shown for this item, when they were kept."""
    return json.loads(row["examples"] or "{}") if "examples" in row.keys() else {}


def _titles(connection: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, str]:
    """Titles of the items the rows point at: the original of a repeat, the judge's examples, the closest item."""
    wanted = {row["duplicate_of"] for row in rows if row["duplicate_of"]}
    for row in rows:
        wanted.update(*_examples(row).values())
        if "similar_to" in row.keys() and row["similar_to"] and not row["similar_to"].startswith("skill:"):
            wanted.add(row["similar_to"])
    if not wanted:
        return {}
    return {r["id"]: _title(r) for r in connection.execute(
        f"SELECT id, title, title_source, substr(text, 1, 400) AS text FROM items WHERE id IN "
        f"({','.join('?' * len(wanted))})", list(wanted))}


def _ratings(connection: sqlite3.Connection, item_ids: list[str]) -> dict[str, sqlite3.Row]:
    if not item_ids:
        return {}
    return {row["item_id"]: row for row in connection.execute(
        f"SELECT item_id, rating, reason FROM ratings WHERE item_id IN ({','.join('?' * len(item_ids))})", item_ids)}


def _rate(item_id: str, rating: sqlite3.Row | None, project: str | None = None) -> str:
    """α and β, one tap each (the active one again clears it); after β, three optional reasons. Each button posts its
    next state, so the form works without the script; the script saves it in place (dashboard.js). With `project`,
    the buttons rate the item's suggestion for that project."""
    action = f"/item/{quote(item_id)}" + (f"/suggestion/{quote(project, safe='')}" if project else "")
    labels = SUGGESTION_BUTTONS if project else RATE_BUTTONS
    current, reason = (rating["rating"] or "", rating["reason"] or "") if rating else ("", "")
    buttons = "".join(
        f'<button type="submit" class="rate-{name}" name="rating" value="{"" if current == name else name}" '
        f'data-value="{name}" data-label="{escape(label)}" data-done="{escape(done)}" aria-label="{escape(label)}" '
        f'aria-pressed="{"true" if current == name else "false"}" title="{escape(done if current == name else label)}">'
        f'<span class="glyph" aria-hidden="true">{glyph}</span><span class="word" aria-hidden="true">{word}</span></button>'
        for name, (glyph, word, label, done) in labels.items()
    )
    chips = "".join(
        f'<button type="submit" name="reason" value="{"" if reason == key else key}" data-value="{key}" '
        f'formaction="{action}/reason" aria-pressed="{"true" if reason == key else "false"}">{escape(label)}</button>'
        for key, label in taste.REASONS.items()
    )
    return (f'<form class="rate" method="post" action="{action}/rate" data-rating="{escape(current)}" '
            f'data-reason="{escape(reason or "")}">{buttons}'
            f'<span class="reasons" role="group" aria-label="Why β (optional)">{chips}</span>'
            '<span class="said" role="status"></span></form>')


def _how(row: sqlite3.Row, settings: Settings, titles: dict[str, str], action: sqlite3.Row | None = None) -> str:
    """How it knew, one tap away: the derivation behind a discovered item's one-line why. Where it came from, what it
    was found for, both judges, how new it is, and the examples the judge saw (https://interfacelineage.com/show-how-it-knew).
    Candidates judged before the examples were kept show the rest. With `action` (a Feed row led by its suggestion's
    headline), the suggestion's how and the judge's why come first."""
    keys = row.keys()
    if row["trust"] != "discovered" or "jev_sauce" not in keys or row["jev_sauce"] is None:
        return ""
    source = escape(_source_label(row["source"]))
    if tab := TIMELINE_TABS.get(row["found_in"] or ""):
        came = f"{source}, {tab}"
    elif row["request_query"]:
        came = f"{source}, searching an agent's request “{escape(row['request_query'])}”"
    elif row["searched"] and row["source"] == "hf_following":  # the query is the followed account it came from
        came = f"{source}, released by {escape(row['searched'])}, an account you follow"
    elif row["searched"] and row["source"] == "substack_following":  # the query is the publication it came from
        came = f"{source}, from {escape(row['searched'])}, a newsletter you subscribe to"
    elif row["searched"]:
        came = f"{source}, searching “{escape(row['searched'])}”"
    else:
        came = source
    lines = []
    if action is not None:
        lines += [("How", f'<a href="/project/{quote(action["project"])}">{escape(action["project"])}</a>: '
                          f'{escape(action["action"])}'),
                  ("Judge's why", _verdict_line(row) or "not recorded")]
    lines.append(("Came from", came))
    project = row["found_for"] or ""
    if project:
        lines.append(("Found for", "an agent's request" if project.startswith("request:")
                      else "your taste, no project in particular" if project == "taste"
                      else f'<a href="/project/{quote(project)}">{escape(project)}</a>'))
    lines.append(("Jev", f"{row['jev_sauce']:.0%} yes, it is real alpha"))
    verdict = {1: "yes", 0: "no"}.get(row["judge_sauce"], "not recorded")
    paid = " · judged on its public preview (paid)" if row["paid"] else ""
    lines.append(("Claude judge", f"{verdict} ({escape(settings.judge.model)}){paid}"))
    if row["tier"] == "idea" and row["idea_judge"]:
        other = "Claude" if row["idea_judge"] == "jev" else "Jev"
        lines.append(("Split", f"an idea: {JUDGE_NAMES[row['idea_judge']]} said yes, {other} said no; a find needs "
                               "both. Rate it α to keep it as a find (then it may feed skills)."))
    closest = row["similar_to"] or ""
    if closest.startswith("skill:"):
        closest = f' · closest: the skill <a href="/skills#skill-{quote(closest[6:])}">{escape(closest[6:])}</a>'
    elif closest:
        closest = f' · closest: <a href="/item/{quote(closest)}">{escape(titles.get(closest) or "an earlier item")}</a>'
    lines.append(("New to you", f"{row['novelty']:.0%}{closest}"))
    record = row["track_record"]
    lines.append(("Author", "first item from this author" if record is None else f"{record:.0%} of their judged posts got in"))
    examples = _examples(row)
    for key, label in (("kept", "Picked items shown to the judge"), ("rejected", "β items shown as counter-examples")):
        if ids := examples.get(key):
            links = "".join(f'<li><a href="/item/{quote(i)}">{escape(titles.get(i) or "an item no longer kept")}</a></li>'
                            for i in ids)
            lines.append((label, f"<ul>{links}</ul>"))
    if not examples and getattr(SOURCES.get(row["source"]), "TASTE", False):
        lines.append(("Examples", "not recorded: judged before Alpha kept which bookmarks the judge saw"))
    entries = "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in lines)
    return f'<details class="how-knew"><summary>how?</summary><dl>{entries}</dl></details>'


def _verdict_line(row: sqlite3.Row) -> str:
    """The judge's one line for a discovered item: Claude's why, or for an idea only Jev passed, Jev's call beside
    Claude's no (Jev gives a probability, not a reason)."""
    why = escape(row["why"] or "")
    if row["tier"] == "idea" and row["idea_judge"] == "jev":
        return f"Jev says yes ({row['jev_sauce']:.0%}); Claude said no: {why}"
    return why


def _idea_chip(row: sqlite3.Row) -> str:
    if row["tier"] != "idea":
        return ""
    judge = JUDGE_NAMES.get(row["idea_judge"] if "idea_judge" in row.keys() else "", "One judge")
    return f'<span class="chip idea" title="{judge} said yes; the other judge said no">Idea</span>'


def _judged(row: sqlite3.Row, settings: Settings, titles: dict[str, str]) -> str:
    """The judge's why for a discovered item (row joined with its candidate), and how it knew."""
    if row["trust"] != "discovered" or row["jev_sauce"] is None:
        return ""
    return (f'<div class="judged">{_found_chip(row)}{_idea_chip(row)}<p>{_verdict_line(row)}</p>'
            f"{_how(row, settings, titles)}</div>")


def _actions(connection: sqlite3.Connection, item_ids: list[str]) -> dict[str, sqlite3.Row]:
    """Each item's suggestion to show in a list: one the user rated α, else one written from the judge's why (the
    project it was found for), else the newest; β-rated and done ones are left out."""
    found: dict[str, sqlite3.Row] = {}
    if item_ids:
        for row in connection.execute(
            f"SELECT * FROM suggestions WHERE item_id IN ({','.join('?' * len(item_ids))}) "
            "AND COALESCE(rating, '') != 'beta' AND status = 'open' "
            "ORDER BY COALESCE(rating = 'alpha', 0), basis_kind != 'match', created_at",
            item_ids,
        ):
            found[row["item_id"]] = row  # the last one wins: the best by the order above
    return found


def _elapsed(timestamp: str) -> str:
    """Minutes since `timestamp`, as the running clock beside Implementing… starts it (dashboard.js keeps it going)."""
    minutes = int((datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds() // 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def _implementation(item_id: str, suggestion: sqlite3.Row) -> tuple[str, str]:
    """Implement and what it did, each state its own (https://interfacelineage.com/show-every-state): the button
    for the suggestion's foot and the line under it. Implement and Try again open the readback; nothing starts
    before its Confirm."""
    base = f"/item/{quote(item_id)}/suggestion/{quote(suggestion['project'], safe='')}"
    status = implement.state(suggestion)
    start = (lambda label: f'<form method="get" action="{base}/implement"><button class="quiet implement">{label}'
             "</button></form>")
    branch = f'<code>{escape(suggestion["implement_branch"] or "")}</code>'
    remove = (f'<a class="quiet-link danger" href="{base}/remove">Remove branch</a>'
              if suggestion["implement_branch"] else "")
    if status is None:
        return start("Implement"), ""
    if status == "queued":
        return "", ('<p class="implement-state running">Waiting for another implementation to finish… '
                    f'<span class="elapsed" data-since="{escape(suggestion["implement_started_at"])}">'
                    f'{_elapsed(suggestion["implement_started_at"])}</span></p>')
    if status == "implementing":
        on = f" on {branch}" if suggestion["implement_branch"] else ""
        return "", ('<p class="implement-state running">Implementing… '
                    f'<span class="elapsed" data-since="{escape(suggestion["implement_started_at"])}">'
                    f'{_elapsed(suggestion["implement_started_at"])}</span>{on}</p>')
    if status == "implemented":
        files = suggestion["implement_files"] or 0
        changed = f"{files} file{'' if files == 1 else 's'} changed" if files else "nothing changed, no commit"
        return "", (f'<p class="implement-state ready">Implemented on {branch} · {changed} · '
                    f'<a href="{base}/review">Review changes</a></p>')
    if status == "handoff":
        return start("Write again"), ('<p class="implement-state">No local repository, so nothing ran. Task file: '
                                      f'<code class="copyable">{escape(suggestion["implement_path"] or "")}</code></p>')
    reason = ("It stopped without finishing (no word from the run for too long)." if status == "stale"
              else _plain(suggestion["implement_error"] or "no reason recorded"))
    return start("Try again"), f'<p class="implement-state failed">Failed: {escape(reason)} {remove}</p>'


def _ask_anchor(item_id: str, project: str) -> str:
    """The page anchor of a suggestion's Ask about this, so an answer lands back on it."""
    return "ask-" + re.sub(r"[^\w.-]+", "-", f"{item_id}--{project}")


def _ask(item_id: str, project: str, messages: list[dict], error: str) -> str:
    """Ask about this: questions about a suggestion before acting on it, answered by Claude from the suggestion, its
    item, and the project (suggestion_chat.py), in the skill chat's bubbles; open while it holds a conversation."""
    base = f"/item/{quote(item_id)}/suggestion/{quote(project, safe='')}"
    bubbles = "".join(f'<div class="bubble{" you" if m["role"] == "user" else ""}">{escape(m["content"])}</div>'
                      for m in messages)
    note = ("" if messages else '<p class="note">Ask how it would affect your project before you implement it: '
            "impact, cost, risk, effort. Implement gives your questions and answers to its agent.</p>")
    failed = f'<p class="error">Something went wrong: {escape(error)}</p>' if error else ""
    over = (f'<form method="post" action="{base}/ask-reset" onsubmit="return confirm(\'Start this conversation over?\')">'
            '<button class="quiet">Start over</button></form>' if messages else "")
    return (
        f'<details class="ask" id="{_ask_anchor(item_id, project)}"{" open" if messages or error else ""}>'
        f"<summary>Ask about this{f' · {len(messages) // 2} answered' if messages else ''}</summary>{note}"
        f'{f"<div class=chat>{bubbles}</div>" if bubbles else ""}{failed}'
        f'<form class="wait stack" data-wait="Thinking…" method="post" action="{base}/ask">'
        '<textarea name="message" rows="2" required placeholder="e.g. How would this affect my pipeline, and how much '
        'work is it? (⌘↩ sends)"></textarea><div class="actions"><button class="secondary">Ask</button></div></form>'
        f"{over}</details>"
    )


def _suggestion(item_id: str, suggestion: sqlite3.Row | None, item: sqlite3.Row | None = None,
                chat: list[dict] | None = None, error: str = "") -> str:
    """A suggestion (what to do with the item in one project): the benefit (its headline) and the outcome to expect
    first, then How (the technical steps, with the first step), with α / β, Mark done, Implement, and Ask about this
    (`chat`: its conversation so far; `error`: why the last question got no answer). With `item` (a project's list),
    the headline and outcome lead and open to the how, the item it came from, and Ask about this."""
    if suggestion is None:
        return ""
    project = suggestion["project"]
    base = f"/item/{quote(item_id)}/suggestion/{quote(project, safe='')}"
    done = suggestion["status"] == "done"
    yours = '<span class="chip yours" title="You wrote this one">Your idea</span>' if suggestion["basis_kind"] == "user" else ""
    if suggestion["headline"]:
        headline = f'<span class="headline">{yours}{escape(suggestion["headline"])}</span>'
        outcome = f'<span class="outcome">{escape(suggestion["outcome"])}</span>' if suggestion["outcome"] else ""
        if suggestion["effort"]:
            outcome += f'<span class="note"> · {escape(suggestion["effort"])} effort</span>'
        how = f'<p class="action"><span class="label">How</span>{escape(suggestion["action"])}</p>'
    else:  # written before headlines, until the next run gives it one
        headline, outcome, how = f'<span class="headline">{yours}{escape(suggestion["action"])}</span>', "", ""
    if yours:
        how += f'<p class="action"><span class="label">You wrote</span>{escape(suggestion["basis"])}</p>'
    ask = _ask(item_id, project, chat or [], error)
    if item is not None:
        idea = '<span class="chip idea">Idea</span>' if item["tier"] == "idea" else ""
        source = (f'<p class="from">From <a href="/item/{quote(item_id)}">{escape(_title(item))}</a>'
                  f'<span class="note"> · {escape(_source_label(item["source"]))}</span>{idea}</p>')
        opened = " open" if chat or error else ""
        text = f'<details class="expand"{opened}><summary>{headline}{outcome}</summary>{how}{source}{ask}</details>'
        ask = ""
    else:
        text = f"{headline}{outcome}{how}"
    button, state = _implementation(item_id, suggestion)
    return (
        f'<{"li" if item is not None else "div"} class="suggestion{" done" if done else ""}">{text}'
        f'<div class="suggestion-foot">{_rate(item_id, suggestion, project)}'
        f'<form method="post" action="{base}/{"reopen" if done else "done"}">'
        f'<button class="quiet">{"Reopen" if done else "Mark done"}</button></form>{button}</div>{state}{ask}'
        f'</{"li" if item is not None else "div"}>'
    )


def _thumbs(connection: sqlite3.Connection, item_ids: list[str]) -> dict[str, str]:
    """The first saved image of each item (local files only, so the feed loads nothing from outside)."""
    if not item_ids:
        return {}
    thumbs: dict[str, str] = {}
    for row in connection.execute(
        f"SELECT item_id, detail FROM content WHERE kind = 'image' AND item_id IN ({','.join('?' * len(item_ids))}) "
        "ORDER BY rowid",
        item_ids,
    ):
        if row["item_id"] not in thumbs and (url := _media_url(json.loads(row["detail"] or "{}"), local_only=True)):
            thumbs[row["item_id"]] = url
    return thumbs


def _matches(connection: sqlite3.Connection, settings: Settings, item_ids: list[str]) -> dict[str, list[sqlite3.Row]]:
    found: dict[str, list[sqlite3.Row]] = defaultdict(list)
    if item_ids:
        for row in connection.execute(
            f"SELECT item_id, project, score, reason FROM relevance WHERE score >= ? "
            f"AND item_id IN ({','.join('?' * len(item_ids))}) ORDER BY score DESC",
            (settings.relevance_threshold, *item_ids),
        ):
            found[row["item_id"]].append(row)
    return found


def _row(row: sqlite3.Row, *, settings: Settings, matches: list[sqlite3.Row], thumb: str | None, seen: str,
         titles: dict[str, str], rating: sqlite3.Row | None, fit: sqlite3.Row | None = None,
         compact: bool = False, action: sqlite3.Row | None = None) -> str:
    """One item in a list, marked new when Alpha stored it after `seen`. `fit` (a project page) shows that
    project's score and reason instead of the project pills; a search result row carries hit_* columns with the
    matched words marked. `compact` (the Feed) keeps only what it takes to read and rate: for a find or an idea with
    a suggestion (`action`), its headline as the title, the outcome to expect, and the item and source it came from;
    else the title and one line (the judge's why, else the summary) and the source; how? for a find, and α / β."""
    keys = row.keys()
    title = _marked(row["hit_title"]) if "hit_title" in keys and row["hit_title"] else escape(_title(row))
    # Not summarized yet: the start of the item's own text stands in.
    summary = escape(row["summary"] or " ".join((row["text"] or "").split())[:280])
    hit = ""
    if "hit_summary" in keys:
        if OPEN in (row["hit_summary"] or ""):
            summary = _marked(row["hit_summary"])
        elif snippet := next((row[c] for c in ("hit_text", "hit_content") if OPEN in (row[c] or "")), None):
            hit = f'<p class="hit">…{_marked(snippet.strip(" …"))}…</p>'
    new = row["found_at"] > seen
    beta = " rated-beta" if rating and rating["rating"] == "beta" else ""
    if compact:
        source = escape(_source_label(row["source"]))
        dot = '<span class="new" title="New since your last visit"><span class="sr">new</span></span>' if new else ""
        led = action is not None and bool(action["headline"])
        if led:
            # A find or an idea leads with what it would do for the project, plainly: the benefit, the outcome to
            # expect, then the item and source it came from. The how and the judge's why wait under how?.
            title = escape(action["headline"])
            line = summary if "<mark>" in summary else escape(action["outcome"] or "")
            effort = f' · {escape(action["effort"])} effort' if action["effort"] else ""
            under = f'<p class="from-item">{escape(_title(row))} · {source}{effort}</p>'
            chip = _idea_chip(row)
        else:
            why = _verdict_line(row) if row["trust"] == "discovered" and "why" in keys and row["why"] else ""
            line = summary if "<mark>" in summary else escape(action["action"]) if action else why or summary
            under = ""
            chip = (f'<span class="chip src found" title="{escape(TRUST_LABELS["discovered"])}">{source}</span>'
                    if row["trust"] == "discovered" else f'<span class="chip src">{source}</span>') + _idea_chip(row)
        paragraph = f'<p class="{"line outcome" if led else "line"}">{line}</p>' if line else ""
        return (
            f'<li class="row compact {escape(row["trust"])}{" new" if new else ""}{beta}"><div class="row-body">'
            f'<h3><a href="/item/{quote(row["id"])}">{dot}{title}</a></h3>{paragraph}{under}{hit}'
            f'<div class="foot">{chip}{_how(row, settings, titles, action if led else None)}'
            f'{_rate(row["id"], rating)}</div></div></li>'
        )
    meta = ['<span class="new">new</span>'] if new else []
    meta.append(f'<span class="src">{escape(_source_label(row["source"]))}</span>')
    if row["kind"] not in ("post",):
        meta.append(f"<span>{escape(row['kind'])}</span>")
    if row["author"]:
        meta.append(f"<span>{'@' if row['source'] in X_SOURCES else ''}{escape(row['author'])}</span>")
    if row["created_at"]:
        meta.append(f'<time datetime="{escape(row["created_at"])}">{_date(row["created_at"])}</time>')
    if row["category"]:
        meta.append(f'<span class="cat">{escape(row["category"])}</span>')
    found = ""
    if row["trust"] == "discovered":
        found = (f'<div class="found"><p>{_found_chip(row)}{_idea_chip(row)}{_verdict_line(row) if "why" in keys else ""}'
                 f'</p>{_how(row, settings, titles)}</div>')
    tail = ""
    if fit is not None:
        resource = '<span class="chip">resource</span>' if row["kind"] in RESOURCE_KINDS else ""
        tail = (f'<p class="fit">{resource}<b>{fit["score"]:.1f}</b> {escape(fit["reason"] or "Reason pending.")}</p>')
    elif row["duplicate_of"]:
        original = titles.get(row["duplicate_of"])
        tail = (f'<p class="repeat">Same idea as <a href="/item/{quote(row["duplicate_of"])}">'
                f'{escape(original or "an earlier item")}</a></p>')
    elif matches:
        tail = '<ul class="fits">' + "".join(
            f'<li><a href="/project/{quote(m["project"])}" title="{escape(m["reason"] or "Reason pending.")}">'
            f'{escape(m["project"])}<b>{m["score"]:.1f}</b></a></li>'
            for m in matches
        ) + "</ul>"
    picture = (f'<a class="thumb" href="/item/{quote(row["id"])}" tabindex="-1" aria-hidden="true">'
               f'<img src="{escape(thumb)}" alt="" loading="lazy" decoding="async"></a>') if thumb else ""
    return (
        f'<li class="row {escape(row["trust"])}{" new" if new else ""}{beta}"><div class="row-body">'
        f'<div class="meta">{"".join(meta)}</div>'
        f'<h3><a href="/item/{quote(row["id"])}">{title}</a></h3>'
        f'{f"<p class=summary>{summary}</p>" if summary else ""}{hit}{found}{tail}'
        f'<div class="foot">{_rate(row["id"], rating)}</div></div>{picture}</li>'
    )


def _rows(connection: sqlite3.Connection, settings: Settings, rows: list[sqlite3.Row], seen: str,
          fits: dict[str, sqlite3.Row] | None = None, marks: dict[int, str] | None = None, compact: bool = False) -> str:
    """The rows as list items; `marks` puts landmarks (a day, the end of what is new) before the row at an index."""
    ids = [row["id"] for row in rows]
    matches = {} if compact else _matches(connection, settings, list({row["duplicate_of"] or row["id"] for row in rows}))
    thumbs = {} if compact else _thumbs(connection, ids)
    titles = _titles(connection, rows)
    ratings = _ratings(connection, ids)
    actions = _actions(connection, [row["id"] for row in rows if row["trust"] == "discovered"]) if compact else {}
    return "".join(
        (marks or {}).get(index, "")
        + _row(row, settings=settings, matches=matches.get(row["id"], []), thumb=thumbs.get(row["id"]), seen=seen,
               titles=titles, rating=ratings.get(row["id"]), fit=fits.get(row["id"]) if fits else None, compact=compact,
               action=actions.get(row["id"]))
        for index, row in enumerate(rows)
    )


# Feed -----------------------------------------------------------------------------------------------


def _match_expression(text: str) -> str:
    """An FTS5 query: every word must appear; the last one may be the start of a word (search as you type)."""
    words = re.findall(r"\w+", text)
    if not words:
        return ""
    return " ".join([*(f'"{word}"' for word in words[:-1]), f'"{words[-1]}"*'])


def _feed_filters(query: dict[str, list[str]]) -> dict[str, str]:
    filters = {name: (query.get(name) or [""])[0].strip() for name in (*FACETS, "q", "since")}
    if filters["source"] == "discovered":  # the old "discovered" source link
        filters["source"], filters["trust"] = "", "discovered"
    filters["since"] = _instant(filters["since"]) or ""
    if filters["rating"] not in RATING_LABELS:
        filters["rating"] = ""
    return filters


def _feed_url(filters: dict[str, str], **changes: str) -> str:
    values = {key: value for key, value in (filters | changes).items() if value}
    return "/?" + urlencode(values) if values else "/"


def _where(settings: Settings, filters: dict[str, str], skip: str = "") -> tuple[str, list]:
    """SQL condition on items `i` for the filters, without the one named `skip` (a facet's own counts
    ignore its own choice)."""
    clauses, parameters = ["1"], []
    for name in ("source", "trust", "tier", "kind", "category", "author"):
        if name != skip and filters[name]:
            clauses.append(f"i.{name} = ?")
            parameters.append(filters[name])
    if skip != "project" and filters["project"]:
        clauses.append("i.id IN (SELECT item_id FROM relevance WHERE project = ? AND score >= ?)")
        parameters += [filters["project"], settings.relevance_threshold]
    if skip != "rating":  # β items leave the default feed; the Rated β filter still finds them
        clauses.append({"alpha": "i.id IN (SELECT item_id FROM ratings WHERE rating = 'alpha')",
                        "beta": "i.id IN (SELECT item_id FROM ratings WHERE rating = 'beta')",
                        "unrated": "i.id NOT IN (SELECT item_id FROM ratings)"}.get(
            filters["rating"], "i.id NOT IN (SELECT item_id FROM ratings WHERE rating = 'beta')"))
    if skip != "since" and filters["since"]:
        clauses.append("i.found_at > ?")
        parameters.append(filters["since"])
    if skip != "q" and (expression := _match_expression(filters["q"])):
        clauses.append("i.rowid IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
        parameters.append(expression)
    return " AND ".join(clauses), parameters


def _facet_option(filters: dict[str, str], name: str, value: str, label: str, count: int) -> str:
    """One filter option with how many items it would leave. An option that would leave none stays on screen,
    grayed with its 0, instead of disappearing (https://interfacelineage.com/faceted-search: graying them out
    "with their zero would keep that fact on screen"); only the chosen one stays a link, to take it off."""
    on = filters[name] == value
    if count == 0 and not on:
        return (f'<span class="zero" aria-disabled="true" title="No items with the other filters">'
                f'<span>{escape(label)}</span><span class="n">0</span></span>')
    return (f'<a href="{escape(_feed_url(filters, **{name: "" if on else value}))}" '
            f'class="{"on" if on else ""}{" found" if value == "discovered" else ""}">'
            f'<span>{escape(label)}</span><span class="n">{count:,}</span></a>')


def _facet_label(name: str, value: str) -> str:
    labels = {"trust": TRUST_LABELS, "tier": TIER_LABELS, "rating": RATING_LABELS, "kind": KIND_LABELS}.get(name, {})
    return labels.get(value) or (_source_label(value) if name == "source" else value)


def _facets(connection: sqlite3.Connection, settings: Settings, filters: dict[str, str]) -> str:
    groups = []
    for name, title in (("trust", "Who picked it"), ("tier", "The judges"), ("rating", "Your rating"),
                        ("source", "Source"), ("kind", "Kind"), ("category", "Category"), ("project", "Fits project")):
        where, parameters = _where(settings, filters, skip=name)
        if name == "rating":
            rows = connection.execute(
                "SELECT COALESCE(r.rating, 'unrated') AS value, COUNT(*) AS n FROM items i "
                f"LEFT JOIN ratings r ON r.item_id = i.id WHERE {where} GROUP BY value",
                parameters,
            ).fetchall()
            present = list(RATING_LABELS)
        elif name == "project":
            rows = connection.execute(
                f"SELECT r.project AS value, COUNT(*) AS n FROM relevance r JOIN items i ON i.id = r.item_id "
                f"WHERE r.score >= ? AND {where} GROUP BY r.project ORDER BY n DESC, r.project",
                (settings.relevance_threshold, *parameters),
            ).fetchall()
            present = [row[0] for row in connection.execute(
                "SELECT DISTINCT project FROM relevance WHERE score >= ? ORDER BY project", (settings.relevance_threshold,))]
        else:
            rows = connection.execute(
                f"SELECT i.{name} AS value, COUNT(*) AS n FROM items i WHERE {where} AND i.{name} IS NOT NULL "
                f"GROUP BY i.{name} ORDER BY n DESC, i.{name}",
                parameters,
            ).fetchall()
            present = [row[0] for row in connection.execute(
                f"SELECT DISTINCT {name} FROM items WHERE {name} IS NOT NULL ORDER BY {name}")]
        counts = {row["value"]: row["n"] for row in rows}
        # Every value the warehouse holds, in the facet's own order; the counts follow the other filters.
        order = {"trust": list(TRUST_LABELS), "tier": list(TIER_LABELS), "rating": list(RATING_LABELS), "kind": list(KINDS),
                 "category": list(settings.categories)}.get(name, [])
        values = [value for value in order if value in present]
        values += sorted((value for value in present if value not in order), key=lambda v: (-counts.get(v, 0), v))
        if filters[name] and filters[name] not in values:
            values.append(filters[name])
        if len(values) < 2 and not filters[name]:
            continue
        links = "".join(_facet_option(filters, name, value, _facet_label(name, value), counts.get(value, 0))
                        for value in values)
        groups.append(f'<div class="facet"><h4>{title}</h4>{links}</div>')
    return "".join(groups)


SUGGESTED = (("category", "Category"), ("project", "Project"), ("source", "Source"), ("author", "Author"))


def _suggestions(connection: sqlite3.Connection, settings: Settings, filters: dict[str, str]) -> str:
    """The filter options whose names match the words typed in search (a category, a project, a source, an
    author), each with how many items it would leave, above the results: one tap turns the words into that
    filter. Recognition over recall: the options are shown, not remembered (https://interfacelineage.com/faceted-search)."""
    words = " ".join(filters["q"].split()).lower()
    if len(words) < 2:
        return ""
    like = "%" + words.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    found: list[tuple[str, str]] = []
    categories = list(dict.fromkeys([*settings.categories, *(row[0] for row in connection.execute(
        "SELECT DISTINCT category FROM items WHERE category IS NOT NULL"))]))
    found += [("category", value) for value in categories if words in value.lower()]
    found += [("project", value) for value in _project_names(connection) if words in value.lower()]
    sources = [row[0] for row in connection.execute("SELECT DISTINCT source FROM items")]
    found += [("source", value) for value in sources if words in value.lower() or words in _source_label(value).lower()]
    found += [("author", row[0]) for row in connection.execute(
        "SELECT author FROM items WHERE author LIKE ? ESCAPE '\\' GROUP BY author ORDER BY COUNT(*) DESC LIMIT 5", (like,))]
    found = [(name, value) for name, value in found if filters[name] != value][:10]
    if not found:
        return ""
    base = filters | {"q": ""}
    chips = []
    for name, value in found:
        where, parameters = _where(settings, base | {name: value})
        count = connection.execute(f"SELECT COUNT(*) FROM items i WHERE {where}", parameters).fetchone()[0]
        chips.append(_facet_option(base, name, value, f"{dict(SUGGESTED)[name]} · {_facet_label(name, value)}", count))
    return (f'<div class="suggest facet"><h4>Filters matching “{escape(filters["q"].strip())}”</h4>'
            f'{"".join(chips)}</div>')


def _searchers(settings: Settings) -> list:
    """The discovered sources a request is searched in: enabled, ready, and searching (not reading a feed)."""
    return [source for source in enabled(settings, "discovered")
            if not missing(source) and getattr(source, "SEARCHES", True)]


def _ask_form(connection: sqlite3.Connection, settings: Settings, query: str = "", project: str = "",
              primary: bool = False) -> str:
    """The form that asks Alpha to find something. It opens the readback (GET /requests/confirm), which files
    the request only on Confirm; or it says what Alpha needs before it can take one."""
    if not _searchers(settings):
        return ('<p class="note">To let Alpha look beyond what you saved, enable a discovered source in config.toml '
                "(<code>[sources.substack]</code>, <code>[sources.papers]</code>, <code>[sources.huggingface]</code>, "
                "<code>[sources.hf_following]</code>).</p>")
    choices = "".join(f'<option value="{escape(name)}"{" selected" if name == project else ""}>{escape(name)}</option>'
                      for name in _project_names(connection))
    return (
        '<form class="ask" method="get" action="/requests/confirm">'
        f'<input name="query" value="{escape(query)}" placeholder="What should Alpha look for?" required minlength="3" '
        'aria-label="What Alpha should look for">'
        f'<select name="project" aria-label="For which project"><option value="">Any project</option>{choices}</select>'
        f'<button class="{"primary" if primary else "secondary"}">Ask Alpha to find it</button>'
        '<p class="note">Next you see what Alpha will search and when, before anything is filed; the judge keeps '
        "only what is specific, proven, and usable now.</p></form>"
    )


def _today() -> str:
    """The start of today here, as a stored (UTC) timestamp, so it compares with found_at and seen_at."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).isoformat(timespec="seconds")


def _local_day(timestamp: str) -> date:
    return datetime.fromisoformat(timestamp).astimezone().date()


def _day(day: date) -> str:
    """A day divider's label, in local time: Today, Yesterday, or the date."""
    today = datetime.now().astimezone().date()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{day:%a}, {day:%b} {day.day}" + ("" if day.year == today.year else f", {day.year}")


def _wake(connection: sqlite3.Connection, settings: Settings, due_only: bool = True) -> datetime | None:
    """When the scheduled run next wakes (every RUN_INTERVAL_SECONDS after the last one started); with `due_only`,
    the first wake by which a source is due, when new items can next arrive. None when no source is ready."""
    ready = [source for source in enabled(settings) if not missing(source)]
    if not ready:
        return None
    moment, interval = datetime.now(UTC), timedelta(seconds=RUN_INTERVAL_SECONDS)
    ran = {source.NAME: last_run(connection, source)[0] for source in ready}
    started = [datetime.fromisoformat(value) for value in ran.values() if value]
    wake = max(started) + interval if started else moment
    while wake < moment:
        wake += interval
    if due_only:
        due = min(datetime.fromisoformat(ran[s.NAME]) + timedelta(hours=float(options(settings, s)["every_hours"]))
                  if ran[s.NAME] else moment for s in ready)
        while wake < due:
            wake += interval
    return wake


def _in(moment: datetime) -> str:
    minutes = int((moment - datetime.now(UTC)).total_seconds() // 60)
    return f"in {max(minutes, 0)} min" if minutes < 60 else f"in {round(minutes / 60)} h"


def _next_run(connection: sqlite3.Connection, settings: Settings) -> str:
    """When new items can next arrive."""
    wake = _wake(connection, settings)
    return f"next run {_in(wake)}" if wake else "no source is enabled"


def _policy(connection: sqlite3.Connection) -> str:
    """What the default feed is, said where the list begins; each count opens what it counts, and what the judge
    turned down today is one tap away (https://interfacelineage.com/facebook-news-feed: a feed sorted newest first
    hides that it was selected)."""
    today = _today()
    found = connection.execute("SELECT COUNT(*) FROM items WHERE trust = 'discovered' AND found_at > ?",
                               (today,)).fetchone()[0]
    read = connection.execute("SELECT COUNT(*) FROM candidates WHERE seen_at > ?", (today,)).fetchone()[0]
    saved = connection.execute("SELECT COUNT(*) FROM items WHERE trust = 'curated'").fetchone()[0]
    hidden = connection.execute("SELECT COUNT(*) FROM ratings WHERE rating = 'beta'").fetchone()[0]
    ideas = connection.execute("SELECT COUNT(*) FROM items i WHERE tier = 'idea' AND NOT EXISTS "
                               "(SELECT 1 FROM ratings r WHERE r.item_id = i.id AND r.rating = 'beta')").fetchone()[0]
    parts = [
        "Newest first",
        f'<a href="{escape(_feed_url({"trust": "discovered", "since": today}))}"><b>{found:,}</b> found by Alpha today</a> '
        f'out of <a href="/rejected?{escape(urlencode({"since": today}))}" title="What the judge turned down">'
        f"<b>{read:,}</b> it read</a>",
        f'<a href="{escape(_feed_url({"trust": "curated"}))}"><b>{saved:,}</b> saved by you</a>',
    ]
    if ideas:
        parts.append(f'<a href="{escape(_feed_url({"tier": "idea"}))}" title="Found by Alpha; one judge said yes">'
                     f"<b>{ideas:,}</b> idea{'s' if ideas != 1 else ''}</a>")
    if hidden:
        parts.append(f'<a href="{escape(_feed_url({"rating": "beta"}))}"><b>{hidden:,}</b> rated β, hidden</a>')
    return f'<p class="tally policy">{" · ".join(parts)}</p>'


def _landmarks(connection: sqlite3.Connection, settings: Settings, filters: dict[str, str], rows: list[sqlite3.Row],
               offset: int, seen: str, where: str, parameters: list) -> dict[int, str]:
    """Landmarks in a newest-first list: a divider for each day Alpha stored items, and where the items new since
    your last visit end, a card that says you are caught up, then Older (https://interfacelineage.com/infinite-scroll:
    an endless feed has no place to stop).

    The list is in rank order, and found dates are not always monotonic with it: bookmarks imported today rank by
    their place in the X list, below older finds. So a day's divider comes only where the list reaches a day older
    than every day labeled above it, and a label is never shown twice."""
    previous = oldest = None
    if offset:  # the rows above this page: the one just above (new items), and the oldest day already labeled
        above = connection.execute(
            f"SELECT (SELECT i.found_at FROM items i WHERE {where} ORDER BY i.rank DESC LIMIT 1 OFFSET ?), "
            f"(SELECT MIN(found_at) FROM (SELECT i.found_at FROM items i WHERE {where} ORDER BY i.rank DESC LIMIT ?))",
            (*parameters, offset - 1, *parameters, offset)).fetchone()
        previous, oldest = above[0], _local_day(above[1]) if above[1] else None
    ends = not filters["since"]
    marks: dict[int, str] = defaultdict(str)
    for index, row in enumerate(rows):
        if ends and row["found_at"] <= seen and (previous is None or previous > seen):
            ends = False
            marks[index] += (f'<li class="caught-up"><p><span class="tick" aria-hidden="true">✓</span>'
                             f"<b>You’re caught up</b> · {escape(_next_run(connection, settings))}</p></li>"
                             '<li class="older"><h2>Older</h2></li>')
        day = _local_day(row["found_at"])
        if oldest is None or day < oldest:
            marks[index] += f'<li class="day"><h2>{_day(day)}</h2></li>'
            oldest = day
        previous = row["found_at"]
    return marks



def _feed_results(connection: sqlite3.Connection, settings: Settings, filters: dict[str, str], offset: int,
                  seen: str) -> str:
    expression = _match_expression(filters["q"])
    where, parameters = _where(settings, filters, skip="q")
    if expression:
        rows = connection.execute(
            f"SELECT i.*, {JUDGE_COLUMNS}, highlight(items_fts, 1, ?, ?) AS hit_title, "
            "highlight(items_fts, 2, ?, ?) AS hit_summary, snippet(items_fts, 3, ?, ?, '', 18) AS hit_text, "
            "snippet(items_fts, 4, ?, ?, '', 18) AS hit_content "
            "FROM items_fts JOIN items i ON i.rowid = items_fts.rowid LEFT JOIN candidates c ON c.id = i.id "
            f"WHERE items_fts MATCH ? AND {where} ORDER BY items_fts.rank LIMIT ? OFFSET ?",
            (*[OPEN, CLOSE] * 4, expression, *parameters, PAGE_SIZE, offset),
        ).fetchall()
    else:
        rows = connection.execute(
            f"SELECT i.*, {JUDGE_COLUMNS} FROM items i LEFT JOIN candidates c ON c.id = i.id "
            f"WHERE {where} ORDER BY i.rank DESC LIMIT ? OFFSET ?",
            (*parameters, PAGE_SIZE, offset),
        ).fetchall()
    full_where, full_parameters = _where(settings, filters)
    total = connection.execute(f"SELECT COUNT(*) FROM items i WHERE {full_where}", full_parameters).fetchone()[0]
    chosen = [(name, value) for name, value in filters.items() if value and name != "q"]
    pills = "".join(
        f'<a class="pill" href="{escape(_feed_url(filters, **{name: ""}))}" aria-label="Remove filter">'
        f'{escape(_pill_label(name, value))}'
        '<span aria-hidden="true">×</span></a>'
        for name, value in chosen
    )
    if chosen:
        pills += f'<a class="clear" href="{escape(_feed_url({"q": filters["q"]}))}">Clear filters</a>'
    noun = "item" if total == 1 else "items"
    order = "best match first" if expression else "newest first"
    searched = f" “{escape(filters['q'])}”" if expression else ""
    tally = f'<p class="tally"><b>{total:,}</b> {noun}{" matching" + searched if expression else ""} · {order}</p>'
    if not expression and not chosen:
        tally = _policy(connection)
    since = ""
    if not filters["since"] and offset == 0:
        fresh, fitting = _fresh(connection, settings, seen, full_where, full_parameters)
        if fresh:
            since = (
                f'<div class="since"><p><b>{fresh:,}</b> new{" here" if chosen or expression else ""} since your last '
                f'visit ({_ago(seen)})' + (f' · <b>{fitting:,}</b> fit a project' if fitting else "") + "</p>"
                f'<a href="{escape(_feed_url(filters, since=seen))}">Show only new</a>'
                '<form method="post" action="/seen"><button class="quiet">Mark all seen</button></form></div>'
            )
    suggest = _suggestions(connection, settings, filters) if expression and offset == 0 else ""
    if not rows and offset == 0:
        if expression or chosen:
            fixes = []
            if expression:
                fixes.append(f'<a href="{escape(_feed_url(filters, q=""))}">Clear the search</a>')
            if chosen:
                fixes.append(f'<a href="{escape(_feed_url({"q": filters["q"]}))}">Clear filters</a>')
            ask = ""
            if expression:
                ask = ("<p>Or ask Alpha to go find it:</p>"
                       + _ask_form(connection, settings, filters["q"], filters["project"], primary=True))
            body = (f'<div class="empty"><p>Nothing matches{searched} with these filters.</p>'
                    f'<p>{" or ".join(fixes)}.</p>{ask}</div>')
        else:
            body = ('<div class="empty"><p>No items yet.</p><p>Run <code>uv run alpha run</code> to collect from your '
                    "sources; items appear here as soon as they are stored.</p></div>")
        return f"{suggest}{tally}<div class=pills>{pills}</div>{body}"
    more = ""
    if offset + len(rows) < total:
        more = (f'<a class="more" href="{escape(_feed_url(filters, offset=str(offset + PAGE_SIZE)))}">'
                f"Show {min(PAGE_SIZE, total - offset - len(rows))} more</a>")
    ask = ""
    if expression and offset == 0:
        ask = (f'<section class="ask-more"><h4>Not what you need?</h4>'
               f'{_ask_form(connection, settings, filters["q"], filters["project"])}</section>')
    start = f' start="{offset + 1}"' if offset else ""
    marks = {} if expression else _landmarks(connection, settings, filters, rows, offset, seen, where, parameters)
    return (f'{suggest}{since}{tally}<div class="pills">{pills}</div><ol class="rows"{start}>'
            f"{_rows(connection, settings, rows, seen, marks=marks, compact=True)}</ol>{more}{ask}")


def _pill_label(name: str, value: str) -> str:
    if name == "since":
        return f"New since {_moment(value)}"
    if name == "source":
        return _source_label(value)
    if name == "author":
        return f"by {value}"
    return {"trust": TRUST_LABELS, "tier": TIER_LABELS, "rating": RATING_LABELS,
            "kind": KIND_LABELS}.get(name, {}).get(value) or value


def feed_page(settings: Settings, query: dict[str, list[str]], seen: str) -> str:
    """The stream and nothing else: search and the filters wait behind one control, open while a search is on.
    Until the first rating, α and β say their words on every screen."""
    filters = _feed_filters(query)
    offset = int(value) if (value := (query.get("offset") or ["0"])[0]).isdigit() else 0
    connection = _connect(settings)
    try:
        hidden = "".join(f'<input type="hidden" name="{name}" value="{escape(filters[name])}">'
                         for name in (*FACETS, "since") if filters[name])
        first_use = " first-use" if not connection.execute("SELECT 1 FROM ratings LIMIT 1").fetchone() else ""
        main = (
            f'<div class="feed{first_use}">'
            f'<details class="finder" id="finder"{" open" if filters["q"] else ""}>'
            '<summary>Search and filter <kbd>/</kbd></summary>'
            '<form class="search" action="/" method="get" role="search">'
            '<input type="search" name="q" id="q" placeholder="Search everything you kept and found" autocomplete="off" '
            f'value="{escape(filters["q"])}" aria-label="Search">{hidden}</form>'
            f'<div class="facet-groups">{_facets(connection, settings, filters)}</div></details>'
            f'<div id="results" aria-live="polite">{_feed_results(connection, settings, filters, offset, seen)}</div>'
            "</div>"
        )
        title = f"Search: {filters['q']}" if filters["q"] else "Feed"
        return _layout(connection, settings, "feed", title, main, seen)
    finally:
        connection.close()


# Item -----------------------------------------------------------------------------------------------


def _item_content(row: sqlite3.Row, content: list[sqlite3.Row]) -> str:
    """The item's text, its images with their transcriptions, thread, replies, quoted post, article, linked
    pages, and anything else a source kept (shown under its kind)."""
    parts = [f'<section class="block"><h4>{escape(row["kind"].capitalize())}</h4>{_text(row["text"] or "")}']
    figures = []
    for item in content:
        if item["kind"] != "image":
            continue
        source = _media_url(json.loads(item["detail"] or "{}"))
        if not source:
            continue
        transcription = (f"<details><summary>Text in the image</summary>{_text(item['text'])}</details>"
                         if item["text"] else "")
        figures.append(f'<figure><a href="{escape(source)}" target="_blank" rel="noopener"><img src="{escape(source)}" '
                       f'loading="lazy" alt=""></a>{transcription}</figure>')
    if figures:
        parts.append(f'<div class="figures">{"".join(figures)}</div>')
    parts.append("</section>")
    for item in content:
        if not item["text"] or item["kind"] in ("image", "link"):
            continue
        if item["kind"] in CONTENT_HEADINGS:
            parts.append(f'<section class="block"><h4>{CONTENT_HEADINGS[item["kind"]]}</h4>{_text(item["text"])}</section>')
        elif item["kind"] == "article":
            title = json.loads(item["detail"] or "{}").get("title") or "Article"
            parts.append(f'<section class="block"><details class="long"><summary>{escape(title)}'
                         f'<span class="note">full article</span></summary>{_text(item["text"])}</details></section>')
        else:
            heading = item["kind"].replace("_", " ").capitalize()
            parts.append(f'<section class="block"><details class="long"><summary>{escape(heading)}</summary>'
                         f'{_text(item["text"])}</details></section>')
    pages = {item["key"]: item for item in content if item["kind"] == "link"}
    links = [(link["url"], link.get("title")) for link in json.loads(row["links"] or "[]")]
    links += [(url, None) for url in pages if url not in {url for url, _ in links}]
    if links:
        entries = []
        for url, title in links:
            text = pages[url]["text"] if url in pages else None
            body = f"<details><summary>Page text</summary>{_text(text)}</details>" if text else ""
            entries.append(f'<li><a href="{escape(url)}" target="_blank" rel="noopener">{escape(title or url)}</a>{body}</li>')
        parts.append(f'<section class="block"><h4>Links</h4><ul class="links">{"".join(entries)}</ul></section>')
    return "".join(parts)


def _skill_section(connection: sqlite3.Connection, settings: Settings, item_id: str, error: str) -> str:
    """The skill chat: Make a skill, the conversation, the current plan, and Build."""
    chat = skill_chat.get(connection, item_id)
    plan = skill_chat.plan_of(chat)
    action = f"/item/{quote(item_id)}"
    parts = ["<h4>Make a skill</h4>"]
    if error:
        parts.append(f'<p class="error">Something went wrong: {escape(error)}</p>')
    if not chat:
        parts.append(
            '<p class="note">Claude proposes a skill built from this item and asks what it needs to know. '
            "Answer until the plan matches what you have in mind, then build it.</p>"
            f'<form class="wait stack" data-wait="Thinking…" method="post" action="{action}/chat">'
            '<textarea name="message" rows="3" placeholder="Optional: what you have in mind for the skill"></textarea>'
            '<button class="primary">Make a skill</button></form>'
        )
        return "".join(parts)
    bubbles = [
        f'<div class="bubble you">{escape(m["content"])}</div>' if m["role"] == "user"
        else f'<div class="bubble">{escape(SkillPlan.model_validate_json(m["content"]).reply)}</div>'
        for m in json.loads(chat["messages"])
    ]
    parts.append(f'<div class="chat">{"".join(bubbles)}</div>')
    if plan is None:
        parts.append(f'<form class="wait stack" data-wait="Thinking…" method="post" action="{action}/chat">'
                     '<textarea name="message" rows="3" required placeholder="Say how you want the skill to work (⌘↩ sends)">'
                     '</textarea><button class="primary">Send</button></form>')
        return "".join(parts)
    topic = connection.execute("SELECT * FROM topics WHERE name = ?", (chat["topic"],)).fetchone() if chat["topic"] else None
    sources = ["<li>This item</li>"] + [
        f'<li><a href="/item/{quote(row["id"])}">{escape(row["title"] or "Untitled")}</a></li>'
        for p in plan.post_ids
        if (row := connection.execute("SELECT id, title FROM items WHERE id = ?", (p.strip(),)).fetchone())
    ]
    built = ""
    if topic:
        latest = versions(connection, topic["name"])
        state = f"v{latest[0]['version']} {latest[0]['status']}" if latest else "starting"
        built = (f'<p class="note">Built as <a href="/skills#skill-{quote(topic["name"])}">{escape(topic["name"])}</a>'
                 f" · {state}</p>")
    target = plan.test_target or (topic["test_target"] if topic else "") or ""
    label = f"Rebuild {topic['name']}" if topic else "Build this skill"
    brief = f'<p class="label">Your brief</p>{_text(plan.brief)}' if plan.brief else ""
    parts.append(
        f'<div class="plan"><div class="meta"><span class="chip {"ready" if plan.ready else ""}">'
        f'{"ready to build" if plan.ready else "still planning"}</span>'
        f"<span>{escape(topic['name'] if topic else plan.name)}</span></div>{built}"
        f'<h3>{escape(plan.title)}</h3><p>{escape(plan.description)}</p>{brief}'
        f'<p class="label">Written from</p><ul class="plain">{"".join(sources)}</ul>'
        f'<form class="inline-form" method="get" action="{action}/build">'
        f'<input name="target" required placeholder="Test task: a URL, a repo, or a one-line scenario" value="{escape(target)}" '
        'aria-label="Test task">'
        f'<button class="primary">{escape(label)}…</button></form>'
        '<p class="note">Next you see what the build will do (the skill, its items, the test, the model) and start it '
        "there; it takes 10 to 30 minutes and shows on the Skills tab.</p></div>"
        f'<form class="wait stack" data-wait="Thinking…" method="post" action="{action}/chat">'
        '<textarea name="message" rows="3" required placeholder="Answer, or say how you want the skill to work (⌘↩ sends)">'
        '</textarea><div class="actions"><button class="primary">Send</button></div></form>'
        f'<form method="post" action="{action}/reset" onsubmit="return confirm(\'Start this conversation over?\')">'
        '<button class="quiet">Start over</button></form>'
    )
    return "".join(parts)


def _idea_section(connection: sqlite3.Connection, item_id: str, matched: list[str], suggested: set[str],
                  error: str) -> str:
    """Have an idea: the user names a project and says what they would do with the item there; Claude shapes it
    with them (own_idea.py: the conversation, the current plan, and Make it a suggestion or Implement). Projects
    that fit the item come first, then the rest with a spec or a brief; one that already has a live suggestion is
    not offered (rate or ask about that one). `error`: why the last message, keep, or implement failed."""
    action = f"/item/{quote(item_id)}"
    chat = own_idea.get(connection, item_id)
    plan = own_idea.plan_of(chat)
    parts = ["<h4>Have an idea</h4>"]
    if error:
        parts.append(f'<p class="error">Something went wrong: {escape(error)}</p>')
    if chat is None or plan is None:
        known = own_idea.projects(connection)
        names = [name for name in matched if name not in suggested and name in known]
        names += [name for name in known if name not in suggested and name not in names]
        if not names:
            parts.append('<p class="note">Every project with a spec or a brief already has a suggestion for this item; '
                         "rate or ask about those.</p>")
            return "".join(parts)
        options = "".join(f'<option value="{escape(name)}">{escape(name)}</option>' for name in names)
        parts.append(
            '<p class="note">See a use Alpha did not? Say what you would do with this in a project. Claude shapes it '
            "with you, then keep it as a suggestion or implement it; either way it is rated α in your words and "
            "teaches Alpha what you want items for.</p>"
            f'<form class="wait stack" data-wait="Thinking…" method="post" action="{action}/idea">'
            f'<select name="project" aria-label="Project">{options}</select>'
            '<textarea name="message" rows="3" required placeholder="What you would do with this there (⌘↩ sends)">'
            '</textarea><button class="primary">Have an idea</button></form>'
        )
        return "".join(parts)
    project = chat["project"]
    bubbles = [
        f'<div class="bubble you">{escape(m["content"])}</div>' if m["role"] == "user"
        else f'<div class="bubble">{escape(IdeaPlan.model_validate_json(m["content"]).reply)}</div>'
        for m in own_idea.messages_of(chat)
    ]
    parts.append(f'<p class="note">For <a href="/project/{quote(project)}">{escape(project)}</a></p>'
                 f'<div class="chat">{"".join(bubbles)}</div>')
    kept = ""
    if chat["suggested_at"]:
        anchor = f'/item/{quote(item_id)}#{_ask_anchor(item_id, project)}'
        kept = (f'<p class="note">Now a suggestion for {escape(project)} (<a href="{anchor}">under Fits your '
                f"projects</a>), updated {_ago(chat['suggested_at'])}; keeping it again replaces it with this plan.</p>")
    polish = (f'<p class="label">To make more of it</p><ul class="plain">'
              + "".join(f"<li>{escape(line)}</li>" for line in plan.polish) + "</ul>"
              '<p class="note">Take one by saying so below; it folds into the plan.</p>') if plan.polish else ""
    parts.append(
        f'<div class="plan"><div class="meta"><span class="chip {"ready" if plan.ready else ""}">'
        f'{"ready to implement" if plan.ready else "still shaping"}</span>'
        f"<span>{escape(plan.effort)} effort</span></div>{kept}"
        f"<h3>{escape(plan.headline)}</h3><p>{escape(plan.outcome)}</p>"
        f'<p class="label">The core</p><p>{escape(plan.core)}</p>'
        f'<p class="label">How</p>{_text(plan.action)}{polish}'
        f'<div class="actions"><form method="post" action="{action}/idea-keep"><button class="secondary">'
        "Make it a suggestion</button></form>"
        f'<form method="post" action="{action}/idea-implement"><button class="primary">Implement…</button></form></div>'
        '<p class="note">Make it a suggestion keeps it in the project’s What to do for later. Implement keeps it and '
        "shows what the agent will do before it starts.</p></div>"
        f'<form class="wait stack" data-wait="Thinking…" method="post" action="{action}/idea">'
        '<textarea name="message" rows="3" required placeholder="Take a sharpening, answer, or change the idea (⌘↩ sends)">'
        '</textarea><div class="actions"><button class="primary">Send</button></div></form>'
        f'<form method="post" action="{action}/idea-reset" onsubmit="return confirm(\'Start this idea over?\')">'
        '<button class="quiet">Start over</button></form>'
    )
    return "".join(parts)


def item_page(settings: Settings, item_id: str, notice: str = "", error: str = "", *, seen: str,
              ask_error: tuple[str, str] = ("", ""), idea_error: str = "") -> str | None:
    """One item: everything kept from it, its project matches and skills, the skill chat (not for resources:
    datasets, models, Spaces), Ask about this under each suggestion (`ask_error`: the anchor of the one whose
    last question failed, and why), and Your idea (`idea_error`: why the last one was not written)."""
    connection = _connect(settings)
    try:
        row = connection.execute(
            f"SELECT i.*, {JUDGE_COLUMNS} FROM items i LEFT JOIN candidates c ON c.id = i.id WHERE i.id = ?", (item_id,)
        ).fetchone()
        if not row:
            return None
        idea = row["duplicate_of"] or item_id
        content = connection.execute("SELECT * FROM content WHERE item_id = ? ORDER BY rowid", (item_id,)).fetchall()
        # Each fact about the item starts a Feed filtered to it (https://interfacelineage.com/faceted-search).
        feed = (lambda name, value, text, css="": f'<a class="{css}" href="{escape(_feed_url({name: value}))}" '
                f'title="Feed: {escape(_pill_label(name, value))}">{text}</a>')
        meta = [feed("source", row["source"], escape(_source_label(row["source"])), "src"),
                f"<span>{escape(row['kind'])}</span>"]
        if row["author"]:
            meta.append(feed("author", row["author"],
                             f"{'@' if row['source'] in X_SOURCES else ''}{escape(row['author'])}"))
        if row["created_at"]:
            meta.append(f"<span>posted {_date(row['created_at'])}</span>")
        meta.append(f"<span>kept {_ago(row['found_at'])}</span>")
        if row["category"]:
            meta.append(feed("category", row["category"], escape(row["category"]), "cat"))
        same = ""
        if row["duplicate_of"]:
            original = connection.execute("SELECT title FROM items WHERE id = ?", (idea,)).fetchone()
            same = (f'<p class="repeat">Same idea as <a href="/item/{quote(idea)}">'
                    f'{escape((original["title"] if original else None) or "an earlier item")}</a>, which holds the matches.</p>')
        elif repeats := duplicates_by_original(connection)[idea]:
            links = ", ".join(f'<a href="/item/{quote(p)}">{n}</a>' for n, p in enumerate(repeats, 1))
            same = f'<p class="repeat">The same idea was saved again: {links}</p>'
        matches = connection.execute(
            "SELECT project, score, reason FROM relevance WHERE item_id = ? AND score >= ? ORDER BY score DESC",
            (idea, settings.relevance_threshold),
        ).fetchall()
        suggestions = {s["project"]: s for s in connection.execute(
            "SELECT * FROM suggestions WHERE item_id = ? AND status != 'dismissed'", (idea,))}
        chats = suggestion_chat.all_for(connection, [idea])
        suggested = (lambda project: _suggestion(
            idea, suggestions.get(project), chat=chats.get((idea, project)),
            error=ask_error[1] if ask_error[0] == _ask_anchor(idea, project) else ""))
        projects = "".join(
            f'<li>{feed("project", m["project"], escape(m["project"]))}<b>{m["score"]:.1f}</b>'
            f'<a class="to-project" href="/project/{quote(m["project"])}">brief ›</a>'
            f'<p>{escape(m["reason"] or "Reason pending.")}</p>{suggested(m["project"])}</li>'
            for m in matches
        )
        projects += "".join(  # suggestions for a project that is not a match: found for it, or the user's own idea
            f'<li><a href="/project/{quote(name)}">{escape(name)}</a>'
            f'<span class="note"> {"your idea" if suggestions[name]["basis_kind"] == "user" else "found for"}</span>'
            f"{suggested(name)}</li>"
            for name in suggestions if name not in {m["project"] for m in matches}
        )
        projects = f'<ul class="matches">{projects}</ul>' if projects else '<p class="note">No project fits this one yet.</p>'
        have_idea = _idea_section(connection, idea, [m["project"] for m in matches], set(suggestions), idea_error)
        unrated_idea = row["tier"] == "idea"
        feeds_skills = row["kind"] in SKILL_KINDS and not unrated_idea
        skills_html = (f'<p class="note">{KIND_LABELS.get(row["kind"], row["kind"])} never feed skills; this one shows '
                       "as a resource on the projects it fits.</p>")
        if unrated_idea:
            skills_html = ('<p class="note">An idea (one judge said yes) feeds no skill until you rate it α, which makes '
                           "it a find.</p>")
        if feeds_skills:
            in_skills = connection.execute(
                "SELECT DISTINCT t.name, t.title FROM topic_scores s JOIN topics t ON t.name = s.topic "
                "WHERE s.item_id IN (?, ?) AND s.probability >= ? AND s.excluded = 0 ORDER BY t.name",
                (item_id, idea, settings.topic_threshold),
            ).fetchall()
            addable = connection.execute(
                "SELECT name, title FROM topics WHERE status = 'active' AND name NOT IN "
                "(SELECT topic FROM topic_scores WHERE item_id IN (?, ?) AND probability >= ? AND excluded = 0) ORDER BY name",
                (item_id, idea, settings.topic_threshold),
            ).fetchall()
            skills_html = "".join(
                f'<li><a href="/skills#skill-{quote(s["name"])}">{escape(s["title"])}</a><code>{escape(s["name"])}</code></li>'
                for s in in_skills
            )
            skills_html = (f'<ul class="in-skills">{skills_html}</ul>' if skills_html
                           else '<p class="note">Not part of a skill yet.</p>')
            if addable:
                choices = "".join(f'<option value="{escape(t["name"])}">{escape(t["title"])}</option>' for t in addable)
                skills_html += (
                    f'<form class="inline-form" method="post" action="/item/{quote(item_id)}/add">'
                    f'<select name="topic" aria-label="Skill">{choices}</select>'
                    '<button class="secondary">Add to skill</button></form>'
                )
        if notice:
            skills_html += f'<p class="notice">{escape(notice)}</p>'
        chat = (f'<section class="panel" id="skill">{_skill_section(connection, settings, item_id, error)}</section>'
                if feeds_skills else "")
        chat += f'<section class="panel" id="own-idea">{have_idea}</section>'
        title = _title(row)
        original_title = (f'<p class="source-title">{escape(row["title_source"])}</p>'
                          if row["title_source"] and row["title_source"] != row["title"] else "")
        main = (
            f'<article class="item {escape(row["trust"])}">'
            f'<header class="item-head"><a class="back" href="/">← Feed</a><div class="meta">{"".join(meta)}</div>'
            f"<h1>{escape(title)}</h1>{original_title}"
            f'<p class="lede">{escape(row["summary"] or "Not summarized yet; the next run writes the summary.")}</p>'
            f'{_judged(row, settings, _titles(connection, [row]))}{same}'
            f'<div class="item-actions"><a class="open" href="{escape(row["url"])}" target="_blank" rel="noopener">'
            f"Open on {escape(_source_label(row['source']))} ↗</a>"
            f"{_rate(item_id, _ratings(connection, [item_id]).get(item_id))}</div></header>"
            f'<div class="item-body">{_item_content(row, content)}</div>'
            f'<aside class="item-side"><section class="panel"><h4>Fits your projects · what to do</h4>{projects}</section>'
            f'<section class="panel"><h4>Skills</h4>{skills_html}</section>{chat}</aside></article>'
        )
        return _layout(connection, settings, "feed", title, main, seen)
    finally:
        connection.close()


# Projects ---------------------------------------------------------------------------------------------


def projects_page(settings: Settings, seen: str) -> str:
    """Every project: whether it is active, its brief, and how much fits it; the meetings the briefs may read;
    and what discovery did with the briefs' queries."""
    connection = _connect(settings)
    try:
        scan = json.loads(get_metadata(connection, "focus_scan") or "{}")
        active = {project["name"]: project for project in scan.get("projects", [])}
        briefs = focus.latest_briefs(connection)
        counts = defaultdict(lambda: [0, 0])
        for row in connection.execute(
            f"SELECT r.project, i.kind IN ({', '.join('?' * len(RESOURCE_KINDS))}) AS resource, COUNT(*) AS n "
            "FROM relevance r "
            "JOIN items i ON i.id = r.item_id WHERE r.score >= ? GROUP BY r.project, resource",
            (*RESOURCE_KINDS, settings.relevance_threshold),
        ):
            counts[row["project"]][row["resource"]] = row["n"]
        fresh = {row["project"]: row["n"] for row in connection.execute(
            "SELECT r.project, COUNT(*) AS n FROM relevance r JOIN items i ON i.id = r.item_id "
            "WHERE r.score >= ? AND i.found_at > ? GROUP BY r.project", (settings.relevance_threshold, seen))}
        names = sorted(_project_names(connection), key=lambda name: (name not in active, -counts[name][0], name))
        cards = []
        for name in names:
            brief = briefs.get(name)
            excerpt = escape(" ".join(brief["brief"].split())[:240]) + "…" if brief else "No brief yet."
            state = (f'<span class="chip ready">active</span>{escape(active[name]["reason"])}' if name in active
                     else '<span class="chip">quiet</span>Not active in the last focus scan')
            matched, resources = counts[name]
            cards.append(
                f'<a class="project-card" href="/project/{quote(name)}"><h3>{escape(name)}</h3>'
                f'<p class="state">{state}</p><p class="excerpt">{excerpt}</p>'
                f'<p class="figures-line"><b>{matched}</b> matched items'
                f'{f" · <b class=fresh-n>{fresh[name]} new</b>" if fresh.get(name) else ""} · <b>{resources}</b> resources'
                f'{f" · brief {_ago(brief["created_at"])}" if brief else ""}</p></a>'
            )
        excluded_now = focus.excluded_meetings(connection)  # live, not the state at the last scan
        meetings = "".join(
            f'<li class="{"excluded" if m["id"] in excluded_now else ""}"><span class="when">{escape(m["date"])}</span>'
            f'<span class="what">{escape(m["title"])}</span>'
            f'{"<span class=note>excluded: briefs are rewritten without it on the next run</span>" if m["id"] in excluded_now else ""}'
            f'<form method="post" action="/focus/meeting/{quote(m["id"])}/{"include" if m["id"] in excluded_now else "exclude"}">'
            f'<button class="quiet">{"Use again" if m["id"] in excluded_now else "Exclude"}</button></form></li>'
            for m in scan.get("meetings", [])
        )
        meetings = (f'<ul class="meetings">{meetings}</ul>' if meetings
                    else '<p class="note">No Anarlog meetings in the window (or the meetings feed is off).</p>')
        unassigned = scan.get("unassigned") or {}
        loose = ", ".join(f"{n} {focus.FEEDS[s]}" for s, n in unassigned.items())
        context = settings.context
        totals = {row["status"]: row["n"] for row in
                  connection.execute("SELECT status, COUNT(*) AS n FROM candidates GROUP BY status")}
        judged = totals.get("find", 0) + totals.get("idea", 0) + totals.get("below", 0)
        funnel = "".join(
            f"<div><b>{value:,}</b><span>{label}</span></div>"
            for value, label in ((sum(totals.values()), "candidates seen"),
                                 (totals.get("dropped", 0), "dropped on title and preview"),
                                 (judged, "read and judged"), (totals.get("find", 0), "found"),
                                 (totals.get("idea", 0), "ideas (one judge said yes)"),
                                 (totals.get("passed", 0), "waiting to be read"))
        )
        main = (
            '<header class="page-head"><h1>Projects</h1>'
            f"<p class=\"note\">Briefs last scanned {_ago(scan.get('scanned_at'))}."
            + (f" Not tied to any project at the last rewrite: {escape(loose)}." if loose else "")
            + "</p><details class=\"how\"><summary>How briefs work</summary>"
            "<p>Each active project gets a short brief: what you are working on, why, open problems, and what outside "
            "knowledge would help. Claude writes it from your recent agent prompts, commits, dictations, and meetings "
            f"(the feeds enabled in config.toml; the last {context.days} days, meetings {context.meeting_days}) and "
            f"rewrites it every {context.refresh_days} days or when the activity changes. Your edit is used until the "
            "next rewrite and given to every later one as your correction. Discovered sources search the brief's "
            "queries.</p></details></header>"
            f'<div class="project-grid">{"".join(cards) or "<p class=empty>No projects yet. Run <code>alpha focus</code>.</p>"}</div>'
            '<section class="panel"><h4>Discovery so far</h4>'
            f'<div class="funnel">{funnel}</div><p class="note">Each candidate that fits a brief is read and judged twice, '
            f"independently, by Jev and by Claude ({escape(settings.judge.model)}): sauce or slop? When both say sauce "
            "and it repeats nothing you have, it becomes a find; when only one does, an idea, shown in the Feed with a "
            "quiet Idea mark.</p></section>"
            f'<section class="panel" id="meetings"><h4>Meetings the briefs may read</h4>{meetings}</section>'
        )
        return _layout(connection, settings, "projects", "Projects", main, seen)
    finally:
        connection.close()


def project_page(settings: Settings, name: str, show_all: bool = False, *, seen: str, view: str = "",
                 ask_error: tuple[str, str] = ("", "")) -> str | None:
    """One project: its focus brief (editable), what to do (its suggestions: α first, then unrated; `view` beta or
    done shows those instead; each opens to Ask about this, `ask_error` as on the item page), the items that fit it
    (best fit first; the first PAGE_SIZE unless `show_all`), and datasets, models, and Spaces as resources."""
    connection = _connect(settings)
    try:
        if name not in _project_names(connection):
            return None
        scan = json.loads(get_metadata(connection, "focus_scan") or "{}")
        active = next((project for project in scan.get("projects", []) if project["name"] == name), None)
        brief = focus.latest_briefs(connection).get(name)
        if brief:
            version = connection.execute("SELECT COUNT(*) FROM focus_briefs WHERE project = ?", (name,)).fetchone()[0]
            sources = ", ".join(f"{n} {focus.FEEDS[s]}" for s, n in json.loads(brief["sources"] or "{}").items())
            written = "Your edit" if brief["author"] == "user" else f"Written by Claude from {sources or 'the spec only'}"
            queries = [row["query"] for row in
                       connection.execute("SELECT query FROM queries WHERE brief_id = ?", (brief["id"],))]
            brief_html = (
                f'<div class="brief-text">{_text(brief["brief"])}</div>'
                f'<p class="note">{escape(written)} · {_ago(brief["created_at"])} · version {version}</p>'
                + (f'<p class="label">Discovery searches</p><ul class="queries">'
                   f'{"".join(f"<li>{escape(q)}</li>" for q in queries)}</ul>' if queries else "")
                + f'<details class="edit"><summary>Edit brief</summary>'
                f'<form class="stack" method="post" action="/focus/brief/{quote(name)}">'
                f'<textarea name="brief" rows="14" aria-label="Brief">{escape(brief["brief"])}</textarea>'
                '<div class="actions"><button class="primary">Save brief</button></div></form></details>'
            )
        else:
            brief_html = ('<p class="note">No brief yet; the next discovery run (or <code>alpha focus</code>) writes one '
                          "when the project is active.</p>")
        rows = connection.execute(
            f"SELECT i.*, {JUDGE_COLUMNS}, r.score, r.reason FROM relevance r JOIN items i ON i.id = r.item_id "
            "LEFT JOIN candidates c ON c.id = i.id WHERE r.project = ? AND r.score >= ? "
            "ORDER BY r.score DESC, i.rank DESC",
            (name, settings.relevance_threshold),
        ).fetchall()
        fits = {row["id"]: row for row in rows}
        items = [row for row in rows if row["kind"] not in RESOURCE_KINDS]
        shown = items if show_all else items[:PAGE_SIZE]
        rest = (f'<a class="more" href="/project/{quote(name)}?all=1#matched">Show all {len(items)}</a>'
                if len(shown) < len(items) else "")
        resources = [row for row in rows if row["kind"] in RESOURCE_KINDS]
        state = (f'<span class="chip ready">active</span>{escape(active["reason"])}' if active
                 else '<span class="chip">quiet</span>Not active in the last focus scan')
        view = view if view in SUGGESTION_VIEWS else ""
        condition = {"": "s.status = 'open' AND COALESCE(s.rating, '') != 'beta'",
                     "beta": "s.rating = 'beta'", "done": "s.status = 'done'"}[view]
        counts = {key: connection.execute(
            f"SELECT COUNT(*) FROM suggestions s WHERE s.project = ? AND {where}", (name,)).fetchone()[0]
            for key, where in (("", "s.status = 'open' AND COALESCE(s.rating, '') != 'beta'"),
                               ("beta", "s.rating = 'beta'"), ("done", "s.status = 'done'"))}
        listed = connection.execute(
            f"SELECT s.*, i.title, i.title_source, substr(i.text, 1, 400) AS text, i.source, i.tier FROM suggestions s "
            f"JOIN items i ON i.id = s.item_id WHERE s.project = ? AND {condition} "
            "ORDER BY COALESCE(s.rating = 'alpha', 0) DESC, s.created_at DESC LIMIT ?",
            (name, PAGE_SIZE),
        ).fetchall()
        chats = suggestion_chat.all_for(connection, [s["item_id"] for s in listed])
        rendered = "".join(_suggestion(s["item_id"], s, item=s, chat=chats.get((s["item_id"], name)),
                                       error=ask_error[1] if ask_error[0] == _ask_anchor(s["item_id"], name) else "")
                           for s in listed)
        chips = "".join(
            f'<a href="/project/{quote(name)}{f"?suggestions={key}" if key else ""}#suggestions" '
            f'class="{"on" if key == view else ""}">{label} <span class="n">{counts[key]}</span></a>'
            for key, label in SUGGESTION_VIEWS.items()
        )
        empty = {"": "No suggestions yet. The pipeline writes one for each matched item on its next runs.",
                 "beta": "No suggestion rated β.", "done": "Nothing marked done yet."}[view]
        to_do = (
            f'<section id="suggestions"><h2>What to do <span class="n">suggestions from the items that fit</span></h2>'
            f'<nav class="filter-chips" aria-label="Which suggestions">{chips}</nav>'
            + (f'<ul class="suggestions">{rendered}</ul>'
               if listed else f'<p class="empty">{empty}</p>')
            + "</section>"
        )
        main = (
            f'<header class="page-head"><a class="back" href="/projects">← Projects</a><h1>{escape(name)}</h1>'
            f'<p class="state">{state}</p></header>'
            f'<div class="project-layout"><section class="panel brief" id="brief"><h4>Focus brief</h4>{brief_html}</section>'
            f'<div class="project-lists">{to_do}'
            + (f'<section><h2>Resources <span class="n">{len(resources)}</span></h2>'
               f'<ol class="rows">{_rows(connection, settings, resources, seen, fits)}</ol></section>' if resources else "")
            + f'<section id="matched"><h2>Matched items <span class="n">{len(items)} · best fit first</span></h2>'
            + (f'<ol class="rows">{_rows(connection, settings, shown, seen, fits)}</ol>{rest}' if items else
               '<p class="empty">Nothing fits this project yet. Items are scored against it on every run.</p>')
            + f'<p class="note feed-link"><a href="{escape(_feed_url({"project": name}))}">Search within these on the Feed →</a></p>'
            + ("" if resources else '<h2>Resources <span class="n">0</span></h2><p class="note">No datasets, models, '
               "or Spaces fit this project yet. Those that do show here as resources, never in skills.</p>")
            + "</section></div></div>"
        )
        return _layout(connection, settings, "projects", name, main, seen)
    finally:
        connection.close()


# Skills -----------------------------------------------------------------------------------------------


def _target_form(name: str, action: str, label: str, target: str | None) -> str:
    """The test task and the button that opens the build's readback (GET /skills/<name>/confirm); the build itself
    starts only on Confirm there."""
    return (
        f'<form class="inline-form" method="get" action="/skills/{quote(name)}/confirm">'
        f'<input type="hidden" name="action" value="{escape(action)}">'
        f'<input name="target" required placeholder="Test task: a URL, a repo, or a one-line scenario" value="{escape(target or "")}" '
        'aria-label="Test task">'
        f'<button class="primary">{label}…</button></form>'
    )


def _compare_line(name: str, version: sqlite3.Row, live: sqlite3.Row | None) -> str:
    """A draft update on its card: how it scored against the live version, and the way into the comparison."""
    attempts = json.loads(version["checks"]).get("attempts") or []
    comparison = attempts[-1].get("comparison") if attempts else None
    scored = (f" · {comparison['new_mean']:.2f} against live v{comparison['live_version']}'s "
              f"{comparison['live_mean']:.2f}" if comparison else "")
    since = (f" · v{live['version']} went live since" if comparison and live
             and live["version"] != comparison["live_version"] else "")
    return (f'<p class="version draft"><b>Update ready: v{version["version"]}</b> · started '
            f'{_ago(version["created_at"])}{scored}{since}</p>'
            f'<div class="actions"><a class="button primary" href="/skills/{quote(name)}/compare/{version["version"]}">'
            "Compare with live, side by side</a></div>")


def _checks(version: sqlite3.Row) -> str:
    """What the last attempt of a build found: validation, script tests, and the graded test run (beside
    the live version's, for an update)."""
    attempts = attempts_of(version)
    if not attempts:
        return ""
    last = attempts[-1]
    parts = [f"{len(attempts)} attempt{'s' if len(attempts) > 1 else ''}"]
    if last.get("error"):
        parts.append(f"crashed: {escape(last['error'])}")
    if last.get("validation"):
        parts.append(f"{len(last['validation'])} rule violations")
    elif "validation" in last:
        parts.append("rules passed")
    if scripts := last.get("scripts"):
        parts.append(f"scripts {sum(s['ok'] for s in scripts)}/{len(scripts)} ran")
    comparison = last.get("comparison")
    if grade := last.get("grade"):
        scores = [c["score"] for c in grade["criteria"]]
        against = f" against live v{comparison['live_version']} {comparison['live_mean']:.2f}" if comparison else ""
        parts.append(f"test run {sum(scores) / len(scores):.2f}{against}")
    lines = [f'<p class="note">{" · ".join(parts)}</p>']
    if grade and comparison:
        lines.append(_score_table(grade, last["live_grade"], comparison))
    elif grade:
        lines.append('<ul class="plain">' + "".join(
            f"<li>{escape(c['name'])}: <b>{c['score']:.2f}</b> {escape(c['reason'])}</li>" for c in grade["criteria"]
        ) + "</ul>")
    if problems := last.get("problems"):
        lines.append('<details><summary>Problems found</summary><ul class="plain">' + "".join(
            f"<li>{escape(p)}</li>" for p in problems
        ) + "</ul></details>")
    return "".join(lines)


# A problem on the failed card is one short sentence; the full text stays under Problems found.
PROBLEM_CHARACTERS = 220
FAILURE_HEADINGS = {"test": "Its test run fell short", "crash": "The build crashed",
                    "scripts": "A script failed on the test task", "rules": "It broke the fixed rules"}


def _plain(problem: str) -> str:
    text = " ".join(problem.split())
    if len(text) > PROBLEM_CHARACTERS:
        text = text[:PROBLEM_CHARACTERS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    text = text[:1].upper() + text[1:]
    return text if text[-1:] in (".", "!", "?", "…") else f"{text}."


def _failed_panel(connection: sqlite3.Connection, topic: sqlite3.Row, version: sqlite3.Row) -> str:
    """A failed build you can act on (https://interfacelineage.com/show-every-state: a failure says what happened
    and what to do next): what went wrong in its last attempt, what Alpha already tried, whether it retries on its
    own, and Try again (through the build's readback), Change the plan, and Dismiss."""
    name, number = topic["name"], version["version"]
    checks = json.loads(version["checks"] or "{}")
    attempts = checks.get("attempts") or []
    last, kind = (attempts[-1] if attempts else {}), failure(version)
    if last.get("error"):
        wrong = [f"The build crashed: {last['error']}"]
    elif not attempts:
        wrong = ["The build stopped before it recorded an attempt (the process was killed or ran out of time)."]
    else:
        wrong = last.get("problems") or []
    report = (f' <a href="#report-{escape(name)}">Read the test report</a>' if kind == "test" and version["report"] else "")
    # Builds from before automatic repairs (no "repairs" in their attempts) told each revision only the last problems.
    repairing = any("repairs" in attempt for attempt in attempts)
    told = ", each revision told every problem found so far" if repairing and len(attempts) > 1 else ""
    tried = [f"Wrote the skill {len(attempts)} time{'' if len(attempts) == 1 else 's'}{told}."]
    if fed := checks.get("fed_in"):
        tried.append(f"Started from v{fed['version']}'s {len(fed['problems'])} problems.")
    repairs = list(dict.fromkeys(r for attempt in attempts for r in attempt.get("repairs") or []))
    if repairing:
        tried.append("Repaired in code: " + "; ".join(repairs) + "." if repairs else "No automatic repairs were needed.")
    if retry_due(connection, name):
        retry = "Alpha tries again once on its own in the next run, starting from these problems."
    elif checks.get("retry_of"):
        retry = f"This was Alpha's one automatic retry of v{checks['retry_of']}; it does not retry again."
    elif kind == "test":
        retry = "No automatic retry: a test run that fell short waits for you."
    elif kind == "scripts":
        retry = "No automatic retry: a failing script waits for you."
    else:
        retry = "No automatic retry: the skill needs an active topic with a test task."
    target = topic["test_target"]
    try_again = (f'<form method="get" action="/skills/{quote(name)}/confirm"><input type="hidden" name="action" '
                 f'value="build"><input type="hidden" name="target" value="{escape(target)}">'
                 '<button class="primary">Try again…</button></form>' if target else "")
    chat = connection.execute("SELECT item_id FROM skill_chats WHERE topic = ?", (name,)).fetchone()
    change = (f'<a class="button secondary" href="/item/{quote(chat["item_id"])}#skill">Change the plan</a>' if chat else
              '<details class="change-plan"><summary>Change the plan</summary>'
              f'{_target_form(name, "build", "Rebuild and test", target)}</details>')
    dismiss = (f'<form method="post" action="/skills/{quote(name)}/reject/{number}">'
               '<button class="secondary">Dismiss</button></form>')
    return (
        f'<section class="failed-panel"><h4>{FAILURE_HEADINGS[kind]}</h4>'
        f'<p class="label">What went wrong in attempt {len(attempts) or 1}{report}</p>'
        f'<ul class="plain">{"".join(f"<li>{escape(_plain(p))}</li>" for p in wrong)}</ul>'
        f'<p class="label">What Alpha already tried</p><ul class="plain">{"".join(f"<li>{escape(t)}</li>" for t in tried)}</ul>'
        f'<p class="note">{escape(retry)}</p>'
        f'<div class="actions">{try_again}{change}{dismiss}</div>'
        '<p class="note">Try again builds a new version with these problems fed to the writer; you confirm it first. '
        "Dismiss sets this version aside; the skill stays.</p></section>"
    )



def _score_table(grade: dict, live_grade: dict, comparison: dict) -> str:
    """Live and new version's scores per criterion from the one blinded grading call; a criterion more
    than writer.REGRESSION_TOLERANCE below the live one is marked worse."""
    live = {c["name"]: c for c in live_grade["criteria"]}
    rows, reasons = [], []
    for criterion in grade["criteria"]:
        old = live.get(criterion["name"])
        before = old["score"] if old else None
        mark = ""
        if before is not None and round(before - criterion["score"], 3) > REGRESSION_TOLERANCE:
            mark = "worse"
        elif before is not None and criterion["score"] > before:
            mark = "better"
        shown = f"{before:.2f}" if before is not None else "–"
        rows.append(f"<tr><td>{escape(criterion['name'])}</td><td>{shown}</td>"
                    f"<td class='{mark}'>{criterion['score']:.2f}</td></tr>")
        reasons.append(f"<li><b>{escape(criterion['name'])}</b>: live: {escape(old['reason'] if old else '')} "
                       f"New: {escape(criterion['reason'])}</li>")
    worse = "worse" if comparison["new_mean"] < comparison["live_mean"] else ""
    rows.append(f"<tr class='mean'><th>Mean</th><th>{comparison['live_mean']:.2f}</th>"
                f"<th class='{worse}'>{comparison['new_mean']:.2f}</th></tr>")
    verdict = (f"<p class='note'>Graded in one call with the versions unnamed (this one was {comparison['new_label']}): "
               f"{escape(comparison['verdict'])}</p>")
    return (f"<table class='scores'><thead><tr><th>Head to head</th><th>Live v{comparison['live_version']}</th>"
            f"<th>This version</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
            f"<details><summary>Why, per criterion</summary><ul class='plain'>{''.join(reasons)}</ul>{verdict}</details>")


def _post_review(connection: sqlite3.Connection, name: str, version: sqlite3.Row) -> str:
    """The items a version adds and drops against the live one, and those no step cites, each with a
    Remove button that keeps it out of the skill for good."""
    posts = json.loads(version["checks"] or "{}").get("posts")
    if not posts:
        return ""
    unused = set(posts["unused"])
    removed = {row["item_id"] for row in connection.execute(
        "SELECT item_id FROM topic_scores WHERE topic = ? AND excluded = 1", (name,)
    )}

    def entry(item_id: str, note: str) -> str:
        row = connection.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()
        title = escape((row["title"] if row else None) or "Untitled")
        action = ('<span class="note">removed</span>' if item_id in removed else
                  f'<form method="post" action="/skills/{quote(name)}/remove/{quote(item_id)}">'
                  '<button class="quiet danger">Remove</button></form>')
        return (f'<li><a href="/item/{quote(item_id)}">{title}</a>{f"<span class=note>{note}</span>" if note else ""}'
                f"{action}</li>")

    sections = []
    if "added" in posts:
        if posts["added"]:
            sections.append(f"<p class='label'>Added since live v{posts['live_version']}</p><ul class='review'>" + "".join(
                entry(p, "unused: no step cites it, left out" if p in unused else "") for p in posts["added"]
            ) + "</ul>")
        if posts["dropped"]:
            sections.append(f"<p class='label'>Dropped since live v{posts['live_version']}</p><ul class='review'>" + "".join(
                entry(p, "unused: no step cites it" if p in unused else "no longer a member") for p in posts["dropped"]
            ) + "</ul>")
    elif unused:
        sections.append("<p class='label'>Unused: no step cites them, left out</p><ul class='review'>"
                        + "".join(entry(p, "") for p in posts["unused"]) + "</ul>")
    if not sections:
        return ""
    return ("<h4>Items</h4>" + "".join(sections) + "<p class='note'>Remove keeps an item out of this skill for good; "
            "Rebuild to build without it.</p>")


def _overlap_section(connection: sqlite3.Connection) -> str:
    """The last `skills overlap` check: groups of skills whose triggers compete, each with a proposed
    scope, and a button to check again. Nothing here changes a skill."""
    result = overlaps(connection)
    button = ('<form class="wait" data-wait="Starting…" method="post" action="/skills/overlap">'
              f'<button class="secondary">{"Check again" if result else "Check for competing skills"}</button></form>')
    if not result:
        return (f'<section class="panel overlap"><h4>Competing skills</h4><p class="note">Not checked yet. Opus reads '
                "every skill's description and lists the ones an agent could confuse, with a scope for each.</p>"
                f"{button}</section>")

    def label(name: str) -> str:
        notes = [note for note, names in (("active", result["active"]), ("tool-written", result["tool_written"]))
                 if name in names]
        return f"<b>{escape(name)}</b>" + (f" <span class='note'>{', '.join(notes)}</span>" if notes else "")

    groups = "".join(
        f"<details><summary>{' · '.join(label(name) for name in group['skills'])}</summary>"
        f"<p>Overlap: {escape(group['triggers'])}</p><ul class='plain'>"
        + "".join(f"<li>{label(scope['skill'])}: {escape(scope['scope'])}</li>" for scope in group["scopes"])
        + "</ul></details>"
        for group in result["groups"]
    )
    return (
        f'<section class="panel overlap"><h4>Competing skills</h4><p class="note">{len(result["groups"])} groups among '
        f'{result["skills"]} skills ({len(result["active"])} active) · checked {_ago(result["checked_at"])}. Skills you '
        "wrote are never changed here; edit their description by hand. A tool-written skill is scoped against the "
        f"others at its next build.</p><details><summary>Show the {len(result['groups'])} groups</summary>{groups}"
        f"</details>{button}</section>"
    )


def _skill_card(connection: sqlite3.Connection, settings: Settings, topic: sqlite3.Row) -> tuple[str, str]:
    name = topic["name"]
    item_ids = members(connection, settings, name)
    titles = [
        (item_id, (row["title"] if (row := connection.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone())
                   else None))
        for item_id in item_ids
    ]
    all_versions = versions(connection, name)
    live = live_version(connection, name)
    latest = all_versions[0] if all_versions else None
    parts = []
    if topic["status"] == "proposed":
        state = "proposed"
        parts.append(
            '<details class="build"><summary>Build this skill</summary>'
            + _target_form(name, "approve", "Build and test", None)
            + '<p class="note">Opus writes the skill, it is checked against the rules, its scripts run, and a '
            "report-only test runs on the target; it is published if everything passes (10 to 30 minutes).</p></details>"
        )
    else:
        state = "live" if live else (latest["status"] if latest else "active")
        if live:
            path = settings.skills_directory / name / "SKILL.md"
            parts.append(
                f'<p class="note">Live v{live["version"]} · published {_ago(live["published_at"])} · '
                f"<code>{escape(str(path))}</code></p>"
                f"<details><summary>Read the skill</summary><pre>{escape(live['skill_md'])}</pre></details>"
            )
        if latest is not None and (not live or latest["version"] > live["version"]):
            label = {"building": "Building", "draft": "Update ready", "failed": "Failed",
                     "rejected": "Rejected"}.get(latest["status"], latest["status"].capitalize())
            if latest["status"] == "draft" and live:
                state = "update ready"
            elif latest["status"] in ("building", "failed"):
                state = latest["status"]
            if latest["status"] == "draft" and live:
                # The update is read, judged, and published or rejected in the comparison.
                parts.append(_compare_line(name, latest, live))
            else:
                parts.append(f'<p class="version {escape(latest["status"])}"><b>{label}: v{latest["version"]}</b> · '
                             f"started {_ago(latest['created_at'])}</p>")
                if latest["status"] == "building":
                    parts.append('<p class="note">Writing, checking, and testing; refresh in a few minutes.</p>')
                else:
                    if latest["status"] == "failed":
                        parts.append(_failed_panel(connection, topic, latest))
                    parts.append(_checks(latest))
                    parts.append(_post_review(connection, name, latest))
                if latest["report"]:
                    parts.append(f'<details id="report-{escape(name)}"><summary>Test run report</summary>'
                                 f"<pre>{escape(latest['report'])}</pre></details>")
        # Drafts left behind by a later version going live still wait for a call.
        parts += [_compare_line(name, version, live) for version in all_versions
                  if version["status"] == "draft" and live and version["version"] < live["version"]]
        # A failed version's panel carries its own Try again and Change the plan.
        if not latest or latest["status"] not in ("building", "failed"):
            parts.append(_target_form(name, "build", "Rebuild and test", topic["test_target"]))
    chat = connection.execute("SELECT item_id FROM skill_chats WHERE topic = ?", (name,)).fetchone()
    sources = "".join(
        f'<li><a href="/item/{quote(item_id)}">{escape(title or "Untitled")}</a></li>' for item_id, title in titles
    )
    asked = ""
    if topic["brief"]:
        asked += f"<details><summary>What you asked for</summary>{_text(topic['brief'])}</details>"
    if chat:
        asked += (f'<p class="note">Planned in the <a href="/item/{quote(chat["item_id"])}#skill">chat on its item</a>; '
                  "change it there and build again.</p>")
    search = " ".join([name, topic["title"], topic["description"], *(t or "" for _, t in titles)]).lower()
    target = f"<span>test task: {escape(topic['test_target'])}</span>" if topic["test_target"] else ""
    card = (
        f'<article class="skill {state.replace(" ", "-")}" id="skill-{escape(name)}" data-state="{state}" '
        f'data-search="{escape(search)}">'
        f'<div class="meta"><span class="chip state">{state}</span><span>{len(item_ids)} items</span>'
        f"<code>{escape(name)}</code>{target}</div>"
        f'<h3>{escape(topic["title"])}</h3><p class="description">{escape(topic["description"])}</p>{asked}'
        f'{"".join(parts)}'
        f'<details><summary>{len(item_ids)} source items</summary><ul class="plain">{sources}</ul></details></article>'
    )
    return card, state


def skills_page(settings: Settings, notice: str = "", *, seen: str) -> str:
    connection = _connect(settings)
    try:
        topics = connection.execute("SELECT * FROM topics ORDER BY status = 'proposed', name").fetchall()
        built = [_skill_card(connection, settings, topic) for topic in topics]
        order = ["live", "update ready", "building", "failed", "active", "rejected", "draft", "proposed"]
        present = {state for _, state in built}
        states = [state for state in order if state in present] + sorted(present - set(order))
        tally = {state: sum(1 for _, s in built if s == state) for state in states}
        chips = '<button class="on" data-state="">All</button>' + "".join(
            f'<button data-state="{escape(state)}">{escape(state)} <span class="n">{tally[state]}</span></button>'
            for state in states
        )
        cards = "".join(card for card, _ in built)
        main = (
            '<header class="page-head"><h1>Skills</h1>'
            '<details class="how"><summary>How skills are built</summary>'
            "<p>Topics group items that teach one process (posts, articles, papers, and repos; datasets, models, and "
            "Spaces never feed skills). Building one has Opus write the skill, checks it against fixed rules, runs its "
            "scripts and a report-only test on a target you choose, and publishes it to the skill library every agent "
            "reads if it passes. When new items change a live skill, the new version is tested head to head with the "
            "live one and waits here for you to publish; it is offered only if it scores at least as well. To make a "
            "skill from one item, open it and press Make a skill.</p></details>"
            '<div class="actions"><form class="wait" data-wait="Starting…" method="post" action="/skills/propose">'
            '<button class="primary">Propose new topics</button></form></div>'
            f"{f'<p class=notice>{escape(notice)}</p>' if notice else ''}</header>"
            '<div class="toolbar"><input type="search" id="skill-search" placeholder="Filter skills" autocomplete="off" '
            f'aria-label="Filter skills"><div class="chips" id="skill-states">{chips}</div></div>'
            f'<div class="skills" id="skills">{cards or "<p class=empty>No topics yet. Press Propose new topics.</p>"}</div>'
            '<p class="empty" id="skills-none" hidden>No skill matches this filter.</p>'
            f"{_overlap_section(connection)}"
        )
        return _layout(connection, settings, "skills", "Skills", main, seen)
    finally:
        connection.close()


# Requests ---------------------------------------------------------------------------------------------


def requests_page(settings: Settings, seen: str, notice: str = "", error: str = "") -> str:
    """What you and your agents asked discovery to look for, open first, with what each request admitted, and the
    form that files a new one."""
    connection = _connect(settings)
    try:
        rows = connection.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM candidates c WHERE c.request_id = r.id) AS offered FROM requests r "
            "ORDER BY r.status = 'done', r.created_at DESC"
        ).fetchall()

        def entry(request: sqlite3.Row) -> str:
            admitted = connection.execute(
                "SELECT i.id, i.title, i.source, i.kind FROM candidates c JOIN items i ON i.id = c.id "
                "WHERE c.request_id = ? ORDER BY i.rank DESC",
                (request["id"],),
            ).fetchall()
            found = "".join(
                f'<li><a href="/item/{quote(i["id"])}">{escape(i["title"] or "Untitled")}</a>'
                f'<span class="note">{escape(_source_label(i["source"]))} {escape(i["kind"])}</span></li>'
                for i in admitted
            )
            project = (f'<a href="/project/{quote(request["project"])}">{escape(request["project"])}</a>'
                       if request["project"] else "any project")
            handled = (f" · handled {_ago(request['handled_at'])}, {request['result_count'] or 0} admitted"
                       if request["handled_at"] else "")
            return (
                f'<li class="request {escape(request["status"])}"><div class="meta"><span class="chip state">'
                f'{escape(request["status"])}</span><span>for {project}</span><span>asked {_ago(request["created_at"])}'
                f"{handled}</span><span>{request['offered']} candidate{'' if request['offered'] == 1 else 's'}</span></div>"
                f'<p class="query">{escape(request["query"])}</p>'
                + (f'<ul class="plain">{found}</ul>' if found else "")
                + "</li>"
            )

        open_rows = [row for row in rows if row["status"] != "done"]
        done_rows = [row for row in rows if row["status"] == "done"]
        said = (f'<p class="error">{escape(error)}</p>' if error else "") + (
            f'<p class="notice">{escape(notice)}</p>' if notice else "")
        main = (
            '<header class="page-head"><h1>Requests</h1><p>What you and your agents asked Alpha to look for. Each run '
            "searches every enabled discovered source for the open ones, judges what it finds, and lists what it kept "
            f'under the request.</p></header><section class="panel ask-panel" id="ask"><h4>Ask Alpha</h4>'
            f"{_ask_form(connection, settings, primary=True)}{said}"
            '<p class="note">Agents file the same requests over MCP with the <code>request</code> tool.</p></section>'
            f'<section><h2>Open <span class="n">{len(open_rows)}</span></h2>'
            + (f'<ul class="requests">{"".join(entry(r) for r in open_rows)}</ul>' if open_rows
               else '<p class="empty">No open requests. New ones are searched on the next run.</p>')
            + f'</section><section><h2>Handled <span class="n">{len(done_rows)}</span></h2>'
            + (f'<ul class="requests">{"".join(entry(r) for r in done_rows)}</ul>' if done_rows
               else '<p class="empty">Nothing handled yet. What Alpha keeps for a request shows under it here.</p>')
            + "</section>"
        )
        return _layout(connection, settings, "requests", "Requests", main, seen)
    finally:
        connection.close()


# Turned down ------------------------------------------------------------------------------------------

REJECTED = {"": "All", "below": "Read and judged", "dropped": "Dropped on title and preview"}


def rejected_page(settings: Settings, query: dict[str, list[str]], seen: str) -> str:
    """What the judge turned down, newest first, with its why: read-only, so the feed's selection can be checked.
    `since` (the Feed's "out of N it read" link: today) and `status` narrow it."""
    field = (lambda key: (query.get(key) or [""])[0])
    status = field("status") if field("status") in REJECTED else ""
    since = _instant(field("since")) or ""
    offset = int(field("offset")) if field("offset").isdigit() else 0
    clauses, parameters = [f"c.status = '{status}'" if status else "c.status IN ('below', 'dropped')"], []
    if since:
        clauses.append("c.seen_at > ?")
        parameters.append(since)
    where = " AND ".join(clauses)

    def url(**changes: str) -> str:
        values = {key: value for key, value in ({"status": status, "since": since} | changes).items() if value}
        return "/rejected" + (f"?{urlencode(values)}" if values else "")

    connection = _connect(settings)
    try:
        total = connection.execute(f"SELECT COUNT(*) FROM candidates c WHERE {where}", parameters).fetchone()[0]
        rows = connection.execute(
            f"SELECT c.*, COALESCE(c.scored_at, c.seen_at) AS judged_at FROM candidates c WHERE {where} "
            "ORDER BY judged_at DESC LIMIT ? OFFSET ?",
            (*parameters, PAGE_SIZE, offset),
        ).fetchall()
        entries = []
        for row in rows:
            meta = [f'<span class="src">{escape(_source_label(row["source"]))}</span>']
            if row["source"] == "x_timeline" and (tab := TIMELINE_TABS.get(row["query"] or "")):
                meta.append(f"<span>{escape(tab)}</span>")
            if row["kind"] != "post":
                meta.append(f"<span>{escape(row['kind'])}</span>")
            if row["author"]:
                meta.append(f"<span>{'@' if row['source'] in X_SOURCES else ''}{escape(row['author'])}</span>")
            meta.append(f"<span>turned down {_ago(row['judged_at'])}</span>")
            if row["status"] == "below":
                numbers = [f"Jev {row['jev_sauce']:.0%} yes" if row["jev_sauce"] is not None else "",
                           f"Claude {'yes' if row['judge_sauce'] else 'no'}" if row["judge_sauce"] is not None else "",
                           f"new to you {row['novelty']:.0%}" if row["novelty"] is not None else ""]
                verdict = (f'<p class="verdict"><span class="chip">below the bar</span>{escape(row["why"] or "")}</p>'
                           f'<p class="numbers">{" · ".join(filter(None, numbers))}</p>')
            else:
                target = row["fit_project"] or ""
                target = ("your taste" if target == "taste" else "its request" if target.startswith("request:")
                          else target or "any brief")
                verdict = (f'<p class="verdict"><span class="chip">dropped</span>Fit {row["fit"] or 0:.2f} to '
                           f"{escape(target)} on its title and preview; the bar is {settings.judge.fit_threshold:.2f}.</p>")
            entries.append(
                f'<li class="row rejected"><div class="row-body"><div class="meta">{"".join(meta)}</div>'
                f'<h3><a href="{escape(row["url"])}" target="_blank" rel="noopener">{escape(row["title"] or "Untitled")}</a></h3>'
                f"{verdict}</div></li>"
            )
        chips = "".join(f'<a href="{escape(url(status=key, offset=""))}" class="{"on" if key == status else ""}">'
                        f"{escape(label)}</a>" for key, label in REJECTED.items())
        pill = (f'<a class="pill" href="{escape(url(since=""))}" aria-label="Remove filter">Read since '
                f'{escape(_moment(since))}<span aria-hidden="true">×</span></a>' if since else "")
        more = (f'<a class="more" href="{escape(url(offset=str(offset + PAGE_SIZE)))}">Show '
                f"{min(PAGE_SIZE, total - offset - len(rows))} more</a>" if offset + len(rows) < total else "")
        main = (
            '<header class="page-head"><a class="back" href="/">← Feed</a><h1>Turned down</h1>'
            "<p>What Alpha read and left out of the Feed, newest first, with the judge’s reason. A candidate is dropped "
            "when its title and preview fit no brief (or your taste), and stays below the bar when either judge says "
            "no or it repeats something you have.</p></header>"
            f'<nav class="filter-chips" aria-label="Which ones">{chips}</nav>'
            f'<p class="tally"><b>{total:,}</b> turned down · newest first</p><div class="pills">{pill}</div>'
            + (f'<ol class="rows">{"".join(entries)}</ol>{more}' if entries
               else '<p class="empty">Nothing turned down here yet.</p>')
        )
        return _layout(connection, settings, "feed", "Turned down", main, seen)
    finally:
        connection.close()


# Skill update, side by side -----------------------------------------------------------------------------

INLINE = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _inline(text: str) -> str:
    """Escaped text with Markdown's inline code, bold, and links."""
    out, last = [], 0
    for match in INLINE.finditer(text):
        out.append(escape(text[last:match.start()]))
        if match[1] is not None:
            out.append(f"<code>{escape(match[1])}</code>")
        elif match[2] is not None:
            out.append(f"<b>{escape(match[2])}</b>")
        else:
            out.append(f'<a href="{escape(match[4])}" target="_blank" rel="noopener">{escape(match[3])}</a>')
        last = match.end()
    return "".join(out) + escape(text[last:])


def _skill_md(text: str, changed: set[int], mark: str) -> str:
    """SKILL.md made readable line by line (front matter, headings, lists, code, tables, paragraphs), so that each
    changed line (`changed`, by line number) can carry its mark: `del` in the live column, `ins` in the new one,
    as a pull request's diff marks them (https://interfacelineage.com/github-pull-request-review)."""
    lines, front, code = [], False, False
    for number, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if (number == 0 and stripped == "---") or front:
            front = not (front and stripped == "---") if number else True
            kind, body = "fm", escape(line)
        elif stripped.startswith("```"):
            code = not code
            kind, body = "fence", escape(line)
        elif code:
            kind, body = "code", escape(line)
        elif heading := re.match(r"(#{1,6})\s+(.*)", stripped):
            kind, body = f"h{min(len(heading[1]), 4)}", _inline(heading[2])
        elif bullet := re.match(r"(\s*)([-*+]|\d+[.)])\s+(.*)", line):
            depth = min(len(bullet[1].expandtabs(2)) // 2, 4)
            marker = "•" if bullet[2] in "-*+" else escape(bullet[2])
            kind, body = f"li d{depth}", f'<span class="bullet">{marker}</span><span>{_inline(bullet[3])}</span>'
        elif stripped.startswith("|"):
            kind, body = "table", escape(line)
        elif not stripped:
            kind, body = "blank", ""
        else:
            kind, body = "p", _inline(line)
        changed_here = number in changed and kind != "blank"
        said = f'<span class="sr">{"removed: " if mark == "del" else "added: "}</span>' if changed_here else ""
        lines.append(f'<div class="ln {kind}{f" {mark}" if changed_here else ""}">{said}{body}</div>')
    return "".join(lines)


def _criteria(grade: dict | None, against: dict | None = None) -> str:
    """A test run's score per criterion with the grader's reason; beside `against` (the live grade), each new
    score says whether it is better or worse."""
    if not grade:
        return '<p class="note">No graded test run.</p>'
    before = {c["name"]: c["score"] for c in (against or {}).get("criteria", [])}
    rows = []
    for criterion in grade["criteria"]:
        old, score, mark = before.get(criterion["name"]), criterion["score"], ""
        if old is not None and round(old - score, 3) > REGRESSION_TOLERANCE:
            mark = "worse"
        elif old is not None and score > old:
            mark = "better"
        rows.append(f'<li><span class="score {mark}">{score:.2f}</span><b>{escape(criterion["name"].replace("_", " ").capitalize())}</b>'
                    f'{f"<span class=sr> ({mark} than live)</span>" if mark else ""}'
                    f'<p>{escape(criterion["reason"])}</p></li>')
    scores = [c["score"] for c in grade["criteria"]]
    problems = "".join(f"<li>{escape(p)}</li>" for p in grade.get("problems") or [])
    return (f'<p class="mean">Mean <b>{sum(scores) / len(scores):.2f}</b></p><ul class="criteria">{"".join(rows)}</ul>'
            + (f'<details><summary>Problems the grader found</summary><ul class="plain">{problems}</ul></details>'
               if problems else ""))


def _compare_column(side: str, title: str, version: sqlite3.Row, grade: dict | None, against: dict | None,
                    report: str | None, changed: set[int]) -> str:
    mark = "del" if side == "live" else "ins"
    count = sum(1 for line in changed if (version["skill_md"] or "").splitlines()[line].strip())
    noun = "removed or changed" if side == "live" else "new or changed"
    return (
        f'<section class="col {side}" aria-label="{escape(title)}"><header class="col-head"><h2>{escape(title)}</h2>'
        f'<span class="note">{escape(version["status"])} · built {_ago(version["created_at"])} · '
        f'{escape(version["model"] or "")}</span></header>'
        f'<h4>Test run scores</h4>{_criteria(grade, against)}'
        f'<h4>SKILL.md <span class="changed-n {mark}">{count} line{"" if count == 1 else "s"} {noun}</span></h4>'
        f'<div class="skill-md">{_skill_md(version["skill_md"] or "", changed, mark)}</div>'
        f'<details class="report"><summary>Test run report</summary>'
        f'<pre>{escape(report or "No report kept.")}</pre></details></section>'
    )


def compare_page(settings: Settings, name: str, number: int, *, seen: str) -> str | None:
    """A draft update beside the live version it was tested against: each column its SKILL.md (changed lines
    marked), test scores per criterion, and test report; the verdict and Publish / Reject under both. Comparison
    is a view you can leave, and on a phone one column with a Live / New switch
    (https://interfacelineage.com/place-ideas-side-by-side)."""
    connection = _connect(settings)
    try:
        topic = connection.execute("SELECT * FROM topics WHERE name = ?", (name,)).fetchone()
        row = connection.execute("SELECT * FROM skill_versions WHERE topic = ? AND version = ?", (name, number)).fetchone()
        if not topic or not row:
            return None
        checks = json.loads(row["checks"])
        last = (checks.get("attempts") or [{}])[-1]
        comparison = last.get("comparison")
        live_number = ((comparison or {}).get("live_version") or (checks.get("live_test") or {}).get("version")
                       or (checks.get("posts") or {}).get("live_version"))
        old = connection.execute("SELECT * FROM skill_versions WHERE topic = ? AND version = ?",
                                 (name, live_number)).fetchone() if live_number else None
        if old is None:
            return None
        live = live_version(connection, name)
        matcher = difflib.SequenceMatcher(None, (old["skill_md"] or "").splitlines(), (row["skill_md"] or "").splitlines(),
                                          autojunk=False)
        removed, added = set(), set()
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "delete"):
                removed.update(range(i1, i2))
            if tag in ("replace", "insert"):
                added.update(range(j1, j2))
        live_title = f"Live v{old['version']}" if old["status"] == "live" else f"Live then: v{old['version']}"
        new_title = f"New v{row['version']}"
        columns = (_compare_column("live", live_title, old, last.get("live_grade"), None,
                                   (checks.get("live_test") or {}).get("report"), removed)
                   + _compare_column("new", new_title, row, last.get("grade"), last.get("live_grade"), row["report"], added))
        back = f"/skills#skill-{quote(name)}"
        since = ""
        if live and live["version"] != old["version"]:
            since = (f'<p class="note warn">v{live["version"]} has gone live since this build. The scores compare v'
                     f'{row["version"]} with v{old["version"]}, the version live when it was built; publishing it '
                     f"replaces v{live['version']}.</p>")
        verdict = ""
        if comparison:
            worse = comparison["new_mean"] < comparison["live_mean"]
            verdict = (f'<p class="verdict-line"><b class="{"worse" if worse else "better"}">{comparison["new_mean"]:.2f}'
                       f'</b> new against <b>{comparison["live_mean"]:.2f}</b> live · graded in one call with the versions '
                       f"unnamed (the new one was {escape(comparison['new_label'])})</p>"
                       f'<p>{escape(comparison["verdict"])}</p>')
            if comparison.get("regressions"):
                verdict += ('<p class="error">Worse on: ' + escape(", ".join(str(r) for r in comparison["regressions"]))
                            + "</p>")
        actions = f'<a class="button secondary" href="{back}" data-leave>Back to Skills</a>'
        if row["status"] == "draft":
            actions = (f'<form class="wait" data-wait="Publishing…" method="post" '
                       f'action="/skills/{quote(name)}/publish/{row["version"]}">'
                       f'<button class="primary">Publish v{row["version"]}</button></form>'
                       f'<form method="post" action="/skills/{quote(name)}/reject/{row["version"]}">'
                       f'<button class="secondary">Reject v{row["version"]}</button></form>' + actions)
        scripts = diff(connection, name, row["version"]) if live else ""
        main = (
            f'<header class="page-head compare-head"><a class="back" href="{back}" data-leave>← Skills</a>'
            f'<h1>{escape(topic["title"])}</h1><p>Update v{row["version"]} beside v{old["version"]}, tested on the same '
            f'task in the same build: <code>{escape(topic["test_target"] or "")}</code>. Changed lines are marked in '
            "both.</p></header>"
            '<div class="compare">'
            '<input type="radio" name="side" id="side-live" class="sr">'
            '<input type="radio" name="side" id="side-new" class="sr" checked>'
            f'<div class="switch" aria-hidden="true"><label for="side-live">{escape(live_title)}</label>'
            f'<label for="side-new">{escape(new_title)}</label></div>'
            f'<div class="columns">{columns}</div></div>'
            f'<section class="panel decide"><h4>Verdict</h4>{since}{verdict}'
            f'{_post_review(connection, name, row)}'
            + (f"<details><summary>Every file, as a diff from live v{live['version']}</summary><pre>{escape(scripts)}</pre>"
               "</details>" if scripts else "")
            + f'<div class="actions">{actions}</div></section>'
        )
        return _layout(connection, settings, "skills", f"{topic['title']}: v{row['version']} beside v{old['version']}",
                       main, seen)
    finally:
        connection.close()


# Readbacks: what an expensive action will do, before it starts ------------------------------------------


def _readback(rows: list[tuple[str, str]]) -> str:
    return '<dl class="readback">' + "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in rows) + "</dl>"


def _confirm_page(connection: sqlite3.Connection, settings: Settings, view: str, title: str, lede: str, body: str,
                  action: str, fields: dict[str, str], label: str, back: str, seen: str) -> str:
    """A readback and its separate accept (https://interfacelineage.com/interpretation-preview: show the system's
    reading, then make the accept a separate act). Confirm posts `fields` to `action`; Cancel (or Esc) goes back.
    Only builds, discovery requests, Implement, and removing an implementation's branch ask: α / β and other cheap
    actions stay one tap, since an accept asked for all day stops being read (the dossier's "approval fatigue")."""
    hidden = "".join(f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">' for k, v in fields.items())
    main = (
        f'<div class="confirm"><header class="page-head"><a class="back" href="{escape(back)}" data-leave>← Back</a>'
        f"<h1>{escape(title)}</h1><p>{lede}</p></header>{body}"
        f'<div class="actions confirm-actions"><form class="wait" data-wait="Starting…" method="post" '
        f'action="{escape(action)}">{hidden}<button class="primary">{escape(label)}</button></form>'
        f'<a class="button secondary" href="{escape(back)}">Cancel</a></div></div>'
    )
    return _layout(connection, settings, view, title, main, seen)


def _items_list(connection: sqlite3.Connection, item_ids: list[str]) -> str:
    rows = {row["id"]: row for row in connection.execute(
        f"SELECT id, title, title_source, substr(text, 1, 400) AS text, source FROM items WHERE id IN "
        f"({','.join('?' * len(item_ids))})", item_ids)} if item_ids else {}
    entries = "".join(f'<li><a href="/item/{quote(i)}">{escape(_title(rows[i]))}</a>'
                      f'<span class="note">{escape(_source_label(rows[i]["source"]))}</span></li>'
                      for i in item_ids if i in rows)
    return f'<ul class="plain sources-list">{entries}</ul>' if entries else '<p class="note">None yet.</p>'


def _build_terms(connection: sqlite3.Connection, settings: Settings, name: str | None) -> list[tuple[str, str]]:
    """What every build does: the model, the test and its bar, what happens after, and how long it takes."""
    live = live_version(connection, name) if name else None
    last = connection.execute("SELECT model FROM skill_versions WHERE topic = ? AND model IS NOT NULL "
                              "ORDER BY version DESC LIMIT 1", (name,)).fetchone() if name else None
    model = escape(settings.skill_model) + (f' <span class="note">(last build: {escape(last["model"])})</span>'
                                             if last else "")
    after = (f"tested beside live v{live['version']} on the same task; it must score at least as well, then waits on "
             "the Skills tab for you to publish" if live else "goes live in the skill library if it passes")
    return [
        ("Model", f"{model} writes the spec; a headless Claude Code agent follows the skill in report-only mode on "
                  "the test task, and Opus grades the report on six criteria (pass: mean 0.75, none under 0.5)"),
        ("Then", escape(after)),
        ("Time", "10 to 30 minutes; progress shows on the Skills tab"),
    ]


def build_readback_page(settings: Settings, name: str, action: str, target: str, back: str, *,
                        seen: str) -> str | None:
    """Build this skill (approve) or Rebuild (build), read back before it starts."""
    connection = _connect(settings)
    try:
        topic = connection.execute("SELECT * FROM topics WHERE name = ?", (name,)).fetchone()
        if not topic or action not in ("approve", "build"):
            return None
        item_ids = members(connection, settings, name)
        extra = (f" Jev then adds any other item that fits it (probability {settings.topic_threshold:.2f} or more)."
                 if action == "approve" else "")
        building = connection.execute("SELECT 1 FROM skill_versions WHERE topic = ? AND status = 'building'",
                                      (name,)).fetchone()
        rows = [
            ("Skill", f"<b>{escape(topic['title'])}</b> <code>{escape(name)}</code>"),
            ("Scope", escape(topic["description"])),
            (f"Written from {len(item_ids)} item{'' if len(item_ids) == 1 else 's'}",
             _items_list(connection, item_ids) + (f'<p class="note">{escape(extra.strip())}</p>' if extra else "")),
            ("Test task", f"<code>{escape(target)}</code>" if target else '<span class="error">none given</span>'),
        ]
        if topic["brief"]:
            rows.insert(2, ("Your brief", _text(topic["brief"])))
        if (failed := last_failed(connection, name)) and (fed := last_problems(failed)):
            rows.append((f"Starts from v{failed['version']}'s problems",
                         '<ul class="plain">' + "".join(f"<li>{escape(_plain(p))}</li>" for p in fed) + "</ul>"
                         '<p class="note">The writer is told each one, and every earlier problem of that build, so it '
                         "fixes them first and does not bring one back.</p>"))
        rows += _build_terms(connection, settings, name)
        warn = '<p class="error">A build of this skill is already running; starting another waits for it.</p>' if building else ""
        lede = ("Alpha will build and test the first version of this skill." if action == "approve"
                else "Alpha will build and test a new version of this skill.")
        return _confirm_page(connection, settings, "skills", f"Build {topic['title']}?", lede, warn + _readback(rows),
                             f"/skills/{quote(name)}/{action}", {"target": target}, "Confirm: build and test", back, seen)
    finally:
        connection.close()


def chat_build_readback_page(settings: Settings, item_id: str, target: str, back: str, *, seen: str) -> str | None:
    """Build from the skill chat, read back before it starts, with what you said set apart from what Claude
    assumed (https://interfacelineage.com/interpretation-preview: set what the system inferred apart from what the
    user said)."""
    connection = _connect(settings)
    try:
        chat = skill_chat.get(connection, item_id)
        plan = skill_chat.plan_of(chat)
        if plan is None:
            return None
        topic = connection.execute("SELECT * FROM topics WHERE name = ?", (chat["topic"],)).fetchone() if chat["topic"] else None
        name = topic["name"] if topic else (slug(plan.name) or slug(plan.title))
        picked = [p.strip() for p in plan.post_ids if p.strip()]
        item_ids = list(dict.fromkeys([item_id, *picked, *(members(connection, settings, topic["name"]) if topic else [])]))
        said_messages = [m["content"] for m in json.loads(chat["messages"]) if m["role"] == "user" and m["content"].strip()]
        typed = target != (plan.test_target or "")
        said = [("Your brief", _text(plan.brief) if plan.brief else '<span class="note">nothing yet beyond the item</span>')]
        if said_messages:
            said.append(("In the chat", f"<details><summary>Your {len(said_messages)} message"
                         f"{'' if len(said_messages) == 1 else 's'}</summary><ul class=\"plain quotes\">"
                         + "".join(f"<li>“{escape(m)}”</li>" for m in said_messages) + "</ul></details>"))
        if typed:
            said.append(("Test task", f"<code>{escape(target)}</code> (you typed it)"))
        assumed = [
            ("Name", f"<code>{escape(name)}</code>" + ("" if topic else ' <span class="note">(or a free name like it)</span>')),
            ("Title", escape(plan.title)),
            ("Scope", escape(plan.description)),
            ("Other items", _items_list(connection, picked) if picked else '<span class="note">none</span>'),
        ]
        if not typed:
            assumed.append(("Test task", f"<code>{escape(target)}</code> (Claude suggested it)"))
        everything = [
            (f"Written from {len(item_ids)} item{'' if len(item_ids) == 1 else 's'}",
             _items_list(connection, item_ids) + '<p class="note">'
             + ("Items no step cites are left out." if topic else "Jev then adds any other item that fits (probability "
                f"{settings.topic_threshold:.2f} or more); items no step cites are left out.") + "</p>"),
            *_build_terms(connection, settings, topic["name"] if topic else None),
        ]
        body = (f'<div class="readback-split"><section><h4>What you said</h4>{_readback(said)}</section>'
                f'<section class="assumed"><h4>What Claude assumed</h4>{_readback(assumed)}</section></div>'
                f"<h4>What the build does</h4>{_readback(everything)}")
        title = f"Rebuild {topic['name']}?" if topic else f"Build {plan.title}?"
        lede = ("Alpha will write the chat's plan into the skill and build and test a new version." if topic
                else "Alpha will create this skill from the chat's plan and build and test its first version.")
        return _confirm_page(connection, settings, "feed", title, lede, body, f"/item/{quote(item_id)}/build",
                             {"target": target}, "Confirm: build and test", back, seen)
    finally:
        connection.close()


def request_readback_page(settings: Settings, query: str, project: str, back: str, *, seen: str) -> str:
    """Ask Alpha, read back before it is filed: what it will look for, for which project, in which sources, and when."""
    connection = _connect(settings)
    try:
        query = " ".join(query.split())
        project = project if project in _project_names(connection) else ""
        searchers = _searchers(settings)
        wake = _wake(connection, settings, due_only=False)
        when = (f"the next run, {_in(wake)} ({wake.astimezone():%H:%M}); open requests are searched on every run, "
                "whether or not a source is due" if wake else "the next run")
        existing = connection.execute(
            "SELECT id, status FROM requests WHERE lower(query) = lower(?) AND project IS ? AND status != 'done'",
            (query, project or None)).fetchone()
        rows = [
            ("Look for", f"<b>{escape(query)}</b>" if len(query) >= 3 else '<span class="error">a few words, please</span>'),
            ("For", f'<a href="/project/{quote(project)}">{escape(project)}</a>' if project else "any project"),
            ("Searched in", escape(", ".join(_source_label(s.NAME) for s in searchers)) if searchers
             else '<span class="error">no discovered source is enabled and ready</span>'),
            ("When", escape(when)),
            ("Then", "the judge keeps only what is specific, proven, and usable now; what it keeps shows under the "
                     "request on the Requests tab"),
        ]
        warn = (f'<p class="notice">Already asked: request {existing["id"]} is still {escape(existing["status"])}; '
                "Confirm files nothing new.</p>" if existing else "")
        return _confirm_page(connection, settings, "requests", "Ask Alpha?", "Nothing is filed until you confirm.",
                             warn + _readback(rows), "/requests", {"query": query, "project": project},
                             "Confirm: file the request", back, seen)
    finally:
        connection.close()


def _suggestion_named(connection: sqlite3.Connection, item_id: str, project: str) -> tuple[sqlite3.Row, sqlite3.Row] | None:
    suggestion = connection.execute("SELECT * FROM suggestions WHERE item_id = ? AND project = ?",
                                    (item_id, project)).fetchone()
    item = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return (suggestion, item) if suggestion and item else None


def _suggestion_rows(suggestion: sqlite3.Row, item: sqlite3.Row) -> list[tuple[str, str]]:
    """The suggestion and where it came from, at the top of its readbacks and its review."""
    project = suggestion["project"]
    outcome = f'<p class="note">{escape(suggestion["outcome"])}</p>' if suggestion["outcome"] else ""
    return [
        ("Suggestion", f"<b>{escape(implement.headline_of(suggestion))}</b>{outcome}"),
        ("How", escape(suggestion["action"])),
        ("From", f'<a href="/item/{quote(item["id"])}">{escape(_title(item))}</a> '
                 f'<span class="note">{escape(_source_label(item["source"]))} · </span>'
                 f'<a href="{escape(item["url"])}" target="_blank" rel="noopener">{escape(item["url"])} ↗</a>'),
        ("Project", f'<a href="/project/{quote(project)}">{escape(project)}</a>'),
    ]


def implement_readback_page(settings: Settings, item_id: str, project: str, back: str, *, seen: str) -> str | None:
    """Implement, read back before it starts: the suggestion and its item, the repository, branch, and worktree it
    will work in, the model, the time, and what it will never do. A project with no local repository gets a task
    file instead, and the readback says so."""
    connection = _connect(settings)
    try:
        chosen = implement.plan(connection, settings, item_id, project)
        if chosen is None:
            return None
        rows = _suggestion_rows(chosen.suggestion, chosen.item)
        if messages := suggestion_chat.get(connection, item_id, project):
            asked = len(messages) // 2
            rows.append(("Your questions", f"<b>Your questions and answers will be given to the agent</b> ({asked} "
                         f"question{'' if asked == 1 else 's'}); your decisions and constraints in them take "
                         "precedence over the suggestion.<details><summary>Read them</summary><div class=\"chat\">"
                         + "".join(f'<div class="bubble{" you" if m["role"] == "user" else ""}">{escape(m["content"])}'
                                   "</div>" for m in messages) + "</div></details>"))
        minutes = implement.TIME_LIMIT_SECONDS // 60
        if chosen.repository:
            busy = [row for row in connection.execute(
                "SELECT * FROM suggestions WHERE implement_status IN ('queued', 'implementing')")
                if implement.state(row) in ("queued", "implementing")]
            rows += [
                ("Works in", f"<code>{escape(str(chosen.repository))}</code>, from its current "
                             f"<code>{escape(chosen.head)}</code> (uncommitted changes there are not included)"),
                ("New branch", f"<code>{escape(chosen.branch)}</code>, checked out in its own worktree "
                               f"<code>{escape(str(chosen.worktree))}</code>, so your working folder is never touched"),
                ("Agent", f"Claude Code, headless, with <code>{escape(settings.claude_model)}</code> (claude_model): it "
                          "reads the project, edits files in the worktree, and runs the project's tests, checks, and "
                          "read-only commands; no MCP servers, no git commit or push, no deploy"),
                ("Then", "Alpha commits the change on the branch itself, as you (your git identity, no trailers): "
                         f"“Alpha: {escape(chosen.headline)}”, with the item's link. The report and the diff wait "
                         "here under Review changes; merging, deleting, and marking it done stay yours."),
                ("Time", f"usually 5 to 20 minutes, stopped at {minutes}; one implementation runs at a time"
                         + (f" ({len(busy)} running or waiting now, so this one waits its turn)" if busy else "")),
                ("Guarantees", "<b>Report and change on a new branch only.</b> Nothing is pushed, deployed, published, "
                               "sent, or bought."),
            ]
            title, lede, label = (f"Implement in {project}?", "Alpha will carry out this suggestion on a new branch "
                                  "and report what it did.", "Confirm: implement on a new branch")
        else:
            source = connection.execute("SELECT source FROM projects WHERE name = ?", (project,)).fetchone()
            where = (f"its spec is {escape(source['source'])}" if source else "it has no spec in Alpha")
            rows += [
                ("Works in", f'<span class="error">No local repository.</span> {escape(project)} has none in '
                             f"config.toml or under {escape(str(settings.context.projects_directory or 'projects_directory'))}"
                             f" ({where}), so nothing runs."),
                ("Instead", f"Alpha writes a ready-to-run task file, <code>{escape(str(chosen.handoff))}</code>: the "
                            "suggestion, the source item's text, the first step, and how to tell it is done; hand it to "
                            "an agent where the code lives"),
                ("Guarantees", "Nothing runs, and nothing is pushed, deployed, published, sent, or bought."),
            ]
            title, lede, label = (f"Write a task for {project}?", "Alpha will write a task file; nothing runs.",
                                  "Confirm: write the task file")
        warn = ("" if implement.can_start(chosen.suggestion) else
                '<p class="error">This suggestion is already being implemented or has a branch to review; Confirm '
                "starts nothing.</p>")
        action = f"/item/{quote(item_id)}/suggestion/{quote(project, safe='')}/implement"
        return _confirm_page(connection, settings, "projects", title, lede, warn + _readback(rows), action,
                             {"back": back}, label, back, seen)
    finally:
        connection.close()


def remove_readback_page(settings: Settings, item_id: str, project: str, back: str, *, seen: str) -> str | None:
    """Remove an implementation's branch and worktree, asked first."""
    connection = _connect(settings)
    try:
        found = _suggestion_named(connection, item_id, project)
        if found is None or not found[0]["implement_branch"]:
            return None
        suggestion, item = found
        worktree = Path(suggestion["implement_path"] or "")
        rows = _suggestion_rows(suggestion, item) + [
            ("Deletes", f"the branch <code>{escape(suggestion['implement_branch'])}</code> (its commit goes with it "
                        f"unless you merged it) and the worktree <code>{escape(str(worktree))}</code>"),
            ("Keeps", "the suggestion, its rating, and your own working folder; Implement can run again"),
        ]
        action = f"/item/{quote(item_id)}/suggestion/{quote(project, safe='')}/remove"
        return _confirm_page(connection, settings, "projects", "Remove the branch and its worktree?",
                             "Nothing is deleted until you confirm.", _readback(rows), action, {"back": back},
                             "Confirm: remove", back, seen)
    finally:
        connection.close()


def _diff_html(patch: str) -> str:
    """A unified diff made readable: each file under its name, hunks marked, added and removed lines colored."""
    files: list[tuple[str, list[str]]] = []
    for line in patch.splitlines()[:DIFF_LINES]:
        if line.startswith("diff --git "):
            files.append((line.rsplit(" b/", 1)[-1], []))
        elif files and not line.startswith(("index ", "--- ", "+++ ", "new file mode", "deleted file mode",
                                            "similarity index", "rename from", "rename to")):
            kind = ("hunk" if line.startswith("@@") else "ins" if line.startswith("+") else
                    "del" if line.startswith("-") else "ctx")
            files[-1][1].append(f'<span class="ln {kind}">{escape(line) or " "}</span>')
    more = (f'<p class="note">Only the first {DIFF_LINES} lines are shown; the commands below show the rest.</p>'
            if len(patch.splitlines()) > DIFF_LINES else "")
    return "".join(f'<section class="diff-file"><h4><code>{escape(name)}</code></h4><pre class="diff">'
                   f'{"".join(lines)}</pre></section>' for name, lines in files) + more


def _commands(label: str, text: str) -> str:
    return (f'<div class="commands"><p class="label">{label}</p><pre>{escape(text)}</pre>'
            '<button type="button" class="quiet" data-copy>Copy</button></div>')


def review_page(settings: Settings, item_id: str, project: str, *, seen: str) -> str | None:
    """An implementation to review: the agent's report, the diff of its branch against the commit it started from,
    the commands to merge or delete the branch, and Remove (asked first)."""
    connection = _connect(settings)
    try:
        found = _suggestion_named(connection, item_id, project)
        if found is None or not found[0]["implement_branch"]:
            return None
        suggestion, item = found
        branch, worktree = suggestion["implement_branch"], Path(suggestion["implement_path"] or "")
        repo = implement.repository_of(worktree)
        stat, patch = implement.diff(suggestion)
        base = f"/item/{quote(item_id)}/suggestion/{quote(project, safe='')}"
        status = implement.state(suggestion)
        state = {"implemented": f"Implemented {_ago(suggestion['implement_finished_at'])}",
                 "failed": f"Failed: {escape(suggestion['implement_error'] or '')}"}.get(status, escape(status or ""))
        rows = _suggestion_rows(suggestion, item) + [
            ("Branch", f"<code>{escape(branch)}</code> from <code>{escape((suggestion['implement_base'] or '')[:10])}"
                       f"</code> · {suggestion['implement_files'] or 0} files changed"),
            ("Worktree", f'<code class="copyable">{escape(str(worktree))}</code>'),
            ("State", state),
        ]
        report = suggestion["implement_report"]
        changes = (f'<pre class="stat">{escape(stat)}</pre>{_diff_html(patch)}' if patch else
                   '<p class="note">No changes on the branch (the agent changed nothing, so Alpha made no commit), or '
                   "its worktree is gone.</p>")
        commands = ""
        if repo:
            folder, tree = shlex.quote(str(repo)), shlex.quote(str(worktree))
            commands = (
                _commands("Merge it (into the branch checked out there)",
                          f"cd {folder}\ngit merge --no-ff {branch}\ngit worktree remove {tree}\ngit branch -d {branch}")
                + _commands("Or delete it",
                            f"git -C {folder} worktree remove --force {tree}\ngit -C {folder} branch -D {branch}")
            )
        main = (
            f'<div class="review"><header class="page-head"><a class="back" href="/project/{quote(project)}#suggestions">'
            f"← {escape(project)}</a><h1>Review: {escape(implement.headline_of(suggestion))}</h1></header>"
            f"{_readback(rows)}"
            f'<section class="panel"><h4>The agent’s report</h4>'
            f'{_text(report) if report else "<p class=note>No report.</p>"}</section>'
            f'<section class="panel"><h4>Changes</h4>{changes}</section>'
            f'<section class="panel"><h4>Merge or delete</h4><p class="note">Alpha pushes and merges nothing; these are '
            f"yours to run. Marking the suggestion done stays manual.</p>{commands}"
            f'<div class="actions"><a class="button secondary danger" href="{base}/remove">Remove branch and worktree…'
            "</a></div></section></div>"
        )
        return _layout(connection, settings, "projects", f"Review {branch}", main, seen)
    finally:
        connection.close()


# Server -----------------------------------------------------------------------------------------------


def _start(*arguments: str, command: str = "skills") -> None:
    """Run a CLI command (`alpha skills ...` or `alpha implement ...`) detached, so a build or an implementation
    outlives the request; output goes to var/logs/<command>.log."""
    log_path = TOOL_ROOT / "var" / "logs" / f"{command}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log:
        subprocess.Popen(
            [str(Path(sys.executable).parent / "alpha"), command, *arguments],
            cwd=TOOL_ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )


NOT_FOUND = b"<!DOCTYPE html><title>Not found</title><p style='font:16px system-ui;padding:2em'>Not found. <a href='/'>Feed</a></p>"


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes = b"", location: str | None = None,
              content_type: str = "text/html; charset=utf-8", cache: str = "no-store",
              cookies: list[str] | tuple = ()) -> None:
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, folder: Path, name: str, cache: str) -> None:
        path = folder / name
        if "/" in name or name.startswith(".") or not path.is_file():
            self._send(404, NOT_FOUND)
        else:
            self._send(200, path.read_bytes(), content_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                       cache=cache)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path
        if path.startswith("/media/"):
            self._file(load_settings().database_path.parent / "media", unquote(path.removeprefix("/media/")),
                       "max-age=604800, immutable")
            return
        if path.startswith("/static/"):
            self._file(STATIC, unquote(path.removeprefix("/static/")), "max-age=31536000, immutable")
            return
        if path == "/focus":  # the old Focus tab
            self._send(302, location="/projects")
            return
        settings = load_settings()
        if path == "/ledger":  # the script's poll; not a page view, so it leaves the visit alone
            connection = _connect(settings)
            try:
                html = _ledger(connection, settings, _instant((query.get("since") or [""])[0]) or now())
            finally:
                connection.close()
            self._send(200, html.encode())
            return
        seen, cookies = _visit(self.headers.get("Cookie"))
        field = (lambda key: (query.get(key) or [""])[0])
        notice = {
            "propose": "Proposing topics. Refresh in a minute or two.",
            "build": "Started. Jev first checks which other items fit the skill, then the build begins; refresh in a few minutes.",
            "overlap": "Checking the skill library for competing skills. Refresh in a minute or two.",
        }.get(field("started"), "")
        item_id = unquote(path.removeprefix("/item/"))
        ask_error = (field("ask"), field("ask_error"))  # a suggestion's Ask about this whose question got no answer
        if path == "/":
            html = feed_page(settings, query, seen)
        elif path == "/skills":
            html = skills_page(settings, notice, seen=seen)
        elif path == "/projects":
            html = projects_page(settings, seen)
        elif path.startswith("/project/"):
            html = project_page(settings, unquote(path.removeprefix("/project/")), "all" in query, seen=seen,
                                view=field("suggestions"), ask_error=ask_error)
        elif path == "/rejected":
            html = rejected_page(settings, query, seen)
        elif path == "/requests":
            filed = (f"Filed request {field('filed')}. The next run searches for it; what the judge keeps shows under "
                     "it below." if field("filed").isdigit() else
                     f"Already asked: request {field('open')} is still open." if field("open").isdigit() else "")
            html = requests_page(settings, seen, filed, field("error"))
        elif path == "/requests/confirm":
            html = request_readback_page(settings, field("query"), field("project").strip(), self._back("/requests#ask"),
                                         seen=seen)
        elif (parts := path.strip("/").split("/"))[0] == "skills" and len(parts) >= 3:
            name = unquote(parts[1])
            if parts[2] == "compare" and len(parts) == 4 and parts[3].isdigit():
                html = compare_page(settings, name, int(parts[3]), seen=seen)
            elif parts[2] == "confirm" and len(parts) == 3:
                html = build_readback_page(settings, name, field("action"), field("target").strip(),
                                           self._back(f"/skills#skill-{quote(name)}"), seen=seen)
            else:
                html = None
        elif (len(parts) == 5 and parts[0] == "item" and parts[2] == "suggestion" and ITEM_ID.fullmatch(unquote(parts[1]))
              and parts[4] in ("implement", "remove", "review")):
            suggested, project = unquote(parts[1]), unquote(parts[3])
            back = self._back(f"/project/{quote(project)}#suggestions")
            if parts[4] == "review":
                html = review_page(settings, suggested, project, seen=seen)
            else:
                page = implement_readback_page if parts[4] == "implement" else remove_readback_page
                html = page(settings, suggested, project, back, seen=seen)
        elif path.startswith("/item/") and path.endswith("/build") and ITEM_ID.fullmatch(item_id.removesuffix("/build")):
            build_item = item_id.removesuffix("/build")
            html = chat_build_readback_page(settings, build_item, field("target").strip(),
                                            self._back(f"/item/{quote(build_item)}#skill"), seen=seen)
        elif path.startswith("/item/") and ITEM_ID.fullmatch(item_id):
            added = "Added. A live skill gets an update to review on the Skills tab after the next run; Rebuild there to do it now."
            html = item_page(settings, item_id, added if "added" in query else "", field("error"), seen=seen,
                             ask_error=ask_error, idea_error=field("idea_error"))
        else:
            html = None
        self._send(200 if html else 404, html.encode() if html else NOT_FOUND, cookies=cookies if html else ())

    def _item_action(self, item_id: str, action: str, form: dict[str, list[str]]) -> None:
        settings = load_settings()
        connection = connect(settings.database_path)
        base = location = f"/item/{quote(item_id)}"
        field = (lambda key: (form.get(key) or [""])[0].strip())
        try:
            if not connection.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
                self._send(404, NOT_FOUND)
                return
            if action == "chat":
                skill_chat.send(connection, settings, item_id, field("message"))
                location += "#skill"
            elif action == "reset":
                skill_chat.reset(connection, item_id)
                location += "#skill"
            elif action == "build" and field("target"):
                command, name = skill_chat.prepare_build(connection, settings, item_id, field("target"))
                _start(command, name, "--target", field("target"))
                location = "/skills?started=build"
            elif action == "add" and skill_chat.add_to_topic(connection, item_id, field("topic")):
                location += "?added=1"
            elif action in ("idea", "idea-keep", "idea-implement", "idea-reset"):
                try:
                    if action == "idea":
                        own_idea.send(connection, settings, item_id, field("project"), field("message"))
                    elif action == "idea-reset":
                        own_idea.reset(connection, item_id)
                    else:
                        project = own_idea.keep(connection, settings, item_id)
                        if action == "idea-implement":  # the readback; nothing starts before its Confirm
                            location = f"{base}/suggestion/{quote(project, safe='')}/implement"
                except Exception as error:  # shown under Have an idea; it can be sent again (a refusal in plain words)
                    why = str(error) if isinstance(error, ValueError) else f"{type(error).__name__}: {error}"
                    location = f"{base}?{urlencode({'idea_error': why[:300]})}"
                if not location.endswith("/implement"):
                    location += "#own-idea"
            else:
                self._send(404, NOT_FOUND)
                return
        except Exception as error:  # shown on the page; the chat can be retried
            location = f"{base}?{urlencode({'error': f'{type(error).__name__}: {error}'[:300]})}#skill"
        finally:
            connection.close()
        self._send(303, location=location)

    def _focus_action(self, parts: list[str], form: dict[str, list[str]]) -> None:
        """Save an edit of a brief, or exclude (or include again) an Anarlog meeting."""
        connection = connect(load_settings().database_path)
        try:
            if len(parts) == 2 and parts[0] == "brief":
                project = unquote(parts[1])
                focus.save_edit(connection, project, (form.get("brief") or [""])[0])
                location = f"/project/{quote(project)}#brief"
            elif len(parts) == 3 and parts[0] == "meeting" and parts[2] in ("exclude", "include"):
                focus.set_excluded(connection, unquote(parts[1]), parts[2] == "exclude")
                location = "/projects#meetings"
            else:
                self._send(404, NOT_FOUND)
                return
        finally:
            connection.close()
        self._send(303, location=location)

    def _request_action(self, form: dict[str, list[str]]) -> None:
        """File a discovery request from the dashboard's form: the same row the MCP `request` tool writes."""
        settings = load_settings()
        query = " ".join((form.get("query") or [""])[0].split())
        project = (form.get("project") or [""])[0].strip() or None
        error = ""
        if len(query) < 3:
            error = "Say what Alpha should look for (a few words)."
        elif not _searchers(settings):
            error = "No discovered source is enabled and ready, so nothing was filed."
        if error:
            self._send(303, location="/requests?" + urlencode({"error": error}) + "#ask")
            return
        connection = connect(settings.database_path)
        try:
            if project not in (None, *_project_names(connection)):
                project = None
            request_id, status = file_request(connection, query, project)
        finally:
            connection.close()
        self._send(303, location=f"/requests?{'open' if status else 'filed'}={request_id}#ask")

    def _back(self, default: str = "/") -> str:
        """The page a form was on (its Referer), or `default`."""
        referer = urlparse(self.headers.get("Referer") or "")
        if not referer.path:
            return default
        back = referer.path + (f"?{referer.query}" if referer.query else "")
        return back if back.startswith("/") and not back.startswith("//") else default

    def _seen_action(self) -> None:
        """Mark everything stored so far as seen, then go back to the page the button was on."""
        moment = now()
        self._send(303, location=self._back(), cookies=[_cookie(SEEN_COOKIE, moment), _cookie(ACTIVE_COOKIE, moment)])

    def _rate_action(self, item_id: str, action: str, form: dict[str, list[str]]) -> None:
        """Save an α or β (rate; an empty value clears it) or the reason for a β (reason). The script asks for JSON and
        gets the saved state; a plain form goes back to its page."""
        value = (form.get("rating" if action == "rate" else "reason") or [""])[0].strip()
        if value not in ("", *(taste.RATINGS if action == "rate" else taste.REASONS)):
            self._send(400, b"<h1>Unknown rating</h1>")
            return
        connection = connect(load_settings().database_path)
        try:
            if not connection.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
                self._send(404, NOT_FOUND)
                return
            if action == "rate":
                taste.rate(connection, item_id, value or None)
            else:
                taste.rate(connection, item_id, "beta", value or None)
            saved = connection.execute("SELECT rating, reason FROM ratings WHERE item_id = ?", (item_id,)).fetchone()
        finally:
            connection.close()
        if "application/json" in (self.headers.get("Accept") or ""):
            state = {"rating": saved["rating"] if saved else None, "reason": saved["reason"] if saved else None}
            self._send(200, json.dumps(state).encode(), content_type="application/json")
        else:
            self._send(303, location=self._back())

    def _implement_action(self, item_id: str, project: str, action: str, form: dict[str, list[str]]) -> None:
        """Confirm on Implement's readback (implement: start the run in the background, or write the task file for
        a project with no local repository) or on Remove's (remove: delete the branch and its worktree)."""
        settings = load_settings()
        back = (form.get("back") or [""])[0]
        location = back if back.startswith("/") and not back.startswith("//") else f"/project/{quote(project)}#suggestions"
        connection = connect(settings.database_path)
        try:
            chosen = implement.plan(connection, settings, item_id, project)
            if chosen is None:
                self._send(404, NOT_FOUND)
                return
            if action == "remove":
                implement.remove(connection, settings, item_id, project)
                location = f"/project/{quote(project)}#suggestions" if "/review" in location else location
            elif not implement.can_start(chosen.suggestion):
                pass  # already running or waiting for review: Confirm starts nothing
            elif chosen.repository is None:
                implement.handoff(connection, chosen)
            else:
                implement.queue(connection, item_id, project)
                _start(item_id, project, command="implement")
        finally:
            connection.close()
        self._send(303, location=location)

    def _ask_action(self, item_id: str, project: str, action: str, form: dict[str, list[str]]) -> None:
        """Ask about this: a question about a suggestion (ask; Claude answers while the request waits, as in the skill
        chat) or Start over (ask-reset). Back to the page it was asked on, at the suggestion; a failed question comes
        back with its reason."""
        settings = load_settings()
        back = urlparse(self._back(f"/item/{quote(item_id)}"))
        kept = [(key, value) for key, value in parse_qsl(back.query) if key not in ("ask", "ask_error")]
        anchor = _ask_anchor(item_id, project)
        connection = connect(settings.database_path)
        try:
            if not connection.execute("SELECT 1 FROM suggestions WHERE item_id = ? AND project = ?",
                                      (item_id, project)).fetchone():
                self._send(404, NOT_FOUND)
                return
            if action == "ask":
                suggestion_chat.send(connection, settings, item_id, project, (form.get("message") or [""])[0])
            else:
                suggestion_chat.reset(connection, item_id, project)
        except Exception as error:  # shown in the suggestion's Ask about this; the question can be asked again
            kept += [("ask", anchor), ("ask_error", f"{type(error).__name__}: {error}"[:300])]
        finally:
            connection.close()
        self._send(303, location=f"{back.path}{'?' + urlencode(kept) if kept else ''}#{anchor}")

    def _suggestion_action(self, item_id: str, project: str, action: str, form: dict[str, list[str]]) -> None:
        """α / β on a suggestion (rate, reason: as for an item), or Mark done / Reopen (done, reopen)."""
        field = "rating" if action == "rate" else "reason"
        value = (form.get(field) or [""])[0].strip()
        if action in ("rate", "reason") and value not in ("", *(taste.RATINGS if action == "rate" else taste.REASONS)):
            self._send(400, b"<h1>Unknown rating</h1>")
            return
        connection = connect(load_settings().database_path)
        try:
            if not connection.execute("SELECT 1 FROM suggestions WHERE item_id = ? AND project = ?",
                                      (item_id, project)).fetchone():
                self._send(404, NOT_FOUND)
                return
            if action == "rate":
                taste.rate_suggestion(connection, item_id, project, value or None)
            elif action == "reason":
                taste.rate_suggestion(connection, item_id, project, "beta", value or None)
            else:
                connection.execute("UPDATE suggestions SET status = ? WHERE item_id = ? AND project = ?",
                                    ("done" if action == "done" else "open", item_id, project))
                connection.commit()
            saved = connection.execute("SELECT rating, reason FROM suggestions WHERE item_id = ? AND project = ?",
                                       (item_id, project)).fetchone()
        finally:
            connection.close()
        if action in ("rate", "reason") and "application/json" in (self.headers.get("Accept") or ""):
            self._send(200, json.dumps({"rating": saved["rating"], "reason": saved["reason"]}).encode(),
                       content_type="application/json")
        else:
            self._send(303, location=self._back())

    def do_POST(self) -> None:
        # Only this dashboard may start work: a page on another site could otherwise post here.
        origin = self.headers.get("Origin") or ""
        if origin and urlparse(origin).hostname not in ("localhost", "127.0.0.1"):
            self._send(403, b"<h1>Forbidden</h1>")
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode()) if length else {}
        parts = urlparse(self.path).path.strip("/").split("/")
        if (len(parts) == 5 and parts[0] == "item" and parts[2] == "suggestion" and ITEM_ID.fullmatch(unquote(parts[1]))
                and parts[4] in ("ask", "ask-reset")):
            self._ask_action(unquote(parts[1]), unquote(parts[3]), parts[4], form)
            return
        if (len(parts) == 5 and parts[0] == "item" and parts[2] == "suggestion" and ITEM_ID.fullmatch(unquote(parts[1]))
                and parts[4] in ("implement", "remove")):
            self._implement_action(unquote(parts[1]), unquote(parts[3]), parts[4], form)
            return
        if (len(parts) == 5 and parts[0] == "item" and parts[2] == "suggestion" and ITEM_ID.fullmatch(unquote(parts[1]))
                and parts[4] in ("rate", "reason", "done", "reopen")):
            self._suggestion_action(unquote(parts[1]), unquote(parts[3]), parts[4], form)
            return
        if len(parts) == 3 and parts[0] == "item" and parts[2] in ("rate", "reason") and ITEM_ID.fullmatch(unquote(parts[1])):
            self._rate_action(unquote(parts[1]), parts[2], form)
            return
        if len(parts) == 3 and parts[0] == "item" and ITEM_ID.fullmatch(unquote(parts[1])):
            self._item_action(unquote(parts[1]), parts[2], form)
            return
        if parts == ["requests"]:
            self._request_action(form)
            return
        if parts == ["seen"]:
            self._seen_action()
            return
        if parts[0] == "focus":
            self._focus_action(parts[1:], form)
            return
        if parts == ["skills", "propose"]:
            _start("propose")
            self._send(303, location="/skills?started=propose")
            return
        if parts == ["skills", "overlap"]:
            _start("overlap")
            self._send(303, location="/skills?started=overlap")
            return
        if len(parts) >= 3 and parts[0] == "skills":
            name, action = unquote(parts[1]), parts[2]
            connection = _connect(load_settings())
            try:
                exists = connection.execute("SELECT 1 FROM topics WHERE name = ?", (name,)).fetchone()
            finally:
                connection.close()
            target = (form.get("target") or [""])[0].strip()
            if exists and action in ("approve", "build") and target:
                _start(action, name, "--target", target)
                self._send(303, location=f"/skills?started=build#skill-{quote(name)}")
                return
            if exists and action in ("publish", "reject") and len(parts) == 4 and parts[3].isdigit():
                _start(action, name, "--version", parts[3])
                self._send(303, location=f"/skills#skill-{quote(name)}")
                return
            if exists and action == "remove" and len(parts) == 4:
                item_id = unquote(parts[3])
                connection = connect(load_settings().database_path)
                try:
                    if connection.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
                        remove(connection, name, item_id)
                finally:
                    connection.close()
                self._send(303, location=f"/skills#skill-{quote(name)}")
                return
        self._send(404, NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        pass


def serve(host: str, port: int) -> None:
    connect(load_settings().database_path).close()  # create new tables and columns before the read-only pages run
    server = ThreadingHTTPServer((host, port), _Handler)
    print(f"Dashboard: http://{'localhost' if host == '127.0.0.1' else host}:{port}", flush=True)
    server.serve_forever()
