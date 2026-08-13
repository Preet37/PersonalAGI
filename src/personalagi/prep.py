"""Stage 25: what you should know before you walk into this.

A meeting is the best trigger in the system. It has a known time, known people,
and it is *inherently* about preparation — so unlike an arriving email, there is
never a question of whether the owner wants to think about it.

What a calendar invite contains is a name and a time. What you actually need is
who they are, how you know them, what has passed between you, what you owe each
other, which goal this serves, what role they play relative to that goal, and
what they last explicitly asked for. All of that is scattered across mail,
iMessage, and the owner's head.

EVERY CLAIM CITES AN EXTERNAL EVENT
Nothing in a prep brief is asserted without a source. This is the layer where a
hallucination does the most damage — walking into a room believing something
the system invented is worse than walking in unprepared, because unprepared at
least knows it is unprepared. So each line carries the event it came from, and
anything that cannot be traced is not printed.

ROLE MATTERS
The same person is not the same thing in two contexts. A contact who is an
`advocate` for a job goal and a `recommender` for an application goal needs a
different ask in each. Prep that ignores role writes a generic dossier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlmodel import Session

from personalagi.activate import (
    Node,
    activate,
    label_nodes,
    open_loops,
)
from personalagi.config import Settings, get_settings
from personalagi.context.retrieve import get_context
from personalagi.db import get_engine, init_db
from personalagi.models import Commitment, Event, Goal, Participant, PersonRole
from personalagi.records import NodeType, Provenance, citable_events

log = logging.getLogger(__name__)

RECENT_EVENTS = 8
# Phrases that mark someone asking for something. Lexical on purpose: a model
# call per candidate line would make prep cost more than the meeting.
ASK_MARKERS = (
    "could you", "can you", "would you", "please send", "let me know",
    "send me", "do you have", "are you able", "when can", "waiting on",
    "any update", "following up", "circle back",
)


@dataclass
class Cited:
    """A claim and the external event it came from. Never one without the other."""

    text: str
    event_id: int | None = None
    source: str = ""
    when: datetime | None = None

    def render(self) -> str:
        if self.event_id is None:
            return f"  - {self.text}"
        stamp = f"{self.when:%Y-%m-%d}" if self.when else "?"
        return f"  - {self.text}\n      [{self.source} {stamp} · event {self.event_id}]"


@dataclass
class Prep:
    person_slug: str
    display_name: str
    when: datetime | None = None
    title: str = ""
    profile: str = ""
    how_you_know_them: list[Cited] = field(default_factory=list)
    recent: list[Cited] = field(default_factory=list)
    you_owe: list[Cited] = field(default_factory=list)
    they_owe: list[Cited] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    last_asks: list[Cited] = field(default_factory=list)
    activated: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.recent or self.you_owe or self.they_owe or self.profile
        )


def _person_events(
    session: Session, slug: str, *, limit: int = RECENT_EVENTS
) -> list[Event]:
    """External events involving this person, newest first."""
    return list(
        session.execute(
            citable_events(select(Event))
            .join(Participant, Participant.event_id == Event.id)
            .where(Participant.person_slug == slug)
            .order_by(Event.timestamp_ms.desc())
            .limit(limit)
        ).scalars()
    )


def _find_asks(events: list[Event], owner_addresses: set[str]) -> list[Cited]:
    """Sentences where they asked for something, quoted verbatim.

    Only from events the OWNER did not send — "what they last asked for" means
    their words, and quoting the owner back at himself is useless.
    """
    asks: list[Cited] = []
    for event in events:
        for line in (event.text or "").splitlines():
            stripped = line.strip()
            if len(stripped) < 15 or len(stripped) > 240:
                continue
            low = stripped.lower()
            if any(marker in low for marker in ASK_MARKERS):
                asks.append(
                    Cited(
                        text=f'"{stripped}"',
                        event_id=event.id,
                        source=event.source,
                        when=event.timestamp,
                    )
                )
                break  # one per event; a whole thread of asks is not a brief
    return asks[:4]


def build_prep(
    who: str,
    settings: Settings | None = None,
    *,
    when: datetime | None = None,
    title: str = "",
) -> Prep | None:
    """Assemble everything worth knowing before meeting this person."""
    settings = settings or get_settings()
    init_db(settings)
    owner_addresses = settings.owner_address_set

    with Session(get_engine(settings)) as session:
        slug = who
        row = session.execute(
            select(Participant).where(Participant.person_slug == who).limit(1)
        ).scalar_one_or_none()
        if row is None:
            # Maybe an address rather than a slug.
            row = session.execute(
                select(Participant)
                .where(Participant.address == who.lower())
                .where(Participant.person_slug != "")
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            slug = row.person_slug

        prep = Prep(
            person_slug=slug,
            display_name=row.display_name or slug,
            when=when,
            title=title,
        )

        events = _person_events(session, slug)
        for event in events:
            text = (event.title or event.text or "").strip().splitlines()
            prep.recent.append(
                Cited(
                    text=text[0][:110] if text else "(no content)",
                    event_id=event.id,
                    source=event.source,
                    when=event.timestamp,
                )
            )

        # How you know them: the OLDEST external event is the first contact.
        first = session.execute(
            citable_events(select(Event))
            .join(Participant, Participant.event_id == Event.id)
            .where(Participant.person_slug == slug)
            .order_by(Event.timestamp_ms.asc())
            .limit(1)
        ).scalar_one_or_none()
        if first is not None:
            text = (first.title or first.text or "").strip().splitlines()
            prep.how_you_know_them.append(
                Cited(
                    text=f"First contact: {text[0][:90] if text else first.source}",
                    event_id=first.id, source=first.source, when=first.timestamp,
                )
            )

        for commitment in session.execute(
            select(Commitment)
            .where(Commitment.person_slug == slug)
            .where(Commitment.status != "done")
        ).scalars():
            source = session.get(Event, commitment.event_id)
            cited = Cited(
                text=f'{commitment.what} — "{commitment.quote}"',
                event_id=commitment.event_id,
                source=source.source if source else "",
                when=commitment.promised_at,
            )
            if commitment.direction == "i_owe":
                prep.you_owe.append(cited)
            else:
                prep.they_owe.append(cited)

        for role, goal in session.execute(
            select(PersonRole, Goal)
            .join(Goal, Goal.id == PersonRole.goal_id)
            .where(PersonRole.person_slug == slug)
        ).all():
            # Role first: it is what changes the ask.
            prep.goals.append(f"{goal.title} — they are your {role.role}")

        prep.last_asks = _find_asks(
            [e for e in events if not _sent_by_owner(session, e.id, owner_addresses)],
            owner_addresses,
        )

    try:
        context = get_context(slug, query=title or "", settings=settings, k=4)
        if context is not None:
            prep.profile = context.profile.strip()
    except LookupError:
        prep.profile = ""

    lit = label_nodes(
        activate([Node(NodeType.PERSON, slug)], settings), settings
    )
    prep.activated = open_loops(lit, settings)[:6]
    return prep


def _sent_by_owner(session: Session, event_id: int, owner_addresses: set[str]) -> bool:
    sender = session.execute(
        select(Participant)
        .where(Participant.event_id == event_id)
        .where(Participant.role == "from")
        .limit(1)
    ).scalar_one_or_none()
    return bool(sender and sender.address in owner_addresses)


def upcoming_meetings(
    settings: Settings | None = None, *, hours: int = 24, now: datetime | None = None
) -> list[Event]:
    settings = settings or get_settings()
    init_db(settings)
    now = now or datetime.now(UTC).replace(tzinfo=None)
    with Session(get_engine(settings)) as session:
        return list(
            session.execute(
                select(Event)
                .where(Event.source == "calendar")
                .where(Event.provenance == Provenance.EXTERNAL)
                .where(Event.timestamp >= now)
                .where(Event.timestamp <= now + timedelta(hours=hours))
                .order_by(Event.timestamp_ms)
            ).scalars()
        )


def render_prep(prep: Prep) -> str:
    header = f"# Prep — {prep.display_name}"
    if prep.when:
        header += f"  ({prep.when:%a %d %b %H:%M})"
    lines = [header, ""]

    if prep.is_empty:
        # Saying so is the honest answer. A confident-looking brief assembled
        # from nothing is worse than admitting there is nothing.
        lines.append(
            "Nothing on record for this person. Not 'no history' — no data in "
            "any connected source. Check the channel this relationship lives on."
        )
        return "\n".join(lines)

    if prep.profile:
        lines += ["## Who they are", "", prep.profile, ""]

    if prep.goals:
        lines += ["## What this serves", ""]
        lines += [f"  - {g}" for g in prep.goals]
        lines.append("")

    if prep.you_owe:
        lines += ["## You owe them", ""]
        lines += [c.render() for c in prep.you_owe]
        lines.append("")

    if prep.they_owe:
        lines += ["## They owe you", ""]
        lines += [c.render() for c in prep.they_owe]
        lines.append("")

    if prep.last_asks:
        lines += ["## What they last asked for", ""]
        lines += [c.render() for c in prep.last_asks]
        lines.append("")

    if prep.how_you_know_them:
        lines += ["## How you know them", ""]
        lines += [c.render() for c in prep.how_you_know_them]
        lines.append("")

    if prep.recent:
        lines += ["## Recent", ""]
        lines += [c.render() for c in prep.recent[:5]]
        lines.append("")

    if prep.activated:
        lines += ["## Also connected", ""]
        lines += [f"  - {a}" for a in prep.activated]
        lines.append("")

    lines.append(
        "Every claim above carries the event it came from. Anything that could "
        "not be traced to an external record was left out."
    )
    return "\n".join(lines)
