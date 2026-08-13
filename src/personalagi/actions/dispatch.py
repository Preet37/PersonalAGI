"""Dispatch: route a Proposal by the tier the registry declares, and log it.

Routing (D6 + D8):

    AUTO     -> execute immediately
    APPROVE  -> queue for a human, handler untouched
    NEVER    -> reject, handler untouched
    unknown  -> reject, before any handler could possibly run

Every dispatch is audited, including AUTO. Auto means no prompt, not no trace.

The audit table lives in THIS module, not in personalagi.models: it is owned by
the actions package and written with plain sqlite3 so an audit write never
depends on the ORM session an action might itself be mutating.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from personalagi.actions.registry import (
    REGISTRY,
    Proposal,
    Registry,
    UnknownActionError,
)
from personalagi.actions.tiers import Result, Tier, TierResolution

AUDIT_TABLE = "action_audit"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    proposal_id   TEXT    NOT NULL,
    action        TEXT    NOT NULL,
    args_json     TEXT    NOT NULL,
    base_tier     TEXT,
    tier          TEXT    NOT NULL,
    decision      TEXT    NOT NULL,
    outcome       TEXT    NOT NULL,
    detail        TEXT    NOT NULL DEFAULT '',
    rationale     TEXT    NOT NULL DEFAULT '',
    confidence    REAL,
    evidence_json TEXT    NOT NULL DEFAULT '[]',
    escalations   TEXT    NOT NULL DEFAULT '',
    batch_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_audit_action ON {AUDIT_TABLE}(action);
CREATE INDEX IF NOT EXISTS idx_action_audit_ts ON {AUDIT_TABLE}(ts);
"""


class Decision(StrEnum):
    EXECUTED = "executed"
    QUEUED = "queued"
    REJECTED = "rejected"


class Outcome(StrEnum):
    OK = "ok"
    ERROR = "error"
    PENDING = "pending"
    BLOCKED = "blocked"
    UNKNOWN_ACTION = "unknown_action"


def default_audit_path() -> Path:
    """Same SQLite file the rest of the system uses, resolved lazily.

    Lazy because importing settings at module import time would make this
    package unusable in a test that never wants a real database.
    """
    try:
        from personalagi.config import get_settings

        url = get_settings().database_url
    except Exception:  # noqa: BLE001 - config is optional for this package
        url = "sqlite:///data/personalagi.db"
    prefix = "sqlite:///"
    return Path(url[len(prefix) :] if url.startswith(prefix) else "data/personalagi.db")


