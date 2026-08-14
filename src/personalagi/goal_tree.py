"""One nested goals file, because goals are not flat and are not a form.

The first version made you type a goal per CLI invocation and gave each its own
file. That is wrong twice over.

Goals nest. "Land a full-time role" is the goal; Cisco, NVIDIA, McCoy, Gatlin
and Deepgram are five paths to it, and losing one is not losing the goal.
"Get into a masters program" is the goal; CMU matters only because it has the
earliest deadline among the schools needing letters, which is a fact about
scheduling, not about wanting CMU more than Stanford.

And a goal is not a form to fill in. `context/goals/goals.md` is one file you
edit like notes. The system reads it, links steps to evidence, and writes what
it infers into `## Proposed` for you to confirm or delete — both of which are
signal.

WHY HIERARCHY CHANGES BEHAVIOUR AND IS NOT DECORATION

  - A sub-goal with no deadline inherits its parent's, so "Follow up with
    Isaac" is not treated as timeless just because nobody wrote a date on it.
  - A parent's urgency is its EARLIEST child deadline. "Get into a masters
    program" is urgent in August because CMU is, even though the parent names
    no date at all.
  - Activity anywhere under a goal counts as activity on the goal. The old
    flat model reported "Land HackDev sponsors has had no activity" while a
    sponsor conversation was live, because the activity was on a child.

`history:` exists because Loomin caused GTC, which caused Jensen, which caused
the viral post, which is how Pratik and Ryan appeared. That chain is why "follow
up with Ryan" should carry where Ryan came from — a fact about consequence, not
a fact about hierarchy.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.context.people import slugify
from personalagi.db import get_engine, init_db
from personalagi.models import Goal, GoalStep, PersonRole
from personalagi.records import GoalStatus, PersonRoleKind

log = logging.getLogger(__name__)

GOALS_FILE = "goals.md"
PROPOSED_HEADING = "## Proposed"

_HEADING = re.compile(r"^(?P<hashes>#{2,4})\s+(?P<title>.+?)\s*$")
_STEP = re.compile(r"^-\s*\[(?P<done>[ xX])\]\s*(?P<text>.+?)\s*$")
_FIELD = re.compile(r"^(?P<key>why|deadline|people|status|source|history)\s*:\s*(?P<value>.*)$")
# A continuation line under a field: indented, not a heading, not a step.
_CONT = re.compile(r"^\s{2,}(?P<text>\S.*)$")


@dataclass
class TreeStep:
    description: str
    done: bool = False


@dataclass
class TreeGoal:
    slug: str
    title: str
    depth: int
    why: str = ""
    deadline: date | None = None
    status: str = GoalStatus.ACTIVE
    people: dict[str, str] = field(default_factory=dict)
    source: str = ""
    history: str = ""
    steps: list[TreeStep] = field(default_factory=list)
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    proposed: bool = False


def _parse_deadline(raw: str) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        # A deadline we cannot read is dropped rather than guessed. A wrong
        # date drives urgency, and urgency drives interruption.
        log.warning("unreadable deadline %r in goals.md, ignoring", text)
        return None


def _parse_people(raw: str) -> dict[str, str]:
    """`people: dj-sampath, arjun-sambamoorthy` or `name:role` pairs."""
    people: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        slug, _, role = chunk.partition(":")
        people[slugify(slug.strip())] = role.strip() or PersonRoleKind.CONTACT
    return people


def parse_tree(text: str) -> list[TreeGoal]:
    """Parse the nested file. `##` is a goal, `###`/`####` are sub-goals."""
    goals: list[TreeGoal] = []
    by_depth: dict[int, str] = {}
    current: TreeGoal | None = None
    last_key: str | None = None
    in_proposed = False
    seen: set[str] = set()

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("---"):
            continue

        heading = _HEADING.match(stripped)
        if heading:
            title = heading.group("title").strip()
            depth = len(heading.group("hashes"))
            if title.lower() == "proposed":
                # Everything below this line was written by the system and is
                # waiting on a human. Never treated as confirmed.
                in_proposed = True
                current = None
                continue

            slug = slugify(title)
            # Two sub-goals can share a name under different parents
            # ("Remaining schools"), so collisions are qualified by parent.
            parent = by_depth.get(depth - 1)
            if slug in seen and parent:
                slug = f"{parent}-{slug}"
            seen.add(slug)

            current = TreeGoal(
                slug=slug, title=title, depth=depth, parent=parent,
                proposed=in_proposed,
            )
            by_depth[depth] = slug
            # A deeper heading later cannot be a child of a stale branch.
            for deeper in [d for d in by_depth if d > depth]:
                by_depth.pop(deeper, None)
            if parent:
                for goal in goals:
                    if goal.slug == parent:
                        goal.children.append(slug)
            goals.append(current)
            last_key = None
            continue

        if current is None:
            continue

        step = _STEP.match(stripped)
        if step:
            current.steps.append(
                TreeStep(
                    description=step.group("text").strip(),
                    done=step.group("done").lower() == "x",
                )
            )
            last_key = None
            continue

        field_match = _FIELD.match(stripped)
        if field_match:
            key, value = field_match.group("key"), field_match.group("value").strip()
            if key == "deadline":
                current.deadline = _parse_deadline(value)
            elif key == "people":
                current.people = _parse_people(value)
            elif key == "status":
                current.status = value or GoalStatus.ACTIVE
            elif key == "why":
                current.why = value
            elif key == "source":
                current.source = value
            elif key == "history":
                current.history = value
            last_key = key
            continue

        # A wrapped continuation of the previous field. Without this, the
        # second line of every multi-line `why:` is silently discarded.
        cont = _CONT.match(line)
        if cont and last_key in {"why", "history", "source"}:
            extra = cont.group("text")
            if last_key == "why":
                current.why = f"{current.why} {extra}".strip()
            elif last_key == "history":
                current.history = f"{current.history} {extra}".strip()
            else:
                current.source = f"{current.source} {extra}".strip()

    return goals


def inherit(goals: list[TreeGoal]) -> list[TreeGoal]:
    """Push deadlines down, pull urgency up.

    Down: a sub-goal with no date of its own answers to its parent's, so
    "Follow up with Isaac" is not timeless merely because nobody wrote a date.

    Up: a parent's effective deadline is its EARLIEST child's. "Get into a
    masters program" names no date and is urgent in August anyway, because CMU
    is. Without this a parent goal can never become urgent at all.
    """
    by_slug = {g.slug: g for g in goals}

    # UP FIRST, then down. The other order looks natural and is wrong: a child
    # inherits from a parent that has not yet acquired its own effective date,
    # so a sibling of the deadline-bearing child inherits nothing. McCoy came
    # out undated even though the branch it sits in is due in eleven days.
    for goal in sorted(goals, key=lambda g: -g.depth):
        child_dates = [
            by_slug[c].deadline
            for c in goal.children
            if by_slug.get(c) and by_slug[c].deadline
        ]
        if child_dates:
            earliest = min(child_dates)
            if goal.deadline is None or earliest < goal.deadline:
                goal.deadline = earliest

    for goal in sorted(goals, key=lambda g: g.depth):
        if goal.deadline is None and goal.parent:
            parent = by_slug.get(goal.parent)
            if parent is not None:
                goal.deadline = parent.deadline

    return goals


def load_tree(settings: Settings | None = None) -> list[TreeGoal]:
    settings = settings or get_settings()
    path = Path(settings.context_dir) / "goals" / GOALS_FILE
    if not path.exists():
        return []
    return inherit(parse_tree(path.read_text(encoding="utf-8")))


def sync_tree(settings: Settings | None = None) -> dict[str, int]:
    """Rebuild the goal index from the nested file. Files stay untouched."""
    settings = settings or get_settings()
    init_db(settings)
    goals = load_tree(settings)
    if not goals:
        return {"goals": 0, "steps": 0, "people": 0, "proposed": 0}

    now = datetime.now(UTC).replace(tzinfo=None)
    counts = {"goals": 0, "steps": 0, "people": 0, "proposed": 0}

    with Session(get_engine(settings)) as session:
        for goal in goals:
            if goal.proposed:
                # Inferred, not confirmed. Counted so the CLI can say how many
                # are waiting, but never indexed as a real goal.
                counts["proposed"] += 1
                continue

            row = session.execute(
                select(Goal).where(Goal.slug == goal.slug)
            ).scalar_one_or_none()
            deadline = (
                datetime.combine(goal.deadline, datetime.min.time())
                if goal.deadline
                else None
            )
            why = goal.why
            if goal.history:
                # Carried into the goal so a nudge can say where a relationship
                # came from, not just that it exists.
                why = f"{why}  [history: {goal.history}]".strip()

            if row is None:
                row = Goal(
                    slug=goal.slug, title=goal.title, why=why, deadline=deadline,
                    status=goal.status, parent_slug=goal.parent or "",
                    created_at=now, updated_at=now,
                )
                session.add(row)
                session.flush()
            else:
                row.title, row.why = goal.title, why
                row.deadline, row.status = deadline, goal.status
                row.parent_slug = goal.parent or ""
                row.updated_at = now
            counts["goals"] += 1

            session.execute(delete(GoalStep).where(GoalStep.goal_id == row.id))
            session.flush()
            for position, step in enumerate(goal.steps):
                session.add(
                    GoalStep(
                        goal_id=row.id, description=step.description,
                        position=position, done=step.done, created_at=now,
                    )
                )
                counts["steps"] += 1

            for slug, role in goal.people.items():
                exists = session.execute(
                    select(PersonRole)
                    .where(PersonRole.person_slug == slug)
                    .where(PersonRole.goal_id == row.id)
                ).scalar_one_or_none()
                if exists is None:
                    session.add(
                        PersonRole(
                            person_slug=slug, goal_id=row.id, role=role,
                            created_at=now,
                        )
                    )
                else:
                    exists.role = role
                counts["people"] += 1

        # Goals that vanished from the file are removed from the index. The
        # file is the source of truth (D2), so a goal deleted there must stop
        # driving the sweep -- otherwise the two fake goals I invented as
        # examples keep firing alerts forever after being deleted.
        confirmed = {g.slug for g in goals if not g.proposed}
        stale = [
            row
            for row in session.execute(select(Goal)).scalars()
            if row.slug not in confirmed
        ]
        for row in stale:
            session.execute(delete(GoalStep).where(GoalStep.goal_id == row.id))
            session.execute(delete(PersonRole).where(PersonRole.goal_id == row.id))
            session.delete(row)
            counts["removed"] = counts.get("removed", 0) + 1

        session.commit()

    roll_up_activity(settings)
    log.info(
        "goals.md: %(goals)d goal(s), %(steps)d step(s), %(people)d role(s), "
        "%(proposed)d proposed", counts,
    )
    return counts


def roll_up_activity(settings: Settings | None = None) -> int:
    """A child's activity is the parent's activity.

    Without this the flat model reported "Land HackDev sponsors has had no
    activity at all" while a sponsor conversation was live -- the activity was
    real, it was just attached to a child. A parent goal is an umbrella, and an
    umbrella is being worked on whenever anything under it is.
    """
    settings = settings or get_settings()
    init_db(settings)
    changed = 0

    with Session(get_engine(settings)) as session:
        rows = list(session.execute(select(Goal)).scalars())
        by_slug = {r.slug: r for r in rows}

        # Deepest first, so a grandchild's activity reaches the top.
        def depth(row: Goal) -> int:
            steps, current = 0, row
            while current.parent_slug and current.parent_slug in by_slug:
                current = by_slug[current.parent_slug]
                steps += 1
                if steps > 8:
                    break
            return steps

        for row in sorted(rows, key=depth, reverse=True):
            if not row.parent_slug or row.last_activity is None:
                continue
            parent = by_slug.get(row.parent_slug)
            if parent is None:
                continue
            if parent.last_activity is None or row.last_activity > parent.last_activity:
                parent.last_activity = row.last_activity
                changed += 1
        session.commit()
    return changed


def render_tree(goals: list[TreeGoal]) -> str:
    """The tree as an outline, deadlines shown where they came from."""
    if not goals:
        return (
            "No goals. Create context/goals/goals.md — it is a file you edit, "
            "not a form you fill in."
        )
    by_slug = {g.slug: g for g in goals}
    lines: list[str] = []
    for goal in goals:
        if goal.proposed:
            continue
        indent = "  " * (goal.depth - 2)
        open_steps = sum(1 for s in goal.steps if not s.done)
        bits = []
        if goal.deadline:
            days = (goal.deadline - date.today()).days
            own = any(
                c for c in goal.children if by_slug.get(c) and by_slug[c].deadline == goal.deadline
            )
            # Say whose deadline it is. A parent showing a date it inherited
            # from a child looks like a date nobody wrote down.
            via = " (via child)" if own and goal.children else ""
            bits.append(f"{days}d{via}")
        if open_steps:
            bits.append(f"{open_steps} open")
        suffix = f"   [{', '.join(bits)}]" if bits else ""
        lines.append(f"{indent}{goal.title}{suffix}")

    proposed = [g for g in goals if g.proposed]
    if proposed:
        lines += ["", "Proposed by the system, waiting on you:"]
        lines += [f"  ? {g.title}" for g in proposed]
    return "\n".join(lines)
