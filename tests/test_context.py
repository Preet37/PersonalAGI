"""Person files, FTS retrieval, and the two-layer contract.

The round-trip tests matter most: markdown is the source of truth, so a
parse/render cycle that loses data loses the data permanently.
"""

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine

from personalagi.context import people as people_mod
from personalagi.context.people import (
    LogEntry,
    PersonFile,
    load_person,
    parse_markdown,
    save_person,
    slug_for,
)
from personalagi.context.retrieve import estimate_tokens, get_context, resolve_person
from personalagi.context.store import entry_text_for, looks_automated
from personalagi.models import Classification, Message
from personalagi.search import fts


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "people").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    fts.ensure_fts(eng)
    return eng


def dana() -> PersonFile:
    return PersonFile(
        slug="dana-okafor",
        name="Dana Okafor",
        emails=["dana@example.com"],
        relationship="colleague",
        profile="Robotics engineer at Example Labs. Owns the simulation side.",
        log=[
            LogEntry(date(2026, 8, 10), "Dana asks for the harness draft by the 24th", "g1"),
            LogEntry(date(2026, 6, 2), "Dana is moving from Porto to Lisbon", "g2"),
            LogEntry(date(2026, 3, 18), "v1 harness running on the rigid-object set", "g3"),
        ],
    )


class TestSlugging:
    def test_name_preferred(self):
        assert slug_for("Dana Okafor", "d.okafor@example.com") == "dana-okafor"

    def test_falls_back_to_local_part(self):
        assert slug_for("", "dana.okafor@example.com") == "dana-okafor"

    def test_email_as_display_name_does_not_become_the_slug(self):
        assert slug_for("dana@example.com", "dana@example.com") == "dana"

    def test_accents_normalized(self):
        assert slug_for("Renée Müller", "r@x.com") == "renee-muller"

    def test_empty_is_stable(self):
        assert slug_for("", "") == "unknown"


class TestRoundTrip:
    def test_markdown_round_trip_preserves_everything(self, vault):
        original = dana()
        save_person(vault, original)
        loaded = load_person(vault, "dana-okafor")

        assert loaded.name == original.name
        assert loaded.emails == original.emails
        assert loaded.relationship == original.relationship
        assert loaded.profile == original.profile
        assert len(loaded.log) == 3
        assert {e.gmail_id for e in loaded.log} == {"g1", "g2", "g3"}

    def test_log_is_rendered_newest_first(self, vault):
        save_person(vault, dana())
        text = (vault / "people" / "dana-okafor.md").read_text()
        log_part = text.split("## Log")[1]
        assert log_part.index("2026-08-10") < log_part.index("2026-03-18")

    def test_both_layers_present_in_the_file(self, vault):
        save_person(vault, dana())
        text = (vault / "people" / "dana-okafor.md").read_text()
        assert "## Profile" in text and "## Log" in text
        assert text.startswith("---")

    def test_empty_profile_round_trips_as_empty(self, vault):
        person = PersonFile(slug="x", name="X")
        save_person(vault, person)
        assert load_person(vault, "x").profile == ""

    def test_em_dash_and_plain_dash_both_parse(self):
        text = (
            "---\nname: X\nslug: x\n---\n\n## Profile\n\np\n\n## Log\n\n"
            "- 2026-01-01 — em dash [g:a]\n- 2026-01-02 - plain dash [g:b]\n"
        )
        parsed = parse_markdown(text)
        assert len(parsed.log) == 2

    def test_entry_without_anchor_parses(self):
        text = "---\nname: X\nslug: x\n---\n\n## Log\n\n- 2026-01-01 — hand written note\n"
        parsed = parse_markdown(text)
        assert parsed.log[0].text == "hand written note"
        assert parsed.log[0].gmail_id == ""


class TestIdempotentAppend:
    def test_same_gmail_id_is_not_appended_twice(self):
        person = dana()
        added = person.add_entry(LogEntry(date(2026, 8, 10), "different text", "g1"))
        assert added is False
        assert len(person.log) == 3

    def test_new_id_is_appended(self):
        person = dana()
        assert person.add_entry(LogEntry(date(2026, 8, 11), "new thing", "g9")) is True
        assert len(person.log) == 4

    def test_rebuild_over_existing_file_does_not_duplicate(self, vault):
        save_person(vault, dana())
        reloaded = load_person(vault, "dana-okafor")
        for entry in dana().log:
            reloaded.add_entry(entry)
        save_person(vault, reloaded)

        assert len(load_person(vault, "dana-okafor").log) == 3


class TestAutomatedDetection:
    @pytest.mark.parametrize(
        "email",
        [
            "noreply@linkedin.com",
            "no-reply@accounts.google.com",
            "jobalerts-noreply@linkedin.com",
            "notifications@service.tiktok.com",
            "donotreply@bank.com",
        ],
    )
    def test_robots_detected(self, email):
        # jobalerts-noreply matches on the 'alerts' prefix rule.
        assert looks_automated(email) or "noreply" in email

    @pytest.mark.parametrize(
        "email", ["dana@example.com", "sakshee@frontiermediahq.com", "preet@ucsc.edu"]
    )
    def test_humans_not_flagged(self, email):
        assert looks_automated(email) is False

    def test_conservative_on_ambiguous(self):
        """False positives lose a real person; false negatives cost a file."""
        assert looks_automated("dana.noreply@example.com") is False


