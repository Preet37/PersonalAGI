"""Sampling is the part of the eval that was actually broken.

v1 drew 30 rows at random from an inbox that is ~95% automated and got ONE
needs_response. Precision 0.25 and recall 1.00 were both computed on that
single row, so neither number meant anything. These tests pin the two fixes:
sample humans only, and balance the classes.
"""

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session

from personalagi import db as db_module
from personalagi.adapters.base import resolve_participant
from personalagi.config import Settings
from personalagi.evals.harness import EvalError, generate_template, read_labels
from personalagi.identity import is_human_sender, looks_automated, shared_addresses
from personalagi.models import Classification, Event, Participant

BASE = datetime(2026, 8, 1, 9, 0)


@pytest.fixture
def settings(tmp_path):
    db_module._engine = None
    yield Settings(database_url=f"sqlite:///{tmp_path / 'eval.db'}")
    db_module._engine = None


def seed(settings, rows: list[tuple[str, str, str]]) -> None:
    """rows: (sender_email, sender_name, predicted_category).

    Seeds Events and Participants — the canonical shape — and resolves each
    sender through the real adapter path, so these tests exercise the same
    automated/human verdict the pipeline uses rather than a parallel one.
    """
    engine = db_module.init_db(settings)
    with Session(engine) as session:
        shared = shared_addresses((e, n) for e, n, _ in rows)
        for i, (email, name, category) in enumerate(rows):
            event = Event(
                source="gmail",
                source_id=f"g{i}",
                account_label="personal",
                thread_key=f"t{i}",
                title=f"subject {i}",
                text=f"body {i}",
                timestamp=BASE + timedelta(minutes=i),
                timestamp_ms=1_786_000_000_000 + i * 60_000,
                ingested_at=BASE,
            )
            session.add(event)
            session.flush()
            resolved = resolve_participant(email, name, shared=shared)
            session.add(
                Participant(event_id=event.id, **{
                    k: v for k, v in resolved.as_row(event.id).items()
                    if k != "event_id"
                })
            )
            session.add(
                Classification(
                    event_id=event.id,
                    category=category,
                    urgency="low",
                    summary="",
                    classified_at=BASE,
                )
            )
        session.commit()


def senders_in(path) -> list[str]:
    import csv

    with path.open(newline="", encoding="utf-8") as handle:
        return [row["sender_email"] for row in csv.DictReader(handle)]


class TestIdentity:
    def test_robot_addresses_are_not_human(self):
        for email in [
            "jobalerts-noreply@linkedin.com",
            "no-reply@x.com",
            "support@luma.com",
            "invitations@linkedin.com",
        ]:
            assert looks_automated(email) is True
            assert is_human_sender(email) is False

    def test_real_people_are_human(self):
        for email in ["grad-student@stanford.edu", "ryan@westfuller.com", "dana@example.com"]:
            assert is_human_sender(email) is True

    def test_a_shared_envelope_is_not_a_person_even_with_a_clean_address(self):
        """The 215-display-name rule, at the identity layer.

        `updates@somebrand.com` would be caught by the regex, but an address
        with no robot marker still is not a person if hundreds of different
        humans send through it.
        """
        pairs = [("bulk@brand.com", f"Person {i}") for i in range(10)]
        shared = shared_addresses(pairs)

        assert "bulk@brand.com" in shared
        assert looks_automated("bulk@brand.com") is False  # regex alone misses it
        assert is_human_sender("bulk@brand.com", shared) is False

    def test_one_person_using_one_address_is_not_shared(self):
        pairs = [("dana@example.com", "Dana Okafor")] * 50
        assert shared_addresses(pairs) == set()

    def test_empty_address_is_never_human(self):
        assert is_human_sender("") is False


