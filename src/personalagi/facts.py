"""Stage 24: statements about the future, so a date arriving is a trigger.

Events record what happened. That is structurally unable to represent
"applications open in mid-August", which has not happened and may never — and
without it the system can never notice that mid-August has arrived.

A Fact turns a date into a trigger. `valid_from` passing is a free query, so
this costs nothing to check every morning; the only expense is extracting them
once, from messages that were going to be read anyway.

DATES ARE THE WHOLE VALUE AND THE WHOLE RISK
A fact with a guessed date fires a notification on the wrong day, which is
worse than never firing — it teaches the owner that the alerts are noise. So a
fact whose date cannot be resolved to an actual day is DISCARDED rather than
stored with an approximation, and the quote must be findable in the source or
the fact is dropped as ungrounded.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.events import EventView, load_views
from personalagi.llm.client import GroqClient, LLMError
from personalagi.llm.prompts import load_prompt
from personalagi.models import Event, ExtractedFact, Fact
from personalagi.records import CallBudget, citable_events

log = logging.getLogger(__name__)

BODY_CHARS = 1800


class FactOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statement: str = Field(default="", max_length=300)
    valid_from: str = ""
    valid_until: str = ""
    quote: str = Field(default="", max_length=400)
    confidence: float = 0.5

    @field_validator("statement", "quote", mode="before")
    @classmethod
    def _flatten(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, value: object) -> object:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.5


class FactsOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    facts: list[FactOut] = Field(default_factory=list)

    @field_validator("facts", mode="before")
    @classmethod
    def _drop_junk(cls, value: object) -> object:
        if isinstance(value, list):
            return [f for f in value if isinstance(f, dict) and f.get("statement")]
        return []


@dataclass
class FactResult:
    considered: int = 0
    extracted: int = 0
    stored: int = 0
    dropped_no_date: int = 0
    dropped_ungrounded: int = 0
    calls: int = 0
    failed: int = 0
    statements: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"considered={self.considered} extracted={self.extracted} "
            f"stored={self.stored}\n  dropped: {self.dropped_no_date} undateable, "
            f"{self.dropped_ungrounded} ungrounded  ({self.calls} call(s), "
            f"{self.failed} failed)"
        )


def parse_date(raw: str) -> datetime | None:
    """Strict. A date we cannot read is not a date we should guess."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.combine(date.fromisoformat(text), datetime.min.time())
    except ValueError:
        return None


def is_grounded(quote: str, body: str) -> bool:
    """The quote must actually be in the message.

    Same rule as commitments. A fact the model composed rather than read is a
    fact about nothing, and it would go on to fire a real notification.
    """
    needle = " ".join((quote or "").split()).casefold()
    if len(needle) < 10:
        return False
    return needle in " ".join((body or "").split()).casefold()


def statement_key(statement: str) -> str:
    return hashlib.sha256(
        " ".join((statement or "").lower().split()).encode()
    ).hexdigest()[:20]


def extract_from_view(
    view: EventView, client: GroqClient, prompt, settings: Settings
) -> list[FactOut]:
    body = (view.event.text or "").strip()[:BODY_CHARS]
    user = prompt.render_user(
        date=view.event.timestamp.date().isoformat(),
        sender=view.sender_name or view.sender_address or "(unknown)",
        subject=view.event.title or "(no subject)",
        body=body or "(empty)",
    )
    content = client.complete_json(prompt.system, user, max_tokens=1200)
    return FactsOut.model_validate_json(content).facts


