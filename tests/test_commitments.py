"""Commitment tracking: the open-loop ledger behind `personalagi owed`.

The rules that matter are about not lying to the owner. A commitment the
system invented, or one it silently reopened after the owner closed it, is
worse than an empty list — it teaches them not to trust the report.
"""

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from personalagi import db as db_module
from personalagi.commitments import (
    DONE,
    OPEN,
    STALE,
    close_commitment,
    list_owed,
    refresh_stale,
    render_owed,
)
from personalagi.config import Settings
from personalagi.models import Commitment
from tests.factories import make_event

NOW = datetime(2026, 8, 13, 2, 0)


@pytest.fixture
def settings(tmp_path):
    db_module._engine = None
    yield Settings(
        database_url=f"sqlite:///{tmp_path / 'c.db'}", commitment_stale_days=7
    )
    db_module._engine = None


def add(settings, **overrides) -> int:
    """Insert one commitment (plus its source message) and return its id."""
    engine = db_module.init_db(settings)
    with Session(engine) as session:
        event = make_event(
            eid=None,
            source_id=overrides.pop("gmail_id", "g1"),
            title="sponsorship",
            text="I'll send you the prospectus this week.",
            ms=int(NOW.timestamp() * 1000),
        )
        session.add(event)
        session.flush()

        base = {
            "event_id": event.id,
            "direction": "i_owe",
            "person_slug": "karan",
            "person_name": "Karan",
            "person_email": "karan@example.com",
            "what": "Send Karan the event prospectus",
            "what_hash": "h1",
            "quote": "I'll send you the prospectus this week.",
            "due_text": "this week",
            "status": OPEN,
            "promised_at": NOW,
            "manually_closed": False,
            "extracted_at": NOW,
        }
        base.update(overrides)
        row = Commitment(**base)
        session.add(row)
        session.commit()
        return row.id


class TestStaleness:
    def test_an_old_open_promise_goes_stale(self, settings):
        add(settings, promised_at=NOW - timedelta(days=10))

        assert refresh_stale(settings, now=NOW) == 1
        assert list_owed(settings)[0].items[0].status == STALE

    def test_a_recent_promise_does_not(self, settings):
        add(settings, promised_at=NOW - timedelta(days=2))

        assert refresh_stale(settings, now=NOW) == 0

    def test_the_boundary_is_the_configured_number_of_days(self, settings):
        add(settings, promised_at=NOW - timedelta(days=7, seconds=1))

        assert refresh_stale(settings, now=NOW) == 1

    def test_a_manually_closed_promise_is_never_restaled(self, settings):
        """The correction mechanism has to survive the nightly job, or it is
        theatre — the same reason person files carry `corrections`."""
        cid = add(settings, promised_at=NOW - timedelta(days=30))
        close_commitment(cid, settings, now=NOW)

        refresh_stale(settings, now=NOW)

        with Session(db_module.get_engine(settings)) as session:
            assert session.get(Commitment, cid).status == DONE

    def test_a_done_promise_is_hidden_by_default_and_shown_on_request(self, settings):
        cid = add(settings)
        close_commitment(cid, settings, now=NOW)

        assert list_owed(settings) == []
        assert len(list_owed(settings, include_done=True)) == 1


class TestGrouping:
    def test_grouped_by_person_with_the_oldest_debt_first(self, settings):
        add(settings, person_slug="daniel", person_name="Daniel",
            promised_at=NOW - timedelta(days=3), gmail_id="g2", what_hash="h2")
        add(settings, person_slug="karan", person_name="Karan",
            promised_at=NOW - timedelta(days=20), gmail_id="g3", what_hash="h3")
        add(settings, person_slug="sisi", person_name="Sisi",
            promised_at=NOW - timedelta(days=9), gmail_id="g4", what_hash="h4")

        groups = list_owed(settings)

        assert [g.person_slug for g in groups] == ["karan", "sisi", "daniel"]

    def test_several_promises_to_one_person_stay_together_oldest_first(self, settings):
        add(settings, promised_at=NOW - timedelta(days=2), gmail_id="g5", what_hash="a")
        add(settings, promised_at=NOW - timedelta(days=8), gmail_id="g6", what_hash="b")

        groups = list_owed(settings)

        assert len(groups) == 1
        assert groups[0].items[0].promised_at < groups[0].items[1].promised_at

    def test_the_two_directions_do_not_mix(self, settings):
        add(settings, direction="i_owe", gmail_id="g7", what_hash="c")
        add(settings, direction="they_owe", gmail_id="g8", what_hash="d")

        assert len(list_owed(settings, direction="i_owe")) == 1
        assert len(list_owed(settings, direction="they_owe")) == 1


