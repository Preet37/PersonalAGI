"""Google Calendar -> Event. Built, tested against fakes, NOT authorized.

No OAuth flow runs from this module. `sync_calendar` refuses to do anything
without an existing token file and prints how to create one. That is
deliberate: an auth flow opens a browser and asks a human to grant a scope, and
neither of those should happen unattended.

Everything except the network call is exercised by tests, so authorizing it is
a one-command step rather than a debugging session.

A calendar entry maps onto the Event shape more naturally than mail does:

    summary      -> title
    description  -> text
    start        -> timestamp
    organizer    -> participant, role "from"
    attendees    -> participants, role "attendee"

The parts with no equivalent — recurrence, conferencing links, RSVP status,
the end time — go in metadata, which is exactly what D1 says it is for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlmodel import Session

from personalagi.adapters.base import ATTENDEE, FROM, EventRecord, resolve_participant
from personalagi.adapters.gmail_adapter import _upsert_events, _upsert_participants
from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.models import Event

log = logging.getLogger(__name__)

SOURCE = "calendar"
ACCOUNT_LABEL = "calendar"

# Read-only, matching the Gmail scope's posture. Nothing in this system needs
# to write to a calendar, and Stage 11's create_calendar_event is a stub for
# exactly that reason.
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
TOKEN_LABEL = "calendar"

# Entries longer than this are almost always all-day placeholders, out-of-office
# blocks, or holiday feeds rather than things the owner is doing.
LONG_EVENT_DAYS = 3


class CalendarNotAuthorized(RuntimeError):
    """No calendar token exists. Authorizing requires a browser and a human."""


@dataclass
class CalendarResult:
    considered: int = 0
    events: int = 0
    participants: int = 0
    skipped_cancelled: int = 0
    skipped_no_start: int = 0

    def summary(self) -> str:
        return (
            f"calendar: considered={self.considered} events={self.events} "
            f"participants={self.participants} "
            f"cancelled={self.skipped_cancelled} no_start={self.skipped_no_start}"
        )


def parse_timestamp(node: dict) -> datetime | None:
    """Read a Calendar start/end node.

    Timed events carry `dateTime` (RFC 3339, usually with an offset). All-day
    events carry `date` only, and are stored at midnight — a date is not a
    time, and pretending otherwise by picking 09:00 would invent information.
    """
    if not isinstance(node, dict):
        return None

    raw = node.get("dateTime")
    if raw:
        text = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        # Stored naive-UTC to match every other source; mixing aware and naive
        # values in comparisons is a reliable source of bugs later.
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed

    raw = node.get("date")
    if raw:
        try:
            return datetime.combine(date.fromisoformat(raw), datetime.min.time())
        except ValueError:
            return None
    return None


def event_text(item: dict) -> str:
    """Description plus the details a person would want in a log line."""
    parts = []
    if item.get("description"):
        parts.append(item["description"].strip())
    if item.get("location"):
        parts.append(f"Location: {item['location'].strip()}")
    if item.get("hangoutLink"):
        parts.append(f"Call: {item['hangoutLink']}")
    return "\n\n".join(parts)


def event_from_item(
    item: dict,
    *,
    owner_addresses: set[str] | None = None,
    calendar_id: str = "primary",
) -> EventRecord | None:
    """One Calendar API item -> one Event, or None when it should be skipped."""
    if (item.get("status") or "").lower() == "cancelled":
        return None

    start = parse_timestamp(item.get("start") or {})
    if start is None:
        # An item with no resolvable start cannot be placed on a timeline, and
        # guessing a time would put a fabricated commitment in the brief.
        return None

    end = parse_timestamp(item.get("end") or {})
    all_day = bool((item.get("start") or {}).get("date"))

    record = EventRecord(
        source=SOURCE,
        source_id=item.get("id") or "",
        account_label=ACCOUNT_LABEL,
        # Recurring instances share the series id, so a weekly standup is one
        # thread rather than fifty unrelated events.
        thread_key=item.get("recurringEventId") or item.get("id") or "",
        title=(item.get("summary") or "").strip(),
        text=event_text(item),
        timestamp=start,
        timestamp_ms=int(start.replace(tzinfo=UTC).timestamp() * 1000),
        metadata={
            "calendar_id": calendar_id,
            "end": end.isoformat() if end else "",
            "all_day": all_day,
            "status": item.get("status") or "",
            "location": item.get("location") or "",
            "hangout_link": item.get("hangoutLink") or "",
            "recurring_event_id": item.get("recurringEventId") or "",
            "html_link": item.get("htmlLink") or "",
            "self_response": next(
                (
                    a.get("responseStatus", "")
                    for a in item.get("attendees") or []
                    if a.get("self")
                ),
                "",
            ),
        },
    )

    organizer = item.get("organizer") or {}
    if organizer.get("email"):
        record.participants.append(
            resolve_participant(
                organizer["email"],
                organizer.get("displayName") or "",
                role=FROM,
                owner_addresses=owner_addresses,
            )
        )

    seen = {(organizer.get("email") or "").strip().lower()}
    for attendee in item.get("attendees") or []:
        address = (attendee.get("email") or "").strip().lower()
        if not address or address in seen:
            continue
        seen.add(address)
        # Rooms and equipment are attendees in the API and are not people.
        if attendee.get("resource"):
            continue
        record.participants.append(
            resolve_participant(
                address,
                attendee.get("displayName") or "",
                role=ATTENDEE,
                owner_addresses=owner_addresses,
            )
        )

    return record


def fetch_items(
    service,
    *,
    calendar_id: str = "primary",
    time_min: datetime,
    time_max: datetime,
    max_results: int = 2500,
) -> list[dict]:
    """Page the Calendar API. Requires an authorized service object.

    `singleEvents=True` expands recurring series into instances, which is what
    a timeline needs — an unexpanded weekly standup is one event in 2019 with a
    recurrence rule, and would never appear in a brief.
    """
    items: list[dict] = []
    page_token = None
    while True:
        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min.isoformat() + "Z",
                timeMax=time_max.isoformat() + "Z",
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page_token,
            )
            .execute()
        )
        items.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token or len(items) >= max_results:
            break
    return items[:max_results]


def sync_calendar(
    settings: Settings | None = None,
    *,
    service=None,
    calendar_id: str = "primary",
    days_back: int = 30,
    days_forward: int = 60,
    items: list[dict] | None = None,
) -> CalendarResult:
    """Project calendar entries into Events.

    Pass `items` to run entirely offline (tests, dry runs). Pass `service` to
    use an already-authorized client. With neither, this raises rather than
    starting an OAuth flow — that needs a browser and a human decision.
    """
    settings = settings or get_settings()
    init_db(settings)
    result = CalendarResult()

    if items is None:
        if service is None:
            token = settings.token_path(TOKEN_LABEL)
            if not token.exists():
                raise CalendarNotAuthorized(
                    f"no calendar token at {token}.\n"
                    "This adapter is built and tested but deliberately not "
                    "authorized: granting a scope needs a browser and your "
                    "decision, so it is not something to do unattended.\n"
                    f"When you want it:  python -m personalagi auth {TOKEN_LABEL}\n"
                    f"Scope requested:   {SCOPE} (read-only)"
                )
            raise CalendarNotAuthorized(
                "a calendar token exists but no service was supplied; build one "
                "and pass service= rather than having this module do auth."
            )
        now = datetime.now(UTC).replace(tzinfo=None)
        items = fetch_items(
            service,
            calendar_id=calendar_id,
            time_min=now - timedelta(days=days_back),
            time_max=now + timedelta(days=days_forward),
        )

    result.considered = len(items)
    owner_addresses = settings.owner_address_set

    records: list[EventRecord] = []
    for item in items:
        if (item.get("status") or "").lower() == "cancelled":
            result.skipped_cancelled += 1
            continue
        record = event_from_item(
            item, owner_addresses=owner_addresses, calendar_id=calendar_id
        )
        if record is None:
            result.skipped_no_start += 1
            continue
        records.append(record)

    if not records:
        return result

    now = datetime.now(UTC).replace(tzinfo=None)
    for start in range(0, len(records), 200):
        batch = records[start : start + 200]
        with Session(get_engine(settings)) as session:
            result.events += _upsert_events(session, [r.as_row(now) for r in batch])
            session.commit()
            ids = dict(
                session.execute(
                    select(Event.source_id, Event.id)
                    .where(Event.source == SOURCE)
                    .where(Event.source_id.in_([r.source_id for r in batch]))
                ).all()
            )
            result.participants += _upsert_participants(
                session,
                [
                    p.as_row(ids[r.source_id])
                    for r in batch
                    if r.source_id in ids
                    for p in r.participants
                ],
            )
            session.commit()

    log.info(result.summary())
    return result
