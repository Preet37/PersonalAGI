"""Action registry, permission tiers, and the proposal dispatcher.

ARCHITECTURE.md D6 (three permission tiers, keyed to reversibility) and D8
(propose, don't execute).

    from personalagi.actions import Dispatcher, Proposal, Evidence

    d = Dispatcher()
    d.dispatch(Proposal(
        action="draft_email",
        args={"to": "arjun@example.com", "subject": "re: v2", "body": "..."},
        rationale="Arjun asked for the benchmark numbers twice this week.",
        confidence=0.8,
        evidence=[Evidence(source="gmail", event_id="msg-1", excerpt="any update?")],
    ))

Importing this package imports ``builtins``, so every registration error is an
import error.
"""

from __future__ import annotations

from personalagi.actions import builtins  # registers the built-in actions
from personalagi.actions.dispatch import (
    AUDIT_TABLE,
    ApprovalBatch,
    AuditLog,
    BatchError,
    BatchItem,
    Decision,
    Dispatcher,
    DispatchResult,
    Outcome,
    default_audit_path,
)
from personalagi.actions.registry import (
    REGISTRY,
    Action,
    DuplicateActionError,
    Evidence,
    Proposal,
    Registry,
    UnknownActionError,
    action,
)
from personalagi.actions.tiers import (
    ActionArgs,
    ActionDefinitionError,
    EscalationRule,
    MissingTierError,
    Result,
    Tier,
    TierLoweringError,
    TierResolution,
    resolve_tier,
)

__all__ = [
    "AUDIT_TABLE",
    "REGISTRY",
    "Action",
    "ActionArgs",
    "ActionDefinitionError",
    "ApprovalBatch",
    "AuditLog",
    "BatchError",
    "BatchItem",
    "Decision",
    "DispatchResult",
    "Dispatcher",
    "DuplicateActionError",
    "EscalationRule",
    "Evidence",
    "MissingTierError",
    "Outcome",
    "Proposal",
    "Registry",
    "Result",
    "Tier",
    "TierLoweringError",
    "TierResolution",
    "UnknownActionError",
    "action",
    "builtins",
    "default_audit_path",
    "resolve_tier",
]
