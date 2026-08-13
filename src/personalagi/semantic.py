"""Semantic judgement: the model decides, keyword search only nominates.

The system was full of lexical rules standing in for understanding — stopword
lists, regex "ask" markers, IDF keyword scoring. Every one of them is a
hardcoded guess about meaning, and they failed exactly where you would expect:
`letter` matched inside `newsletter`, and a credit-card application "supported"
a Carnegie Mellon application because both contain the word `application`.

The fix is not a better regex. It is the same two-stage shape that already
works elsewhere in this system:

    cheap lexical pass   ->  RECALL.    Find candidates. Wrong is fine.
    model judgement      ->  PRECISION. Decide. This is where meaning lives.

Keyword search keeps its job — it is free and it only has to avoid missing
things. What it no longer does is DECIDE, and a threshold constant is no
longer standing in for a verdict.

WHY THE VERDICTS ARE CACHED
A (task, event) pair has a stable answer: neither the message nor the step text
changes. Re-paying for that judgement on every sweep would make the nightly run
cost real money for an answer already known. Cached on a content hash, so
editing the step text correctly invalidates it.

WHY IT IS BATCHED
One call judges many candidates. Per-candidate calls would multiply the cost of
the one stage that is supposed to be expensive-but-rare by however many words
a keyword search happened to match.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.db import get_engine, init_db
from personalagi.llm.client import GroqClient, LLMError
from personalagi.llm.prompts import load_prompt
from personalagi.models import Judgement
from personalagi.records import CallBudget

log = logging.getLogger(__name__)

# Candidate text sent per item. Enough to judge, small enough that twenty
# candidates still fit comfortably in one call.
CANDIDATE_CHARS = 700
MAX_PER_CALL = 8


class VerdictOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    supports: bool = False
    why: str = Field(default="", max_length=200)

    @field_validator("supports", mode="before")
    @classmethod
    def _coerce(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "y", "1"}
        return value


class VerdictsOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdicts: list[VerdictOut] = Field(default_factory=list)


@dataclass
class Verdict:
    event_id: int
    supports: bool
    why: str = ""
    cached: bool = False


@dataclass
class JudgeResult:
    verdicts: list[Verdict] = field(default_factory=list)
    calls: int = 0
    cache_hits: int = 0
    failed: int = 0

    @property
    def supported(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.supports]

    def summary(self) -> str:
        return (
            f"judged {len(self.verdicts)}: {len(self.supported)} support, "
            f"{self.cache_hits} cached, {self.calls} call(s), {self.failed} failed"
        )


def question_hash(task: str, event_id: int, kind: str) -> str:
    """Stable key for a judgement. Changing the task text invalidates it."""
    normalized = " ".join((task or "").lower().split())
    raw = f"{kind}|{normalized}|{event_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _cached(session: Session, hashes: list[str]) -> dict[str, Judgement]:
    if not hashes:
        return {}
    found: dict[str, Judgement] = {}
    for start in range(0, len(hashes), 400):
        chunk = hashes[start : start + 400]
        for row in session.execute(
            select(Judgement).where(Judgement.question_hash.in_(chunk))
        ).scalars():
            found[row.question_hash] = row
    return found


def _store(session: Session, rows: list[dict]) -> None:
    if not rows:
        return
    for start in range(0, len(rows), 60):
        chunk = rows[start : start + 60]
        stmt = sqlite_insert(Judgement).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["question_hash"],
            set_={
                k: getattr(stmt.excluded, k)
                for k in ("verdict", "why", "model", "prompt_version", "decided_at")
            },
        )
        session.execute(stmt)
    session.commit()


def judge_evidence(
    task: str,
    candidates: list,
    settings: Settings | None = None,
    *,
    context: str = "",
    budget: CallBudget | None = None,
    kind: str = "evidence",
) -> JudgeResult:
    """Does each candidate event actually support this task?

    `candidates` are Event rows. Returns a verdict for every one — an
    unjudgeable candidate comes back `supports=False`, never silently dropped,
    because a missing verdict and a negative verdict must not look the same to
    the caller.
    """
    settings = settings or get_settings()
    init_db(settings)
    result = JudgeResult()
    if not candidates:
        return result

    hashes = {c.id: question_hash(task, c.id, kind) for c in candidates}
    with Session(get_engine(settings)) as session:
        cached = _cached(session, list(hashes.values()))

    pending = []
    for candidate in candidates:
        row = cached.get(hashes[candidate.id])
        if row is not None:
            result.verdicts.append(
                Verdict(candidate.id, bool(row.verdict), row.why, cached=True)
            )
            result.cache_hits += 1
        else:
            pending.append(candidate)

    if not pending:
        return result

    prompt = load_prompt("judge_evidence")
    client = GroqClient(settings, model=settings.groq_model_large or None)
    now = datetime.now(UTC).replace(tzinfo=None)
    to_store: list[dict] = []

    for start in range(0, len(pending), MAX_PER_CALL):
        batch = pending[start : start + MAX_PER_CALL]
        if budget is not None and not budget.can_spend():
            # Out of budget. Everything unjudged is reported as unsupported
            # with the reason stated, so a truncated run cannot masquerade as
            # a set of negative findings.
            for candidate in batch:
                result.verdicts.append(
                    Verdict(candidate.id, False, "not judged: call budget exhausted")
                )
            continue

        rendered = "\n\n".join(
            f"[{c.id}] {(c.title or '').strip()}\n"
            f"{' '.join((c.text or '').split())[:CANDIDATE_CHARS]}"
            for c in batch
        )
        user = prompt.render_user(
            task=task,
            context=f"CONTEXT: {context}" if context else "",
            candidates=rendered,
        )

        try:
            if budget is not None:
                budget.spend()
            result.calls += 1
            content = client.complete_json(prompt.system, user, max_tokens=1600)
            parsed = VerdictsOut.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError, LLMError) as exc:
            log.warning("evidence judgement failed: %s", str(exc)[:160])
            result.failed += len(batch)
            for candidate in batch:
                # Fail CLOSED. An unjudged candidate must not become evidence,
                # because a false "supported" silences the alert entirely.
                result.verdicts.append(
                    Verdict(candidate.id, False, "not judged: model error")
                )
            continue

        by_id = {v.id: v for v in parsed.verdicts}
        for candidate in batch:
            verdict = by_id.get(candidate.id)
            if verdict is None:
                result.verdicts.append(
                    Verdict(candidate.id, False, "not judged: no verdict returned")
                )
                continue
            result.verdicts.append(
                Verdict(candidate.id, verdict.supports, verdict.why)
            )
            to_store.append(
                {
                    "question_hash": hashes[candidate.id],
                    "kind": kind,
                    "subject": task[:300],
                    "event_id": candidate.id,
                    "verdict": verdict.supports,
                    "why": verdict.why,
                    "model": client.model,
                    "prompt_version": prompt.version,
                    "decided_at": now,
                }
            )

    if to_store:
        with Session(get_engine(settings)) as session:
            _store(session, to_store)

    log.info(result.summary())
    return result
