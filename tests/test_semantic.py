"""The semantic layer: the model decides, keyword search only nominates.

The system was full of lexical rules standing in for understanding. They failed
where you would expect — `letter` inside `newsletter`, and a credit-card
application "supporting" a Carnegie Mellon one. The fix is not a better regex;
it is moving the DECISION to something that can read.

These tests pin the properties that make that safe: it fails closed, it never
silently drops a candidate, and it caches so the nightly run does not re-pay
for an answer it already has.
"""

import json
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, select

from personalagi import db as db_module
from personalagi.config import Settings
from personalagi.llm.client import LLMError
from personalagi.models import Event, Judgement
from personalagi.records import CallBudget
from personalagi.semantic import judge_evidence, question_hash

NOW = datetime(2026, 8, 13, 9, 0)


@pytest.fixture
def settings(tmp_path):
    db_module._engine = None
    yield Settings(
        database_url=f"sqlite:///{tmp_path / 'j.db'}",
        groq_api_key="gsk_test_key_not_a_placeholder",
    )
    db_module._engine = None


def make_events(settings, specs):
    engine = db_module.init_db(settings)
    out = []
    with Session(engine) as session:
        for source_id, title, text in specs:
            event = Event(
                source="gmail", source_id=source_id, title=title, text=text,
                timestamp=NOW, timestamp_ms=1, ingested_at=NOW,
            )
            session.add(event)
            session.flush()
            session.refresh(event)
            session.expunge(event)
            out.append(event)
        session.commit()
    return out


class FakeClient:
    """Stands in for GroqClient. Records calls so cost is assertable."""

    model = "fake"

    def __init__(self, verdicts=None, error=None, malformed=False):
        self.verdicts = verdicts or {}
        self.error = error
        self.malformed = malformed
        self.calls = 0

    def complete_json(self, system, user, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        if self.malformed:
            return "not json at all"
        import re

        ids = [int(i) for i in re.findall(r"\[(\d+)\]", user)]
        return json.dumps(
            {
                "verdicts": [
                    {
                        "id": i,
                        "supports": self.verdicts.get(i, False),
                        "why": "because" if self.verdicts.get(i) else "unrelated",
                    }
                    for i in ids
                ]
            }
        )


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def install(client):
        holder["client"] = client
        monkeypatch.setattr(
            "personalagi.semantic.GroqClient", lambda *a, **k: client
        )
        return client

    return install


class TestJudgement:
    def test_it_keeps_what_the_model_supports(self, settings, fake):
        events = make_events(settings, [("a", "CMU", "your CMU application")])
        fake(FakeClient(verdicts={events[0].id: True}))

        result = judge_evidence("Submit the CMU application", events, settings)

        assert len(result.supported) == 1

    def test_it_rejects_a_lexical_lookalike(self, settings, fake):
        """The case the keyword layer structurally cannot get right: both
        contain 'submit' and 'application', only one is Carnegie Mellon."""
        events = make_events(
            settings, [("a", "Card", "Submit your credit card application today")]
        )
        fake(FakeClient(verdicts={}))

        result = judge_evidence("Submit the CMU application", events, settings)

        assert result.supported == []

    def test_every_candidate_gets_a_verdict(self, settings, fake):
        """A missing verdict and a negative verdict must never look the same."""
        events = make_events(
            settings, [("a", "", "one"), ("b", "", "two"), ("c", "", "three")]
        )
        fake(FakeClient(verdicts={events[0].id: True}))

        result = judge_evidence("task", events, settings)

        assert len(result.verdicts) == 3

    def test_no_candidates_costs_nothing(self, settings, fake):
        client = fake(FakeClient())

        result = judge_evidence("task", [], settings)

        assert client.calls == 0
        assert result.verdicts == []


class TestFailsClosed:
    """A false 'supports' marks a task handled and the system goes SILENT.
    A false 'does not support' leaves an alert firing. Only one is recoverable.
    """

    def test_a_model_error_yields_unsupported(self, settings, fake):
        events = make_events(settings, [("a", "", "x")])
        fake(FakeClient(error=LLMError("groq exploded")))

        result = judge_evidence("task", events, settings)

        assert result.supported == []
        assert result.failed == 1
        assert "model error" in result.verdicts[0].why

    def test_malformed_json_yields_unsupported(self, settings, fake):
        events = make_events(settings, [("a", "", "x")])
        fake(FakeClient(malformed=True))

        result = judge_evidence("task", events, settings)

        assert result.supported == []

    def test_a_missing_verdict_yields_unsupported(self, settings, fake):
        events = make_events(settings, [("a", "", "x")])

        class Silent(FakeClient):
            def complete_json(self, system, user, **kwargs):
                self.calls += 1
                return json.dumps({"verdicts": []})

        fake(Silent())

        result = judge_evidence("task", events, settings)

        assert result.supported == []
        assert "no verdict" in result.verdicts[0].why

    def test_an_exhausted_budget_yields_unsupported_and_says_so(self, settings, fake):
        """A truncated run must not masquerade as a set of negative findings."""
        events = make_events(settings, [("a", "", "x")])
        fake(FakeClient(verdicts={events[0].id: True}))

        result = judge_evidence("task", events, settings, budget=CallBudget(limit=0))

        assert result.supported == []
        assert "budget" in result.verdicts[0].why


class TestCaching:
    def test_a_repeat_question_costs_no_call(self, settings, fake):
        events = make_events(settings, [("a", "CMU", "your CMU application")])
        client = fake(FakeClient(verdicts={events[0].id: True}))

        judge_evidence("Submit the CMU application", events, settings)
        first = client.calls
        again = judge_evidence("Submit the CMU application", events, settings)

        assert client.calls == first
        assert again.cache_hits == 1
        assert len(again.supported) == 1

    def test_editing_the_task_invalidates_the_cache(self, settings, fake):
        """Otherwise a reworded step silently reuses a verdict about a
        different question."""
        events = make_events(settings, [("a", "", "x")])
        client = fake(FakeClient())

        judge_evidence("Submit the CMU application", events, settings)
        judge_evidence("Ask Pratik for a letter", events, settings)

        assert client.calls == 2

    def test_whitespace_and_case_do_not_invalidate_it(self):
        assert question_hash("Submit  The CMU Application", 1, "evidence") == (
            question_hash("submit the cmu application", 1, "evidence")
        )

    def test_the_verdict_is_stored_with_its_reasoning(self, settings, fake):
        events = make_events(settings, [("a", "", "x")])
        fake(FakeClient(verdicts={events[0].id: True}))

        judge_evidence("task", events, settings)

        with Session(db_module.get_engine(settings)) as session:
            row = session.execute(select(Judgement)).scalars().first()

        assert row.verdict is True
        assert row.why
        assert row.model == "fake"


class TestBatching:
    def test_many_candidates_do_not_mean_many_calls(self, settings, fake):
        """Per-candidate calls would multiply the cost of the one stage that is
        supposed to be expensive-but-rare."""
        events = make_events(settings, [(f"e{i}", "", f"text {i}") for i in range(8)])
        client = fake(FakeClient())

        judge_evidence("task", events, settings)

        assert client.calls == 1

    def test_the_budget_bounds_the_calls(self, settings, fake):
        events = make_events(settings, [(f"e{i}", "", f"text {i}") for i in range(40)])
        client = fake(FakeClient())

        judge_evidence("task", events, settings, budget=CallBudget(limit=2))

        assert client.calls <= 2
