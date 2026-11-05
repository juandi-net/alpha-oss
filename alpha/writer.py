"""Text Claude writes: item summaries, image transcriptions, the reason an item matters to a
project, skill topics, the skills themselves, the grades of their test runs (alone, or a new version
beside the live one), which library skills compete for the same requests, focus briefs, search queries,
and the judge's sauce verdict.

Uses the Messages API, with structured outputs (`messages.parse` + `output_format`,
https://platform.claude.com/docs/en/build-with-claude/structured-outputs) where the answer has fields.
ALPHA_CLAUDE_API_KEY comes from .env; ALPHA_CLAUDE_BASE_URL is optional (unset: api.anthropic.com), for a
local proxy such as CLIProxyAPI that serves a Claude subscription. They are not named ANTHROPIC_*: omp loads
.env from its working folder and would send its own requests there.
"""

import base64
import json
import os
import random
import sqlite3
from collections.abc import Iterable
from functools import cache

import anthropic
from pydantic import BaseModel, Field

from .jev import sauce_definition
from .skill_spec import MARKER, SkillSpec

SUMMARY_SYSTEM = (
    "You summarize items the user saved or Alpha found (posts, articles, repos, models, datasets, papers), for a "
    "personal research archive. Write plain, specific English. The title is the takeaway: what the item lets the user "
    "do or learn, benefit first, in plain words, not the item's name (the name is shown beside it). The summary "
    "names the concrete tool, technique, or claim. Do not invent details the item does not contain; if it is only "
    "a link with no description, say what the link appears to be and that the content was not available."
)

RETITLE_SYSTEM = (
    "Rewrite each item's title as its takeaway: what it lets the user do or learn, benefit first, ten words or "
    "fewer, plain US English for a busy founder. Not the item's name and no model sizes, licenses, or jargon (the "
    "name is shown beside the title). Say only what the summary supports. Return one title per id."
)

REASON_SYSTEM = (
    "You explain to the user why a post they saved on X matters to one of their projects. "
    "Write one or two plain sentences: what in the post is useful, and how it could be applied to this "
    "specific project. Refer to the project's actual goals or components. No preamble."
)

IMAGE_SYSTEM = (
    "You transcribe an image attached to a post the user saved on X, so the image can be searched and "
    "turned into instructions later. Copy every piece of readable text exactly, keeping lists, steps, and "
    "table rows in order. Then, if the image shows a diagram, chart, interface, or process, describe what it "
    "shows in a few plain sentences. If it is only a photo or meme with no information, say so in one line. "
    "No preamble."
)

TOPICS_SYSTEM = (
    "The user collects items about methods they want to put to work: posts and articles, repos they starred, "
    "papers that ship code. Group the items into topics, each of which becomes an agent skill: a playbook an AI "
    "agent follows to do the work for the user (for example, audit and fix a website's SEO, run a cold email "
    "campaign). A topic must be a concrete, repeatable process with steps, checks, or tactics an agent can execute, "
    "backed by at least three items. Use every source: a starred repo or a paper that implements a step belongs "
    "beside the posts that describe it. Skip items that are news, opinion, or announcements with nothing to "
    "execute. An item may belong to more than one topic. Prefer focused topics an agent can finish in one session "
    "over broad fields. For each topic, give a test task from the user's own projects (listed below the items): "
    "something real an agent can work on in report-only mode within 30 minutes, written as a URL, a repository, or "
    "a one-line scenario (for example 'example.com: plan a comparison page against Wix for local trades')."
)

SKILL_SYSTEM = (
    "You design an agent skill from items the user collected (posts, articles, starred repos, papers). An AI agent "
    "will load the skill and execute it on a real task the user names: a website, repository, product, campaign, "
    "or a written scenario. Turn the items' processes, systems, tools, tips, and tricks into a typed skill spec; "
    "code renders SKILL.md from it.\n\n"
    "Rules:\n"
    "- Steps are concrete actions in execution order. Each says what to do, which tools or commands the items "
    "name, observable evidence that it is done, and cites the numbered items that support it.\n"
    "- Keep only what the items support. Where items disagree, say so in a pitfall and give the safer option. "
    "Mark claims the items state without evidence (traffic, rankings, revenue) as the author's claim.\n"
    "- Cite an item only where it supports a step. An item outside this skill's scope (another process or field) "
    "stays uncited; items no step cites are dropped from the skill's sources.\n"
    "- A step that publishes, sends, buys, deletes, deploys, or changes a live system requires approval.\n"
    "- Agents often lack the paid tools and account access the items assume. Every input and step that needs "
    "one gets a free fallback (public pages, free search, the task's own files) and says how much less to "
    "trust it.\n"
    "- The skill must work in report-only mode: the agent proposes changes and the Output says which items are "
    "proposed and which were done.\n"
    "- Ship a script for every mechanical, repeatable part (crawling, fetching, parsing, counting, comparing, "
    "scoring) so every run produces the same result for the same input; leave judgment to the agent. A skill with "
    "no mechanical part ships no scripts. Scripts are complete single Python files with a PEP 723 `# /// script` "
    "block listing dependencies (run with `uv run`), take what they work on (a URL, a path, a query) as "
    "command-line arguments, print a clear text or JSON result, and only read: no subprocess or shell, no file "
    "deletion, no eval/exec, no POST/PUT/PATCH/DELETE requests, no email. Keep requests polite (a User-Agent, a "
    "page limit, a short delay). The smoke command runs the script with small limits so it finishes in under two "
    "minutes: put {target} where the test task goes when the script takes that kind of input (for example a URL "
    "when the task is a URL); otherwise give it a small fixed example input.\n"
    "- The description says what the skill does, then 'Use when ...' with the phrases a request would contain."
)

