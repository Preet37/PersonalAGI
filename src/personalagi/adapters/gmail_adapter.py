"""Gmail -> Event.

The only module that is allowed to know what a sender, a subject, or a thread
is. Everything below reads Events.

Note what does NOT happen here: no decision about whether a sender is a person,
no person-file naming, no bulk filtering. Those live in adapters.base so the
iMessage and calendar adapters inherit them.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from email.utils import getaddresses

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from personalagi.adapters.base import CC, FROM, TO, EventRecord, resolve_participant
from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.identity import shared_addresses
from personalagi.models import Event, Message, Participant

log = logging.getLogger(__name__)

SOURCE = "gmail"
# Recipients beyond this are a mailing-list blast, not a conversation. Storing
# 400 participants for one message would make the participant index useless
# for its actual purpose ("who do I talk to").
MAX_RECIPIENTS = 20


def _headers_of(message: Message) -> dict[str, str]:
    if not message.headers_json:
        return {}
    try:
        loaded = json.loads(message.headers_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def event_from_message(
    message: Message,
    *,
    owner_addresses: set[str] | None = None,
    shared: set[str] | None = None,
) -> EventRecord:
    """Turn one stored Gmail message into a canonical Event."""
    headers = _headers_of(message)

    record = EventRecord(
        source=SOURCE,
        source_id=message.gmail_id,
        account_label=message.account_label,
        thread_key=message.thread_id or "",
        title=message.subject or "",
        text=message.body_text or "",
        timestamp=message.timestamp,
        timestamp_ms=message.internal_date_ms,
        # Gmail structure the common shape cannot hold. D1's stated mitigation
        # for lowest-common-denominator loss.
        metadata={
            "headers": headers,
            "gmail_thread_id": message.thread_id or "",
        },
    )

    record.participants.append(
        resolve_participant(
            message.sender_email,
            message.sender_name,
            role=FROM,
            owner_addresses=owner_addresses,
            shared=shared,
            headers=headers,
        )
    )

    seen = {(message.sender_email or "").strip().lower()}
    for header_name, role in (("to", TO), ("cc", CC)):
        for name, address in getaddresses([headers.get(header_name, "")]):
            normalized = (address or "").strip().lower()
            if not normalized or normalized in seen:
                continue
            if len(record.participants) >= MAX_RECIPIENTS:
                break
            seen.add(normalized)
            record.participants.append(
                resolve_participant(
                    normalized,
                    name,
                    role=role,
                    owner_addresses=owner_addresses,
                    shared=shared,
                )
            )

    return record


def _upsert_events(session: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), 60):
        chunk = rows[start : start + 60]
        stmt = sqlite_insert(Event).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "account_label", "source_id"],
            set_={
                k: getattr(stmt.excluded, k)
                for k in (
                    "thread_key", "title", "text", "timestamp", "timestamp_ms",
                    "metadata_json",
                )
            },
        )
        written += session.execute(stmt).rowcount or 0
    return written


def _upsert_participants(session: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), 60):
        chunk = rows[start : start + 60]
        stmt = sqlite_insert(Participant).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id", "address", "role"],
            set_={
                k: getattr(stmt.excluded, k)
                for k in (
                    "display_name", "is_owner", "is_automated", "automated_reason",
                    "person_slug",
                )
            },
        )
        written += session.execute(stmt).rowcount or 0
    return written


def sync_events(
    settings: Settings | None = None,
    *,
    account: str | None = None,
    limit: int | None = None,
    rebuild: bool = False,
) -> tuple[int, int]:
    """Project stored Gmail messages into Events. Returns (events, participants).

    Idempotent and re-runnable: Events are keyed on (source, account, source_id)
    and upserted, so this can be run after every ingest.

    Event ids are set equal to the Message id they derive from. That is what
    lets classification, relevance, and commitment rows keep pointing at the
    right record through a column rename rather than a remapping migration.
    """
    settings = settings or get_settings()
    init_db(settings)
    owner_addresses = settings.owner_address_set

    with Session(get_engine(settings)) as session:
        stmt = select(Message)
        if account:
            stmt = stmt.where(Message.account_label == account)
        if not rebuild:
            # Match on the SOURCE ID, never on the row id.
            #
            # Stage 8 assigned Event.id = Message.id so a migration could be a
            # column rename instead of a remapping. That was correct while
            # Gmail was the only source. It became a landmine at three: iMessage
            # and Claude events take ids from the same sequence, so 350 message
            # ids collided with non-Gmail event ids and `not_in(Event.id)`
            # silently excluded every one of them -- 481 messages sat
            # unprojected while ingest reported success.
            #
            # The id-sharing scheme is now history: existing rows keep their
            # ids and their foreign keys, and new events are assigned ids by
            # SQLite like any other source.
            already = select(Event.source_id).where(Event.source == SOURCE)
            if account:
                already = already.where(Event.account_label == account)
            stmt = stmt.where(Message.gmail_id.not_in(already))
        stmt = stmt.order_by(Message.internal_date_ms.desc())
        if limit:
            stmt = stmt.limit(limit)
        messages = list(session.execute(stmt).scalars())

        # Shared-envelope detection is a property of the whole corpus, so it is
        # computed over every message, not just the batch being projected.
        all_pairs = list(
            session.execute(select(Message.sender_email, Message.sender_name))
        )

    if not messages:
        log.info("no messages to project into events")
        return 0, 0

    shared = shared_addresses(all_pairs)
    now = datetime.now(UTC).replace(tzinfo=None)

    events = 0
    participants = 0
    for start in range(0, len(messages), 200):
        batch = messages[start : start + 200]
        event_rows: list[dict] = []
        participant_rows: list[dict] = []

        for message in batch:
            record = event_from_message(
                message, owner_addresses=owner_addresses, shared=shared
            )
            # No forced id. Passing message.id here would collide with the
            # events another source already owns at that number.
            event_rows.append(record.as_row(now))

        with Session(get_engine(settings)) as session:
            events += _upsert_events(session, event_rows)
            session.commit()

            # Participants need the id SQLite actually assigned, so they are
            # attached after the events land rather than guessed beforehand.
            ids = dict(
                session.execute(
                    select(Event.source_id, Event.id)
                    .where(Event.source == SOURCE)
                    .where(Event.source_id.in_([m.gmail_id for m in batch]))
                ).all()
            )
            participant_rows = [
                p.as_row(ids[message.gmail_id])
                for message in batch
                if message.gmail_id in ids
                for p in event_from_message(
                    message, owner_addresses=owner_addresses, shared=shared
                ).participants
            ]
            participants += _upsert_participants(session, participant_rows)
            session.commit()
        log.info(
            "events %d/%d", min(start + len(batch), len(messages)), len(messages)
        )

    log.info("projected %d event(s), %d participant(s)", events, participants)
    return events, participants


def projection_lag(settings: Settings | None = None) -> dict[str, int]:
    """Messages ingested but never projected, per account.

    Lives here rather than in the CLI because comparing the Gmail landing
    table to Events requires knowing both, and only an adapter may.
    """
    from sqlalchemy import func

    settings = settings or get_settings()
    init_db(settings)
    with Session(get_engine(settings)) as session:
        messages = dict(
            session.execute(
                select(Message.account_label, func.count()).group_by(
                    Message.account_label
                )
            ).all()
        )
        events = dict(
            session.execute(
                select(Event.account_label, func.count())
                .where(Event.source == SOURCE)
                .group_by(Event.account_label)
            ).all()
        )
    return {
        label: count - events.get(label, 0)
        for label, count in messages.items()
        if count - events.get(label, 0) > 0
    }
