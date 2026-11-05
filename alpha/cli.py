"""Command line: alpha {setup,install,uninstall,status,run,collect,discover,add,focus,process,render,serve,mcp,skills,
implement}."""

import argparse
import os
from datetime import UTC, datetime
from types import ModuleType

from . import enrich, focus, implement, judge, letthere, links, pipeline, skills
from .backup import backup
from .config import TOOL_ROOT, Settings, load_settings, missing_secrets, require_secrets
from .install import install, setup, uninstall
from .jev import Jev
from .projects import discover_projects, synchronize_projects
from .render import render
from .sources import SOURCES, close_all, due, enabled, last_run, mark_run, missing, options
from .sources import x as x_source
from .storage import connect, now


def _ago(timestamp: str | None) -> str:
    if not timestamp:
        return "never"
    hours = (datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds() / 3600
    return f"{hours * 60:.0f} min ago" if hours < 1 else f"{hours:.1f} h ago" if hours < 48 else f"{hours / 24:.0f} days ago"


def _status(settings: Settings) -> None:
    print(f"Alpha in {TOOL_ROOT}")
    for service, label in (("claude", "Claude"), ("jev", "Jev")):
        lacking = missing_secrets(service)
        print(f"  {label}: {'ready' if not lacking else 'missing ' + ', '.join(lacking)}"
              + (f" ({os.environ.get('ALPHA_CLAUDE_BASE_URL') or 'api.anthropic.com'})" if service == "claude" else ""))
    connection = connect(settings.database_path)
    print("Sources:")
    for source in SOURCES.values():
        config = options(settings, source)
        lacking = missing(source)
        ran, result = last_run(connection, source)
        state = "off" if not config["enabled"] else "ready" if not lacking else "missing " + ", ".join(lacking)
        print(f"  {source.NAME} ({source.TRUST}, {'/'.join(source.KINDS)}): {state}; every {config['every_hours']} h; "
              f"last run {_ago(ran)}" + (f": {result}" if result else ""))
    context = settings.context
    feeds = [feed for feed in ("agents", "git", "superwhisper", "anarlog") if getattr(context, feed)]
    print(f"Context feeds: {', '.join(feeds) or 'none'} (projects directory: {context.projects_directory or 'unset'})")
    print("Projects:")
    for project in discover_projects(settings):
        print(f"  {project.name}: {len(project.spec)} characters from {project.source}")
    print(f"Warehouse: {settings.database_path}")
    for row in connection.execute(
        "SELECT source, kind, trust, COUNT(*) AS n, COUNT(summary) AS summarized, COUNT(duplicate_of) AS repeats "
        "FROM items GROUP BY source, kind, trust ORDER BY n DESC"
    ):
        print(f"  {row['n']} {row['source']} {row['kind']}s ({row['trust']}): {row['summarized']} summarized, "
              f"{row['repeats']} repeats")
    matches = connection.execute("SELECT COUNT(*) FROM relevance WHERE score >= ?", (settings.relevance_threshold,)).fetchone()[0]
    candidates = ", ".join(f"{row['n']} {row['status']}" for row in connection.execute(
        "SELECT status, COUNT(*) AS n FROM candidates GROUP BY status ORDER BY n DESC"))
    requests = ", ".join(f"{row['n']} {row['status']}" for row in connection.execute(
        "SELECT status, COUNT(*) AS n FROM requests GROUP BY status"))
    print(f"  {matches} project matches; candidates: {candidates or 'none'}; requests: {requests or 'none'}")


def _collect(settings: Settings, connection, source: ModuleType) -> None:
    """One curated source: collect, then enrich."""
    lacking = missing(source)
    if lacking:
        print(f"{source.NAME}: skipped, missing {', '.join(lacking)}")
        return
    started, result = now(), "failed"
    try:
        stats = source.collect(connection, settings)
        if hasattr(source, "enrich") and not stats.get("stopped"):
            source.enrich(connection, settings)
        result = ", ".join(f"{key} {value}" for key, value in stats.items())
    except (Exception, SystemExit) as error:  # the other sources and processing still run
        result = f"failed: {error}" if isinstance(error, SystemExit) else f"failed: {type(error).__name__}: {error}"
        print(f"{source.NAME}: {result}")
    finally:
        mark_run(connection, source, started, result)


def _discover(settings: Settings, connection, chosen: list[ModuleType], cadence: bool) -> None:
    """Discovered sources: with `cadence`, a full pass only for those due (open requests are handled either
    way); without, a full pass for each. Then requests whose candidates are all judged are closed."""
    requests = judge.open_requests(connection)
    runnable = []
    for source in chosen:
        lacking = missing(source)
        if lacking:
            print(f"{source.NAME}: skipped, missing {', '.join(lacking)}")
            continue
        full = not cadence or due(connection, settings, source)
        if full or (requests and getattr(source, "SEARCHES", True)):
            runnable.append((source, full))
        else:
            print(f"{source.NAME}: not due (last run {_ago(last_run(connection, source)[0])}, "
                  f"every {options(settings, source)['every_hours']} h)")
    if not runnable:
        return
    jev = Jev(settings.jev_model)
    briefs = None
    if any(full for _, full in runnable):
        focus.refresh(connection, settings)
        briefs = focus.latest_briefs(connection)
        if not briefs:
            print("Discovery: no focus briefs yet (see [context] in config.toml); only open requests are searched")
    for source, full in runnable:
        wanted = judge.needs(connection, settings, source.NAME, options(settings, source).get("requery_days", 14),
                             requests, briefs if full else None) if getattr(source, "SEARCHES", True) else []
        started, result = now(), "failed"
        try:
            result = judge.run(connection, settings, jev, source, wanted)
        except Exception as error:  # discovery is extra; collected items are still processed
            result = f"failed: {type(error).__name__}: {error}"
            print(f"{source.NAME}: {result}")
        finally:
            if full:
                mark_run(connection, source, started, result)
    judge.close_requests(connection)
    cost = jev.usage.cost(settings.jev_price_per_million_input_tokens)
    print(f"Jev (discovery): {jev.usage.requests} requests, {jev.usage.input_tokens} input tokens, estimated cost ${cost:.4f}")


def _process(settings: Settings, connection) -> None:
    require_secrets("claude")
    require_secrets("jev")
    jev = Jev(settings.jev_model)
    pipeline.sync_jev_version(connection, jev)
    synchronize_projects(connection, settings)
    letthere.sync(connection, settings)  # notes made on an app while the dashboard was down
    enrich.fetch_links(connection)
    enrich.transcribe_images(connection, settings)
    pipeline.summarize_pending(connection, settings)
    pipeline.classify_pending(connection, settings, jev)
    pipeline.score_pending(connection, settings, jev)
    pipeline.explain_pending(connection, settings)
    pipeline.suggest_pending(connection, settings)
    pipeline.frame_pending(connection, settings)
    pipeline.index_pending(connection)
    skills.assign_pending(connection, settings, jev)
    skills.update_drafts(connection, settings)
    skills.retry_failed(connection, settings)
    render(connection, settings)
    cost = jev.usage.cost(settings.jev_price_per_million_input_tokens)
    print(f"Jev: {jev.usage.requests} requests, {jev.usage.input_tokens} input tokens, estimated cost ${cost:.4f}")
    backup(connection, settings)


def _named(name: str | None, trust: str, settings: Settings) -> list[ModuleType]:
    """The named source (run even when not enabled), or every enabled source of this trust."""
    if not name:
        return enabled(settings, trust)
    source = SOURCES.get(name)
    if source is None or source.TRUST != trust:
        names = ", ".join(n for n, s in SOURCES.items() if s.TRUST == trust)
        raise SystemExit(f"No {trust} source named {name}; {trust} sources: {names}")
    return [source]


def _override(settings: Settings, sources: list[ModuleType], **values) -> None:
    """Command-line options for this run only, for sources that have the setting."""
    for source in sources:
        for key, value in values.items():
            if value is not None and key in source.CONFIG:
                settings.sources.setdefault(source.NAME, {})[key] = value


def _skills(settings: Settings, connection, args) -> None:
    if args.action == "list":
        skills.show(connection, settings)
        return
    if args.action == "propose":
        require_secrets("claude")
        skills.propose(connection, settings)
        return
    if args.action == "overlap":
        require_secrets("claude")
        skills.overlap(connection, settings)
        return
    if not args.name:
        raise SystemExit(f"skills {args.action} needs a topic name")
    if args.action == "approve":
        require_secrets("claude")
        require_secrets("jev")
        skills.approve(connection, settings, args.name, args.target)
    elif args.action == "build":
        require_secrets("claude")
        if args.target:
            connection.execute("UPDATE topics SET test_target = ? WHERE name = ?", (args.target, args.name))
            connection.commit()
        skills.build(connection, settings, args.name)
    elif args.action == "publish":
        skills.publish(connection, settings, args.name, args.version)
    elif args.action == "reject":
        skills.reject(connection, args.name, args.version)
    elif args.action == "remove":
        if not args.item:
            raise SystemExit("skills remove needs --item ID")
        skills.remove(connection, args.name, args.item)
        print(f"Skills: item {args.item} removed from {args.name}; the next build leaves it out")
    else:
        print(skills.diff(connection, args.name, args.version or skills.versions(connection, args.name)[0]["version"]))


def main() -> None:
    parser = argparse.ArgumentParser(prog="alpha", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="check tools, create .env and config.toml, show what is missing")
    commands.add_parser("install", help="write and load the launchd jobs (daily run, dashboard) for this folder")
    commands.add_parser("uninstall", help="unload and remove the launchd jobs")
    commands.add_parser("status", help="sources, projects, and warehouse counts")
    commands.add_parser("run", help="each enabled source on its cadence, open requests, then process")
    collect = commands.add_parser("collect", help="read a curated source now (default: every enabled one)")
    collect.add_argument("source", nargs="?")
    collect.add_argument("--limit", type=int, help="x: read at most this many bookmarks")
    collect.add_argument("--full-history", action="store_true", default=None,
                         help="x: scroll through every bookmark instead of stopping at known ones")
    discover = commands.add_parser("discover", help="run a discovered source now and judge what it finds")
    discover.add_argument("source", nargs="?")
    discover.add_argument("--max-requests", type=int, help="request budget for this run")
    add = commands.add_parser("add", help="add a page you found (high signal: stored as yours, rated α, read now, "
                              "then processed) and pin it to a project")
    add.add_argument("url")
    add.add_argument("--note", help="why it is alpha: becomes the item's own words and the match reason")
    add.add_argument("--project", help="pin a full-score match to this project (a name from [projects])")
    add.add_argument("--later", action="store_true", help="leave the summary, matches, and suggestions to the next run")
    commands.add_parser("focus", help="rewrite every focus brief now")
    commands.add_parser("process", help="summarize, classify, match, explain, index, and render")
    commands.add_parser("render", help="rebuild the vault pages from the database only")
    serve = commands.add_parser("serve", help="browser dashboard on localhost")
    serve.add_argument("--port", type=int, default=3460)
    commands.add_parser("mcp", help="MCP server (stdio) so agents can search the warehouse: search, get_item, "
                        "project_context, list_projects, skills, suggestions, request, add")
    skill = commands.add_parser("skills", help="propose, build, test, and publish skills made from item topics")
    skill.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "propose", "approve", "build", "publish", "reject", "diff", "remove", "overlap"],
    )
    skill.add_argument("name", nargs="?", help="topic name")
    skill.add_argument("--target", help="approve/build: the test task (a URL, a repository, or a one-line scenario)")
    skill.add_argument("--version", type=int, help="publish/reject/diff: a specific version (default: the latest)")
    skill.add_argument("--item", help="remove: the item id to take out of the skill for good")
    carry = commands.add_parser("implement", help="carry out a suggestion on a new branch in a worktree of the "
                                "project's repository (the dashboard's Implement starts it after its readback)")
    carry.add_argument("item", help="the suggestion's item id")
    carry.add_argument("project")
    args = parser.parse_args()

    if args.command == "mcp":  # stdout is the JSON-RPC channel: nothing may print before this
        from . import mcp as mcp_server

        mcp_server.main()
        return
    if args.command == "setup":
        setup()
        return
    if args.command == "install":
        install()
        return
    if args.command == "uninstall":
        uninstall()
        return
    settings = load_settings()
    if args.command == "status":
        _status(settings)
        return
    if args.command == "serve":
        from .dashboard import serve as serve_dashboard

        serve_dashboard("127.0.0.1", args.port)
        return
    connection = connect(settings.database_path)
    if args.command == "run":
        try:
            for source in enabled(settings, "curated"):
                if due(connection, settings, source):
                    _collect(settings, connection, source)
                else:
                    print(f"{source.NAME}: not due (last run {_ago(last_run(connection, source)[0])}, "
                          f"every {options(settings, source)['every_hours']} h)")
            _discover(settings, connection, enabled(settings, "discovered"), cadence=True)
        finally:
            close_all()
            x_source.record_status(connection)  # the dashboard's X banners: an expired session, a refusal
        _process(settings, connection)
        skills.propose_if_due(connection, settings)
    elif args.command == "collect":
        chosen = _named(args.source, "curated", settings)
        _override(settings, chosen, limit=args.limit, full_history=args.full_history)
        try:
            for source in chosen:
                _collect(settings, connection, source)
        finally:
            close_all()
            x_source.record_status(connection)
    elif args.command == "discover":
        require_secrets("claude")
        require_secrets("jev")
        chosen = _named(args.source, "discovered", settings)
        _override(settings, chosen, max_requests=args.max_requests)
        try:
            _discover(settings, connection, chosen, cadence=False)
        finally:
            close_all()
            x_source.record_status(connection)
    elif args.command == "add":
        project = links.project_named(connection, args.project)
        if args.project and project is None:
            names = [row["name"] for row in connection.execute("SELECT name FROM projects ORDER BY name")]
            raise SystemExit(f"No project named '{args.project}' has a spec. Projects: {', '.join(names) or 'none'}")
        try:
            added = links.add(connection, args.url, args.note, project)
        except ValueError as error:
            raise SystemExit(str(error)) from None
        print(f"{'Added' if added.new else 'Already in Alpha, now rated α'}: {added.title} ({added.kind}, "
              f"{added.item_id}; {added.text_characters:,} characters read"
              + (f"; page not read: {added.error}" if added.error else "")
              + (f"; pinned to {project}" if project else "") + ")")
        if not args.later:
            _process(settings, connection)
    elif args.command == "focus":
        require_secrets("claude")
        focus.refresh(connection, settings, force=True)
    elif args.command == "process":
        _process(settings, connection)
    elif args.command == "render":
        render(connection, settings)
    elif args.command == "skills":
        _skills(settings, connection, args)
    elif args.command == "implement":
        require_secrets("claude")
        implement.run(connection, settings, args.item, args.project)