SKILL_REVISION = (
    "\n\nThis is a new version of an existing skill. Keep step ids, order, and wording unless the items, new "
    "items, or the problems listed below require a change; a reader comparing versions should see only real "
    "changes."
)

GRADE_SYSTEM = (
    "You grade a test run of an agent skill. An agent followed the skill in report-only mode on a test task "
    "and wrote a report. Judge the skill, using the run as evidence, against each criterion from 0 (failed) to 1 "
    "(fully met), with a one-sentence reason. Then list concrete problems in the skill itself (vague, wrong, "
    "blocked, or missing steps) that a revision should fix; an empty list if none."
)

COMPARE_SYSTEM = (
    "You grade test runs of two versions of the same agent skill, labeled A and B in no particular order. On the "
    "same test task, an agent followed each version in report-only mode and wrote a report. Judge each skill, "
    "using its own run as evidence, against each criterion from 0 (failed) to 1 (fully met), with a one-sentence "
    "reason. Use one scale for both, so equal work gets equal scores and a difference in score means a real "
    "difference in the runs. For each, list concrete problems in the skill itself (vague, wrong, blocked, or "
    "missing steps) that a revision should fix. Then say where one version did better than the other and why."
)

NEIGHBORS_SYSTEM = (
    "You check an agent skill against the user's skill library. Pick the library skills whose scope overlaps it: "
    "a request meant for one could make an agent load the other (shared trigger phrases, the same task, or one "
    "doing part of the other's task). Leave out skills that only share a broad field. Return their names exactly "
    "as listed; an empty list if none overlap."
)

SHORTEN_SYSTEM = (
    "You shorten an agent skill's description to fit a character limit. Keep what the skill does, the 'Use when "
    "...' sentence with its most telling trigger phrases, and any 'Not for ...; use <other> instead' that sends "
    "requests to another skill. Cut repetition and secondary triggers; add nothing new. Return only the description."
)

OVERLAPS_SYSTEM = (
    "You review the user's library of agent skills. An agent picks a skill from its name and description, so two "
    "skills whose descriptions would both match one request compete, and the agent may load the weaker one. Find "
    "the groups of skills whose triggers overlap (shared trigger phrases, the same task, or one doing part of the "
    "other's task); ignore skills that only share a broad field. For each group, say which requests trigger more "
    "than one of them, and propose a scope for each skill in the group that ends the overlap: what it is for, and "
    "'Not for ...; use <other skill> instead' where needed. Base the scopes on what each description says the "
    "skill does; do not invent abilities. Name skills exactly as listed."
)

SKILL_CHAT_SYSTEM = (
    "You help the user turn items they collected (posts, articles, starred repos, papers) into an agent skill: a "
    "playbook an AI agent follows to do the work for them on a real task (a website, repository, product, "
    "campaign, or a written scenario). The user opened one item and wants a skill from it. Work out with them what "
    "the skill should do and how: its scope, the test task it runs on, what a good result looks like, their "
    "preferences on tools and approach, what to leave out, and which other items belong in it. Ask at most three "
    "short questions at a time, each on its own numbered line, only about what changes the skill, and "
    "propose sensible defaults instead of asking about everything. If an existing skill already covers this, say so "
    "and suggest adding the item to it. Every answer carries your current plan, updated with everything said so "
    "far. Set ready when the plan is specific enough that a writer could build the skill without asking anything "
    "else; the user may build earlier."
)

# Used when ALPHA_CLAUDE_BASE_URL is unset.
ANTHROPIC_API = "https://api.anthropic.com"
# The model thinks before answering and thinking counts against max_tokens; 512 truncated
# about one reason in twenty mid-JSON.
MAX_TOKENS = 4096
# Long outputs are streamed, so no request-time limit applies; parse refuses above ~21k tokens.
SKILL_MAX_TOKENS = 64000
TOPICS_MAX_TOKENS = 16000
GRADE_CRITERIA = {
    "steps_followed": "Every step was carried out, or the report says why it could not be and what the fallback gave",
    "concrete_findings": "The report contains specific findings or work for the test task, not generic advice",
    "scripts_worked": "The shipped scripts ran and their output was used (1 if the skill ships none)",
    "no_invented_facts": "Nothing in the report is made up; unknowns are marked as unknown",
    "stayed_report_only": "The agent changed nothing outside its working folder and made no purchases or posts",
    "output_complete": "The report has every section the skill's Output lists",
}
PASS_MEAN, PASS_MINIMUM = 0.75, 0.5
# An update must also match the live version graded beside it: a mean at least as high and no criterion
# more than this much lower, so a new version cannot trade one strength for another unnoticed.
REGRESSION_TOLERANCE = 0.1
# Per item, so one long article cannot crowd out the rest of a summary prompt.
SUMMARY_ITEM_CHARACTERS = 6000
CONTENT_LABELS = {
    "image": "Image text",
    "article": "X article",
    "quoted": "Quoted post",
    "replied_to": "Post it replies to",
    "thread": "Rest of the author's thread",
    "author_replies": "The author's replies to comments",
    "link": "Linked page",
}


class Summary(BaseModel):
    title: str = Field(description="Ten words or fewer: the takeaway, benefit first, in plain words (not the item's name)")
    summary: str = Field(description="Two or three sentences: what it is, why it matters, how it works")

class Retitle(BaseModel):
    id: str
    title: str


class Retitles(BaseModel):
    titles: list[Retitle]


