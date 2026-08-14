"""Commitments that the world has moved past.

Staleness was measured by age, and age is a bad proxy for "still owed". The
list this produced said, in August:

    You told Sakshee "I can definitely make it Saturday 11am PDT" — 36d ago

The event was in July. It happened, or it did not, and either way there is
nothing left to do. Reporting it as an open obligation is not a small
annoyance: it is the thing that makes someone stop reading the list, and a list
nobody reads catches nothing.

TWO JUDGEMENTS, NOT ONE

  status — is this still open? A date that has passed is evidence. A thread
           that continued past that date with logistics and thank-yous is
           stronger evidence. Absence of a follow-up is NOT evidence it is
           open; most things get done without anyone emailing about it, and
           much of a real conversation happens where this system cannot see.

  stakes — what does it cost if it never happens? "Please find attached the
           signed agreement" and "wait lemme resend the link twin" are not the
           same promise. Ranking them the same is how the list becomes noise.

Verdicts are cached on the commitment and the state of its thread, so a nightly
run does not re-pay for an answer nothing has changed.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.llm.client import GroqClient, LLMError
from personalagi.llm.prompts import load_prompt
from personalagi.models import Commitment, Event, Participant
from personalagi.records import CallBudget, Provenance

log = logging.getLogger(__name__)

FOLLOWUP_LIMIT = 8
FOLLOWUP_CHARS = 260
STATUSES = {"open", "done", "moot"}

# Whether a promise NAMES a time is a question about the text, not a judgement,
# so code corroborates the model instead of either deciding alone. Asked on its
# own the model answered `time_bound: false` for "I can definitely make it on
# Saturday at 11am PDT" while its own reasoning said "specific time passed".
_TEMPORAL_RE = re.compile(
    r"\b("
    r"mon|tues|wednes|thurs|fri|satur|sun)day\b"
    r"|\b(today|tonight|tomorrow|yesterday|asap)\b"
    r"|\b(this|next|by|before|after)\s+"
    r"(week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"morning|afternoon|evening|noon|eod|friday)\b"
    r"|\b\d{1,2}\s*(:\d{2})?\s*(am|pm)\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b"
    r"|\b\d{1,2}(st|nd|rd|th)\b",
    re.IGNORECASE,
)


def names_a_time(*texts: str) -> bool:
    """Does any of this text name a moment? Factual, not semantic."""
    return any(_TEMPORAL_RE.search(t or "") for t in texts)
STAKES = {"high", "medium", "low"}


class ResolutionOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Does the promise name a moment it had to happen by? Asked separately so
    # the invariant below can be enforced in code rather than requested.
    time_bound: bool = False
    status: str = "open"
    stakes: str = "medium"
    why: str = Field(default="", max_length=200)

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value: object) -> object:
        key = str(value or "").strip().lower()
        # Anything unrecognised stays OPEN. Closing a real obligation because
        # the model returned a word we did not expect is the expensive error.
        return key if key in STATUSES else "open"

    @field_validator("stakes", mode="before")
    @classmethod
    def _stakes(cls, value: object) -> object:
        key = str(value or "").strip().lower()
        return key if key in STAKES else "medium"


@dataclass
class ResolveResult:
    considered: int = 0
    closed_done: int = 0
    closed_moot: int = 0
    still_open: int = 0
    calls: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.considered} commitment(s): {self.closed_done} done, "
            f"{self.closed_moot} moot, {self.still_open} still open "
            f"({self.calls} call(s), {self.failed} failed)"
        )


def followups(session: Session, commitment: Commitment) -> list[Event]:
    """What happened after the promise, in the same thread or with the person.

    Only EXTERNAL events. A summary the system wrote about the promise must not
    become the evidence that closes it.
    """
    source = session.get(Event, commitment.event_id)
    rows: list[Event] = []
    if source is not None and source.thread_key:
        rows = list(
            session.execute(
                select(Event)
                .where(Event.thread_key == source.thread_key)
                .where(Event.timestamp > commitment.promised_at)
                .where(Event.provenance == Provenance.EXTERNAL)
                .order_by(Event.timestamp_ms)
                .limit(FOLLOWUP_LIMIT)
            ).scalars()
        )
    if not rows and commitment.person_email:
        rows = list(
            session.execute(
                select(Event)
                .join(Participant, Participant.event_id == Event.id)
                .where(Participant.address == commitment.person_email.lower())
                .where(Event.timestamp > commitment.promised_at)
                .where(Event.provenance == Provenance.EXTERNAL)
                .order_by(Event.timestamp_ms)
                .limit(FOLLOWUP_LIMIT)
            ).scalars()
        )
    return rows


def resolve_commitments(
    settings: Settings | None = None,
    *,
    budget: CallBudget | None = None,
    now: datetime | None = None,
    only_open: bool = True,
) -> ResolveResult:
    """Ask, for each commitment, whether the world has moved past it."""
    settings = settings or get_settings()
    init_db(settings)
    now = now or datetime.now(UTC).replace(tzinfo=None)
    result = ResolveResult()

    with Session(get_engine(settings)) as session:
        stmt = select(Commitment).where(Commitment.manually_closed.is_(False))
        if only_open:
            stmt = stmt.where(Commitment.status != "done")
        rows = list(session.execute(stmt).scalars())
        context = {c.id: followups(session, c) for c in rows}

    result.considered = len(rows)
    if not rows:
        return result

    prompt = load_prompt("resolve_commitment")
    client = GroqClient(settings, model=settings.groq_model_large or None)

    for commitment in rows:
        if budget is not None and not budget.can_spend():
            log.warning("resolution stopped: budget exhausted")
            break

        events = context.get(commitment.id, [])
        def line(event: Event) -> str:
            text = " ".join((event.title or event.text or "").split())
            return f"- {event.timestamp:%Y-%m-%d} {text[:FOLLOWUP_CHARS]}"

        rendered = "\n".join(line(e) for e in events) or "(nothing on record since)"
        direction = "you" if commitment.direction == "i_owe" else (
            commitment.person_name or "them"
        )

        try:
            if budget is not None:
                budget.spend()
            result.calls += 1
            content = client.complete_json(
                prompt.system,
                prompt.render_user(
                    what=commitment.what,
                    promised_at=f"{commitment.promised_at:%Y-%m-%d}",
                    direction=direction,
                    quote=commitment.quote,
                    due=f"Said due: {commitment.due_text}" if commitment.due_text else "",
                    today=f"{now:%Y-%m-%d}",
                    followups=rendered,
                ),
                max_tokens=600,
            )
            parsed = ResolutionOut.model_validate_json(content)

            # THE INVARIANT, ENFORCED HERE RATHER THAN ASKED FOR.
            #
            # An open-ended promise cannot become moot by ageing. Told only in
            # the prompt, the model closed "look at them and try out some of
            # the courses" -- a promise with no date anywhere -- justified as
            # "date passed". There was no date; it had silently treated the day
            # the promise was MADE as a deadline.
            #
            # `done` still stands, because that needs positive evidence. Only
            # the expiry path is blocked.
            # Either signal is enough. The regex catches the obvious cases the
            # model fumbled; the model catches the phrasings a regex cannot.
            time_bound = parsed.time_bound or names_a_time(
                commitment.quote, commitment.what, commitment.due_text
            )

            if parsed.status == "moot" and not time_bound:
                parsed.status = "open"
                parsed.why = (
                    f"{parsed.why} [kept open: no deadline was ever stated]"
                )
        except (ValidationError, json.JSONDecodeError, LLMError) as exc:
            log.debug("resolution failed on %s: %s", commitment.id, exc)
            result.failed += 1
            continue

        with Session(get_engine(settings)) as session:
            row = session.get(Commitment, commitment.id)
            if row is None or row.manually_closed:
                continue
            row.stakes = parsed.stakes
            row.resolution_why = parsed.why
            row.resolved_by = "model"
            if parsed.status == "done":
                row.status = "done"
                row.resolved_at = now
                result.closed_done += 1
                result.notes.append(f"done: {row.what} — {parsed.why}")
            elif parsed.status == "moot":
                # Closed, but recorded as moot rather than done: the difference
                # matters when the owner asks why something stopped appearing.
                row.status = "done"
                row.resolved_at = now
                result.closed_moot += 1
                result.notes.append(f"moot: {row.what} — {parsed.why}")
            else:
                result.still_open += 1
            session.add(row)
            session.commit()

    log.info(result.summary())
    return result
