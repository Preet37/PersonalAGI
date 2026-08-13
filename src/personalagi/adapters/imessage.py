"""iMessage -> Event. The second source, and the test of whether D1 holds.

Nothing downstream changes to support this. The context store, FTS index,
compaction, relevance scoring, and commitment extraction all consumed Events
before this file existed and consume them after. If that claim were false, this
module would need its own filtering, its own person-file logic, and its own
retrieval — a second pipeline rather than a second adapter.

READ-ONLY, ALWAYS
`chat.db` is a live database macOS writes to continuously. This module never
opens it. It snapshots the file (plus its -wal and -shm sidecars, or the WAL's
uncommitted transactions are lost and the copy is torn), then opens the COPY
with `mode=ro`. Two independent guards, because a corrupted message history is
not recoverable and the cost of the second guard is one URI parameter.

IDENTITY IS THE POINT
An email address resolves a person directly. A phone number does not — it
matches nothing in a vault built from mail. Contacts supplies the missing hop:
+1415... -> "Karan Gupta" -> slug `karan-gupta` -> the SAME person file the
email pipeline already created. Without Contacts, a phone number can only ever
be its own orphan file, and the cross-source premise fails quietly.
"""

from __future__ import annotations

import glob
import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlmodel import Session

from personalagi.adapters.base import FROM, TO, EventRecord, resolve_participant
from personalagi.adapters.gmail_adapter import _upsert_events, _upsert_participants
from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.models import Event

log = logging.getLogger(__name__)

SOURCE = "imessage"
ACCOUNT_LABEL = "imessage"

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
CONTACTS_GLOB = (
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb"
)

# Apple stores message timestamps as nanoseconds since 2001-01-01 UTC.
APPLE_EPOCH_OFFSET = 978_307_200
# Older rows (pre-10.13) used whole seconds. Anything below this threshold is
# seconds, not nanoseconds — a 2001-era nanosecond value is implausibly small.
NANOSECOND_THRESHOLD = 1_000_000_000_000


class IMessageUnavailable(RuntimeError):
    """chat.db cannot be read. Almost always missing Full Disk Access."""


@dataclass
class IMessageResult:
    considered: int = 0
    events: int = 0
    participants: int = 0
    skipped_empty: int = 0
    contacts_matched: int = 0
    contacts_loaded: int = 0

    def summary(self) -> str:
        return (
            f"imessage: considered={self.considered} events={self.events} "
            f"participants={self.participants} skipped_empty={self.skipped_empty}\n"
            f"  contacts: {self.contacts_loaded} loaded, "
            f"{self.contacts_matched} handle(s) resolved to a name"
        )


# --- snapshot ----------------------------------------------------------


