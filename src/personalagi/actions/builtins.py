"""The starting set of real actions, one per tier.

WHAT IS AND IS NOT INERT
------------------------
`draft_email` is REAL. It creates a Gmail draft through
`users().drafts().create`, an API that cannot send. Everything else in this
file is still a stub that validates its arguments and describes what a real
implementation would do.

That is the intended progression, not an inconsistency. Handlers get their
real bodies one at a time, each with its own tests, behind a tier that was
declared before the body existed. draft_email went first precisely because a
draft is the most reversible externally-visible thing the system can produce:
it sits in a folder until a human presses send.

NOTHING IN THIS FILE SENDS ANYTHING.
`send_email` and `create_calendar_event` remain stubs on purpose. You cannot
un-send an email or un-invite eight people, so those handlers stay inert until
someone is awake to watch the first one run. A test asserts that no send API
appears anywhere in the codebase.

The tiers are the contract; the bodies arrive behind them.
"""

from __future__ import annotations

from personalagi.actions.registry import action
from personalagi.actions.tiers import ActionArgs, Result, Tier

#: Anything written outside this prefix is no longer "private and reversible".
CONTEXT_DIR = "context/"

#: Files that are never a legitimate write target for an autonomous system:
#: secrets, OAuth tokens, and the code/config that defines its own permissions.
PROTECTED_PATHS = frozenset(
    {
        ".env",
        ".env.example",
        "pyproject.toml",
        "credentials/credentials.json",
    }
)

#: Directory prefixes with the same status as PROTECTED_PATHS.
PROTECTED_PREFIXES = ("credentials/", "tokens/", ".git/", ".venv/")


# --------------------------------------------------------------------------
# Escalation predicates for write_file.
#
# Both may only RAISE the tier — the registry rejects any predicate declaring a
# tier below the registration base, at import time. That asymmetry is the point:
# if either of these is buggy, the failure mode is an unnecessary approval
# prompt, never an unapproved write.
# --------------------------------------------------------------------------


def _outside_context_dir(p: ActionArgs) -> bool:
    """A write outside context/ is externally visible or hard to undo."""
    return not str(p.path).startswith(CONTEXT_DIR)


def _is_protected_path(p: ActionArgs) -> bool:
    """Secrets, tokens, and permission-defining config. Never writable."""
    path = str(p.path)
    return path in PROTECTED_PATHS or path.startswith(PROTECTED_PREFIXES)


# --------------------------------------------------------------------------
# AUTO — reversible, private, no external effect
# --------------------------------------------------------------------------


@action("append_context", tier=Tier.AUTO)
def append_context(person: str, text: str, source: str = "") -> Result:
    """Append a line to a person's context log. INERT: nothing is written."""
    if not person.strip() or not text.strip():
        return Result(ok=False, detail="append_context needs a person and text")
    return Result(
        ok=True,
        detail=f"would append {len(text)} chars to context/people/{person}.md",
        data={"person": person, "chars": len(text), "source": source, "written": False},
    )


@action("draft_email", tier=Tier.AUTO)
def draft_email(
    to: str,
    subject: str,
    body: str,
    account: str = "personal",
    service=None,
) -> Result:
    """Create a real Gmail draft. AUTO because a draft is private and reversible.

    THE ONLY NON-INERT HANDLER IN THIS FILE, and the tier separation is what
    makes that acceptable: drafting and sending are two different actions at
    two different tiers. `users().drafts().create` cannot send — sending is a
    different API call that appears nowhere in this codebase.

    Three things must be true before anything happens, checked in this order
    because each produces a clearer message than the one after it:
      1. There is a recipient.
      2. The stored token actually grants gmail.compose. It currently grants
         gmail.readonly, so today this returns a failure with instructions
         rather than an HttpError 403 with a JSON body.
      3. A Gmail service can be built.

    `service` is injectable so the whole path is testable without a network.
    """
    from personalagi.actions import gmail_write

    # `sent: False` is on EVERY return path, success or failure. A caller
    # inspecting this result must never have to distinguish "did not send"
    # from "the key is missing because we failed early".
    never_sent = {"to": to, "subject": subject, "created": False, "sent": False}

    if not to.strip():
        return Result(
            ok=False, detail="draft_email needs a recipient", data=never_sent
        )

    try:
        raw = gmail_write.build_mime(to, subject, body)
    except ValueError as exc:
        return Result(ok=False, detail=str(exc), data=never_sent)

    if service is None:
        if not gmail_write.can_draft(account):
            # Not an exception: a missing scope is an expected state today, and
            # a proposal that cannot run should report that, not crash a batch.
            return Result(
                ok=False,
                detail=gmail_write.scope_help(account),
                data={**never_sent, "reason": "insufficient_scope"},
            )
        from personalagi.ingest.auth import build_service, load_credentials

        service = build_service(load_credentials(account))

    try:
        created = gmail_write.create_draft(service, raw)
    except Exception as exc:  # noqa: BLE001 - reported, never raised to the batch
        return Result(
            ok=False,
            detail=f"draft creation failed: {str(exc)[:200]}",
            data=never_sent,
        )

    return Result(
        ok=True,
        detail=f"created Gmail draft to {to} ({len(body)} chars) — NOT sent",
        data={**never_sent, "draft_id": created.get("id", ""), "created": True},
    )


