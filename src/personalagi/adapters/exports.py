"""Stage 30: export files as Events. Claude, ChatGPT, Gemini, WhatsApp, LinkedIn.

There is no live API for "what did I discuss with Claude about DJ". All three
assistants offer a data export and nothing else, so the honest design is a
folder you drop files into, not an integration that pretends to be live. Plan
for the export, rather than being surprised by its absence later.

PROVENANCE IS THE INTERESTING PART HERE
An assistant transcript contains two very different kinds of text. What the
OWNER typed is external — a real record of what he said and wanted. What the
ASSISTANT replied is GENERATED, and if it were citable the system could ground
a confident claim in an answer some model already invented. That is the
self-citation bug with a fresh face, arriving through a new door.

So every turn carries its own provenance, and only the owner's own words are
ever citable. This is also why the export adapters were worth building
carefully rather than quickly: they are the first source where a single file
contains both kinds of record.

FORMATS DRIFT
Every one of these formats is undocumented and changes without notice. Each
parser therefore accepts several shapes, and a file it cannot read raises with
the path and what it expected — silently importing zero conversations from a
format change would look exactly like an empty export.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from personalagi.adapters.base import FROM, TO, EventRecord, resolve_participant
from personalagi.records import Provenance

log = logging.getLogger(__name__)

ASSISTANT_SOURCES = {"claude", "chatgpt", "gemini"}
TEXT_LIMIT = 20_000


class ExportError(RuntimeError):
    """The file could not be read as the format claimed."""


@dataclass
class ExportResult:
    source: str = ""
    conversations: int = 0
    events: int = 0
    owner_turns: int = 0
    assistant_turns: int = 0
    skipped_empty: int = 0
    records: list[EventRecord] = field(default_factory=list)

    def summary(self) -> str:
        detail = ""
        if self.source in ASSISTANT_SOURCES:
            detail = (
                f"\n  {self.owner_turns} owner turn(s) EXTERNAL, "
                f"{self.assistant_turns} assistant turn(s) GENERATED "
                f"(never citable)"
            )
        return (
            f"{self.source}: {self.conversations} conversation(s), "
            f"{self.events} event(s), {self.skipped_empty} empty skipped{detail}"
        )


def _ts(value) -> datetime:
    """Accept epoch seconds, epoch ms, or ISO-8601. Formats drift."""
    if value in (None, ""):
        return datetime.now(UTC).replace(tzinfo=None)
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=UTC).replace(tzinfo=None)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromtimestamp(float(text), tz=UTC).replace(tzinfo=None)
        except (TypeError, ValueError):
            return datetime.now(UTC).replace(tzinfo=None)
    return (
        parsed.astimezone(UTC).replace(tzinfo=None)
        if parsed.tzinfo
        else parsed
    )


def _flatten(content) -> str:
    """Message content is a string, a list of parts, or a dict. All three occur."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "parts" in content:
            return _flatten(content["parts"])
        return str(content.get("text") or content.get("content") or "")
    if isinstance(content, list):
        return "\n".join(_flatten(part) for part in content if part)
    return str(content)


def _turn_record(
    *,
    source: str,
    conversation_id: str,
    title: str,
    index: int,
    role: str,
    text: str,
    when: datetime,
    owner_addresses: set[str],
    owner_name: str,
) -> EventRecord:
    is_owner = role in {"user", "human", "owner"}
    owner_address = next(iter(sorted(owner_addresses)), "owner@local")
    assistant_address = f"{source}@assistant.local"

    record = EventRecord(
        source=source,
        source_id=f"{conversation_id}:{index}",
        account_label=source,
        thread_key=conversation_id,
        title=title,
        text=text[:TEXT_LIMIT],
        timestamp=when,
        timestamp_ms=int(when.replace(tzinfo=UTC).timestamp() * 1000),
        metadata={"role": role, "conversation": conversation_id, "turn": index},
    )
    # THE RULE THAT MATTERS. The owner's own words are a real record of what he
    # said. The assistant's reply is something a model produced, and treating
    # it as evidence would let the system cite an answer it invented.
    record.provenance = (
        Provenance.EXTERNAL if is_owner else Provenance.GENERATED
    )

    speaker = owner_address if is_owner else assistant_address
    listener = assistant_address if is_owner else owner_address
    record.participants = [
        resolve_participant(
            speaker,
            owner_name if is_owner else source.title(),
            role=FROM,
            owner_addresses=owner_addresses,
        ),
        resolve_participant(
            listener,
            source.title() if is_owner else owner_name,
            role=TO,
            owner_addresses=owner_addresses,
        ),
    ]
    return record


