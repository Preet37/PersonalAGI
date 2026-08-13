"""The feedback loop and the investigation loop.

Systems like this do not improve by running. They improve if you capture what
the owner did, store it, and change behaviour because of it. These tests pin
the parts that are easy to get subtly wrong: silence is not a rejection, old
lessons keep counting, and blind spots have to stay visible.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from personalagi import db as db_module
from personalagi.config import Settings
from personalagi.feedback import (
    FeedbackError,
    exploration_sample,
    guidance,
    history,
    mark_ignored,
    outcome_bias,
    record_outcome,
    render_examples,
    similar_proposals,
    similarity,
    stats,
)
from personalagi.investigate import investigate, keywords
from personalagi.models import Commitment, Event, Participant, ProposalRecord
from personalagi.records import BudgetExhausted, CallBudget, Outcome, Provenance

NOW = datetime(2026, 8, 13, 9, 0)


@pytest.fixture
def settings(tmp_path):
    db_module._engine = None
    yield Settings(
        database_url=f"sqlite:///{tmp_path / 'f.db'}",
        context_dir=tmp_path / "ctx",
        feedback_ignore_days=5,
    )
    db_module._engine = None


def session_for(settings):
    return Session(db_module.init_db(settings))


def add_proposal(settings, pid, rationale, *, action="draft_email",
                 outcome=Outcome.PENDING, created=NOW, suppressed=False,
                 note="", confidence=0.7):
    with session_for(settings) as session:
        row = ProposalRecord(
            proposal_id=pid, trigger=f"sweep:{pid}", action_name=action,
            args_json="{}", rationale=rationale, evidence_event_ids="",
            confidence=confidence, permission_tier="approve",
            attention_level="ambient", suppressed=suppressed, outcome=outcome,
            outcome_note=note, created_at=created,
        )
        session.add(row)
        session.commit()
        return row.proposal_id


def add_event(settings, source_id, text, *, people=(), when=NOW,
              provenance=Provenance.EXTERNAL):
    with session_for(settings) as session:
        event = Event(
            source="gmail", source_id=source_id, account_label="personal",
            thread_key="t", title="", text=text, timestamp=when,
            timestamp_ms=int(when.replace(tzinfo=UTC).timestamp() * 1000),
            provenance=provenance, ingested_at=NOW,
        )
        session.add(event)
        session.flush()
        for slug in people:
            session.add(
                Participant(
                    event_id=event.id, address=f"{slug}@example.com",
                    display_name=slug, role="from", person_slug=slug,
                )
            )
        session.commit()
        return event.id


# --- stage 26 ----------------------------------------------------------


class TestLedger:
    def test_recording_an_outcome(self, settings):
        add_proposal(settings, "abc123", "Ask Pratik")

        row = record_outcome("abc123", Outcome.ACCEPTED, settings)

        assert row.outcome == "accepted"
        assert row.outcome_at is not None

    def test_a_short_prefix_works(self, settings):
        """Nobody types a 32-char hex id off a terminal."""
        add_proposal(settings, "abc123def456", "Ask Pratik")

        assert record_outcome("abc1", Outcome.DISMISSED, settings).outcome == "dismissed"

    def test_an_ambiguous_prefix_is_refused_not_guessed(self, settings):
        add_proposal(settings, "abc111", "one")
        add_proposal(settings, "abc222", "two")

        with pytest.raises(FeedbackError, match="matches 2"):
            record_outcome("abc", Outcome.ACCEPTED, settings)

    def test_an_unknown_id_is_refused(self, settings):
        with pytest.raises(FeedbackError, match="no proposal"):
            record_outcome("nope", Outcome.ACCEPTED, settings)

    def test_an_invalid_outcome_is_refused(self, settings):
        add_proposal(settings, "abc", "x")

        with pytest.raises(FeedbackError, match="not an outcome"):
            record_outcome("abc", "sort-of-liked-it", settings)

    def test_an_edit_stores_what_was_actually_sent(self, settings):
        """The most valuable signal in the system: the diff between what it
        wrote and what he sent is a direct, specific lesson."""
        add_proposal(settings, "abc", "Draft a follow-up")

        row = record_outcome(
            "abc", Outcome.EDITED, settings, note="Hey Pratik, quick favour —"
        )

        assert row.outcome_note.startswith("Hey Pratik")


class TestAgingOut:
    def test_an_unanswered_proposal_becomes_ignored(self, settings):
        add_proposal(settings, "old", "x", created=NOW - timedelta(days=10))

        assert mark_ignored(settings, now=NOW) == 1

    def test_a_recent_one_is_left_pending(self, settings):
        add_proposal(settings, "new", "x", created=NOW - timedelta(days=1))

        assert mark_ignored(settings, now=NOW) == 0

    def test_a_suppressed_proposal_never_ages_out(self, settings):
        """It was never shown. Calling it 'ignored' would punish the system for
        the owner not seeing something it deliberately hid."""
        add_proposal(
            settings, "hidden", "x", created=NOW - timedelta(days=30), suppressed=True
        )

        assert mark_ignored(settings, now=NOW) == 0

    def test_an_already_judged_proposal_is_untouched(self, settings):
        add_proposal(
            settings, "judged", "x", created=NOW - timedelta(days=30),
            outcome=Outcome.ACCEPTED,
        )

        mark_ignored(settings, now=NOW)

        assert history(settings)[0].outcome == "accepted"


class TestStats:
    def test_acceptance_is_none_when_nothing_is_judged(self, settings):
        """Reporting 0% for 'no data yet' is a lie in the direction that makes
        the system look worse than it is, which is still a lie."""
        add_proposal(settings, "a", "x")

        assert stats(settings).acceptance_rate is None
        assert "n/a" in stats(settings).summary()

    def test_pending_proposals_are_excluded_from_the_rate(self, settings):
        add_proposal(settings, "a", "x", outcome=Outcome.ACCEPTED)
        add_proposal(settings, "b", "x", outcome=Outcome.DISMISSED)
        add_proposal(settings, "c", "x")  # pending

        assert stats(settings).acceptance_rate == pytest.approx(0.5)

    def test_edited_counts_as_a_success(self, settings):
        add_proposal(settings, "a", "x", outcome=Outcome.EDITED)

        assert stats(settings).acceptance_rate == 1.0


# --- stage 27 ----------------------------------------------------------


class TestSimilarity:
    def test_identical_text_scores_one(self):
        assert similarity("send the prospectus", "send the prospectus") == 1.0

    def test_unrelated_text_scores_zero(self):
        assert similarity("send the prospectus", "dentist appointment") == 0.0

    def test_empty_is_safe(self):
        assert similarity("", "anything") == 0.0


class TestOutcomeConditionedExamples:
    def test_only_judged_proposals_become_examples(self, settings):
        """A pending proposal teaches nothing and would dilute the examples."""
        add_proposal(settings, "judged", "LinkedIn connection request",
                     outcome=Outcome.DISMISSED)
        add_proposal(settings, "pending", "LinkedIn connection request")

        examples = similar_proposals("draft_email", "LinkedIn connection request",
                                     settings, now=NOW)

        assert [e.proposal_id for e in examples] == ["judged"]

    def test_the_most_similar_come_first(self, settings):
        add_proposal(settings, "close", "Send Karan the event prospectus",
                     outcome=Outcome.ACCEPTED)
        add_proposal(settings, "far", "Dentist appointment reminder",
                     outcome=Outcome.ACCEPTED)

        examples = similar_proposals(
            "draft_email", "Send Karan the event prospectus", settings, now=NOW
        )

        assert examples[0].proposal_id == "close"

    def test_an_old_lesson_still_counts(self, settings):
        """Overfitting to recent feedback is a real failure mode. A correction
        from March is still a correction."""
        add_proposal(settings, "ancient", "LinkedIn connection request",
                     outcome=Outcome.DISMISSED,
                     created=NOW - timedelta(days=300))

        examples = similar_proposals(
            "draft_email", "LinkedIn connection request", settings, now=NOW
        )

        assert len(examples) == 1

    def test_a_recent_lesson_outranks_an_identical_old_one(self, settings):
        add_proposal(settings, "old", "LinkedIn connection request",
                     outcome=Outcome.DISMISSED, created=NOW - timedelta(days=300))
        add_proposal(settings, "new", "LinkedIn connection request",
                     outcome=Outcome.DISMISSED, created=NOW - timedelta(days=1))

        examples = similar_proposals(
            "draft_email", "LinkedIn connection request", settings, now=NOW
        )

        assert examples[0].proposal_id == "new"

    def test_the_ids_used_are_returned_so_the_effect_is_measurable(self, settings):
        """Without recording which examples informed a proposal, 'the feedback
        loop is working' is an assertion nobody can check."""
        add_proposal(settings, "abc", "Ask for a letter", outcome=Outcome.ACCEPTED)

        block, used = guidance("draft_email", "Ask for a letter", settings)

        assert used == ["abc"]
        assert "ACCEPTED" in block

    def test_no_history_produces_an_empty_block_not_a_disclaimer(self, settings):
        """A prompt that says there is no track record invites the model to
        comment on that. Absence of history should change nothing."""
        assert render_examples([]) == ""

    def test_an_edit_shows_what_was_actually_sent(self, settings):
        add_proposal(settings, "abc", "Draft a nudge", outcome=Outcome.EDITED,
                     note="Hey — quick one, are you still able to?")

        block, _ = guidance("draft_email", "Draft a nudge", settings)

        assert "what he actually sent" in block


