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

from personalagi.context.people import slug_for, strip_directory_uid
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
    # EXTERNAL by default: almost every adapter reads records made by the
    # world. The exception is an assistant transcript, where the owner's turns
    # are external and the assistant's replies are GENERATED -- and if those
    # were citable the system could ground a claim in an answer a model
    # invented, which is the self-citation bug arriving through a new door.
    provenance: str = "external"

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
            "provenance": self.provenance,
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
        # Stripped here too, not just in the slug: otherwise every brief and
        # prep header reads "DJ Sampath (djsam)", showing the reader a
        # corporate login they never think of as part of the name.
        display_name=strip_directory_uid((display_name or "").strip().strip('"')),
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

    is_shared = normalized in shared
    if not record.is_automated and is_shared:
        record.is_automated = True
        record.automated_reason = "shared bulk envelope (many display names)"

    # A person file is only ever created for a real, non-owner human. Leaving
    # the slug empty for everyone else is what stops the vault filling with
    # files for robots and with one fused file for 215 different people.
    if not record.is_automated and not record.is_owner:
        record.person_slug = slug_for(record.display_name, normalized)

    elif is_shared and record.display_name and not record.is_owner:
        # A SHARED ENVELOPE CARRIES A REAL PERSON'S NAME.
        #
        # LinkedIn sends every connection request from invitations@linkedin.com
        # with the requester's name in the From header. Refusing to attach the
        # ADDRESS is correct -- it would fuse 215 unrelated people into one
        # file. But refusing the PERSON as well made all 215 of them invisible
        # to the graph, including the one the owner is meeting on Monday:
        #
        #   ('invitations@linkedin.com', 'Arjun Sambamoorthy', '', 1, ...)
        #
        # On a shared envelope the display name IS the identity and the address
        # is not. So the person is resolved from the name alone, and the
        # address is deliberately never recorded against them.
        #
        # Scoped to SHARED addresses specifically, not to bulk mail generally:
        # a newsletter has one constant display name, so it never qualifies and
        # never gets a person file.
        record.person_slug = slug_for(record.display_name, "")

    return record
