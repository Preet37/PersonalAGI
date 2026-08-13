"""Calendar adapter: built and tested, deliberately not authorized.

Everything except the network call is exercised here, so granting the scope in
the morning is one command rather than a debugging session. The first test is
the important one: this module must never start an OAuth flow by itself.
"""

from datetime import datetime

import pytest

from personalagi.adapters.base import ATTENDEE, FROM
from personalagi.adapters.calendar import (
    CalendarNotAuthorized,
    event_from_item,
    event_text,
    parse_timestamp,
    sync_calendar,
)
from personalagi.config import Settings

OWNER = {"preet@example.com"}


def item(**overrides) -> dict:
    base = {
        "id": "evt-1",
        "status": "confirmed",
        "summary": "Sponsor sync",
        "description": "Walk through the prospectus",
        "location": "Zoom",
        "start": {"dateTime": "2026-08-15T17:00:00Z"},
        "end": {"dateTime": "2026-08-15T17:30:00Z"},
        "organizer": {"email": "Karan@Example.com", "displayName": "Karan Gupta"},
        "attendees": [
            {"email": "preet@example.com", "self": True, "responseStatus": "accepted"},
            {"email": "sisi@example.com", "displayName": "Sisi Chen"},
        ],
    }
    base.update(overrides)
    return base


class TestNoAuthFlowEverRuns:
    def test_it_refuses_rather_than_authorizing(self, tmp_path):
        """The constraint for this stage. An OAuth flow opens a browser and
        asks a human to grant a scope; neither belongs in an unattended run."""
        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'c.db'}", tokens_dir=tmp_path
        )

        with pytest.raises(CalendarNotAuthorized, match="not\n *authorized|no calendar token"):
            sync_calendar(settings)

    def test_the_error_says_exactly_how_to_authorize(self, tmp_path):
        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'c.db'}", tokens_dir=tmp_path
        )

        with pytest.raises(CalendarNotAuthorized) as excinfo:
            sync_calendar(settings)

        assert "personalagi auth calendar" in str(excinfo.value)
        assert "readonly" in str(excinfo.value)

    def test_offline_items_need_no_token_at_all(self, tmp_path):
        """The path the tests use, and the path a dry run would use."""
        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'c.db'}", tokens_dir=tmp_path
        )

        result = sync_calendar(settings, items=[item()])

        assert result.events == 1


class TestTimestamps:
    def test_a_timed_event_is_stored_as_naive_utc(self):
        parsed = parse_timestamp({"dateTime": "2026-08-15T17:00:00Z"})

        assert parsed == datetime(2026, 8, 15, 17, 0)
        assert parsed.tzinfo is None

    def test_an_offset_is_converted_not_discarded(self):
        """Dropping the offset instead of converting shifts the event by hours
        and would put a 9am meeting in the previous day's brief."""
        assert parse_timestamp({"dateTime": "2026-08-15T17:00:00-07:00"}) == datetime(
            2026, 8, 16, 0, 0
        )

    def test_an_all_day_event_sits_at_midnight(self):
        """A date is not a time. Picking 09:00 would invent information."""
        assert parse_timestamp({"date": "2026-08-15"}) == datetime(2026, 8, 15, 0, 0)

    @pytest.mark.parametrize("node", [{}, None, {"dateTime": "not a date"}, {"date": "x"}])
    def test_unparseable_nodes_return_none_rather_than_guessing(self, node):
        assert parse_timestamp(node) is None


