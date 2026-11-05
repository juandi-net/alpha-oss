"""Decisions Jev makes: category, duplicate detection, project relevance, skill membership, and the judge's
fit and sauce questions.

TypeSafe System One API through the official SDK (https://docs.typesafe.ai). Each request sends
one item as `state` and many questions, which Jev answers in parallel. Billing is per
input token; output is free. TYPESAFE_API_KEY comes from .env.
"""

import re
from dataclasses import dataclass, field
from functools import cache

from typesafe_sdk import Choice, Noul, NoulCriteria, Score, TypeSafeClient

# Levels describe situations, low to high, as the Score documentation recommends.
RELEVANCE_LEVELS = [
    "Unrelated to the project's goals, technology, or audience",
    "Same broad field as the project, but offers nothing the project could use",
    "Offers an idea, tool, or technique the project could adopt",
    "Directly addresses a current goal, problem, or planned feature of the project",
]
# Character budget for the questions in one request. Jev allows 64k tokens per request;
# at roughly 3 characters per token this stays well inside it.
REQUEST_CHARACTER_BUDGET = 120_000
DUPLICATE_CANDIDATES = 25

# The judge (judge.py). Stage 1 sees only a candidate's title and preview; stage 2 the full text of survivors.
FIT_LEVELS = [
    "Nothing in it touches the work described",
    "Same field, but not the problems or goals described",
    "Likely to help with one of the problems, questions, or goals described",
    "Squarely about one of the open problems or questions described",
]
# Stage 1 for feeds (judge.stage_one, batched): the briefs and the user's taste in one state.
FEED_FIT_LEVELS = [
    "Nothing for the work or the taste described: a post the user would scroll past",
    "Same field, but nothing specific to use",
    "Likely useful: specific, and about one of the projects or what the user keeps",
    "Squarely what the user keeps, or what one of the projects needs now",
]
FEED_POSTS_PER_REQUEST = 40
# Stage 2 asks one yes/no question with this definition, of Jev and (separately) of Claude (writer.judge_sauce).
# On 63 scored posts Jev and Haiku agreed on 59 and each picked 6; a four-score rubric had passed link roundups.
SAUCE_DEFINITION = (
    "Sauce: the post teaches something specific the user could put to work on this project now: named tools, "
    "numbers, exact steps or settings, backed by the author's own results or data, and not common knowledge. "
    "Slop: generic advice, listicles, link roundups, opinion, news, promotion, beginner basics, or claims with "
    "nothing to execute; also anything that only touches the project's field without helping its current work."
)
# Alpha's rule for every kind: the idea first, the vessel second. A repo, model, dataset, or paper carries a concept,
# technique, finding, or capability; that is what is judged and acted on. The artifact itself counts only when it
# squarely meets a current need.
IDEA_RULE = (
    "Judge the idea first: the concept, technique, finding, or capability the item presents. The repo, model, "
    "dataset, or paper is its vessel; the user does not have to adopt or run it for the idea to be sauce. The artifact "
    "itself counts only when it squarely meets a current, concrete need in the brief, so that using it directly "
    "would clearly be worth the work; mere relevance to the field does not make the artifact sauce."
)
# Datasets are judged as data, not as write-ups: on 31 datasets the post definition gave Jev 0.05-0.33 throughout.
DATASET_SAUCE_DEFINITION = (
    "Sauce: a dataset that presents an idea the user can put to work on this project or that fits what they keep: "
    "how its tasks, labels, or checks are designed (a pattern for their own tests, evaluations, or prompts), a finding "
    "from building it, or, rarely, data that squarely meets a concrete need in the brief. Its card says what is in it "
    "and how it was collected or labeled. Slop: an idea off the project's work and the user's taste; an unofficial "
    "mirror or reupload of a "
    "better-known release; a toy, test, or dummy set; or unclear provenance. " + IDEA_RULE
)
# Models and Spaces are judged as releases, in the dataset definition's style.
RELEASE_SAUCE_DEFINITION = (
    "Sauce: a model or Space that presents an idea the user can put to work on this project or that fits what they "
    "keeps: a technique, design, or method shown in how it was built, trained, or evaluated, or a capability now "
    "small, cheap, or open enough to change what they can do. A real card backs it (what it does, how it was made, "
    "sizes, benchmarks, or examples). Slop: a quantization, checkpoint, or reupload of a release that adds nothing "
    "new; a test, placeholder, or internal artifact; an empty or template card; or an idea off the project's work "
    "and the user's taste. " + IDEA_RULE
)


