"""Talking to the Gmail API: watermark-driven listing, and message retrieval.

The watermark strategy is history-first with a date-query fallback. See
README "Incremental sync" for the reasoning; the short version is that
history.list is the real change feed but Gmail only retains ~1 week of it,
so we need a fallback that never expires.

Quota: 250 units/sec/user. messages.get and messages.list cost 5 each,
history.list 2, getProfile 1. Backfill is one get per message, so it is the
expensive path — hence backoff.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

MAX_RETRIES = 6
BASE_BACKOFF_SECONDS = 1.0
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
RETRYABLE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}


@dataclass
class ListResult:
    """What a watermark evaluation produced."""

    message_ids: list[str] = field(default_factory=list)
    # "history" | "query" | "bootstrap" — reported to the user so an
    # unexpected full re-pull is visible rather than silent.
    mode: str = "bootstrap"
    # historyId captured BEFORE listing. Written back on success so anything
    # arriving mid-run has a higher id and is caught next run.
    profile_history_id: str | None = None
    history_expired: bool = False


def _is_retryable(exc: HttpError) -> bool:
    status = getattr(exc.resp, "status", None)
    if status in RETRYABLE_STATUSES:
        return True
    # 403 is overloaded: rate limiting is retryable, permission denied is not.
    if status == 403:
        details = getattr(exc, "error_details", None) or []
        reasons = {d.get("reason") for d in details if isinstance(d, dict)}
        if reasons & RETRYABLE_REASONS:
            return True
        return any(reason in str(exc) for reason in RETRYABLE_REASONS)
    return False


def execute(request):
    """Execute a Gmail API request with exponential backoff and jitter.

    Google's documented remedy for 429/5xx. Jitter matters because three
    accounts syncing in sequence otherwise retry in lockstep.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return request.execute()
        except HttpError as exc:
            if not _is_retryable(exc) or attempt == MAX_RETRIES - 1:
                raise
            delay = BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 1)
            log.warning(
                "gmail api %s, retrying in %.1fs (attempt %d/%d)",
                getattr(exc.resp, "status", "?"),
                delay,
                attempt + 1,
                MAX_RETRIES,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")


def get_profile_history_id(service, user_id: str = "me") -> str | None:
    profile = execute(service.users().getProfile(userId=user_id))
    history_id = profile.get("historyId")
    return str(history_id) if history_id is not None else None


def _list_via_history(service, start_history_id: str, user_id: str = "me") -> list[str] | None:
    """Incremental listing via the change feed.

    Returns None if the watermark has aged out of Gmail's retention window,
    which is the caller's signal to fall back to a date query.
    """
    message_ids: list[str] = []
    page_token: str | None = None

    while True:
        request = (
            service.users()
            .history()
            .list(
                userId=user_id,
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                pageToken=page_token,
            )
        )
        try:
            response = execute(request)
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                log.warning(
                    "historyId %s expired (Gmail retains ~1 week); "
                    "falling back to date query",
                    start_history_id,
                )
                return None
            raise

        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added.get("message", {})
                if msg.get("id"):
                    message_ids.append(msg["id"])

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    # history can report the same message under multiple records
    return list(dict.fromkeys(message_ids))


def _list_via_query(
    service,
    after_epoch_seconds: int,
    max_messages: int = 0,
    user_id: str = "me",
) -> list[str]:
    """Fallback listing by internalDate.

    Coarser than history: no deletions, no label changes. The `after:`
    boundary is intentionally overlapped by the caller because mail is not
    delivered in internalDate order.
    """
    message_ids: list[str] = []
    page_token: str | None = None
    query = f"after:{after_epoch_seconds}"

    while True:
        request = (
            service.users()
            .messages()
            .list(userId=user_id, q=query, pageToken=page_token, maxResults=500)
        )
        response = execute(request)

        for msg in response.get("messages", []):
            message_ids.append(msg["id"])
            if max_messages and len(message_ids) >= max_messages:
                log.info("hit ingest_max_messages cap of %d", max_messages)
                return message_ids

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return message_ids


def list_new_message_ids(
    service,
    *,
    last_history_id: str | None,
    last_internal_date_ms: int | None,
    initial_backfill_days: int,
    overlap_seconds: int,
    max_messages: int = 0,
    user_id: str = "me",
) -> ListResult:
    """Decide what to fetch, given this account's stored watermark."""
    result = ListResult()
    # Captured first, on purpose: messages arriving during this run get a
    # higher historyId and are picked up next time instead of being skipped.
    result.profile_history_id = get_profile_history_id(service, user_id)

    if last_history_id:
        ids = _list_via_history(service, last_history_id, user_id)
        if ids is not None:
            result.message_ids = ids[:max_messages] if max_messages else ids
            result.mode = "history"
            return result
        result.history_expired = True

    if last_internal_date_ms is not None:
        after = (last_internal_date_ms // 1000) - overlap_seconds
        result.mode = "query"
    else:
        cutoff = datetime.now(UTC) - timedelta(days=initial_backfill_days)
        after = int(cutoff.timestamp())
        result.mode = "bootstrap"

    result.message_ids = _list_via_query(service, max(after, 0), max_messages, user_id)
    return result


def get_message(service, message_id: str, user_id: str = "me") -> dict:
    """Fetch one full message.

    format='full' rather than 'metadata' because we need bodies; both cost
    the same 5 quota units, so 'metadata' would only save bandwidth.
    """
    return execute(
        service.users().messages().get(userId=user_id, id=message_id, format="full")
    )
