"""Open loops: what the owner owes people, and what people owe the owner.

Triage answers "what arrived". This answers "what did I say I would do, and
have I done it" — which is the question that actually costs you things.

Two rules keep it honest:

  1. Every commitment shows the sentence that created it. If the words cannot
     be quoted from the source message, the commitment is not recorded at all
     (enforced upstream in relevance._quote_is_grounded). Telling someone they
     promised something they did not write is the one failure that would make
     this worse than having nothing.

  2. Staleness is derived from dates in code, never asserted by a model. The
     rule is one comparison you can read, so "why is this stale" always has an
     answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.models import Commitment, Event, Participant
from personalagi.records import Provenance

log = logging.getLogger(__name__)

OPEN, DONE, STALE = "open", "done", "stale"


@dataclass
class PersonOwed:
    person_slug: str
    person_name: str
    person_email: str
    items: list[Commitment] = field(default_factory=list)

    @property
    def oldest(self) -> datetime:
        return min(c.promised_at for c in self.items)

    @property
    def stale_count(self) -> int:
        return sum(1 for c in self.items if c.status == STALE)


def refresh_activity(settings: Settings | None = None) -> int:
    """Push `last_activity_at` forward when a conversation has continued.

    Stage 17. The first version measured staleness from `promised_at`, so a
    promise the owner fulfilled a week later still went stale on schedule and
    nagged forever. What matters is whether anything has happened SINCE, so
    any later event in the same thread — or with the same person, when there
    is no thread — counts as activity.

    Deliberately generous about what counts. A false "this moved" costs one
    missed nudge; a false "nothing moved" costs the owner's trust in every
    nudge, which is the failure that gets the system switched off.
    """
    settings = settings or get_settings()
    init_db(settings)
    changed = 0

    with Session(get_engine(settings)) as session:
        rows = list(
            session.execute(
                select(Commitment, Event).join(Event, Event.id == Commitment.event_id)
            )
        )
        for commitment, source in rows:
            latest = None
            if source.thread_key:
                latest = session.execute(
                    select(func.max(Event.timestamp))
                    .where(Event.thread_key == source.thread_key)
                    .where(Event.timestamp > commitment.promised_at)
                    .where(Event.provenance == Provenance.EXTERNAL)
                ).scalar()

            if latest is None and commitment.person_email:
                # No thread: any later event involving the same person counts.
                latest = session.execute(
                    select(func.max(Event.timestamp))
                    .join(Participant, Participant.event_id == Event.id)
                    .where(Participant.address == commitment.person_email.lower())
                    .where(Event.timestamp > commitment.promised_at)
                    .where(Event.provenance == Provenance.EXTERNAL)
                ).scalar()

            newest = max(v for v in (latest, commitment.promised_at) if v is not None)
            if commitment.last_activity_at != newest:
                commitment.last_activity_at = newest
                changed += 1
        session.commit()

    if changed:
        log.info("refreshed last_activity_at on %d commitment(s)", changed)
    return changed


def refresh_stale(settings: Settings | None = None, *, now: datetime | None = None) -> int:
    """Move commitments with no recent ACTIVITY to `stale`.

    Measured from `last_activity_at`, not `promised_at` (stage 17). COALESCE to
    promised_at so rows written before that column existed still behave — a
    NULL must not read as "infinitely stale".
    """
    settings = settings or get_settings()
    init_db(settings)
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(days=settings.commitment_stale_days)

    with Session(get_engine(settings)) as session:
        result = session.execute(
            update(Commitment)
            .where(Commitment.status == OPEN)
            .where(Commitment.manually_closed.is_(False))
            .where(
                func.coalesce(Commitment.last_activity_at, Commitment.promised_at)
                < cutoff
            )
            .values(status=STALE)
        )
        session.commit()
        changed = result.rowcount or 0

    if changed:
        log.info("marked %d commitment(s) stale (>%dd)", changed, settings.commitment_stale_days)
    return changed


def list_owed(
    settings: Settings | None = None,
    *,
    direction: str = "i_owe",
    include_done: bool = False,
    person: str | None = None,
) -> list[PersonOwed]:
    """Open commitments grouped by person, oldest promise first."""
    settings = settings or get_settings()
    init_db(settings)

    with Session(get_engine(settings)) as session:
        stmt = select(Commitment).where(Commitment.direction == direction)
        if not include_done:
            stmt = stmt.where(Commitment.status != DONE)
        if person:
            stmt = stmt.where(Commitment.person_slug == person)
        rows = list(session.execute(stmt).scalars())

    grouped: dict[str, PersonOwed] = {}
    for row in rows:
        key = row.person_slug or row.person_email or "(unknown)"
        entry = grouped.setdefault(
            key,
            PersonOwed(
                person_slug=row.person_slug,
                person_name=row.person_name,
                person_email=row.person_email,
            ),
        )
        entry.items.append(row)

    for entry in grouped.values():
        entry.items.sort(key=lambda c: c.promised_at)

    # Oldest debt first: the thing rotting longest is the thing to deal with.
    return sorted(grouped.values(), key=lambda e: e.oldest)


def close_commitment(
    commitment_id: int, settings: Settings | None = None, *, now: datetime | None = None
) -> bool:
    """Mark one commitment done, by hand.

    Sets `manually_closed` so the next extraction run cannot reopen it. Same
    principle as a person file's `corrections`: a human decision that a nightly
    job would otherwise silently undo has to be recorded as authoritative, or
    the correction mechanism is theatre.
    """
    settings = settings or get_settings()
    init_db(settings)
    now = now or datetime.now(UTC).replace(tzinfo=None)

    with Session(get_engine(settings)) as session:
        row = session.get(Commitment, commitment_id)
        if row is None:
            return False
        row.status = DONE
        row.manually_closed = True
        row.resolved_at = now
        session.add(row)
        session.commit()
    return True


def render_owed(groups: list[PersonOwed], *, direction: str = "i_owe") -> str:
    """Human-readable report. Quote first — the quote IS the evidence."""
    if not groups:
        which = "you owe anyone" if direction == "i_owe" else "anyone owes you"
        return f"Nothing {which}. (Or nothing detected — see `relevance` coverage.)"

    heading = "YOU OWE" if direction == "i_owe" else "OWED TO YOU"
    total = sum(len(g.items) for g in groups)
    lines = [f"{heading} — {total} open across {len(groups)} people", ""]

    for group in groups:
        who = group.person_name or group.person_email or group.person_slug
        contact = f" <{group.person_email}>" if group.person_email else ""
        flag = f"  [{group.stale_count} STALE]" if group.stale_count else ""
        lines.append(f"{who}{contact}{flag}")

        for item in group.items:
            since = item.last_activity_at or item.promised_at
            age = (datetime.now(UTC).replace(tzinfo=None) - since).days
            due = f"  (said: {item.due_text})" if item.due_text else ""
            mark = "!" if item.status == STALE else "-"
            lines.append(f"  {mark} [{item.id}] {item.what}{due}   {age}d ago")
            lines.append(f'      "{item.quote}"')
        lines.append("")

    lines.append("Close one with:  python -m personalagi done <id>")
    return "\n".join(lines)


def events_for(
    commitment_ids: list[int], settings: Settings | None = None
) -> dict[int, Event]:
    """Source event per commitment, so a claim can always be traced back."""
    settings = settings or get_settings()
    with Session(get_engine(settings)) as session:
        rows = list(
            session.execute(
                select(Commitment.id, Event)
                .join(Event, Event.id == Commitment.event_id)
                .where(Commitment.id.in_(commitment_ids))
            )
        )
    return {cid: event for cid, event in rows}
