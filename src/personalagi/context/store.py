"""Build the markdown vault from ingested mail.

Direction of flow is one-way and deliberate: SQLite messages -> markdown
files -> FTS index. The vault is the source of truth (D2), so nothing here
ever reads context back out of the database to decide what a file should say.
"""

from __future__ import annotations

import logging
import re
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
from personalagi.models import Classification, Message
from personalagi.search import fts

log = logging.getLogger(__name__)

# Categories worth a person file. Promotional and spam are counted but not
# written: a vault with 400 files for job-alert robots is not a context store,
# it is landfill. Override with include_categories=("*",).
DEFAULT_INCLUDE = ("needs_response", "fyi", "unclassified")

# Robot markers anywhere in the local part, delimiter-bounded. The prefix-only
# version of this missed `jobalerts-noreply@linkedin.com` — 377 messages in the
# real corpus — because the marker was not at the start.
_AUTOMATED_RE = re.compile(
    r"(?:^|[.\-+_])"
    r"("
    # machine senders
    r"no-?reply|do-?not-?reply|notifications?|alerts?|mailer|bounce|postmaster|"
    r"automated|digest|newsletter|unsubscribe|noreply|"
    # role accounts: a shared mailbox is a company, not a person, and a
    # person file for one is landfill. Dropping these was a regression that
    # gave support@luma.com a 338-entry "person" file.
    r"support|billing|receipts?|invoices?|sales|admin|info|contact|hello|help|"
    r"service|marketing|offers|deals|events|apply|careers|jobs|team|press|"
    r"newsroom|editors?|premium|promotions?|invitations?|updates?|news"
    r")"
    r"(?:[.\-+_]|$)",
    re.IGNORECASE,
)

# An address used by more than this many distinct display names is a shared
# bulk sender, not a person. LinkedIn sends every connection request from
# invitations@linkedin.com with the requester's name in the From header, so
# keying identity on the address would fuse hundreds of people into one file.
SHARED_ADDRESS_NAME_THRESHOLD = 3


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


def looks_automated(email: str) -> bool:
    """Heuristic, and deliberately conservative.

    Matches a robot marker anywhere in the local part, delimiter-bounded.
    Bounding is what keeps it conservative: `alerts@` and `job-alerts@` match,
    but `alerta@` and `dana@` do not. False negatives cost one junk file;
    false positives lose a real person, so the bound matters more than reach.
    """
    local = (email or "").split("@", 1)[0]
    return bool(_AUTOMATED_RE.search(local))


def shared_addresses(messages, threshold: int = SHARED_ADDRESS_NAME_THRESHOLD) -> set[str]:
    """Addresses used by many different display names.

    Data-driven rather than a hardcoded blocklist of providers: any bulk
    sender that stamps a human's name onto a shared envelope address gets
    caught, not just the ones I happened to think of.
    """
    names_by_address: dict[str, set[str]] = {}
    for message in messages:
        address = (message.sender_email or "").lower()
        name = (message.sender_name or "").strip().lower()
        if address and name:
            names_by_address.setdefault(address, set()).add(name)
    return {
        address
        for address, names in names_by_address.items()
        if len(names) > threshold
    }


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
