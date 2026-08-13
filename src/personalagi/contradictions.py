"""Stage 31: two statements about one goal that cannot both be true.

The most valuable check in the system and the hardest, for the same reason:
**you cannot catch this yourself.** A missed deadline you eventually notice. A
promise you forgot the system already tracks. But telling one sponsor the venue
is free and another that it costs $500 is invisible from the inside, because
each conversation was individually coherent.

Scoped to one goal at a time, deliberately. Comparing every statement to every
other across 4,346 events is quadratic and would mostly surface people
disagreeing about lunch. Statements attached to the same goal are the ones
where an inconsistency actually costs something.

THE FALSE-POSITIVE RULE
A plan that CHANGED is not a contradiction. "The deadline is the 15th" followed
by "it moved to the 30th" is an update, and reporting it as a conflict sends
the owner to re-read a thread for a problem that does not exist. Do that twice
and they stop reading these. So statements carry their dates, the prompt is
explicit that later supersedes earlier, and anything the model will not defend
is dropped.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.llm.client import GroqClient, LLMError
from personalagi.llm.prompts import load_prompt
from personalagi.models import (
    Commitment,
    Event,
    Goal,
    GoalStep,
    Participant,
    StepEvidence,
)
from personalagi.records import CallBudget, citable_events

log = logging.getLogger(__name__)

STATEMENT_CHARS = 400
MAX_STATEMENTS = 14
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


class ConflictOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    a: int
    b: int
    what: str = Field(default="", max_length=300)
    severity: str = "medium"

    @field_validator("severity", mode="before")
    @classmethod
    def _normalize(cls, value: object) -> object:
        if isinstance(value, str):
            key = value.strip().lower()
            return key if key in SEVERITY_ORDER else "medium"
        return "medium"


class ConflictsOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conflicts: list[ConflictOut] = Field(default_factory=list)

    @field_validator("conflicts", mode="before")
    @classmethod
    def _drop_junk(cls, value: object) -> object:
        if isinstance(value, list):
            return [c for c in value if isinstance(c, dict) and c.get("what")]
        return []


@dataclass
class Statement:
    ref: int
    text: str
    when: datetime
    who: str
    event_id: int | None = None

    def render(self) -> str:
        return f"[{self.ref}] {self.when:%Y-%m-%d} · {self.who}: {self.text}"


@dataclass
class Conflict:
    goal_slug: str
    goal_title: str
    what: str
    severity: str
    left: Statement
    right: Statement

    def render(self) -> str:
        return (
            f"[{self.severity.upper()}] {self.goal_title}\n"
            f"  {self.what}\n"
            f"    {self.left.render()}\n"
            f"    {self.right.render()}"
        )


@dataclass
class ContradictionResult:
    goals_checked: int = 0
    statements_read: int = 0
    conflicts: list[Conflict] = field(default_factory=list)
    calls: int = 0
    failed: int = 0

    def summary(self) -> str:
        return (
            f"checked {self.goals_checked} goal(s), {self.statements_read} "
            f"statement(s): {len(self.conflicts)} conflict(s) "
            f"({self.calls} call(s), {self.failed} failed)"
        )


def gather_statements(
    session: Session, goal: Goal, *, limit: int = MAX_STATEMENTS
) -> list[Statement]:
    """Everything on record that bears on this goal, newest first.

    Only EXTERNAL events. A summary the system wrote could otherwise
    "contradict" the message it was summarising, which would be the
    self-citation bug wearing a different hat.
    """
    statements: list[Statement] = []
    ref = 1

    for commitment in session.execute(
        select(Commitment).where(Commitment.goal_id == goal.id)
    ).scalars():
        statements.append(
            Statement(
                ref=ref,
                text=commitment.quote or commitment.what,
                when=commitment.promised_at,
                who=commitment.person_name or commitment.person_slug or "someone",
                event_id=commitment.event_id,
            )
        )
        ref += 1

    rows = session.execute(
        citable_events(select(Event))
        .join(StepEvidence, StepEvidence.event_id == Event.id)
        .join(GoalStep, GoalStep.id == StepEvidence.step_id)
        .where(GoalStep.goal_id == goal.id)
        .order_by(Event.timestamp_ms.desc())
        .limit(limit)
    ).scalars()
    for event in rows:
        sender = session.execute(
            select(Participant)
            .where(Participant.event_id == event.id)
            .where(Participant.role == "from")
            .limit(1)
        ).scalar_one_or_none()
        text = " ".join((event.text or event.title or "").split())[:STATEMENT_CHARS]
        if not text:
            continue
        statements.append(
            Statement(
                ref=ref,
                text=text,
                when=event.timestamp,
                who=(sender.display_name or sender.address) if sender else "unknown",
                event_id=event.id,
            )
        )
        ref += 1

    # Newest last, so "later supersedes earlier" is visible in reading order.
    statements.sort(key=lambda s: s.when)
    return statements[:limit]


def find_contradictions(
    settings: Settings | None = None,
    *,
    goal_slug: str | None = None,
    budget: CallBudget | None = None,
) -> ContradictionResult:
    """Check each active goal's statements against each other."""
    settings = settings or get_settings()
    init_db(settings)
    result = ContradictionResult()

    with Session(get_engine(settings)) as session:
        stmt = select(Goal).where(Goal.status == "active")
        if goal_slug:
            stmt = stmt.where(Goal.slug == goal_slug)
        goals = list(session.execute(stmt).scalars())

        by_goal: dict[int, list[Statement]] = {}
        for goal in goals:
            statements = gather_statements(session, goal)
            # Fewer than two statements cannot contradict. Skipping them keeps
            # the call count proportional to real work.
            if len(statements) >= 2:
                by_goal[goal.id] = statements
            result.goals_checked += 1

        goal_by_id = {g.id: g for g in goals}

    if not by_goal:
        return result

    prompt = load_prompt("contradiction")
    client = GroqClient(settings, model=settings.groq_model_large or None)

    for goal_id, statements in by_goal.items():
        goal = goal_by_id[goal_id]
        result.statements_read += len(statements)
        if budget is not None and not budget.can_spend():
            log.warning("contradiction check stopped: budget exhausted")
            break

        rendered = "\n".join(s.render() for s in statements)
        try:
            if budget is not None:
                budget.spend()
            result.calls += 1
            content = client.complete_json(
                prompt.system,
                prompt.render_user(goal=goal.title, statements=rendered),
                max_tokens=1200,
            )
            parsed = ConflictsOut.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError, LLMError) as exc:
            log.debug("contradiction check failed on %s: %s", goal.slug, exc)
            result.failed += 1
            continue

        by_ref = {s.ref: s for s in statements}
        for conflict in parsed.conflicts:
            left, right = by_ref.get(conflict.a), by_ref.get(conflict.b)
            if left is None or right is None or left.ref == right.ref:
                # A reference to a statement that was not shown is a
                # hallucinated pair. Drop it rather than render half of one.
                continue
            result.conflicts.append(
                Conflict(
                    goal_slug=goal.slug,
                    goal_title=goal.title,
                    what=conflict.what,
                    severity=conflict.severity,
                    left=left,
                    right=right,
                )
            )

    result.conflicts.sort(key=lambda c: SEVERITY_ORDER.get(c.severity, 1))
    log.info(result.summary())
    return result


def render_conflicts(result: ContradictionResult) -> str:
    if not result.conflicts:
        return (
            "No contradictions found.\n"
            f"Checked {result.goals_checked} goal(s) across "
            f"{result.statements_read} statement(s). This only covers statements "
            "linked to a goal — untracked conversations are not compared."
        )
    lines = [f"{len(result.conflicts)} contradiction(s):", ""]
    for conflict in result.conflicts:
        lines.append(conflict.render())
        lines.append("")
    return "\n".join(lines)