class TestOutcomeBias:
    def test_repeated_dismissals_push_the_signal_negative(self, settings):
        for i in range(4):
            add_proposal(settings, f"d{i}", "LinkedIn connection request",
                         outcome=Outcome.DISMISSED)

        assert outcome_bias("draft_email", "LinkedIn connection request", settings) < 0

    def test_acceptances_push_it_positive(self, settings):
        for i in range(3):
            add_proposal(settings, f"a{i}", "Send the prospectus",
                         outcome=Outcome.ACCEPTED)

        assert outcome_bias("draft_email", "Send the prospectus", settings) > 0

    def test_no_history_is_neutral(self, settings):
        assert outcome_bias("draft_email", "anything at all", settings) == 0.0

    def test_silence_counts_less_than_an_explicit_dismissal(self, settings):
        """He may simply not have looked. Treating that as a firm rejection
        teaches the wrong lesson from the owner being busy."""
        add_proposal(settings, "ig", "Thing A", outcome=Outcome.IGNORED)
        ignored_bias = outcome_bias("draft_email", "Thing A", settings)

        add_proposal(settings, "di", "Thing B", outcome=Outcome.DISMISSED)
        dismissed_bias = outcome_bias("draft_email", "Thing B", settings)

        assert dismissed_bias < ignored_bias < 0