def retitle(model: str, items: list[tuple[str, str, str]]) -> dict[str, str]:
    """items: (id, current title, summary). Returns id -> takeaway title, for items titled by their name."""
    content = "\n\n".join(f"id: {item_id}\ntitle: {title}\nsummary: {summary}" for item_id, title, summary in items)
    answer = _stream_parsed(model, RETITLE_SYSTEM, content, Retitles, MAX_TOKENS * 4)
    known = {item_id for item_id, _, _ in items}
    return {t.id: t.title.strip() for t in answer.titles if t.id in known and t.title.strip()}



class Reason(BaseModel):
    reason: str


class TopicProposal(BaseModel):
    name: str = Field(description="Skill folder name: lowercase words joined by hyphens, e.g. local-seo-audit")
    title: str = Field(description="Five words or fewer")
    description: str = Field(description="One sentence: what an agent does with this skill")
    post_ids: list[str] = Field(description="Ids of the items that teach this process")
    test_task: str = Field(description="A URL, a repository, or a one-line scenario from the user's projects to test the skill on")


class Topics(BaseModel):
    topics: list[TopicProposal]


class SkillPlan(BaseModel):
    reply: str = Field(description="What you say to the user: your current understanding in a sentence or two, then any questions")
    ready: bool = Field(description="True when the plan is specific enough to build without more questions")
    name: str = Field(description="Skill folder name: lowercase words joined by hyphens, e.g. local-seo-audit")
    title: str = Field(description="Short title for people")
    description: str = Field(description="What the skill does, then 'Use when ...' with the phrases a request would contain")
    brief: str = Field(
        description="Everything the user wants from this skill, written as instructions to the skill writer: scope, "
        "approach, preferences, exclusions. Only what the user said or agreed to; empty at the start."
    )
    post_ids: list[str] = Field(description="Ids of other items the skill should also be written from, not the opened item")
    test_target: str = Field(
        description="The test task: a URL, a repository, or a one-line scenario from the user's projects; empty if not known yet"
    )


@cache
def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(base_url=os.environ.get("ALPHA_CLAUDE_BASE_URL") or ANTHROPIC_API,
                               api_key=os.environ["ALPHA_CLAUDE_API_KEY"])


@cache
def resolve_model(model: str) -> str:
    """A family name (sonnet, opus, haiku) means the newest model of that family the server lists,
    so the config never needs a version bump. Anything else is used as given."""
    if any(character.isdigit() for character in model):
        return model
    family = [m for m in _client().models.list(limit=1000) if m.id.startswith(f"claude-{model}-")]
    if not family:
        raise SystemExit(f"No Claude model in the '{model}' family is available at {_client().base_url}")
    return max(family, key=lambda m: m.created_at).id


def item_text(row: sqlite3.Row, content: Iterable[sqlite3.Row] = (), item_characters: int | None = None) -> str:
    lines = [f"Post text:\n{row['text']}"]
    links = json.loads(row["links"])
    if links:
        lines.append("Links:")
        for link in links:
            details = " - ".join(part for part in (link.get("title"), link.get("description")) if part)
            lines.append(f"- {link['url']}" + (f" ({details})" if details else ""))
    if row["title_source"] and row["title_source"] not in row["text"]:
        lines.append(f"{'Attached article title' if row['kind'] == 'post' else 'Title'}: {row['title_source']}")
    for item in content:
        text = item["text"][:item_characters] if item_characters else item["text"]
        source = f" ({item['key']})" if item["kind"] == "link" else ""
        lines.append(f"\n{CONTENT_LABELS.get(item['kind'], item['kind'])}{source}:\n{text}")
    return "\n".join(lines)


# Safety checks misfire on some harmless text and differ by model (Sonnet declined DeepSeek's DeepInfer README;
# Haiku and Opus summarized it), so a declined summary is asked of the other models in turn.
REFUSAL_FALLBACK_MODELS = ("opus", "sonnet", "haiku")


def summarize(model: str, row: sqlite3.Row, content: Iterable[sqlite3.Row] = ()) -> Summary:
    """When every model declines an item, its own words stand in, marked, so it is not retried every run."""
    message = item_text(row, content, SUMMARY_ITEM_CHARACTERS)
    for name in (model, *(m for m in REFUSAL_FALLBACK_MODELS if m != model)):
        response = _client().messages.parse(
            model=resolve_model(name),
            max_tokens=MAX_TOKENS,
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": message}],
            output_format=Summary,
        )
        if response.stop_reason != "refusal":
            break
    else:
        text = " ".join(row["text"].split())
        title = row["title_source"] or text[:80] or row["url"]
        return Summary(title=title, summary=f"Not summarized (every model declined). {text[:400]}")
    if response.parsed_output is None:
        raise RuntimeError(f"no summary in the answer (stop reason {response.stop_reason})")
    return response.parsed_output


def explain(model: str, item: sqlite3.Row, project_name: str, project_spec: str) -> str:
    content = (
        f"Project: {project_name}\n\nProject spec:\n{project_spec}\n\n---\n\n"
        f"Saved post: {item['title']}\n{item['summary']}\n\n{item_text(item)}"
    )
    response = _client().messages.parse(
        model=resolve_model(model),
        max_tokens=MAX_TOKENS,
        system=REASON_SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_format=Reason,
    )
    return response.parsed_output.reason


