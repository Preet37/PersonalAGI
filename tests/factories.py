"""Builders for the canonical Event shape.

After Stage 8 the tests construct Events and Participants rather than Gmail
messages. Centralising that here is the point of the refactor showing up in the
test suite: adding iMessage means one more factory, not a rewrite of every
fixture that happened to say `sender_email`.
"""

from __future__ import annotations

import json
from datetime import datetime

from personalagi.adapters.base import CC, FROM, TO
from personalagi.events import EventView
from personalagi.models import Event, Participant

DEFAULT_MS = 1_786_353_600_000


def make_event(
    *,
    eid: int = 1,
    source: str = "gmail",
    source_id: str | None = None,
    account: str = "personal",
    title: str = "Benchmark v2",
    text: str = "b",
    ms: int = DEFAULT_MS,
    thread_key: str = "t",
    metadata: dict | None = None,
) -> Event:
    return Event(
        id=eid,
        source=source,
        source_id=source_id or f"g{eid}",
        account_label=account,
        thread_key=thread_key,
        title=title,
        text=text,
        timestamp=datetime.fromtimestamp(ms / 1000),
        timestamp_ms=ms,
        metadata_json=json.dumps(metadata or {}),
        ingested_at=datetime(2026, 8, 13),
    )


def make_participant(
    address: str,
    display_name: str = "",
    *,
    eid: int = 1,
    role: str = FROM,
    is_owner: bool = False,
    is_automated: bool = False,
    automated_reason: str = "",
    person_slug: str | None = None,
) -> Participant:
    if person_slug is None:
        person_slug = "" if (is_automated or is_owner) else _slug(display_name, address)
    return Participant(
        event_id=eid,
        address=address.lower(),
        display_name=display_name,
        role=role,
        is_owner=is_owner,
        is_automated=is_automated,
        automated_reason=automated_reason,
        person_slug=person_slug,
    )


def _slug(name: str, address: str) -> str:
    from personalagi.context.people import slug_for

    return slug_for(name, address)


def make_view(
    *,
    eid: int = 1,
    sender: str = "Dana Okafor",
    email: str = "dana@example.com",
    to: list[tuple[str, str]] | None = None,
    cc: list[tuple[str, str]] | None = None,
    owner_addresses: set[str] | None = None,
    sender_automated: bool = False,
    automated_reason: str = "",
    **event_kwargs,
) -> EventView:
    """An Event plus resolved participants — the unit downstream code works in."""
    owner_addresses = {a.lower() for a in (owner_addresses or set())}
    event = make_event(eid=eid, **event_kwargs)

    participants = [
        make_participant(
            email,
            sender,
            eid=eid,
            role=FROM,
            is_owner=email.lower() in owner_addresses,
            is_automated=sender_automated,
            automated_reason=automated_reason
            or ("robot address pattern" if sender_automated else ""),
        )
    ]
    for role, people in ((TO, to or []), (CC, cc or [])):
        for name, address in people:
            participants.append(
                make_participant(
                    address,
                    name,
                    eid=eid,
                    role=role,
                    is_owner=address.lower() in owner_addresses,
                )
            )

    return EventView(event=event, participants=participants)