class TestExploration:
    def test_suppressed_proposals_can_be_surfaced_deliberately(self, settings):
        """If the system stops surfacing a class of thing, he never sees it, so
        never corrects it, so the blind spot becomes permanent AND invisible."""
        add_proposal(settings, "hidden", "low confidence thing", suppressed=True)

        assert [r.proposal_id for r in exploration_sample(settings)] == ["hidden"]

    def test_near_misses_come_first(self, settings):
        add_proposal(settings, "far", "x", suppressed=True, confidence=0.1)
        add_proposal(settings, "near", "y", suppressed=True, confidence=0.49)

        assert exploration_sample(settings)[0].proposal_id == "near"

    def test_already_judged_suppressed_items_are_not_reshown(self, settings):
        add_proposal(settings, "done", "x", suppressed=True,
                     outcome=Outcome.DISMISSED)

        assert exploration_sample(settings) == []


# --- stage 28 ----------------------------------------------------------


class TestInvestigate:
    def test_it_follows_a_name_to_the_events_around_it(self, settings):
        add_event(settings, "e1", "Meeting with DJ about the programme",
                  people=["dj-patel"])

        result = investigate("what did we discuss with DJ", settings)

        assert result.event_ids
        assert "dj-patel" in result.people

    def test_people_found_become_new_search_terms(self, settings):
        """The 'that reminds me' step: reading an event teaches you who was on
        it, and those names drive the next round."""
        add_event(settings, "e1", "DJ introduced me to John", people=["dj-patel"])
        add_event(settings, "e2", "John says the programme opens in August",
                  people=["john-mercer"])

        result = investigate("DJ", settings)

        assert any(s.action == "expand" for s in result.steps)

    def test_it_surfaces_open_commitments_for_people_it_found(self, settings):
        event_id = add_event(settings, "e1", "chat with Karan", people=["karan"])
        with session_for(settings) as session:
            session.add(
                Commitment(
                    event_id=event_id, direction="i_owe", person_slug="karan",
                    person_name="Karan", person_email="karan@example.com",
                    what="Send the prospectus", what_hash="h", quote="I'll send it",
                    status="open", promised_at=NOW, manually_closed=False,
                    extracted_at=NOW,
                )
            )
            session.commit()

        result = investigate("Karan", settings)

        assert any("prospectus" in c for c in result.commitments)

    def test_generated_events_are_never_read(self, settings):
        add_event(settings, "made-up", "System summary about Karan",
                  provenance=Provenance.GENERATED)

        result = investigate("Karan summary", settings)

        assert result.event_ids == []

    def test_finding_nothing_says_so_rather_than_answering(self, settings):
        result = investigate("something nobody ever mentioned", settings)

        assert "Found nothing" in result.render()
        assert "channel that is not" in result.render()

    def test_the_iteration_cap_stops_it(self, settings):
        for i in range(30):
            add_event(settings, f"e{i}", f"person{i} met person{i+1}",
                      people=[f"person-{i}"])

        result = investigate("person0", settings, max_iterations=2)

        assert result.stopped_because in ("iteration cap", "no new leads")

    def test_the_call_budget_stops_it(self, settings):
        for i in range(30):
            add_event(settings, f"e{i}", f"alpha beta gamma delta {i}",
                      people=[f"person-{i}"])

        result = investigate(
            "alpha beta gamma delta epsilon", settings,
            max_iterations=20, budget=CallBudget(limit=1),
        )

        assert result.calls_spent <= 1

    def test_an_exhausted_run_says_the_answer_is_incomplete(self, settings):
        """A truncated investigation presented as a complete one is the failure
        this design exists to avoid."""
        for i in range(30):
            add_event(settings, f"e{i}", f"chain{i} links to chain{i+1}",
                      people=[f"person-{i}"])

        result = investigate(
            "chain0 chain1 chain2", settings, max_iterations=20,
            budget=CallBudget(limit=1),
        )

        if result.exhausted:
            assert "INCOMPLETE" in result.render()

    def test_it_always_records_why_it_stopped(self, settings):
        add_event(settings, "e1", "a thing about Karan", people=["karan"])

        assert investigate("Karan", settings).stopped_because

    def test_every_step_is_recorded_even_fruitless_ones(self, settings):
        result = investigate("nothing here", settings)

        assert result.steps

    def test_the_answer_carries_its_evidence_chain(self, settings):
        add_event(settings, "e1", "Karan mentioned sponsorship", people=["karan"])

        result = investigate("Karan sponsorship", settings)

        assert result.event_ids
        assert "Evidence" in result.render()

    def test_stopwords_do_not_become_search_terms(self):
        assert "what" not in keywords("what did we discuss")
        assert "discuss" in keywords("what did we discuss")

    def test_the_budget_type_raises_rather_than_capping(self):
        budget = CallBudget(limit=1)
        budget.spend()
        with pytest.raises(BudgetExhausted):
            budget.spend()


