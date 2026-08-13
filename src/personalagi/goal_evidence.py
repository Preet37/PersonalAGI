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
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.models import Event, Goal, GoalStep, Participant, StepEvidence
from personalagi.records import citable_events

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
# Below this, the match is one incidental word and not evidence of anything.
# Raised from 0.34 after the real-data run: a third of a three-word step is one
# word, and one shared word between a marketing email and a task is a
# coincidence, not support.
MIN_SCORE = 0.6
# A step must contribute at least this many content words before any match is
# believable. "Follow up" cannot be evidenced by search.
MIN_TERMS_FOR_SEARCH = 2
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

    def summary(self) -> str:
        return (
            f"examined {self.steps_examined} step(s), added {self.links_added} link(s); "
            f"{self.steps_with_evidence} supported, {self.steps_without} with nothing"
        )


def keywords(text: str) -> list[str]:
    """Content words from a step description.

    Stopwords are dropped because "ask Pratik for the letter" searched
    literally matches every message containing "the".
    """
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    return [w for w in words if len(w) >= MIN_TERM_LENGTH and w not in STOPWORDS]


def score_event(terms: list[str], event: Event, people: set[str]) -> EvidenceMatch:
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

    scored = [
        score_event(terms, event, by_event_people.get(event.id, set()))
        for event in events[:MAX_CANDIDATES * 20]
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
) -> LinkResult:
    """Search for supporting events for every open step and record the links."""
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

        with Session(get_engine(settings)) as session:
            for match in fresh:
                session.add(
                    StepEvidence(
                        step_id=step.id,
                        event_id=match.event.id,
                        # "search", never "manual": an automatically-found link
                        # is a candidate, not a confirmation, and the method is
                        # stored so a wrong one is diagnosable.
                        method="search",
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
