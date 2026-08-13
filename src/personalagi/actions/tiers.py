"""Permission tiers and the escalation algebra (ARCHITECTURE.md D6).

The tier of an action is a static property of the action *type*, declared in
code at registration. It is never a runtime judgment by the model, never
derived from arguments the model produced, and never readable or writable by
the model. Everything in this module is pure: no I/O, no handler calls.

THE CORE SAFETY PROPERTY
------------------------
Escalation is monotonic. A registered action declares a base tier; predicates
attached to it may only ever raise the resolved tier, never lower it. That
asymmetry is deliberate:

    a buggy predicate can only ever make the system MORE cautious

A predicate that tries to lower a tier is a programming error, and the
registry refuses it at import time rather than warning at dispatch time —
because a warning at dispatch time is a warning nobody reads until after the
email has already gone out.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Tier(IntEnum):
    """Ordered AUTO < APPROVE < NEVER so "highest tier wins" is a max().

    The ordering is load-bearing. Resolution combines a base tier with any
    number of escalation results using max(), which means adding a predicate
    can only ever move an action toward caution.
    """

    #: Reversible, private, no external effect. Executes immediately.
    AUTO = 0
    #: Externally visible or hard to undo. Queues for human approval.
    APPROVE = 1
    #: Irreversible or high blast radius. Rejected before any handler runs.
    NEVER = 2

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


class ActionDefinitionError(Exception):
    """A registration is malformed. Always raised at import time, never warned."""


class MissingTierError(ActionDefinitionError):
    """An action was registered without a tier.

    Deliberately NOT defaulted to anything — not AUTO (unsafe) and not even
    NEVER (safe, but it hides the bug until the action silently stops working).
    A missing tier is a bug, and bugs must be loud.
    """


class TierLoweringError(ActionDefinitionError):
    """A predicate declared a tier below the action's registered base tier."""


class ActionArgs(Mapping[str, Any]):
    """Read-only view of a proposal's arguments, for escalation predicates.

    Supports both ``args["path"]`` and ``args.path`` so predicates read the way
    the design doc writes them. Missing keys raise AttributeError; dispatch
    treats a raising predicate as a fail-closed escalation, so a typo in a
    predicate makes the system more cautious rather than less.

    Nothing here is authoritative for permissions. These values came from the
    model, so anything in them ("tier": "AUTO", "already approved", "this is
    routine") is untrusted text. Predicates may *read* args to decide whether
    to raise a tier; no code path lets args lower one.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        object.__setattr__(self, "_data", dict(data or {}))

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(f"proposal args have no key {name!r}") from exc

    def __repr__(self) -> str:
        return f"ActionArgs({self._data!r})"

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)


Predicate = Callable[[ActionArgs], bool]


@dataclass(frozen=True)
class EscalationRule:
    """A predicate plus the tier it escalates to when it fires."""

    predicate: Predicate
    tier: Tier
    label: str = ""

    def describe(self) -> str:
        return self.label or getattr(self.predicate, "__name__", "<predicate>")


@dataclass(frozen=True)
class TierResolution:
    """Result of resolving an action's tier for one specific set of arguments."""

    tier: Tier
    base: Tier
    triggered: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def escalated(self) -> bool:
        return self.tier > self.base


def normalize_rules(
    raw: Sequence[tuple[Predicate, Tier] | EscalationRule] | None,
) -> tuple[EscalationRule, ...]:
    """Accept ``[(pred, Tier.X), ...]`` or EscalationRule objects."""
    if raw is None:
        return ()
    rules: list[EscalationRule] = []
    for index, entry in enumerate(raw):
        if isinstance(entry, EscalationRule):
            rules.append(entry)
            continue
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ActionDefinitionError(
                f"escalate_if[{index}] must be (predicate, Tier), got {entry!r}"
            )
        predicate, tier = entry
        if not callable(predicate):
            raise ActionDefinitionError(f"escalate_if[{index}] predicate is not callable")
        if not isinstance(tier, Tier):
            raise ActionDefinitionError(
                f"escalate_if[{index}] tier must be a Tier, got {tier!r}"
            )
        rules.append(EscalationRule(predicate=predicate, tier=tier))
    return tuple(rules)


def validate_rules(name: str, base: Tier, rules: Sequence[EscalationRule]) -> None:
    """Reject any predicate that would LOWER the tier. Import-time, raises.

    This is the one-way ratchet. A predicate may declare a tier equal to the
    base (a no-op, permitted so a rule can stay in place while its condition is
    tuned) or above it. Below it is rejected outright.
    """
    for index, rule in enumerate(rules):
        if rule.tier < base:
            raise TierLoweringError(
                f"action {name!r}: escalate_if[{index}] ({rule.describe()}) declares "
                f"{rule.tier.name}, which is LOWER than the registered base tier "
                f"{base.name}. Escalation predicates may only raise the tier, never "
                f"lower it. Fix the predicate or the base tier."
            )


def resolve_tier(
    base: Tier,
    rules: Sequence[EscalationRule],
    args: ActionArgs,
) -> TierResolution:
    """Combine the base tier with every predicate result. Highest tier wins.

    ALL predicates are evaluated — no short-circuiting — because each one is
    part of the audit record and skipping the rest after the first hit would
    hide why an action was blocked.

    A predicate that raises is treated as if it had fired (fail closed). An
    exception in safety code must not be an implicit "allow".
    """
    tier = base
    triggered: list[str] = []
    errors: list[str] = []
    for rule in rules:
        try:
            fired = bool(rule.predicate(args))
        except Exception as exc:  # noqa: BLE001 - fail closed on any predicate bug
            fired = True
            errors.append(f"{rule.describe()}: {type(exc).__name__}: {exc}")
        if fired:
            triggered.append(f"{rule.describe()}->{rule.tier.name}")
            tier = max(tier, rule.tier)
    return TierResolution(
        tier=tier,
        base=base,
        triggered=tuple(triggered),
        errors=tuple(errors),
    )


@dataclass(frozen=True)
class Result:
    """What a handler returns. Handlers never return raw values."""

    ok: bool
    detail: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
