<img src="alpha/static/logo.svg" width="64" alt="Alpha">

# Alpha

Alpha is a stream of alpha: it pulls from many sources (what you save: X bookmarks, GitHub stars, Hugging Face likes;
what it finds for you: your X Following and For You timelines, the Substacks you subscribe to, what the Hugging Face
accounts you follow release, what is trending on GitHub and Hugging Face, Substack search, papers that ship code,
Hugging Face datasets), keeps only the real signal, and turns it into actionable skills your AI agents use.

How: one hard judge for everything Alpha finds (your bookmarks teach it your taste; your projects' focus briefs tell
it what matters now), one warehouse, a feed you scroll instead of X, skills built and tested from what lands there,
and agents reading it all through the MCP server ("check Alpha for ...").

The rule throughout: the idea first, the vessel second. A post, repo, model, dataset, or paper carries a concept,
technique, finding, or capability. The judge weighs that idea, and suggestions put it to work with what your
projects already have. The artifact itself (installing the repo, running the model, loading the dataset) counts only
when it squarely meets a current need and is clearly worth the work.

## Quick start

macOS, Python 3.14, [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/juandi-net/alpha-oss.git alpha && cd alpha
uv sync
uv run alpha setup        # checks tools, creates .env and config.toml, says what is missing
# fill in .env (keys) and config.toml (projects, sources, context feeds)
uv run alpha run          # first run: collect, discover, process
uv run alpha install      # launchd: a run every 6 hours (each source keeps its own cadence) and the dashboard
open http://localhost:3457
```

You need an Anthropic API key (`ALPHA_CLAUDE_API_KEY`) and a TypeSafe key (`TYPESAFE_API_KEY`, for Jev). Every
source and context feed is off until you enable it in config.toml. `alpha status` shows what is set up.

## How it works

```
sources ──> (discovered only: judge) ──> warehouse (SQLite) ──> outputs: skills, project matches, dashboard, vault pages
                    ^
     context: focus briefs (what you work on)
```

- **Curated sources** bring items you picked, such as your X bookmarks. They are trusted and stored as they are.
- **Discovered sources** bring items Alpha finds, such as Substack posts. Every candidate goes through the shared
  judge (`judge.py`): stage 1, Jev scores title and preview against your focus briefs; stage 2, the full text goes
  to Jev and to Claude (`[judge] model`, Haiku by default), and it becomes a find when both call it "sauce"
  (specific, backed by the author's own results, usable now; for a dataset or a model or Space: it presents an idea
  you can put to work, in how its tasks or labels are designed, how it was built or evaluated, or a capability now
  cheap or open enough to change what you can do, backed by a real card; you need not run it) and it repeats nothing
  you already have (`jev.py` holds the definitions and `IDEA_RULE`).
  When only one judge says sauce (Jev is strict about "usable now"; Claude often says yes to what you could act on
  later), a new candidate becomes an **idea**: an item with a quiet Idea mark in the Feed and the Ideas filter. Rate
  an idea α and it becomes a find; until then it feeds no skill.
- **Taste**: Claude writes a short profile of what you keep and what you would call slop from everything you picked
  (X bookmarks, GitHub stars, Hugging Face likes, sampled so each source is represented) and your α / β ratings on
  items and suggestions, and rewrites it weekly or on the next run after a rating changes (`taste.py`). Your
  timelines, your Substack subscriptions, your Hugging Face follows, and the trending feeds are judged against it,
  with the three most similar picked items as examples and up to three similar β items as counter-examples, so a
  post you would have saved gets in even when it fits no project.
- **Suggestions**: for every item that fits a project (and every find or idea, for the project it was found for),
  Claude (`skill_model`, Opus) rewrites why it fits as one action, in one call: a headline (the benefit for the
  project in 6 to 12 plain words, no model names or jargon), the outcome to expect (one plain sentence), how
  ("In <project>: do X, because Y; first step: Z"), and the effort (small, medium, large). It looks for the lightest
  action that captures the benefit: take the idea, not necessarily the artifact, using what the project already
  has (its stack and hosted models such as Claude). Heavy adoption (a local model, training, GPUs, large datasets,
  new infrastructure) is suggested only when nothing lighter works; when that is the only path and the benefit is
  small, Claude skips the pair and it is stored as dismissed (`pipeline.suggest_pending`, 30 per run; suggestions
  written before headlines get theirs from `pipeline.frame_pending`, many per `claude_model` call). Rate them α / β
  on the item's page and on each project's page, mark them done, Implement them, and agents read them over MCP
  (`suggestions`, and "Suggestions you kept" in `project_context`).
- **Context** is separate from sources. Claude writes one focus brief per active project from your recent agent
  prompts, commits, dictations, and meetings (`focus.py`, each feed enabled under `[context.<feed>]`). Briefs say
  what you are working on; they drive the discovery queries and the judge. Edit them on the dashboard's Projects
  tab (each project's page). The Projects tab also lists the meetings the last scan read; Exclude (or Use again)
  takes effect at once, and every brief written from meetings before that change is rewritten on the next run.
- **Requests**: an agent (or you) can ask for discovery: the MCP `request` tool, the form on the dashboard's Requests
  tab, or the "Ask Alpha to find it" form under a Feed search (which offers itself when the search finds nothing).
  Each one is a row in the `requests` table (`query`, optional `project`, `created_at`). The next run searches every
  enabled discovered source for it, judges what it finds, and records how many items it admitted (`status` open →
  judging → done, `result_count`).
- **The warehouse** (`var/alpha.db`) holds `items` (id, source, kind, trust, url, author, text, raw source JSON, and
  the processed title, summary, category, duplicate link), `content` (everything an item points at: images and
  their transcriptions, threads, quoted posts, articles, linked pages), `relevance` per project, `candidates`,
  `requests`, focus briefs, and skills, plus an FTS5 index (`items_fts`) over title, summary, text, and content.
  Agents read only the warehouse; they never call sources.
- **Outputs depend on kind** (post, article, dataset, model, space, paper, repo). Every kind is matched to projects.
  Posts, articles, papers, and repos can feed skills; datasets, models, and Spaces never do and show as project
  resources.

Processing (`alpha process`, also the end of every run): linked pages are fetched, images transcribed (Claude),
items summarized (Claude; the title is the takeaway, what the item lets you do or learn, benefit first, not its
name), categorized and checked for repeats (Jev), scored 0 to 3 against each project (Jev), explained where they
match (Claude), indexed for search, and checked against active skill topics (Jev).

A project's spec is a note `<project_notes_directory>/<name>.md`, or the README/AGENTS/CLAUDE files of a repository
in `[project_repositories]` (local) or `[project_github]` (via `gh`). A changed spec re-scores every item.

## Sources

| Source | Trust | Kind | Needs | Config |
| --- | --- | --- | --- | --- |
| `x` | curated | post | `X_AUTH_TOKEN`, `X_CT0` (cookies of a logged-in x.com tab), [Lightpanda](https://github.com/lightpanda-io/browser) (`brew install lightpanda-io/browser/lightpanda`) | `[sources.x]`: `every_hours` (20), `post_pages` (40) |
| `github_stars` | curated | repo | `gh auth login`, or `GITHUB_TOKEN` in .env | `[sources.github_stars]`: `every_hours` (6), `readmes` (300), `readme_characters` (12000) |
| `x_timeline` | discovered | post | same as `x` (one X session per run with the bookmarks) | `[sources.x_timeline]`: `every_hours` (20), `following_scrolls` (60, a safety cap: Following stops at posts already seen), `for_you_scrolls` (30) |
| `substack` | discovered | article | nothing beyond Claude and Jev | `[sources.substack]`: `every_hours` (20), `max_requests` (120), `request_delay_seconds` (3) |
| `substack_following` | discovered | article | `SUBSTACK_SID` in .env (the `substack.sid` cookie of a logged-in substack.com tab) | `[sources.substack_following]`: `every_hours` (20), `max_requests` (300), `first_days` (7), `per_publication` (10) |
| `papers` | discovered | paper | nothing (free public APIs) | `[sources.papers]`: `every_hours` (20), `max_requests` (150), `daily_papers` (50) |
| `huggingface` | discovered | dataset | nothing (free public API) | `[sources.huggingface]`: `every_hours` (168), `max_requests` (40), `min_downloads` (20) or `min_likes` (2) |
| `hf_likes` | curated | model, dataset, space | `HF_TOKEN` in .env, or a token saved by `hf auth login` | `[sources.hf_likes]`: `every_hours` (6), `max_requests` (100), `cards` (40), `card_characters` (12000) |
| `hf_following` | discovered | model, dataset, space | same as `hf_likes` | `[sources.hf_following]`: `every_hours` (20), `max_requests` (150), `first_days` (14), `per_listing` (100) |
| `github_trending` | discovered | repo | same sign-in as `github_stars` (for READMEs) | `[sources.github_trending]`: `every_hours` (20), `max_requests` (60), `languages` (python, typescript, swift) |
| `hf_trending` | discovered | model, dataset, space | same as `hf_likes` | `[sources.hf_trending]`: `every_hours` (20), `max_requests` (60), `per_listing` (50) |

X is read through x.com in a headless browser, slowly on purpose (4 to 9 s between scrolls, 12 to 25 s between
post pages, at most `post_pages` a day, one session per run for bookmarks, post pages, and timelines, and the first
refusal stops all X reading until the next run): X's terms do not allow automated access. The timelines skip
promoted posts and plain reposts. Substack is read through its public JSON, one request at a time, cached, stopping at the first 429/403;
paywalls are never worked around (paid posts are judged on their public preview and marked). Papers reads Hugging
Face Daily Papers each day plus paper search for the needs weekly; only papers with a code repository or models or
datasets on Hugging Face reach the judge, and their text is the title, abstract, what was released, and the
repository README's opening (no PDFs).

Substack subscriptions reads the newsletters you subscribe to. Your session cookie goes to substack.com only, in one
request per run, to list your subscriptions; each publication's newest posts and their text are then read without
it, through the same polite, cached client as Substack search (one archive page per publication per run, oldest-read
first when the budget runs short). Each run reads what was published since the last complete listing (the first
run: the last 7 days), skips posts Alpha has already seen from any source, and judges the rest like your timelines:
against your briefs and your taste. Paid posts are judged on their public preview, even ones you pay for.

Hugging Face likes are stored newest first and ranked like GitHub stars; each item's text is the repo's key
metadata (task, license, size, downloads, likes) and its card. Hugging Face follows lists, each run, the models,
datasets, and Spaces that the organizations and people you follow created or updated since the last run (the first
run: the last 14 days), skips what is already stored, and collapses obvious variants of one release (quantizations,
GGUF and MLX copies, precision and instruct/base variants, checkpoints) into its main repo, which lists the variant
names, so the judge sees each release once. It is judged like your timelines: against your briefs and your taste.

The two trending sources are feeds judged like your timelines. GitHub trending reads github.com/trending (today and
this week) and the daily page of each language in `languages`, like a browser (no API exists for it), skips repos
you starred or Alpha has seen, and gives the judge each repo's description, topics, stars, the stars it gained, and
README; admitted repos can feed skills like stars. Hugging Face trending reads the Hub's trending models, datasets,
and Spaces, drops obvious junk Spaces (adult or "uncensored" apps, face swaps, test Spaces) by name and tags, skips
what you liked or Alpha has seen, and collapses variants as follows does.

`alpha run` runs each enabled source when its cadence is due; `alpha collect [source]` and `alpha discover [source]`
run one now regardless of cadence (`alpha discover substack --max-requests 10` for a small budget).

## Adding a source

A source is one module in `alpha/sources/` plus an entry in `SOURCES` in `alpha/sources/__init__.py` and a
`[sources.<name>]` table in `config.example.toml`. The interface (from `alpha/sources/__init__.py`):

```
Every source declares:
    NAME     registry name; also the `source` of its items and the prefix of their ids (`<name>-<native id>`;
             X posts keep their numeric ids)
    TRUST    "curated" (items the user picked; stored as they are) or "discovered" (items Alpha finds; each
             candidate goes through the shared judge, judge.py, before it becomes an item)
    KINDS    the item kinds it produces: post, article, dataset, model, space, paper, repo
    SECRETS  names it needs in .env
    TOOLS    executables it needs on PATH
    CONFIG   its `[sources.<name>]` settings with defaults; always `enabled` (False) and `every_hours` (cadence)
    missing() -> list[str]   optional: what else it still needs, when SECRETS and TOOLS cannot say (either of two
             ways to sign in, say)

A curated source exposes
    collect(connection, settings) -> stats    store new items with storage.store_item (and add_content);
                                              stats is a dict of counts; a `stopped` entry skips enrich
    enrich(connection, settings)              optional: fetch more for stored items (threads, comments)
A discovered source exposes
    discover(connection, settings, needs) -> candidates
                                              search each judge.Need (a query from a focus brief or an open
                                              request), set need.searched on the ones it searched, and return
                                              judge.Candidate objects; raise nothing for a spent budget, just stop
    fetch_text(connection, settings, candidate) -> text
                                              the full text of one stored candidate row, for stage 2 and the item;
                                              raise judge.Stop when the source cannot answer more this run
  optional for a discovered source:
    TASTE = True        judge it against the user's taste (taste.py) as well as the briefs; stage 1 is batched
    SEARCHES = False    it reads a feed instead of searching needs, so open requests never start it
    admit(connection, settings, candidate, rank)   store an admitted candidate itself (default: storage.store_item)
Any source may expose close(), called once at the end of a run (x.py ends its shared X session there).
```

Read options with `config.source_config(settings, NAME, CONFIG)`. A source may keep its own tables (named
`<name>_*`, created on first use), as `substack.py` does for its cache. Availability (secrets and tools present)
is checked by the registry; `alpha setup` and `alpha status` report it.

## Dashboard

`alpha serve` (or the launchd job from `alpha install`) serves http://localhost:3457: the Feed, each item's page and
skill chat, Projects, Skills, and Requests. The Feed is the stream and little else: each item's title, one line (the
judge's why for a find, with a "how?" that opens where it came from, both judges' calls, and the bookmarks the judge
was shown; a find or an idea with a suggestion leads with its headline instead, see Suggestions below), its source,
and α / β; search and filters wait behind one control, and one quiet line above the list
says what you are looking at and links to what the judge turned down. Items Alpha stored since your last visit are
marked new until the visit ends (30 minutes without a page view) or you press Mark all seen; where they end, a card
says you are caught up and when the next run is, then Older follows, with a divider each time the list reaches an
older day (the list is in rank order, so a day's label never repeats further down). The status dot in
the header pulses while a run is going and opens the run ledger, where a failed source shows red with its error.
Press `?` for the keyboard shortcuts (`j`/`k` through a list, `o` to open, `a`/`b` to rate, `g` then
`f`/`p`/`s`/`r` for the tabs, `/` to search, Esc to leave a comparison or a readback).

**Filters.** Every filter option shows how many items it would leave; one that would leave none stays, grayed, with
its 0. Typing a search also lists the categories, projects, sources, and authors whose names match, above the
results. On an item's page its source, author, category, and projects each open the Feed filtered to them.

**Two banners.** Routine status stays in the dot. Two X states come forward as a banner across the top of every
page until they clear: the X session expired (the bookmarks page loaded none; copy `auth_token` and `ct0` from a
logged-in x.com tab into `X_AUTH_TOKEN` and `X_CT0` in .env) and X refused or throttled a read. `x.py` records
them in the metadata keys `x_session_expired` and `x_refused` (JSON `{"at", "reason"}`), and the next successful
X read clears both.

**Read back before it starts.** Build this skill, Rebuild, and Build in the skill chat first show what the build
will do: the skill's name and scope, the items with their titles, the test task, the model, what happens after,
and the time (10 to 30 minutes); for the chat's plan, what you said (your brief, your messages) set apart from what
Claude assumed. The build starts only on Confirm. Ask Alpha does the same: what it will look for, for which
project, which sources search, and when the next run is. α / β and other cheap actions stay one tap.

**Skill updates side by side.** An update waiting on the Skills tab opens a comparison
(`/skills/<name>/compare/<version>`): the live version and the new one in two columns, each with its SKILL.md
(readable, changed lines marked), its per-criterion scores, and its test report; the verdict, the items it adds and
drops, and Publish / Reject sit under both. On a phone it is one column with a Live / New switch.

**α and β.** Every item has two buttons: α (a real edge, keep it) and β (no edge: generic, slop, or something you
already knew; in finance, beta is what everyone already gets). Tap the active one again to clear it; after β you can
add a reason (slop, not interested, not now, knew it). β items leave the default Feed (the Rated β filter still
finds them), and every rating teaches the judge: the next run rewrites your taste profile with your α items as what
you keep and your β items as what you reject, and the stage-2 judges see the β items most like each new post as
counter-examples.

**Suggestions.** A find's or idea's Feed row leads with what Alpha suggests doing with it: the headline (the benefit),
the outcome to expect, then the item and source it came from; the how and the judge's why are under "how?" and on the
item's page. Each project's page lists its suggestions under What to do, headline and outcome first, opening to the
how and the item: the ones you rated α first, then the unrated; β-rated and done ones each have a filter.

**Ask about this.** Under each suggestion on an item's page (and in a project's list, once opened), ask questions
before you implement it, for example "how would this affect X, and how much work is it?". Claude (`claude_model`)
answers in a few plain sentences (impact, cost, risk, effort) from the suggestion, the item's full text, and the
project's spec and focus brief, and says it doesn't know when they don't tell (`suggestion_chat.py`). The
conversation is kept per suggestion until Start over; Implement's readback says it will be given to the agent, and
your decisions and constraints in it take precedence over the suggestion. Agents see it over MCP (`get_item`,
`suggestions`: the number of questions and the last answer).

**Implement.** Implement on a suggestion first reads back what it will do: the suggestion and its item, the
repository folder, the new branch `alpha/<slug>`, the model (`claude_model`), the time, and the guarantees. On
Confirm, `alpha implement` (`implement.py`) makes a git worktree of the project's local repository (its
`[project_repositories]` folder, or a repository with its name under `[context] projects_directory`) on that branch
from its current HEAD, in `var/worktrees/<project>-<slug>`, so your own working folder is never touched. A headless
Claude Code agent (the skill test agent's clean environment: no MCP servers, don't-ask mode) carries out the first
step there in 30 minutes at most: it may read, edit files in the worktree, fetch pages, and run the project's tests,
checks, and dependency installs; no commit, push, or deploy. Alpha then commits the change on the branch itself, as
you (your git identity and hooks, no trailers), "Alpha: <headline>" with the item's link; nothing is pushed,
deployed, published, sent, or bought. One implementation runs at a time (`var/implement.lock`). The suggestion
shows Implementing… with its running time, then Implemented with the branch and Review changes (the agent's report,
the diff against the commit it started from, the commands to merge or delete the branch, and Remove, asked first),
or Failed with the reason and Try again. Marking it done stays yours. A project with no local repository (a GitHub
repository or a vault note only) gets a ready-to-run task file instead, `var/handoffs/<project>/<slug>.md` (the
suggestion, the item's text, the first step, and acceptance checks); nothing runs.

## Agents (MCP)

Alpha serves its warehouse to agents over MCP (stdio). Register it once per harness; the command works from any
folder:

- Claude Code: `claude mcp add --scope user alpha -- uv run --project /path/to/alpha alpha mcp`
- Codex (`~/.codex/config.toml`): `[mcp_servers.alpha]` with `command = "uv"`,
  `args = ["run", "--project", "/path/to/alpha", "alpha", "mcp"]`
- omp (`~/.omp/agent/mcp.json`), under `mcpServers`:
  `"alpha": {"type": "stdio", "command": "uv", "args": ["run", "--project", "/path/to/alpha", "alpha", "mcp"]}`

Then ask any chat "check Alpha for local SEO". Tools: `search`, `get_item`, `project_context`, `list_projects`,
`skills`, `suggestions` (what to do with items in each project, α first, each with its implementation status and
branch), and `request` (the only write: it files a discovery request that the next `alpha run` handles).

Answers are short briefs an agent can use correctly, modeled on the Interface Lineage MCP. Each `search` result
gives the title, source, who vouches for it (saved by you / found by Alpha), date, url, one line on why it matters
(the judge's why for a find, otherwise the best project match's reason), the projects it fits, and your α / β.
α items rank first, and β items stay out unless `include_skipped=True`. `get_item` opens with a brief, followed by
the full item. `project_context` opens with the project's brief, then lists its top items (α first) and its live
skills. When Alpha has nothing, it says so and offers `request` instead of weak matches. An example from
`get_item` (the claim and the why are cut short here):

```text
**Brief for your answer** (Alpha's reading of this item):
- Claim: The post argues that a standard map embed gives Google little context about a business's physical
  proximity, while custom maps showing driving directions from major local landmarks to the business address…
- Why it matters: for sitebuilder: Sitebuilder makes local-business websites from research into the owner's
  area, so this is a concrete idea for the site template… (fits sitebuilder 2.0, leadgen 1.7)
- Quote (verbatim, from its own text): "Embedding a standard map embed gives Google minimal context about your
  physical proximity."
- Evidence: you saved it, so no judge read it; Alpha's reading of the text: no sentence reports the author's own
  results, so treat its numbers and outcomes as claims.
- Your rating: not rated yet
- Suggest next: Mail-drop QR codes to boost Google Business Profile ranking (get_item('2064669669139607866'),
  https://x.com/i/web/status/2064669669139607866; shares direction, seo, signals); Grok Bot Prompts for Local SEO
  and Google Business Profile (…; shares maps, seo); Use Review Responses to Feed Google Job Details (…).
- Mark anything you add from outside Alpha as your own.
```

The quote is the item's most specific sentence (numbers, named tools, steps) copied exactly. The evidence gives
the judges' verdict for a find and Alpha's own reading of the text: first-person sentences with numbers count as
the author's results, and everything else counts as the author's claim. Suggest next lists other items in the
same live skill, then items that share at least two of the title's topical words.

## Skills

A topic is a repeatable process (an SEO audit, a cold email campaign) backed by at least three items from any
source: posts, articles, starred repos, and papers can all teach a step (datasets, models, and Spaces never feed
skills). `alpha run` has Opus propose topics once a week from everything collected (or `alpha skills propose`, or the
Skills tab); each proposal comes with a test task from your projects. The skill chat on an item's page plans one
from a single item. A build: Jev adds every item that fits (`topic_threshold`); Opus writes a typed spec told the
test task and which library skills overlap; code first repairs what needs no judgment (sets the approval flag the
rules require; a description over 1024 characters is shortened by Opus in one small call, or trimmed at a sentence
keeping "Use when"), then checks it against fixed rules (`skill_spec.py`: naming, cited
and checkable steps, approval on anything that publishes or spends, read-only scripts); uncited items are dropped;
SKILL.md is rendered from one template; each script runs once; a headless Claude Code agent (`claude -p`) follows
the skill in report-only mode on the test task and Opus grades the report on six criteria (pass: mean 0.75, none under
0.5). A failed check goes back to Opus with every problem the build has seen and which are fixed, so a revision
cannot bring one back: up to five spec writes, at most three past the rules and two test runs. The test task is
whatever the skill works on: a URL, a repository, or a one-line scenario
("example.com: plan a comparison page against Wix for local trades"). An update is tested beside the live version
in the same build and must score at least as well. A passing first version is published to `skills_directory`
(and linked into `active_skills_directory` if set); later versions wait on the Skills tab for you to publish.

A failed build says on its card what went wrong in its last attempt, what Alpha tried (attempts, repairs), and
whether it retries: one that failed only on the rules or crashed is built once more by the next `alpha run`
(once per failure; the retry is never retried); one that failed its test run or the head-to-head waits for you.
**Try again** builds a new version starting from the failed one's problems (through the usual readback), **Change
the plan** opens the skill chat (or the test task field), and **Dismiss** rejects the version.

```sh
uv run alpha skills                             # topics and versions
uv run alpha skills approve NAME [--target TASK]  # activate, build, test; live if it passes
uv run alpha skills build NAME                  # build and test a new version
uv run alpha skills publish|reject|diff NAME [--version N]
uv run alpha skills remove NAME --item ID       # keep an item out of a skill for good
uv run alpha skills overlap                     # skills an agent could confuse, with a scope for each
```

## Privacy

Raw agent prompts, dictations, and meeting text go only to Claude, which writes the briefs; briefs carry no personal
details or names (Claude is told so, and known meeting participants, emails, and phone numbers are also removed in
code). Jev sees only briefs and item text. Search engines see only queries (topics, no names). Logs carry counts,
never text. The dashboard listens on 127.0.0.1 only. `.env`, `config.toml`, and `var/` are git-ignored.

## Costs

- Jev (TypeSafe): $0.042 per million input tokens. Processing a first batch of about 650 items cost about $0.42;
  a daily run with a few new items costs under $0.01; a Substack discovery pass about $0.02 to $0.05.
- Claude: image transcriptions, match reasons, briefs, queries, the Haiku judge, and suggestion questions are small
  calls; summaries and suggestions use Opus (suggestions at most 30 per run). Skill builds (Opus plus a Claude Code
  test run of up to 30 minutes) and Implement (a Claude Code run of up to 30 minutes) are the expensive part and run
  only when you ask or when a live skill's items change. `ALPHA_CLAUDE_BASE_URL` can point at a local proxy (such
  as CLIProxyAPI) that serves a Claude subscription instead of the pay-per-token API.
- Source reads are free: X and GitHub trending through their web pages, Substack, papers, GitHub, and Hugging Face
  through public APIs or your own sign-in.

## Tuning (`config.toml`)

- `relevance_threshold` (1.5 of 3): lower to see more project matches; scores are kept, so only `process` is needed.
- `relevance_guidance`: added to every relevance question (changing it re-scores everything).
- `duplicate_threshold` (0.85), `topic_threshold` (0.7): Jev probabilities for repeats and skill membership.
- `[judge]`: `fit_threshold` (0.4), `model` ("haiku"), `novelty` (0.6).
- `claude_model` ("sonnet"), `summary_model` ("opus"), `skill_model` ("opus"): a family name follows the newest
  release; a full id pins one. Summaries use Opus because they keep more of the specifics that search, matching,
  and skills rely on.
- `jev_model` ("jev-latest"): when the version behind it changes, scores are made again so versions never mix.
- `[categories]`: your categories; Jev picks one per item.
- `output_directory`: set it to write Markdown pages (one per project, an index, and all items); unset turns them off.
- `backup_directory`: daily copy of the database and images (14 kept); restore by copying `alpha-<date>.db` to
  `var/alpha.db` and `media` to `var/media`.

## Commands

```sh
uv run alpha setup | install | uninstall | status
uv run alpha run                 # sources on their cadence, open requests, then process
uv run alpha collect [source]    # curated now (x: --limit N, --full-history)
uv run alpha discover [source]   # discovered now (--max-requests N)
uv run alpha focus               # rewrite every focus brief now
uv run alpha process | render | serve [--port 3457]
uv run alpha mcp                 # MCP server for agents (stdio)
uv run alpha implement ITEM PROJECT  # carry out a suggestion on a new branch (the dashboard's Implement runs it)
```

Logs: `var/logs/run.log`, `var/logs/dashboard.log`, `var/logs/skills.log`. After changing code, restart the
dashboard with `launchctl kickstart -k gui/$(id -u)/com.$USER.alpha.dashboard`.
