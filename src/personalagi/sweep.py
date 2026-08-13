"""Trigger B: time passed and nothing arrived. The absence is the signal.

This is the half of the system that catches what never showed up. A letter of
recommendation you never asked for generates no email, no message, and no
calendar entry — so an event-driven system is structurally incapable of
mentioning it, no matter how good its classifier is.

WHY THIS IS CHEAP
The naive proactive design wakes up and reasons over everything. On this corpus
that is 237 people and 4,346 events per sweep, forever, and almost every
conclusion is "nothing to report". You would pay hundreds of times the current
cost to be told nothing.

Almost none of these checks are AI problems:

    "deadline inside 30 days?"           date arithmetic
    "no activity in 14 days?"            date arithmetic
    "step with no evidence?"             anti-join
    "no contact in 90 days?"             MAX() and a comparison

All free. A model is only ever asked to WRITE the proposal for something that
already fired. Five calls a day, not five thousand — and a hard budget in code
in case that reasoning is ever wrong.

WHY IT STAYS QUIET
A reactive answer that is 70% right is useful, because you asked and you are
already evaluating it. An unprompted interruption that is 70% right is spam,
and three of those and the owner stops reading the system forever. So the most
common output here is nothing, suppressed proposals are STORED rather than
discarded, and `--show-suppressed` exists so the blind spots stay visible.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.models import (
    Commitment,
    Event,
    Fact,
    Goal,
    GoalStep,
    Participant,
    ProposalRecord,
    StepEvidence,
)
from personalagi.records import (
    AttentionLevel,
    BudgetExhausted,
    CallBudget,
    CommitmentStatus,
    GoalStatus,
    PermissionTier,
    Provenance,
    clamp_attention,
    max_attention_for,
)

log = logging.getLogger(__name__)


@dataclass
class Finding:
    """Something a free query turned up. Not yet a proposal.

    Ranked before any model call so the budget is spent on the best ones. The
    ordering below is the agenda, best signal first.
    """

    kind: str
    subject: str
    detail: str
    #: 0.0-1.0. Drives ordering and the suppression threshold.
    confidence: float
    #: EXTERNAL event ids supporting this. May be empty for absence findings —
    #: that is the point of them, and it is stated rather than faked.
    evidence: list[int] = field(default_factory=list)
    deadline: datetime | None = None
    goal_id: int | None = None
    person_slug: str = ""
    suggested_action: str = "draft_email"
    #: What the finding WANTS. Clamped against the deadline before it is used.
    wants_attention: str = AttentionLevel.AMBIENT

    def key(self) -> str:
        return f"{self.kind}:{self.subject}"


# The agenda, best signal first. Order matters: the budget runs out from the
# bottom, so the highest-signal findings are always the ones that get written.
AGENDA = (
    "deadline_gap",       # near deadline, required step, nothing supports it
    "stale_commitment",   # you promised, time passed, no evidence you did it
    "fact_window",        # a future-dated fact just became true
    "meeting_prep",       # a meeting inside 24h
    "stale_goal",         # something you said mattered, untouched
    "relationship_decay", # someone important, no contact in a long time
)


@dataclass
class SweepResult:
    findings: list[Finding] = field(default_factory=list)
    surfaced: int = 0
    suppressed: int = 0
    calls_spent: int = 0
    budget_exhausted: bool = False

    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.kind] = counts.get(finding.kind, 0) + 1
        return counts

    def summary(self) -> str:
        kinds = "  ".join(f"{k}={v}" for k, v in sorted(self.by_kind().items()))
        note = "  BUDGET EXHAUSTED" if self.budget_exhausted else ""
        return (
            f"{len(self.findings)} finding(s): {kinds or 'none'}\n"
            f"  surfaced={self.surfaced} suppressed={self.suppressed} "
            f"model_calls={self.calls_spent}{note}"
        )


# --- the seven queries, none of which cost a model call ----------------


def find_deadline_gaps(
    session: Session, now: datetime, settings: Settings
) -> list[Finding]:
    """A required step, a near deadline, and nothing anywhere supporting it.

    The highest-value signal in the system, and the only one that fires purely
    on absence. This is the Pratik case.
    """
    horizon = now + timedelta(days=settings.sweep_deadline_days)
    rows = session.execute(
        select(Goal, GoalStep)
        .join(GoalStep, GoalStep.goal_id == Goal.id)
        .outerjoin(StepEvidence, StepEvidence.step_id == GoalStep.id)
        .where(Goal.status == GoalStatus.ACTIVE)
        .where(GoalStep.done.is_(False))
        .where(Goal.deadline.is_not(None))
        .where(Goal.deadline <= horizon)
        .group_by(GoalStep.id)
        .having(func.count(StepEvidence.id) == 0)
    ).all()

    findings = []
    for goal, step in rows:
        days = (goal.deadline - now).days
        # Confidence rises as the deadline closes. A blocking step is worse.
        urgency = max(0.0, min(1.0, 1.0 - (days / max(settings.sweep_deadline_days, 1))))
        confidence = round(min(1.0, 0.55 + 0.4 * urgency + (0.05 if step.blocking else 0)), 3)
        findings.append(
            Finding(
                kind="deadline_gap",
                subject=f"{goal.slug}/{step.id}",
                detail=(
                    f'"{step.description}" for "{goal.title}" — deadline in {days}d '
                    f"and nothing in any source supports it"
                ),
                confidence=confidence,
                evidence=[],  # by construction: the absence IS the finding
                deadline=goal.deadline,
                goal_id=goal.id,
                # As loud as the deadline permits. clamp_attention only ever
                # caps, so a finding that always asked for NUDGE could never
                # interrupt even the day before a deadline -- which is the one
                # case interrupting is for.
                wants_attention=max_attention_for(goal.deadline, now=now),
            )
        )
    return findings


def find_stale_commitments(
    session: Session, now: datetime, settings: Settings
) -> list[Finding]:
    """You promised, and nothing has happened since."""
    cutoff = now - timedelta(days=settings.commitment_stale_days)
    rows = session.execute(
        select(Commitment)
        .where(Commitment.direction == "i_owe")
        .where(Commitment.status != CommitmentStatus.DONE)
        .where(Commitment.manually_closed.is_(False))
        .where(
            func.coalesce(Commitment.last_activity_at, Commitment.promised_at) < cutoff
        )
    ).scalars()

    findings = []
    for row in rows:
        since = row.last_activity_at or row.promised_at
        days = (now - since).days
        findings.append(
            Finding(
                kind="stale_commitment",
                subject=str(row.id),
                detail=(
                    f'You told {row.person_name or row.person_email} "{row.quote}" '
                    f"— {days}d ago, nothing since"
                ),
                confidence=round(min(0.9, 0.5 + days / 100), 3),
                # A commitment always has its source event, so this one CAN
                # cite. Absence findings above cannot, and say so.
                evidence=[row.event_id],
                goal_id=row.goal_id,
                person_slug=row.person_slug,
            )
        )
    return findings


def find_fact_windows(
    session: Session, now: datetime, settings: Settings
) -> list[Finding]:
    """A future-dated fact just became true. The date is the trigger."""
    rows = session.execute(
        select(Fact)
        .where(Fact.status == "open")
        .where(Fact.valid_from.is_not(None))
        .where(Fact.valid_from <= now)
        .where((Fact.valid_until.is_(None)) | (Fact.valid_until >= now))
    ).scalars()

    return [
        Finding(
            kind="fact_window",
            subject=str(row.id),
            detail=f'"{row.statement}" — that window is open now',
            confidence=round(max(0.4, row.confidence or 0.6), 3),
            evidence=[row.source_event_id] if row.source_event_id else [],
            goal_id=row.goal_id,
            person_slug=row.person_slug,
            wants_attention=AttentionLevel.NUDGE,
        )
        for row in rows
    ]


def find_meetings(session: Session, now: datetime, settings: Settings) -> list[Finding]:
    """A meeting inside 24 hours. Known time, known people, inherently prep."""
    horizon = now + timedelta(hours=settings.sweep_meeting_hours)
    rows = session.execute(
        select(Event)
        .where(Event.source == "calendar")
        .where(Event.provenance == Provenance.EXTERNAL)
        .where(Event.timestamp >= now)
        .where(Event.timestamp <= horizon)
        .order_by(Event.timestamp_ms)
    ).scalars()

    return [
        Finding(
            kind="meeting_prep",
            subject=row.source_id,
            detail=f'"{row.title}" at {row.timestamp:%a %H:%M} — prep not assembled',
            confidence=0.8,
            evidence=[row.id],
            deadline=row.timestamp,
            wants_attention=AttentionLevel.NUDGE,
            suggested_action="append_context",
        )
        for row in rows
    ]


def find_stale_goals(
    session: Session, now: datetime, settings: Settings
) -> list[Finding]:
    """Something the owner said mattered, with nothing happening on it."""
    cutoff = now - timedelta(days=settings.sweep_goal_stale_days)
    rows = session.execute(
        select(Goal)
        .where(Goal.status == GoalStatus.ACTIVE)
        .where((Goal.last_activity.is_(None)) | (Goal.last_activity < cutoff))
    ).scalars()

    findings = []
    for goal in rows:
        if goal.last_activity is None:
            detail = f'"{goal.title}" has had no activity at all since it was created'
            confidence = 0.45
        else:
            days = (now - goal.last_activity).days
            detail = f'"{goal.title}" — nothing has moved in {days}d'
            confidence = round(min(0.75, 0.4 + days / 120), 3)
        findings.append(
            Finding(
                kind="stale_goal",
                subject=goal.slug,
                detail=detail,
                confidence=confidence,
                evidence=[],
                deadline=goal.deadline,
                goal_id=goal.id,
                wants_attention=AttentionLevel.AMBIENT,
            )
        )
    return findings


def find_relationship_decay(
    session: Session, now: datetime, settings: Settings
) -> list[Finding]:
    """Someone attached to an active goal, with no contact in a long time.

    Scoped to people on goals rather than all 237 person files. "You have not
    spoken to this newsletter in 90 days" is exactly the noise that gets a
    proactive system switched off.
    """
    from personalagi.models import PersonRole

    cutoff = now - timedelta(days=settings.sweep_contact_days)
    tracked = list(
        session.execute(
            select(PersonRole.person_slug, PersonRole.role, PersonRole.goal_id)
            .join(Goal, Goal.id == PersonRole.goal_id)
            .where(Goal.status == GoalStatus.ACTIVE)
        ).all()
    )

    findings = []
    for slug, role, goal_id in tracked:
        last = session.execute(
            select(func.max(Event.timestamp))
            .join(Participant, Participant.event_id == Event.id)
            .where(Participant.person_slug == slug)
            .where(Event.provenance == Provenance.EXTERNAL)
        ).scalar()

        if last is not None and last >= cutoff:
            continue
        days = (now - last).days if last else None
        detail = (
            f"{slug} ({role}) — no contact in {days}d"
            if days is not None
            else f"{slug} ({role}) — no contact on record at all"
        )
        findings.append(
            Finding(
                kind="relationship_decay",
                subject=slug,
                detail=detail,
                confidence=0.4 if days is None else round(min(0.7, 0.35 + days / 300), 3),
                evidence=[],
                goal_id=goal_id,
                person_slug=slug,
                wants_attention=AttentionLevel.AMBIENT,
            )
        )
    return findings


QUERIES = {
    "deadline_gap": find_deadline_gaps,
    "stale_commitment": find_stale_commitments,
    "fact_window": find_fact_windows,
    "meeting_prep": find_meetings,
    "stale_goal": find_stale_goals,
    "relationship_decay": find_relationship_decay,
}


# --- the sweep ---------------------------------------------------------


def collect_findings(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    kinds: tuple[str, ...] = AGENDA,
) -> list[Finding]:
    """Run every free query. No model calls happen here at all."""
    settings = settings or get_settings()
    init_db(settings)
    now = now or datetime.now(UTC).replace(tzinfo=None)

    findings: list[Finding] = []
    with Session(get_engine(settings)) as session:
        for kind in kinds:
            query = QUERIES.get(kind)
            if query is None:
                continue
            findings.extend(query(session, now, settings))

    # Agenda order first, then confidence. The budget runs out from the bottom,
    # so the highest-signal findings are the ones that get written.
    return sorted(
        findings,
        key=lambda f: (AGENDA.index(f.kind) if f.kind in AGENDA else 99, -f.confidence),
    )


def sweep(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    budget: CallBudget | None = None,
    dry_run: bool = False,
    kinds: tuple[str, ...] = AGENDA,
) -> SweepResult:
    """Run the agenda and record proposals for what fired.

    `dry_run` reports what WOULD be proposed without writing anything, which is
    how you check the sweep before letting it speak.
    """
    settings = settings or get_settings()
    init_db(settings)
    now = now or datetime.now(UTC).replace(tzinfo=None)
    budget = budget or CallBudget(limit=settings.sweep_call_budget)

    result = SweepResult(findings=collect_findings(settings, now=now, kinds=kinds))
    if dry_run:
        result.surfaced = sum(
            1 for f in result.findings if f.confidence >= settings.sweep_min_confidence
        )
        result.suppressed = len(result.findings) - result.surfaced
        return result

    with Session(get_engine(settings)) as session:
        seen = {
            row.trigger
            for row in session.execute(
                select(ProposalRecord).where(ProposalRecord.outcome == "pending")
            ).scalars()
        }

        for finding in result.findings:
            trigger = f"sweep:{finding.key()}"
            if trigger in seen:
                # Already pending. Re-proposing the same thing every morning is
                # how a useful nudge becomes noise the owner filters out.
                continue

            suppressed = finding.confidence < settings.sweep_min_confidence
            # The ceiling is enforced here, not requested from a model.
            ceiling = max_attention_for(finding.deadline, now=now)
            attention = clamp_attention(finding.wants_attention, ceiling)

            if not suppressed:
                # A model call would write the user-facing wording here. It is
                # only spent on findings that already cleared the threshold --
                # that is the entire cost argument.
                try:
                    budget.spend()
                    result.calls_spent += 1
                except BudgetExhausted:
                    result.budget_exhausted = True
                    log.warning(
                        "sweep budget of %d exhausted; %d finding(s) not written",
                        budget.limit,
                        len(result.findings) - result.calls_spent,
                    )
                    break

            session.add(
                ProposalRecord(
                    proposal_id=uuid.uuid4().hex,
                    trigger=trigger,
                    action_name=finding.suggested_action,
                    args_json="{}",
                    rationale=finding.detail,
                    evidence_event_ids=",".join(str(e) for e in finding.evidence),
                    confidence=finding.confidence,
                    permission_tier=PermissionTier.APPROVE,
                    attention_level=attention,
                    suppressed=suppressed,
                    goal_id=finding.goal_id,
                    created_at=now,
                )
            )
            if suppressed:
                result.suppressed += 1
            else:
                result.surfaced += 1
        session.commit()

    log.info(result.summary())
    return result


def pending_proposals(
    settings: Settings | None = None,
    *,
    include_suppressed: bool = False,
    min_attention: str = AttentionLevel.SILENT,
) -> list[ProposalRecord]:
    settings = settings or get_settings()
    init_db(settings)
    from personalagi.records import attention_rank

    with Session(get_engine(settings)) as session:
        stmt = select(ProposalRecord).where(ProposalRecord.outcome == "pending")
        if not include_suppressed:
            stmt = stmt.where(ProposalRecord.suppressed.is_(False))
        rows = list(session.execute(stmt).scalars())

    floor = attention_rank(min_attention)
    rows = [r for r in rows if attention_rank(r.attention_level) >= floor]
    return sorted(
        rows,
        key=lambda r: (-attention_rank(r.attention_level), -r.confidence),
    )


def render_proposals(rows: list[ProposalRecord], *, show_suppressed: bool = False) -> str:
    if not rows:
        return (
            "Nothing to raise.\n"
            "That is the most common correct answer for a proactive sweep -- "
            "but run with --show-suppressed to see what it decided not to say."
        )

    mark = {
        AttentionLevel.INTERRUPT: "!!!",
        AttentionLevel.NUDGE: " ! ",
        AttentionLevel.AMBIENT: "   ",
        AttentionLevel.SILENT: " . ",
    }
    lines = []
    for row in rows:
        flag = "[suppressed] " if row.suppressed else ""
        lines.append(
            f"{mark.get(row.attention_level, '   ')} {flag}{row.rationale}"
        )
        cited = row.evidence_event_ids
        # An absence finding has nothing to cite BY CONSTRUCTION, and saying so
        # is more honest than printing an empty citation list.
        trace = f"evidence: {cited}" if cited else "no evidence — this fired on absence"
        lines.append(
            f"      {trace}  ({row.attention_level}, confidence {row.confidence:.2f})"
        )
    if show_suppressed:
        lines.append("")
        lines.append("Suppressed items are shown so the blind spots stay visible.")
    return "\n".join(lines)
