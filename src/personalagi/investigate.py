"""Stage 28: search, read, decide if that was enough, repeat.

Retrieval today is one pass — grab the top matches and stop. This is the
version that can decide it needs more and go get it, which is what "unlimited
power to query" actually means in practice.

The worked example: you ask about DJ. It searches mail and finds a meeting
invite. That implies a transcript, so it searches for one. The transcript
mentions John, so it pulls John's file. John's file mentions an Anthropic
contact, which links to a goal. Four hops, none of them planned in advance.

It is the same loop a coding agent runs over a repository — think, search,
read, decide, repeat — pointed at a life instead of a codebase.

TWO THINGS KEEP IT FROM RUNNING FOREVER
A hard iteration cap and a hard call budget, both enforced in code rather than
requested in a prompt. An agentic loop with no ceiling is an unbounded bill,
and the failure mode of "it kept going" is much worse here than "it stopped
early" — because it stops with a partial answer that says it is partial.

EVERY ANSWER CARRIES ITS CHAIN
The return value includes every step taken and every event read, so an answer
can be audited back to source. An investigation that cannot show its work is
indistinguishable from a confident guess, and this loop reads enough material
to make a confident guess sound extremely plausible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.context.people import slugify
from personalagi.db import get_engine, init_db
from personalagi.models import Commitment, Event, Goal, Participant
from personalagi.records import BudgetExhausted, CallBudget, citable_events

log = logging.getLogger(__name__)

_STOPWORDS = frozenset(
    """
    a an the and or but if then to of for from with about into over after before
    is are was were be been being do does did doing have has had having i me my
    we our you your he she it they them this that these those on in at by as so
    what when where who why how did tell know about
    """.split()
)


@dataclass
class Step:
    """One iteration. Recorded whether or not it found anything."""

    number: int
    action: str
    query: str
    found: int
    note: str = ""

    def render(self) -> str:
        outcome = f"{self.found} result(s)" if self.found else "nothing"
        extra = f" — {self.note}" if self.note else ""
        return f"  {self.number}. {self.action}({self.query!r}) -> {outcome}{extra}"


@dataclass
class Investigation:
    question: str
    steps: list[Step] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    commitments: list[str] = field(default_factory=list)
    #: Why it stopped. Always set, so "is this answer complete" is answerable.
    stopped_because: str = ""
    calls_spent: int = 0

    @property
    def exhausted(self) -> bool:
        return self.stopped_because in ("iteration cap", "call budget")

    def render(self) -> str:
        lines = [f"# Investigation — {self.question}", ""]

        if not self.event_ids and not self.people:
            lines += [
                "Found nothing. Not 'no answer' — no matching records in any",
                "connected source. The answer may live on a channel that is not",
                "ingested.",
                "",
                "## Steps taken",
                "",
                *[s.render() for s in self.steps],
            ]
            return "\n".join(lines)

        if self.people:
            lines += ["## People", "", *[f"  - {p}" for p in self.people], ""]
        if self.goals:
            lines += ["## Goals touched", "", *[f"  - {g}" for g in self.goals], ""]
        if self.commitments:
            lines += [
                "## Open commitments", "",
                *[f"  - {c}" for c in self.commitments], "",
            ]

        lines += [
            f"## Evidence ({len(self.event_ids)} external events)",
            "",
            "  " + ", ".join(str(e) for e in self.event_ids[:40]),
            "",
            "## Steps taken",
            "",
            *[s.render() for s in self.steps],
            "",
            f"Stopped: {self.stopped_because}.",
        ]
        if self.exhausted:
            # Say so plainly. A truncated investigation presented as a complete
            # one is the failure this whole design is trying to avoid.
            lines.append(
                "THIS ANSWER IS INCOMPLETE — the loop hit its ceiling, not the "
                "end of the evidence. Re-run with a higher --max-iterations."
            )
        return "\n".join(lines)


# Two characters, not three. "DJ" is a person, "AI" and "ML" are subjects, and
# a >2 filter silently dropped all of them -- so "what did we discuss with DJ",
# the worked example this loop was designed around, returned nothing at all.
# One-character tokens stay excluded; they are initials and noise.
MIN_TERM_LENGTH = 2


def _is_name_like(slug: str) -> bool:
    """A slug worth searching for. Digits-only slugs are unnamed handles."""
    return any(ch.isalpha() for ch in slug) and not slug.replace("-", "").isdigit()


def keywords(text: str) -> list[str]:
    import re

    words = re.findall(r"[\w']+", (text or "").lower())
    return [
        w for w in words if len(w) >= MIN_TERM_LENGTH and w not in _STOPWORDS
    ]


def _search_events(
    session: Session, terms: list[str], *, exclude: set[int], limit: int
) -> list[Event]:
    """Text search over EXTERNAL events only."""
    if not terms:
        return []
    clauses = [Event.text.ilike(f"%{t}%") for t in terms]
    clauses += [Event.title.ilike(f"%{t}%") for t in terms]
    stmt = (
        citable_events(select(Event))
        .where(or_(*clauses))
        .order_by(Event.timestamp_ms.desc())
        .limit(limit + len(exclude))
    )
    return [e for e in session.execute(stmt).scalars() if e.id not in exclude][:limit]


def investigate(
    question: str,
    settings: Settings | None = None,
    *,
    max_iterations: int | None = None,
    budget: CallBudget | None = None,
    per_step: int = 6,
) -> Investigation:
    """Follow the question outward until it stops finding new things.

    Termination, in priority order:
      1. nothing new was found this round  (the good exit)
      2. iteration cap
      3. call budget

    Only the first means the investigation is complete, and `stopped_because`
    records which one fired so the caller never has to guess.
    """
    settings = settings or get_settings()
    init_db(settings)
    max_iterations = max_iterations or settings.investigate_max_iterations
    budget = budget or CallBudget(limit=settings.investigate_call_budget)

    result = Investigation(question=question)
    seen_events: set[int] = set()
    seen_people: set[str] = set()
    # The owner is on most of his own correspondence. Listing him as a
    # discovered "person" is noise, and expanding on his name pulls in every
    # message he ever sent.
    owner_slugs = {slugify(a.split("@", 1)[0]) for a in settings.owner_address_set}
    owner_slugs |= {slugify(settings.owner_name)} if settings.owner_name else set()
    frontier = keywords(question)
    tried: set[str] = set()

    with Session(get_engine(settings)) as session:
        for iteration in range(1, max_iterations + 1):
            fresh_terms = [t for t in frontier if t not in tried]
            if not fresh_terms:
                result.stopped_because = "no new leads"
                break

            try:
                budget.spend()
                result.calls_spent += 1
            except BudgetExhausted:
                result.stopped_because = "call budget"
                break

            tried.update(fresh_terms)
            events = _search_events(
                session, fresh_terms, exclude=seen_events, limit=per_step
            )
            result.steps.append(
                Step(iteration, "search", " ".join(fresh_terms[:4]), len(events))
            )

            if not events:
                result.stopped_because = "no new leads"
                break

            new_people: set[str] = set()
            for event in events:
                seen_events.add(event.id)
                result.event_ids.append(event.id)
                for participant in session.execute(
                    select(Participant)
                    .where(Participant.event_id == event.id)
                    .where(Participant.person_slug != "")
                ).scalars():
                    slug = participant.person_slug
                    if slug in seen_people or slug in owner_slugs:
                        continue
                    new_people.add(slug)

            # Reading an event teaches you who was on it. Those names become
            # the next round's search terms -- this is the "that reminds me"
            # step, and it is why the loop finds things the question never
            # mentioned.
            for slug in sorted(new_people):
                seen_people.add(slug)
                # Unnamed handles are still traversed (their events are real
                # evidence) but they are not listed as PEOPLE, because
                # "14085046227" tells the reader nothing and crowds out the
                # names that do.
                if _is_name_like(slug):
                    result.people.append(slug)
                # Only NAMES become search terms. An unnamed handle slug is a
                # phone number, and feeding "14085046227" back in as a query
                # amplifies noise -- it matches unrelated events that happen to
                # contain the digits, and each of those drags in more handles.
                if _is_name_like(slug):
                    frontier.extend(
                        part for part in slug.split("-") if len(part) > 2
                    )
            if new_people:
                result.steps.append(
                    Step(
                        iteration,
                        "expand",
                        ", ".join(sorted(new_people)[:4]),
                        len(new_people),
                        note="new people became search terms",
                    )
                )
        else:
            result.stopped_because = "iteration cap"

        # Attach what the discovered people are actually involved in.
        if seen_people:
            for commitment in session.execute(
                select(Commitment)
                .where(Commitment.person_slug.in_(seen_people))
                .where(Commitment.status != "done")
            ).scalars():
                who = commitment.person_name or commitment.person_slug
                verb = "you owe" if commitment.direction == "i_owe" else "owed to you"
                result.commitments.append(f"{verb} {who}: {commitment.what}")

            from personalagi.models import PersonRole

            for goal in session.execute(
                select(Goal)
                .join(PersonRole, PersonRole.goal_id == Goal.id)
                .where(PersonRole.person_slug.in_(seen_people))
                .where(Goal.status == "active")
            ).scalars():
                if goal.title not in result.goals:
                    result.goals.append(goal.title)

    if not result.stopped_because:
        result.stopped_because = "no new leads"
    log.info(
        "investigate(%r): %d event(s), %d people, stopped on %s",
        question, len(result.event_ids), len(result.people), result.stopped_because,
    )
    return result