def sauce_definition(kind: str) -> tuple[str, str]:
    """(what the candidate is called, the definition of sauce) for an item kind."""
    if kind == "dataset":
        return "dataset", DATASET_SAUCE_DEFINITION
    if kind in ("model", "space"):
        return kind, RELEASE_SAUCE_DEFINITION
    return "post", SAUCE_DEFINITION


@dataclass
class Usage:
    input_tokens: int = 0
    requests: int = 0

    def cost(self, price_per_million: float) -> float:
        return self.input_tokens / 1_000_000 * price_per_million


@dataclass
class Classification:
    category: str
    category_confidence: float
    duplicate_of: str | None
    duplicate_probability: float | None


@dataclass
class Jev:
    model: str
    usage: Usage = field(default_factory=Usage)

    def _ask(self, state: str, questions: dict):
        response = _client().system_one(state, questions, model=self.model)
        self.usage.requests += 1
        self.usage.input_tokens += response.usage.input_tokens or 0
        return response.answers

    def version(self) -> str:
        """The model the server actually runs for `self.model` (jev-latest resolves to a version)."""
        response = _client().system_one(
            "Version check.",
            {"check": Noul(instructions="Is this a check?", criteria=NoulCriteria(true="Yes", false="No"))},
            model=self.model,
        )
        self.usage.requests += 1
        self.usage.input_tokens += response.usage.input_tokens or 0
        return response.model

    def classify(
        self,
        state: str,
        categories: dict[str, str],
        candidates: list[tuple[str, str]],
        duplicate_threshold: float,
    ) -> Classification:
        """candidates: (post_id, "title: summary") for earlier canonical bookmarks."""
        questions: dict = {
            "category": Choice(
                instructions="Which topic best describes the main idea of this post?",
                criteria=categories,
            )
        }
        for index, (_, description) in enumerate(candidates):
            questions[f"duplicate_{index}"] = Noul(
                instructions=(
                    "Does this post present essentially the same idea, tool, or technique as the "
                    f"following earlier post?\n\nEarlier post: {description}"
                ),
                criteria=NoulCriteria(
                    true="The same core idea, tool, or technique, even if worded differently or by another author",
                    false="A different idea, or only the same broad topic",
                ),
            )
        answers = self._ask(state, questions)
        duplicate_of = probability = None
        if candidates:
            best = max(range(len(candidates)), key=lambda index: answers[f"duplicate_{index}"].noul)
            probability = answers[f"duplicate_{best}"].noul
            if probability >= duplicate_threshold:
                duplicate_of = candidates[best][0]
        category = answers["category"]
        return Classification(category.choice, category.confidence, duplicate_of, probability)

    def relevance(self, state: str, projects: list[tuple[str, str]], guidance: str) -> dict[str, tuple[float, float]]:
        """projects: (name, spec). guidance: added to every question. Returns name -> (score 0..3, confidence)."""
        preamble = "How useful is the idea in this post to the project described below?"
        if guidance:
            preamble += f"\n\n{guidance}"
        results: dict[str, tuple[float, float]] = {}
        for batch in _batches(projects):
            questions = {
                f"project_{index}": Score(
                    instructions=f"{preamble}\n\nProject: {name}\n\n{spec}",
                    criteria=RELEVANCE_LEVELS,
                )
                for index, (name, spec) in enumerate(batch)
            }
            answers = self._ask(state, questions)
            for index, (name, _) in enumerate(batch):
                answer = answers[f"project_{index}"]
                results[name] = (answer.score, answer.confidence)
        return results

    def topic_membership(self, state: str, topics: list[tuple[str, str]]) -> dict[str, float]:
        """topics: (name, "title: description"). Returns name -> probability the post teaches part of it."""
        questions = {
            f"topic_{index}": Noul(
                instructions=(
                    "Does this post teach a step, check, tool, or tactic an agent could use for the skill below?\n\n"
                    f"Skill: {description}"
                ),
                criteria=NoulCriteria(
                    true="It contains something concrete to do or check for this skill",
                    false="It is unrelated, or only touches the same field without anything to execute",
                ),
            )
            for index, (_, description) in enumerate(topics)
        }
        answers = self._ask(state, questions)
        return {name: answers[f"topic_{index}"].noul for index, (name, _) in enumerate(topics)}

    def fit(self, state: str, briefs: list[tuple[str, str]]) -> dict[str, float]:
        """Stage 1. briefs: (project, focus brief). Returns project -> 0..1, how likely the post helps."""
        results: dict[str, float] = {}
        for batch in _batches(briefs):
            questions = {
                f"brief_{index}": Score(
                    instructions=(
                        "Judging by the title and preview only, how likely is this post to help with the current "
                        f"work below?\n\nProject: {name}\n\nFocus brief:\n{brief}"
                    ),
                    criteria=FIT_LEVELS,
                )
                for index, (name, brief) in enumerate(batch)
            }
            answers = self._ask(state, questions)
            for index, (name, _) in enumerate(batch):
                results[name] = answers[f"brief_{index}"].score / (len(FIT_LEVELS) - 1)
        return results

    def fit_feed(self, contexts: dict[str, str], posts: list[str]) -> list[tuple[float, str]]:
        """Stage 1 for a feed (many short posts): the projects' briefs and the user's taste go in the state once,
        and each request asks about many posts, two questions each (how well it fits, and what it fits best).
        contexts: name -> brief or taste profile. Returns (0..1, best context name) per post."""
        state = "The user's current work and taste:\n\n" + "\n\n".join(
            f"[{name}]\n{text}" for name, text in contexts.items())
        choices = {name: " ".join(text.split())[:200] for name, text in contexts.items()}
        room = max(REQUEST_CHARACTER_BUDGET - len(state) - 300 * len(choices), 10_000)
        results: list[tuple[float, str]] = []
        start = 0
        while start < len(posts):
            batch, size = [], 0
            for post in posts[start:]:
                if batch and (size + len(post) + 600 > room or len(batch) >= FEED_POSTS_PER_REQUEST):
                    break
                batch.append(post)
                size += len(post) + 600
            questions: dict = {}
            for index, post in enumerate(batch):
                questions[f"fit_{index}"] = Score(
                    instructions="Judging by this post's text only, how likely is it to help with the work, or to be "
                    f"what the user keeps, as described in the state?\n\nPost:\n{post}",
                    criteria=FEED_FIT_LEVELS,
                )
                questions[f"best_{index}"] = Choice(
                    instructions=f"Which entry in the state does this post fit best?\n\nPost:\n{post}", criteria=choices,
                )
            answers = self._ask(state, questions)
            results += [(answers[f"fit_{index}"].score / (len(FEED_FIT_LEVELS) - 1), answers[f"best_{index}"].choice)
                        for index in range(len(batch))]
            start += len(batch)
        return results

    def sauce(self, state: str, project: str, brief: str, candidates: list[tuple[str, str]],
              kind: str = "post") -> "Sauce":
        """Stage 2: the probability the candidate is sauce for the project (definition by kind), and novelty against
        candidates: (id, "title: summary") of items and skills the user already has (see shortlist), 1 minus the
        closest repeat."""
        noun, definition = sauce_definition(kind)
        questions: dict = {
            "sauce": Noul(
                instructions=f"{definition}\n\nIs this {noun} sauce for the project below?\n\nProject: {project}"
                f"\n\nFocus brief:\n{brief}",
                criteria=NoulCriteria(true="Sauce", false="Slop"),
            ),
        }
        for index, (_, description) in enumerate(candidates):
            questions[f"repeat_{index}"] = Noul(
                instructions=(
                    "Does this post teach essentially the same idea, tool, or technique as the following item the "
                    f"user already has?\n\nItem: {description}"
                ),
                criteria=NoulCriteria(
                    true="The same core idea, tool, or technique, even if worded differently or by another author",
                    false="A different idea, or only the same broad topic",
                ),
            )
        answers = self._ask(state, questions)
        similar, repeat = None, 0.0
        if candidates:
            best = max(range(len(candidates)), key=lambda index: answers[f"repeat_{index}"].noul)
            similar, repeat = candidates[best][0], answers[f"repeat_{best}"].noul
        return Sauce(sauce=answers["sauce"].noul, novelty=1 - repeat, similar_to=similar)


@dataclass
class Sauce:
    sauce: float                     # probability the post is sauce for the project
    novelty: float
    similar_to: str | None


@cache
def _client() -> TypeSafeClient:
    return TypeSafeClient()


def _batches(projects: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    batches: list[list[tuple[str, str]]] = [[]]
    size = 0
    for project in projects:
        length = len(project[1]) + 300
        if batches[-1] and size + length > REQUEST_CHARACTER_BUDGET:
            batches.append([])
            size = 0
        batches[-1].append(project)
        size += length
    return [batch for batch in batches if batch]


_WORD = re.compile(r"[a-z0-9]{4,}")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def shortlist(text: str, earlier: list[tuple[str, str]], limit: int = DUPLICATE_CANDIDATES) -> list[tuple[str, str]]:
    """Earlier bookmarks with the most word overlap, so Jev compares only plausible duplicates."""
    words = _words(text)

    def overlap(candidate: tuple[str, str]) -> float:
        other = _words(candidate[1])
        return len(words & other) / (len(words | other) or 1)

    return sorted(earlier, key=overlap, reverse=True)[:limit]
