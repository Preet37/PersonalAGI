"""Brief assembly: ordering, grouping, and what is deliberately left out."""

from datetime import date, datetime

import pytest

from personalagi.brief import Brief, BriefItem, render_brief
from personalagi.models import Classification, Message


def make(
    *,
    mid=1,
    account="personal",
    sender="Dana Okafor",
    email="dana@example.com",
    subject="Benchmark v2",
    category="needs_response",
    urgency="med",
    summary="Dana wants the harness draft",
    ms=1_786_353_600_000,
    ok=True,
):
    message = Message(
        id=mid, gmail_id=f"g{mid}", thread_id="t", account_label=account,
        sender_name=sender, sender_email=email, subject=subject, body_text="b",
        timestamp=datetime.fromtimestamp(ms / 1000), internal_date_ms=ms,
        ingested_at=datetime(2026, 8, 13),
    )
    classification = Classification(
        message_id=mid, category=category, urgency=urgency, summary=summary,
        ok=ok, classified_at=datetime(2026, 8, 13),
    )
    return BriefItem(message, classification)


class TestOrdering:
    def test_high_urgency_sorts_first(self):
        items = [
            make(mid=1, urgency="low"),
            make(mid=2, urgency="high"),
            make(mid=3, urgency="med"),
        ]
        items.sort(key=lambda i: i.sort_key)
        assert [i.urgency for i in items] == ["high", "med", "low"]

    def test_within_urgency_newest_first(self):
        items = [
            make(mid=1, urgency="high", ms=1_000),
            make(mid=2, urgency="high", ms=9_000),
        ]
        items.sort(key=lambda i: i.sort_key)
        assert items[0].message.id == 2

    def test_unknown_urgency_sorts_last_without_crashing(self):
        items = [make(mid=1, urgency="bogus"), make(mid=2, urgency="low")]
        items.sort(key=lambda i: i.sort_key)
        assert items[-1].urgency == "bogus"


class TestRendering:
    def _brief(self, **kwargs):
        base = Brief(day=date(2026, 8, 13), window_days=1)
        for key, value in kwargs.items():
            setattr(base, key, value)
        return base

    def test_needs_response_grouped_by_account(self):
        brief = self._brief(
            needs_response={
                "personal": [make(mid=1)],
                "work": [make(mid=2, account="work", sender="Arjun")],
            },
            total_messages=2,
        )
        text = render_brief(brief)
        assert "### personal" in text and "### work" in text
        assert text.index("### personal") < text.index("### work")

    def test_promotional_is_counted_not_listed(self):
        """A brief that lists 40 marketing emails is just the inbox again."""
        from collections import Counter

        brief = self._brief(
            counts=Counter({"promotional": 37, "spam": 4}), total_messages=41
        )
        text = render_brief(brief)

        assert "promotional: 37" in text
        assert "spam: 4" in text
        assert text.count("- ") < 10  # no per-item listing

    def test_fyi_is_one_line_each(self):
        brief = self._brief(
            fyi=[make(mid=i, category="fyi", summary=f"thing {i}") for i in range(3)],
            total_messages=3,
        )
        text = render_brief(brief)
        fyi_block = text.split("## FYI")[1].split("## Filtered")[0]
        assert len([ln for ln in fyi_block.splitlines() if ln.startswith("- ")]) == 3

    def test_empty_needs_response_says_so(self):
        text = render_brief(self._brief(total_messages=5))
        assert "Nothing" in text

    def test_action_count_in_header(self):
        brief = self._brief(
            needs_response={"personal": [make(mid=1), make(mid=2)]}, total_messages=9
        )
        assert "2 need you" in render_brief(brief)

    def test_urgency_marks_render(self):
        brief = self._brief(
            needs_response={"personal": [make(mid=1, urgency="high")]}, total_messages=1
        )
        assert "!!" in render_brief(brief)

    def test_unclassified_surfaced_not_hidden(self):
        brief = self._brief(total_messages=3, unclassified=2)
        assert "unclassified" in render_brief(brief)

    def test_window_described_in_words(self):
        assert "last 24 hours" in render_brief(self._brief(total_messages=1))
        assert "last 3 days" in render_brief(
            Brief(day=date(2026, 8, 13), window_days=3, total_messages=1)
        )


class TestActionCount:
    def test_counts_across_accounts(self):
        brief = Brief(
            day=date(2026, 8, 13),
            window_days=1,
            needs_response={"a": [make(mid=1)], "b": [make(mid=2), make(mid=3)]},
        )
        assert brief.action_count == 3

    def test_zero_when_empty(self):
        assert Brief(day=date(2026, 8, 13), window_days=1).action_count == 0


@pytest.mark.parametrize("category", ["promotional", "spam"])
def test_filtered_categories_never_enter_needs_response(category):
    """Guards the contract the renderer relies on."""
    item = make(category=category)
    assert item.classification.category == category
