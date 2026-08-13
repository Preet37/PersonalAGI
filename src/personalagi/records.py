"""The vocabulary every track binds against. Phase 0's actual output.

Four parallel sessions are about to build against these record types. If each
invents its own spelling of "dismissed" or its own idea of what counts as
evidence, the merge is a rewrite rather than a rebase. So the constants live
here, in one file, and the tracks import them instead of typing string
literals.

The two rules worth reading before anything else:

  1. ONLY EXTERNAL EVENTS MAY BE CITED. `citable_events()` is the single
     enforcement point. Everything the system wrote is invisible to evidence
     scoring, because self-reference is indistinguishable from corroboration
     from the inside.

  2. PERMISSION AND ATTENTION ARE INDEPENDENT. How reversible an action is has
     nothing to do with what it costs to be interrupted about it. Collapsing
     them is what makes an assistant people switch off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import Select
from sqlalchemy import select as sa_select

from personalagi.models import Event

# --- provenance --------------------------------------------------------


class Provenance(StrEnum):
    """Where a record came from. The fix for the self-citation bug."""

    #: Came from the world: a real message, a real invite, a real chat.
    EXTERNAL = "external"
    #: The system wrote it: a summary, a compacted profile, a proposal.
    GENERATED = "generated"


def citable_events(stmt: Select | None = None) -> Select:
    """Every query that gathers EVIDENCE must start here.

    Filtering at retrieval means remembering to filter at each of a dozen call
    sites, and the one that gets forgotten is the one that grounds a confident
    claim in the system's own output. This exists so "did we exclude generated
    records" has exactly one answer.
    """
    base = stmt if stmt is not None else sa_select(Event)
    return base.where(Event.provenance == Provenance.EXTERNAL)


# --- goals -------------------------------------------------------------


class GoalStatus(StrEnum):
    ACTIVE = "active"
    DONE = "done"
    ABANDONED = "abandoned"
    BLOCKED = "blocked"


class PersonRoleKind(StrEnum):
    """What someone is TO a goal, which is not what they are in general."""

    #: Argues for you when you are not in the room.
    ADVOCATE = "advocate"
    #: Writes the letter, gives the reference.
    RECOMMENDER = "recommender"
    #: Can say yes.
    DECISION_MAKER = "decision_maker"
    #: Controls access to someone who can say yes.
    GATEKEEPER = "gatekeeper"
    COLLABORATOR = "collaborator"
    SPONSOR = "sponsor"
    MENTOR = "mentor"
    CONTACT = "contact"


# --- commitments -------------------------------------------------------


class CommitmentStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    STALE = "stale"


class Direction(StrEnum):
    I_OWE = "i_owe"
    THEY_OWE = "they_owe"


# --- proposals ---------------------------------------------------------


class PermissionTier(StrEnum):
    """How hard is this to undo. Static, from the registry, never judged."""

    AUTO = "auto"
    APPROVE = "approve"
    NEVER = "never"


class AttentionLevel(StrEnum):
    """What does telling the owner cost. Independent of permission.

    Ordered so `max()` works and a rule can only ever make the system quieter
    or louder deliberately, never by accident.
    """

    #: Do it, log it, they find out later. Filing, updating a context file.
    SILENT = "silent"
    #: Appears in the morning brief. No interruption. The default.
    AMBIENT = "ambient"
    #: A notification, at a reasonable hour.
    NUDGE = "nudge"
    #: Breaks into the day. Reserved for a deadline inside 48 hours.
    INTERRUPT = "interrupt"


ATTENTION_ORDER: tuple[str, ...] = (
    AttentionLevel.SILENT,
    AttentionLevel.AMBIENT,
    AttentionLevel.NUDGE,
    AttentionLevel.INTERRUPT,
)


def attention_rank(level: str) -> int:
    try:
        return ATTENTION_ORDER.index(level)
    except ValueError:
        # An unknown level is treated as the quietest, not the loudest. A typo
        # must never buy an interruption.
        return 0


#: Nothing may be INTERRUPT unless a deadline lands inside this window.
INTERRUPT_DEADLINE_WINDOW = timedelta(hours=48)


def max_attention_for(deadline: datetime | None, *, now: datetime | None = None) -> str:
    """The loudest level a proposal about this deadline is allowed to use.

    A ceiling in code, not a suggestion in a prompt. The model proposes; this
    caps it. Three wrong interruptions and the owner stops reading the system
    entirely, so the expensive failure is being too loud, not too quiet.
    """
    if deadline is None:
        return AttentionLevel.NUDGE
    now = now or datetime.now(UTC).replace(tzinfo=None)
    if deadline - now <= INTERRUPT_DEADLINE_WINDOW:
        return AttentionLevel.INTERRUPT
    return AttentionLevel.NUDGE


def clamp_attention(requested: str, ceiling: str) -> str:
    """Never louder than the ceiling. Quieter is always allowed."""
    return requested if attention_rank(requested) <= attention_rank(ceiling) else ceiling


class Outcome(StrEnum):
    """What the owner did. The only training signal that costs nothing."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    #: The most valuable one: the diff between what it wrote and what was sent.
    EDITED = "edited"
    DISMISSED = "dismissed"
    #: Nothing happened for N days. Silence is a signal too, a weaker one.
    IGNORED = "ignored"
    REVERSED = "reversed"


