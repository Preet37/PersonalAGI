"""Compaction invariants.

The correction tests are the important ones — they are the mechanism that
answers "the profile is wrong, how do I stop the nightly job reintroducing
the error" (ARCHITECTURE.md open question 4).
"""

from datetime import date

import pytest

from personalagi.context.compact import (
    _fingerprint,
    add_correction,
    append_history,
    compact_person,
    enforce_word_cap,
    read_history,
)
from personalagi.context.people import LogEntry, PersonFile, load_person, save_person


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        self.model = "fake-model"

    def complete_json(self, system, user, **kwargs):
        self.prompts.append(user)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakePrompt:
    name = "compact"
    version = "v1"
    system = "compact it"

    def render_user(self, **fields):
        return (
            f"CORRECTIONS:\n{fields['corrections']}\n"
            f"PROFILE:\n{fields['profile']}\n"
            f"ENTRIES:\n{fields['entries']}"
        )


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "people").mkdir(parents=True)
    return tmp_path


def person_with_log(n=5) -> PersonFile:
    return PersonFile(
        slug="dana-okafor",
        name="Dana Okafor",
        emails=["dana@example.com"],
        log=[LogEntry(date(2026, 8, 10 - i), f"entry {i}", f"g{i}") for i in range(n)],
    )


class TestWordCap:
    def test_under_cap_untouched(self):
        text = "Short profile."
        assert enforce_word_cap(text, 150) == text

    def test_over_cap_truncated(self):
        text = " ".join(["word"] * 300)
        assert len(enforce_word_cap(text, 150).split()) <= 150

    def test_prefers_sentence_boundary(self):
        text = "First sentence here. " + " ".join(["filler"] * 200)
        out = enforce_word_cap(text, 150)
        assert out.endswith((".", "!", "?"))

    def test_cap_is_enforced_even_when_the_model_ignores_it(self):
        """The prompt asking for 150 words is not a guarantee."""
        client = FakeClient(['{"profile": "' + " ".join(["word"] * 400) + '"}'])
        profile, error = compact_person(person_with_log(), client, FakePrompt())

        assert error is None
        assert len(profile.split()) <= 150


class TestCorrections:
    def test_correction_reaches_the_prompt(self, vault):
        person = person_with_log()
        person.corrections = ["Dana left Example Labs in June 2026"]
        client = FakeClient(['{"profile": "Updated."}'])

        compact_person(person, client, FakePrompt())
        assert "left Example Labs" in client.prompts[0]

    def test_no_corrections_renders_as_none_not_blank(self, vault):
        client = FakeClient(['{"profile": "x"}'])
        compact_person(person_with_log(), client, FakePrompt())
        assert "(none)" in client.prompts[0]

    def test_correction_survives_compaction(self, vault):
        person = person_with_log()
        person.corrections = ["Dana is not at Example Labs"]
        save_person(vault, person)

        reloaded = load_person(vault, "dana-okafor")
        reloaded.profile = "A newly generated profile."
        save_person(vault, reloaded)

        # The whole point: the model rewrites the profile, never the correction.
        assert load_person(vault, "dana-okafor").corrections == [
            "Dana is not at Example Labs"
        ]

    def test_add_correction_does_not_touch_the_profile(self, vault):
        person = person_with_log()
        person.profile = "Stale profile claiming Example Labs."
        save_person(vault, person)

        add_correction("dana-okafor", "Dana left in June", context_dir=vault)

        after = load_person(vault, "dana-okafor")
        assert after.profile == "Stale profile claiming Example Labs."
        assert after.corrections == ["Dana left in June"]

    def test_add_correction_is_idempotent(self, vault):
        save_person(vault, person_with_log())
        add_correction("dana-okafor", "same text", context_dir=vault)
        add_correction("dana-okafor", "same text", context_dir=vault)

        assert len(load_person(vault, "dana-okafor").corrections) == 1

    def test_correction_changes_the_fingerprint(self, vault):
        person = person_with_log()
        before = _fingerprint(person)
        person.corrections = ["something new"]

        # Otherwise a correction would be skipped as "nothing changed" and
        # never take effect.
        assert _fingerprint(person) != before

    def test_add_correction_unknown_person_raises(self, vault):
        with pytest.raises(LookupError):
            add_correction("nobody", "x", context_dir=vault)


class TestLogPreservation:
    def test_compaction_never_deletes_log_lines(self, vault):
        person = person_with_log(6)
        save_person(vault, person)

        reloaded = load_person(vault, "dana-okafor")
        reloaded.profile = "Rewritten."
        save_person(vault, reloaded)

        assert len(load_person(vault, "dana-okafor").log) == 6


class TestHistory:
    def test_history_appends_a_version_per_compaction(self, vault):
        person = person_with_log()
        for i in range(3):
            append_history(
                vault, person, profile=f"version {i}", fingerprint=f"f{i}",
                model="m", prompt_version="v1", entries_considered=5,
            )

        versions = read_history(vault, "dana-okafor")
        assert len(versions) == 3
        assert [v["profile"] for v in versions] == ["version 0", "version 1", "version 2"]

    def test_history_records_corrections_in_force(self, vault):
        person = person_with_log()
        person.corrections = ["c1"]
        append_history(
            vault, person, profile="p", fingerprint="f",
            model="m", prompt_version="v1", entries_considered=1,
        )
        assert read_history(vault, "dana-okafor")[0]["corrections"] == ["c1"]

    def test_missing_history_is_empty_not_an_error(self, vault):
        assert read_history(vault, "nobody") == []

    def test_corrupt_history_line_is_skipped_not_fatal(self, vault):
        path = vault / "history" / "dana-okafor.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"profile": "good"}\nnot json at all\n')

        assert len(read_history(vault, "dana-okafor")) == 1


class TestFingerprint:
    def test_stable_when_nothing_changes(self):
        assert _fingerprint(person_with_log()) == _fingerprint(person_with_log())

    def test_changes_when_an_entry_is_added(self):
        person = person_with_log()
        before = _fingerprint(person)
        person.add_entry(LogEntry(date(2026, 8, 20), "new", "gnew"))
        assert _fingerprint(person) != before


class TestFailureHandling:
    def test_malformed_output_retries_then_reports(self):
        client = FakeClient(["not json", "still not json"])
        profile, error = compact_person(person_with_log(), client, FakePrompt())

        assert profile is None
        assert error is not None
        assert len(client.prompts) == 2

    def test_recovers_on_the_retry(self):
        client = FakeClient(["bad", '{"profile": "recovered"}'])
        profile, error = compact_person(person_with_log(), client, FakePrompt())

        assert profile == "recovered"
        assert error is None
