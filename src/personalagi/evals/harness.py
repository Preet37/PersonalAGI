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
from personalagi.events import load_views, pending_events
from personalagi.llm.schemas import CATEGORIES, UNCLASSIFIED, URGENCIES
from personalagi.models import Classification

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


def _round_robin(buckets: list[list], n: int) -> list:
    """Take one from each bucket in turn until n are picked or all are dry."""
    picked: list = []
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
        views = load_views(session, pending_events(account=account))
        predicted_rows = {
            c.event_id: c.category
            for c in session.execute(select(Classification)).scalars()
        }

    if not views:
        raise EvalError("no events to sample - run `ingest` then `sync-events` first")

    predicted = {
        v.event.id: predicted_rows.get(v.event.id, UNCLASSIFIED) for v in views
    }
    messages = views

    if human_only:
        # Reads the adapter's participant verdict rather than recomputing it,
        # so the eval samples exactly the population stage B actually scores.
        messages = [v for v in views if not v.from_automated and v.sender_address]
        if not messages:
            raise EvalError(
                "no human senders found - every ingested address looks automated"
            )

    # Round-robin over senders: take the 1st message of every sender, then
    # the 2nd of every sender, and so on, up to the per-sender cap.
    by_sender: dict[str, list] = {}
    for view in messages:
        by_sender.setdefault(view.sender_address or "(unknown)", []).append(view)

    capped: list = []
    for depth in range(per_sender_cap):
        for sender_messages in by_sender.values():
            if depth < len(sender_messages):
                capped.append(sender_messages[depth])

    if stratify:
        by_category: dict[str, list] = {}
        for view in capped:
            by_category.setdefault(
                predicted.get(view.event.id, UNCLASSIFIED), []
            ).append(view)
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
        for view in picked:
            writer.writerow(
                {
                    # Column name kept as message_id: the owner already has a
                    # labels.csv with this header, and Event ids equal the
                    # Message ids they were migrated from, so old files still
                    # score correctly against the new schema.
                    "message_id": view.event.id,
                    "gmail_id": view.event.source_id,
                    "date": view.event.timestamp.isoformat(sep=" ", timespec="minutes"),
                    "sender_email": view.sender_address,
                    "subject": _excerpt(view.event.title),
                    "body_excerpt": _excerpt(view.event.text),
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
            c.event_id: c
            for c in session.execute(
                select(Classification).where(Classification.event_id.in_(wanted))
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
