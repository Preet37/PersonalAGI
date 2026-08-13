"""Gmail ingest orchestration: auth -> fetch -> normalize -> SQLite.

Read-only scope. One account label per run (or all, in sequence).
This module owns the watermark transaction; auth.py, fetch.py, and
normalize.py know nothing about each other.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime

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
    truncated: bool = False
    oldest_fetched_ms: int | None = None

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
        if self.truncated:
            # Loud on purpose: this is the condition whose silence made the
            # original bootstrap bug invisible.
            parts.append(
                "TRUNCATED - older mail not fetched, run: "
                f"personalagi backfill --account {self.account_label}"
            )
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


def _fetch_and_normalize(
    service,
    label: str,
    message_ids: list[str],
    result: IngestResult,
    *,
    workers: int = 1,
    service_factory: Callable[[], object] | None = None,
) -> tuple[list[dict], int | None, int | None]:
    """Fetch and normalize a batch. Returns (rows, newest_ms, oldest_ms).

    Sequential by default. `workers > 1` needs a service_factory because
    googleapiclient's underlying httplib2.Http is NOT thread-safe — sharing
    one service across threads corrupts responses intermittently, which is
    the worst possible failure mode for an ingest pipeline. Each thread
    therefore builds and keeps its own client.
    """
    rows: list[dict] = []
    newest_ms: int | None = None
    oldest_ms: int | None = None

    local = threading.local()
    # The factory is only consulted when we actually fan out. Otherwise the
    # caller's existing client is reused - building a second one on the
    # default sequential path would be pure waste.
    concurrent = workers > 1 and service_factory is not None

    def client_for_thread():
        if not concurrent:
            return service
        existing = getattr(local, "service", None)
        if existing is None:
            existing = service_factory()
            local.service = existing
        return existing

    def fetch_one(message_id: str):
        try:
            raw = fetch.get_message(client_for_thread(), message_id)
        except Exception:
            # One bad message must not abandon the batch; the cursor simply
            # will not advance past it on this run.
            log.exception("%s: failed to fetch message %s", label, message_id)
            return None
        return normalize.normalize_message(raw, label)

    if concurrent:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            normalized_all = list(pool.map(fetch_one, message_ids))
    else:
        normalized_all = [fetch_one(mid) for mid in message_ids]

    # Accumulation stays single-threaded, so no locking is needed here.
    for normalized in normalized_all:
        if normalized is None:
            result.failed += 1
            continue

        rows.append(normalized.as_row())
        result.fetched += 1

        ms = normalized.internal_date_ms
        if ms:
            newest_ms = ms if newest_ms is None else max(newest_ms, ms)
            oldest_ms = ms if oldest_ms is None else min(oldest_ms, ms)

    return rows, newest_ms, oldest_ms


@dataclass
class HeaderRefreshResult:
    account_label: str
    considered: int = 0
    updated: int = 0
    failed: int = 0
    bulk_markers_found: int = 0

    def summary(self) -> str:
        return (
            f"{self.account_label}: considered={self.considered} "
            f"updated={self.updated} failed={self.failed}  "
            f"bulk markers on {self.bulk_markers_found}"
        )


# Commit every N rows rather than once at the end. A 4,000-message refresh
# that dies at row 3,900 should keep the 3,900, not discard them: unlike
# ingest there is no cursor to make a re-run cheap, and the work is pure
# read-then-update, so partial progress is always valid.
HEADER_COMMIT_CHUNK = 200


def refresh_headers(
    label: str,
    settings: Settings | None = None,
    *,
    limit: int | None = None,
    fetch_workers: int | None = None,
    service: object | None = None,
) -> HeaderRefreshResult:
    """Backfill `headers_json` for messages ingested before it existed.

    Bodies are already stored, so this re-reads headers only (metadata format)
    rather than re-downloading 4,000 message bodies for data we discarded.

    Idempotent and resumable: only rows where headers_json IS NULL are
    considered, so re-running picks up exactly what an interrupted run missed.
    """
    from personalagi.identity import automated_by_header

    settings = settings or get_settings()
    init_db(settings)
    result = HeaderRefreshResult(account_label=label)

    with Session(get_engine(settings)) as session:
        stmt = (
            select(Message.id, Message.gmail_id)
            .where(Message.account_label == label)
            .where(Message.headers_json.is_(None))
            .order_by(Message.internal_date_ms.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        targets = list(session.execute(stmt))

    result.considered = len(targets)
    if not targets:
        log.info("%s: no messages need headers", label)
        return result

    service = service or build_service(load_credentials(label, settings))
    workers = fetch_workers or settings.fetch_workers
    header_names = [name.title() for name in normalize.HEADER_WHITELIST]

    local = threading.local()
    concurrent = workers > 1

    def client_for_thread():
        if not concurrent:
            return service
        existing = getattr(local, "service", None)
        if existing is None:
            # httplib2.Http is not thread-safe; each thread gets its own.
            existing = build_service(load_credentials(label, settings))
            local.service = existing
        return existing

    def fetch_one(target) -> tuple[int, dict[str, str]] | None:
        message_id, gmail_id = target
        try:
            raw = fetch.get_message_headers(
                client_for_thread(), gmail_id, header_names
            )
        except Exception:
            log.exception("%s: failed to fetch headers for %s", label, gmail_id)
            return None
        return message_id, normalize.extract_headers(raw.get("payload", {}) or {})

    for start in range(0, len(targets), HEADER_COMMIT_CHUNK):
        chunk = targets[start : start + HEADER_COMMIT_CHUNK]
        if concurrent:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                fetched = list(pool.map(fetch_one, chunk))
        else:
            fetched = [fetch_one(t) for t in chunk]

        with Session(get_engine(settings)) as session:
            for item in fetched:
                if item is None:
                    result.failed += 1
                    continue
                message_id, headers = item
                message = session.get(Message, message_id)
                if message is None:
                    result.failed += 1
                    continue
                # "{}" not None: a message genuinely carrying none of the
                # whitelisted headers must record that it was CHECKED, or the
                # next run re-fetches it forever.
                message.headers_json = json.dumps(headers, ensure_ascii=False)
                session.add(message)
                result.updated += 1
                if automated_by_header(headers):
                    result.bulk_markers_found += 1
            session.commit()
        log.info(
            "%s: headers %d/%d", label, min(start + len(chunk), len(targets)), len(targets)
        )

    log.info(result.summary())
    return result


def _finish(session: Session, state: IngestState, label: str) -> None:
    """Stamp the sync time, refresh the count, and commit the cursor row."""
    state.last_synced_at = datetime.now(UTC).replace(tzinfo=None)
    state.message_count = session.execute(
        select(func.count()).select_from(Message).where(Message.account_label == label)
    ).scalar_one()
    session.add(state)
    session.commit()


def ingest_account(
    label: str,
    settings: Settings | None = None,
    *,
    max_messages: int | None = None,
    dry_run: bool = False,
    fetch_workers: int | None = None,
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
        truncated=listing.truncated,
    )
    log.info("%s: %d message(s) to fetch (mode=%s)", label, result.listed, listing.mode)

    if dry_run:
        return result

    rows, newest_ms, oldest_ms = _fetch_and_normalize(
        service,
        label,
        listing.message_ids,
        result,
        workers=fetch_workers or settings.fetch_workers,
        service_factory=lambda: build_service(load_credentials(label, settings)),
    )
    result.oldest_fetched_ms = oldest_ms

    with Session(get_engine(settings)) as session:
        if rows:
            result.inserted = _insert_messages(session, rows)
            result.skipped_duplicate = len(rows) - result.inserted

        # Commit the messages BEFORE advancing any cursor. A crash between
        # the two re-fetches (cheap, deduped) instead of skipping (permanent).
        session.commit()

        state = _get_state(session, label)

        # Forward cursor: we have everything newer than this.
        if listing.profile_history_id:
            state.last_history_id = listing.profile_history_id
        if newest_ms:
            state.last_internal_date_ms = max(newest_ms, state.last_internal_date_ms or 0)

        # Backfill cursor only ever moves BACKWARD. An incremental forward run
        # fetches recent mail, whose oldest message is newer than the stored
        # floor; raising the floor there would erase the record of the hole.
        if oldest_ms:
            state.oldest_internal_date_ms = (
                oldest_ms
                if state.oldest_internal_date_ms is None
                else min(state.oldest_internal_date_ms, oldest_ms)
            )

        state.last_run_truncated = listing.truncated
        if listing.truncated:
            # Explicitly false, not merely unset: we now KNOW mail is missing.
            state.backfill_complete = False

        _finish(session, state, label)

    if listing.truncated:
        log.warning(
            "%s: listing truncated by cap - older mail was NOT fetched. "
            "Run `personalagi backfill --account %s` to recover it.",
            label,
            label,
        )
    log.info(result.summary())
    return result


def backfill_account(
    label: str,
    settings: Settings | None = None,
    *,
    until: date | None = None,
    max_messages: int | None = None,
    dry_run: bool = False,
    fetch_workers: int | None = None,
) -> IngestResult:
    """Walk backwards from the backfill cursor into older mail.

    This is the recovery path for a bootstrap that a message cap truncated.
    It never touches the forward cursor, so running it cannot cause new mail
    to be missed.
    """
    settings = settings or get_settings()
    limit = settings.ingest_max_messages if max_messages is None else max_messages

    init_db(settings)
    service = build_service(load_credentials(label, settings))

    with Session(get_engine(settings)) as session:
        state = _get_state(session, label)
        cursor_ms = state.oldest_internal_date_ms
        already_complete = state.backfill_complete

        if cursor_ms is None:
            # Self-heal: a DB written before the backfill cursor existed has
            # messages but no floor. Derive it rather than refusing to run or,
            # worse, re-pulling the whole mailbox.
            cursor_ms = session.execute(
                select(func.min(Message.internal_date_ms)).where(
                    Message.account_label == label
                )
            ).scalar_one_or_none()
            if cursor_ms:
                log.info("%s: derived backfill cursor from stored mail", label)
                state.oldest_internal_date_ms = cursor_ms
                session.add(state)
                session.commit()

    result = IngestResult(account_label=label, mode="backfill")

    if already_complete and until is None:
        log.info("%s: backfill already complete, nothing older to fetch", label)
        return result

    if cursor_ms is None:
        # Nothing ingested yet — there is no floor to walk back from.
        log.warning("%s: no messages ingested yet; run `ingest` before `backfill`", label)
        return result

    until_epoch = None
    if until is not None:
        until_epoch = int(datetime(until.year, until.month, until.day, tzinfo=UTC).timestamp())

    message_ids, truncated = fetch.list_backfill_message_ids(
        service,
        # `before:` is second-granularity and exclusive. Deliberately +1 so
        # the boundary SECOND is re-listed: excluding it would permanently
        # skip any message sharing that second with the cursor message.
        # The cost is re-fetching the cursor message each pass, which dedupes
        # to nothing. So convergence is measured by `inserted`, not `fetched`.
        before_epoch_seconds=(cursor_ms // 1000) + 1,
        until_epoch_seconds=until_epoch,
        max_messages=limit,
    )

    result.listed = len(message_ids)
    result.truncated = truncated
    log.info("%s: %d older message(s) to fetch (backfill)", label, result.listed)

    if dry_run:
        return result

    rows, _newest_ms, oldest_ms = _fetch_and_normalize(
        service,
        label,
        message_ids,
        result,
        workers=fetch_workers or settings.fetch_workers,
        service_factory=lambda: build_service(load_credentials(label, settings)),
    )
    result.oldest_fetched_ms = oldest_ms

    with Session(get_engine(settings)) as session:
        if rows:
            result.inserted = _insert_messages(session, rows)
            result.skipped_duplicate = len(rows) - result.inserted
        session.commit()

        state = _get_state(session, label)
        if oldest_ms:
            state.oldest_internal_date_ms = min(
                state.oldest_internal_date_ms or oldest_ms, oldest_ms
            )

        # Complete only when the pass exhausted the query with no cap cutting
        # it short. A bounded pass (--until) proves nothing about earlier mail.
        if not truncated and until is None:
            state.backfill_complete = True
        state.last_run_truncated = truncated

        _finish(session, state, label)

    if truncated:
        log.info("%s: more history remains, run backfill again to continue", label)
    log.info(result.summary())
    return result


def ingest_all(
    settings: Settings | None = None,
    *,
    max_messages: int | None = None,
    dry_run: bool = False,
    fetch_workers: int | None = None,
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
                ingest_account(
                    label, settings, max_messages=max_messages,
                    dry_run=dry_run, fetch_workers=fetch_workers,
                )
            )
        except Exception as exc:
            log.error("%s: ingest failed: %s", label, exc)
            results.append(IngestResult(account_label=label, mode="failed"))
    return results
