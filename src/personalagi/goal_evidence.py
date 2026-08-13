"""Stage 16: for each goal step, find anything anywhere that supports it.

This is the half of gap detection that makes the other half trustworthy. A step
with no evidence only means something if the system actually looked — otherwise
"nothing supports this" just means "nobody typed it in", and the alert is noise
the owner learns to ignore.

The search is FTS over event text, and it is deliberately NOT a model call.
Running an LLM over 4,346 events per step, per goal, every morning, is the
expensive design the whole two-trigger architecture exists to avoid. Full-text
search finds the candidates for free; a model only ever gets involved in
writing the proposal that results.

Precision here is less important than the direction of the error. A missed link
produces a false "nothing supports this", which costs the owner a glance. A
wrong link marks a step as handled when it is not, which is exactly the failure
the feature exists to prevent — so links are recorded with their method and
confidence, and only manual ones are treated as certain.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.models import Event, Goal, GoalStep, Participant, StepEvidence
from personalagi.records import CallBudget, citable_events
from personalagi.semantic import judge_evidence

log = logging.getLogger(__name__)

# Words that match everything and therefore mean nothing in a search.
STOPWORDS = frozenset(
    """
    a an the and or but if then to of for from with about into over after before
    is are was were be been being do does did doing have has had having i me my
    we our you your he she it they them this that these those on in at by as so
    not no yes get got send sent ask asked need needs want wants make made
    """.split()
)

MIN_TERM_LENGTH = 3
# The keyword pass is now RECALL ONLY. It nominates candidates; the model
# decides. So this threshold is deliberately LOOSE -- its job is to avoid
# missing things, not to be right. Precision is bought one layer up, in
# semantic.judge_evidence, where meaning actually lives.
#
# It was 0.6 when this score was the verdict. That number was doing work it
# could not do: at 0.6 a credit-card application still "supported" a Carnegie
# Mellon application, because both contain "submit" and "application".
MIN_SCORE = 0.34
# A step must contribute at least this many content words before any match is
# believable. "Follow up" cannot be evidenced by search.
MIN_TERMS_FOR_SEARCH = 2
# Candidates handed to the judge per step. Wider than the final link limit
# because the judge is expected to reject most of them.
JUDGE_CANDIDATES = 6
MAX_CANDIDATES = 40

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


@dataclass
class EvidenceMatch:
    event: Event
    score: float
    matched: list[str] = field(default_factory=list)


@dataclass
class LinkResult:
    steps_examined: int = 0
    links_added: int = 0
    steps_with_evidence: int = 0
    steps_without: int = 0
    judge_calls: int = 0
    judge_rejected: int = 0

    def summary(self) -> str:
        judged = (
            f"  judge: {self.judge_calls} call(s), rejected {self.judge_rejected} "
            f"keyword candidate(s)"
            if self.judge_calls or self.judge_rejected
            else ""
        )
        return (
            f"examined {self.steps_examined} step(s), added {self.links_added} link(s); "
            f"{self.steps_with_evidence} supported, {self.steps_without} with nothing"
            + (f"\n{judged}" if judged else "")
        )


def keywords(text: str) -> list[str]:
    """Content words from a step description.

    Stopwords are dropped because "ask Pratik for the letter" searched
    literally matches every message containing "the".
    """
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    return [w for w in words if len(w) >= MIN_TERM_LENGTH and w not in STOPWORDS]


def inverse_document_frequency(
    terms: list[str], events: list[Event]
) -> dict[str, float]:
    """How rare is each term in this corpus? Rare words carry the meaning.

    Word boundaries removed the "newsletter contains letter" class of error but
    did NOT make the linker correct. Audited afterwards, both surviving links
    were still false:

        "Submit the CMU application"  <-  "Thank you for starting an
                                          application!"   (a different one)
        "Submit the CMU application"  <-  "Earn more points in more ways"
                                          (a credit card)

    Because "submit" and "application" are everywhere in a mailbox, matching
    two common words looked like a two-thirds match. "cmu" -- the word that
    actually identifies the step -- was absent from both.

    IDF fixes exactly that: a term appearing in most events tells you nothing,
    a term appearing in three events is the whole signal. Free, no model call.
    """
    if not events:
        return {t: 1.0 for t in terms}

    total = len(events)
    counts = {t: 0 for t in terms}
    for event in events:
        present = set(_WORD_RE.findall(f"{event.title or ''} {event.text or ''}".lower()))
        for term in terms:
            if term in present:
                counts[term] += 1

    weights: dict[str, float] = {}
    for term, seen in counts.items():
        # SMOOTHED, and the +1 is load-bearing. Plain log(total/seen) is 0 when
        # a term appears in every document -- which on a two-event corpus is
        # every term, making every weight 0 and every score 0. Smoothing keeps
        # weights strictly positive so a small corpus degrades to plain
        # proportional matching instead of matching nothing at all.
        weights[term] = math.log((total + 1) / (seen + 1)) + 1.0
    return weights


def score_event(
    terms: list[str],
    event: Event,
    people: set[str],
    weights: dict[str, float] | None = None,
) -> EvidenceMatch:
    """Fraction of the step's content words present, with a bonus for people.

    Matching is on WORD BOUNDARIES, not substrings. The first version used
    `term in haystack` and produced this against the real corpus:

        [0.67] "Ask Pratik for a letter of recommendation"
                  <- "Welcome to Balenciaga"

    because "letter" is inside "newsletter", and marketing mail is full of
    newsletters and recommendations. Six of six links were false. A false link
    marks a step as handled when nothing has happened, which is precisely the
    failure gap detection exists to prevent -- so the error direction here is
    the dangerous one and boundaries are not optional.
    """
    if not terms:
        return EvidenceMatch(event=event, score=0.0)

    haystack = f"{event.title or ''}\n{event.text or ''}".lower()
    present = set(_WORD_RE.findall(haystack))
    hits = [t for t in terms if t in present]

    if weights:
        # Weighted by rarity: matching the one distinctive word beats matching
        # three words every mailbox contains.
        total = sum(weights.get(t, 0.0) for t in terms)
        matched = sum(weights.get(t, 0.0) for t in hits)
        score = (matched / total) if total > 0 else 0.0
    else:
        score = len(hits) / len(terms)

    # The bonus tops up a partial match; it can never carry a message that
    # shares no vocabulary at all. Without this floor, every message a goal's
    # person ever sent scores 0.25 and the threshold does nothing.
    if people and hits:
        score = min(1.0, score + 0.25)

    return EvidenceMatch(event=event, score=score, matched=hits)


def find_evidence(
    step_description: str,
    settings: Settings | None = None,
    *,
    person_slugs: set[str] | None = None,
    limit: int = 5,
    since: datetime | None = None,
) -> list[EvidenceMatch]:
    """Candidate events supporting one step. Only EXTERNAL events."""
    settings = settings or get_settings()
    init_db(settings)

    terms = keywords(step_description)
    if len(terms) < MIN_TERMS_FOR_SEARCH:
        # Too generic to search for. Returning nothing is honest; returning
        # whatever shares one word would be worse than saying "I don't know".
        return []

    with Session(get_engine(settings)) as session:
        # citable_events, not select(Event): a summary the system generated
        # about a step must never come back as proof the step is handled.
        stmt = citable_events(select(Event))
        if since is not None:
            stmt = stmt.where(Event.timestamp >= since)
        stmt = stmt.order_by(Event.timestamp_ms.desc()).limit(2000)
        events = list(session.execute(stmt).scalars())

        by_event_people: dict[int, set[str]] = {}
        if person_slugs:
            for participant in session.execute(
                select(Participant).where(Participant.person_slug.in_(person_slugs))
            ).scalars():
                by_event_people.setdefault(participant.event_id, set()).add(
                    participant.person_slug
                )

    candidates = events[: MAX_CANDIDATES * 20]
    weights = inverse_document_frequency(terms, events)
    scored = [
        score_event(terms, event, by_event_people.get(event.id, set()), weights)
        for event in candidates
    ]
    kept = [m for m in scored if m.score >= MIN_SCORE]
    kept.sort(key=lambda m: (-m.score, -m.event.timestamp_ms))
    return kept[:limit]


def link_evidence(
    settings: Settings | None = None,
    *,
    goal_slug: str | None = None,
    limit_per_step: int = 3,
    dry_run: bool = False,
    judge: bool = True,
    budget: CallBudget | None = None,
) -> LinkResult:
    """Find candidates by keyword, then have the model decide which support.

    `judge=False` falls back to the keyword score alone. That path exists for
    offline use and tests, and it is NOT the default, because a keyword score
    cannot tell a Carnegie Mellon application from a credit-card one.
    """
    settings = settings or get_settings()
    init_db(settings)
    result = LinkResult()
    now = datetime.now(UTC).replace(tzinfo=None)

    with Session(get_engine(settings)) as session:
        stmt = select(GoalStep, Goal).join(Goal, Goal.id == GoalStep.goal_id)
        if goal_slug:
            stmt = stmt.where(Goal.slug == goal_slug)
        pairs = list(session.execute(stmt))

        from personalagi.models import PersonRole

        people_by_goal: dict[int, set[str]] = {}
        for role in session.execute(select(PersonRole)).scalars():
            people_by_goal.setdefault(role.goal_id, set()).add(role.person_slug)

        existing: dict[int, set[int]] = {}
        for link in session.execute(select(StepEvidence)).scalars():
            existing.setdefault(link.step_id, set()).add(link.event_id)

    for step, goal in pairs:
        result.steps_examined += 1
        matches = find_evidence(
            step.description,
            settings,
            person_slugs=people_by_goal.get(goal.id, set()),
            limit=limit_per_step,
        )
        already = existing.get(step.id, set())
        fresh = [m for m in matches if m.event.id not in already]

        if already or fresh:
            result.steps_with_evidence += 1
        else:
            result.steps_without += 1

        if dry_run or not fresh:
            continue

        if judge:
            # The keyword pass proposed; the model disposes. Fails CLOSED --
            # an unjudged candidate is never linked, because a false link
            # marks the step handled and the alert goes silent.
            verdicts = judge_evidence(
                step.description,
                [m.event for m in fresh],
                settings,
                context=f'This step belongs to the goal "{goal.title}".',
                budget=budget,
            )
            result.judge_calls += verdicts.calls
            result.judge_rejected += len(verdicts.verdicts) - len(verdicts.supported)
            supported = {v.event_id: v for v in verdicts.supported}
            fresh = [m for m in fresh if m.event.id in supported]
            if not fresh:
                # Candidates existed but none survived judgement. That is a
                # real finding, not a failure -- the step still has no support.
                result.steps_with_evidence -= 1
                result.steps_without += 1
                continue

        with Session(get_engine(settings)) as session:
            for match in fresh:
                session.add(
                    StepEvidence(
                        step_id=step.id,
                        event_id=match.event.id,
                        # "search", never "manual": an automatically-found link
                        # is a candidate, not a confirmation, and the method is
                        # stored so a wrong one is diagnosable.
                        method="judged" if judge else "search",
                        confidence=round(match.score, 3),
                        linked_at=now,
                    )
                )
                result.links_added += 1
            row = session.get(GoalStep, step.id)
            if row is not None:
                newest = max(m.event.timestamp for m in fresh)
                if row.last_activity is None or newest > row.last_activity:
                    row.last_activity = newest
            session.commit()

    from personalagi.goals import refresh_activity

    refresh_activity(settings)
    log.info(result.summary())
    return result
