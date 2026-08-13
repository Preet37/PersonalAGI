"""The Event layer (ARCHITECTURE.md D1), and the boundary it exists to create.

The claim D1 makes is "adding a source means writing one adapter; everything
downstream is untouched". That claim is only true if downstream genuinely
cannot see source concepts, so the layering is asserted here rather than
trusted — a stray `sender_email` in the context store would silently make the
iMessage adapter a second pipeline instead of a second adapter.
"""

import ast
import pathlib

import pytest

from personalagi.adapters.base import CC, FROM, TO, resolve_participant
from personalagi.adapters.gmail_adapter import event_from_message
from personalagi.events import EventView
from personalagi.models import Message
from tests.factories import make_view

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "personalagi"

# Names that only make sense for ONE source. Any of these below the adapter
# layer means that layer has learned what email is.
#
# `sender_name` / `sender_address` are deliberately NOT here. Every source has
# someone who produced the event — an iMessage has a sender, a calendar invite
# has an organiser — so those are canonical vocabulary on EventView, not a
# leak. The test would be theatre if it banned the words the abstraction is
# supposed to provide.
SOURCE_CONCEPTS = (
    "gmail_id",
    "internal_date_ms",
    "body_text",
    "headers_json",
    "thread_id",
)

# Modules allowed to know about Gmail: the adapter, the ingest pipeline that
# fills the landing-zone table, and the model definitions themselves.
ALLOWED_PREFIXES = ("adapters/", "ingest/", "models.py", "identity.py")


def _downstream_modules():
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        if any(rel.startswith(prefix) for prefix in ALLOWED_PREFIXES):
            continue
        yield rel, path


class TestLayering:
    def test_no_downstream_module_reads_a_gmail_shaped_attribute(self):
        """The constraint that makes D1 real rather than aspirational."""
        offenders: list[str] = []

        for rel, path in _downstream_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in SOURCE_CONCEPTS:
                    offenders.append(f"{rel}:{node.lineno} .{node.attr}")

        assert offenders == [], "source concepts leaked below the adapter layer:\n" + "\n".join(
            offenders
        )

    def test_no_downstream_module_imports_the_message_table(self):
        offenders: list[str] = []

        for rel, path in _downstream_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if any(alias.name == "Message" for alias in node.names):
                        offenders.append(f"{rel}:{node.lineno}")

        assert offenders == [], f"Message imported below the adapter layer: {offenders}"


class TestGmailAdapter:
    def message(self, **overrides) -> Message:
        from datetime import datetime

        base = dict(
            id=1, gmail_id="g1", thread_id="t1", account_label="personal",
            sender_name="Karan G", sender_email="Karan@Example.com",
            subject="sponsorship", body_text="hi",
            timestamp=datetime(2026, 8, 13), internal_date_ms=1_786_000_000_000,
            ingested_at=datetime(2026, 8, 13),
        )
        base.update(overrides)
        return Message(**base)

    def test_it_produces_the_canonical_shape(self):
        record = event_from_message(self.message())

        assert record.source == "gmail"
        assert record.source_id == "g1"
        assert record.title == "sponsorship"
        assert record.text == "hi"
        assert record.thread_key == "t1"

    def test_addresses_are_normalised_to_lowercase(self):
        record = event_from_message(self.message())

        assert record.sender.address == "karan@example.com"

    def test_recipients_become_participants_with_roles(self):
        import json

        record = event_from_message(
            self.message(
                headers_json=json.dumps(
                    {"to": "A <a@x.com>, b@x.com", "cc": "C <c@x.com>"}
                )
            )
        )

        roles = {p.address: p.role for p in record.participants}
        assert roles["karan@example.com"] == FROM
        assert roles["a@x.com"] == TO
        assert roles["b@x.com"] == TO
        assert roles["c@x.com"] == CC

    def test_gmail_structure_survives_in_metadata(self):
        """D1's stated mitigation for lowest-common-denominator loss."""
        record = event_from_message(self.message())

        assert record.metadata["gmail_thread_id"] == "t1"

    def test_the_sender_is_not_duplicated_as_a_recipient(self):
        import json

        record = event_from_message(
            self.message(headers_json=json.dumps({"to": "karan@example.com"}))
        )

        assert len(record.participants) == 1

    def test_a_mailing_list_blast_does_not_store_hundreds_of_participants(self):
        import json

        to = ", ".join(f"p{i}@x.com" for i in range(200))
        record = event_from_message(self.message(headers_json=json.dumps({"to": to})))

        assert len(record.participants) <= 20


class TestParticipantResolution:
    def test_a_robot_gets_no_person_slug(self):
        """An empty slug is what stops the vault filling with robot files."""
        resolved = resolve_participant("no-reply@x.com", "Notifications")

        assert resolved.is_automated is True
        assert resolved.person_slug == ""

    def test_the_owner_gets_no_person_slug_either(self):
        resolved = resolve_participant(
            "preet@example.com", "Preet", owner_addresses={"preet@example.com"}
        )

        assert resolved.is_owner is True
        assert resolved.person_slug == ""

    def test_a_real_person_gets_one(self):
        resolved = resolve_participant("karan@example.com", "Karan G")

        assert resolved.is_automated is False
        assert resolved.person_slug == "karan-g"

    def test_bulk_headers_only_apply_to_the_sender(self):
        """List-Unsubscribe says the MESSAGE is bulk. It says nothing about
        whoever it was addressed to, who may well be a person."""
        headers = {"list-unsubscribe": "<mailto:x>"}

        sender = resolve_participant("news@x.com", role=FROM, headers=headers)
        recipient = resolve_participant("karan@example.com", role=TO, headers=headers)

        assert sender.is_automated is True
        assert recipient.is_automated is False

    def test_the_reason_is_recorded_so_a_misfire_is_diagnosable(self):
        resolved = resolve_participant(
            "news@x.com", headers={"list-unsubscribe": "<mailto:x>"}
        )

        assert "list-unsubscribe" in resolved.automated_reason


class TestEventView:
    def test_sender_and_counterparty_agree_on_received_mail(self):
        view = make_view(email="karan@example.com", to=[("", "preet@example.com")])

        assert view.sender_address == "karan@example.com"
        assert view.counterparty.address == "karan@example.com"

    def test_an_event_with_no_participants_does_not_crash(self):
        """Defensive: a source could produce an event with no resolvable
        parties, and every accessor has to degrade rather than raise."""
        view = EventView(event=make_view().event, participants=[])

        assert view.sender is None
        assert view.sender_address == ""
        assert view.person_slug == ""
        assert view.counterparty is None
        assert view.sent_by_owner is False
        assert view.from_automated is False

    def test_metadata_survives_a_corrupt_json_blob(self):
        view = make_view()
        view.event.metadata_json = "{not json"

        assert view.metadata == {}

    @pytest.mark.parametrize("blob", [None, "", "[]", "null"])
    def test_metadata_is_always_a_dict(self, blob):
        view = make_view()
        view.event.metadata_json = blob

        assert view.metadata == {}