#: Outcomes that mean "this was right". Used to weight retrieved examples.
POSITIVE_OUTCOMES = frozenset({Outcome.ACCEPTED, Outcome.EDITED})
NEGATIVE_OUTCOMES = frozenset({Outcome.DISMISSED, Outcome.IGNORED, Outcome.REVERSED})


# --- edges -------------------------------------------------------------


class NodeType(StrEnum):
    PERSON = "person"
    GOAL = "goal"
    COMMITMENT = "commitment"
    EVENT = "event"
    FACT = "fact"


class Relation(StrEnum):
    MENTIONS = "mentions"
    WORKS_AT = "works_at"
    OWES = "owes"
    ABOUT = "about"
    ATTENDED = "attended"
    RECOMMENDS = "recommends"
    BLOCKS = "blocks"
    INTRODUCED = "introduced"
    SERVES = "serves"


#: Default decay per relation. Overridable per edge.
#:
#: These are starting weights, not measured ones, and the honest thing is to
#: say so: they were chosen so that a direct obligation carries further than
#: a coincidental co-occurrence, then left to be tuned against real output.
DEFAULT_EDGE_WEIGHT: dict[str, float] = {
    Relation.OWES: 0.9,
    Relation.SERVES: 0.85,
    Relation.ABOUT: 0.8,
    Relation.RECOMMENDS: 0.75,
    Relation.WORKS_AT: 0.7,
    Relation.INTRODUCED: 0.7,
    Relation.BLOCKS: 0.7,
    Relation.ATTENDED: 0.5,
    Relation.MENTIONS: 0.4,
}


@dataclass(frozen=True)
class ActivationLimits:
    """Spreading activation, bounded.

    Without decay and a floor, three hops connects everything to everything and
    the output is "this lunch order relates to your entire life". The threshold
    is the difference between the feature working and it being noise.
    """

    start_energy: float = 1.0
    #: Below this, stop following the edge.
    threshold: float = 0.15
    max_depth: int = 3
    #: Hard cap on nodes visited, so a dense graph cannot hang a sweep.
    max_nodes: int = 200


DEFAULT_ACTIVATION = ActivationLimits()


# --- sweep budget ------------------------------------------------------


@dataclass
class CallBudget:
    """A hard ceiling on model calls, enforced in code.

    A system that thinks on its own is a system that spends on its own. The
    ceiling is not advisory: `spend()` raises when it is gone, so a runaway
    loop hits a wall instead of a bill.
    """

    limit: int
    spent: int = 0

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def can_spend(self, n: int = 1) -> bool:
        return self.remaining() >= n

    def spend(self, n: int = 1) -> None:
        if not self.can_spend(n):
            raise BudgetExhausted(
                f"call budget of {self.limit} exhausted; "
                f"raise SWEEP_CALL_BUDGET or narrow the sweep"
            )
        self.spent += n


class BudgetExhausted(RuntimeError):
    """The sweep hit its ceiling. Loud on purpose."""
