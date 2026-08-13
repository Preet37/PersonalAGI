"""Turn a raw Gmail API message into a flat, storable record.

Pure functions only — no network, no database. Everything here is
deterministic given a message dict, which is what makes it testable.
"""

from __future__ import annotations

import base64
import binascii
import html as html_module
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import parseaddr

# Headers worth persisting, lowercased. Two groups:
#
#   Bulk markers. These are what actually separate a machine from a person.
#   The local-part regex cannot see `uber@uber.com` or `googlecloud@google.com`
#   — there is no robot token in either — but both carry List-Unsubscribe, and
#   essentially no human's mail client emits one. RFC 2369 / RFC 3834.
#
#   Threading and recipients. Needed for participant resolution (Stage 8) and
#   to tell "I promised them" from "they promised me" (commitment tracking):
#   a message where the owner is the sender is a promise made, not received.
HEADER_WHITELIST = (
    "list-unsubscribe",
    "list-id",
    "precedence",
    "auto-submitted",
    "x-auto-response-suppress",
    "return-path",
    "reply-to",
    "to",
    "cc",
    "message-id",
    "in-reply-to",
    "references",
)

# Long headers cost storage and buy nothing: References on a deep thread runs
# to kilobytes and we only ever test it for presence and split it on spaces.
HEADER_VALUE_LIMIT = 2000


@dataclass
class NormalizedMessage:
    gmail_id: str
    thread_id: str
    account_label: str
    sender_name: str
    sender_email: str
    subject: str
    body_text: str
    timestamp: datetime
    internal_date_ms: int
    headers: dict[str, str] = field(default_factory=dict)
    ingested_at: datetime = field(
        default_factory=lambda: datetime.now(UTC).replace(tzinfo=None)
    )

    def as_row(self) -> dict:
        return {
            "gmail_id": self.gmail_id,
            "thread_id": self.thread_id,
            "account_label": self.account_label,
            "sender_name": self.sender_name,
            "sender_email": self.sender_email,
            "subject": self.subject,
            "body_text": self.body_text,
            "timestamp": self.timestamp,
            "internal_date_ms": self.internal_date_ms,
            "headers_json": json.dumps(self.headers, ensure_ascii=False),
            "ingested_at": self.ingested_at,
        }


def extract_headers(payload: dict) -> dict[str, str]:
    """Pull the whitelisted headers into a flat lowercase dict.

    Repeated headers keep the first occurrence. Gmail can return duplicates
    (notably Received and, rarely, Reply-To); for every header here the first
    is the one that matters.
    """
    found: dict[str, str] = {}
    for header in payload.get("headers", []) or []:
        name = (header.get("name") or "").lower()
        if name in HEADER_WHITELIST and name not in found:
            value = (header.get("value") or "").strip()
            if value:
                found[name] = value[:HEADER_VALUE_LIMIT]
    return found


# --- headers -----------------------------------------------------------