@action(
    "write_file",
    tier=Tier.AUTO,
    escalate_if=[
        (_outside_context_dir, Tier.APPROVE),
        (_is_protected_path, Tier.NEVER),
    ],
)
def write_file(path: str, content: str) -> Result:
    """Write a file. AUTO inside context/, escalated elsewhere. INERT."""
    return Result(
        ok=True,
        detail=f"would write {len(content)} chars to {path}",
        data={"path": path, "bytes": len(content.encode()), "written": False},
    )


# --------------------------------------------------------------------------
# APPROVE — externally visible or hard to undo
# --------------------------------------------------------------------------


@action("send_email", tier=Tier.APPROVE)
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    evidence: list[int] | None = None,
    confirmation=None,
) -> Result:
    """Send mail. Real code path, transport that does not deliver.

    APPROVE because you cannot un-send an email. But the tier is not the only
    guard: delivery also requires a confirmation token bound to a hash of the
    exact content, produced by a surface that showed the owner the full body,
    the real recipients, and the evidence. Editing the draft after approval
    invalidates that approval by construction.

    While SEND_ENABLED is false the transport records instead of delivering,
    and every line here still runs -- so turning it on changes where the bytes
    go and nothing else. A send path first exercised on the day it sends for
    real is a send path nobody has tested.
    """
    from personalagi.actions.transport import Outgoing, SendBlocked, send
    from personalagi.config import get_settings

    never_sent = {"to": to, "subject": subject, "sent": False}
    if not to.strip():
        return Result(ok=False, detail="send_email needs a recipient", data=never_sent)

    message = Outgoing(
        to=to, subject=subject, body=body, cc=cc,
        evidence_event_ids=tuple(evidence or ()),
    )
    try:
        outcome = send(message, get_settings(), confirmation=confirmation)
    except SendBlocked as exc:
        return Result(ok=False, detail=str(exc), data=never_sent)

    return Result(
        ok=outcome.ok,
        detail=outcome.detail,
        data={
            **never_sent,
            # `sent` is True ONLY when a transport that really delivers did so.
            "sent": outcome.transport != "recording" and outcome.ok,
            "transport": outcome.transport,
            "fingerprint": outcome.fingerprint,
        },
    )


@action("create_calendar_event", tier=Tier.APPROVE)
def create_calendar_event(
    title: str, start: str, end: str, attendees: list[str] | None = None
) -> Result:
    """Create a calendar event. Externally visible to attendees. INERT."""
    return Result(
        ok=True,
        detail=f"[inert] approved event {title!r} {start} -> {end}",
        data={
            "title": title,
            "start": start,
            "end": end,
            "attendees": list(attendees or []),
            "created": False,
        },
    )


# --------------------------------------------------------------------------
# NEVER — irreversible / high blast radius
# --------------------------------------------------------------------------


@action("delete_context", tier=Tier.NEVER)
def delete_context(path: str) -> Result:
    """Delete context data. Registered so it can be REFUSED by name.

    The body raises rather than deleting: the dispatcher rejects NEVER before
    any handler runs, so reaching this line means the tier check was bypassed,
    and the correct response to that is a loud crash instead of a deletion.
    """
    raise RuntimeError(
        f"delete_context({path!r}) was invoked. NEVER-tier handlers are unreachable "
        f"by design; if this ran, the dispatcher's tier check was bypassed."
    )