# --- assistant exports -------------------------------------------------


def parse_assistant_export(
    path: Path,
    source: str,
    *,
    owner_addresses: set[str],
    owner_name: str = "me",
) -> ExportResult:
    """Claude / ChatGPT / Gemini conversation JSON.

    All three ship an array of conversations, each with a list of turns, under
    keys that differ and change. Rather than three brittle parsers this reads
    the union of shapes seen in the wild and fails loudly on anything else.
    """
    result = ExportResult(source=source)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError(f"{path} is not readable JSON: {exc}") from exc

    conversations = raw if isinstance(raw, list) else raw.get("conversations", raw)
    if isinstance(conversations, dict):
        conversations = [conversations]
    if not isinstance(conversations, list):
        raise ExportError(
            f"{path}: expected a list of conversations, got {type(raw).__name__}"
        )

    for conversation in conversations:
        if not isinstance(conversation, dict):
            continue
        cid = str(
            conversation.get("uuid")
            or conversation.get("id")
            or conversation.get("conversation_id")
            or f"conv{result.conversations}"
        )
        title = str(conversation.get("name") or conversation.get("title") or "")
        turns = (
            conversation.get("chat_messages")
            or conversation.get("messages")
            or conversation.get("mapping")
            or []
        )
        # ChatGPT's `mapping` is a dict of nodes keyed by id.
        if isinstance(turns, dict):
            turns = [
                node.get("message")
                for node in turns.values()
                if isinstance(node, dict) and node.get("message")
            ]

        result.conversations += 1
        for index, turn in enumerate(turns):
            if not isinstance(turn, dict):
                continue
            role = str(
                turn.get("sender")
                or turn.get("role")
                or (turn.get("author") or {}).get("role")
                or "user"
            ).lower()
            text = _flatten(
                turn.get("text") or turn.get("content") or turn.get("parts")
            ).strip()
            if not text:
                result.skipped_empty += 1
                continue

            when = _ts(
                turn.get("created_at")
                or turn.get("create_time")
                or conversation.get("created_at")
            )
            record = _turn_record(
                source=source, conversation_id=cid, title=title, index=index,
                role=role, text=text, when=when,
                owner_addresses=owner_addresses, owner_name=owner_name,
            )
            result.records.append(record)
            result.events += 1
            if record.provenance == Provenance.EXTERNAL:
                result.owner_turns += 1
            else:
                result.assistant_turns += 1

    return result


# --- WhatsApp ----------------------------------------------------------

# "13/08/2026, 14:32 - Karan: text"  and the US/bracketed variants.
_WA_LINE = re.compile(
    r"^\[?(?P<date>\d{1,2}[/.]\d{1,2}[/.]\d{2,4}),?\s+"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap][Mm])?)\]?\s*[-–]?\s*"
    r"(?P<sender>[^:]{1,60}?):\s(?P<text>.*)$"
)
# System lines have no sender and must not become messages from a person.
_WA_SYSTEM = re.compile(
    r"(end-to-end encrypted|created group|added you|changed the subject|"
    r"joined using|security code changed|deleted this message|"
    r"<Media omitted>|This message was deleted)",
    re.IGNORECASE,
)


def _wa_timestamp(date_part: str, time_part: str) -> datetime:
    for fmt in (
        "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M", "%m/%d/%Y %H:%M", "%m/%d/%y %H:%M",
        "%d/%m/%Y %I:%M %p", "%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p",
        "%d.%m.%Y %H:%M",
    ):
        try:
            return datetime.strptime(f"{date_part} {time_part.strip()}", fmt)
        except ValueError:
            continue
    return datetime.now(UTC).replace(tzinfo=None)