class TestShortNamesAreSearchable:
    """A >2-char filter silently dropped "DJ", "AI", and "ML".

    The worked example this loop was designed around -- "what did we discuss
    with DJ" -- returned nothing, because the only meaningful token in the
    question was two characters long.
    """

    def test_two_character_names_survive(self):
        assert "dj" in keywords("what did we discuss with DJ")

    def test_two_character_subjects_survive(self):
        assert {"ai", "ml"} <= set(keywords("the AI and ML roles"))

    def test_single_characters_are_still_dropped(self):
        assert keywords("a b c the") == []

    def test_the_dj_question_actually_finds_something(self, settings):
        add_event(settings, "e1", "Call with DJ about the programme",
                  people=["dj-patel"])

        result = investigate("what did we discuss with DJ", settings)

        assert result.event_ids


class TestInvestigationNoise:
    """Two noise sources found running this on the real corpus."""

    def test_the_owner_is_not_a_discovered_person(self, settings):
        """He is on most of his own correspondence. Listing him is noise, and
        expanding on his name pulls in every message he ever sent."""
        settings.owner_emails = "preet@example.com"
        settings.owner_name = "Preet Karia"
        add_event(settings, "e1", "sponsorship chat", people=["preet-karia", "karan"])

        result = investigate("sponsorship", settings)

        assert "preet-karia" not in result.people
        assert "karan" in result.people

    def test_a_phone_number_slug_never_becomes_a_search_term(self, settings):
        """Feeding "14085046227" back in as a query matches unrelated events
        that happen to contain the digits, and each drags in more handles."""
        from personalagi.investigate import _is_name_like

        assert _is_name_like("14085046227") is False
        assert _is_name_like("arjun-sambamoorthy") is True

    def test_a_named_person_still_expands(self, settings):
        add_event(settings, "e1", "intro", people=["dj-patel"])
        add_event(settings, "e2", "patel said the programme opens", people=["john-m"])

        result = investigate("intro", settings)

        assert any(s.action == "expand" for s in result.steps)
