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
)
from personalagi.db import get_engine, init_db
from personalagi.events import EventView, load_views, pending_events
from personalagi.identity import (
    SHARED_ADDRESS_NAME_THRESHOLD,
    looks_automated,
    shared_addresses,
)
from personalagi.models import Classification
from personalagi.search import fts

log = logging.getLogger(__name__)

# Categories worth a person file. Promotional and spam are counted but not
# written: a vault with 400 files for job-alert robots is not a context store,
# it is landfill. Override with include_categories=("*",).
DEFAULT_INCLUDE = ("needs_response", "fyi", "unclassified")

# The identity heuristics live in personalagi.identity, which knows nothing
# about any source. Re-exported so callers and tests that predate the move keep
# working; build_people itself no longer calls them at all, because the adapter
# has already resolved every participant by the time an Event is stored.
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


def entry_text_for(view, classification: Classification | None) -> str:
    """Prefer the model's one-line summary; fall back to the event title.

    Takes an EventView. For a source with no title — an iMessage has no
    subject — the fallback is the first line of the text rather than a
    fabricated one.
    """
    if classification and classification.ok and classification.summary:
        return classification.summary
    title = (view.event.title or "").strip()
    if title:
        return title
    first_line = (view.event.text or "").strip().splitlines()
    return first_line[0][:120] if first_line else "(no content)"


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
    """Fold events into per-person markdown files, then rebuild the index.

    Reads Events, not Gmail messages. The automated-sender filter and the
    shared-envelope rule are no longer applied here at all — they were resolved
    once at adapter time and are read off the participant row. That is the
    payoff of D1: an iMessage thread lands in the same person file with no new
    filtering code.
    """
    settings = settings or get_settings()
    root = context_dir or settings.context_dir
    init_db(settings)

    result = BuildResult()

    with Session(get_engine(settings)) as session:
        stmt = pending_events(account=account, limit=limit, newest_first=False)
        views = load_views(session, stmt)
        classifications = {
            c.event_id: c
            for c in session.execute(select(Classification)).scalars()
        }

    # Group by person first so each file is opened and written exactly once.
    grouped: dict[str, list[tuple[EventView, Classification | None]]] = {}
    identities: dict[str, tuple[str, set[str]]] = {}

    for view in views:
        result.messages_considered += 1
        classification = classifications.get(view.event.id)

        category = classification.category if classification else "unclassified"
        if "*" not in include_categories and category not in include_categories:
            result.skipped_category += 1
            continue

        sender = view.sender
        if not include_automated and (sender is None or sender.is_automated):
            result.skipped_automated += 1
            continue

        # An empty person_slug means the adapter decided this participant must
        # never own a person file — a robot, the owner, or a shared envelope.
        slug = sender.person_slug
        if not slug:
            result.skipped_automated += 1
            continue

        grouped.setdefault(slug, []).append((view, classification))

        name, addresses = identities.setdefault(
            slug, (sender.display_name or sender.address, set())
        )
        if sender.address:
            addresses.add(sender.address)

    for slug, items in grouped.items():
        name, addresses = identities[slug]
        person = load_person(root, slug) or PersonFile(slug=slug, name=name)
        person.name = person.name or name
        # One person, several handles. Routed by shape so a phone number never
        # ends up in a field called `emails` — this is the merge point where
        # a Gmail address and an iMessage number become one file.
        person.emails = sorted(set(person.emails) | {a for a in addresses if "@" in a})
        person.phones = sorted(set(person.phones) | {a for a in addresses if "@" not in a})

        added = 0
        for view, classification in items:
            entry = LogEntry(
                entry_date=view.event.timestamp.date(),
                text=entry_text_for(view, classification),
                # Still called source_id in the log-line anchor format: the
                # vault is on disk with thousands of `[g:...]` anchors already
                # written, and rewriting them would break every existing file's
                # idempotency key for a cosmetic gain.
                source_id=view.event.source_id,
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