def parse_whatsapp_export(
    path: Path, *, owner_addresses: set[str], owner_name: str = "me"
) -> ExportResult:
    """WhatsApp's exported `.txt` chat log.

    Multi-line messages continue on lines that do NOT start with a timestamp,
    so continuation lines are appended to the previous message rather than
    dropped — otherwise every paragraph after the first is silently lost.
    """
    result = ExportResult(source="whatsapp")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ExportError(f"{path} is not readable: {exc}") from exc

    chat = path.stem.replace("WhatsApp Chat with ", "").strip() or "whatsapp"
    owner_lower = owner_name.strip().lower()
    pending: list[tuple[str, datetime, list[str]]] = []

    for line in lines:
        match = _WA_LINE.match(line.strip())
        if match:
            if _WA_SYSTEM.search(match.group("text")):
                continue
            pending.append(
                (
                    match.group("sender").strip(),
                    _wa_timestamp(match.group("date"), match.group("time")),
                    [match.group("text")],
                )
            )
        elif pending and line.strip():
            pending[-1][2].append(line.strip())

    if not pending and lines:
        raise ExportError(
            f"{path}: no WhatsApp messages parsed from {len(lines)} lines. "
            "The export format may have changed."
        )

    result.conversations = 1 if pending else 0
    for index, (sender, when, parts) in enumerate(pending):
        text = "\n".join(parts).strip()
        if not text:
            result.skipped_empty += 1
            continue
        is_owner = sender.lower() == owner_lower
        # WhatsApp exports carry display names, not numbers. Slugging the name
        # is what lets one person's WhatsApp and email land in one file.
        handle = (
            next(iter(sorted(owner_addresses)), "owner@local")
            if is_owner
            else f"{re.sub(r'[^a-z0-9]+', '-', sender.lower()).strip('-')}@whatsapp.local"
        )
        record = EventRecord(
            source="whatsapp", source_id=f"{chat}:{index}", account_label="whatsapp",
            thread_key=chat, title="", text=text[:TEXT_LIMIT], timestamp=when,
            timestamp_ms=int(when.replace(tzinfo=UTC).timestamp() * 1000),
            metadata={"chat": chat, "sender": sender},
        )
        record.participants = [
            resolve_participant(
                handle, sender, role=FROM, owner_addresses=owner_addresses
            )
        ]
        result.records.append(record)
        result.events += 1

    return result


# --- LinkedIn ----------------------------------------------------------


def parse_linkedin_messages(
    path: Path, *, owner_addresses: set[str], owner_name: str = "me"
) -> ExportResult:
    """LinkedIn's `messages.csv` from the data archive.

    This is the source that would have made Arjun visible: connection requests
    and DMs live here and nowhere else, which is exactly why the shared-envelope
    workaround was needed in the first place.
    """
    result = ExportResult(source="linkedin")
    try:
        rows = list(csv.DictReader(path.open(newline="", encoding="utf-8-sig")))
    except OSError as exc:
        raise ExportError(f"{path} is not readable: {exc}") from exc

    if rows and "CONTENT" not in {k.upper() for k in rows[0]}:
        raise ExportError(
            f"{path}: expected a LinkedIn messages.csv with a CONTENT column, "
            f"found {list(rows[0])[:6]}"
        )

    owner_lower = owner_name.strip().lower()
    for index, row in enumerate(rows):
        lookup = {k.upper(): (v or "") for k, v in row.items()}
        text = lookup.get("CONTENT", "").strip()
        if not text:
            result.skipped_empty += 1
            continue
        sender = lookup.get("FROM", "").strip() or "unknown"
        when = _ts(lookup.get("DATE") or lookup.get("DATE SENT"))
        conversation = lookup.get("CONVERSATION ID", "") or f"li{index}"
        is_owner = sender.lower() == owner_lower

        handle = (
            next(iter(sorted(owner_addresses)), "owner@local")
            if is_owner
            else f"{re.sub(r'[^a-z0-9]+', '-', sender.lower()).strip('-')}@linkedin.local"
        )
        record = EventRecord(
            source="linkedin", source_id=f"{conversation}:{index}",
            account_label="linkedin", thread_key=conversation,
            title=lookup.get("SUBJECT", "").strip(), text=text[:TEXT_LIMIT],
            timestamp=when,
            timestamp_ms=int(when.replace(tzinfo=UTC).timestamp() * 1000),
            metadata={"folder": lookup.get("FOLDER", "")},
        )
        record.participants = [
            resolve_participant(
                handle, sender, role=FROM, owner_addresses=owner_addresses
            )
        ]
        result.records.append(record)
        result.events += 1

    result.conversations = len({r.thread_key for r in result.records})
    return result


