"""Classify ingested messages into needs_response / fyi / promotional / spam.

Design constraints, in priority order:
  1. A batch must never crash. One bad message becomes one tombstone row.
  2. Every result is attributable: model + prompt_version stored per row.
  3. Re-running is idempotent — already-classified messages are skipped
     unless explicitly re-classified.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.llm.client import GroqClient, LLMError
from personalagi.llm.prompts import load_prompt
from personalagi.llm.schemas import UNCLASSIFIED, ClassificationOut
from personalagi.models import Classification, Message

log = logging.getLogger(__name__)

# Body text beyond this adds cost without changing the label. Measured
# against the corpus: the signal for all four classes is in the opening.
BODY_CHAR_LIMIT = 1500


@dataclass
class ClassifyResult:
    account_label: str
    considered: int = 0
    classified: int = 0
    unclassified: int = 0
    skipped_existing: int = 0
    usage_summary: str = ""
    by_category: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        counts = "  ".join(f"{k}={v}" for k, v in sorted(self.by_category.items()))
        return (
            f"{self.account_label}: considered={self.considered} "
            f"classified={self.classified} unclassified={self.unclassified} "
            f"skipped={self.skipped_existing}\n  {counts}\n  {self.usage_summary}"
        )


def render_message(message: Message) -> dict[str, str]:
    """The fields the prompt template consumes."""
    body = (message.body_text or "").strip()
    if len(body) > BODY_CHAR_LIMIT:
        body = body[:BODY_CHAR_LIMIT] + "\n[...truncated]"
    return {
        "sender_name": message.sender_name or "(unknown)",
        "sender_email": message.sender_email or "(unknown)",
        "date": message.timestamp.isoformat(sep=" ", timespec="minutes"),
        "subject": message.subject or "(no subject)",
        "body": body or "(empty body)",
    }


def classify_one(
    client: GroqClient,
    prompt,
    message: Message,
    *,
    max_attempts: int = 2,
) -> tuple[ClassificationOut | None, str | None]:
    """Classify a single message. Returns (result, error).

    One retry on malformed output, per the spec. The retry is not a plain
    repeat: it appends an explicit corrective instruction, because repeating
    an identical failing prompt at temperature 0 mostly reproduces the same
    failure.
    """
    user = prompt.render_user(**render_message(message))
    last_error: str | None = None

    for attempt in range(max_attempts):
        content = None
        try:
            attempt_user = user
            if attempt > 0:
                attempt_user = (
                    f"{user}\n\n"
                    "Your previous reply was not valid. Return ONLY a JSON object "
                    'with keys "category", "urgency", "summary". No prose, no '
                    "markdown fences."
                )
            content = client.complete_json(prompt.system, attempt_user)
            return ClassificationOut.model_validate_json(content), None
        except (ValidationError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.debug("message %s attempt %d invalid: %s", message.gmail_id, attempt + 1, exc)
        except LLMError as exc:
            last_error = f"LLMError: {str(exc)[:200]}"
            log.debug("message %s attempt %d failed: %s", message.gmail_id, attempt + 1, exc)

    return None, last_error


def _pending_messages(
    session: Session,
    label: str | None,
    limit: int | None,
    reclassify: bool,
) -> list[Message]:
    stmt = select(Message)
    if label:
        stmt = stmt.where(Message.account_label == label)
    if not reclassify:
        classified = select(Classification.message_id)
        stmt = stmt.where(Message.id.not_in(classified))
    stmt = stmt.order_by(Message.internal_date_ms.desc())
    if limit:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars())


def _upsert(session: Session, rows: list[dict]) -> None:
    """Idempotent write: re-classifying a message updates its row."""
    if not rows:
        return
    for start in range(0, len(rows), 90):
        chunk = rows[start : start + 90]
        stmt = sqlite_insert(Classification).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["message_id"],
            set_={
                "category": stmt.excluded.category,
                "urgency": stmt.excluded.urgency,
                "summary": stmt.excluded.summary,
                "model": stmt.excluded.model,
                "prompt_version": stmt.excluded.prompt_version,
                "ok": stmt.excluded.ok,
                "error": stmt.excluded.error,
                "classified_at": stmt.excluded.classified_at,
            },
        )
        session.execute(stmt)
    session.commit()


def classify_labelled(
    message_ids: list[int],
    settings: Settings | None = None,
    *,
    workers: int | None = None,
) -> int:
    """Classify exactly these messages if they have no prediction yet.

    Used by `eval --classify-missing` so scoring never silently drops rows.
    """
    settings = settings or get_settings()
    init_db(settings)

    with Session(get_engine(settings)) as session:
        already = {
            row
            for row in session.execute(
                select(Classification.message_id).where(
                    Classification.message_id.in_(message_ids)
                )
            ).scalars()
        }
        todo = [mid for mid in message_ids if mid not in already]
        if not todo:
            return 0
        messages = list(
            session.execute(select(Message).where(Message.id.in_(todo))).scalars()
        )

    if not messages:
        return 0

    prompt = load_prompt("classify")
    client = GroqClient(settings)
    now = datetime.now(UTC).replace(tzinfo=None)
    rows: list[dict] = []

    def work(message: Message) -> dict:
        parsed, error = classify_one(client, prompt, message)
        base = {
            "message_id": message.id,
            "model": client.model,
            "prompt_version": prompt.version,
            "classified_at": now,
        }
        if parsed is None:
            return {**base, "category": UNCLASSIFIED, "urgency": "low", "summary": "",
                    "ok": False, "error": error}
        return {**base, "category": parsed.category, "urgency": parsed.urgency,
                "summary": parsed.summary, "ok": True, "error": None}

    with ThreadPoolExecutor(max_workers=workers or settings.classify_workers) as pool:
        rows = list(pool.map(work, messages))

    with Session(get_engine(settings)) as session:
        _upsert(session, rows)
    log.info("classified %d previously-unscored message(s)", len(rows))
    return len(rows)


def classify_account(
    label: str | None = None,
    settings: Settings | None = None,
    *,
    limit: int | None = None,
    reclassify: bool = False,
    workers: int | None = None,
    dry_run: bool = False,
) -> ClassifyResult:
    """Classify unclassified messages for one account (or all if label=None)."""
    settings = settings or get_settings()
    init_db(settings)

    prompt = load_prompt("classify")
    client = GroqClient(settings)
    worker_count = workers or settings.classify_workers

    result = ClassifyResult(account_label=label or "(all)")

    with Session(get_engine(settings)) as session:
        messages = _pending_messages(session, label, limit, reclassify)
        result.considered = len(messages)

        if not reclassify:
            total = session.execute(
                select(func.count()).select_from(Message).where(
                    Message.account_label == label if label else True
                )
            ).scalar_one()
            result.skipped_existing = max(0, total - len(messages))

    if not messages:
        result.usage_summary = "no messages to classify"
        return result

    log.info(
        "classifying %d message(s) with %s (prompt %s, %d workers)",
        len(messages),
        client.model,
        prompt.version,
        worker_count,
    )

    if dry_run:
        result.usage_summary = f"dry run - would classify {len(messages)}"
        return result

    now = datetime.now(UTC).replace(tzinfo=None)
    rows: list[dict] = []

    def work(message: Message) -> dict:
        parsed, error = classify_one(client, prompt, message)
        if parsed is None:
            # Tombstone, not a dropped row: a failure must be visible in the
            # data or it silently shrinks the denominator of every metric.
            return {
                "message_id": message.id,
                "category": UNCLASSIFIED,
                "urgency": "low",
                "summary": "",
                "model": client.model,
                "prompt_version": prompt.version,
                "ok": False,
                "error": error,
                "classified_at": now,
            }
        return {
            "message_id": message.id,
            "category": parsed.category,
            "urgency": parsed.urgency,
            "summary": parsed.summary,
            "model": client.model,
            "prompt_version": prompt.version,
            "ok": True,
            "error": None,
            "classified_at": now,
        }

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        for row in pool.map(work, messages):
            rows.append(row)
            category = row["category"]
            result.by_category[category] = result.by_category.get(category, 0) + 1
            if row["ok"]:
                result.classified += 1
            else:
                result.unclassified += 1

    with Session(get_engine(settings)) as session:
        _upsert(session, rows)

    result.usage_summary = client.usage.summary()
    log.info(result.summary())
    return result
