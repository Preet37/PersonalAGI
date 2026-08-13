"""Build the markdown vault from ingested mail.

Direction of flow is one-way and deliberate: SQLite messages -> markdown
files -> FTS index. The vault is the source of truth (D2), so nothing here
ever reads context back out of the database to decide what a file should say.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.context.people import (
    LogEntry,
    PersonFile,
    load_person,
    save_person,
    slug_for,
)
from personalagi.db import get_engine, init_db
from personalagi.identity import SHARED_ADDRESS_NAME_THRESHOLD, looks_automated
from personalagi.identity import shared_addresses as _shared_addresses
from personalagi.models import Classification, Message
from personalagi.search import fts

log = logging.getLogger(__name__)

# Categories worth a person file. Promotional and spam are counted but not
# written: a vault with 400 files for job-alert robots is not a context store,
# it is landfill. Override with include_categories=("*",).
DEFAULT_INCLUDE = ("needs_response", "fyi", "unclassified")

# The identity heuristics themselves live in personalagi.identity, which knows
# nothing about Gmail — Stage 8 needs them to serve iMessage handles and
# calendar organisers too. Re-exported here so callers and tests that predate
# the move keep working.
__all__ = [
    "SHARED_ADDRESS_NAME_THRESHOLD",
    "BuildResult",
    "build_people",
    "entry_text_for",
    "looks_automated",
    "shared_addresses",
]


@dataclass
class BuildResult:
    people_written: int = 0
    entries_added: int = 0
    messages_considered: int = 0
    skipped_category: int = 0
    skipped_automated: int = 0
    indexed_lines: int = 0
    by_person: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        top = sorted(self.by_person.items(), key=lambda kv: -kv[1])[:5]
        top_text = ", ".join(f"{slug}({n})" for slug, n in top) or "none"
        return (
            f"people={self.people_written}  entries+={self.entries_added}  "
            f"considered={self.messages_considered}  "
            f"skipped_category={self.skipped_category}  "
            f"skipped_automated={self.skipped_automated}  "
            f"indexed={self.indexed_lines}\n  top: {top_text}"
        )


def shared_addresses(messages, threshold: int = SHARED_ADDRESS_NAME_THRESHOLD) -> set[str]:
    """Message-shaped adapter over identity.shared_addresses."""
    return _shared_addresses(
        ((m.sender_email or "", m.sender_name or "") for m in messages),
        threshold=threshold,
    )


def entry_text_for(message: Message, classification: Classification | None) -> str:
    """Prefer the model's one-line summary; fall back to the subject."""
    if classification and classification.ok and classification.summary:
        return classification.summary
    subject = (message.subject or "").strip()
    return subject or "(no subject)"


def build_people(
    settings: Settings | None = None,
    *,
    account: str | None = None,
    include_categories: tuple[str, ...] = DEFAULT_INCLUDE,
    include_automated: bool = False,
    limit: int | None = None,
    context_dir: Path | None = None,
    reindex: bool = True,
) -> BuildResult:
    """Fold messages into per-person markdown files, then rebuild the index."""
    settings = settings or get_settings()
    root = context_dir or settings.context_dir
    init_db(settings)

    result = BuildResult()

    with Session(get_engine(settings)) as session:
        stmt = select(Message, Classification).join(
            Classification, Classification.message_id == Message.id, isouter=True
        )
        if account:
            stmt = stmt.where(Message.account_label == account)
        stmt = stmt.order_by(Message.internal_date_ms.asc())
        if limit:
            stmt = stmt.limit(limit)
        pairs = list(session.execute(stmt))

    shared = shared_addresses(m for m, _ in pairs)
    if shared:
        log.info("treating %d address(es) as shared bulk senders", len(shared))

    # Group by person first so each file is opened and written exactly once.
    grouped: dict[str, list[tuple[Message, Classification | None]]] = {}
    identities: dict[str, tuple[str, set[str]]] = {}

    for message, classification in pairs:
        result.messages_considered += 1

        category = classification.category if classification else "unclassified"
        if "*" not in include_categories and category not in include_categories:
            result.skipped_category += 1
            continue

        if not include_automated and looks_automated(message.sender_email):
            result.skipped_automated += 1
            continue

        slug = slug_for(message.sender_name, message.sender_email)
        grouped.setdefault(slug, []).append((message, classification))

        name, emails = identities.setdefault(
            slug, (message.sender_name or message.sender_email, set())
        )
        # Never attach a shared bulk address to a person. It is not their
        # address, and recording it would make two unrelated people look like
        # the same person on the next lookup.
        if message.sender_email and message.sender_email not in shared:
            emails.add(message.sender_email)

    for slug, items in grouped.items():
        name, emails = identities[slug]
        person = load_person(root, slug) or PersonFile(slug=slug, name=name)
        person.name = person.name or name
        person.emails = sorted(set(person.emails) | emails)

        added = 0
        for message, classification in items:
            entry = LogEntry(
                entry_date=message.timestamp.date(),
                text=entry_text_for(message, classification),
                gmail_id=message.gmail_id,
            )
            if person.add_entry(entry):
                added += 1

        if added or not (root / "people" / f"{slug}.md").exists():
            save_person(root, person)
            result.people_written += 1
        result.entries_added += added
        result.by_person[slug] = len(person.log)

    if reindex:
        result.indexed_lines = fts.reindex(get_engine(settings), root)

    log.info(result.summary())
    return result
