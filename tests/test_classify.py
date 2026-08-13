"""Classification boundary behavior: validation, retry, and tombstones.

No network. classify_one takes a client, so a fake one exercises every path
including the ones a real API would only produce intermittently.
"""


import pytest

from personalagi.llm import classify as classify_module
from personalagi.llm.prompts import Prompt, PromptError, load_prompt
from personalagi.llm.schemas import ClassificationOut
from tests.factories import make_view


class FakeClient:
    """Returns queued responses; raises whatever is queued as an exception."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.model = "fake-model"

    def complete_json(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def prompt():
    return Prompt(
        name="classify",
        system="classify it",
        user_template="From: {sender_name} <{sender_email}>\n{date}\n{subject}\n{body}",
        version="testv1",
    )


@pytest.fixture
def message():
    return make_view(
        eid=1,
        sender="Dana Okafor",
        email="dana@example.com",
        title="Benchmark v2",
        text="Can you send the harness draft by the 24th?",
    )


class TestClassifyOne:
    def test_valid_response(self, prompt, message):
        client = FakeClient(
            ['{"category":"needs_response","urgency":"high","summary":"Dana wants the draft"}']
        )
        result, error = classify_module.classify_one(client, prompt, message)

        assert error is None
        assert result.category == "needs_response"
        assert result.urgency == "high"
        assert len(client.calls) == 1

    def test_retries_once_on_malformed_json(self, prompt, message):
        client = FakeClient(
            ["not json at all", '{"category":"fyi","urgency":"low","summary":"ok"}']
        )
        result, error = classify_module.classify_one(client, prompt, message)

        assert error is None
        assert result.category == "fyi"
        assert len(client.calls) == 2

    def test_retry_prompt_differs_from_the_first(self, prompt, message):
        """Repeating an identical failing prompt at temperature 0 mostly
        reproduces the same failure, so the retry adds a correction."""
        client = FakeClient(["garbage", '{"category":"fyi"}'])
        classify_module.classify_one(client, prompt, message)

        assert client.calls[0]["user"] != client.calls[1]["user"]
        assert "ONLY a JSON object" in client.calls[1]["user"]

    def test_gives_up_after_two_attempts(self, prompt, message):
        client = FakeClient(["bad", "still bad"])
        result, error = classify_module.classify_one(client, prompt, message)

        assert result is None
        assert error is not None
        assert len(client.calls) == 2

    def test_invalid_category_is_a_failure_not_a_guess(self, prompt, message):
        client = FakeClient(
            ['{"category":"important","urgency":"low","summary":"x"}'] * 2
        )
        result, error = classify_module.classify_one(client, prompt, message)

        assert result is None
        assert "category" in error

    def test_body_is_truncated(self, prompt, message):
        message.event.text = "x" * 5000
        rendered = classify_module.render_message(message)

        assert len(rendered["body"]) < 2000
        assert rendered["body"].endswith("[...truncated]")

    def test_empty_body_does_not_render_blank(self, prompt, message):
        message.event.text = ""
        assert classify_module.render_message(message)["body"] == "(empty body)"


class TestSchemaNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("NEEDS_RESPONSE", "needs_response"),
            ("needs response", "needs_response"),
            ("Needs-Response", "needs_response"),
            ("Promo", "promotional"),
            ("marketing", "promotional"),
            ("informational", "fyi"),
            ("phishing", "spam"),
        ],
    )
    def test_category_aliases(self, raw, expected):
        assert ClassificationOut(category=raw).category == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("medium", "med"), ("Urgent", "high"), ("NORMAL", "med"), ("none", "low")],
    )
    def test_urgency_aliases(self, raw, expected):
        assert ClassificationOut(category="fyi", urgency=raw).urgency == expected

    def test_summary_is_flattened_to_one_line(self):
        out = ClassificationOut(category="fyi", summary="line one\n\nline  two")
        assert out.summary == "line one line two"

    def test_extra_keys_ignored(self):
        out = ClassificationOut.model_validate(
            {"category": "fyi", "confidence": 0.9, "reasoning": "..."}
        )
        assert out.category == "fyi"

    def test_urgency_defaults_when_omitted(self):
        assert ClassificationOut(category="spam").urgency == "low"


class TestPromptLoading:
    def test_real_prompt_file_parses(self):
        prompt = load_prompt("classify")
        assert "category" in prompt.system
        assert "{subject}" in prompt.user_template
        assert len(prompt.version) == 12

    def test_version_changes_with_content(self, tmp_path):
        (tmp_path / "a.md").write_text("# System\nA\n\n# User\n{x}\n")
        (tmp_path / "b.md").write_text("# System\nB\n\n# User\n{x}\n")
        assert load_prompt("a", tmp_path).version != load_prompt("b", tmp_path).version

    def test_missing_section_raises(self, tmp_path):
        (tmp_path / "bad.md").write_text("# System\nonly a system section\n")
        with pytest.raises(PromptError, match="missing section"):
            load_prompt("bad", tmp_path)

    def test_missing_field_raises_with_the_field_name(self, prompt):
        with pytest.raises(PromptError, match="sender_name"):
            prompt.render_user(subject="s")
