"""The shape every adapter produces, and the participant resolution they share.

An adapter's job is narrow: turn one native record into an EventRecord plus its
ParticipantRecords. It does NOT decide who is a person, who is a robot, or
which person file an address belongs to — that is resolved here, once, so
every source inherits it.

That sharing is the whole argument for D1. The rules that took a night of real
data to find — robot markers anywhere in the local part, the 215-display-names
shared-envelope rule, List-Unsubscribe outranking address shape — were written
for Gmail and now apply to iMessage handles and calendar organisers without a
line of new code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from personalagi.context.people import slug_for
from personalagi.identity import automated_by_header, looks_automated

FROM, TO, CC, ATTENDEE = "from", "to", "cc", "attendee"


@dataclass
class ParticipantRecord:
    address: str = ""
    display_name: str = ""
    role: str = FROM
    is_owner: bool = False
    is_automated: bool = False
    automated_reason: str = ""
    person_slug: str = ""

    def as_row(self, event_id: int) -> dict:
        return {
            "event_id": event_id,
            "address": (self.address or "").strip().lower(),
            "display_name": self.display_name or "",
            "role": self.role,
            "is_owner": self.is_owner,
            "is_automated": self.is_automated,
            "automated_reason": self.automated_reason,
            "person_slug": self.person_slug,
        }


@dataclass
class EventRecord:
    source: str
    source_id: str
    timestamp: datetime
    timestamp_ms: int
    account_label: str = ""
    thread_key: str = ""
    title: str = ""
    text: str = ""
    metadata: dict = field(default_factory=dict)
    participants: list[ParticipantRecord] = field(default_factory=list)

    @property
    def sender(self) -> ParticipantRecord | None:
        return next((p for p in self.participants if p.role == FROM), None)

    def as_row(self, ingested_at: datetime, event_id: int | None = None) -> dict:
        row = {
            "source": self.source,
            "source_id": self.source_id,
            "account_label": self.account_label,
            "thread_key": self.thread_key,
            "title": self.title,
            "text": self.text,
            "timestamp": self.timestamp,
            "timestamp_ms": self.timestamp_ms,
            "metadata_json": json.dumps(self.metadata, ensure_ascii=False, default=str),
            "ingested_at": ingested_at,
        }
        if event_id is not None:
            row["id"] = event_id
        return row


def resolve_participant(
    address: str,
    display_name: str = "",
    *,
    role: str = FROM,
    owner_addresses: set[str] | None = None,
    shared: set[str] | None = None,
    headers: dict[str, str] | None = None,
) -> ParticipantRecord:
    """Decide what one address IS, using every signal available.

    Order matters and mirrors identity.classify_sender: declarations first
    (headers), then address shape, then corpus-wide statistics (the shared
    envelope set). `headers` only applies to the sender — a List-Unsubscribe
    says the message is bulk, which says nothing about its recipients.
    """
    normalized = (address or "").strip().lower()
    owner_addresses = owner_addresses or set()
    shared = shared or set()

    record = ParticipantRecord(
        address=normalized,
        display_name=(display_name or "").strip().strip('"'),
        role=role,
        is_owner=normalized in owner_addresses,
    )
    if not normalized:
        return record

    if role == FROM and headers:
        reason = automated_by_header(headers)
        if reason:
            record.is_automated = True
            record.automated_reason = f"header {reason}"

    if not record.is_automated and looks_automated(normalized):
        record.is_automated = True
        record.automated_reason = "robot address pattern"

    if not record.is_automated and normalized in shared:
        record.is_automated = True
        record.automated_reason = "shared bulk envelope (many display names)"

    # A person file is only ever created for a real, non-owner human. Leaving
    # the slug empty for everyone else is what stops the vault filling with
    # files for robots and with one fused file for 215 different people.
    if not record.is_automated and not record.is_owner:
        record.person_slug = slug_for(record.display_name, normalized)

    return record