class TestEventConstruction:
    def test_the_canonical_fields_map_over(self):
        record = event_from_item(item(), owner_addresses=OWNER)

        assert record.source == "calendar"
        assert record.source_id == "evt-1"
        assert record.title == "Sponsor sync"
        assert record.timestamp == datetime(2026, 8, 15, 17, 0)

    def test_location_and_call_link_survive_into_the_text(self):
        text = event_text(item(hangoutLink="https://meet.example/abc"))

        assert "Walk through the prospectus" in text
        assert "Location: Zoom" in text
        assert "Call: https://meet.example/abc" in text

    def test_the_organizer_is_the_sender(self):
        record = event_from_item(item(), owner_addresses=OWNER)

        assert record.sender.role == FROM
        assert record.sender.address == "karan@example.com"  # normalised

    def test_attendees_become_participants(self):
        record = event_from_item(item(), owner_addresses=OWNER)

        roles = {p.address: p.role for p in record.participants}
        assert roles["sisi@example.com"] == ATTENDEE
        assert roles["preet@example.com"] == ATTENDEE

    def test_the_owner_is_flagged_and_gets_no_person_file(self):
        record = event_from_item(item(), owner_addresses=OWNER)
        owner = next(p for p in record.participants if p.address == "preet@example.com")

        assert owner.is_owner is True
        assert owner.person_slug == ""

    def test_a_real_attendee_resolves_to_the_same_slug_email_would_build(self):
        """Cross-source identity again: a calendar invite and an email from the
        same person land in one file."""
        record = event_from_item(item(), owner_addresses=OWNER)
        sisi = next(p for p in record.participants if p.address == "sisi@example.com")

        assert sisi.person_slug == "sisi-chen"

    def test_rooms_and_equipment_are_not_people(self):
        record = event_from_item(
            item(
                attendees=[
                    {"email": "room-4@resource.calendar.google.com", "resource": True},
                    {"email": "sisi@example.com", "displayName": "Sisi Chen"},
                ]
            ),
            owner_addresses=OWNER,
        )

        addresses = {p.address for p in record.participants}
        assert "room-4@resource.calendar.google.com" not in addresses

    def test_the_organizer_is_not_duplicated_as_an_attendee(self):
        record = event_from_item(
            item(attendees=[{"email": "karan@example.com"}]), owner_addresses=OWNER
        )

        assert len(record.participants) == 1

    def test_rsvp_status_is_kept_in_metadata(self):
        record = event_from_item(item(), owner_addresses=OWNER)

        assert record.metadata["self_response"] == "accepted"

    def test_recurring_instances_share_one_thread(self):
        """Otherwise a weekly standup is fifty unrelated events."""
        a = event_from_item(
            item(id="i1", recurringEventId="series-9"), owner_addresses=OWNER
        )
        b = event_from_item(
            item(id="i2", recurringEventId="series-9"), owner_addresses=OWNER
        )

        assert a.thread_key == b.thread_key == "series-9"
        assert a.source_id != b.source_id

    def test_a_cancelled_event_is_dropped(self):
        assert event_from_item(item(status="cancelled"), owner_addresses=OWNER) is None

    def test_an_event_with_no_start_is_dropped_rather_than_placed(self):
        """Guessing a time would put a fabricated commitment in the brief."""
        assert event_from_item(item(start={}), owner_addresses=OWNER) is None

    def test_an_all_day_event_is_flagged(self):
        record = event_from_item(
            item(start={"date": "2026-08-15"}, end={"date": "2026-08-16"}),
            owner_addresses=OWNER,
        )

        assert record.metadata["all_day"] is True


class TestSync:
    def settings(self, tmp_path):
        return Settings(
            database_url=f"sqlite:///{tmp_path / 'c.db'}", tokens_dir=tmp_path
        )

    def test_counts_are_reported(self, tmp_path):
        result = sync_calendar(
            self.settings(tmp_path),
            items=[item(id="a"), item(id="b", status="cancelled"), item(id="c", start={})],
        )

        assert result.considered == 3
        assert result.events == 1
        assert result.skipped_cancelled == 1
        assert result.skipped_no_start == 1

    def test_re_running_does_not_duplicate(self, tmp_path):
        from personalagi import db as db_module

        db_module._engine = None
        settings = self.settings(tmp_path)
        sync_calendar(settings, items=[item()])
        sync_calendar(settings, items=[item()])

        from sqlmodel import Session, select

        from personalagi.models import Event

        with Session(db_module.get_engine(settings)) as session:
            rows = list(session.execute(select(Event)).scalars())
        db_module._engine = None

        assert len(rows) == 1

    def test_an_empty_calendar_is_not_an_error(self, tmp_path):
        assert sync_calendar(self.settings(tmp_path), items=[]).events == 0
