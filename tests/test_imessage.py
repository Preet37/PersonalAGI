"""iMessage adapter: read-only discipline, text recovery, and identity merging.

The interesting assertions are not "it parses SQLite". They are:

  1. The live database is never opened.
  2. The 12% of messages whose text lives only in an archived blob are not
     silently dropped.
  3. A phone number resolves to the same person file an email address built —
     which is the only evidence that D1's one-Event-type claim is real.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from personalagi.adapters.imessage import (
    APPLE_EPOCH_OFFSET,
    IMessageUnavailable,
    apple_time_to_datetime,
    event_from_row,
    normalize_phone,
    read_rows,
    snapshot,
    text_from_attributed_body,
)
from personalagi.context.people import slug_for

OWNER = {"preet@example.com"}


def build_chat_db(path: Path, rows: list[dict]) -> Path:
    """A minimal chat.db with the tables and columns the adapter reads."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
            date INTEGER, is_from_me INTEGER, service TEXT, handle_id INTEGER
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        """
    )
    conn.execute("INSERT INTO chat VALUES (1, 'chat-guid', 'Group')")
    for i, row in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO handle VALUES (?, ?, 'iMessage')", (i, row["handle"])
        )
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?,?,'iMessage',?)",
            (
                i,
                f"guid-{i}",
                row.get("text"),
                row.get("blob"),
                row.get("date", 700_000_000_000_000_000),
                int(row.get("is_from_me", 0)),
                i,
            ),
        )
        conn.execute("INSERT INTO chat_message_join VALUES (1, ?)", (i,))
    conn.commit()
    conn.close()
    return path


class TestReadOnlyDiscipline:
    def test_a_missing_database_fails_with_a_clear_message(self, tmp_path):
        with pytest.raises(IMessageUnavailable, match="no iMessage database"):
            snapshot(tmp_path / "nope.db", tmp_path / "copy.db")

    def test_the_wal_sidecar_is_copied_too(self, tmp_path):
        """Copying chat.db alone yields a database missing recent messages:
        the WAL holds committed transactions not yet folded in."""
        source = tmp_path / "chat.db"
        build_chat_db(source, [{"handle": "+14155550123", "text": "hi"}])
        source.with_name("chat.db-wal").write_bytes(b"walcontents")

        snapshot(source, tmp_path / "out" / "copy.db")

        assert (tmp_path / "out" / "copy.db-wal").read_bytes() == b"walcontents"

    def test_the_snapshot_is_opened_read_only(self, tmp_path):
        source = build_chat_db(
            tmp_path / "chat.db", [{"handle": "+14155550123", "text": "hi"}]
        )
        copy = snapshot(source, tmp_path / "copy.db")

        conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM message")
        conn.close()

    def test_the_original_is_untouched(self, tmp_path):
        source = build_chat_db(
            tmp_path / "chat.db", [{"handle": "+1415", "text": "hi"}]
        )
        before = source.read_bytes()

        copy = snapshot(source, tmp_path / "copy.db")
        read_rows(copy, since_apple_ns=0, limit=None)

        assert source.read_bytes() == before


class TestTimestamps:
    def test_nanoseconds_since_the_apple_epoch(self):
        result = apple_time_to_datetime(808_325_297_075_472_384)

        assert result.year == 2026 and result.month == 8

    def test_legacy_second_precision_rows_still_parse(self):
        """Pre-10.13 rows stored whole seconds, not nanoseconds. Treating one
        as the other puts the message in 1970 or the year 27000."""
        seconds = int(
            datetime(2015, 6, 1, tzinfo=UTC).timestamp() - APPLE_EPOCH_OFFSET
        )

        assert apple_time_to_datetime(seconds).year == 2015


class TestAttributedBody:
    """25,140 of 212,513 real messages have text ONLY in this blob."""

    def blob(self, text: str) -> bytes:
        body = text.encode()
        return (
            b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01\x40\x84\x84\x84"
            b"NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84"
            b"NSString\x01\x94\x84\x01\x2b" + bytes([len(body)]) + body
        )

    def test_a_short_message_is_recovered(self):
        assert text_from_attributed_body(self.blob("hey are we still on?")) == (
            "hey are we still on?"
        )

    def test_a_long_message_uses_the_two_byte_length(self):
        text = "x" * 400
        body = text.encode()
        blob = (
            b"NSString\x01\x94\x84\x01\x2b\x81"
            + len(body).to_bytes(2, "little")
            + body
        )

        assert text_from_attributed_body(blob) == text

    def test_an_unparseable_blob_returns_empty_rather_than_garbage(self):
        assert text_from_attributed_body(b"\x00\x01\x02not an archive") == ""

    def test_none_is_handled(self):
        assert text_from_attributed_body(None) == ""


class TestPhoneNormalisation:
    @pytest.mark.parametrize(
        "raw",
        ["+14155550123", "(415) 555-0123", "415-555-0123", "+1 415 555 0123"],
    )
    def test_every_common_rendering_agrees(self, raw):
        assert normalize_phone(raw) == "4155550123"

    def test_a_short_string_does_not_crash(self):
        assert normalize_phone("911") == "911"

    def test_empty_is_empty(self):
        assert normalize_phone("") == ""


class TestEventConstruction:
    def row(self, **overrides) -> dict:
        base = {
            "ROWID": 1,
            "guid": "guid-1",
            "text": "I'll send the deck tomorrow",
            "attributedBody": None,
            "date": 800_000_000_000_000_000,
            "is_from_me": 0,
            "service": "iMessage",
            "handle_id": "+14155550123",
            "chat_guid": "chat-guid",
            "display_name": "",
        }
        base.update(overrides)
        return base

    def build(self, row, contacts=None):
        return event_from_row(
            row,
            owner_addresses=OWNER,
            contacts=contacts or {},
            owner_name="Preet",
        )

    def test_it_produces_a_canonical_event(self):
        record = self.build(self.row())

        assert record.source == "imessage"
        assert record.source_id == "guid-1"
        assert record.text == "I'll send the deck tomorrow"

    def test_an_imessage_has_no_title_and_does_not_invent_one(self):
        assert self.build(self.row()).title == ""

    def test_an_empty_message_is_skipped_entirely(self):
        """Attachment-only rows, reactions, and system messages. Storing them
        adds empty log lines to person files for no gain."""
        assert self.build(self.row(text=None, attributedBody=None)) is None

    def test_a_row_with_no_handle_is_skipped(self):
        assert self.build(self.row(handle_id="")) is None

    def test_received_messages_put_the_other_person_first(self):
        record = self.build(self.row(is_from_me=0))

        assert record.sender.address == "+14155550123"
        assert record.sender.is_owner is False

    def test_sent_messages_put_the_owner_as_sender(self):
        record = self.build(self.row(is_from_me=1))

        assert record.sender.is_owner is True

    def test_text_is_recovered_from_the_blob_when_text_is_null(self):
        blob = TestAttributedBody().blob("from the archive")
        record = self.build(self.row(text=None, attributedBody=blob))

        assert record.text == "from the archive"


class TestCrossSourceIdentity:
    """The only evidence that D1's claim is real rather than aspirational."""

    def test_a_named_contact_gets_the_same_slug_email_would_produce(self):
        record = event_from_row(
            {
                "ROWID": 1, "guid": "g", "text": "hi", "attributedBody": None,
                "date": 800_000_000_000_000_000, "is_from_me": 0,
                "service": "iMessage", "handle_id": "+14155550123",
                "chat_guid": "c", "display_name": "",
            },
            owner_addresses=OWNER,
            contacts={"4155550123": "Karan Gupta"},
            owner_name="Preet",
        )

        # This is the merge: the email pipeline slugs Karan from his display
        # name, and so does this. Same slug means the same markdown file.
        assert record.sender.person_slug == "karan-gupta"
        assert slug_for("Karan Gupta", "karan@example.com") == "karan-gupta"

    def test_an_unknown_number_becomes_its_own_orphan_and_is_not_guessed_at(self):
        record = event_from_row(
            {
                "ROWID": 1, "guid": "g", "text": "hi", "attributedBody": None,
                "date": 800_000_000_000_000_000, "is_from_me": 0,
                "service": "iMessage", "handle_id": "+14155559999",
                "chat_guid": "c", "display_name": "",
            },
            owner_addresses=OWNER,
            contacts={},
            owner_name="Preet",
        )

        # No contact match, so no name. It gets a slug from the number rather
        # than being attached to whichever person seems plausible.
        assert record.sender.person_slug == slug_for("", "+14155559999")

    def test_the_owner_never_gets_a_person_file_from_their_own_messages(self):
        record = event_from_row(
            {
                "ROWID": 1, "guid": "g", "text": "hi", "attributedBody": None,
                "date": 800_000_000_000_000_000, "is_from_me": 1,
                "service": "iMessage", "handle_id": "+14155550123",
                "chat_guid": "c", "display_name": "",
            },
            owner_addresses=OWNER,
            contacts={"4155550123": "Karan Gupta"},
            owner_name="Preet",
        )

        owner = record.sender
        assert owner.is_owner is True
        assert owner.person_slug == ""


class TestReading:
    def test_the_since_bound_is_applied(self, tmp_path):
        source = build_chat_db(
            tmp_path / "chat.db",
            [
                {"handle": "+1415", "text": "old", "date": 100_000_000_000_000_000},
                {"handle": "+1415", "text": "new", "date": 800_000_000_000_000_000},
            ],
        )
        copy = snapshot(source, tmp_path / "copy.db")

        rows = read_rows(copy, since_apple_ns=500_000_000_000_000_000, limit=None)

        assert [r["text"] for r in rows] == ["new"]

    def test_rows_come_back_newest_first(self, tmp_path):
        source = build_chat_db(
            tmp_path / "chat.db",
            [
                {"handle": "+1415", "text": "a", "date": 100_000_000_000_000_000},
                {"handle": "+1415", "text": "b", "date": 800_000_000_000_000_000},
            ],
        )
        copy = snapshot(source, tmp_path / "copy.db")

        rows = read_rows(copy, since_apple_ns=0, limit=None)

        assert [r["text"] for r in rows] == ["b", "a"]
