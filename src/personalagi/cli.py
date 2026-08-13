"""Command-line entry point.

    python -m personalagi auth personal
    python -m personalagi ingest --account personal
    python -m personalagi ingest --all --limit 50
    python -m personalagi status
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from personalagi.config import get_settings


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="personalagi", description="Personal context system")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="run the OAuth consent flow for one account")
    auth.add_argument("account", help="account label from GMAIL_ACCOUNTS")

    ingest = sub.add_parser("ingest", help="pull new mail into SQLite")
    target = ingest.add_mutually_exclusive_group(required=True)
    target.add_argument("--account", help="account label from GMAIL_ACCOUNTS")
    target.add_argument("--all", action="store_true", help="every account in GMAIL_ACCOUNTS")
    ingest.add_argument(
        "--limit", type=int, default=None, help="cap messages this run (0 = no cap)"
    )
    ingest.add_argument(
        "--workers", type=int, default=None,
        help="concurrent message fetches (default 1; each thread gets its own client)",
    )
    ingest.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be fetched, write nothing, leave the watermark alone",
    )

    backfill = sub.add_parser(
        "backfill",
        help="walk backwards into older mail a truncated bootstrap never fetched",
    )
    backfill.add_argument("--account", required=True, help="account label from GMAIL_ACCOUNTS")
    backfill.add_argument(
        "--until",
        type=date.fromisoformat,
        default=None,
        help="stop at this date (YYYY-MM-DD); omit to walk to the beginning",
    )
    backfill.add_argument("--limit", type=int, default=None, help="cap messages this pass")
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument("--workers", type=int, default=None, help="concurrent fetches")
    backfill.add_argument(
        "--loop",
        action="store_true",
        help="repeat passes until no older mail remains",
    )

    sync = sub.add_parser(
        "sync-events",
        help="project ingested mail into canonical Events (ARCHITECTURE.md D1)",
    )
    sync.add_argument("--account", default=None)
    sync.add_argument("--limit", type=int, default=None)
    sync.add_argument(
        "--rebuild",
        action="store_true",
        help="re-project every message, not only those without an event",
    )

    cal = sub.add_parser(
        "calendar",
        help="ingest Google Calendar as Events (needs `auth calendar` first)",
    )
    cal.add_argument("--days-back", type=int, default=30)
    cal.add_argument("--days-forward", type=int, default=60)
    cal.add_argument("--calendar-id", default="primary")

    ims = sub.add_parser(
        "imessage",
        help="ingest iMessage as Events (read-only snapshot of chat.db)",
    )
    ims.add_argument("--days", type=int, default=90, help="how far back to read")
    ims.add_argument("--limit", type=int, default=None)

    headers = sub.add_parser(
        "refresh-headers",
        help="backfill message headers (List-Unsubscribe etc) for existing mail",
    )
    headers.add_argument("--account", default=None, help="default: every account")
    headers.add_argument("--limit", type=int, default=None)
    headers.add_argument("--workers", type=int, default=None)

    classify = sub.add_parser("classify", help="classify ingested mail with Groq")
    classify.add_argument("--account", default=None, help="account label; omit for all")
    classify.add_argument("--limit", type=int, default=None, help="cap messages this run")
    classify.add_argument(
        "--reclassify",
        action="store_true",
        help="re-run messages that already have a classification",
    )
    classify.add_argument("--workers", type=int, default=None, help="concurrent Groq calls")
    classify.add_argument("--dry-run", action="store_true")

    rel = sub.add_parser(
        "relevance",
        help="two-stage relevance: structural triage, then context-aware scoring",
    )
    rel.add_argument("--account", default=None)
    rel.add_argument("--limit", type=int, default=None)
    rel.add_argument("--rescore", action="store_true", help="re-score already-scored mail")
    rel.add_argument("--workers", type=int, default=None)
    rel.add_argument(
        "--dry-run",
        action="store_true",
        help="report what stage A filters without making any LLM call",
    )

    goal = sub.add_parser("goal", help="goals: the records that make absence a signal")
    goal_sub = goal.add_subparsers(dest="goal_command", required=True)

    g_add = goal_sub.add_parser("add", help="create a goal")
    g_add.add_argument("title")
    g_add.add_argument("--why", default="", help="why it matters; carried into nudges")
    g_add.add_argument("--deadline", type=date.fromisoformat, default=None)
    g_add.add_argument("--step", action="append", default=[], help="repeatable")
    g_add.add_argument(
        "--person", action="append", default=[],
        help="slug:role, e.g. pratik:recommender. Repeatable.",
    )

    g_list = goal_sub.add_parser("list", help="goals, soonest deadline first")
    g_list.add_argument("--all", action="store_true", help="include done/abandoned")

    g_show = goal_sub.add_parser("show", help="one goal with its steps and evidence")
    g_show.add_argument("slug")

    goal_sub.add_parser("sync", help="rebuild the index from context/goals/*.md")

    g_link = goal_sub.add_parser(
        "link", help="search all sources for evidence supporting each open step"
    )
    g_link.add_argument("--goal", default=None, help="slug; omit for every goal")
    g_link.add_argument("--limit", type=int, default=3, help="max links per step")
    g_link.add_argument("--dry-run", action="store_true")

    g_gaps = goal_sub.add_parser(
        "gaps", help="open steps with no supporting evidence anywhere"
    )
    g_gaps.add_argument("--days", type=int, default=30, help="deadline window")

    swp = sub.add_parser(
        "sweep", help="proactive check: what did NOT happen that should have"
    )
    swp.add_argument(
        "--dry-run", action="store_true",
        help="report what would be raised without recording anything",
    )
    swp.add_argument(
        "--show-suppressed", action="store_true",
        help="include what fell below the confidence bar, so blind spots stay visible",
    )
    swp.add_argument("--budget", type=int, default=None, help="max model calls")
    swp.add_argument(
        "--only", default=None,
        help="comma-separated kinds, e.g. deadline_gap,stale_commitment",
    )

    prep = sub.add_parser(
        "prep", help="what you should know before meeting someone; every claim cited"
    )
    prep.add_argument("who", help="person slug or email")
    prep.add_argument("--title", default="", help="what the meeting is about")

    act = sub.add_parser(
        "activate", help="what does this person or event connect to?"
    )
    act.add_argument("seed", help="person slug, or event:<id>")
    act.add_argument("--depth", type=int, default=None)
    act.add_argument("--limit", type=int, default=12)

    sub.add_parser("build-edges", help="derive the graph from existing records")

    meets = sub.add_parser("meetings", help="calendar events inside the next N hours")
    meets.add_argument("--hours", type=int, default=24)

    fb = sub.add_parser("feedback", help="record what you did with a proposal")
    fb.add_argument("proposal_id", help="id or unique prefix")
    fb.add_argument(
        "outcome",
        choices=["accepted", "edited", "dismissed", "ignored", "reversed"],
    )
    fb.add_argument("--note", default="", help="for `edited`: what you actually sent")

    fbh = sub.add_parser("proposals", help="the proposal ledger and its outcomes")
    fbh.add_argument("--limit", type=int, default=20)
    fbh.add_argument("--outcome", default=None)
    fbh.add_argument(
        "--age-out", action="store_true",
        help="mark unanswered surfaced proposals as ignored",
    )
    fbh.add_argument(
        "--explore", action="store_true",
        help="show suppressed proposals deliberately, so blind spots stay visible",
    )

    inv = sub.add_parser(
        "investigate", help="follow a question outward: search, read, repeat"
    )
    inv.add_argument("question")
    inv.add_argument("--max-iterations", type=int, default=None)
    inv.add_argument("--budget", type=int, default=None)

    imp = sub.add_parser(
        "import", help="ingest an export file (Claude/ChatGPT/Gemini/WhatsApp/LinkedIn)"
    )
    imp.add_argument("path", type=Path)
    imp.add_argument(
        "--format", default=None,
        choices=["claude", "chatgpt", "gemini", "whatsapp", "linkedin"],
        help="override detection",
    )
    imp.add_argument("--dry-run", action="store_true")

    facts = sub.add_parser("facts", help="future-dated statements; a date is a trigger")
    facts_sub = facts.add_subparsers(dest="facts_command", required=True)
    f_ex = facts_sub.add_parser("extract", help="pull facts out of relevant events")
    f_ex.add_argument("--limit", type=int, default=40)
    f_ex.add_argument("--min-relevance", type=int, default=2)
    f_ex.add_argument("--budget", type=int, default=40)
    f_add = facts_sub.add_parser("add", help="record one by hand")
    f_add.add_argument("statement")
    f_add.add_argument("--from", dest="valid_from", type=date.fromisoformat, required=True)
    f_add.add_argument("--until", dest="valid_until", type=date.fromisoformat, default=None)
    f_list = facts_sub.add_parser("list", help="known facts, soonest first")
    f_list.add_argument("--all", action="store_true")

    con = sub.add_parser(
        "contradictions", help="statements about one goal that cannot both be true"
    )
    con.add_argument("--goal", default=None)
    con.add_argument("--budget", type=int, default=10)

    owed = sub.add_parser("owed", help="open commitments, grouped by person")
    owed.add_argument(
        "--to-me",
        action="store_true",
        help="show what others owe you instead of what you owe them",
    )
    owed.add_argument("--person", default=None, help="filter to one person slug")
    owed.add_argument("--include-done", action="store_true")

    done = sub.add_parser("done", help="mark a commitment as fulfilled")
    done.add_argument("commitment_id", type=int)

    template = sub.add_parser(
        "labels-template",
        help="generate a CSV of real messages for you to hand-label",
    )
    template.add_argument("--out", type=Path, default=Path("evals/labels_template.csv"))
    template.add_argument("--n", type=int, default=30)
    template.add_argument("--account", default=None)
    template.add_argument(
        "--human-only",
        action="store_true",
        help="sample only human senders (drops robot and shared bulk addresses)",
    )
    template.add_argument(
        "--stratify",
        action="store_true",
        help="balance the sample across predicted categories",
    )

    ev = sub.add_parser("eval", help="score stored predictions against hand-labels")
    ev.add_argument("--labels", type=Path, default=Path("evals/labels.csv"))
    ev.add_argument(
        "--classify-missing",
        action="store_true",
        help="classify any labelled message that has no prediction yet",
    )

    build = sub.add_parser("context-build", help="fold classified mail into person files")
    build.add_argument("--account", default=None)
    build.add_argument("--limit", type=int, default=None)
    build.add_argument(
        "--all-categories",
        action="store_true",
        help="include promotional and spam (default: skip them)",
    )
    build.add_argument(
        "--include-automated",
        action="store_true",
        help="include noreply-style senders (default: skip them)",
    )

    show = sub.add_parser("context", help="retrieve context for one person")
    show.add_argument("person", help="slug, name, or email")
    show.add_argument("--query", default="", help="what you want relevant log lines about")
    show.add_argument("-k", type=int, default=5, help="max log lines to retrieve")

    find = sub.add_parser("search", help="full-text search across all log lines")
    find.add_argument("query")
    find.add_argument("--person", default=None)
    find.add_argument("-k", type=int, default=10)

    sub.add_parser("reindex", help="rebuild the FTS index from the markdown vault")

    compact = sub.add_parser("compact", help="fold new log entries into profiles")
    compact.add_argument("--person", default=None, help="slug; omit for everyone")
    compact.add_argument(
        "--force", action="store_true", help="recompact even if nothing changed"
    )
    compact.add_argument(
        "--min-entries", type=int, default=2, help="skip people with fewer log entries"
    )

    correct = sub.add_parser(
        "correct",
        help="record an authoritative correction that compaction cannot overwrite",
    )
    correct.add_argument("person")
    correct.add_argument("correction", help="e.g. 'Dana left Example Labs in June 2026'")

    history = sub.add_parser("profile-history", help="show how a profile evolved")
    history.add_argument("person")

    brief = sub.add_parser("brief", help="render the morning brief")
    brief.add_argument("--days", type=int, default=1, help="window size in days")
    brief.add_argument("--date", type=date.fromisoformat, default=None)
    brief.add_argument("--account", action="append", default=None)
    brief.add_argument("--no-write", action="store_true", help="print only")

    sub.add_parser("status", help="show per-account watermarks and counts")
    return parser


def _cmd_auth(args: argparse.Namespace) -> int:
    from personalagi.ingest.auth import authorize

    settings = get_settings()
    if args.account not in settings.account_labels:
        print(
            f"'{args.account}' is not in GMAIL_ACCOUNTS "
            f"({', '.join(settings.account_labels)}). Add it to .env first.",
            file=sys.stderr,
        )
        return 2
    authorize(args.account, settings)
    print(f"Authorized '{args.account}' -> {settings.token_path(args.account)}")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from personalagi.ingest.gmail import ingest_account, ingest_all

    settings = get_settings()
    if args.all:
        results = ingest_all(
            settings, max_messages=args.limit, dry_run=args.dry_run,
            fetch_workers=args.workers,
        )
    else:
        if args.account not in settings.account_labels:
            print(
                f"'{args.account}' is not in GMAIL_ACCOUNTS "
                f"({', '.join(settings.account_labels)}).",
                file=sys.stderr,
            )
            return 2
        results = [
            ingest_account(
                args.account, settings, max_messages=args.limit,
                dry_run=args.dry_run, fetch_workers=args.workers,
            )
        ]

    for result in results:
        print(result.summary())
    return 1 if any(r.mode == "failed" for r in results) else 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    from personalagi.ingest.gmail import backfill_account

    settings = get_settings()
    if args.account not in settings.account_labels:
        print(
            f"'{args.account}' is not in GMAIL_ACCOUNTS "
            f"({', '.join(settings.account_labels)}).",
            file=sys.stderr,
        )
        return 2

    passes = 0
    while True:
        result = backfill_account(
            args.account,
            settings,
            until=args.until,
            max_messages=args.limit,
            dry_run=args.dry_run,
            fetch_workers=args.workers,
        )
        passes += 1
        print(result.summary())
        # Stop on: not looping, nothing left, dry run, or no forward progress.
        # Progress is `inserted`, not `fetched`: each pass re-reads the cursor
        # second by design, so `fetched` is never 0 and would spin forever.
        if not args.loop or not result.truncated or args.dry_run or result.inserted == 0:
            break
    if passes > 1:
        print(f"({passes} backfill passes)")
    return 0


def _cmd_sync_events(args: argparse.Namespace) -> int:
    from personalagi.adapters.gmail_adapter import sync_events

    events, participants = sync_events(
        get_settings(),
        account=args.account,
        limit=args.limit,
        rebuild=args.rebuild,
    )
    print(f"{events} event(s), {participants} participant(s)")
    return 0


def _cmd_calendar(args: argparse.Namespace) -> int:
    from personalagi.adapters.calendar import CalendarNotAuthorized, sync_calendar

    try:
        result = sync_calendar(
            get_settings(),
            calendar_id=args.calendar_id,
            days_back=args.days_back,
            days_forward=args.days_forward,
        )
    except CalendarNotAuthorized as exc:
        print(f"calendar not authorized:\n{exc}")
        return 2
    print(result.summary())
    return 0


def _cmd_imessage(args: argparse.Namespace) -> int:
    from personalagi.adapters.imessage import IMessageUnavailable, sync_imessage

    try:
        result = sync_imessage(get_settings(), days=args.days, limit=args.limit)
    except IMessageUnavailable as exc:
        # Fail with a clear message and a non-zero code rather than a
        # traceback: a missing permission is a setup problem, not a crash.
        print(f"iMessage unavailable: {exc}")
        return 2
    print(result.summary())
    return 0


def _cmd_refresh_headers(args: argparse.Namespace) -> int:
    from personalagi.ingest.gmail import refresh_headers

    settings = get_settings()
    labels = [args.account] if args.account else settings.account_labels
    total = 0
    for label in labels:
        if not settings.token_path(label).exists():
            print(f"{label}: NOT AUTHORIZED - skipping")
            continue
        result = refresh_headers(
            label, settings, limit=args.limit, fetch_workers=args.workers
        )
        print(result.summary())
        total += result.updated
    print(f"\n{total} message(s) now have headers.")
    return 0


def _cmd_classify(args: argparse.Namespace) -> int:
    from personalagi.llm.classify import classify_account

    settings = get_settings()
    if args.account and args.account not in settings.account_labels:
        print(f"'{args.account}' is not in GMAIL_ACCOUNTS.", file=sys.stderr)
        return 2

    result = classify_account(
        args.account,
        settings,
        limit=args.limit,
        reclassify=args.reclassify,
        workers=args.workers,
        dry_run=args.dry_run,
    )
    print(result.summary())
    return 0


def _cmd_relevance(args: argparse.Namespace) -> int:
    from personalagi.llm.relevance import RelevanceError, score_messages

    try:
        result = score_messages(
            get_settings(),
            account=args.account,
            limit=args.limit,
            rescore=args.rescore,
            workers=args.workers,
            dry_run=args.dry_run,
        )
    except RelevanceError as exc:
        print(f"error: {exc}")
        return 2
    print(result.summary())
    return 0


def _cmd_goal(args: argparse.Namespace) -> int:
    from personalagi import goals as goals_mod

    settings = get_settings()
    command = args.goal_command

    if command == "add":
        people = {}
        for entry in args.person:
            slug, _, role = entry.partition(":")
            if slug.strip():
                people[slug.strip()] = role.strip() or "contact"
        try:
            created = goals_mod.create_goal(
                args.title, settings, why=args.why, deadline=args.deadline,
                steps=args.step, people=people,
            )
        except goals_mod.GoalError as exc:
            print(f"error: {exc}")
            return 2
        print(f"created {goals_mod.goal_path(settings, created.slug)}")
        if not args.step:
            print("No steps yet. A goal with no steps cannot have a gap, so it")
            print("will never trigger anything -- add some with the file or --step.")
        return 0

    if command == "sync":
        print(goals_mod.sync_goals(settings).summary())
        return 0

    if command == "list":
        views = goals_mod.list_goals(settings, status=None if args.all else "active")
        if not views:
            print("No goals. `personalagi goal add \"...\"` to start.")
            return 0
        for view in views:
            gaps = len(view.unevidenced_steps)
            days = view.days_left
            when = f"{days}d" if days is not None else "no deadline"
            flag = f"  {gaps} step(s) with nothing behind them" if gaps else ""
            print(f"{view.goal.slug:28} {when:>12}  {view.goal.title}{flag}")
        return 0

    if command == "show":
        view = goals_mod.get_goal(args.slug, settings)
        if view is None:
            print(f"no goal '{args.slug}'")
            return 1
        print(goals_mod.render_goal(view))
        return 0

    if command == "link":
        from personalagi.goal_evidence import link_evidence

        result = link_evidence(
            settings, goal_slug=args.goal, limit_per_step=args.limit,
            dry_run=args.dry_run,
        )
        print(result.summary())
        return 0

    if command == "gaps":
        from datetime import timedelta

        cutoff = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=args.days)
        found = False
        for view in goals_mod.list_goals(settings):
            missing = view.unevidenced_steps
            if not missing:
                continue
            if view.goal.deadline and view.goal.deadline > cutoff:
                continue
            found = True
            days = view.days_left
            when = f"deadline in {days}d" if days is not None else "no deadline"
            print(f"\n{view.goal.title}  ({when})")
            for step in missing:
                mark = "!!" if step.blocking else " -"
                print(f"  {mark} {step.description}")
        if not found:
            print("No gaps inside the window.")
            print("That is only meaningful if `goal link` has run -- otherwise it")
            print("means nobody looked, not that nothing is missing.")
        return 0

    return 1


def _cmd_sweep(args: argparse.Namespace) -> int:
    from personalagi.records import CallBudget
    from personalagi.sweep import (
        AGENDA,
        pending_proposals,
        render_proposals,
        sweep,
    )

    settings = get_settings()
    kinds = tuple(k.strip() for k in args.only.split(",")) if args.only else AGENDA
    budget = CallBudget(limit=args.budget) if args.budget is not None else None

    result = sweep(settings, budget=budget, dry_run=args.dry_run, kinds=kinds)
    print(result.summary())
    print()

    if args.dry_run:
        for finding in result.findings:
            bar = "surface " if finding.confidence >= settings.sweep_min_confidence else "suppress"
            print(f"  [{bar}] {finding.confidence:.2f} {finding.kind}: {finding.detail}")
        return 0

    rows = pending_proposals(settings, include_suppressed=args.show_suppressed)
    print(render_proposals(rows, show_suppressed=args.show_suppressed))
    return 0


def _cmd_build_edges(_: argparse.Namespace) -> int:
    from personalagi.activate import build_edges

    print(build_edges(get_settings()).summary())
    return 0


def _cmd_activate(args: argparse.Namespace) -> int:
    from dataclasses import replace

    from personalagi.activate import (
        DEFAULT_ACTIVATION,
        Node,
        activate,
        label_nodes,
        render_activation,
        seeds_for_event,
    )
    from personalagi.records import NodeType

    settings = get_settings()
    if args.seed.startswith("event:"):
        seeds = seeds_for_event(int(args.seed.split(":", 1)[1]), settings)
    else:
        seeds = [Node(NodeType.PERSON, args.seed)]

    limits = DEFAULT_ACTIVATION
    if args.depth is not None:
        limits = replace(limits, max_depth=args.depth)

    lit = label_nodes(activate(seeds, settings, limits=limits), settings)
    print(render_activation(lit, limit=args.limit))
    return 0


def _cmd_meetings(args: argparse.Namespace) -> int:
    from personalagi.prep import upcoming_meetings

    rows = upcoming_meetings(get_settings(), hours=args.hours)
    if not rows:
        print(f"No calendar events in the next {args.hours}h.")
        print("(Calendar is built but not authorized -- `personalagi auth calendar`.)")
        return 0
    for row in rows:
        print(f"{row.timestamp:%a %H:%M}  {row.title}  [{row.source_id}]")
    return 0


def _cmd_prep(args: argparse.Namespace) -> int:
    from personalagi.prep import build_prep, render_prep

    result = build_prep(args.who, get_settings(), title=args.title)
    if result is None:
        print(f"no person matching '{args.who}'")
        return 1
    print(render_prep(result))
    return 0


def _cmd_feedback(args: argparse.Namespace) -> int:
    from personalagi.feedback import FeedbackError, record_outcome

    try:
        row = record_outcome(
            args.proposal_id, args.outcome, get_settings(), note=args.note
        )
    except FeedbackError as exc:
        print(f"error: {exc}")
        return 2
    print(f"{row.proposal_id[:8]} -> {row.outcome}")
    return 0


def _cmd_proposals(args: argparse.Namespace) -> int:
    from personalagi.feedback import (
        exploration_sample,
        history,
        mark_ignored,
        render_history,
        stats,
    )

    settings = get_settings()
    if args.age_out:
        print(f"aged {mark_ignored(settings)} unanswered proposal(s) to ignored")

    if args.explore:
        rows = exploration_sample(settings)
        if not rows:
            print("Nothing suppressed. No blind spots to show.")
            return 0
        print("SUPPRESSED — shown on purpose, so the blind spots stay visible:")
        print()
        print(render_history(rows))
        return 0

    print(stats(settings).summary())
    print()
    print(render_history(history(settings, limit=args.limit, outcome=args.outcome)))
    return 0


def _cmd_investigate(args: argparse.Namespace) -> int:
    from personalagi.investigate import investigate
    from personalagi.records import CallBudget

    result = investigate(
        args.question,
        get_settings(),
        max_iterations=args.max_iterations,
        budget=CallBudget(limit=args.budget) if args.budget else None,
    )
    print(result.render())
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    from personalagi.adapters.exports import ExportError, ingest_export

    try:
        result = ingest_export(
            args.path, get_settings(), fmt=args.format, dry_run=args.dry_run
        )
    except ExportError as exc:
        print(f"error: {exc}")
        return 2
    print(result.summary())
    if args.dry_run:
        print("\n(dry run -- nothing was written)")
    return 0


def _cmd_facts(args: argparse.Namespace) -> int:
    from personalagi import facts as facts_mod
    from personalagi.records import CallBudget

    settings = get_settings()
    if args.facts_command == "extract":
        result = facts_mod.extract_facts(
            settings, limit=args.limit, min_relevance=args.min_relevance,
            budget=CallBudget(limit=args.budget),
        )
        print(result.summary())
        for statement in result.statements:
            print(f"  + {statement}")
        return 0

    if args.facts_command == "add":
        fact = facts_mod.add_fact(
            args.statement, args.valid_from, settings, valid_until=args.valid_until
        )
        print(f"recorded fact {fact.id}: {fact.statement}")
        return 0

    rows = facts_mod.list_facts(settings, status=None if args.all else "open")
    print(facts_mod.render_facts(rows))
    return 0


def _cmd_contradictions(args: argparse.Namespace) -> int:
    from personalagi.contradictions import find_contradictions, render_conflicts
    from personalagi.records import CallBudget

    result = find_contradictions(
        get_settings(), goal_slug=args.goal, budget=CallBudget(limit=args.budget)
    )
    print(render_conflicts(result))
    return 0


def _cmd_owed(args: argparse.Namespace) -> int:
    from personalagi.commitments import list_owed, refresh_stale, render_owed

    settings = get_settings()
    refresh_stale(settings)
    direction = "they_owe" if args.to_me else "i_owe"
    groups = list_owed(
        settings,
        direction=direction,
        include_done=args.include_done,
        person=args.person,
    )
    print(render_owed(groups, direction=direction))
    return 0


def _cmd_done(args: argparse.Namespace) -> int:
    from personalagi.commitments import close_commitment

    if close_commitment(args.commitment_id, get_settings()):
        print(f"commitment {args.commitment_id} marked done")
        return 0
    print(f"no commitment with id {args.commitment_id}")
    return 1


def _cmd_labels_template(args: argparse.Namespace) -> int:
    from personalagi.evals.harness import generate_template

    count = generate_template(
        args.out,
        get_settings(),
        n=args.n,
        account=args.account,
        human_only=args.human_only,
        stratify=args.stratify,
    )
    print(f"Wrote {count} rows to {args.out}")
    if args.human_only or args.stratify:
        # Say this at the point of use, not only in a doc nobody rereads: a
        # balanced sample measures separability, not what the inbox looks like.
        print(
            "\nNOTE: this sample is filtered/balanced, so its class frequencies\n"
            "are NOT your inbox's. Per-class precision and recall are still\n"
            "meaningful; overall accuracy is not comparable to a random sample."
        )
    print(
        "\nFill in true_category (needs_response|fyi|promotional|spam) and\n"
        "true_urgency (low|med|high), save as evals/labels.csv, then run:\n"
        "  python -m personalagi eval --labels evals/labels.csv --classify-missing"
    )
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from personalagi.evals.harness import evaluate_labels, read_labels
    from personalagi.evals.metrics import render_report

    settings = get_settings()
    labels = read_labels(args.labels)

    if args.classify_missing:
        from personalagi.llm.classify import classify_labelled

        classify_labelled([row.message_id for row in labels], settings)

    category_report, urgency_report, meta = evaluate_labels(labels, settings)

    print(render_report(category_report, title="Category"))
    print()
    print(render_report(urgency_report, title="Urgency"))
    print()
    print(f"model={meta['model']}  prompt={meta['prompt_version']}")
    print(f"labelled={meta['labelled']}  scored={meta['scored']}")
    if meta["missing_prediction"]:
        # Reported, never silently dropped: a shrinking denominator flatters
        # every metric above.
        print(
            f"WARNING: {len(meta['missing_prediction'])} labelled message(s) had no "
            "prediction and were excluded. Re-run with --classify-missing."
        )
    if meta["unclassified"]:
        print(f"NOTE: {len(meta['unclassified'])} message(s) failed to parse twice.")
    return 0


def _cmd_context_build(args: argparse.Namespace) -> int:
    from personalagi.context.store import DEFAULT_INCLUDE, build_people

    result = build_people(
        get_settings(),
        account=args.account,
        include_categories=("*",) if args.all_categories else DEFAULT_INCLUDE,
        include_automated=args.include_automated,
        limit=args.limit,
    )
    print(result.summary())
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    from personalagi.context.retrieve import get_context

    try:
        context = get_context(args.person, args.query, get_settings(), k=args.k)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if context is None:
        print(f"No person file for '{args.person}'. Run `context-build` first.")
        return 1

    print(context.render())
    print()
    print(context.stats())
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    from personalagi.db import get_engine
    from personalagi.search.fts import search

    hits = search(get_engine(get_settings()), args.query, person_slug=args.person, limit=args.k)
    if not hits:
        print("no matches")
        return 1
    for hit in hits:
        print(f"{hit.person_name or hit.person_slug:<24} {hit.render()}")
    return 0


def _cmd_reindex(_: argparse.Namespace) -> int:
    from personalagi.db import get_engine, init_db
    from personalagi.search.fts import reindex

    settings = get_settings()
    init_db(settings)
    count = reindex(get_engine(settings), settings.context_dir)
    print(f"Indexed {count} log line(s) from {settings.context_dir}")
    return 0


def _cmd_compact(args: argparse.Namespace) -> int:
    from personalagi.context.compact import compact_all

    result = compact_all(
        get_settings(),
        person=args.person,
        force=args.force,
        min_entries=args.min_entries,
    )
    print(result.summary())
    if result.people:
        print("  " + ", ".join(result.people[:10]))
    return 0


def _cmd_correct(args: argparse.Namespace) -> int:
    from personalagi.context.compact import add_correction

    try:
        person = add_correction(args.person, args.correction, get_settings())
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Correction recorded on {person.slug}:")
    for item in person.corrections:
        print(f"  - {item}")
    print(
        "\nThe profile is NOT edited directly - a direct edit would be undone by the\n"
        "next compaction. Run `personalagi compact --person "
        f"{person.slug} --force` to regenerate it under the correction."
    )
    return 0


def _cmd_profile_history(args: argparse.Namespace) -> int:
    import difflib

    from personalagi.context.compact import read_history
    from personalagi.context.retrieve import resolve_person

    settings = get_settings()
    try:
        found = resolve_person(settings.context_dir, args.person)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if found is None:
        print(f"no person file matching '{args.person}'", file=sys.stderr)
        return 1

    versions = read_history(settings.context_dir, found.slug)
    if not versions:
        print(f"no compaction history for {found.slug} yet")
        return 1

    previous = ""
    for index, record in enumerate(versions):
        print(f"--- version {index + 1}  {record['timestamp']}  "
              f"(entries={record['log_entries']}, prompt={record.get('prompt_version', '?')})")
        if previous:
            diff = difflib.unified_diff(
                previous.split(), record["profile"].split(), lineterm="", n=3
            )
            changed = [
                d
                for d in diff
                if d.startswith(("+", "-")) and not d.startswith(("+++", "---"))
            ]
            print("    changed: " + (" ".join(changed[:40]) or "(no change)"))
        else:
            print(f"    {record['profile']}")
        if record.get("corrections"):
            print(f"    corrections in force: {len(record['corrections'])}")
        previous = record["profile"]
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    from personalagi.brief import build_brief, render_brief, write_brief

    settings = get_settings()
    brief = build_brief(
        settings, day=args.date, window_days=args.days, accounts=args.account
    )
    print(render_brief(brief))
    if not args.no_write:
        path = write_brief(brief, settings)
        print(f"\n(written to {path})")
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    from sqlmodel import Session

    from personalagi.db import get_engine, init_db
    from personalagi.models import IngestState

    settings = get_settings()
    init_db(settings)
    with Session(get_engine(settings)) as session:
        for label in settings.account_labels:
            state = session.get(IngestState, label)
            token = settings.token_path(label)
            auth_status = "authorized" if token.exists() else "NOT AUTHORIZED"
            if state is None:
                print(f"{label:12} {auth_status:16} never synced")
                continue
            print(
                f"{label:12} {auth_status:16} "
                f"messages={state.message_count} "
                f"last_sync={state.last_synced_at or '-'} "
                f"history_id={state.last_history_id or '-'}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    handlers = {
        "auth": _cmd_auth,
        "ingest": _cmd_ingest,
        "backfill": _cmd_backfill,
        "sync-events": _cmd_sync_events,
        "imessage": _cmd_imessage,
        "calendar": _cmd_calendar,
        "refresh-headers": _cmd_refresh_headers,
        "classify": _cmd_classify,
        "relevance": _cmd_relevance,
        "goal": _cmd_goal,
        "sweep": _cmd_sweep,
        "prep": _cmd_prep,
        "activate": _cmd_activate,
        "build-edges": _cmd_build_edges,
        "meetings": _cmd_meetings,
        "feedback": _cmd_feedback,
        "proposals": _cmd_proposals,
        "investigate": _cmd_investigate,
        "import": _cmd_import,
        "facts": _cmd_facts,
        "contradictions": _cmd_contradictions,
        "owed": _cmd_owed,
        "done": _cmd_done,
        "labels-template": _cmd_labels_template,
        "eval": _cmd_eval,
        "context-build": _cmd_context_build,
        "context": _cmd_context,
        "search": _cmd_search,
        "reindex": _cmd_reindex,
        "compact": _cmd_compact,
        "correct": _cmd_correct,
        "profile-history": _cmd_profile_history,
        "brief": _cmd_brief,
        "status": _cmd_status,
    }
    try:
        return handlers[args.command](args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logging.getLogger("personalagi").debug("unhandled", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
