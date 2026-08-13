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
from datetime import date
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
        "classify": _cmd_classify,
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
