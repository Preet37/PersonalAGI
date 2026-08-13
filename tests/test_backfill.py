"""Regression tests for the bootstrap truncation bug.

The bug: `ingest --limit N` on a fresh account fetched the NEWEST n messages
(Gmail lists newest-first), then advanced the forward watermark as though the
mailbox were fully ingested. Every later run asked "what's new since then",
got nothing, and the older mail was permanently unreachable with no record
that it was missing and no command to recover it.

The unit tests missed it because they modelled the cap and the cursor
separately. The bug lived in the interaction, so the tests here drive
ingest_account end to end against a fake mailbox.
"""

import base64
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

import personalagi.db as db_module
from personalagi.config import Settings
from personalagi.ingest import gmail
from personalagi.models import IngestState, Message

TOTAL_MESSAGES = 10


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def build_corpus(n: int = TOTAL_MESSAGES) -> list[dict]:
    """n messages, newest first — the order the real Gmail API returns."""
    base = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    corpus = []
    for i in range(n):
        ts = base - timedelta(days=i)
        corpus.append(
            {
                "id": f"m{i}",
                "threadId": f"t{i}",
                "internalDate": str(int(ts.timestamp() * 1000)),
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": f"Person {i} <p{i}@example.com>"},
                        {"name": "Subject", "value": f"Message {i}"},
                    ],
                    "body": {"data": b64(f"body of message {i}")},
                },
            }
        )
    return corpus


class FakeRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeMessages:
    """Honors after:/before: in q, and returns newest-first like the real API."""

    def __init__(self, corpus):
        self.corpus = corpus
        self.queries = []

    def list(self, userId=None, q="", pageToken=None, maxResults=500):  # noqa: N803
        self.queries.append(q)
        after = before = None
        for token in q.split():
            if token.startswith("after:"):
                after = int(token.removeprefix("after:"))
            elif token.startswith("before:"):
                before = int(token.removeprefix("before:"))

        hits = []
        for msg in self.corpus:  # already newest-first
            secs = int(msg["internalDate"]) // 1000
            if after is not None and secs <= after:
                continue
            if before is not None and secs >= before:
                continue
            hits.append({"id": msg["id"]})
        return FakeRequest({"messages": hits})

    def get(self, userId=None, id=None, format=None):  # noqa: A002, N803
        for msg in self.corpus:
            if msg["id"] == id:
                return FakeRequest(msg)
        raise AssertionError(f"unknown message {id}")


class FakeHistory:
    def __init__(self):
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        # Nothing new since the forward cursor — the steady state after a
        # bootstrap. This is exactly what made the bug look like success.
        return FakeRequest({"history": []})


class FakeUsers:
    def __init__(self, corpus):
        self._messages = FakeMessages(corpus)
        self._history = FakeHistory()

    def getProfile(self, **_):  # noqa: N802
        return FakeRequest({"historyId": "99999"})

    def messages(self):
        return self._messages

    def history(self):
        return self._history


class FakeService:
    def __init__(self, corpus):
        self._users = FakeUsers(corpus)

    def users(self):
        return self._users


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp DB + a fake Gmail, wired into the orchestrator."""
    db_module._engine = None  # module-level engine cache
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        gmail_accounts="personal",
        initial_backfill_days=3650,
        ingest_overlap_seconds=0,
        ingest_max_messages=0,
    )
    service = FakeService(build_corpus())
    monkeypatch.setattr(gmail, "load_credentials", lambda *a, **k: object())
    monkeypatch.setattr(gmail, "build_service", lambda *a, **k: service)
    yield settings, service
    db_module._engine = None


def stored(settings) -> list[Message]:
    with Session(db_module.get_engine(settings)) as s:
        return list(s.exec(select(Message)))


def state_of(settings, label="personal") -> IngestState:
    with Session(db_module.get_engine(settings)) as s:
        return s.get(IngestState, label)


class TestTheBug:
    def test_capped_bootstrap_fetches_only_the_newest(self, env):
        """Baseline: confirms the cap keeps the NEWEST n, not an arbitrary n."""
        settings, _ = env
        gmail.ingest_account("personal", settings, max_messages=3)

        ids = {m.gmail_id for m in stored(settings)}
        assert ids == {"m0", "m1", "m2"}  # m0 is newest

    def test_capped_bootstrap_is_recorded_as_truncated(self, env):
        """THE REGRESSION. Previously nothing recorded that mail was missing."""
        settings, _ = env
        result = gmail.ingest_account("personal", settings, max_messages=3)

        assert result.truncated is True
        st = state_of(settings)
        assert st.last_run_truncated is True
        assert st.backfill_complete is False

    def test_backfill_cursor_marks_the_floor_not_the_ceiling(self, env):
        settings, _ = env
        gmail.ingest_account("personal", settings, max_messages=3)

        st = state_of(settings)
        oldest_fetched = min(int(m.internal_date_ms) for m in stored(settings))
        # The floor is the oldest message we actually have — the boundary the
        # hole starts at. Not the newest, which is the forward cursor.
        assert st.oldest_internal_date_ms == oldest_fetched
        assert st.last_internal_date_ms > st.oldest_internal_date_ms

    def test_older_mail_is_recoverable_after_a_capped_bootstrap(self, env):
        """The bug's actual consequence: mail was unreachable forever."""
        settings, _ = env
        gmail.ingest_account("personal", settings, max_messages=3)

        # Re-running ingest finds nothing: the forward cursor is caught up.
        again = gmail.ingest_account("personal", settings)
        assert again.fetched == 0
        assert len(stored(settings)) == 3

        # Backfill is the recovery path the bug had no answer for.
        gmail.backfill_account("personal", settings)

        assert len(stored(settings)) == TOTAL_MESSAGES
        assert state_of(settings).backfill_complete is True

    def test_forward_run_does_not_raise_the_backfill_floor(self, env):
        """A forward run's oldest message is newer than the floor.

        If it raised the floor, the record of the hole would be erased and
        backfill would silently skip the gap — the original bug, reintroduced
        through a different door.
        """
        settings, _ = env
        gmail.ingest_account("personal", settings, max_messages=3)
        floor_before = state_of(settings).oldest_internal_date_ms

        gmail.ingest_account("personal", settings)
        assert state_of(settings).oldest_internal_date_ms == floor_before