class TestHumanOnlySampling:
    def test_robots_are_excluded(self, settings, tmp_path):
        seed(
            settings,
            [
                ("jobalerts-noreply@linkedin.com", "LinkedIn", "promotional"),
                ("no-reply@stripe.com", "Stripe", "fyi"),
                ("grad-student@stanford.edu", "Grace", "needs_response"),
                ("ryan@westfuller.com", "Ryan", "needs_response"),
            ],
        )
        out = tmp_path / "t.csv"

        generate_template(out, settings, n=10, human_only=True)

        assert set(senders_in(out)) == {"grad-student@stanford.edu", "ryan@westfuller.com"}

    def test_without_the_flag_the_old_behaviour_is_unchanged(self, settings, tmp_path):
        seed(
            settings,
            [
                ("jobalerts-noreply@linkedin.com", "LinkedIn", "promotional"),
                ("grad-student@stanford.edu", "Grace", "needs_response"),
            ],
        )
        out = tmp_path / "t.csv"

        generate_template(out, settings, n=10)

        assert "jobalerts-noreply@linkedin.com" in senders_in(out)

    def test_an_all_robot_inbox_fails_loudly(self, settings, tmp_path):
        """Silence here would hand back an empty CSV that looks like a valid one."""
        seed(settings, [("no-reply@x.com", "X", "promotional")])

        with pytest.raises(EvalError, match="no human senders"):
            generate_template(tmp_path / "t.csv", settings, n=10, human_only=True)


class TestStratifiedSampling:
    def test_a_rare_class_is_not_drowned_out(self, settings, tmp_path):
        """The v1 failure, reproduced and then fixed.

        40 promotional to 2 needs_response, with the human mail OLDER than the
        bulk — which is the realistic case, since bulk senders mail constantly
        and a person mails once. Sampling is newest-first, so a flat 10-row
        sample gets none of the class that matters; stratified it gets both.
        """
        rows = [
            ("grad-student@stanford.edu", "Grace", "needs_response"),
            ("ryan@westfuller.com", "Ryan", "needs_response"),
        ]
        rows += [(f"bulk{i}@brand{i}.com", f"Brand {i}", "promotional") for i in range(40)]
        seed(settings, rows)
        flat, balanced = tmp_path / "flat.csv", tmp_path / "balanced.csv"

        generate_template(flat, settings, n=10)
        generate_template(balanced, settings, n=10, stratify=True)

        assert "grad-student@stanford.edu" not in senders_in(flat)
        assert {"grad-student@stanford.edu", "ryan@westfuller.com"} <= set(
            senders_in(balanced)
        )

    def test_classes_come_out_roughly_even(self, settings, tmp_path):
        rows = []
        for category, count in [("promotional", 20), ("fyi", 20), ("needs_response", 20)]:
            rows += [
                (f"{category}{i}@example.com", f"P {i}", category) for i in range(count)
            ]
        seed(settings, rows)
        out = tmp_path / "t.csv"

        generate_template(out, settings, n=9, stratify=True)

        by_class = {}
        for sender in senders_in(out):
            key = sender.split("@")[0].rstrip("0123456789")
            by_class[key] = by_class.get(key, 0) + 1
        assert by_class == {"promotional": 3, "fyi": 3, "needs_response": 3}

    def test_stratify_does_not_invent_rows_when_a_class_is_empty(
        self, settings, tmp_path
    ):
        seed(settings, [("a@example.com", "A", "fyi"), ("b@example.com", "B", "fyi")])
        out = tmp_path / "t.csv"

        assert generate_template(out, settings, n=50, stratify=True) == 2


class TestTheV1LabelsStillRead:
    """labels.csv is the frozen baseline; nothing in Stage 7A may disturb it."""

    def test_the_recorded_baseline_still_parses(self):
        from pathlib import Path

        path = Path("evals/labels.csv")
        if not path.exists():  # not present in a fresh clone
            pytest.skip("no labels.csv")
        rows = read_labels(path)

        assert len(rows) == 30
        assert sum(r.true_category == "needs_response" for r in rows) == 1
