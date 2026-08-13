"""Reading Events: the query layer everything downstream shares.

Consumers need "the event plus who sent it" constantly. Without one place to
express that, every module re-derives the sender, and the first one to get it
slightly wrong (role ordering, owner-sent mail, missing participants) does so
invisibly.

Nothing here imports an adapter or names a source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlmodel import Session

from personalagi.adapters.base import CC, FROM, TO
from personalagi.models import Event, Participant


@dataclass(frozen=True)
class EventView:
    """An Event with its participants resolved. The unit downstream works in."""

    event: Event
    participants: list[Participant]

    @property
    def sender(self) -> Participant | None:
        return next((p for p in self.participants if p.role == FROM), None)

    @property
    def sender_address(self) -> str:
        sender = self.sender
        return sender.address if sender else ""

    @property
    def sender_name(self) -> str:
        sender = self.sender
        return (sender.display_name if sender else "") or ""

    @property
    def sent_by_owner(self) -> bool:
        sender = self.sender
        return bool(sender and sender.is_owner)

    @property
    def from_automated(self) -> bool:
        sender = self.sender
        return bool(sender and sender.is_automated)

    @property
    def person_slug(self) -> str:
        """The person file this event belongs to, or "" if it belongs to none."""
        sender = self.sender
        return (sender.person_slug if sender else "") or ""

    @property
    def counterparty(self) -> Participant | None:
        """The other end of the conversation.

        The sender on received events; the first non-owner recipient on events
        the owner sent. Attributing a promise in sent mail to its sender means
        reporting that the owner owes themselves.
        """
        sender = self.sender
        if sender and not sender.is_owner:
            return sender
        for role in (TO, CC):
            for participant in self.participants:
                if participant.role == role and not participant.is_owner:
                    return participant
        return None

    @property
    def metadata(self) -> dict:
        if not self.event.metadata_json:
            return {}
        try:
            loaded = json.loads(self.event.metadata_json)
        except (json.JSONDecodeError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def recipients_line(self, limit: int = 300) -> str:
        names = [
            p.address for p in self.participants if p.role in (TO, CC) and p.address
        ]
        return ", ".join(names)[:limit] or "(unknown)"


def load_views(session: Session, stmt: Select) -> list[EventView]:
    """Run an Event select and attach participants in one extra query.

    Two queries regardless of result size. Fetching participants per event
    would be one query per row, which on a 4,000-event pass is the difference
    between a second and a minute.
    """
    events = list(session.execute(stmt).scalars())
    if not events:
        return []

    by_event: dict[int, list[Participant]] = {}
    ids = [e.id for e in events]
    for start in range(0, len(ids), 500):  # SQLite caps variables per statement
        chunk = ids[start : start + 500]
        for participant in session.execute(
            select(Participant).where(Participant.event_id.in_(chunk))
        ).scalars():
            by_event.setdefault(participant.event_id, []).append(participant)

    return [EventView(event=e, participants=by_event.get(e.id, [])) for e in events]


def pending_events(
    *,
    account: str | None = None,
    exclude_scored: Select | None = None,
    limit: int | None = None,
    newest_first: bool = True,
) -> Select:
    """Build the standard "events still needing work" select."""
    stmt = select(Event)
    if account:
        stmt = stmt.where(Event.account_label == account)
    if exclude_scored is not None:
        stmt = stmt.where(Event.id.not_in(exclude_scored))
    stmt = stmt.order_by(
        Event.timestamp_ms.desc() if newest_first else Event.timestamp_ms.asc()
    )
    if limit:
        stmt = stmt.limit(limit)
    return stmt
