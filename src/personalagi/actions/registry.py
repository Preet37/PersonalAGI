"""The action registry: static tiers declared at registration (D6), proposals
carrying evidence (D8).

The model's ONLY output is a Proposal — an action name plus arguments, with the
reasoning and evidence that produced it. It does not assign a tier, does not see
one, and cannot edit one. The dispatcher looks the name up here, reads the tier
that the *code* declared, and routes on that.

    "Permission is a property of the action, checked in code.
     The model can propose anything and escalate nothing."
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from personalagi.actions.tiers import (
    ActionArgs,
    ActionDefinitionError,
    EscalationRule,
    MissingTierError,
    Predicate,
    Result,
    Tier,
    TierResolution,
    normalize_rules,
    resolve_tier,
    validate_rules,
)

Handler = Callable[..., Result]


class UnknownActionError(Exception):
    """A proposal named an action that is not in the registry."""


class DuplicateActionError(ActionDefinitionError):
    """Two registrations claimed the same action name."""


# --------------------------------------------------------------------------
# Proposals (D8): "The unit of work is a proposal, not an action.
#                  Every proposal carries its evidence."
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One source event a conclusion was drawn from.

    Evidence exists so a claim can be pointed back at the specific message it
    came from (D4). It is descriptive only — nothing here influences the tier.
    """

    source: str
    event_id: str
    excerpt: str = ""
    timestamp: datetime | None = None


@dataclass(frozen=True)
class Proposal:
    """What the system thinks should happen, why, how sure it is, and from what.

    Note what is absent: a tier. There is no field for it and no way to add one.
    ``args`` is untrusted model output; a key called "tier" in there is just an
    argument with an unfortunate name and has no effect on routing.
    """

    action: str
    args: ActionArgs = field(default_factory=ActionArgs)
    rationale: str = ""
    confidence: float = 0.0
    evidence: tuple[Evidence, ...] = ()
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not isinstance(self.action, str) or not self.action.strip():
            raise ValueError("proposal.action must be a non-empty action name")
        if not self.rationale.strip():
            # D8: every proposal carries its reason. A blank one is a bug in
            # the caller, not something to paper over with a placeholder.
            raise ValueError(f"proposal for {self.action!r} has no rationale")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence!r}")
        if not isinstance(self.args, ActionArgs):
            object.__setattr__(self, "args", ActionArgs(copy.deepcopy(dict(self.args))))
        object.__setattr__(self, "evidence", tuple(self.evidence))

    def snapshot(self) -> Proposal:
        """An independent copy, safe to hold across later queue mutation."""
        return replace(
            self,
            args=ActionArgs(copy.deepcopy(self.args.as_dict())),
            evidence=tuple(self.evidence),
        )


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """A registered action. ``tier`` is the static declaration from code."""

    name: str
    handler: Handler
    tier: Tier
    escalations: tuple[EscalationRule, ...] = ()
    description: str = ""

    def resolve(self, args: ActionArgs) -> TierResolution:
        return resolve_tier(self.tier, self.escalations, args)


class Registry:
    """Name -> Action. Registration is the only way a tier ever gets set."""

    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}

    # -- registration ------------------------------------------------------

    def action(
        self,
        name: str,
        *,
        tier: Tier | None = None,
        escalate_if: Sequence[tuple[Predicate, Tier] | EscalationRule] | None = None,
        description: str = "",
    ) -> Callable[[Handler], Handler]:
        """Decorator. Every validation below raises at import time.

            @action("send_email", tier=Tier.APPROVE)
            def send_email(to: str, subject: str, body: str) -> Result: ...
        """

        def decorate(handler: Handler) -> Handler:
            self.register(
                name,
                handler,
                tier=tier,
                escalate_if=escalate_if,
                description=description or (handler.__doc__ or "").strip().split("\n")[0],
            )
            return handler

        return decorate

    def register(
        self,
        name: str,
        handler: Handler,
        *,
        tier: Tier | None = None,
        escalate_if: Sequence[tuple[Predicate, Tier] | EscalationRule] | None = None,
        description: str = "",
    ) -> Action:
        if not isinstance(name, str) or not name.strip():
            raise ActionDefinitionError("action name must be a non-empty string")
        if tier is None:
            raise MissingTierError(
                f"action {name!r} was registered without a tier. There is no default — "
                f"not AUTO, not even NEVER — because a missing tier is a bug and bugs "
                f"must be loud. Declare tier=Tier.AUTO/APPROVE/NEVER explicitly."
            )
        if not isinstance(tier, Tier):
            # bool/int would silently compare equal to a Tier via IntEnum, and a
            # string tier would come from data rather than code. Both are rejected.
            raise ActionDefinitionError(
                f"action {name!r}: tier must be a Tier member, got {tier!r}"
            )
        if not callable(handler):
            raise ActionDefinitionError(f"action {name!r}: handler is not callable")
        if name in self._actions:
            raise DuplicateActionError(
                f"action {name!r} is already registered by "
                f"{self._actions[name].handler!r}"
            )

        rules = normalize_rules(escalate_if)
        # The one-way ratchet, enforced before the action can ever be dispatched.
        validate_rules(name, tier, rules)

        registered = Action(
            name=name,
            handler=handler,
            tier=tier,
            escalations=rules,
            description=description,
        )
        self._actions[name] = registered
        return registered

    # -- lookup ------------------------------------------------------------

    def __contains__(self, name: object) -> bool:
        return name in self._actions

    def __len__(self) -> int:
        return len(self._actions)

    def get(self, name: str) -> Action:
        try:
            return self._actions[name]
        except KeyError as exc:
            raise UnknownActionError(
                f"no action registered under {name!r}. Proposals may only name "
                f"actions that exist in code."
            ) from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._actions))

    def all(self) -> tuple[Action, ...]:
        return tuple(self._actions[name] for name in self.names())

    def tier_of(self, name: str) -> Tier:
        """The declared (pre-escalation) tier. Read-only, by construction."""
        return self.get(name).tier

    def resolve(self, name: str, args: Mapping[str, Any] | ActionArgs) -> TierResolution:
        """Resolve the effective tier for one proposal's arguments."""
        action = self.get(name)
        return action.resolve(args if isinstance(args, ActionArgs) else ActionArgs(args))


#: The process-wide registry. ``builtins`` populates it at import time.
REGISTRY = Registry()


def action(
    name: str,
    *,
    tier: Tier | None = None,
    escalate_if: Sequence[tuple[Predicate, Tier] | EscalationRule] | None = None,
    description: str = "",
) -> Callable[[Handler], Handler]:
    """Register on the process-wide registry. See Registry.action."""
    return REGISTRY.action(
        name, tier=tier, escalate_if=escalate_if, description=description
    )