class AuditLog:
    """Append-only record of every dispatch decision.

    Not derived state in the D2 sense: rebuilding the index from markdown will
    not regenerate it, so this table is only ever appended to, never rewritten.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_audit_path()
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            if str(self.path) not in (":memory:", ""):
                self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
        return self._conn

    def record(
        self,
        *,
        proposal: Proposal,
        tier: Tier,
        decision: Decision,
        outcome: Outcome,
        base_tier: Tier | None = None,
        detail: str = "",
        escalations: str = "",
        batch_id: str | None = None,
        when: datetime | None = None,
    ) -> int:
        conn = self._connect()
        row = (
            (when or datetime.now(UTC)).isoformat(),
            proposal.id,
            proposal.action,
            _safe_json(proposal.args.as_dict()),
            base_tier.name if base_tier is not None else None,
            tier.name,
            str(decision),
            str(outcome),
            detail,
            proposal.rationale,
            float(proposal.confidence),
            _safe_json(
                [
                    {
                        "source": e.source,
                        "event_id": e.event_id,
                        "excerpt": e.excerpt,
                        "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                    }
                    for e in proposal.evidence
                ]
            ),
            escalations,
            batch_id,
        )
        cur = conn.execute(
            f"INSERT INTO {AUDIT_TABLE} (ts, proposal_id, action, args_json, base_tier, "
            f"tier, decision, outcome, detail, rationale, confidence, evidence_json, "
            f"escalations, batch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
        conn.commit()
        return int(cur.lastrowid or 0)

    def records(self, *, action: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        conn = self._connect()
        if action is None:
            cur = conn.execute(
                f"SELECT * FROM {AUDIT_TABLE} ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            cur = conn.execute(
                f"SELECT * FROM {AUDIT_TABLE} WHERE action = ? ORDER BY id DESC LIMIT ?",
                (action, limit),
            )
        return [dict(row) for row in cur.fetchall()]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - default=str covers most
        return json.dumps({"unserializable": repr(value)})


@dataclass(frozen=True)
class DispatchResult:
    proposal: Proposal
    tier: Tier
    decision: Decision
    outcome: Outcome
    base_tier: Tier | None = None
    result: Result | None = None
    reason: str = ""
    audit_id: int = 0
    batch_id: str | None = None

    @property
    def executed(self) -> bool:
        return self.decision is Decision.EXECUTED


@dataclass(frozen=True)
class BatchItem:
    """One frozen (proposal, resolved tier) pair inside a snapshot."""

    proposal: Proposal
    tier: Tier
    base_tier: Tier


@dataclass(frozen=True)
class ApprovalBatch:
    """An immutable snapshot of exactly what was shown to the human.

    Approval executes THIS, never a re-read of the pending queue. If the queue
    is what gets executed, then anything that lands in it between render and
    click rides along on a human's approval of something else — a
    confused-deputy bug, and one that a compromised or merely eager proposer
    can drive on purpose.
    """

    items: tuple[BatchItem, ...]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __len__(self) -> int:
        return len(self.items)

    @property
    def proposal_ids(self) -> tuple[str, ...]:
        return tuple(item.proposal.id for item in self.items)

    def item(self, proposal_id: str) -> BatchItem:
        for item in self.items:
            if item.proposal.id == proposal_id:
                return item
        raise KeyError(proposal_id)


class BatchError(Exception):
    """A batch was approved twice, or approved with ids it never contained."""


class Dispatcher:
    """Looks the name up, reads the tier, routes, and logs. In that order."""

    def __init__(
        self,
        registry: Registry | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.registry = registry if registry is not None else REGISTRY
        self.audit = audit if audit is not None else AuditLog()
        self._pending: list[Proposal] = []
        self._consumed_batches: set[str] = set()

    # -- single dispatch ---------------------------------------------------

    def dispatch(self, proposal: Proposal) -> DispatchResult:
        # 1. Name lookup FIRST. An unregistered name never reaches a handler
        #    because there is no handler to reach.
        try:
            action = self.registry.get(proposal.action)
        except UnknownActionError as exc:
            return self._finish(
                proposal,
                tier=Tier.NEVER,
                base_tier=None,
                decision=Decision.REJECTED,
                outcome=Outcome.UNKNOWN_ACTION,
                reason=str(exc),
            )

        # 2. Tier comes from the registry and the predicates. Nothing in
        #    proposal.args, rationale, or evidence is consulted to *lower* it.
        resolution = action.resolve(proposal.args)
        return self._route(proposal, resolution)

    def _route(
        self,
        proposal: Proposal,
        resolution: TierResolution,
        batch_id: str | None = None,
    ) -> DispatchResult:
        escalations = _describe(resolution)

        if resolution.tier is Tier.NEVER:
            # Rejected before any handler runs. Irreversible or high blast
            # radius: there is no approval path, by design.
            return self._finish(
                proposal,
                tier=resolution.tier,
                base_tier=resolution.base,
                decision=Decision.REJECTED,
                outcome=Outcome.BLOCKED,
                reason="tier NEVER: irreversible or high blast radius",
                escalations=escalations,
                batch_id=batch_id,
            )

        if resolution.tier is Tier.APPROVE:
            self._pending.append(proposal)
            return self._finish(
                proposal,
                tier=resolution.tier,
                base_tier=resolution.base,
                decision=Decision.QUEUED,
                outcome=Outcome.PENDING,
                reason="tier APPROVE: waiting for human approval",
                escalations=escalations,
                batch_id=batch_id,
            )

        return self._execute(proposal, resolution, batch_id=batch_id)

    def _execute(
        self,
        proposal: Proposal,
        resolution: TierResolution,
        batch_id: str | None = None,
    ) -> DispatchResult:
        action = self.registry.get(proposal.action)
        try:
            # Strict kwargs: an argument the handler does not declare is a
            # TypeError, not a silently dropped key. Injected extras should
            # fail loudly rather than be quietly ignored.
            result = action.handler(**proposal.args.as_dict())
            if not isinstance(result, Result):
                raise TypeError(
                    f"handler for {proposal.action!r} returned {type(result).__name__}, "
                    f"expected Result"
                )
            outcome = Outcome.OK if result.ok else Outcome.ERROR
            detail = result.detail
        except Exception as exc:  # noqa: BLE001 - a handler bug is audited, not fatal
            result = Result(ok=False, detail=f"{type(exc).__name__}: {exc}")
            outcome = Outcome.ERROR
            detail = result.detail
        return self._finish(
            proposal,
            tier=resolution.tier,
            base_tier=resolution.base,
            decision=Decision.EXECUTED,
            outcome=outcome,
            reason=detail,
            result=result,
            escalations=_describe(resolution),
            batch_id=batch_id,
        )

    def _finish(
        self,
        proposal: Proposal,
        *,
        tier: Tier,
        base_tier: Tier | None,
        decision: Decision,
        outcome: Outcome,
        reason: str = "",
        result: Result | None = None,
        escalations: str = "",
        batch_id: str | None = None,
    ) -> DispatchResult:
        # Every path through the dispatcher lands here, so every dispatch is
        # audited — including AUTO, which prompts nobody but still leaves a trace.
        audit_id = self.audit.record(
            proposal=proposal,
            tier=tier,
            base_tier=base_tier,
            decision=decision,
            outcome=outcome,
            detail=reason,
            escalations=escalations,
            batch_id=batch_id,
        )
        return DispatchResult(
            proposal=proposal,
            tier=tier,
            base_tier=base_tier,
            decision=decision,
            outcome=outcome,
            result=result,
            reason=reason,
            audit_id=audit_id,
            batch_id=batch_id,
        )

    def dispatch_all(self, proposals: Iterable[Proposal]) -> tuple[DispatchResult, ...]:
        return tuple(self.dispatch(p) for p in proposals)

    # -- approval queue ----------------------------------------------------

    def pending(self) -> tuple[Proposal, ...]:
        """A tuple, not the list. Callers cannot mutate the queue by accident."""
        return tuple(self._pending)

    def clear_pending(self) -> None:
        self._pending.clear()

    def render_approval_batch(
        self, proposals: Sequence[Proposal] | None = None
    ) -> ApprovalBatch:
        """Freeze exactly the set being shown to the human.

        Proposals are deep-copied and their tiers resolved now, so later queue
        churn — additions, removals, argument edits — cannot change what a
        later approval executes.
        """
        source = list(self._pending) if proposals is None else list(proposals)
        items = []
        for proposal in source:
            frozen = proposal.snapshot()
            resolution = self.registry.resolve(frozen.action, frozen.args)
            items.append(
                BatchItem(
                    proposal=frozen, tier=resolution.tier, base_tier=resolution.base
                )
            )
        return ApprovalBatch(items=tuple(items))

    def approve(
        self, batch: ApprovalBatch, approved_ids: Iterable[str] | None = None
    ) -> tuple[DispatchResult, ...]:
        """Execute the snapshot — never a re-read of the queue.

        ``approved_ids`` selects a subset of THIS batch. Ids outside the batch
        are an error, not a silent extra send.
        """
        if batch.id in self._consumed_batches:
            raise BatchError(f"approval batch {batch.id} was already acted on")
        self._consumed_batches.add(batch.id)

        if approved_ids is None:
            selected = batch.items
        else:
            wanted = list(dict.fromkeys(approved_ids))
            unknown = [pid for pid in wanted if pid not in batch.proposal_ids]
            if unknown:
                raise BatchError(
                    f"ids {unknown} are not in approval batch {batch.id}; approval "
                    f"applies only to what was rendered"
                )
            selected = tuple(batch.item(pid) for pid in wanted)

        results: list[DispatchResult] = []
        for item in selected:
            results.append(self._execute_approved(item, batch.id))
        # Only what was acted on leaves the queue. Anything rendered but not
        # approved stays pending rather than silently disappearing with no
        # audit record of why.
        self._drop_from_pending(item.proposal.id for item in selected)
        return tuple(results)

    def _execute_approved(self, item: BatchItem, batch_id: str) -> DispatchResult:
        """Defence in depth: re-check the registry at execution time.

        A snapshot is data, and data can be forged or go stale. Human approval
        of a batch is not a substitute for the tier check — it is only ever an
        additional gate on top of it.
        """
        proposal = item.proposal
        try:
            action = self.registry.get(proposal.action)
        except UnknownActionError as exc:
            return self._finish(
                proposal,
                tier=Tier.NEVER,
                base_tier=None,
                decision=Decision.REJECTED,
                outcome=Outcome.UNKNOWN_ACTION,
                reason=str(exc),
                batch_id=batch_id,
            )

        resolution = action.resolve(proposal.args)
        if resolution.tier is Tier.NEVER:
            return self._finish(
                proposal,
                tier=resolution.tier,
                base_tier=resolution.base,
                decision=Decision.REJECTED,
                outcome=Outcome.BLOCKED,
                reason="tier NEVER: approval cannot authorize this action",
                escalations=_describe(resolution),
                batch_id=batch_id,
            )
        return self._execute(proposal, resolution, batch_id=batch_id)

    def reject(self, batch: ApprovalBatch, reason: str = "declined") -> tuple[DispatchResult, ...]:
        if batch.id in self._consumed_batches:
            raise BatchError(f"approval batch {batch.id} was already acted on")
        self._consumed_batches.add(batch.id)
        results = tuple(
            self._finish(
                item.proposal,
                tier=item.tier,
                base_tier=item.base_tier,
                decision=Decision.REJECTED,
                outcome=Outcome.BLOCKED,
                reason=f"human declined: {reason}",
                batch_id=batch.id,
            )
            for item in batch.items
        )
        self._drop_from_pending(batch.proposal_ids)
        return results

    def _drop_from_pending(self, ids: Iterable[str]) -> None:
        drop = set(ids)
        self._pending = [p for p in self._pending if p.id not in drop]


def _describe(resolution: TierResolution) -> str:
    parts = list(resolution.triggered)
    parts += [f"ERROR({e})" for e in resolution.errors]
    return "; ".join(parts)
