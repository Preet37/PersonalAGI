"""Watermark logic, exercised against a fake Gmail service.

No network. The point of these tests is the decision tree in
list_new_message_ids: history when we can, date query when we must,
bootstrap when there is nothing stored.
"""

import httplib2
import pytest
from googleapiclient.errors import HttpError

from personalagi.ingest import fetch


class FakeRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeHistory:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return FakeRequest(self._pages.pop(0))


class FakeMessages:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return FakeRequest(self._pages.pop(0))


class FakeUsers:
    def __init__(self, *, profile, history_pages=(), message_pages=()):
        self._profile = profile
        self._history = FakeHistory(history_pages)
        self._messages = FakeMessages(message_pages)

    def getProfile(self, **_):  # noqa: N802 - mirrors the Google client API
        return FakeRequest(self._profile)

    def history(self):
        return self._history

    def messages(self):
        return self._messages


class FakeService:
    def __init__(self, users):
        self._users = users

    def users(self):
        return self._users


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


def test_bootstrap_uses_backfill_window_when_no_watermark():
    users = FakeUsers(
        profile={"historyId": "1000"},
        message_pages=[{"messages": [{"id": "a"}, {"id": "b"}]}],
    )
    result = fetch.list_new_message_ids(
        FakeService(users),
        last_history_id=None,
        last_internal_date_ms=None,
        initial_backfill_days=30,
        overlap_seconds=86_400,
    )

    assert result.mode == "bootstrap"
    assert result.message_ids == ["a", "b"]
    assert result.profile_history_id == "1000"
    assert users.messages().calls[0]["q"].startswith("after:")


def test_history_mode_when_watermark_is_fresh():
    users = FakeUsers(
        profile={"historyId": "2000"},
        history_pages=[
            {
                "history": [
                    {"messagesAdded": [{"message": {"id": "m1"}}]},
                    {"messagesAdded": [{"message": {"id": "m2"}}]},
                ]
            }
        ],
    )
    result = fetch.list_new_message_ids(
        FakeService(users),
        last_history_id="1500",
        last_internal_date_ms=1_700_000_000_000,
        initial_backfill_days=30,
        overlap_seconds=86_400,
    )

    assert result.mode == "history"
    assert result.message_ids == ["m1", "m2"]
    assert result.history_expired is False
    assert users.history().calls[0]["startHistoryId"] == "1500"


def test_history_deduplicates_repeated_ids():
    users = FakeUsers(
        profile={"historyId": "2000"},
        history_pages=[
            {
                "history": [
                    {"messagesAdded": [{"message": {"id": "m1"}}]},
                    {"messagesAdded": [{"message": {"id": "m1"}}]},
                ]
            }
        ],
    )
    result = fetch.list_new_message_ids(
        FakeService(users),
        last_history_id="1500",
        last_internal_date_ms=None,
        initial_backfill_days=30,
        overlap_seconds=86_400,
    )
    assert result.message_ids == ["m1"]


def test_expired_history_falls_back_to_date_query():
    users = FakeUsers(
        profile={"historyId": "9000"},
        history_pages=[http_error(404)],
        message_pages=[{"messages": [{"id": "z"}]}],
    )
    last_ms = 1_700_000_000_000
    result = fetch.list_new_message_ids(
        FakeService(users),
        last_history_id="1",
        last_internal_date_ms=last_ms,
        initial_backfill_days=30,
        overlap_seconds=86_400,
    )

    assert result.history_expired is True
    assert result.mode == "query"
    assert result.message_ids == ["z"]

    # The critical property: the query boundary is the watermark MINUS the
    # overlap window, never the watermark itself.
    queried_after = int(users.messages().calls[0]["q"].removeprefix("after:"))
    assert queried_after == (last_ms // 1000) - 86_400


def test_profile_history_id_is_captured_before_listing():
    """It must come from getProfile, not from the newest message seen."""
    users = FakeUsers(
        profile={"historyId": "5555"},
        history_pages=[{"history": []}],
    )
    result = fetch.list_new_message_ids(
        FakeService(users),
        last_history_id="1",
        last_internal_date_ms=None,
        initial_backfill_days=30,
        overlap_seconds=86_400,
    )
    assert result.profile_history_id == "5555"
    assert result.message_ids == []


def test_query_pagination_follows_next_page_token():
    users = FakeUsers(
        profile={"historyId": "1"},
        message_pages=[
            {"messages": [{"id": "a"}], "nextPageToken": "tok"},
            {"messages": [{"id": "b"}]},
        ],
    )
    result = fetch.list_new_message_ids(
        FakeService(users),
        last_history_id=None,
        last_internal_date_ms=None,
        initial_backfill_days=30,
        overlap_seconds=86_400,
    )
    assert result.message_ids == ["a", "b"]
    assert users.messages().calls[1]["pageToken"] == "tok"


def test_max_messages_caps_the_pull():
    users = FakeUsers(
        profile={"historyId": "1"},
        message_pages=[{"messages": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}],
    )
    result = fetch.list_new_message_ids(
        FakeService(users),
        last_history_id=None,
        last_internal_date_ms=None,
        initial_backfill_days=30,
        overlap_seconds=86_400,
        max_messages=2,
    )
    assert result.message_ids == ["a", "b"]


class TestRetry:
    def test_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(fetch.time, "sleep", lambda _: None)
        attempts = {"n": 0}

        class Flaky:
            def execute(self):
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise http_error(429)
                return {"ok": True}

        assert fetch.execute(Flaky()) == {"ok": True}
        assert attempts["n"] == 3

    def test_permission_denied_is_not_retried(self, monkeypatch):
        monkeypatch.setattr(fetch.time, "sleep", lambda _: None)
        attempts = {"n": 0}

        class Forbidden:
            def execute(self):
                attempts["n"] += 1
                raise http_error(403)

        with pytest.raises(HttpError):
            fetch.execute(Forbidden())
        # 403 without a rate-limit reason means "no access" — retrying it
        # just burns quota against a wall.
        assert attempts["n"] == 1

    def test_404_propagates_for_the_caller_to_interpret(self, monkeypatch):
        monkeypatch.setattr(fetch.time, "sleep", lambda _: None)

        class Gone:
            def execute(self):
                raise http_error(404)

        with pytest.raises(HttpError):
            fetch.execute(Gone())
