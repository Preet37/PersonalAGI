"""Goals, evidence linking, and the staleness fix.

The assertions that matter are about ABSENCE, because that is the whole reason
goals exist. A step nobody has touched and a step nobody recorded look
identical from the outside, and the system has to be honest about which it is
looking at.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlmodel import Session

from personalagi import db as db_module
from personalagi.config import Settings
from personalagi.goal_evidence import find_evidence, keywords, link_evidence
from personalagi.goals import (
    GoalError,
    Step,
    create_goal,
    get_goal,
    list_goals,
    load_goal,
    parse_goal,
    refresh_activity,
    save_goal,
    sync_goals,
)
from personalagi.models import Commitment, Event, Participant
from personalagi.records import Provenance

NOW = datetime(2026, 8, 13, 9, 0)


@pytest.fixture
def settings(tmp_path):
    db_module._engine = None
    yield Settings(
        database_url=f"sqlite:///{tmp_path / 'g.db'}",
        context_dir=tmp_path / "ctx",
        commitment_stale_days=7,
    )
    db_module._engine = None


def add_event(
    settings, source_id, text, *, when=NOW, thread="t1",
    provenance=Provenance.EXTERNAL, person=None, address=None,
):
    engine = db_module.init_db(settings)
    with Session(engine) as session:
        event = Event(
            source="gmail", source_id=source_id, account_label="personal",
            thread_key=thread, title="", text=text, timestamp=when,
            timestamp_ms=int(when.replace(tzinfo=UTC).timestamp() * 1000),
            provenance=provenance, ingested_at=NOW,
        )
        session.add(event)
        session.flush()
        if person or address:
            session.add(
                Participant(
                    event_id=event.id, address=(address or "x@example.com").lower(),
                    display_name="", role="from", person_slug=person or "",
                )
            )
        session.commit()
        return event.id


class TestGoalFiles:
    def test_round_trip_preserves_everything(self, settings):
        goal = create_goal(
            "Get into CMU", settings,
            why="Masters in ML", deadline=date(2026, 8, 31),
            steps=["Ask Pratik for a letter", "Submit the application"],
            people={"pratik": "recommender"},
        )
        loaded = load_goal(settings, goal.slug)

        assert loaded.title == "Get into CMU"
        assert loaded.deadline == date(2026, 8, 31)
        assert loaded.people == {"pratik": "recommender"}
        assert [s.description for s in loaded.steps] == [
            "Ask Pratik for a letter", "Submit the application"
        ]

    def test_done_and_blocking_markers_survive(self, settings):
        goal = create_goal("X", settings, steps=["a", "b"])
        goal.steps[0].done = True
        goal.steps[1].blocking = True
        save_goal(settings, goal)

        loaded = load_goal(settings, goal.slug)

        assert loaded.steps[0].done is True
        assert loaded.steps[1].blocking is True
        assert loaded.steps[1].description == "b"  # marker stripped from text

    def test_evidence_anchors_survive_a_round_trip(self, settings):
        goal = create_goal("X", settings, steps=["a"])
        goal.steps[0].evidence = ["g123", "g456"]
        save_goal(settings, goal)

        assert load_goal(settings, goal.slug).steps[0].evidence == ["g123", "g456"]

    def test_a_duplicate_slug_is_refused(self, settings):
        create_goal("Same Name", settings)

        with pytest.raises(GoalError, match="already exists"):
            create_goal("Same Name", settings)

    def test_an_empty_title_is_refused(self, settings):
        with pytest.raises(GoalError):
            create_goal("   ", settings)

    def test_a_hand_written_file_parses(self):
        """Editing the markdown by hand is the supported correction path."""
        parsed = parse_goal(
            "---\ntitle: Bring Anthropic to UCSC\nslug: anthropic-ucsc\n"
            "status: active\ndeadline: 2026-10-01\npeople: {dj: gatekeeper}\n---\n\n"
            "## Why\n\nIt unlocks the builder club.\n\n"
            "## Steps\n\n- [x] Meet DJ\n- [ ] Email the Anthropic contact  !blocking\n"
        )

        assert parsed.slug == "anthropic-ucsc"
        assert parsed.deadline == date(2026, 10, 1)
        assert parsed.steps[0].done is True
        assert parsed.steps[1].blocking is True


class TestSync:
    def test_it_indexes_goals_steps_and_roles(self, settings):
        create_goal(
            "CMU", settings, deadline=date(2026, 8, 31),
            steps=["Ask Pratik", "Submit"], people={"pratik": "recommender"},
        )
        result = sync_goals(settings)

        assert result.goals == 1
        assert result.steps == 2
        assert result.roles == 1

    def test_it_is_idempotent(self, settings):
        create_goal("CMU", settings, steps=["a", "b"])
        sync_goals(settings)
        sync_goals(settings)

        view = get_goal("cmu", settings)
        assert len(view.steps) == 2

    def test_removing_a_step_from_markdown_removes_it_from_the_index(self, settings):
        goal = create_goal("CMU", settings, steps=["a", "b"])
        goal.steps = [Step(description="a")]
        save_goal(settings, goal)
        sync_goals(settings)

        assert [s.description for s in get_goal("cmu", settings).steps] == ["a"]

    def test_editing_the_text_does_not_count_as_progress(self, settings):
        """updated_at is not last_activity. Rewriting a description must not
        reset a staleness clock, or a goal can be kept 'fresh' by fiddling."""
        create_goal("CMU", settings, steps=["a"])
        sync_goals(settings)

        view = get_goal("cmu", settings)
        assert view.goal.last_activity is None
        assert view.goal.updated_at is not None


class TestGapDetection:
    def test_a_step_with_no_evidence_is_a_gap(self, settings):
        create_goal("CMU", settings, steps=["Ask Pratik for a letter"])
        sync_goals(settings)

        view = get_goal("cmu", settings)

        assert len(view.unevidenced_steps) == 1

    def test_a_done_step_is_not_a_gap_even_without_evidence(self, settings):
        goal = create_goal("CMU", settings, steps=["Ask Pratik"])
        goal.steps[0].done = True
        save_goal(settings, goal)
        sync_goals(settings)

        assert get_goal("cmu", settings).unevidenced_steps == []

    def test_evidence_closes_the_gap(self, settings):
        add_event(settings, "g1", "Pratik, could you write me a letter for CMU?")
        create_goal("CMU", settings, steps=["Ask Pratik for a letter"])
        sync_goals(settings)

        link_evidence(settings, judge=False)

        assert get_goal("cmu", settings).unevidenced_steps == []

    def test_the_pratik_case_end_to_end(self, settings):
        """The example that motivated the whole record type.

        A deadline 18 days out, a required step, and no message anywhere. The
        system has to surface it precisely BECAUSE nothing arrived.
        """
        add_event(settings, "g1", "Lunch tomorrow?")  # unrelated traffic
        create_goal(
            "Get into CMU", settings,
            deadline=date(2026, 8, 31),
            steps=["Ask Pratik for a letter of recommendation", "Submit application"],
            people={"pratik": "recommender"},
        )
        sync_goals(settings)
        link_evidence(settings, judge=False)

        view = get_goal("get-into-cmu", settings)
        missing = [s.description for s in view.unevidenced_steps]

        assert "Ask Pratik for a letter of recommendation" in missing


class TestEvidenceSearch:
    def test_stopwords_are_dropped(self):
        assert "the" not in keywords("Ask the recruiter for the offer")
        assert "recruiter" in keywords("Ask the recruiter for the offer")

    def test_a_generic_step_does_not_match_everything(self, settings):
        """"Follow up" against a mailbox would otherwise match half of it."""
        add_event(settings, "g1", "Hey, want to grab lunch?")
        create_goal("X", settings, steps=["Follow up"])
        sync_goals(settings)

        matches = find_evidence("Follow up", settings)

        assert matches == []

    def test_a_specific_step_matches_the_right_message(self, settings):
        add_event(settings, "g1", "Sending over the sponsorship prospectus now")
        add_event(settings, "g2", "Dinner at 8?")

        matches = find_evidence("Send the sponsorship prospectus", settings)

        assert [m.event.source_id for m in matches] == ["g1"]

    def test_generated_events_are_never_evidence(self, settings):
        """The self-citation rule, at the layer that would break it worst.

        A summary the system wrote about a step must not come back as proof
        the step is handled -- that closes a gap with the system's own words.
        """
        add_event(
            settings, "gen1", "Preet needs to send the sponsorship prospectus",
            provenance=Provenance.GENERATED,
        )

        matches = find_evidence("Send the sponsorship prospectus", settings)

        assert matches == []

    def test_a_person_on_the_goal_tops_up_a_partial_match(self, settings):
        """The bonus rescues a genuine partial match, never a coincidence.

        Four content words, two present: 0.5, below the 0.6 threshold on its
        own. The person attached to the goal being in the conversation is what
        makes it credible.
        """
        add_event(settings, "g1", "the recommendation letter", person="pratik")
        step = "letter recommendation deadline transcript"

        without = find_evidence(step, settings)
        with_person = find_evidence(step, settings, person_slugs={"pratik"})

        # The keyword layer is recall-only, so both may return the event; what
        # the bonus must do is rank a goal-person's message higher.
        assert with_person[0].score > (without[0].score if without else 0.0)

    def test_the_person_bonus_cannot_rescue_a_zero_match(self, settings):
        """Without this floor every message a goal's person ever sent scores
        0.25 and the threshold stops doing anything."""
        add_event(settings, "g1", "want to grab lunch tomorrow", person="pratik")

        matches = find_evidence(
            "letter recommendation Pratik", settings, person_slugs={"pratik"}
        )

        assert matches == []

    def test_newsletter_does_not_evidence_a_letter(self, settings):
        """The exact false positive the real run produced: "letter" is inside
        "newsletter", and marketing mail is full of newsletters. Six of six
        links were false before word boundaries."""
        add_event(
            settings, "g1",
            "Welcome to our newsletter! Recommendations picked for you.",
        )

        matches = find_evidence("letter of recommendation Pratik", settings)

        assert matches == []

    def test_a_two_word_step_is_too_generic_to_search(self, settings):
        add_event(settings, "g1", "following up on the thing")

        assert find_evidence("Follow up", settings) == []

    def test_links_record_how_they_were_made(self, settings):
        """An auto-found link is a candidate, not a confirmation. A wrong one
        marks a step handled when it is not, so the method must be visible."""
        add_event(settings, "g1", "Sending the sponsorship prospectus")
        create_goal("X", settings, steps=["Send the sponsorship prospectus"])
        sync_goals(settings)
        link_evidence(settings, judge=False)

        from personalagi.models import StepEvidence

        with Session(db_module.get_engine(settings)) as session:
            link = session.execute(
                __import__("sqlalchemy").select(StepEvidence)
            ).scalars().first()

        assert link.method == "search"  # judge=False path
        assert 0.0 < link.confidence <= 1.0

    def test_a_dry_run_writes_nothing(self, settings):
        add_event(settings, "g1", "Sending the sponsorship prospectus")
        create_goal("X", settings, steps=["Send the sponsorship prospectus"])
        sync_goals(settings)

        link_evidence(settings, judge=False, dry_run=True)

        assert len(get_goal("x", settings).unevidenced_steps) == 1

    def test_linking_twice_does_not_duplicate(self, settings):
        add_event(settings, "g1", "Sending the sponsorship prospectus")
        create_goal("X", settings, steps=["Send the sponsorship prospectus"])
        sync_goals(settings)

        first = link_evidence(settings, judge=False).links_added
        second = link_evidence(settings, judge=False).links_added

        assert first >= 1
        assert second == 0


class TestGoalActivity:
    def test_evidence_makes_a_goal_active(self, settings):
        add_event(settings, "g1", "Sending the sponsorship prospectus", when=NOW)
        create_goal("X", settings, steps=["Send the sponsorship prospectus"])
        sync_goals(settings)
        link_evidence(settings, judge=False)

        assert get_goal("x", settings).goal.last_activity == NOW

    def test_a_goal_with_nothing_behind_it_has_no_activity(self, settings):
        create_goal("X", settings, steps=["Do a thing nobody mentioned"])
        sync_goals(settings)
        refresh_activity(settings)

        assert get_goal("x", settings).goal.last_activity is None


class TestStalenessFix:
    """Stage 17: measured from last_activity_at, not promised_at."""

    def make_commitment(self, settings, *, promised, thread="t1", email="k@example.com"):
        event_id = add_event(
            settings, "src1", "I'll send the deck", when=promised, thread=thread,
            address=email,
        )
        engine = db_module.init_db(settings)
        with Session(engine) as session:
            row = Commitment(
                event_id=event_id, direction="i_owe", person_slug="karan",
                person_name="Karan", person_email=email, what="send the deck",
                what_hash="h1", quote="I'll send the deck", status="open",
                promised_at=promised, manually_closed=False, extracted_at=NOW,
            )
            session.add(row)
            session.commit()
            return row.id

    def test_an_old_promise_with_no_follow_up_goes_stale(self, settings):
        from personalagi.commitments import refresh_stale

        self.make_commitment(settings, promised=NOW - timedelta(days=30))

        assert refresh_stale(settings, now=NOW) == 1

    def test_a_later_message_in_the_thread_keeps_it_fresh(self, settings):
        """The bug. Before this, a promise you fulfilled a week later still
        went stale on schedule and nagged forever."""
        from personalagi.commitments import refresh_activity as refresh_commit_activity
        from personalagi.commitments import refresh_stale

        self.make_commitment(settings, promised=NOW - timedelta(days=30))
        add_event(
            settings, "src2", "Here is the deck", when=NOW - timedelta(days=1),
            thread="t1",
        )

        refresh_commit_activity(settings)

        assert refresh_stale(settings, now=NOW) == 0

    def test_activity_with_the_person_counts_when_there_is_no_thread(self, settings):
        from personalagi.commitments import refresh_activity as refresh_commit_activity
        from personalagi.commitments import refresh_stale

        self.make_commitment(settings, promised=NOW - timedelta(days=30), thread="")
        add_event(
            settings, "src2", "unrelated chat", when=NOW - timedelta(days=1),
            thread="", address="k@example.com",
        )

        refresh_commit_activity(settings)

        assert refresh_stale(settings, now=NOW) == 0

    def test_a_generated_event_does_not_count_as_follow_up(self, settings):
        """Otherwise the system's own summary keeps a rotting promise fresh --
        the self-citation bug, applied to staleness."""
        from personalagi.commitments import refresh_activity as refresh_commit_activity
        from personalagi.commitments import refresh_stale

        self.make_commitment(settings, promised=NOW - timedelta(days=30))
        add_event(
            settings, "gen", "summary: Preet owes Karan a deck",
            when=NOW - timedelta(days=1), thread="t1",
            provenance=Provenance.GENERATED,
        )

        refresh_commit_activity(settings)

        assert refresh_stale(settings, now=NOW) == 1

    def test_a_null_last_activity_falls_back_to_promised_at(self, settings):
        """Rows written before the column existed must not read as infinitely
        stale, nor as infinitely fresh."""
        from personalagi.commitments import refresh_stale

        self.make_commitment(settings, promised=NOW - timedelta(days=2))

        assert refresh_stale(settings, now=NOW) == 0


class TestListing:
    def test_soonest_deadline_first_and_undated_last(self, settings):
        create_goal("Later", settings, deadline=date(2026, 12, 1))
        create_goal("Sooner", settings, deadline=date(2026, 8, 20))
        create_goal("Someday", settings)
        sync_goals(settings)

        assert [v.goal.title for v in list_goals(settings)] == [
            "Sooner", "Later", "Someday"
        ]

    def test_done_goals_are_hidden_by_default(self, settings):
        goal = create_goal("Done thing", settings)
        goal.status = "done"
        save_goal(settings, goal)
        sync_goals(settings)

        assert list_goals(settings) == []
        assert len(list_goals(settings, status=None)) == 1


class TestIdfWeighting:
    """Word boundaries cut the volume of false links; IDF attacks the cause.

    Audited after the boundary fix, BOTH surviving links on real data were
    still false -- "Submit the CMU application" matched a credit-card promo,
    because "submit" and "application" are everywhere and "cmu" was absent.
    """

    def test_the_distinctive_word_ranks_the_right_one_first(self, settings):
        """The keyword pass is RECALL-only now, so it may return noise. What
        it must do is rank the genuinely distinctive match above it -- the
        judge only sees the top few candidates."""
        for i in range(12):
            add_event(settings, f"noise{i}", "Submit your application today")
        add_event(settings, "real", "Submitting my CMU application now")

        matches = find_evidence("Submit the CMU application", settings)

        assert matches[0].event.source_id == "real"

    def test_idf_still_weights_the_rare_word_highest(self, settings):
        for i in range(12):
            add_event(settings, f"noise{i}", "Submit your application today")
        add_event(settings, "real", "Submitting my CMU application now")

        matches = find_evidence("Submit the CMU application", settings)
        best = matches[0]

        assert "cmu" in best.matched
        assert best.score > (matches[1].score if len(matches) > 1 else 0)

    def test_weights_stay_positive_on_a_tiny_corpus(self, settings):
        """Plain log(total/seen) is 0 when a term is in every document, which
        on a two-event corpus is every term -- scoring everything at zero."""
        from personalagi.goal_evidence import inverse_document_frequency
        from personalagi.models import Event

        events = [
            Event(source="x", source_id="a", title="", text="prospectus deck",
                  timestamp=NOW, timestamp_ms=1, ingested_at=NOW),
        ]
        weights = inverse_document_frequency(["prospectus", "deck"], events)

        assert all(w > 0 for w in weights.values())

    def test_a_term_absent_from_the_corpus_still_has_weight(self, settings):
        from personalagi.goal_evidence import inverse_document_frequency
        from personalagi.models import Event

        events = [
            Event(source="x", source_id="a", title="", text="nothing relevant",
                  timestamp=NOW, timestamp_ms=1, ingested_at=NOW),
        ]
        weights = inverse_document_frequency(["prospectus"], events)

        # It carries the MOST weight: a word nobody else uses is the strongest
        # possible identifier if it ever does appear.
        assert weights["prospectus"] > 1.0
