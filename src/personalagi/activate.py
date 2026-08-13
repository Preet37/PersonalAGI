"""Spreading activation: what does this thing wake up?

The reframe this implements: the system currently asks "is this email
important?", which is the wrong question. A Deepgram job alert is objectively
bulk mail. It mattered because it reminded the owner that someone he knows
works there, that he owes them a resume, and that the same company might
sponsor his hackathon. **None of that was in the email.**

So the right question is "what does this connect to", and the output is not a
label on the message — it is the list of things the message lit up.

That is spreading activation: touch one node, energy spreads to its neighbours,
whatever glows brightest surfaces. It is how memory retrieval is modelled in
cognitive science, and it is a different architecture from classification.

THE HARD PART IS STOPPING
Spreading activation over-fires. Three hops and everything connects to
everything, and the system tells you a lunch order relates to your entire life.
Three things prevent that, and they are the whole difference between the
feature working and it being noise:

  - energy decays by the edge weight at every hop
  - traversal stops below a threshold
  - depth and total nodes are capped

Nothing here calls a model. Activation is a graph walk over indexed rows; it
produces the CONTEXT that a single downstream call then reasons over.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.models import Commitment, Edge, Event, Goal, Participant, PersonRole
from personalagi.records import (
    DEFAULT_ACTIVATION,
    DEFAULT_EDGE_WEIGHT,
    ActivationLimits,
    GoalStatus,
    NodeType,
    Relation,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Node:
    type: str
    id: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.type}:{self.id}"


@dataclass
class Activated:
    node: Node
    energy: float
    depth: int
    label: str = ""
    #: How we got here, so a surprising result is explainable rather than magic.
    path: list[str] = field(default_factory=list)

    def why(self) -> str:
        return " -> ".join(self.path) if self.path else "(seed)"


# --- building the graph ------------------------------------------------


@dataclass
class EdgeResult:
    created: int = 0
    kinds: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        detail = "  ".join(f"{k}={v}" for k, v in sorted(self.kinds.items()))
        return f"{self.created} edge(s): {detail or 'none'}"


def build_edges(settings: Settings | None = None) -> EdgeResult:
    """Derive typed edges from records that already exist.

    Nothing is inferred by a model. Every edge here is a fact already in the
    database expressed as a link — a participant on an event, a commitment
    naming a person, a role attaching a person to a goal. That keeps every edge
    citable back to the record that justified it.
    """
    settings = settings or get_settings()
    init_db(settings)
    result = EdgeResult()
    now = datetime.now(UTC).replace(tzinfo=None)

    with Session(get_engine(settings)) as session:
        existing = {
            (e.src_type, e.src_id, e.dst_type, e.dst_id, e.relation)
            for e in session.execute(select(Edge)).scalars()
        }

        def add(src: Node, dst: Node, relation: str, event_id: int | None = None):
            key = (src.type, src.id, dst.type, dst.id, relation)
            if key in existing:
                return
            existing.add(key)
            session.add(
                Edge(
                    src_type=src.type, src_id=src.id,
                    dst_type=dst.type, dst_id=dst.id,
                    relation=relation,
                    weight=DEFAULT_EDGE_WEIGHT.get(relation, 0.4),
                    source_event_id=event_id, created_at=now,
                )
            )
            result.created += 1
            result.kinds[relation] = result.kinds.get(relation, 0) + 1

        # person <-> event, for every real participant.
        for participant in session.execute(
            select(Participant).where(Participant.person_slug != "")
        ).scalars():
            person = Node(NodeType.PERSON, participant.person_slug)
            event = Node(NodeType.EVENT, str(participant.event_id))
            add(person, event, Relation.MENTIONS, participant.event_id)
            add(event, person, Relation.MENTIONS, participant.event_id)

        # commitment <-> person, and commitment -> its source event.
        for commitment in session.execute(select(Commitment)).scalars():
            node = Node(NodeType.COMMITMENT, str(commitment.id))
            if commitment.person_slug:
                person = Node(NodeType.PERSON, commitment.person_slug)
                add(node, person, Relation.OWES, commitment.event_id)
                add(person, node, Relation.OWES, commitment.event_id)
            add(
                node, Node(NodeType.EVENT, str(commitment.event_id)),
                Relation.ABOUT, commitment.event_id,
            )
            if commitment.goal_id:
                add(node, Node(NodeType.GOAL, str(commitment.goal_id)), Relation.SERVES)

        # person <-> goal, carrying the role's meaning in the relation.
        for role in session.execute(select(PersonRole)).scalars():
            person = Node(NodeType.PERSON, role.person_slug)
            goal = Node(NodeType.GOAL, str(role.goal_id))
            relation = (
                Relation.RECOMMENDS if role.role == "recommender" else Relation.SERVES
            )
            add(person, goal, relation)
            add(goal, person, relation)

        session.commit()

    log.info(result.summary())
    return result


# --- traversal ---------------------------------------------------------


def activate(
    seeds: list[Node],
    settings: Settings | None = None,
    *,
    limits: ActivationLimits = DEFAULT_ACTIVATION,
) -> list[Activated]:
    """Walk outward from the seeds, decaying at every hop.

    Breadth-first so the closest, strongest connections are found first and the
    node cap truncates the periphery rather than the core.
    """
    settings = settings or get_settings()
    init_db(settings)
    if not seeds:
        return []

    with Session(get_engine(settings)) as session:
        outgoing: dict[tuple[str, str], list[Edge]] = {}
        for edge in session.execute(select(Edge)).scalars():
            outgoing.setdefault((edge.src_type, edge.src_id), []).append(edge)

    best: dict[Node, Activated] = {}
    queue: deque[Activated] = deque()

    for seed in seeds:
        entry = Activated(node=seed, energy=limits.start_energy, depth=0)
        best[seed] = entry
        queue.append(entry)

    while queue and len(best) < limits.max_nodes:
        current = queue.popleft()
        if current.depth >= limits.max_depth:
            continue

        for edge in outgoing.get((current.node.type, current.node.id), []):
            energy = current.energy * edge.weight
            if energy < limits.threshold:
                # The floor. Without it, three hops connects everything to
                # everything and the output is noise.
                continue

            node = Node(edge.dst_type, edge.dst_id)
            previous = best.get(node)
            if previous is not None and previous.energy >= energy:
                continue

            entry = Activated(
                node=node,
                energy=energy,
                depth=current.depth + 1,
                path=[*current.path, f"{edge.relation}->{node}"],
            )
            best[node] = entry
            queue.append(entry)

    ranked = [a for a in best.values() if a.depth > 0]
    ranked.sort(key=lambda a: -a.energy)
    return ranked[: limits.max_nodes]


def seeds_for_event(event_id: int, settings: Settings | None = None) -> list[Node]:
    """The nodes an arriving event touches: itself and its real participants."""
    settings = settings or get_settings()
    init_db(settings)
    seeds = [Node(NodeType.EVENT, str(event_id))]
    with Session(get_engine(settings)) as session:
        for participant in session.execute(
            select(Participant)
            .where(Participant.event_id == event_id)
            .where(Participant.person_slug != "")
        ).scalars():
            seeds.append(Node(NodeType.PERSON, participant.person_slug))
    return seeds


def label_nodes(
    activated: list[Activated], settings: Settings | None = None
) -> list[Activated]:
    """Attach human-readable labels so the output is readable, not row ids."""
    settings = settings or get_settings()
    by_type: dict[str, list[str]] = {}
    for entry in activated:
        by_type.setdefault(entry.node.type, []).append(entry.node.id)

    labels: dict[Node, str] = {}
    with Session(get_engine(settings)) as session:
        for raw in by_type.get(NodeType.GOAL, []):
            goal = session.get(Goal, int(raw)) if raw.isdigit() else None
            if goal:
                labels[Node(NodeType.GOAL, raw)] = f"goal: {goal.title}"
        for raw in by_type.get(NodeType.COMMITMENT, []):
            row = session.get(Commitment, int(raw)) if raw.isdigit() else None
            if row:
                who = row.person_name or row.person_email or row.person_slug
                verb = "you owe" if row.direction == "i_owe" else "owed to you"
                labels[Node(NodeType.COMMITMENT, raw)] = f"{verb} {who}: {row.what}"
        for raw in by_type.get(NodeType.EVENT, []):
            row = session.get(Event, int(raw)) if raw.isdigit() else None
            if row:
                text = (row.title or row.text or "").strip().splitlines()
                labels[Node(NodeType.EVENT, raw)] = (
                    f"{row.source}: {text[0][:70]}" if text else row.source
                )

    for entry in activated:
        entry.label = labels.get(entry.node, str(entry.node))
    return activated


def render_activation(activated: list[Activated], *, limit: int = 12) -> str:
    """What this woke up, brightest first, each with how it was reached."""
    if not activated:
        return "Nothing connected. This is genuinely isolated."
    lines = []
    for entry in activated[:limit]:
        lines.append(f"  {entry.energy:.2f}  {entry.label}")
        lines.append(f"        via {entry.why()}")
    return "\n".join(lines)


def open_loops(
    activated: list[Activated], settings: Settings | None = None
) -> list[str]:
    """The subset that is actually actionable: goals and open commitments.

    A brightly-lit event is interesting; an open commitment attached to it is
    something to DO, which is the difference between a connection and a nudge.
    """
    settings = settings or get_settings()
    loops: list[str] = []
    with Session(get_engine(settings)) as session:
        for entry in activated:
            if entry.node.type == NodeType.COMMITMENT and entry.node.id.isdigit():
                row = session.get(Commitment, int(entry.node.id))
                if row and row.status != "done":
                    who = row.person_name or row.person_email
                    loops.append(f"{row.direction}: {row.what} ({who})")
            elif entry.node.type == NodeType.GOAL and entry.node.id.isdigit():
                goal = session.get(Goal, int(entry.node.id))
                if goal and goal.status == GoalStatus.ACTIVE:
                    loops.append(f"goal: {goal.title}")
    return loops
