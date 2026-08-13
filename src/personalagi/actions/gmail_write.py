"""The Gmail write path for draft_email. Composing only — never sending.

This is the first handler in the system that touches the outside world, so the
boundaries are drawn narrowly and stated here:

  - It creates a DRAFT. `users().drafts().create` cannot send; sending is
    `drafts().send` or `messages().send`, neither of which appears in this
    file or anywhere else in the codebase. That is checked by a test.

  - It requires `gmail.compose`. The system's stored token is `gmail.readonly`,
    so today this returns a failure explaining how to re-authorize rather than
    doing anything. Checking the granted scope BEFORE calling means the failure
    is one clear sentence instead of an HttpError 403 with a JSON body.

  - Scope is read from the token file, not assumed from a constant. The
    constant says what we would like; the token says what the user actually
    granted, and only the second one is true.
"""

from __future__ import annotations

import base64
import json
import logging
from email.message import EmailMessage
from pathlib import Path

from personalagi.config import Settings, get_settings

log = logging.getLogger(__name__)

# Create and modify drafts. Explicitly NOT gmail.send, and not the catch-all
# `mail.google.com`. The narrowest scope that can do the job is the one to ask
# for, because a token is only as safe as what it is permitted to do.
COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"

# Guard against a runaway generation loop mailing someone a novel.
MAX_BODY_CHARS = 100_000


class DraftScopeError(RuntimeError):
    """The stored token cannot create drafts."""


def granted_scopes(label: str, settings: Settings | None = None) -> set[str]:
    """Scopes actually present in the stored token file.

    Reads the file directly rather than going through Credentials, so this
    works without a network call and without refreshing anything.
    """
    settings = settings or get_settings()
    path: Path = settings.token_path(label)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return set(data.get("scopes") or [])


def can_draft(label: str, settings: Settings | None = None) -> bool:
    return COMPOSE_SCOPE in granted_scopes(label, settings)


def scope_help(label: str) -> str:
    return (
        f"account '{label}' is authorized for read-only access, so it cannot "
        f"create drafts.\n"
        f"Grant the compose scope by adding {COMPOSE_SCOPE} to auth.SCOPES and "
        f"re-running:  python -m personalagi auth {label}\n"
        "That opens a browser, so it needs you at the keyboard. Nothing here "
        "will do it automatically."
    )


def build_mime(
    to: str, subject: str, body: str, *, sender: str = "", cc: str = ""
) -> str:
    """Build an RFC 2822 message, base64url-encoded as the API wants it.

    `set_content` handles encoding and headers correctly for unicode bodies;
    hand-assembling the MIME string is where accented characters turn into
    mojibake in someone else's inbox.
    """
    if not to.strip():
        raise ValueError("a draft needs at least one recipient")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError(
            f"body is {len(body)} chars, over the {MAX_BODY_CHARS} limit"
        )

    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject or ""
    if sender:
        message["From"] = sender
    if cc:
        message["Cc"] = cc
    message.set_content(body or "")

    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def create_draft(service, raw: str, user_id: str = "me") -> dict:
    """Create a Gmail draft. Composing only — this API cannot send."""
    return (
        service.users()
        .drafts()
        .create(userId=user_id, body={"message": {"raw": raw}})
        .execute()
    )