# Benefit first, for a busy founder: the headline and the outcome say what the user gets; the how keeps the technical
# detail, and the item itself is one click away.
PLAIN_RULE = (
    "Write the headline and the outcome in plain US English for a busy founder: the benefit first, simply put. "
    "Headline: 6 to 12 words naming the benefit for the project, with no model or product names, parameter "
    "counts, licenses, or jargon, and no punctuation at the end. For example \"Catch more swear words in noisy, "
    "real-world audio\", \"Auto-label every shot and dribble from your session videos\", \"Stop swarm agents from "
    "running dangerous commands\", \"Keep your daily data collectors from silently breaking\", \"Let site-building "
    "agents pick up where they left off\". Outcome: one plain sentence on what they can expect once it is done, again "
    "without jargon. Never invent results the source does not support: where a result is a possibility, say could."
)
SUGGEST_SYSTEM = (
    "You turn an item the user collected (a post, article, paper, repo, dataset, model, or Space) into the alpha they "
    "can act on in one of their projects, which they will rate and may carry out.\n\n"
    "Find the lightest action that captures the item's benefit:\n"
    "- The idea comes first. Often the value is the technique, finding, or pattern, which the user can apply with what "
    "the project already uses (its current stack and hosted models, including Claude, which reads images and long "
    "text). A paper's method can become a prompt or a check; a model's existence can tell the user a capability is now "
    "cheap; a dataset can show what good test cases look like.\n"
    "- Use the artifact itself (install the repo, call the model, load the dataset) only when it squarely meets a "
    "current need of the project and using it directly is clearly worth the work.\n"
    "- Heavy adoption (running or fine-tuning a local model, training, renting GPUs, downloading large datasets, new "
    "services or infrastructure) only when the benefit is large and nothing lighter works; then say so plainly with "
    "effort large.\n"
    "- If the only path is heavy and the benefit for this project is small or speculative, set skip to true with a "
    "one-sentence reason; that is better than a stretch.\n\n"
    "How: 1 to 3 sentences in this form: \"In <project>: <do X>, because <Y>; first step: <Z>.\" Name the part of the "
    "project it applies to (a feature, page, pipeline step, or open problem from the project's description) and the "
    "specific method or number from the item. The first step is something the user could start today in under an hour. "
    "No hedging (no might, consider, explore), no preamble, no restating the item.\n\n"
    "Effort: small (under an hour of agent or human work), medium (about a day), large (days, or new "
    "infrastructure).\n\n"
    + PLAIN_RULE
)
REWRITE_SYSTEM = (
    "Alpha already suggested things for the user to do with items they collected, each in one of their projects; each "
    "suggestion below is the technical how. For every numbered suggestion, write its headline and outcome from its "
    "own text only, and return each with its number. " + PLAIN_RULE
)
REWRITE_MAX_TOKENS = 16000
HEADLINE = "6 to 12 plain words: the benefit for the project, no model names or jargon"
OUTCOME = "One plain sentence: the outcome the user can expect, no jargon; 'could' where it is only a possibility"


class Suggestion(BaseModel):
    skip: bool = Field(description="True when only a heavy path exists and the benefit is small or speculative")
    skip_reason: str = Field(description="One sentence when skip is true; empty otherwise")
    headline: str = Field(description=HEADLINE)
    outcome: str = Field(description=OUTCOME)
    action: str = Field(description="How: In <project>: do X, because Y; first step: Z. One to three sentences")
    effort: str = Field(description="small | medium | large")


class Framing(BaseModel):
    number: int
    headline: str = Field(description=HEADLINE)
    outcome: str = Field(description=OUTCOME)


class Framings(BaseModel):
    suggestions: list[Framing]


def suggest(model: str, item: sqlite3.Row, project_name: str, project_text: str, basis: str) -> Suggestion:
    """One action for the item in the project, rewritten from why it fits (`basis`: the match reason and/or the
    judge's why): its headline and outcome (the benefit, plainly) and the how."""
    content = (
        f"Project: {project_name}\n\nWhat the project is and what the user is working on:\n{project_text}\n\n---\n\n"
        f"Item ({item['kind']}): {item['title']}\n{item['summary']}\n\nWhy it fits:\n{basis}\n\n"
        f"{item_text(item)[:SUMMARY_ITEM_CHARACTERS]}"
    )
    suggestion = _stream_parsed(model, SUGGEST_SYSTEM, content, Suggestion, MAX_TOKENS)
    effort = suggestion.effort.strip().lower()
    return suggestion.model_copy(update={
        "headline": suggestion.headline.strip().rstrip("."), "outcome": suggestion.outcome.strip(),
        "action": suggestion.action.strip(), "effort": effort if effort in ("small", "medium", "large") else "medium",
    })


IDEA_CHAT_SYSTEM = (
    "The user collected an item (a post, article, paper, repo, dataset, model, or Space) and has their own idea of what "
    "to do with it in one of their projects. Shape it with them into a suggestion they can implement: the headline "
    "(the benefit), the outcome, the how, and the effort.\n\n"
    "The idea is theirs: keep its intent and the part of the project it names, and do not replace it with another "
    "application. But work it, do not just transcribe it. The user often spills the idea out rough and then wants to "
    "iterate until it is sharp. Each turn: distill it to its core (the one thing it does and why that matters for "
    "the project, in a sentence); make the most of it (what in the item or the project makes it stronger, what to "
    "cut, where the scope is too wide or too narrow, a first step that proves it fastest); and tell them plainly "
    "where it is weak or what it rests on. Put the sharpenings in polish: at most three, each one concrete line "
    "they can take or leave. When they take one, fold it into the plan; when they decline, drop it. Fill in what "
    "the idea leaves open from the project's description and the item: the specific method or number it relies on, "
    "and the first step. Ask at most three short questions at a time, each on its own numbered line, only about what "
    "changes the how (where in the project it goes, a choice between two ways, a constraint); propose sensible "
    "defaults instead of asking about everything. Every answer carries your current plan, updated with everything "
    "said so far. Set ready when the how is specific enough that an agent could start without asking anything "
    "else; the user may keep it or implement it earlier.\n\n"
    "Reply: your reading of the idea in a sentence or two, then the sharpenings you suggest and why (short), then "
    "any questions. Plain US English, no Markdown.\n\n"
    "How: 1 to 3 sentences in this form: \"In <project>: <do X>, because <Y>; first step: <Z>.\" The first step is "
    "something the user could start today in under an hour. No hedging (no might, consider, explore), no preamble, no "
    "restating the item.\n\n"
    "Effort: small (under an hour of agent or human work), medium (about a day), large (days, or new "
    "infrastructure).\n\n"
    + PLAIN_RULE
)


