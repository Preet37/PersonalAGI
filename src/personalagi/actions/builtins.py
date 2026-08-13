"""The starting set of real actions, one per tier, to prove the shape.

EVERY HANDLER IN THIS FILE IS INERT.
------------------------------------
Nothing here sends mail, deletes a file, writes to disk, or opens a socket.
They validate their arguments and return a Result describing what a real
implementation would do. This module runs unattended on a real machine, and an
action registry is worth exactly nothing if wiring it up is what causes the
first unintended send. Handlers get their real bodies one at a time, each with
its own test, behind the tier that is already declared here.

The tiers below are the contract; the bodies are placeholders.
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
def draft_email(to: str, subject: str, body: str) -> Result:
    """Compose a draft. AUTO because a draft is private and reversible.

    Drafting and sending are two different actions at two different tiers, and
    that separation is the entire reason this one can run without asking.
    INERT: the draft is returned, not saved and certainly not sent.
    """
    if not to.strip():
        return Result(ok=False, detail="draft_email needs a recipient")
    return Result(
        ok=True,
        detail=f"drafted email to {to} ({len(body)} chars) — not sent",
        data={"to": to, "subject": subject, "body": body, "sent": False},
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
def send_email(to: str, subject: str, body: str) -> Result:
    """Send mail. INERT — there is no SMTP or Gmail call in this function.

    You cannot un-send an email, which is why this is APPROVE and why the
    handler stays a stub until it has its own tests.
    """
    return Result(
        ok=True,
        detail=f"[inert] approved send to {to}: {subject!r}",
        data={"to": to, "subject": subject, "chars": len(body), "sent": False},
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
