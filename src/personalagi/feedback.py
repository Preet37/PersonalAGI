"""Stages 26 & 27: the loop that makes the system get better by being used.

Systems like this do not improve by running. They improve if you build three
things — capture what the owner did, store it, and change behaviour because of
it. Skip any one and it plateaus at day one forever however much data flows
through.

NO TRAINING HAPPENS HERE
There is no fine-tuning and no weights. Behaviour changes by showing the model
its own track record on similar cases: "the last four times you flagged a
LinkedIn connection request as urgent, he dismissed it". That works immediately,
needs no labelled corpus, and is inspectable — you can read exactly which past
cases moved a decision, which you cannot do with a gradient.

THE FIVE OUTCOMES ARE NOT EQUALLY INFORMATIVE
  accepted   strong positive
  edited     the MOST valuable. He wanted it, and the diff between what the
             system wrote and what he sent is a direct, specific lesson.
  dismissed  strong negative, and deliberate
  ignored    weak negative. Silence is ambiguous — he may simply not have
             looked — so it is inferred by time, never asserted, and weighted
             lower than an explicit dismissal.
  reversed   strongest negative: it acted, and he undid it.

TWO FAILURE MODES THIS HAS TO AVOID
Overfitting to recent feedback, so old corrections keep counting — a lesson
from March is not worth less than one from today unless the owner changed.
And early mistakes locking in: if the system stops surfacing a class of thing,
he never sees it, so he never corrects it, so the blind spot becomes permanent
and invisible. That is what `--show-suppressed` and the exploration sample
below exist for.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.models import ProposalRecord
from personalagi.records import (
    POSITIVE_OUTCOMES,
    Outcome,
)

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)

# Outcomes carry different evidential weight. `edited` outranks `accepted`
# because it proves he wanted the thing AND shows exactly how it was wrong.
OUTCOME_WEIGHT: dict[str, float] = {
    Outcome.EDITED: 1.0,
    Outcome.ACCEPTED: 0.9,
    Outcome.REVERSED: -1.0,
    Outcome.DISMISSED: -0.8,
    # Silence is ambiguous. He may not have looked. Weighted low on purpose.
    Outcome.IGNORED: -0.35,
}


class FeedbackError(RuntimeError):
    pass


@dataclass
class Example:
    """A past proposal and what happened to it. What goes in the prompt."""

    proposal_id: str
    action_name: str
    rationale: str
    outcome: str
    similarity: float
    note: str = ""

    @property
    def was_right(self) -> bool:
        return self.outcome in POSITIVE_OUTCOMES

    def render(self) -> str:
        verdict = {
            Outcome.ACCEPTED: "ACCEPTED",
            Outcome.EDITED: "ACCEPTED BUT EDITED",
            Outcome.DISMISSED: "DISMISSED",
            Outcome.IGNORED: "IGNORED (no response)",
            Outcome.REVERSED: "UNDONE AFTER THE FACT",
        }.get(self.outcome, self.outcome)
        line = f'- "{self.rationale}" -> {verdict}'
        if self.note:
            line += f'\n  what he actually sent: "{self.note}"'
        return line


@dataclass
class FeedbackStats:
    total: int = 0
    by_outcome: dict[str, int] = field(default_factory=dict)

    @property
    def pending(self) -> int:
        return self.by_outcome.get(Outcome.PENDING, 0)

    @property
    def resolved(self) -> int:
        return self.total - self.pending

    @property
    def acceptance_rate(self) -> float | None:
        """Of proposals the owner actually judged, how many were right?

        None when nothing has been judged — reporting 0% for "no data yet"
        would be a lie in the direction that makes the system look worse than
        it is, which is still a lie.
        """
        judged = sum(
            count for key, count in self.by_outcome.items() if key != Outcome.PENDING
        )
        if not judged:
            return None
        good = sum(self.by_outcome.get(key, 0) for key in POSITIVE_OUTCOMES)
        return good / judged

    def summary(self) -> str:
        parts = "  ".join(f"{k}={v}" for k, v in sorted(self.by_outcome.items()))
        rate = (
            f"{self.acceptance_rate:.0%}"
            if self.acceptance_rate is not None
            else "n/a (nothing judged yet)"
        )
        return f"{self.total} proposal(s): {parts}\n  acceptance: {rate}"


# --- stage 26: the ledger ----------------------------------------------


def record_outcome(
    proposal_id: str,
    outcome: str,
    settings: Settings | None = None,
    *,
    note: str = "",
    now: datetime | None = None,
) -> ProposalRecord:
    """Record what the owner did with a proposal."""
    settings = settings or get_settings()
    init_db(settings)
    now = now or datetime.now(UTC).replace(tzinfo=None)

    valid = {o.value for o in Outcome}
    if outcome not in valid:
        raise FeedbackError(
            f"'{outcome}' is not an outcome. One of: {', '.join(sorted(valid))}"
        )

    with Session(get_engine(settings)) as session:
        row = session.execute(
            select(ProposalRecord).where(ProposalRecord.proposal_id == proposal_id)
        ).scalar_one_or_none()
        if row is None:
            # Try a short prefix, because nobody types a 32-char hex id.
            matches = [
                r
                for r in session.execute(select(ProposalRecord)).scalars()
                if r.proposal_id.startswith(proposal_id)
            ]
            if len(matches) > 1:
                raise FeedbackError(
                    f"'{proposal_id}' matches {len(matches)} proposals; use more characters"
                )
            if not matches:
                raise FeedbackError(f"no proposal matching '{proposal_id}'")
            row = matches[0]

        row.outcome = outcome
        row.outcome_at = now
        if note:
            row.outcome_note = note
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def mark_ignored(
    settings: Settings | None = None,
    *,
    after_days: int | None = None,
    now: datetime | None = None,
) -> int:
    """Age out proposals nobody ever responded to.

    Inferred from time, never asserted, and weighted low downstream. Silence is
    genuinely ambiguous — he may not have looked at the brief that day — so
    treating it as a firm rejection would teach the system the wrong lesson
    from the owner simply being busy.

    Only SURFACED proposals age out. A suppressed one was never shown, so
    calling it "ignored" would punish the system for the owner not seeing
    something it deliberately hid.
    """
    settings = settings or get_settings()
    init_db(settings)
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(days=after_days or settings.feedback_ignore_days)
    changed = 0

    with Session(get_engine(settings)) as session:
        rows = session.execute(
            select(ProposalRecord)
            .where(ProposalRecord.outcome == Outcome.PENDING)
            .where(ProposalRecord.suppressed.is_(False))
            .where(ProposalRecord.created_at < cutoff)
        ).scalars()
        for row in rows:
            row.outcome = Outcome.IGNORED
            row.outcome_at = now
            changed += 1
        session.commit()

    if changed:
        log.info("aged %d unanswered proposal(s) to ignored", changed)
    return changed


def stats(settings: Settings | None = None) -> FeedbackStats:
    settings = settings or get_settings()
    init_db(settings)
    result = FeedbackStats()
    with Session(get_engine(settings)) as session:
        for row in session.execute(select(ProposalRecord)).scalars():
            result.total += 1
            result.by_outcome[row.outcome] = result.by_outcome.get(row.outcome, 0) + 1
    return result


def history(
    settings: Settings | None = None, *, limit: int = 20, outcome: str | None = None
) -> list[ProposalRecord]:
    settings = settings or get_settings()
    init_db(settings)
    with Session(get_engine(settings)) as session:
        stmt = select(ProposalRecord).order_by(ProposalRecord.created_at.desc())
        if outcome:
            stmt = stmt.where(ProposalRecord.outcome == outcome)
        return list(session.execute(stmt.limit(limit)).scalars())


# --- stage 27: outcome-conditioned prompting ---------------------------


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if len(w) > 2}


def similarity(a: str, b: str) -> float:
    """Jaccard over content words.

    Lexical on purpose. Embedding every past proposal to retrieve examples for
    a proposal would cost more than the proposal, and this runs on every one.
    """
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def similar_proposals(
    action_name: str,
    rationale: str,
    settings: Settings | None = None,
    *,
    limit: int = 5,
    now: datetime | None = None,
) -> list[Example]:
    """The k most similar judged proposals, with what happened to them.

    Only JUDGED proposals. A pending one teaches nothing, and including it
    would dilute the examples with cases the owner has not ruled on.

    Recency decays gently and never to zero. A correction from March is still a
    correction; the risk of overfitting to last week is exactly the failure mode
    a steep decay would create.
    """
    settings = settings or get_settings()
    init_db(settings)
    now = now or datetime.now(UTC).replace(tzinfo=None)

    with Session(get_engine(settings)) as session:
        rows = list(
            session.execute(
                select(ProposalRecord).where(
                    ProposalRecord.outcome != Outcome.PENDING
                )
            ).scalars()
        )

    scored: list[tuple[float, Example]] = []
    for row in rows:
        text_score = similarity(rationale, row.rationale)
        if row.action_name == action_name:
            # Same action type is a strong signal even when wording differs.
            text_score = min(1.0, text_score + 0.25)
        if text_score <= 0:
            continue

        age_days = max(0.0, (now - row.created_at).total_seconds() / 86400)
        # Half-life decay with a floor: an old lesson is worth less than a new
        # one, never nothing.
        recency = max(
            settings.feedback_recency_floor,
            math.exp(-age_days / max(settings.feedback_half_life_days, 1)),
        )
        scored.append(
            (
                text_score * recency,
                Example(
                    proposal_id=row.proposal_id,
                    action_name=row.action_name,
                    rationale=row.rationale,
                    outcome=row.outcome,
                    similarity=round(text_score, 3),
                    note=row.outcome_note,
                ),
            )
        )

    scored.sort(key=lambda pair: -pair[0])
    return [example for _, example in scored[:limit]]


def render_examples(examples: list[Example]) -> str:
    """The block that goes into a prompt. Empty string when there is nothing.

    Returning "" rather than "no examples available" matters: a prompt that
    says there is no track record invites the model to comment on that, and
    the absence of history should change nothing about the current judgement.
    """
    if not examples:
        return ""
    lines = [
        "PAST PROPOSALS LIKE THIS ONE, AND WHAT THE OWNER DID:",
        "",
        *[e.render() for e in examples],
        "",
        "Weight these. A pattern of dismissals means this kind of proposal is "
        "not wanted; an edit shows what was wrong with the wording.",
    ]
    return "\n".join(lines)


def guidance(
    action_name: str,
    rationale: str,
    settings: Settings | None = None,
    *,
    limit: int = 5,
) -> tuple[str, list[str]]:
    """The prompt block plus the ids of the examples used.

    The ids are returned so the effect is MEASURABLE. Without recording which
    examples informed a proposal, "the feedback loop is working" is an
    assertion nobody can check.
    """
    examples = similar_proposals(action_name, rationale, settings, limit=limit)
    return render_examples(examples), [e.proposal_id for e in examples]


def outcome_bias(
    action_name: str,
    rationale: str,
    settings: Settings | None = None,
    *,
    limit: int = 5,
) -> float:
    """Net signal from similar past cases, in [-1, 1].

    A cheap confidence adjustment that needs no model call at all: if the last
    several proposals of this shape were all dismissed, the next one starts
    lower. Weighted by outcome severity and similarity.
    """
    examples = similar_proposals(action_name, rationale, settings, limit=limit)
    if not examples:
        return 0.0
    total = sum(e.similarity for e in examples)
    if total <= 0:
        return 0.0
    signal = sum(
        OUTCOME_WEIGHT.get(e.outcome, 0.0) * e.similarity for e in examples
    )
    return max(-1.0, min(1.0, signal / total))


# --- keeping blind spots visible ---------------------------------------


def exploration_sample(
    settings: Settings | None = None, *, limit: int = 3
) -> list[ProposalRecord]:
    """A few suppressed proposals, shown deliberately.

    If the system stops surfacing a class of thing, the owner never sees it, so
    never corrects it, so the blind spot becomes permanent AND invisible. This
    is the same reason recommender systems need exploration: without it, early
    mistakes are self-reinforcing and unfalsifiable.

    Highest-confidence suppressed items first — the near-misses are where a
    wrong threshold shows up first.
    """
    settings = settings or get_settings()
    init_db(settings)
    with Session(get_engine(settings)) as session:
        rows = list(
            session.execute(
                select(ProposalRecord)
                .where(ProposalRecord.suppressed.is_(True))
                .where(ProposalRecord.outcome == Outcome.PENDING)
                .order_by(ProposalRecord.confidence.desc())
                .limit(limit)
            ).scalars()
        )
    return rows


def render_history(rows: list[ProposalRecord]) -> str:
    if not rows:
        return "No proposals recorded yet. Run `personalagi sweep` first."
    lines = []
    for row in rows:
        flag = " [suppressed]" if row.suppressed else ""
        lines.append(
            f"{row.proposal_id[:8]}  {row.outcome:<9}{flag}  {row.rationale[:78]}"
        )
        if row.outcome_note:
            lines.append(f'          edited to: "{row.outcome_note[:70]}"')
    lines.append("")
    lines.append("Record one with:  python -m personalagi feedback <id> <outcome>")
    lines.append(f"Outcomes: {', '.join(sorted(o.value for o in Outcome if o != Outcome.PENDING))}")
    return "\n".join(lines)


def args_of(row: ProposalRecord) -> dict:
    try:
        loaded = json.loads(row.args_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
