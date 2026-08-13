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
        "--dry-run",
        action="store_true",
        help="list what would be fetched, write nothing, leave the watermark alone",
    )

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
        results = ingest_all(settings, max_messages=args.limit, dry_run=args.dry_run)
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
                args.account, settings, max_messages=args.limit, dry_run=args.dry_run
            )
        ]

    for result in results:
        print(result.summary())
    return 1 if any(r.mode == "failed" for r in results) else 0


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

    handlers = {"auth": _cmd_auth, "ingest": _cmd_ingest, "status": _cmd_status}
    try:
        return handlers[args.command](args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logging.getLogger("personalagi").debug("unhandled", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
