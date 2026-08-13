"""Gmail ingest orchestration: auth -> fetch -> normalize -> SQLite.

Read-only scope. One account label per run (or all, in sequence).
This module owns the watermark transaction; auth.py, fetch.py, and
normalize.py know nothing about each other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.ingest import fetch, normalize
from personalagi.ingest.auth import build_service, load_credentials
from personalagi.models import IngestState, Message

log = logging.getLogger(__name__)

# SQLite's default variable limit is 999; 10 columns per row leaves room.
INSERT_CHUNK_SIZE = 90


@dataclass
class IngestResult:
    account_label: str
    mode: str
    listed: int = 0
    fetched: int = 0
    inserted: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    history_expired: bool = False

    def summary(self) -> str:
        parts = [
            f"{self.account_label}: mode={self.mode}",
            f"listed={self.listed}",
            f"fetched={self.fetched}",
            f"new={self.inserted}",
            f"dupe={self.skipped_duplicate}",
        ]
        if self.failed:
            parts.append(f"failed={self.failed}")
        if self.history_expired:
            parts.append("(history expired, fell back to date query)")
        return "  ".join(parts)


def _get_state(session: Session, label: str) -> IngestState:
    state = session.get(IngestState, label)
    if state is None:
        state = IngestState(account_label=label)
        session.add(state)
        session.commit()
        session.refresh(state)
    return state


def _insert_messages(session: Session, rows: list[dict]) -> int:
    """Idempotent bulk insert. Returns the number of genuinely new rows.

    ON CONFLICT DO NOTHING against UNIQUE(account_label, gmail_id) is what
    makes the watermark overlap window free: re-fetching costs quota, never
    correctness.
    """
    inserted = 0
    for start in range(0, len(rows), INSERT_CHUNK_SIZE):
        chunk = rows[start : start + INSERT_CHUNK_SIZE]
        stmt = sqlite_insert(Message).values(chunk).on_conflict_do_nothing(
            index_elements=["account_label", "gmail_id"]
        )
        # session.execute, not SQLModel's exec: exec() is typed for selects.
        result = session.execute(stmt)
        inserted += result.rowcount or 0
    return inserted


def ingest_account(
    label: str,
    settings: Settings | None = None,
    *,
    max_messages: int | None = None,
    dry_run: bool = False,
) -> IngestResult:
    """Pull new messages for one account and store them."""
    settings = settings or get_settings()
    limit = settings.ingest_max_messages if max_messages is None else max_messages

    init_db(settings)
    service = build_service(load_credentials(label, settings))

    with Session(get_engine(settings)) as session:
        state = _get_state(session, label)
        last_history_id = state.last_history_id
        last_internal_date_ms = state.last_internal_date_ms

    listing = fetch.list_new_message_ids(
        service,
        last_history_id=last_history_id,
        last_internal_date_ms=last_internal_date_ms,
        initial_backfill_days=settings.initial_backfill_days,
        overlap_seconds=settings.ingest_overlap_seconds,
        max_messages=limit,
    )

    result = IngestResult(
        account_label=label,
        mode=listing.mode,
        listed=len(listing.message_ids),
        history_expired=listing.history_expired,
    )
    log.info("%s: %d message(s) to fetch (mode=%s)", label, result.listed, listing.mode)

    if dry_run:
        return result

    rows: list[dict] = []
    max_internal_date = last_internal_date_ms or 0

    for message_id in listing.message_ids:
        try:
            raw = fetch.get_message(service, message_id)
        except Exception:
            # One bad message must not abandon the batch; the watermark
            # simply will not advance past it on this run.
            log.exception("%s: failed to fetch message %s", label, message_id)
            result.failed += 1
            continue

        normalized = normalize.normalize_message(raw, label)
        rows.append(normalized.as_row())
        result.fetched += 1
        max_internal_date = max(max_internal_date, normalized.internal_date_ms)

    with Session(get_engine(settings)) as session:
        if rows:
            result.inserted = _insert_messages(session, rows)
            result.skipped_duplicate = len(rows) - result.inserted

        # Commit the messages BEFORE advancing the watermark. A crash between
        # the two re-fetches (cheap, deduped) instead of skipping (permanent).
        session.commit()

        state = _get_state(session, label)
        if listing.profile_history_id:
            state.last_history_id = listing.profile_history_id
        if max_internal_date:
            state.last_internal_date_ms = max_internal_date
        state.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
        state.message_count = (
            session.execute(
                select(func.count()).select_from(Message).where(Message.account_label == label)
            ).scalar_one()
        )
        session.add(state)
        session.commit()

    log.info(result.summary())
    return result


def ingest_all(
    settings: Settings | None = None,
    *,
    max_messages: int | None = None,
    dry_run: bool = False,
) -> list[IngestResult]:
    """Ingest every account in GMAIL_ACCOUNTS, in order.

    One account failing does not stop the others — a Workspace admin block
    on a single account should not take the whole sync down.
    """
    settings = settings or get_settings()
    results: list[IngestResult] = []
    for label in settings.account_labels:
        try:
            results.append(
                ingest_account(label, settings, max_messages=max_messages, dry_run=dry_run)
            )
        except Exception as exc:
            log.error("%s: ingest failed: %s", label, exc)
            results.append(IngestResult(account_label=label, mode="failed"))
    return results