def snapshot(source: Path, dest: Path) -> Path:
    """Copy chat.db and its WAL sidecars, then return the copy's path.

    The -wal file holds committed transactions not yet folded into the main
    database. Copying chat.db alone gives a database that is missing recent
    messages and may be internally inconsistent.
    """
    if not source.exists():
        raise IMessageUnavailable(f"no iMessage database at {source}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, dest)
        for suffix in ("-wal", "-shm"):
            sidecar = source.with_name(source.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, dest.with_name(dest.name + suffix))
    except PermissionError as exc:
        raise IMessageUnavailable(
            f"cannot read {source}: macOS is denying access.\n"
            "Grant Full Disk Access to your terminal:\n"
            "  System Settings -> Privacy & Security -> Full Disk Access\n"
            "then quit and reopen the terminal and run this again."
        ) from exc
    return dest


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


# --- contacts ----------------------------------------------------------


def normalize_phone(value: str) -> str:
    """Reduce a phone number to a comparable key.

    iMessage stores E.164 (+14155550123); Contacts stores whatever the user
    typed ((415) 555-0123, 415-555-0123, +1 415 555 0123). Comparing the last
    10 digits matches all of these without a country-code library, and is
    correct for NANP numbers. It will collide for international numbers that
    share a 10-digit suffix, which is rare enough to accept and noted here so
    it is not mistaken for a guarantee.
    """
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def load_contacts() -> dict[str, str]:
    """Map normalized phone / lowercased email -> display name.

    Best-effort. A missing or unreadable Contacts database degrades to phone
    numbers as their own identity rather than failing the run.
    """
    matches = glob.glob(str(Path(CONTACTS_GLOB).expanduser()))
    if not matches:
        log.warning("no Contacts database found; phone numbers cannot be named")
        return {}

    mapping: dict[str, str] = {}
    for path in matches:
        try:
            conn = _connect_readonly(Path(path))
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(
                "SELECT p.ZFULLNUMBER, r.ZFIRSTNAME, r.ZLASTNAME "
                "FROM ZABCDPHONENUMBER p "
                "JOIN ZABCDRECORD r ON r.Z_PK = p.ZOWNER"
            ).fetchall()
            for number, first, last in rows:
                name = " ".join(part for part in (first, last) if part).strip()
                key = normalize_phone(number or "")
                if key and name:
                    mapping.setdefault(key, name)

            rows = conn.execute(
                "SELECT e.ZADDRESS, r.ZFIRSTNAME, r.ZLASTNAME "
                "FROM ZABCDEMAILADDRESS e "
                "JOIN ZABCDRECORD r ON r.Z_PK = e.ZOWNER"
            ).fetchall()
            for address, first, last in rows:
                name = " ".join(part for part in (first, last) if part).strip()
                if address and name:
                    mapping.setdefault(address.strip().lower(), name)
        except sqlite3.Error as exc:
            log.warning("could not read contacts at %s: %s", path, exc)
        finally:
            conn.close()

    log.info("loaded %d contact identifier(s)", len(mapping))
    return mapping


# --- message text ------------------------------------------------------

# Modern macOS stores the body in `attributedBody` (an NSAttributedString
# archive) and leaves `text` NULL — 25,140 of 212,513 messages in the real
# database. Dropping those would silently lose 12% of every conversation, so
# the readable run is pulled out of the archive rather than parsed properly:
# NSKeyedUnarchiver is not available off-platform and the payload we want is a
# plain string with a length-prefixed header.
_STREAMTYPED_RE = re.compile(rb"NSString\x01\x94\x84\x01\x2b(.)", re.DOTALL)


def text_from_attributed_body(blob: bytes | None) -> str:
    """Best-effort extraction of the readable string from an archived body."""
    if not blob:
        return ""
    match = _STREAMTYPED_RE.search(blob)
    if not match:
        return ""
    start = match.end()
    length = blob[start - 1]
    if length == 0x81:  # 2-byte little-endian length follows
        length = int.from_bytes(blob[start : start + 2], "little")
        start += 2
    elif length == 0x82:  # 4-byte
        length = int.from_bytes(blob[start : start + 4], "little")
        start += 4
    try:
        return blob[start : start + length].decode("utf-8", errors="replace").strip()
    except (ValueError, IndexError):
        return ""


def apple_time_to_datetime(value: int) -> datetime:
    seconds = value / 1e9 if value > NANOSECOND_THRESHOLD else float(value)
    return datetime.fromtimestamp(seconds + APPLE_EPOCH_OFFSET, tz=UTC).replace(
        tzinfo=None
    )


# --- reading -----------------------------------------------------------

_READ_SQL = """
SELECT m.ROWID, m.guid, m.text, m.attributedBody, m.date, m.is_from_me,
       m.service, h.id AS handle_id, c.guid AS chat_guid, c.display_name
FROM message m
LEFT JOIN handle h ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cmj.chat_id
WHERE m.date >= :since
ORDER BY m.date DESC
"""


def read_rows(db_path: Path, *, since_apple_ns: int, limit: int | None) -> list[dict]:
    conn = _connect_readonly(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sql = _READ_SQL + (" LIMIT :limit" if limit else "")
        params = {"since": since_apple_ns, "limit": limit or -1}
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def event_from_row(
    row: dict,
    *,
    owner_addresses: set[str],
    contacts: dict[str, str],
    owner_name: str,
) -> EventRecord | None:
    """One iMessage row -> one Event, or None when there is nothing to store."""
    body = (row.get("text") or "").strip() or text_from_attributed_body(
        row.get("attributedBody")
    )
    if not body:
        # Attachment-only messages, reactions, and system rows. Storing them
        # would add empty log lines to person files for no gain.
        return None

    handle = (row.get("handle_id") or "").strip()
    if not handle:
        return None

    timestamp = apple_time_to_datetime(row.get("date") or 0)
    is_from_me = bool(row.get("is_from_me"))

    record = EventRecord(
        source=SOURCE,
        source_id=row.get("guid") or f"rowid:{row.get('ROWID')}",
        account_label=ACCOUNT_LABEL,
        thread_key=row.get("chat_guid") or handle,
        # An iMessage has no subject. Leaving this empty is the honest answer;
        # entry_text_for falls back to the first line of the body.
        title="",
        text=body,
        timestamp=timestamp,
        timestamp_ms=int(timestamp.replace(tzinfo=UTC).timestamp() * 1000),
        metadata={
            "service": row.get("service") or "",
            "chat_display_name": row.get("display_name") or "",
            "is_from_me": is_from_me,
        },
    )

    # Contacts is what turns a phone number into a person the email pipeline
    # already knows. Without a name, slug_for falls back to the local part,
    # and a phone number becomes its own orphan file.
    key = handle.lower() if "@" in handle else normalize_phone(handle)
    other_name = contacts.get(key, "")

    other = resolve_participant(
        handle,
        other_name,
        role=TO if is_from_me else FROM,
        owner_addresses=owner_addresses,
    )
    owner = resolve_participant(
        next(iter(sorted(owner_addresses)), "owner@local"),
        owner_name,
        role=FROM if is_from_me else TO,
        owner_addresses=owner_addresses,
    )
    record.participants = [owner, other] if is_from_me else [other, owner]
    return record


def sync_imessage(
    settings: Settings | None = None,
    *,
    since: datetime | None = None,
    days: int = 90,
    limit: int | None = None,
    db_path: Path | None = None,
    snapshot_dir: Path | None = None,
) -> IMessageResult:
    """Snapshot chat.db, project it into Events, and store them."""
    settings = settings or get_settings()
    init_db(settings)

    source = db_path or CHAT_DB
    target_dir = snapshot_dir or (Path(settings.database_url.split("///")[-1]).parent)
    copy = snapshot(source, target_dir / "chat.snapshot.db")

    result = IMessageResult()
    contacts = load_contacts()
    result.contacts_loaded = len(contacts)

    if since is None:
        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    since_apple_ns = int(
        (since.replace(tzinfo=UTC).timestamp() - APPLE_EPOCH_OFFSET) * 1e9
    )

    rows = read_rows(copy, since_apple_ns=since_apple_ns, limit=limit)
    result.considered = len(rows)
    if not rows:
        log.info("no iMessage rows in the requested window")
        return result

    owner_addresses = settings.owner_address_set or {"owner@local"}
    owner_name = settings.owner_name or "me"

    records: list[EventRecord] = []
    for row in rows:
        record = event_from_row(
            row,
            owner_addresses=owner_addresses,
            contacts=contacts,
            owner_name=owner_name,
        )
        if record is None:
            result.skipped_empty += 1
            continue
        if any(p.display_name and not p.is_owner for p in record.participants):
            result.contacts_matched += 1
        records.append(record)

    now = datetime.now(UTC).replace(tzinfo=None)

    # Events are keyed on (source, account_label, source_id) and upserted, so
    # re-running is idempotent. Unlike Gmail there is no id to inherit, so ids
    # are assigned by SQLite; nothing downstream depends on their value.
    for start in range(0, len(records), 200):
        batch = records[start : start + 200]
        with Session(get_engine(settings)) as session:
            result.events += _upsert_events(
                session, [r.as_row(now) for r in batch]
            )
            session.commit()

            ids = dict(
                session.execute(
                    select(Event.source_id, Event.id)
                    .where(Event.source == SOURCE)
                    .where(Event.source_id.in_([r.source_id for r in batch]))
                ).all()
            )
            participant_rows = [
                p.as_row(ids[r.source_id])
                for r in batch
                if r.source_id in ids
                for p in r.participants
            ]
            result.participants += _upsert_participants(session, participant_rows)
            session.commit()
        log.info(
            "imessage %d/%d", min(start + len(batch), len(records)), len(records)
        )

    log.info(result.summary())
    return result
