"""Phase 0 contracts. Four parallel tracks are about to build against these.

These tests exist so a merge is a rebase rather than a rewrite. They pin the
two rules that every track has to honour, and the migration behaviour that
would otherwise fail silently.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from personalagi import db as db_module
from personalagi.config import Settings
from personalagi.models import Event
from personalagi.records import (
    ATTENTION_ORDER,
    DEFAULT_ACTIVATION,
    DEFAULT_EDGE_WEIGHT,
    AttentionLevel,
    BudgetExhausted,
    CallBudget,
    Outcome,
    PermissionTier,
    Provenance,
    Relation,
    attention_rank,
    citable_events,
    clamp_attention,
    max_attention_for,
)

NOW = datetime(2026, 8, 13, 9, 0)


@pytest.fixture
def settings(tmp_path):
    db_module._engine = None
    yield Settings(database_url=f"sqlite:///{tmp_path / 'r.db'}")
    db_module._engine = None


def add_event(session, source_id: str, provenance: str) -> Event:
    event = Event(
        source="gmail",
        source_id=source_id,
        account_label="personal",
        title="t",
        text="body",
        timestamp=NOW,
        timestamp_ms=int(NOW.replace(tzinfo=UTC).timestamp() * 1000),
        provenance=provenance,
        ingested_at=NOW,
    )
    session.add(event)
    return event


class TestProvenance:
    """Rule 1: only external events may be cited as evidence."""

    def test_generated_events_are_not_citable(self, settings):
        engine = db_module.init_db(settings)
        with Session(engine) as session:
            add_event(session, "real", Provenance.EXTERNAL)
            add_event(session, "summary", Provenance.GENERATED)
            session.commit()

            citable = list(session.execute(citable_events()).scalars())

        assert [e.source_id for e in citable] == ["real"]

    def test_it_composes_with_an_existing_query(self, settings):
        """Tracks will need to narrow further; the filter must not be the end
        of the statement."""
        engine = db_module.init_db(settings)
        with Session(engine) as session:
            add_event(session, "a", Provenance.EXTERNAL)
            add_event(session, "b", Provenance.EXTERNAL)
            add_event(session, "c", Provenance.GENERATED)
            session.commit()

            stmt = citable_events(select(Event)).where(Event.source_id == "b")
            found = list(session.execute(stmt).scalars())

        assert [e.source_id for e in found] == ["b"]

    def test_new_events_default_to_external(self, settings):
        """An adapter reading a real mailbox should not have to remember."""
        engine = db_module.init_db(settings)
        with Session(engine) as session:
            event = Event(
                source="gmail", source_id="x", title="", text="",
                timestamp=NOW, timestamp_ms=1, ingested_at=NOW,
            )
            session.add(event)
            session.commit()
            session.refresh(event)

        assert event.provenance == Provenance.EXTERNAL

    def test_the_migration_backfills_existing_rows(self, tmp_path):
        """The bug this caught, pinned.

        Adding a nullable column leaves every existing row NULL, so
        citable_events() excluded the whole 4,346-event corpus and every claim
        silently lost its evidence. Nothing raised; the count just went to zero.
        """
        import sqlite3

        db_module._engine = None
        path = tmp_path / "legacy.db"
        settings = Settings(database_url=f"sqlite:///{path}")
        db_module.init_db(settings)
        db_module._engine = None

        # A genuinely pre-provenance event table. A fresh schema declares the
        # column NOT NULL, so inserting a NULL is impossible there -- the real
        # database only had NULLs because ALTER TABLE ADD COLUMN does not
        # enforce NOT NULL on rows that already exist. Reproducing that means
        # actually removing the column first.
        raw = sqlite3.connect(path)
        raw.executescript(
            """
            DROP TABLE event;
            CREATE TABLE event (
                id INTEGER PRIMARY KEY,
                source VARCHAR NOT NULL, source_id VARCHAR NOT NULL,
                account_label VARCHAR, thread_key VARCHAR,
                title VARCHAR, text VARCHAR,
                timestamp DATETIME NOT NULL, timestamp_ms INTEGER NOT NULL,
                metadata_json VARCHAR, ingested_at DATETIME NOT NULL
            );
            INSERT INTO event (source, source_id, account_label, thread_key,
                               title, text, timestamp, timestamp_ms, ingested_at)
            VALUES ('gmail','old','personal','','t','b','2026-08-13',1,'2026-08-13');
            """
        )
        raw.commit()
        columns = {row[1] for row in raw.execute("PRAGMA table_info(event)")}
        raw.close()
        assert "provenance" not in columns

        db_module.init_db(settings)

        raw = sqlite3.connect(path)
        nulls = raw.execute(
            "SELECT count(*) FROM event WHERE provenance IS NULL"
        ).fetchone()[0]
        external = raw.execute(
            "SELECT count(*) FROM event WHERE provenance='external'"
        ).fetchone()[0]
        raw.close()
        db_module._engine = None

        assert nulls == 0
        assert external == 1

    def test_backfill_is_idempotent_and_does_not_relabel(self, settings):
        engine = db_module.init_db(settings)
        with Session(engine) as session:
            add_event(session, "gen", Provenance.GENERATED)
            session.commit()

        db_module._backfill_defaults(engine)

        with Session(engine) as session:
            event = session.execute(
                select(Event).where(Event.source_id == "gen")
            ).scalar_one()

        assert event.provenance == Provenance.GENERATED


class TestAttentionIsIndependentOfPermission:
    """Rule 2: two axes. Reversibility is not interruption cost."""

    def test_they_are_separate_vocabularies(self):
        assert set(PermissionTier) & {AttentionLevel.SILENT} == set()

    def test_the_ladder_is_ordered_quietest_first(self):
        assert ATTENTION_ORDER[0] == AttentionLevel.SILENT
        assert ATTENTION_ORDER[-1] == AttentionLevel.INTERRUPT
        assert attention_rank(AttentionLevel.NUDGE) > attention_rank(
            AttentionLevel.AMBIENT
        )

    def test_an_unknown_level_is_treated_as_quietest(self):
        """A typo must never buy an interruption."""
        assert attention_rank("URGENT!!!") == 0

    def test_interrupt_requires_a_deadline_inside_48h(self):
        assert max_attention_for(NOW + timedelta(hours=24), now=NOW) == (
            AttentionLevel.INTERRUPT
        )
        assert max_attention_for(NOW + timedelta(days=5), now=NOW) == (
            AttentionLevel.NUDGE
        )

    def test_no_deadline_cannot_interrupt(self):
        assert max_attention_for(None, now=NOW) == AttentionLevel.NUDGE

    def test_the_ceiling_caps_a_louder_request(self):
        assert clamp_attention(AttentionLevel.INTERRUPT, AttentionLevel.NUDGE) == (
            AttentionLevel.NUDGE
        )

    def test_quieter_than_the_ceiling_is_always_allowed(self):
        assert clamp_attention(AttentionLevel.SILENT, AttentionLevel.INTERRUPT) == (
            AttentionLevel.SILENT
        )

    def test_a_letter_of_rec_nudge_and_a_linkedin_request_differ_on_attention(self):
        """Same permission tier, opposite attention. The whole point of two
        axes: both need approval, only one is worth an interruption."""
        cmu_deadline = NOW + timedelta(hours=30)

        assert max_attention_for(cmu_deadline, now=NOW) == AttentionLevel.INTERRUPT
        assert max_attention_for(None, now=NOW) != AttentionLevel.INTERRUPT


class TestCallBudget:
    def test_it_stops_at_the_ceiling(self):
        budget = CallBudget(limit=3)
        for _ in range(3):
            budget.spend()

        assert budget.remaining() == 0
        with pytest.raises(BudgetExhausted):
            budget.spend()

    def test_it_raises_rather_than_silently_capping(self):
        """A runaway loop should hit a wall, not a bill."""
        budget = CallBudget(limit=1)
        budget.spend()

        with pytest.raises(BudgetExhausted, match="exhausted"):
            budget.spend()

    def test_can_spend_reports_before_committing(self):
        budget = CallBudget(limit=2)
        assert budget.can_spend(2) is True
        assert budget.can_spend(3) is False

    def test_a_zero_budget_blocks_everything(self):
        assert CallBudget(limit=0).can_spend() is False


class TestActivationLimits:
    def test_decay_stops_the_traversal(self):
        """Without a floor, three hops connects everything to everything."""
        energy = DEFAULT_ACTIVATION.start_energy
        hops = 0
        while energy >= DEFAULT_ACTIVATION.threshold:
            energy *= DEFAULT_EDGE_WEIGHT[Relation.MENTIONS]
            hops += 1

        assert hops <= DEFAULT_ACTIVATION.max_depth + 1

    def test_a_strong_relation_carries_further_than_a_weak_one(self):
        """An obligation should reach further than a coincidence."""
        assert DEFAULT_EDGE_WEIGHT[Relation.OWES] > DEFAULT_EDGE_WEIGHT[Relation.MENTIONS]

    def test_every_relation_has_a_weight(self):
        """A missing weight would silently default and skew activation."""
        assert set(DEFAULT_EDGE_WEIGHT) == set(Relation)

    def test_node_visits_are_capped(self):
        assert DEFAULT_ACTIVATION.max_nodes > 0


class TestOutcomes:
    def test_the_five_outcomes_plus_pending_exist(self):
        assert {o.value for o in Outcome} == {
            "pending", "accepted", "edited", "dismissed", "ignored", "reversed"
        }

    def test_edited_counts_as_positive(self):
        """The owner sending an edited version means the proposal was right
        enough to be worth fixing. The diff is the lesson, not a rejection."""
        from personalagi.records import POSITIVE_OUTCOMES

        assert Outcome.EDITED in POSITIVE_OUTCOMES

    def test_ignored_counts_as_negative(self):
        from personalagi.records import NEGATIVE_OUTCOMES

        assert Outcome.IGNORED in NEGATIVE_OUTCOMES


class TestNewTablesExist:
    """Each track owns one of these. They must all migrate cleanly together."""

    @pytest.mark.parametrize(
        "table",
        ["goal", "goal_step", "step_evidence", "fact", "person_role", "edge", "proposal"],
    )
    def test_table_is_created(self, settings, table):
        import sqlite3

        engine = db_module.init_db(settings)
        path = str(engine.url.database)
        raw = sqlite3.connect(path)
        found = raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        raw.close()

        assert found is not None

    def test_commitment_gained_last_activity_and_goal_link(self, settings):
        import sqlite3

        engine = db_module.init_db(settings)
        raw = sqlite3.connect(str(engine.url.database))
        columns = {row[1] for row in raw.execute("PRAGMA table_info(commitment)")}
        raw.close()

        assert "last_activity_at" in columns
        assert "goal_id" in columns