def decode_mime_header(value: str) -> str:
    """Decode RFC 2047 encoded words (=?UTF-8?B?...?=) to plain text."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return value.strip()


def header_value(payload: dict, name: str) -> str:
    wanted = name.lower()
    for header in payload.get("headers", []):
        if header.get("name", "").lower() == wanted:
            return header.get("value", "")
    return ""


def parse_sender(from_header: str) -> tuple[str, str]:
    """Split a From: header into (display name, email address)."""
    decoded = decode_mime_header(from_header)
    name, email_addr = parseaddr(decoded)
    name = name.strip().strip('"')
    email_addr = email_addr.strip().lower()
    if not name and email_addr:
        # Fall back to the local part so there is something human-readable.
        name = email_addr.split("@", 1)[0].replace(".", " ").title()
    return name, email_addr


# --- body extraction ---------------------------------------------------


def _decode_part_data(data: str) -> str:
    """Gmail returns part bodies as base64url with padding stripped."""
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


def _collect_parts(payload: dict) -> dict[str, list[str]]:
    """Walk the MIME tree, collecting decoded text by mime type."""
    collected: dict[str, list[str]] = {"text/plain": [], "text/html": []}

    def walk(part: dict) -> None:
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body", {})
        # Parts with attachmentId carry no inline data; skip them.
        if mime in collected and body.get("data"):
            text = _decode_part_data(body["data"])
            if text:
                collected[mime].append(text)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return collected


# --- HTML stripping ----------------------------------------------------

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_BLOCKQUOTE_RE = re.compile(r"<blockquote\b[^>]*>.*?</blockquote\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_END_RE = re.compile(
    r"</\s*(p|div|tr|li|ul|ol|h[1-6]|table|section|article)\s*>", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
_GMAIL_QUOTE_RE = re.compile(r'<div[^>]*class="[^"]*gmail_quote', re.IGNORECASE)


def strip_html(html: str) -> str:
    """Flatten HTML to text, dropping quoted chains along the way.

    Regex, not a parser: mail HTML is malformed often enough that a strict
    parser is no more reliable here, and this keeps the dependency list short.
    Swap in selectolax/BeautifulSoup if the output ever proves too lossy.
    """
    if not html:
        return ""

    # Gmail puts the quoted chain in a trailing gmail_quote div. Nested divs
    # make it unmatchable by regex, so cut from its start to the end.
    match = _GMAIL_QUOTE_RE.search(html)
    if match:
        html = html[: match.start()]

    text = _COMMENT_RE.sub(" ", html)
    text = _SCRIPT_STYLE_RE.sub(" ", text)
    text = _BLOCKQUOTE_RE.sub(" ", text)  # quoted replies in HTML mail
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html_module.unescape(text)
    # nbsp / zero-width joiner / zero-width space. Marketing mail is full of
    # these; they survive unescape and would look like content to FTS5 later.
    # Written as escapes on purpose - invisible characters in source are a trap.
    text = text.replace("\xa0", " ").replace("\u200c", "").replace("\u200b", "")
    return collapse_whitespace(text)


# --- quoted reply stripping --------------------------------------------

_ORIGINAL_MSG_RE = re.compile(r"^-{2,}\s*(original message|forwarded message)\s*-{2,}", re.I)
_ON_WROTE_RE = re.compile(r"^on\b.{0,300}?\bwrote:\s*$", re.IGNORECASE | re.DOTALL)
_OUTLOOK_FROM_RE = re.compile(r"^from:\s*\S", re.IGNORECASE)
_OUTLOOK_DIVIDER_RE = re.compile(r"^[_\-=]{10,}\s*$")
_QUOTE_PREFIX_RE = re.compile(r"^\s*>+")


def _find_quote_start(lines: list[str]) -> int:
    """Index of the first line that begins a quoted chain, or len(lines)."""
    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue

        if _ORIGINAL_MSG_RE.match(line) or _OUTLOOK_DIVIDER_RE.match(line):
            return i

        # "On <date>, <person> wrote:" — often wrapped across 2-3 lines.
        if line.lower().startswith("on "):
            window = line
            if _ON_WROTE_RE.match(window):
                return i
            for extra in (1, 2):
                if i + extra < len(lines):
                    window = f"{window} {lines[i + extra].strip()}"
                    if _ON_WROTE_RE.match(window):
                        return i

        # Outlook's quote header block: From: / Sent: / To: / Subject:
        if _OUTLOOK_FROM_RE.match(line):
            lookahead = " ".join(x.strip() for x in lines[i : i + 6])
            if re.search(r"\b(sent|to|subject):", lookahead, re.IGNORECASE):
                return i

    return len(lines)


def strip_quoted(text: str) -> str:
    """Keep the top post, drop the quoted chain below it.

    Two passes: cut at the first quote-header marker, then drop any stray
    '>' lines above it. Dropping '>' lines individually rather than cutting
    at the first one preserves interleaved (bottom-post) replies.

    Signature blocks are deliberately left in — they are short and often
    carry useful context (titles, org, phone).
    """
    if not text:
        return ""

    lines = text.splitlines()
    cut = _find_quote_start(lines)
    kept = [line for line in lines[:cut] if not _QUOTE_PREFIX_RE.match(line)]
    return collapse_whitespace("\n".join(kept))


def collapse_whitespace(text: str) -> str:
    """Trim trailing spaces and squeeze runs of blank lines down to one."""
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    blank = 0
    for line in lines:
        if line.strip():
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip()


def extract_body(payload: dict) -> str:
    """Best available body text: prefer text/plain, fall back to HTML."""
    parts = _collect_parts(payload)

    plain = "\n".join(p for p in parts["text/plain"] if p.strip())
    if plain.strip():
        return strip_quoted(plain)

    html = "\n".join(p for p in parts["text/html"] if p.strip())
    if html.strip():
        return strip_quoted(strip_html(html))

    return ""


# --- top level ---------------------------------------------------------


def normalize_message(raw: dict, account_label: str) -> NormalizedMessage:
    """Convert one Gmail API message resource into a NormalizedMessage."""
    payload = raw.get("payload", {}) or {}

    sender_name, sender_email = parse_sender(header_value(payload, "From"))
    subject = decode_mime_header(header_value(payload, "Subject"))

    internal_date_ms = int(raw.get("internalDate") or 0)
    # Stored naive-UTC: SQLite has no tz type, and mixing aware and naive
    # values in comparisons is a reliable source of bugs later.
    timestamp = datetime.fromtimestamp(internal_date_ms / 1000, tz=UTC).replace(
        tzinfo=None
    )

    return NormalizedMessage(
        gmail_id=raw.get("id", ""),
        thread_id=raw.get("threadId", ""),
        account_label=account_label,
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
        body_text=extract_body(payload),
        timestamp=timestamp,
        internal_date_ms=internal_date_ms,
        headers=extract_headers(payload),
    )
