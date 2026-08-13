"""Eval harness: hand-labels in, confusion matrix out.

The template is generated from real ingested mail, stratified across senders
so it is not 30 LinkedIn digests. Labelling it is the one step that cannot be
automated, and it is the only thing that turns "the classifier seems fine"
into a number.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.evals.metrics import EvalReport, evaluate
from personalagi.identity import is_human_sender, shared_addresses
from personalagi.llm.schemas import CATEGORIES, UNCLASSIFIED, URGENCIES
from personalagi.models import Classification, Message

log = logging.getLogger(__name__)

TEMPLATE_COLUMNS = [
    "message_id",
    "gmail_id",
    "date",
    "sender_email",
    "subject",
    "body_excerpt",
    "true_category",
    "true_urgency",
]

EXCERPT_CHARS = 300


class EvalError(RuntimeError):
    pass


@dataclass
class LabelRow:
    message_id: int
    true_category: str
    true_urgency: str


def _excerpt(text: str) -> str:
    flat = " ".join((text or "").split())
    return flat[:EXCERPT_CHARS]


def _round_robin(buckets: list[list[Message]], n: int) -> list[Message]:
    """Take one from each bucket in turn until n are picked or all are dry."""
    picked: list[Message] = []
    depth = 0
    while len(picked) < n and any(depth < len(b) for b in buckets):
        for bucket in buckets:
            if len(picked) >= n:
                break
            if depth < len(bucket):
                picked.append(bucket[depth])
        depth += 1
    return picked[:n]


def generate_template(
    out_path: Path,
    settings: Settings | None = None,
    *,
    n: int = 30,
    per_sender_cap: int = 2,
    account: str | None = None,
    human_only: bool = False,
    stratify: bool = False,
) -> int:
    """Write an unlabelled CSV sampled across distinct senders.

    Stratifying by sender matters: an unstratified sample of this inbox is
    mostly LinkedIn job alerts, which would measure one easy class 30 times
    and tell you nothing about needs_response.

    `human_only` drops robot addresses and shared bulk envelopes before
    sampling. v1 of this file was drawn from the whole inbox, which is ~95%
    automated, and produced exactly ONE needs_response row in 30 — so the
    precision and recall reported for the class that matters were computed on
    a single example and meant nothing.

    `stratify` additionally round-robins across the *predicted* category so
    the classes come out roughly balanced.

    Both flags trade away base-rate fidelity for statistical power, and that
    trade is not free: metrics measured on a class-balanced sample do NOT
    transfer to the live inbox without reweighting by the true prior. Read
    them as "how well does it separate these classes when it sees them", not
    as "what will my brief look like". Stratifying on the *prediction* also
    biases recall specifically, because a class the model never predicts
    cannot be sampled into its own bucket.
    """
    settings = settings or get_settings()
    init_db(settings)

    with Session(get_engine(settings)) as session:
        stmt = select(Message, Classification).join(
            Classification, Classification.message_id == Message.id, isouter=True
        )
        if account:
            stmt = stmt.where(Message.account_label == account)
        stmt = stmt.order_by(Message.internal_date_ms.desc())
        pairs = list(session.execute(stmt))

    if not pairs:
        raise EvalError("no ingested messages to sample - run `ingest` first")

    predicted = {
        message.id: (classification.category if classification else UNCLASSIFIED)
        for message, classification in pairs
    }
    messages = [message for message, _ in pairs]

    if human_only:
        shared = shared_addresses(
            (m.sender_email or "", m.sender_name or "") for m in messages
        )
        messages = [m for m in messages if is_human_sender(m.sender_email, shared)]
        if not messages:
            raise EvalError(
                "no human senders found - every ingested address looks automated"
            )

    # Round-robin over senders: take the 1st message of every sender, then
    # the 2nd of every sender, and so on, up to the per-sender cap.
    by_sender: dict[str, list[Message]] = {}
    for message in messages:
        by_sender.setdefault(message.sender_email or "(unknown)", []).append(message)

    capped: list[Message] = []
    for depth in range(per_sender_cap):
        for sender_messages in by_sender.values():
            if depth < len(sender_messages):
                capped.append(sender_messages[depth])

    if stratify:
        by_category: dict[str, list[Message]] = {}
        for message in capped:
            by_category.setdefault(predicted.get(message.id, UNCLASSIFIED), []).append(
                message
            )
        # Rarest class first, so a class with few candidates is not starved by
        # the time the quota runs out.
        buckets = sorted(by_category.values(), key=len)
        picked = _round_robin(buckets, n)
        log.info(
            "stratified across %d predicted class(es): %s",
            len(by_category),
            ", ".join(f"{k}={len(v)}" for k, v in sorted(by_category.items())),
        )
    else:
        picked = capped[:n]

    picked = picked[:n]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TEMPLATE_COLUMNS)
        writer.writeheader()
        for message in picked:
            writer.writerow(
                {
                    "message_id": message.id,
                    "gmail_id": message.gmail_id,
                    "date": message.timestamp.isoformat(sep=" ", timespec="minutes"),
                    "sender_email": message.sender_email,
                    "subject": _excerpt(message.subject),
                    "body_excerpt": _excerpt(message.body_text),
                    "true_category": "",
                    "true_urgency": "",
                }
            )

    log.info("wrote %d rows from %d distinct senders", len(picked), len(by_sender))
    return len(picked)


def read_labels(path: Path) -> list[LabelRow]:
    """Read hand-labels, rejecting typos loudly rather than scoring them."""
    if not path.exists():
        raise EvalError(f"no label file at {path}")

    rows: list[LabelRow] = []
    problems: list[str] = []

    with path.open(newline="", encoding="utf-8") as handle:
        for line_no, raw in enumerate(csv.DictReader(handle), start=2):
            category = (raw.get("true_category") or "").strip().lower()
            urgency = (raw.get("true_urgency") or "").strip().lower() or "low"

            if not category:
                continue  # unlabelled row: skipped, not an error

            if category not in CATEGORIES:
                problems.append(
                    f"line {line_no}: true_category '{category}' not one of "
                    f"{', '.join(CATEGORIES)}"
                )
                continue
            if urgency not in URGENCIES:
                problems.append(
                    f"line {line_no}: true_urgency '{urgency}' not one of "
                    f"{', '.join(URGENCIES)}"
                )
                continue
            try:
                message_id = int(raw["message_id"])
            except (KeyError, TypeError, ValueError):
                problems.append(f"line {line_no}: missing or non-integer message_id")
                continue

            rows.append(LabelRow(message_id, category, urgency))

    if problems:
        raise EvalError("label file has errors:\n  " + "\n  ".join(problems))
    if not rows:
        raise EvalError(f"{path} has no labelled rows - fill in true_category")
    return rows


def evaluate_labels(
    labels: list[LabelRow],
    settings: Settings | None = None,
) -> tuple[EvalReport, EvalReport, dict]:
    """Score stored predictions against hand-labels.

    Returns (category_report, urgency_report, meta). Messages with no stored
    classification are reported rather than dropped — a shrinking denominator
    is the easiest way to accidentally flatter a metric.
    """
    settings = settings or get_settings()
    init_db(settings)

    wanted = [row.message_id for row in labels]
    with Session(get_engine(settings)) as session:
        stored = {
            c.message_id: c
            for c in session.execute(
                select(Classification).where(Classification.message_id.in_(wanted))
            ).scalars()
        }

    category_pairs: list[tuple[str, str]] = []
    urgency_pairs: list[tuple[str, str]] = []
    missing: list[int] = []
    tombstones: list[int] = []

    for row in labels:
        prediction = stored.get(row.message_id)
        if prediction is None:
            missing.append(row.message_id)
            continue
        if not prediction.ok:
            tombstones.append(row.message_id)
        category_pairs.append((row.true_category, prediction.category))
        urgency_pairs.append((row.true_urgency, prediction.urgency))

    category_labels = [*CATEGORIES]
    if any(p == UNCLASSIFIED for _, p in category_pairs):
        category_labels.append(UNCLASSIFIED)

    meta = {
        "labelled": len(labels),
        "scored": len(category_pairs),
        "missing_prediction": missing,
        "unclassified": tombstones,
        "model": next((c.model for c in stored.values()), ""),
        "prompt_version": next((c.prompt_version for c in stored.values()), ""),
    }
    return (
        evaluate(category_pairs, category_labels),
        evaluate(urgency_pairs, [*URGENCIES]),
        meta,
    )