def extract_facts(
    settings: Settings | None = None,
    *,
    account: str | None = None,
    limit: int | None = None,
    min_relevance: int = 2,
    budget: CallBudget | None = None,
    since: datetime | None = None,
) -> FactResult:
    """Pull future-dated statements out of events worth reading.

    Scoped to events that already scored as relevant. Running this over all
    4,346 events would spend real money re-reading marketing mail for dates
    that are sales tactics, and "offer ends soon" is not a fact.
    """
    settings = settings or get_settings()
    init_db(settings)
    result = FactResult()
    now = datetime.now(UTC).replace(tzinfo=None)

    from personalagi.models import Relevance

    with Session(get_engine(settings)) as session:
        stmt = (
            citable_events(select(Event))
            .join(Relevance, Relevance.event_id == Event.id)
            .where(Relevance.score >= min_relevance)
        )
        if account:
            stmt = stmt.where(Event.account_label == account)
        if since is not None:
            stmt = stmt.where(Event.timestamp >= since)
        stmt = stmt.order_by(Event.timestamp_ms.desc())
        if limit:
            stmt = stmt.limit(limit)
        views = load_views(session, stmt)

        known = {
            row.statement.lower().strip()
            for row in session.execute(select(Fact)).scalars()
        }

    result.considered = len(views)
    if not views:
        return result

    prompt = load_prompt("extract_facts")
    client = GroqClient(settings, model=settings.groq_model_large or None)

    for view in views:
        if budget is not None and not budget.can_spend():
            log.warning("fact extraction stopped: budget exhausted")
            break
        try:
            if budget is not None:
                budget.spend()
            result.calls += 1
            found = extract_from_view(view, client, prompt, settings)
        except (ValidationError, json.JSONDecodeError, LLMError) as exc:
            log.debug("fact extraction failed on %s: %s", view.event.source_id, exc)
            result.failed += 1
            continue

        for item in found:
            result.extracted += 1

            valid_from = parse_date(item.valid_from)
            if valid_from is None:
                # A guessed date fires on the wrong day, which teaches the
                # owner the alerts are noise. Drop it.
                result.dropped_no_date += 1
                continue
            if not is_grounded(item.quote, view.event.text):
                result.dropped_ungrounded += 1
                continue
            if item.statement.lower().strip() in known:
                continue

            known.add(item.statement.lower().strip())
            with Session(get_engine(settings)) as session:
                fact = Fact(
                    statement=item.statement,
                    valid_from=valid_from,
                    valid_until=parse_date(item.valid_until),
                    source_event_id=view.event.id,
                    person_slug=view.person_slug,
                    confidence=item.confidence,
                    status="open",
                    created_at=now,
                )
                session.add(fact)
                session.flush()
                session.add(
                    ExtractedFact(
                        fact_id=fact.id,
                        event_id=view.event.id,
                        quote=item.quote,
                        linked_at=now,
                    )
                )
                session.commit()
            result.stored += 1
            result.statements.append(item.statement)

    log.info(result.summary())
    return result


def add_fact(
    statement: str,
    valid_from: date,
    settings: Settings | None = None,
    *,
    valid_until: date | None = None,
    person_slug: str = "",
) -> Fact:
    """Record a fact by hand. No source event, so it is never citable."""
    settings = settings or get_settings()
    init_db(settings)
    now = datetime.now(UTC).replace(tzinfo=None)
    with Session(get_engine(settings)) as session:
        fact = Fact(
            statement=statement.strip(),
            valid_from=datetime.combine(valid_from, datetime.min.time()),
            valid_until=(
                datetime.combine(valid_until, datetime.min.time())
                if valid_until
                else None
            ),
            source_event_id=None,
            person_slug=person_slug,
            confidence=1.0,
            status="open",
            created_at=now,
        )
        session.add(fact)
        session.commit()
        session.refresh(fact)
        return fact


def list_facts(
    settings: Settings | None = None, *, status: str | None = "open"
) -> list[Fact]:
    settings = settings or get_settings()
    init_db(settings)
    with Session(get_engine(settings)) as session:
        stmt = select(Fact)
        if status:
            stmt = stmt.where(Fact.status == status)
        return list(session.execute(stmt.order_by(Fact.valid_from)).scalars())


def render_facts(rows: list[Fact], *, now: datetime | None = None) -> str:
    if not rows:
        return (
            "No facts recorded. Run `personalagi facts extract`, or add one:\n"
            '  personalagi facts add "Builder Club apps open" --from 2026-08-15'
        )
    now = now or datetime.now(UTC).replace(tzinfo=None)
    lines = []
    for row in rows:
        when = row.valid_from
        if when is None:
            marker, timing = "  ", "no date"
        else:
            # Compare the same way the sweep query does. `.days` truncates, so
            # a fact 8 hours in the future rounded to 0 and displayed as "OPEN
            # NOW" while the sweep -- correctly -- refused to fire it. A
            # display that disagrees with the trigger is worse than no display.
            is_open = when <= now
            delta = when - now
            marker = "->" if is_open else "  "
            timing = (
                "OPEN NOW"
                if is_open
                else (f"in {delta.days}d" if delta.days else "within 24h")
            )
        source = f"event {row.source_event_id}" if row.source_event_id else "manual"
        lines.append(f"{marker} {timing:>10}  {row.statement}   [{source}]")
    return "\n".join(lines)