class IdeaPlan(BaseModel):
    reply: str = Field(description="What you say to the user: your reading of the idea, the sharpenings and why, then any questions")
    ready: bool = Field(description="True when the how is specific enough for an agent to start without more questions")
    core: str = Field(description="The idea distilled: the one thing it does and why that matters for the project, one sentence")
    polish: list[str] = Field(description="Up to three concrete ways to make more of the idea (a cut, a sharper scope, a "
                                          "stronger first step, something in the item they missed), one line each; "
                                          "empty once nothing is left to sharpen")
    headline: str = Field(description=HEADLINE)
    outcome: str = Field(description=OUTCOME)
    action: str = Field(description="How: In <project>: do X, because Y; first step: Z. One to three sentences")
    effort: str = Field(description="small | medium | large")


def plan_idea(model: str, item: sqlite3.Row, project_name: str, project_text: str, messages: list[dict]) -> IdeaPlan:
    """One turn of the idea chat (the user's own idea for the item in the project). messages alternate user and
    assistant, starting with the user's idea."""
    system = (
        f"{IDEA_CHAT_SYSTEM}\n\nProject: {project_name}\n\nWhat the project is and what the user is working on:\n"
        f"{project_text}\n\n---\n\n"
        f"Item ({item['kind']}): {item['title'] or item['title_source'] or item['url']}\n{item['summary'] or ''}\n\n"
        f"{item_text(item)[:SUMMARY_ITEM_CHARACTERS]}"
    )
    response = _client().messages.parse(
        model=resolve_model(model), max_tokens=MAX_TOKENS * 2, system=system, messages=messages, output_format=IdeaPlan,
    )
    plan = response.parsed_output
    if plan is None:
        raise RuntimeError(f"no plan in the answer (stop reason {response.stop_reason})")
    effort = plan.effort.strip().lower()
    return plan.model_copy(update={
        "headline": plan.headline.strip().rstrip("."), "outcome": plan.outcome.strip(), "action": plan.action.strip(),
        "effort": effort if effort in ("small", "medium", "large") else "medium",
    })


def frame(model: str, suggestions: list[tuple[str, str]]) -> list[Framing | None]:
    """Headline and outcome for many existing suggestions in one call, from their own text; `suggestions` are
    (project, how). A suggestion the answer skipped gets None."""
    content = "\n\n".join(f"{number}. Project: {project}\n{action}"
                          for number, (project, action) in enumerate(suggestions, 1))
    answer = _stream_parsed(model, REWRITE_SYSTEM, content, Framings, REWRITE_MAX_TOKENS)
    written = {entry.number: Framing(number=entry.number, headline=entry.headline.strip().rstrip("."),
                                     outcome=entry.outcome.strip())
               for entry in answer.suggestions if entry.headline.strip() and entry.outcome.strip()}
    return [written.get(number) for number in range(1, len(suggestions) + 1)]


SUGGESTION_CHAT_SYSTEM = (
    "The user is deciding whether to act on a suggestion Alpha made for one of their projects, and asks you about it "
    "before they implement it. Answer their question about this suggestion for this project in plain US English for a "
    "busy founder, benefit first: the impact, the cost, the risk, what changes for them, and the effort, whichever "
    "the question is about. Ground every claim in the suggestion, the source item, and the project's description and "
    "focus brief below. When they do not tell, say \"I don't know\" and what would tell. Never invent results, "
    "numbers, or features. Keep answers short: at most five plain sentences, no Markdown (no bold, headings, or "
    "lists). Offer the technical detail only if the user asks for it."
)


def answer_suggestion(model: str, context: str, messages: list[dict]) -> str:
    """One answer in a suggestion's Ask about this; messages alternate user and assistant, starting with the user."""
    response = _client().messages.create(
        model=resolve_model(model), max_tokens=MAX_TOKENS, system=f"{SUGGESTION_CHAT_SYSTEM}\n\n{context}",
        messages=messages,
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise RuntimeError(f"no answer (stop reason {response.stop_reason})")
    return text



def transcribe_image(model: str, image: bytes, media_type: str, post_text: str) -> str:
    response = _client().messages.create(
        model=resolve_model(model),
        max_tokens=MAX_TOKENS,
        system=IMAGE_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.b64encode(image).decode()}},
                {"type": "text", "text": f"The post this image is attached to:\n{post_text[:2000]}"},
            ],
        }],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


