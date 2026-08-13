"""The idempotency guarantee that makes the watermark overlap window safe."""

from datetime import datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from personalagi.ingest.gmail import _insert_messages
from personalagi.models import Message


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def row(gmail_id: str, account: str = "personal", **overrides) -> dict:
    base = {
        "gmail_id": gmail_id,
        "thread_id": "t1",
        "account_label": account,
        "sender_name": "Dana Okafor",
        "sender_email": "dana@example.com",
        "subject": "Benchmark v2",
        "body_text": "body",
        "timestamp": datetime(2026, 8, 10, 9, 20),
        "internal_date_ms": 1_786_353_600_000,
        "ingested_at": datetime(2026, 8, 12, 0, 0),
    }
    base.update(overrides)
    return base


def test_first_insert_writes_everything(session):
    assert _insert_messages(session, [row("a"), row("b")]) == 2
    session.commit()
    assert len(session.exec(select(Message)).all()) == 2


def test_reingesting_the_same_messages_is_a_noop(session):
    _insert_messages(session, [row("a"), row("b")])
    session.commit()

    # This is what the overlap window causes on every incremental run.
    inserted = _insert_messages(session, [row("a"), row("b")])
    session.commit()

    assert inserted == 0
    assert len(session.exec(select(Message)).all()) == 2


def test_overlapping_batch_inserts_only_the_new(session):
    _insert_messages(session, [row("a"), row("b")])
    session.commit()

    inserted = _insert_messages(session, [row("b"), row("c")])
    session.commit()

    assert inserted == 1
    assert {m.gmail_id for m in session.exec(select(Message)).all()} == {"a", "b", "c"}


def test_same_message_in_two_accounts_is_two_rows(session):
    # Deliberate: the copies have different labels and visibility, so the
    # uniqueness key is (account_label, gmail_id), not gmail_id alone.
    inserted = _insert_messages(session, [row("a", "personal"), row("a", "work")])
    session.commit()

    assert inserted == 2
    assert len(session.exec(select(Message)).all()) == 2


def test_chunking_handles_batches_over_the_sqlite_variable_limit(session):
    rows = [row(f"m{i}") for i in range(250)]
    assert _insert_messages(session, rows) == 250
    session.commit()
    assert len(session.exec(select(Message)).all()) == 250