# --- dispatch ----------------------------------------------------------


def detect_format(path: Path) -> str:
    """Guess the format from the file, not from the filename alone."""
    name = path.name.lower()
    if path.suffix.lower() == ".txt":
        return "whatsapp"
    if path.suffix.lower() == ".csv":
        return "linkedin"
    if path.suffix.lower() != ".json":
        raise ExportError(f"{path}: unsupported export type '{path.suffix}'")

    if "claude" in name:
        return "claude"
    if "chatgpt" in name or "openai" in name:
        return "chatgpt"
    if "gemini" in name or "takeout" in name:
        return "gemini"

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError(f"{path} is not readable JSON: {exc}") from exc
    sample = raw[0] if isinstance(raw, list) and raw else raw
    if isinstance(sample, dict):
        if "chat_messages" in sample:
            return "claude"
        if "mapping" in sample:
            return "chatgpt"
    raise ExportError(
        f"{path}: could not identify the export format. Rename it to include "
        "claude, chatgpt, or gemini."
    )


def parse_export(
    path: Path,
    *,
    owner_addresses: set[str],
    owner_name: str = "me",
    fmt: str | None = None,
) -> ExportResult:
    kind = fmt or detect_format(path)
    if kind == "whatsapp":
        return parse_whatsapp_export(
            path, owner_addresses=owner_addresses, owner_name=owner_name
        )
    if kind == "linkedin":
        return parse_linkedin_messages(
            path, owner_addresses=owner_addresses, owner_name=owner_name
        )
    if kind in ASSISTANT_SOURCES:
        return parse_assistant_export(
            path, kind, owner_addresses=owner_addresses, owner_name=owner_name
        )
    raise ExportError(f"unknown export format '{kind}'")


def ingest_export(
    path: Path,
    settings=None,
    *,
    fmt: str | None = None,
    dry_run: bool = False,
) -> ExportResult:
    """Parse an export and store it as Events."""
    from sqlalchemy import select
    from sqlmodel import Session

    from personalagi.adapters.gmail_adapter import _upsert_events, _upsert_participants
    from personalagi.config import get_settings
    from personalagi.db import get_engine, init_db
    from personalagi.models import Event

    settings = settings or get_settings()
    init_db(settings)
    if not path.exists():
        raise ExportError(f"no such file: {path}")

    result = parse_export(
        path,
        owner_addresses=settings.owner_address_set or {"owner@local"},
        owner_name=settings.owner_name or "me",
        fmt=fmt,
    )
    if dry_run or not result.records:
        return result

    now = datetime.now(UTC).replace(tzinfo=None)
    for start in range(0, len(result.records), 200):
        batch = result.records[start : start + 200]
        with Session(get_engine(settings)) as session:
            _upsert_events(session, [r.as_row(now) for r in batch])
            session.commit()
            ids = dict(
                session.execute(
                    select(Event.source_id, Event.id)
                    .where(Event.source == result.source)
                    .where(Event.source_id.in_([r.source_id for r in batch]))
                ).all()
            )
            _upsert_participants(
                session,
                [
                    p.as_row(ids[r.source_id])
                    for r in batch
                    if r.source_id in ids
                    for p in r.participants
                ],
            )
            session.commit()
    log.info(result.summary())
    return result