class TestDedup:
    def test_re_extracting_the_same_message_does_not_duplicate(self, settings):
        """Re-running relevance over already-scored mail must be idempotent."""
        from personalagi.llm.relevance import _upsert_commitments

        first = add(settings)
        with Session(db_module.get_engine(settings)) as session:
            row = session.get(Commitment, first)
            duplicate = {
                "event_id": row.event_id,
                "direction": row.direction,
                "person_slug": row.person_slug,
                "person_name": row.person_name,
                "person_email": row.person_email,
                "what": row.what,
                "what_hash": row.what_hash,
                "quote": row.quote,
                "due_text": row.due_text,
                "status": OPEN,
                "promised_at": row.promised_at,
                "manually_closed": False,
                "extracted_at": NOW,
            }
            assert _upsert_commitments(session, [duplicate]) == 0

        with Session(db_module.get_engine(settings)) as session:
            assert len(list(session.execute(select(Commitment)).scalars())) == 1

    def test_re_extraction_cannot_resurrect_a_closed_commitment(self, settings):
        from personalagi.llm.relevance import _upsert_commitments

        cid = add(settings)
        close_commitment(cid, settings, now=NOW)

        with Session(db_module.get_engine(settings)) as session:
            row = session.get(Commitment, cid)
            _upsert_commitments(
                session,
                [{
                    "event_id": row.event_id, "direction": row.direction,
                    "person_slug": row.person_slug, "person_name": row.person_name,
                    "person_email": row.person_email, "what": row.what,
                    "what_hash": row.what_hash, "quote": row.quote,
                    "due_text": "", "status": OPEN, "promised_at": row.promised_at,
                    "manually_closed": False, "extracted_at": NOW,
                }],
            )

        with Session(db_module.get_engine(settings)) as session:
            assert session.get(Commitment, cid).status == DONE

    def test_one_message_can_create_two_different_commitments(self, settings):
        """"I'll send the prospectus once you confirm the date" is two."""
        add(settings, what="Send prospectus", what_hash="x1", gmail_id="g9")
        add(settings, what="Confirm the date", what_hash="x2", gmail_id="g10")

        assert sum(len(g.items) for g in list_owed(settings)) == 2


class TestRendering:
    def test_the_quote_is_always_shown(self, settings):
        """The quote IS the evidence; a report without it is an assertion."""
        add(settings)

        output = render_owed(list_owed(settings))

        assert "I'll send you the prospectus this week." in output
        assert "Send Karan the event prospectus" in output

    def test_stale_items_are_flagged(self, settings):
        add(settings, promised_at=NOW - timedelta(days=30))
        refresh_stale(settings, now=NOW)

        assert "STALE" in render_owed(list_owed(settings))

    def test_an_empty_ledger_says_so_without_claiming_completeness(self, settings):
        """Nothing found must not read as nothing owed — most of this owner's
        real commitments are not in the email corpus at all."""
        output = render_owed([])

        assert "Nothing" in output
        assert "detected" in output  # hedged on purpose

    def test_ids_are_shown_so_the_close_command_is_usable(self, settings):
        cid = add(settings)

        assert f"[{cid}]" in render_owed(list_owed(settings))


def test_closing_a_missing_commitment_reports_failure(settings):
    db_module.init_db(settings)
    assert close_commitment(999, settings) is False
