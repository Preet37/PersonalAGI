"""Goals: the record type that makes the system proactive instead of reactive.

Without goals the system can only answer "did something arrive". That is why
it can never mention the letter of recommendation nobody asked for — nothing
arrived, so nothing triggered, so nothing was said. The absence was the signal
and there was no record type capable of representing it.

A goal turns absence into a query:

    a step, not done, with no supporting evidence anywhere, and a deadline soon

That is an anti-join. It costs no model calls, which is what makes checking it
every morning cost pennies rather than hundreds of dollars.

STORAGE (ARCHITECTURE.md D2)
Markdown at `context/goals/<slug>.md` is the source of truth. The `goal`,
`goal_step`, and `step_evidence` tables are a rebuildable index — deadline
arithmetic and anti-joins need SQL, not a directory scan. Editing the markdown
by hand and re-syncing is always safe and is the intended way to correct a
goal.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import frontmatter
import yaml
from sqlalchemy import delete, func, select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.context.people import slugify
from personalagi.db import get_engine, init_db
from personalagi.models import Commitment, Event, Goal, GoalStep, PersonRole, StepEvidence
from personalagi.records import GoalStatus, PersonRoleKind, citable_events

log = logging.getLogger(__name__)

GOALS_DIR = "goals"

# "- [ ] description" / "- [x] description  !blocking"
STEP_RE = re.compile(r"^-\s*\[(?P<done>[ xX])\]\s*(?P<text>.+?)\s*$")
BLOCKING_MARK = "!blocking"


class GoalError(RuntimeError):
    pass


@dataclass
class Step:
    description: str
    done: bool = False
    blocking: bool = False
    evidence: list[str] = field(default_factory=list)

    def render(self) -> str:
        mark = "x" if self.done else " "
        suffix = f"  {BLOCKING_MARK}" if self.blocking else ""
        line = f"- [{mark}] {self.description}{suffix}"
        if self.evidence:
            # Evidence is rendered as source ids so the file stays readable and
            # portable — a database row id means nothing in Obsidian.
            line += "".join(f" [e:{eid}]" for eid in self.evidence)
        return line


@dataclass
class GoalFile:
    slug: str
    title: str
    why: str = ""
    deadline: date | None = None
    status: str = GoalStatus.ACTIVE
    people: dict[str, str] = field(default_factory=dict)  # slug -> role
    steps: list[Step] = field(default_factory=list)
    notes: str = ""

    def to_markdown(self) -> str:
        # str() on every value: StrEnum IS a str subclass, but PyYAML's safe
        # representer dispatches on exact type and refuses the enum member.
        # Coercing at the boundary keeps the enum useful in code and keeps the
        # file plain YAML that opens in Obsidian.
        meta = {
            "title": str(self.title),
            "slug": str(self.slug),
            "status": str(self.status),
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "people": {str(k): str(v) for k, v in self.people.items()},
        }
        header = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
        lines = [
            "---",
            header,
            "---",
            "",
            "## Why",
            "",
            self.why.strip() or "_Not stated._",
            "",
            "## Steps",
            "",
        ]
        lines += [step.render() for step in self.steps]
        if self.notes.strip():
            lines += ["", "## Notes", "", self.notes.strip()]
        return "\n".join(lines).rstrip() + "\n"


def _parse_steps(body: str) -> tuple[str, list[Step], str]:
    why_lines: list[str] = []
    note_lines: list[str] = []
    steps: list[Step] = []
    section = None

    for raw in body.splitlines():
        stripped = raw.strip()
        low = stripped.lower()
        if low.startswith("## why"):
            section = "why"
            continue
        if low.startswith("## steps"):
            section = "steps"
            continue
        if low.startswith("## notes"):
            section = "notes"
            continue

        if section == "why":
            why_lines.append(raw)
        elif section == "notes":
            note_lines.append(raw)
        elif section == "steps" and stripped:
            match = STEP_RE.match(stripped)
            if not match:
                continue
            text = match.group("text")
            evidence = re.findall(r"\[e:([^\]]+)\]", text)
            text = re.sub(r"\s*\[e:[^\]]+\]", "", text)
            blocking = BLOCKING_MARK in text
            text = text.replace(BLOCKING_MARK, "").strip()
            steps.append(
                Step(
                    description=text,
                    done=match.group("done").lower() == "x",
                    blocking=blocking,
                    evidence=evidence,
                )
            )

    why = "\n".join(why_lines).strip()
    if why == "_Not stated._":
        why = ""
    return why, steps, "\n".join(note_lines).strip()


def parse_goal(text: str, slug_hint: str = "") -> GoalFile:
    post = frontmatter.loads(text)
    meta = dict(post.metadata)
    why, steps, notes = _parse_steps(post.content)

    raw_deadline = meta.get("deadline")
    deadline = None
    if raw_deadline:
        deadline = (
            raw_deadline
            if isinstance(raw_deadline, date)
            else date.fromisoformat(str(raw_deadline))
        )

    return GoalFile(
        slug=str(meta.get("slug") or slug_hint or slugify(str(meta.get("title", "")))),
        title=str(meta.get("title", "")),
        why=why,
        deadline=deadline,
        status=str(meta.get("status", GoalStatus.ACTIVE)),
        people=dict(meta.get("people") or {}),
        steps=steps,
        notes=notes,
    )


def goals_dir(settings: Settings) -> Path:
    return settings.context_dir / GOALS_DIR


def goal_path(settings: Settings, slug: str) -> Path:
    return goals_dir(settings) / f"{slug}.md"


def save_goal(settings: Settings, goal: GoalFile) -> Path:
    path = goal_path(settings, goal.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: the vault is the source of truth and a crash mid-write
    # must not truncate it.
    temp = path.with_suffix(".md.tmp")
    temp.write_text(goal.to_markdown(), encoding="utf-8")
    temp.replace(path)
    return path


def load_goal(settings: Settings, slug: str) -> GoalFile | None:
    path = goal_path(settings, slug)
    if not path.exists():
        return None
    return parse_goal(path.read_text(encoding="utf-8"), slug_hint=slug)


def iter_goals(settings: Settings):
    directory = goals_dir(settings)
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.md")):
        yield parse_goal(path.read_text(encoding="utf-8"), slug_hint=path.stem)


# --- creation ----------------------------------------------------------


def create_goal(
    title: str,
    settings: Settings | None = None,
    *,
    why: str = "",
    deadline: date | None = None,
    steps: list[str] | None = None,
    people: dict[str, str] | None = None,
    slug: str | None = None,
) -> GoalFile:
    settings = settings or get_settings()
    if not title.strip():
        raise GoalError("a goal needs a title")

    resolved = slug or slugify(title)
    if load_goal(settings, resolved) is not None:
        raise GoalError(f"a goal with slug '{resolved}' already exists")

    goal = GoalFile(
        slug=resolved,
        title=title.strip(),
        why=why.strip(),
        deadline=deadline,
        people=people or {},
        steps=[Step(description=s.strip()) for s in (steps or []) if s.strip()],
    )
    save_goal(settings, goal)
    sync_goals(settings)
    return goal


# --- sync markdown -> index -------------------------------------------


@dataclass
class SyncResult:
    goals: int = 0
    steps: int = 0
    roles: int = 0

    def summary(self) -> str:
        return f"synced {self.goals} goal(s), {self.steps} step(s), {self.roles} role(s)"


def sync_goals(settings: Settings | None = None) -> SyncResult:
    """Rebuild the goal index from the markdown vault.

    Destructive on the INDEX only, never on the files. Steps are re-derived
    from markdown each time, so editing a goal file by hand and re-syncing is
    the supported way to correct one.

    Evidence links found by search are preserved across a sync by matching on
    the event's source_id, which is stable, rather than on a row id, which is
    not.
    """
    settings = settings or get_settings()
    init_db(settings)
    result = SyncResult()
    now = datetime.now(UTC).replace(tzinfo=None)

    with Session(get_engine(settings)) as session:
        for parsed in iter_goals(settings):
            row = session.execute(
                select(Goal).where(Goal.slug == parsed.slug)
            ).scalar_one_or_none()
            deadline = (
                datetime.combine(parsed.deadline, datetime.min.time())
                if parsed.deadline
                else None
            )
            if row is None:
                row = Goal(
                    slug=parsed.slug,
                    title=parsed.title,
                    why=parsed.why,
                    deadline=deadline,
                    status=parsed.status,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            else:
                row.title = parsed.title
                row.why = parsed.why
                row.deadline = deadline
                row.status = parsed.status
                # updated_at is NOT last_activity. Editing a description is
                # not progress and must not reset a staleness clock.
                row.updated_at = now
            result.goals += 1

            # Steps are rebuilt, so removing a line from markdown removes it
            # here. Evidence is re-attached below from the stable source ids.
            existing_evidence: dict[str, list[tuple[str, str, float]]] = {}
            old_steps = list(
                session.execute(
                    select(GoalStep).where(GoalStep.goal_id == row.id)
                ).scalars()
            )
            for old in old_steps:
                links = list(
                    session.execute(
                        select(StepEvidence, Event.source_id)
                        .join(Event, Event.id == StepEvidence.event_id)
                        .where(StepEvidence.step_id == old.id)
                    )
                )
                existing_evidence[old.description] = [
                    (source_id, link.method, link.confidence) for link, source_id in links
                ]
                session.execute(
                    delete(StepEvidence).where(StepEvidence.step_id == old.id)
                )
            session.execute(delete(GoalStep).where(GoalStep.goal_id == row.id))
            session.flush()

            for position, step in enumerate(parsed.steps):
                step_row = GoalStep(
                    goal_id=row.id,
                    description=step.description,
                    position=position,
                    done=step.done,
                    blocking=step.blocking,
                    created_at=now,
                )
                session.add(step_row)
                session.flush()
                result.steps += 1

                source_ids = {sid for sid, _, _ in existing_evidence.get(step.description, [])}
                source_ids.update(step.evidence)
                if source_ids:
                    _attach_by_source_id(session, step_row, source_ids, now)

            for person_slug, role in (parsed.people or {}).items():
                exists = session.execute(
                    select(PersonRole)
                    .where(PersonRole.person_slug == person_slug)
                    .where(PersonRole.goal_id == row.id)
                ).scalar_one_or_none()
                if exists is None:
                    session.add(
                        PersonRole(
                            person_slug=person_slug,
                            goal_id=row.id,
                            role=role or PersonRoleKind.CONTACT,
                            created_at=now,
                        )
                    )
                else:
                    exists.role = role or PersonRoleKind.CONTACT
                result.roles += 1

        session.commit()

    refresh_activity(settings)
    log.info(result.summary())
    return result


def _attach_by_source_id(
    session: Session, step: GoalStep, source_ids: set[str], now: datetime
) -> int:
    rows = list(
        session.execute(
            citable_events(select(Event)).where(Event.source_id.in_(source_ids))
        ).scalars()
    )
    for event in rows:
        session.add(
            StepEvidence(
                step_id=step.id,
                event_id=event.id,
                method="manual",
                confidence=1.0,
                linked_at=now,
            )
        )
    if rows:
        step.last_activity = max(e.timestamp for e in rows)
    return len(rows)


# --- activity ----------------------------------------------------------


def refresh_activity(settings: Settings | None = None) -> int:
    """Recompute `last_activity` for every goal.

    A goal is active if ANY of its evidence or linked commitments moved. That
    is what "no activity in 14 days" has to mean — a goal whose owner is
    clearly working on it must not be reported as stalled because the goal
    file itself has not been edited.
    """
    settings = settings or get_settings()
    init_db(settings)
    changed = 0

    with Session(get_engine(settings)) as session:
        for goal in session.execute(select(Goal)).scalars():
            latest = session.execute(
                select(func.max(Event.timestamp))
                .join(StepEvidence, StepEvidence.event_id == Event.id)
                .join(GoalStep, GoalStep.id == StepEvidence.step_id)
                .where(GoalStep.goal_id == goal.id)
            ).scalar()

            commitment_latest = session.execute(
                select(
                    func.max(
                        func.coalesce(Commitment.last_activity_at, Commitment.promised_at)
                    )
                ).where(Commitment.goal_id == goal.id)
            ).scalar()

            candidates = [v for v in (latest, commitment_latest) if v is not None]
            newest = max(candidates) if candidates else None
            if newest != goal.last_activity:
                goal.last_activity = newest
                changed += 1
        session.commit()
    return changed


# --- reading -----------------------------------------------------------


@dataclass
class GoalView:
    goal: Goal
    steps: list[GoalStep]
    evidence_counts: dict[int, int]
    people: list[PersonRole]

    @property
    def open_steps(self) -> list[GoalStep]:
        return [s for s in self.steps if not s.done]

    @property
    def unevidenced_steps(self) -> list[GoalStep]:
        """Not done, and nothing anywhere supports it. The gap."""
        return [s for s in self.open_steps if not self.evidence_counts.get(s.id)]

    @property
    def days_left(self) -> int | None:
        if self.goal.deadline is None:
            return None
        return (self.goal.deadline - datetime.now(UTC).replace(tzinfo=None)).days


def get_goal(slug: str, settings: Settings | None = None) -> GoalView | None:
    settings = settings or get_settings()
    init_db(settings)
    with Session(get_engine(settings)) as session:
        goal = session.execute(
            select(Goal).where(Goal.slug == slug)
        ).scalar_one_or_none()
        if goal is None:
            return None
        return _view(session, goal)


def list_goals(
    settings: Settings | None = None, *, status: str | None = GoalStatus.ACTIVE
) -> list[GoalView]:
    settings = settings or get_settings()
    init_db(settings)
    with Session(get_engine(settings)) as session:
        stmt = select(Goal)
        if status:
            stmt = stmt.where(Goal.status == status)
        goals = list(session.execute(stmt).scalars())
        views = [_view(session, goal) for goal in goals]

    # Soonest deadline first; undated goals last. A goal with a date is
    # actionable in a way one without is not.
    return sorted(
        views,
        key=lambda v: (v.goal.deadline is None, v.goal.deadline or datetime.max),
    )


def _view(session: Session, goal: Goal) -> GoalView:
    steps = list(
        session.execute(
            select(GoalStep)
            .where(GoalStep.goal_id == goal.id)
            .order_by(GoalStep.position)
        ).scalars()
    )
    counts = dict(
        session.execute(
            select(StepEvidence.step_id, func.count())
            .where(StepEvidence.step_id.in_([s.id for s in steps] or [-1]))
            .group_by(StepEvidence.step_id)
        ).all()
    )
    people = list(
        session.execute(
            select(PersonRole).where(PersonRole.goal_id == goal.id)
        ).scalars()
    )
    return GoalView(goal=goal, steps=steps, evidence_counts=counts, people=people)


def render_goal(view: GoalView) -> str:
    lines = [f"{view.goal.title}  [{view.goal.status}]"]
    if view.goal.deadline:
        days = view.days_left
        urgency = "  <-- OVERDUE" if days is not None and days < 0 else ""
        lines.append(
            f"  deadline: {view.goal.deadline:%Y-%m-%d} ({days}d){urgency}"
        )
    if view.goal.why:
        lines.append(f"  why: {view.goal.why}")
    if view.people:
        who = ", ".join(f"{p.person_slug} ({p.role})" for p in view.people)
        lines.append(f"  people: {who}")

    lines.append("")
    for step in view.steps:
        count = view.evidence_counts.get(step.id, 0)
        mark = "x" if step.done else " "
        blocking = " !blocking" if step.blocking else ""
        # A step with no evidence is the thing worth seeing, so it is called
        # out rather than shown as a bare zero.
        note = f"  ({count} evidence)" if count else "  <-- nothing supports this"
        if step.done:
            note = f"  ({count} evidence)" if count else ""
        lines.append(f"  [{mark}] {step.description}{blocking}{note}")

    return "\n".join(lines)