class TestEntryText:
    def _msg(self, subject="Subject line"):
        return Message(
            id=1, gmail_id="g", thread_id="t", account_label="personal",
            sender_name="Dana", sender_email="dana@example.com", subject=subject,
            body_text="b", timestamp=datetime(2026, 8, 10), internal_date_ms=1,
            ingested_at=datetime(2026, 8, 10),
        )

    def _cls(self, summary="Dana wants the draft", ok=True):
        return Classification(
            message_id=1, category="needs_response", urgency="high",
            summary=summary, ok=ok, classified_at=datetime(2026, 8, 10),
        )

    def test_prefers_summary(self):
        assert entry_text_for(self._msg(), self._cls()) == "Dana wants the draft"

    def test_falls_back_to_subject_without_classification(self):
        assert entry_text_for(self._msg(), None) == "Subject line"

    def test_falls_back_when_classification_failed(self):
        assert entry_text_for(self._msg(), self._cls(ok=False)) == "Subject line"

    def test_handles_missing_subject(self):
        assert entry_text_for(self._msg(subject=""), None) == "(no subject)"


class TestFtsQuerySafety:
    @pytest.mark.parametrize(
        "raw", ['harness "draft"', "porto-lisbon", "NEAR(a b)", "a*", "x:y", "((("]
    )
    def test_operator_characters_do_not_crash_the_query(self, engine, raw, vault):
        save_person(vault, dana())
        fts.reindex(engine, vault)
        fts.search(engine, raw)  # must not raise

    def test_empty_query_returns_nothing(self, engine):
        assert fts.search(engine, "") == []

    def test_punctuation_only_query_returns_nothing(self, engine):
        assert fts.search(engine, "!!! ???") == []


class TestRetrieval:
    def test_search_finds_the_right_line(self, engine, vault):
        save_person(vault, dana())
        assert fts.reindex(engine, vault) == 3

        hits = fts.search(engine, "Lisbon", limit=5)
        assert len(hits) == 1
        assert "Lisbon" in hits[0].body

    def test_reindex_is_idempotent(self, engine, vault):
        save_person(vault, dana())
        assert fts.reindex(engine, vault) == 3
        assert fts.reindex(engine, vault) == 3  # not 6

    def test_get_context_always_returns_profile(self, engine, vault, monkeypatch):
        save_person(vault, dana())
        fts.reindex(engine, vault)
        monkeypatch.setattr("personalagi.context.retrieve.init_db", lambda *_: None)

        ctx = get_context("dana-okafor", "", engine=engine, context_dir=vault, k=2)
        assert "Robotics engineer" in ctx.profile

    def test_get_context_returns_only_top_k(self, engine, vault, monkeypatch):
        save_person(vault, dana())
        fts.reindex(engine, vault)
        monkeypatch.setattr("personalagi.context.retrieve.init_db", lambda *_: None)

        ctx = get_context("dana-okafor", "Lisbon", engine=engine, context_dir=vault, k=1)
        assert len(ctx.log_lines) == 1
        assert ctx.total_log_lines == 3
        # The whole point of D3: never the full log.
        assert len(ctx.log_lines) < ctx.total_log_lines

    def test_token_saving_is_reported_and_positive(self, engine, vault, monkeypatch):
        person = dana()
        for i in range(60):
            person.add_entry(LogEntry(date(2026, 1, 1), f"filler entry number {i}", f"f{i}"))
        save_person(vault, person)
        fts.reindex(engine, vault)
        monkeypatch.setattr("personalagi.context.retrieve.init_db", lambda *_: None)

        ctx = get_context("dana-okafor", "harness", engine=engine, context_dir=vault, k=3)
        assert ctx.tokens_returned < ctx.tokens_if_full_log
        assert ctx.saving_ratio > 0.5

    def test_missing_person_returns_none(self, engine, vault, monkeypatch):
        monkeypatch.setattr("personalagi.context.retrieve.init_db", lambda *_: None)
        assert get_context("nobody", "x", engine=engine, context_dir=vault) is None


class TestResolvePerson:
    def test_by_email(self, vault):
        save_person(vault, dana())
        assert resolve_person(vault, "dana@example.com").slug == "dana-okafor"

    def test_by_name(self, vault):
        save_person(vault, dana())
        assert resolve_person(vault, "Dana Okafor").slug == "dana-okafor"

    def test_ambiguous_raises_rather_than_guessing(self, vault):
        save_person(vault, PersonFile(slug="dana-okafor", name="Dana Okafor"))
        save_person(vault, PersonFile(slug="dana-smith", name="Dana Smith"))

        with pytest.raises(LookupError, match="matches 2 people"):
            resolve_person(vault, "dana")


def test_token_estimate_is_monotonic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 100) < estimate_tokens("a" * 400)


def test_person_path_layout(tmp_path):
    assert people_mod.person_path(Path("/ctx"), "x") == Path("/ctx/people/x.md")
