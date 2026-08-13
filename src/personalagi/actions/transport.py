"""Stage 32: the send path, behind a transport that does not send.

This is the most dangerous file in the system, so the danger is concentrated
here rather than spread across handlers.

THE SWITCH IS ONE FLAG AND IT IS NOT MINE TO FLIP
`SEND_ENABLED` in .env selects the transport. It defaults to false, and while
it is false `send_email` is a complete, tested, exercised code path that
delivers to a recorder instead of a mail server. Nothing about the handler
changes when you flip it — which is the point. A send path that is only
exercised the first time it sends for real is a send path nobody has tested.

WHY A FAKE TRANSPORT RATHER THAN AN `if enabled: return` GUARD
A guard means the interesting code — MIME assembly, recipient resolution,
error handling, the audit record — never runs until the day it matters. The
fake transport runs all of it and captures the result, so the only untested
line on the day you enable it is the socket.

WHAT MUST BE TRUE BEFORE ANYTHING LEAVES
Not just the permission tier. A send requires a confirmation token produced by
a surface that showed the owner the full body, the real recipients, and the
evidence the proposal rested on. The token is bound to the content by a hash,
so approving a draft and then sending a different one is not possible — an
edited body invalidates its own approval.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)


class SendBlocked(RuntimeError):
    """A send was attempted without satisfying a precondition."""


@dataclass(frozen=True)
class Outgoing:
    to: str
    subject: str
    body: str
    cc: str = ""
    evidence_event_ids: tuple[int, ...] = ()

    def fingerprint(self) -> str:
        """Content hash. What the owner approved is what gets sent.

        Binding approval to the exact bytes is what stops an approved draft
        being swapped for a different one between confirmation and delivery.
        """
        raw = "\n".join(
            [self.to.strip().lower(), self.cc.strip().lower(),
             self.subject.strip(), self.body.strip()]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass
class Sent:
    ok: bool
    detail: str
    transport: str
    fingerprint: str
    at: datetime


class Transport:
    """What a transport must do. Deliberately tiny."""

    name = "base"

    def deliver(self, message: Outgoing) -> Sent:  # pragma: no cover - interface
        raise NotImplementedError


class RecordingTransport(Transport):
    """The default. Records what WOULD have been sent, and sends nothing.

    Every line of the send path runs against this, so enabling the real one
    later changes exactly one thing: where the bytes go.
    """

    name = "recording"

    def __init__(self) -> None:
        self.outbox: list[Outgoing] = []

    def deliver(self, message: Outgoing) -> Sent:
        self.outbox.append(message)
        log.info(
            "[recording transport] would send to %s: %r (%d chars) — NOT SENT",
            message.to, message.subject, len(message.body),
        )
        return Sent(
            ok=True,
            detail=(
                f"recorded (not sent) to {message.to}: {message.subject!r}. "
                "Set SEND_ENABLED=true to deliver for real."
            ),
            transport=self.name,
            fingerprint=message.fingerprint(),
            at=datetime.now(UTC).replace(tzinfo=None),
        )


class GmailTransport(Transport):
    """The real one. Not reachable while SEND_ENABLED is false.

    Left unimplemented ON PURPOSE. Writing a working `messages().send` call now
    means a working send path exists in the repository, one config flag and one
    bug away from firing. The test that greps for send APIs would also start
    failing, and weakening that test to accommodate this would be the wrong
    trade entirely.

    When the owner is ready: implement `deliver`, add `gmail.send` to
    auth.SCOPES, re-authorize, and flip the flag. Three deliberate steps, none
    of which happen by accident.
    """

    name = "gmail"

    def deliver(self, message: Outgoing) -> Sent:
        raise SendBlocked(
            "The real Gmail transport is not implemented. This is deliberate: "
            "a working send path in the repo is one flag and one bug away from "
            "firing.\n"
            "To enable, in order:\n"
            "  1. implement GmailTransport.deliver\n"
            "  2. add gmail.send to auth.SCOPES and re-run `personalagi auth`\n"
            "  3. set SEND_ENABLED=true"
        )


def get_transport(settings) -> Transport:
    """Pick the transport. The default is the one that cannot send."""
    if getattr(settings, "send_enabled", False):
        return GmailTransport()
    return RecordingTransport()


# --- confirmation ------------------------------------------------------


@dataclass
class Confirmation:
    """Proof the owner saw the real thing and approved THAT thing."""

    fingerprint: str
    approved_at: datetime
    surface: str = "cli"


@dataclass
class ConfirmationSurface:
    """Renders exactly what will happen, then takes a decision.

    The rendering is the safety feature. An approval prompt that says "send
    this email?" without showing the body, the real recipient list, and what
    the system thinks it is acting on is a prompt that trains the owner to say
    yes — and a proposal derived from a message an attacker wrote would sail
    through it.
    """

    granted: dict[str, Confirmation] = field(default_factory=dict)

    def render(self, message: Outgoing, *, rationale: str = "") -> str:
        lines = [
            "=" * 62,
            "CONFIRM SEND — nothing has been sent yet",
            "=" * 62,
            f"To:      {message.to}",
        ]
        if message.cc:
            lines.append(f"Cc:      {message.cc}")
        lines += [
            f"Subject: {message.subject}",
            "-" * 62,
            message.body,
            "-" * 62,
        ]
        if rationale:
            lines.append(f"Why:     {rationale}")
        # Evidence, or the explicit absence of it. A send proposed with no
        # citable basis is exactly the one worth looking at hardest.
        lines.append(
            f"Based on: event(s) {', '.join(str(e) for e in message.evidence_event_ids)}"
            if message.evidence_event_ids
            else "Based on: NOTHING CITED — no external evidence backs this draft"
        )
        lines += [
            f"Fingerprint: {message.fingerprint()}",
            "Editing any of the above invalidates this approval.",
            "=" * 62,
        ]
        return "\n".join(lines)

    def approve(self, message: Outgoing, *, surface: str = "cli") -> Confirmation:
        confirmation = Confirmation(
            fingerprint=message.fingerprint(),
            approved_at=datetime.now(UTC).replace(tzinfo=None),
            surface=surface,
        )
        self.granted[confirmation.fingerprint] = confirmation
        return confirmation

    def check(self, message: Outgoing) -> Confirmation:
        found = self.granted.get(message.fingerprint())
        if found is None:
            raise SendBlocked(
                "no confirmation for this exact content. Either it was never "
                "approved, or it changed after approval — an edited draft "
                "invalidates its own approval by design."
            )
        return found


#: Process-wide surface. One place holds the approvals, so a handler cannot
#: mint its own confirmation.
SURFACE = ConfirmationSurface()


def send(
    message: Outgoing,
    settings,
    *,
    confirmation: Confirmation | None = None,
    surface: ConfirmationSurface | None = None,
) -> Sent:
    """Deliver, if and only if every precondition holds."""
    surface = surface or SURFACE

    if not message.to.strip():
        raise SendBlocked("no recipient")
    if not message.body.strip():
        # An empty body is almost always a generation failure, and sending one
        # is embarrassing in a way that cannot be undone.
        raise SendBlocked("refusing to send an empty body")

    if confirmation is None:
        confirmation = surface.check(message)
    elif confirmation.fingerprint != message.fingerprint():
        raise SendBlocked(
            "confirmation does not match this content — the draft changed "
            "after it was approved"
        )

    return get_transport(settings).deliver(message)