def _stream_parsed(model: str, system: str, content: str, output_format: type[BaseModel], max_tokens: int):
    """Structured output over a stream, so long Opus answers are not cut off by the request timeout."""
    with _client().messages.stream(
        model=resolve_model(model),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_format=output_format,
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "max_tokens":
        raise RuntimeError(f"answer cut off at {max_tokens} tokens")
    return message.parsed_output


def propose_topics(model: str, items: str, existing: str, projects: str) -> list[TopicProposal]:
    content = f"Items (id | source | kind | category | title | summary):\n{items}"
    if existing:
        content += f"\n\nSkills that already exist; do not propose them again:\n{existing}"
    if projects:
        content += f"\n\nThe user's projects (for test tasks):\n{projects}"
    return _stream_parsed(model, TOPICS_SYSTEM, content, Topics, TOPICS_MAX_TOKENS).topics


def plan_skill(model: str, post: str, catalogue: str, existing: str, messages: list[dict]) -> SkillPlan:
    """One turn of the skill chat. messages alternate user and assistant, starting with the user."""
    system = (
        f"{SKILL_CHAT_SYSTEM}\n\nThe opened item:\n{post}\n\n"
        f"The user's other items (id | category | title | summary):\n{catalogue}"
    )
    if existing:
        system += f"\n\nSkills that already exist:\n{existing}"
    response = _client().messages.parse(
        model=resolve_model(model), max_tokens=MAX_TOKENS * 2, system=system, messages=messages, output_format=SkillPlan,
    )
    if response.parsed_output is None:
        raise RuntimeError(f"no plan in the answer (stop reason {response.stop_reason})")
    return response.parsed_output


def write_skill_spec(
    model: str, name: str, title: str, description: str, sources: str,
    previous: str | None = None, problems: list[str] | None = None, brief: str | None = None,
    neighbors: dict[str, str] | None = None, test_task: str | None = None,
    fixed: list[str] | None = None, repaired: list[str] | None = None,
) -> SkillSpec:
    """previous: the live version's spec JSON, for a stable revision. problems: what validation,
    the script test, or the graded test run found wrong with the last attempt. fixed: problems earlier
    attempts of this build had that the last one no longer has, so a revision keeps them fixed. repaired:
    what code changed in an earlier spec before checking it, so the writer gets it right itself. brief: what
    the user asked for in the skill chat. neighbors: library skills whose scope overlaps (name: description),
    so the description sends their requests to them instead of competing for them. test_task: what the
    build's test run works on, so scripts take the right kind of input."""
    content = f"Skill name: {name}\nTopic: {title}\nWhat the agent does with it: {description}\n\n"
    if test_task:
        content += f"The build tests the skill on this task: {test_task}\n\n"
    if brief:
        content += f"What the user asked for (follow it; it decides scope and approach within what the items support):\n{brief}\n\n"
    if neighbors:
        content += (
            "Other skills in the user's library overlap this one (name: description). An agent picks a skill by its "
            "description, so scope this one against them: 'Use when ...' names the requests this skill handles best, "
            "and 'Not for ...; use <name> instead' sends the others to the right skill. Do not copy their steps.\n"
            + "\n".join(f"- {other}: {text}" for other, text in neighbors.items()) + "\n\n"
        )
    content += f"Source items, numbered:\n\n{sources}"
    system = SKILL_SYSTEM
    if previous:
        system += SKILL_REVISION
        content += f"\n\n---\n\nCurrent version of the spec:\n{previous}"
    if problems:
        content += "\n\n---\n\nProblems to fix (found in the last attempt):\n" + "\n".join(f"- {p}" for p in problems)
    if fixed:
        content += ("\n\n---\n\nProblems earlier attempts had that the last attempt no longer showed; keep them fixed and "
                    "do not bring them back:\n" + "\n".join(f"- {p}" for p in fixed))
    if repaired:
        content += ("\n\n---\n\nCode had to repair these in an earlier attempt's spec; write them right yourself:\n"
                    + "\n".join(f"- {p}" for p in repaired))
    return _stream_parsed(model, system, content, SkillSpec, SKILL_MAX_TOKENS)


class Shortened(BaseModel):
    description: str


def shorten_description(model: str, description: str, limit: int) -> str:
    """One small call that rewrites a too-long skill description to at most `limit` characters."""
    content = f"Limit: {limit} characters (this one has {len(description)}).\n\nDescription:\n{description}"
    return " ".join(_stream_parsed(model, SHORTEN_SYSTEM, content, Shortened, MAX_TOKENS).description.split())


class Criterion(BaseModel):
    name: str
    score: float = Field(description="0 to 1")
    reason: str


class Grade(BaseModel):
    criteria: list[Criterion]
    problems: list[str] = Field(description="Concrete problems in the skill a revision should fix")


def grade_run(model: str, skill_md: str, target: str, report: str) -> Grade:
    rubric = "\n".join(f"- {name}: {text}" for name, text in GRADE_CRITERIA.items())
    content = (
        f"Criteria (use these names):\n{rubric}\n\nTest task: {target}\n\n"
        f"Skill:\n{skill_md}\n\n---\n\nThe agent's report:\n{report}"
    )
    return _stream_parsed(model, GRADE_SYSTEM, content, Grade, MAX_TOKENS * 4)


def passed(grade: Grade) -> bool:
    scores = {c.name: c.score for c in grade.criteria}
    if set(scores) != set(GRADE_CRITERIA):
        return False
    return sum(scores.values()) / len(scores) >= PASS_MEAN and min(scores.values()) >= PASS_MINIMUM


def mean(grade: Grade) -> float:
    return sum(c.score for c in grade.criteria) / len(grade.criteria) if grade.criteria else 0.0


class PairGrade(BaseModel):
    a: Grade = Field(description="Grade of skill A")
    b: Grade = Field(description="Grade of skill B")
    comparison: str = Field(description="Where one version did better than the other and why, naming A and B")


class HeadToHead(BaseModel):
    new: Grade
    live: Grade
    comparison: str
    new_label: str = Field(description="A or B: the label the new version had, so `comparison` can be read")


def _blind(skill_md: str) -> str:
    """Without the marker line, which names the version and build date."""
    return "\n".join(line for line in skill_md.splitlines() if MARKER not in line)


def grade_pair(model: str, target: str, new: tuple[str, str], live: tuple[str, str]) -> HeadToHead:
    """Grade the new and the live version's test runs (SKILL.md, report) in one call, so both are
    judged on one scale. Labels are blinded and the order is random, so neither position nor
    "newer" can lean the grade."""
    new_first = random.random() < 0.5
    first, second = (new, live) if new_first else (live, new)
    rubric = "\n".join(f"- {name}: {text}" for name, text in GRADE_CRITERIA.items())
    content = f"Criteria (use these names):\n{rubric}\n\nTest task: {target}"
    for label, (skill_md, report) in (("A", first), ("B", second)):
        content += f"\n\n=== Skill {label} ===\n{_blind(skill_md)}\n\n=== The agent's report for skill {label} ===\n{report}"
    pair = _stream_parsed(model, COMPARE_SYSTEM, content, PairGrade, MAX_TOKENS * 8)
    return HeadToHead(
        new=pair.a if new_first else pair.b, live=pair.b if new_first else pair.a,
        comparison=pair.comparison, new_label="A" if new_first else "B",
    )


def regressions(new: Grade, live: Grade, live_version: int) -> list[str]:
    """Why the new version may not replace the live one; empty when it may: a lower mean, or a
    criterion more than REGRESSION_TOLERANCE below the live version's."""
    problems = []
    if round(mean(new), 3) < round(mean(live), 3):
        problems.append(
            f"The live version v{live_version}, tested on the same target in this build, graded higher: "
            f"mean {mean(live):.2f} against {mean(new):.2f} for this version."
        )
    before = {c.name: c for c in live.criteria}
    for criterion in new.criteria:
        old = before.get(criterion.name)
        if old and round(old.score - criterion.score, 3) > REGRESSION_TOLERANCE:
            problems.append(
                f"{criterion.name} fell from {old.score:.2f} (live v{live_version}) to {criterion.score:.2f}. "
                f"Live: {old.reason} This version: {criterion.reason}"
            )
    return problems


class Neighbors(BaseModel):
    names: list[str] = Field(description="Library skills whose scope overlaps the new skill; empty if none")


def pick_neighbors(model: str, name: str, description: str, library: dict[str, str]) -> list[str]:
    """Library skills an agent could confuse with this one, picked by Claude from their descriptions."""
    listing = "\n".join(f"- {other}: {text}" for other, text in library.items())
    content = f"The skill: {name}\n{description}\n\nThe library (name: description):\n{listing}"
    picked = _stream_parsed(model, NEIGHBORS_SYSTEM, content, Neighbors, MAX_TOKENS * 2).names
    return [other for other in dict.fromkeys(picked) if other in library and other != name]


class Scope(BaseModel):
    skill: str
    scope: str = Field(description="What this skill is for within the group, and 'Not for ...; use <other> instead'")


class OverlapGroup(BaseModel):
    skills: list[str] = Field(description="Two or more skill names whose triggers overlap")
    triggers: str = Field(description="The requests or phrases that would make an agent load more than one of them")
    scopes: list[Scope] = Field(description="A proposed scope for each skill in the group")


class Overlaps(BaseModel):
    groups: list[OverlapGroup]


def find_overlaps(model: str, library: dict[str, str]) -> list[OverlapGroup]:
    listing = "\n".join(f"- {name}: {text}" for name, text in library.items())
    groups = _stream_parsed(model, OVERLAPS_SYSTEM, f"The skill library (name: description):\n{listing}",
                            Overlaps, TOPICS_MAX_TOKENS).groups
    for group in groups:
        group.skills = [name for name in dict.fromkeys(group.skills) if name in library]
        group.scopes = [scope for scope in group.scopes if scope.skill in group.skills]
    return [group for group in groups if len(group.skills) > 1]


# Context and discovery (focus.py, judge.py). The user's raw activity (agent prompts, dictations, meeting notes) is
# read only here, by Claude; what leaves is a brief with no personal details, and search queries.

ASSIGN_SYSTEM = (
    "You sort the user's recent activity by project: dictations, meeting notes, and prompts they gave coding agents "
    "outside any project folder. For each numbered item that is clearly about one of the listed projects, return "
    "its number and the project's exact name. Skip items that are personal, about none of the projects, or too "
    "vague to tell. Most items will be skipped; that is expected."
)

BRIEF_SYSTEM = (
    "You write a focus brief for one of the user's projects: a short, factual statement of what the user is working on "
    "right now, used to look for outside knowledge (articles with tactics, tools, experience reports) that would "
    "help. Sources: their recent prompts to coding agents, commit subjects and changed files, dictations and meeting "
    "notes about this project, and the project's spec.\n\n"
    "Rules:\n"
    "- Describe the work, not the person. No personal details (health, family, money, location, schedule, "
    "feelings), no names of people (say 'a client', 'a partner'), no email addresses, phone numbers, keys, or "
    "private URLs. Do not quote the sources.\n"
    "- Prefer what the recent activity shows over what the spec planned. Name the concrete technology, channel, "
    "or method involved.\n"
    "- If the user corrected an earlier brief, keep their corrections unless the activity clearly shows they no "
    "longer hold.\n"
    "- If there is little or no recent activity, say so in working_on and write the rest from the spec.\n"
    "- Keep the whole brief under 250 words."
)

QUERIES_SYSTEM = (
    "You write search queries that find practitioner write-ups with concrete tactics (named tools, "
    "numbers, exact steps, the author's own results) for the open problems and the outside edge in a project's "
    "focus brief. Write 3 to 6 queries of 2 to 6 words each, each aimed at a different problem. They are sent to a "
    "public search engine: no names of people, companies, clients, products, or projects from the brief; describe "
    "the topic instead. No quotes or operators."
)

ASSIGN_MAX_TOKENS = 16000


class Assignment(BaseModel):
    item: int
    project: str


class Assignments(BaseModel):
    assignments: list[Assignment]


class FocusBrief(BaseModel):
    working_on: str = Field(description="What they are building or doing in this project this period, 2 to 4 sentences")
    why: str = Field(description="The goal behind it, 1 or 2 sentences")
    open_problems: list[str] = Field(description="Open problems the user is stuck on or working through, one line each")
    questions: list[str] = Field(description="Open questions the user has not answered yet, one line each")
    outside_edge: str = Field(
        description="What kind of outside knowledge would help most now: which tactics, tools, or experience "
        "reports, 1 to 3 sentences"
    )


class Queries(BaseModel):
    queries: list[str]


def brief_text(brief: FocusBrief) -> str:
    """The stored and editable form of a brief."""
    parts = [f"Working on: {brief.working_on.strip()}", f"Why: {brief.why.strip()}"]
    if brief.open_problems:
        parts.append("Open problems:\n" + "\n".join(f"- {line.strip()}" for line in brief.open_problems))
    if brief.questions:
        parts.append("Questions:\n" + "\n".join(f"- {line.strip()}" for line in brief.questions))
    parts.append(f"Outside edge that would help: {brief.outside_edge.strip()}")
    return "\n\n".join(parts)


def assign_activity(model: str, projects: str, items: str) -> list[Assignment]:
    """projects: one line per project (name: what it is). items: numbered activity, newest first."""
    content = f"Projects:\n{projects}\n\nActivity items:\n{items}"
    return _stream_parsed(model, ASSIGN_SYSTEM, content, Assignments, ASSIGN_MAX_TOKENS).assignments


def write_brief(model: str, project: str, spec: str, activity: str, previous: str | None, correction: str | None) -> str:
    content = f"Project: {project}\n\nSpec:\n{spec or '(none)'}\n\nRecent activity:\n{activity or '(none)'}"
    if previous:
        content += f"\n\n---\n\nThe previous brief:\n{previous}"
    if correction:
        content += f"\n\n---\n\nThe user's own edit of the brief (the user's corrections):\n{correction}"
    return brief_text(_stream_parsed(model, BRIEF_SYSTEM, content, FocusBrief, MAX_TOKENS * 2))


def search_queries(model: str, project: str, brief: str) -> list[str]:
    content = f"Project: {project}\n\nFocus brief:\n{brief}"
    queries = _stream_parsed(model, QUERIES_SYSTEM, content, Queries, MAX_TOKENS).queries
    return [query.strip() for query in dict.fromkeys(queries) if query.strip()][:6]


TASTE_SYSTEM = (
    "You write the user's taste profile from their picks: posts they bookmarked on X, repos they starred on GitHub, "
    "and models, datasets, and Spaces they liked on Hugging Face (title and summary of each, newest first, sampled "
    "across the sources), then the items and suggestions they rated α (keep) or β (no edge) in their Alpha feed, and "
    "the ideas they wrote themselves (what they would do with an item in a project): weigh those most, they say in "
    "the user's own words what they want items for. "
    "It is given to a judge that reads the user's X timelines and Hugging Face releases from accounts they follow, so "
    "they need not check them, and must pass only what they would have picked. Say what they keep: the topics, "
    "the kind of post (exact steps, numbers, tools, code, first-hand results), the kinds of repos and of models, "
    "datasets, and Spaces they pick (tasks, sizes, open weights they can run), and what makes an item worth keeping "
    "to them. Then say what they would call slop: what fills a timeline or a model hub but they never keep (engagement "
    "bait, hot takes, giveaways, motivational lines, news without anything to use, threads that promise steps and "
    "give none, releases with no card or no usable weights). Be concrete; name topics and patterns, not people. "
    "Keep each list to 6 to 9 lines."
)


class Taste(BaseModel):
    keeps: list[str] = Field(description="What the user saves, one line each")
    slop: list[str] = Field(description="What the user would call slop, one line each")


def write_taste(model: str, picked: str) -> str:
    taste = _stream_parsed(model, TASTE_SYSTEM, f"What the user picked:\n{picked}", Taste, MAX_TOKENS * 2)
    return ("The user keeps:\n" + "\n".join(f"- {line.strip()}" for line in taste.keeps)
            + "\n\nHe calls slop:\n" + "\n".join(f"- {line.strip()}" for line in taste.slop))


class Verdict(BaseModel):
    sauce: bool
    why: str = Field(description="One sentence")


def judge_sauce(model: str, project: str, brief: str, text: str, kind: str = "post") -> Verdict:
    """The second, independent stage-2 judge (Jev asks the same question, with the same definition for the kind);
    its why is shown for each find."""
    noun, definition = sauce_definition(kind)
    content = f"Project: {project}\n\nFocus brief:\n{brief}\n\n---\n\n{noun.capitalize()}:\n{text}"
    response = _client().messages.parse(
        model=resolve_model(model), max_tokens=MAX_TOKENS,
        system=f"Decide whether a {noun} is sauce or slop for the user's project.\n\n{definition}",
        messages=[{"role": "user", "content": content}], output_format=Verdict,
    )
    if response.parsed_output is None:
        raise RuntimeError(f"no verdict in the answer (stop reason {response.stop_reason})")
    return response.parsed_output
