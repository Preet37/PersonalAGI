"""OAuth for Gmail. One token file per account label, read-only scope.

The shared OAuth client is at GOOGLE_CREDENTIALS_PATH; each label in
GMAIL_ACCOUNTS is authorized separately and gets tokens/<label>.json.

Note: an OAuth app in "Testing" publishing status with External user type
issues refresh tokens that expire after 7 days. Expect to re-run `auth`
weekly until the app is published or switched to Internal.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from personalagi.config import Settings, get_settings

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class AuthError(RuntimeError):
    """Raised when an account cannot be authorized without user interaction."""


def _write_token(path: Path, creds: Credentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    # Tokens are bearer credentials to a mailbox. Owner-only.
    os.chmod(path, 0o600)


def authorize(label: str, settings: Settings | None = None) -> Credentials:
    """Run the interactive consent flow for one account and store its token."""
    settings = settings or get_settings()
    client_path = settings.google_credentials_path
    if not client_path.exists():
        raise AuthError(
            f"OAuth client not found at {client_path}. "
            "Download it from the Google Cloud console first."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    # port=0 lets the OS pick a free loopback port for the redirect.
    creds = flow.run_local_server(port=0, prompt="consent")
    _write_token(settings.token_path(label), creds)
    log.info("authorized %s -> %s", label, settings.token_path(label))
    return creds


def load_credentials(label: str, settings: Settings | None = None) -> Credentials:
    """Load and, if needed, refresh stored credentials for one account.

    Never opens a browser — callers that want interactive consent call
    authorize() explicitly. This keeps `ingest` non-interactive and safe to
    run from cron.
    """
    settings = settings or get_settings()
    token_path = settings.token_path(label)
    if not token_path.exists():
        raise AuthError(
            f"No token for account '{label}' at {token_path}. "
            f"Run: python -m personalagi auth {label}"
        )

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise AuthError(
                f"Refresh token for '{label}' is no longer valid ({exc}). "
                "Apps in Testing status expire refresh tokens after 7 days. "
                f"Re-run: python -m personalagi auth {label}"
            ) from exc
        _write_token(token_path, creds)
        return creds

    raise AuthError(
        f"Stored credentials for '{label}' are unusable. "
        f"Re-run: python -m personalagi auth {label}"
    )


def build_service(creds: Credentials) -> Resource:
    """Build a Gmail API client. cache_discovery=False avoids a noisy warning."""
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