class TestBackfill:
    def test_uncapped_bootstrap_then_backfill_completes(self, env):
        settings, _ = env
        gmail.ingest_account("personal", settings)
        assert len(stored(settings)) == TOTAL_MESSAGES

        # The cursor second is re-listed by design (see backfill_account), so
        # it re-fetches the boundary message and inserts nothing.
        result = gmail.backfill_account("personal", settings)
        assert result.inserted == 0
        assert state_of(settings).backfill_complete is True

    def test_backfill_is_idempotent(self, env):
        settings, _ = env
        gmail.ingest_account("personal", settings, max_messages=3)
        gmail.backfill_account("personal", settings)
        count = len(stored(settings))

        second = gmail.backfill_account("personal", settings)
        assert second.inserted == 0
        assert len(stored(settings)) == count

    def test_backfill_in_passes_converges(self, env):
        """Small cap: repeated passes must reach the beginning, not loop."""
        settings, _ = env
        gmail.ingest_account("personal", settings, max_messages=2)

        for _ in range(20):
            result = gmail.backfill_account("personal", settings, max_messages=2)
            if not result.truncated or result.inserted == 0:
                break

        assert len(stored(settings)) == TOTAL_MESSAGES

    def test_bounded_backfill_does_not_claim_completeness(self, env):
        """--until proves nothing about mail earlier than the bound."""
        settings, _ = env
        gmail.ingest_account("personal", settings, max_messages=3)

        gmail.backfill_account("personal", settings, until=datetime(2026, 8, 8).date())
        assert state_of(settings).backfill_complete is False

    def test_backfill_never_touches_the_forward_cursor(self, env):
        settings, _ = env
        gmail.ingest_account("personal", settings, max_messages=3)
        before = state_of(settings)
        forward_history, forward_date = before.last_history_id, before.last_internal_date_ms

        gmail.backfill_account("personal", settings)

        after = state_of(settings)
        assert after.last_history_id == forward_history
        assert after.last_internal_date_ms == forward_date

    def test_backfill_before_any_ingest_is_a_noop(self, env):
        settings, _ = env
        result = gmail.backfill_account("personal", settings)
        assert result.fetched == 0
        assert stored(settings) == []


class TestConcurrentFetch:
    """workers>1 must give each thread its own Gmail client.

    googleapiclient's http layer is not thread-safe; sharing one service
    across threads corrupts responses intermittently, which is the worst
    possible failure mode for an ingest pipeline.
    """

    def test_each_thread_builds_its_own_service(self, env, monkeypatch):
        settings, service = env
        import threading

        built = []
        seen_threads = []

        def factory():
            built.append(1)
            seen_threads.append(threading.current_thread().name)
            return FakeService(build_corpus())

        result = gmail.IngestResult(account_label="personal", mode="test")
        rows, newest, oldest = gmail._fetch_and_normalize(
            service, "personal", [f"m{i}" for i in range(10)], result,
            workers=4, service_factory=factory,
        )

        assert len(rows) == 10
        assert result.fetched == 10
        # One client per worker thread that ran, never one shared client.
        assert len(built) == len(set(seen_threads))

    def test_sequential_path_ignores_the_factory(self, env):
        settings, service = env
        built = []
        result = gmail.IngestResult(account_label="personal", mode="test")

        gmail._fetch_and_normalize(
            service, "personal", ["m0", "m1"], result,
            workers=1, service_factory=lambda: built.append(1),
        )
        assert built == []
        assert result.fetched == 2

    def test_concurrent_results_match_sequential(self, env):
        settings, service = env
        ids = [f"m{i}" for i in range(10)]

        seq_result = gmail.IngestResult(account_label="personal", mode="s")
        seq_rows, seq_new, seq_old = gmail._fetch_and_normalize(
            service, "personal", ids, seq_result
        )

        par_result = gmail.IngestResult(account_label="personal", mode="p")
        par_rows, par_new, par_old = gmail._fetch_and_normalize(
            service, "personal", ids, par_result,
            workers=4, service_factory=lambda: FakeService(build_corpus()),
        )

        assert [r["gmail_id"] for r in seq_rows] == [r["gmail_id"] for r in par_rows]
        assert (seq_new, seq_old) == (par_new, par_old)

    def test_one_failure_does_not_abandon_the_batch(self, env):
        settings, service = env
        result = gmail.IngestResult(account_label="personal", mode="test")

        rows, _, _ = gmail._fetch_and_normalize(
            service, "personal", ["m0", "does-not-exist", "m1"], result,
            workers=2, service_factory=lambda: FakeService(build_corpus()),
        )
        assert len(rows) == 2
        assert result.failed == 1
