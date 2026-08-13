"""The morning brief: what happened, what needs you, and what can wait.

This is the first stage whose output a human reads every day, so the ordering
is opinionated. `needs_response` comes first, sorted by urgency, each with
retrieved context about the sender — because the question the brief answers
is "what do I have to do", not "what arrived".

Promotional and spam are counted, never listed. A brief that lists 40
marketing emails is the inbox again, with extra steps.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.context.retrieve import RetrievedContext, get_context
from personalagi.db import get_engine, init_db
from personalagi.events import EventView, load_views
from personalagi.models import Classification, Event

log = logging.getLogger(__name__)

URGENCY_ORDER = {"high": 0, "med": 1, "low": 2}
URGENCY_MARK = {"high": "!!", "med": "!", "low": "  "}


@dataclass
class BriefItem:
    view: EventView
    classification: Classification
    context: RetrievedContext | None = None

    @property
    def urgency(self) -> str:
        return self.classification.urgency or "low"

    @property
    def sender(self) -> str:
        return self.view.sender_name or self.view.sender_address

    @property
    def sort_key(self) -> tuple[int, int]:
        return (URGENCY_ORDER.get(self.urgency, 3), -self.view.event.timestamp_ms)


@dataclass
class Brief:
    day: date
    window_days: int
    needs_response: dict[str, list[BriefItem]] = field(default_factory=dict)
    fyi: list[BriefItem] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    total_messages: int = 0
    unclassified: int = 0

    @property
    def action_count(self) -> int:
        return sum(len(items) for items in self.needs_response.values())


def build_brief(
    settings: Settings | None = None,
    *,
    day: date | None = None,
    window_days: int = 1,
    accounts: list[str] | None = None,
    context_k: int = 3,
) -> Brief:
    settings = settings or get_settings()
    init_db(settings)

    today = day or datetime.now(UTC).date()
    cutoff = datetime.combine(today - timedelta(days=window_days - 1), datetime.min.time())
    cutoff_ms = int(cutoff.replace(tzinfo=UTC).timestamp() * 1000)
    end_ms = int(
        datetime.combine(today + timedelta(days=1), datetime.min.time())
        .replace(tzinfo=UTC)
        .timestamp()
        * 1000
    )

    brief = Brief(day=today, window_days=window_days)

    with Session(get_engine(settings)) as session:
        stmt = (
            select(Event)
            .where(Event.timestamp_ms >= cutoff_ms)
            .where(Event.timestamp_ms < end_ms)
        )
        if accounts:
            stmt = stmt.where(Event.account_label.in_(accounts))
        views = load_views(session, stmt)
        classifications = {
            c.event_id: c
            for c in session.execute(
                select(Classification).where(
                    Classification.event_id.in_([v.event.id for v in views])
                )
            ).scalars()
        }
        rows = [(v, classifications[v.event.id]) for v in views if v.event.id in classifications]

    for view, classification in rows:
        brief.total_messages += 1
        category = classification.category
        brief.counts[category] += 1

        if not classification.ok:
            brief.unclassified += 1
            continue

        if category == "needs_response":
            brief.needs_response.setdefault(view.event.account_label, []).append(
                BriefItem(view, classification)
            )
        elif category == "fyi":
            brief.fyi.append(BriefItem(view, classification))

    # Retrieve context only for what needs action. Doing it for every FYI
    # would cost a file read and an FTS query per message for information
    # nobody acts on.
    for items in brief.needs_response.values():
        items.sort(key=lambda item: item.sort_key)
        for item in items:
            try:
                item.context = get_context(
                    item.view.person_slug or item.view.sender_address,
                    item.view.event.title or "",
                    settings,
                    k=context_k,
                    # A message must not be retrieved as context for itself.
                    exclude_source_ids={item.view.event.source_id},
                )
            except LookupError:
                item.context = None

    brief.fyi.sort(key=lambda item: item.sort_key)
    return brief


def render_brief(brief: Brief) -> str:
    lines = [
        f"# Brief — {brief.day.isoformat()}",
        "",
    ]
    window = (
        "last 24 hours"
        if brief.window_days == 1
        else f"last {brief.window_days} days"
    )
    lines.append(
        f"{brief.total_messages} message(s) in the {window}. "
        f"{brief.action_count} need you."
    )
    lines.append("")

    if brief.action_count:
        lines.append("## Needs response")
        lines.append("")
        for account in sorted(brief.needs_response):
            items = brief.needs_response[account]
            lines.append(f"### {account}")
            lines.append("")
            for item in items:
                mark = URGENCY_MARK.get(item.urgency, "  ")
                lines.append(
                    f"- {mark} **{item.sender}** — {item.classification.summary}"
                )
                lines.append(
                    f"      _{item.view.event.title}_  "
                    f"({item.urgency}, {item.view.event.timestamp:%b %d %H:%M})"
                )
                if item.context and item.context.profile:
                    lines.append(f"      context: {item.context.profile}")
                elif item.context and item.context.log_lines:
                    lines.append(f"      previously: {item.context.log_lines[0]}")
                lines.append("")
    else:
        lines += ["## Needs response", "", "_Nothing. Enjoy it._", ""]

    if brief.fyi:
        lines += ["## FYI", ""]
        for item in brief.fyi:
            lines.append(f"- {item.sender} — {item.classification.summary}")
        lines.append("")

    promo = brief.counts.get("promotional", 0)
    spam = brief.counts.get("spam", 0)
    lines += [
        "## Filtered",
        "",
        f"- promotional: {promo}",
        f"- spam: {spam}",
    ]
    if brief.unclassified:
        lines.append(f"- unclassified (model failed): {brief.unclassified}")
    lines.append("")
    return "\n".join(lines)


def write_brief(brief: Brief, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.briefs_dir / f"{brief.day.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_brief(brief), encoding="utf-8")
    return path
