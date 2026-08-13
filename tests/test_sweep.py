"""The proactive sweep: what did NOT happen that should have.

Two properties carry this feature, and both are about restraint:

  1. It costs almost nothing, because every check is a database query and a
     model is only asked to write what already fired.
  2. It stays quiet. An unprompted interruption that is 70% right is spam, and
     three of those and the owner stops reading. So the bar is higher than for
     a reactive answer, suppressed items are stored rather than dropped, and
     nothing can interrupt without a deadline inside 48 hours.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from personalagi import db as db_module
from personalagi.config import Settings
from personalagi.models import (
    Commitment,
    Event,
    Fact,
    Goal,
    GoalStep,
    Participant,
    PersonRole,
    ProposalRecord,
    StepEvidence,
)
from personalagi.records import AttentionLevel, BudgetExhausted, CallBudget, Provenance
from personalagi.sweep import (
    AGENDA,
    collect_findings,
    pending_proposals,
    render_proposals,
    sweep,
)

NOW = datetime(2026, 8, 13, 9, 0)


@pytest.fixture
def settings(tmp_path):
    db_module._engine = None
    yield Settings(
        database_url=f"sqlite:///{tmp_path / 's.db'}",
        context_dir=tmp_path / "ctx",
        commitment_stale_days=7,
        sweep_call_budget=20,
    )
    db_module._engine = None


def session_for(settings):
    return Session(db_module.init_db(settings))


def make_goal(settings, *, slug="cmu", deadline=None, last_activity=None, steps=()):
    with session_for(settings) as session:
        goal = Goal(
            slug=slug, title=slug.upper(), why="", deadline=deadline,
            status="active", last_activity=last_activity,
            created_at=NOW, updated_at=NOW,
        )
        session.add(goal)
        session.flush()
        for i, (description, done) in enumerate(steps):
            session.add(
                GoalStep(
                    goal_id=goal.id, description=description, position=i,
                    done=done, created_at=NOW,
                )
            )
        session.commit()
        return goal.id


def make_event(settings, source_id, *, source="gmail", when=NOW, title="", person=None,
               provenance=Provenance.EXTERNAL):
    with session_for(settings) as session:
        event = Event(
            source=source, source_id=source_id, account_label="personal",
            title=title, text="body", timestamp=when,
            timestamp_ms=int(when.replace(tzinfo=UTC).timestamp() * 1000),
            provenance=provenance, ingested_at=NOW,
        )
        session.add(event)
        session.flush()
        if person:
            session.add(
                Participant(
                    event_id=event.id, address=f"{person}@example.com",
                    role="from", person_slug=person,
                )
            )
        session.commit()
        return event.id


class TestTheSweepCostsNothing:
    def test_collecting_findings_makes_no_model_call(self, settings, monkeypatch):
        """The entire cost argument. If this ever calls a model, waking up
        hourly over 237 people becomes hundreds of times the current bill."""
        import personalagi.llm.client as client_mod

        def boom(*a, **k):  # pragma: no cover - only fires on regression
            raise AssertionError("the sweep tried to call a model")

        monkeypatch.setattr(client_mod.GroqClient, "complete_json", boom)
        make_goal(settings, deadline=NOW + timedelta(days=10),
                  steps=[("Ask Pratik", False)])

        findings = collect_findings(settings, now=NOW)

        assert findings  # it found something, and it found it for free


class TestDeadlineGap:
    """The Pratik case: the highest-signal finding, and pure absence."""

    def test_a_near_deadline_step_with_no_evidence_fires(self, settings):
        make_goal(settings, deadline=NOW + timedelta(days=18),
                  steps=[("Ask Pratik for a letter", False)])

        findings = collect_findings(settings, now=NOW, kinds=("deadline_gap",))

        assert len(findings) == 1
        assert "Ask Pratik" in findings[0].detail

    def test_a_step_with_evidence_does_not_fire(self, settings):
        goal_id = make_goal(settings, deadline=NOW + timedelta(days=18),
                            steps=[("Ask Pratik", False)])
        event_id = make_event(settings, "g1")
        with session_for(settings) as session:
            step = session.execute(select(GoalStep)).scalars().first()
            session.add(
                StepEvidence(step_id=step.id, event_id=event_id, method="search",
                             confidence=0.9, linked_at=NOW)
            )
            session.commit()
        assert goal_id

        assert collect_findings(settings, now=NOW, kinds=("deadline_gap",)) == []

    def test_a_done_step_does_not_fire(self, settings):
        make_goal(settings, deadline=NOW + timedelta(days=5),
                  steps=[("Ask Pratik", True)])

        assert collect_findings(settings, now=NOW, kinds=("deadline_gap",)) == []

    def test_a_far_deadline_does_not_fire(self, settings):
        make_goal(settings, deadline=NOW + timedelta(days=200),
                  steps=[("Ask Pratik", False)])

        assert collect_findings(settings, now=NOW, kinds=("deadline_gap",)) == []

    def test_confidence_rises_as_the_deadline_closes(self, settings):
        make_goal(settings, slug="soon", deadline=NOW + timedelta(days=2),
                  steps=[("a", False)])
        make_goal(settings, slug="later", deadline=NOW + timedelta(days=28),
                  steps=[("b", False)])

        findings = collect_findings(settings, now=NOW, kinds=("deadline_gap",))
        by_slug = {f.subject.split("/")[0]: f.confidence for f in findings}

        assert by_slug["soon"] > by_slug["later"]

    def test_it_reports_having_no_evidence_rather_than_faking_some(self, settings):
        """An absence finding cannot cite anything. Saying so is the honest
        answer; inventing a citation would be the worst possible failure."""
        make_goal(settings, deadline=NOW + timedelta(days=5),
                  steps=[("Ask Pratik", False)])

        finding = collect_findings(settings, now=NOW, kinds=("deadline_gap",))[0]

        assert finding.evidence == []


class TestStaleCommitment:
    def make(self, settings, *, promised, last_activity=None):
        event_id = make_event(settings, "src")
        with session_for(settings) as session:
            session.add(
                Commitment(
                    event_id=event_id, direction="i_owe", person_slug="karan",
                    person_name="Karan", person_email="k@example.com",
                    what="send the deck", what_hash="h", quote="I'll send the deck",
                    status="open", promised_at=promised,
                    last_activity_at=last_activity, manually_closed=False,
                    extracted_at=NOW,
                )
            )
            session.commit()

    def test_an_old_promise_fires(self, settings):
        self.make(settings, promised=NOW - timedelta(days=30))

        findings = collect_findings(settings, now=NOW, kinds=("stale_commitment",))

        assert len(findings) == 1
        assert "Karan" in findings[0].detail

    def test_recent_activity_silences_it(self, settings):
        self.make(
            settings, promised=NOW - timedelta(days=30),
            last_activity=NOW - timedelta(days=1),
        )

        assert collect_findings(settings, now=NOW, kinds=("stale_commitment",)) == []

    def test_it_cites_the_source_event(self, settings):
        """Unlike absence findings, this one CAN cite, so it must."""
        self.make(settings, promised=NOW - timedelta(days=30))

        assert collect_findings(settings, now=NOW, kinds=("stale_commitment",))[0].evidence

    def test_owed_to_me_does_not_nag_the_owner(self, settings):
        event_id = make_event(settings, "src2")
        with session_for(settings) as session:
            session.add(
                Commitment(
                    event_id=event_id, direction="they_owe", person_slug="x",
                    person_name="X", person_email="x@example.com", what="w",
                    what_hash="h2", quote="q", status="open",
                    promised_at=NOW - timedelta(days=60), manually_closed=False,
                    extracted_at=NOW,
                )
            )
            session.commit()

        assert collect_findings(settings, now=NOW, kinds=("stale_commitment",)) == []


class TestFactWindow:
    def test_a_window_that_just_opened_fires(self, settings):
        """"Builder Club apps open mid-August" plus "it is mid-August"."""
        with session_for(settings) as session:
            session.add(
                Fact(
                    statement="Builder Club applications open mid-August",
                    valid_from=NOW - timedelta(days=1), status="open",
                    confidence=0.8, created_at=NOW,
                )
            )
            session.commit()

        findings = collect_findings(settings, now=NOW, kinds=("fact_window",))

        assert len(findings) == 1
        assert "Builder Club" in findings[0].detail

    def test_a_future_window_stays_quiet(self, settings):
        with session_for(settings) as session:
            session.add(
                Fact(statement="later", valid_from=NOW + timedelta(days=30),
                     status="open", created_at=NOW)
            )
            session.commit()

        assert collect_findings(settings, now=NOW, kinds=("fact_window",)) == []

    def test_an_expired_window_stays_quiet(self, settings):
        with session_for(settings) as session:
            session.add(
                Fact(statement="over", valid_from=NOW - timedelta(days=60),
                     valid_until=NOW - timedelta(days=30), status="open",
                     created_at=NOW)
            )
            session.commit()

        assert collect_findings(settings, now=NOW, kinds=("fact_window",)) == []


class TestRelationshipDecay:
    def test_it_only_considers_people_attached_to_a_goal(self, settings):
        """"You have not spoken to this newsletter in 90 days" is exactly the
        noise that gets a proactive system switched off."""
        make_event(settings, "old", when=NOW - timedelta(days=200), person="randomer")

        assert collect_findings(settings, now=NOW, kinds=("relationship_decay",)) == []

    def test_a_tracked_person_gone_quiet_fires(self, settings):
        goal_id = make_goal(settings, slug="nvidia")
        make_event(settings, "old", when=NOW - timedelta(days=200), person="pratik")
        with session_for(settings) as session:
            session.add(
                PersonRole(person_slug="pratik", goal_id=goal_id,
                           role="recommender", created_at=NOW)
            )
            session.commit()

        findings = collect_findings(settings, now=NOW, kinds=("relationship_decay",))

        assert len(findings) == 1
        assert "pratik" in findings[0].detail

    def test_recent_contact_silences_it(self, settings):
        goal_id = make_goal(settings, slug="nvidia")
        make_event(settings, "new", when=NOW - timedelta(days=3), person="pratik")
        with session_for(settings) as session:
            session.add(
                PersonRole(person_slug="pratik", goal_id=goal_id,
                           role="recommender", created_at=NOW)
            )
            session.commit()

        assert collect_findings(settings, now=NOW, kinds=("relationship_decay",)) == []


class TestAgendaOrdering:
    def test_deadline_gaps_outrank_everything(self, settings):
        """The budget runs out from the bottom, so the best signal must be
        written first."""
        make_goal(settings, slug="cmu", deadline=NOW + timedelta(days=3),
                  steps=[("Ask Pratik", False)])
        make_goal(settings, slug="idle", last_activity=NOW - timedelta(days=60))

        findings = collect_findings(settings, now=NOW)

        assert findings[0].kind == "deadline_gap"

    def test_every_agenda_kind_has_a_query(self):
        from personalagi.sweep import QUERIES

        assert set(AGENDA) == set(QUERIES)


class TestAttentionCeiling:
    def test_nothing_interrupts_without_a_deadline_inside_48h(self, settings):
        make_goal(settings, deadline=NOW + timedelta(days=20),
                  steps=[("Ask Pratik", False)])

        sweep(settings, now=NOW, kinds=("deadline_gap",))
        rows = pending_proposals(settings)

        assert rows
        assert all(r.attention_level != AttentionLevel.INTERRUPT for r in rows)

    def test_a_deadline_inside_48h_may_interrupt(self, settings):
        make_goal(settings, deadline=NOW + timedelta(hours=30),
                  steps=[("Ask Pratik", False)])

        sweep(settings, now=NOW, kinds=("deadline_gap",))
        rows = pending_proposals(settings)

        assert rows[0].attention_level == AttentionLevel.INTERRUPT

    def test_a_finding_cannot_request_its_way_past_the_ceiling(self, settings):
        """The model proposes; the ceiling is enforced in code."""
        from personalagi.records import clamp_attention, max_attention_for

        ceiling = max_attention_for(NOW + timedelta(days=10), now=NOW)

        assert clamp_attention(AttentionLevel.INTERRUPT, ceiling) != (
            AttentionLevel.INTERRUPT
        )


class TestSuppression:
    def test_one_goal_can_legitimately_fire_several_kinds(self, settings):
        """A goal with a near deadline AND no activity is genuinely both a
        deadline_gap and a stale_goal. Deduplicating across kinds would hide
        one of two real findings."""
        make_goal(settings, deadline=NOW + timedelta(days=20),
                  steps=[("Ask Pratik", False)])

        kinds = {f.kind for f in collect_findings(settings, now=NOW)}

        assert {"deadline_gap", "stale_goal"} <= kinds

    def test_low_confidence_findings_are_stored_not_discarded(self, settings):
        """Once the system goes quiet its blind spots become invisible, so
        --show-suppressed needs something to show."""
        settings.sweep_min_confidence = 0.99
        make_goal(settings, deadline=NOW + timedelta(days=20),
                  steps=[("Ask Pratik", False)])

        result = sweep(settings, now=NOW, kinds=("deadline_gap",))

        assert result.surfaced == 0
        assert result.suppressed == 1
        assert pending_proposals(settings) == []
        assert len(pending_proposals(settings, include_suppressed=True)) == 1

    def test_a_suppressed_finding_costs_no_model_call(self, settings):
        settings.sweep_min_confidence = 0.99
        make_goal(settings, deadline=NOW + timedelta(days=20),
                  steps=[("Ask Pratik", False)])

        assert sweep(settings, now=NOW, kinds=("deadline_gap",)).calls_spent == 0

    def test_saying_nothing_is_a_valid_and_explained_outcome(self, settings):
        assert "most common correct answer" in render_proposals([])


class TestBudget:
    def test_the_ceiling_stops_the_sweep(self, settings):
        for i in range(10):
            make_goal(settings, slug=f"g{i}", deadline=NOW + timedelta(days=5),
                      steps=[(f"step {i}", False)])

        result = sweep(
            settings, now=NOW, budget=CallBudget(limit=3), kinds=("deadline_gap",)
        )

        assert result.calls_spent == 3
        assert result.budget_exhausted is True

    def test_an_exhausted_budget_does_not_crash_the_sweep(self, settings):
        make_goal(settings, deadline=NOW + timedelta(days=5), steps=[("a", False)])

        result = sweep(
            settings, now=NOW, budget=CallBudget(limit=0), kinds=("deadline_gap",)
        )

        assert result.budget_exhausted is True
        assert result.surfaced == 0

    def test_the_budget_type_raises_rather_than_capping(self):
        budget = CallBudget(limit=1)
        budget.spend()
        with pytest.raises(BudgetExhausted):
            budget.spend()


class TestNoRepeatNagging:
    def test_the_same_finding_is_not_reproposed_while_pending(self, settings):
        """Re-raising the same thing every morning is how a useful nudge
        becomes noise the owner filters out."""
        make_goal(settings, deadline=NOW + timedelta(days=10),
                  steps=[("Ask Pratik", False)])

        first = sweep(settings, now=NOW, kinds=("deadline_gap",))
        second = sweep(settings, now=NOW, kinds=("deadline_gap",))

        assert first.surfaced == 1
        assert second.surfaced == 0
        with session_for(settings) as session:
            assert len(list(session.execute(select(ProposalRecord)).scalars())) == 1


class TestDryRun:
    def test_it_records_nothing(self, settings):
        make_goal(settings, deadline=NOW + timedelta(days=10),
                  steps=[("Ask Pratik", False)])

        sweep(settings, now=NOW, dry_run=True, kinds=("deadline_gap",))

        with session_for(settings) as session:
            assert list(session.execute(select(ProposalRecord)).scalars()) == []

    def test_it_still_reports_what_would_fire(self, settings):
        make_goal(settings, deadline=NOW + timedelta(days=10),
                  steps=[("Ask Pratik", False)])

        result = sweep(settings, now=NOW, dry_run=True, kinds=("deadline_gap",))

        assert result.surfaced == 1


class TestRendering:
    def test_an_absence_finding_says_it_has_no_evidence(self, settings):
        make_goal(settings, deadline=NOW + timedelta(days=10),
                  steps=[("Ask Pratik", False)])
        sweep(settings, now=NOW, kinds=("deadline_gap",))

        output = render_proposals(pending_proposals(settings))

        assert "fired on absence" in output

    def test_an_evidenced_finding_shows_its_citation(self, settings):
        event_id = make_event(settings, "src")
        with session_for(settings) as session:
            session.add(
                Commitment(
                    event_id=event_id, direction="i_owe", person_slug="k",
                    person_name="Karan", person_email="k@example.com", what="w",
                    what_hash="h", quote="I'll send it", status="open",
                    promised_at=NOW - timedelta(days=40), manually_closed=False,
                    extracted_at=NOW,
                )
            )
            session.commit()
        sweep(settings, now=NOW, kinds=("stale_commitment",))

        output = render_proposals(pending_proposals(settings))

        assert "evidence:" in output
