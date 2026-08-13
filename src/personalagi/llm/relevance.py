"""Two-stage relevance: cheap structural triage, then context-aware judgement.

The defect this replaces: `classify.py` reads one message in isolation and
never opens the context store. Verified by grep before this module existed —
zero references. So the system held 190 person files and consulted none of them
when deciding what mattered, which is why both of the owner's hand-labelled
high-urgency messages were scored low.

Relevance is not a property of a message. A Deepgram job blast is objectively
bulk; it matters only because someone the owner knows works there. That fact
lives in the context store, so relevance has to be judged with the store open.

Stage A — structural, free, all messages.
    Human or machine, decided from headers and address patterns. No LLM unless
    nothing structural settles it. On this corpus this removes ~87% of mail.

Stage B — LLM, expensive, human senders only.
    Retrieve the sender's person file and recent log, then ask for a relevance
    score, a justification citing that context, and any commitments.

Commitments come back in the same call as relevance because the model has
already read the message and loaded the context; a second pass would double
the cost of the expensive stage to re-read the same two things.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.context.retrieve import get_context
from personalagi.db import get_engine, init_db
from personalagi.events import EventView, load_views, pending_events
from personalagi.llm.client import GroqClient, LLMError
from personalagi.llm.prompts import load_prompt
from personalagi.llm.schemas import RelevanceOut
from personalagi.models import Commitment, Relevance

log = logging.getLogger(__name__)

BODY_CHAR_LIMIT = 2000
# Log lines pulled per sender. Enough to establish a thread, small enough that
# the context block does not dwarf the message.
CONTEXT_LOG_LINES = 6
# Stage B results are committed every this many messages. Small enough that a
# crash loses little, large enough that commits are not the bottleneck.
SCORE_COMMIT_CHUNK = 50

NO_CONTEXT = "(no context: sender is not in the store)"

# The owner's own profile: what they work on, what they are trying to achieve,
# who matters to them. Injected into every stage B call.
#
# Without it, relevance can only ever be relational — "have I talked to this
# person before" — and first contact is structurally unanswerable. The message
# that motivated this is a Stanford alumni form the owner rated his single most
# important email: it scored 1, correctly by the rules as written, because the
# sender had no prior history. Its actual importance came from the owner's own
# background, which the system had nowhere to store.
OWNER_PROFILE_FILE = "owner.md"
NO_OWNER_PROFILE = "(no owner profile: context/owner.md does not exist)"
OWNER_PROFILE_CHAR_LIMIT = 3000


def load_owner_profile(settings: Settings) -> str:
    """Read context/owner.md, or a placeholder if it is absent."""
    path = settings.context_dir / OWNER_PROFILE_FILE
    if not path.exists():
        return NO_OWNER_PROFILE
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return NO_OWNER_PROFILE
    return text[:OWNER_PROFILE_CHAR_LIMIT]


class RelevanceError(RuntimeError):
    pass


@dataclass
class RelevanceResult:
    considered: int = 0
    stage_a_automated: int = 0
    stage_a_human: int = 0
    stage_a_uncertain: int = 0
    scored: int = 0
    failed: int = 0
    commitments_found: int = 0
    skipped_existing: int = 0
    by_score: dict[int, int] = field(default_factory=dict)
    usage_summary: str = ""

    def summary(self) -> str:
        dist = "  ".join(f"r{k}={v}" for k, v in sorted(self.by_score.items()))
        saved = self.stage_a_automated
        pct = (saved / self.considered * 100) if self.considered else 0.0
        return (
            f"considered={self.considered}  "
            f"stage A: automated={saved} ({pct:.0f}% filtered free) "
            f"human={self.stage_a_human} uncertain={self.stage_a_uncertain}\n"
            f"  stage B: scored={self.scored} failed={self.failed} "
            f"skipped={self.skipped_existing}  commitments={self.commitments_found}\n"
            f"  {dist}\n  {self.usage_summary}"
        )


def what_hash(text: str) -> str:
    """Stable dedup key for a commitment's `what` field."""
    return hashlib.sha256(" ".join((text or "").lower().split()).encode()).hexdigest()[:16]


def direction_for(promiser: str, sent_by_owner: bool) -> str:
    """Map "who promised, in this message" onto "who owes the owner".

    A truth table, not a judgement call, which is exactly why the model is not
    asked for it:

        author    + owner wrote it     -> i_owe
        author    + someone else wrote -> they_owe
        recipient + owner wrote it     -> they_owe
        recipient + someone else wrote -> i_owe

    The case that makes this necessary is the owner's own sent mail. There,
    "the sender promised it" and "the owner promised it" are the same
    sentence, so a model asked for `i_owe`/`they_owe` directly has no way to
    be consistently right.
    """
    author_owes = (promiser == "author") == sent_by_owner
    return "i_owe" if author_owes else "they_owe"


def render_context(view: EventView, settings: Settings) -> tuple[str, str, int]:
    """Retrieve the COUNTERPARTY's stored context. Returns (text, slug, tokens).

    The query is the event title, so FTS returns the log lines related to
    *this* thread rather than the person's most recent activity in general.

    "Counterparty", not "sender", and the difference is load-bearing on the
    owner's own outgoing messages. Retrieving context for the sender there
    retrieves context about the OWNER, and the model duly justified maximum
    relevance with "SENDER block shows sender is Preet Karia, the owner
    himself" — circular, and it inflated every sent message to a 3.

    The person whose history explains an outgoing message is the person it was
    sent TO.
    """
    other = view.counterparty
    slug = (other.person_slug if other else "") or (
        other.address if other else ""
    )
    if not slug or (other is not None and other.is_owner):
        return NO_CONTEXT, "", 0
    try:
        found = get_context(
            slug,
            query=view.event.title or "",
            settings=settings,
            k=CONTEXT_LOG_LINES,
            # Without this the event retrieves itself: context-build already
            # folded it into this person's log, so it comes back as a "prior"
            # entry corroborating its own importance.
            exclude_source_ids={view.event.source_id} if view.event.source_id else None,
        )
    except LookupError:
        # Ambiguous match. Treated as no context rather than guessing which
        # person it is — a confident claim about the wrong person is worse
        # than no claim.
        return NO_CONTEXT, "", 0
    if found is None:
        return NO_CONTEXT, "", 0

    # A sender whose only log line WAS this message has no independent context
    # once it is excluded. Returning the bare frontmatter would read to the
    # model as "this person is in the store", implying history that does not
    # exist — a first-ever email would look like an established relationship.
    if not found.log_lines and not found.profile.strip():
        return NO_CONTEXT, found.slug, 0

    return found.render(), found.slug, found.tokens_returned


def score_one(
    client: GroqClient,
    prompt,
    view: EventView,
    context_text: str,
    owner_label: str,
    owner_profile: str,
    *,
    max_attempts: int = 2,
) -> tuple[RelevanceOut | None, str | None]:
    """One Stage B call. Returns (result, error)."""
    body = (view.event.text or "").strip()
    if len(body) > BODY_CHAR_LIMIT:
        body = body[:BODY_CHAR_LIMIT] + "\n[...truncated]"

    user = prompt.render_user(
        owner=owner_label,
        owner_profile=owner_profile,
        context=context_text,
        sender_name=view.sender_name or "(unknown)",
        sender_email=view.sender_address or "(unknown)",
        recipients=view.recipients_line(),
        date=view.event.timestamp.isoformat(sep=" ", timespec="minutes"),
        subject=view.event.title or "(no subject)",
        body=body or "(empty body)",
    )

    last_error: str | None = None
    for attempt in range(max_attempts):
        try:
            attempt_user = user
            if attempt > 0:
                attempt_user = (
                    f"{user}\n\nYour previous reply was not valid. Return ONLY a "
                    'JSON object with keys "relevance" (integer 0-3), "why" '
                    '(string), "commitments" (array). No prose, no fences.'
                )
            content = client.complete_json(prompt.system, attempt_user)
            return RelevanceOut.model_validate_json(content), None
        except (ValidationError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        except LLMError as exc:
            last_error = f"LLMError: {str(exc)[:200]}"

    return None, last_error


def _upsert_relevance(session: Session, rows: list[dict]) -> None:
    if not rows:
        return
    for start in range(0, len(rows), 60):
        chunk = rows[start : start + 60]
        stmt = sqlite_insert(Relevance).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id"],
            set_={
                k: getattr(stmt.excluded, k)
                for k in (
                    "score", "why", "person_slug", "context_tokens", "sender_kind",
                    "sender_reason", "model", "prompt_version", "ok", "error",
                    "scored_at",
                )
            },
        )
        session.execute(stmt)
    session.commit()


def _upsert_commitments(session: Session, rows: list[dict]) -> int:
    """Insert commitments, never clobbering a human's `manually_closed`."""
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), 40):
        chunk = rows[start : start + 40]
        stmt = sqlite_insert(Commitment).values(chunk)
        # DO NOTHING, not DO UPDATE: re-extracting must not resurrect a
        # commitment the owner already marked done. The message text has not
        # changed, so there is nothing new to learn from it anyway.
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "direction", "what_hash"]
        )
        written += session.execute(stmt).rowcount or 0
    session.commit()
    return written


def score_messages(
    settings: Settings | None = None,
    *,
    account: str | None = None,
    limit: int | None = None,
    rescore: bool = False,
    workers: int | None = None,
    dry_run: bool = False,
) -> RelevanceResult:
    """Run both stages over ingested mail."""
    settings = settings or get_settings()
    init_db(settings)

    owner_addresses = settings.owner_address_set
    if not owner_addresses:
        raise RelevanceError(
            "OWNER_EMAILS is not set in .env — commitment direction cannot be "
            "determined without it, and guessing would attribute every promise "
            "to the wrong side. Set it to your own address(es), comma-separated."
        )
    owner_label = settings.owner_name or next(iter(sorted(owner_addresses)))

    result = RelevanceResult()

    with Session(get_engine(settings)) as session:
        stmt = pending_events(
            account=account,
            exclude_scored=None if rescore else select(Relevance.event_id),
            limit=limit,
        )
        messages = load_views(session, stmt)

    result.considered = len(messages)
    if not messages:
        result.usage_summary = "no messages to score"
        return result

    # --- Stage A: structural, free -------------------------------------
    now = datetime.now(UTC).replace(tzinfo=None)
    automated_rows: list[dict] = []
    human: list[EventView] = []

    for view in messages:
        # Stage A is now a read, not a computation: the adapter already
        # resolved every participant when the Event was created, so the same
        # rules apply to iMessage handles and calendar organisers for free.
        sender = view.sender
        if sender is not None and not sender.is_automated:
            result.stage_a_human += 1
            human.append(view)
            continue

        result.stage_a_automated += 1
        reason = sender.automated_reason if sender else "no sender participant"
        # Machine mail gets a score of 0 written WITHOUT an LLM call. Storing
        # it rather than leaving it absent is what makes "scored" and
        # "considered" reconcile, and keeps the filter auditable.
        automated_rows.append(
            {
                "event_id": view.event.id,
                "score": 0,
                "why": f"stage A: {reason}",
                "person_slug": "",
                "context_tokens": 0,
                "sender_kind": "automated",
                "sender_reason": reason,
                "model": "",
                "prompt_version": "",
                "ok": True,
                "error": None,
                "scored_at": now,
            }
        )
        result.by_score[0] = result.by_score.get(0, 0) + 1

    if dry_run:
        result.usage_summary = (
            f"dry run - would send {len(human)} of {len(messages)} to the model"
        )
        return result

    with Session(get_engine(settings)) as session:
        _upsert_relevance(session, automated_rows)

    if not human:
        result.usage_summary = "stage A filtered everything; no LLM calls made"
        return result

    # --- Stage B: LLM, human senders only ------------------------------
    prompt = load_prompt("relevance")
    owner_profile = load_owner_profile(settings)
    if owner_profile == NO_OWNER_PROFILE:
        log.warning(
            "no context/owner.md — relevance can only be relational, so mail "
            "from first-time senders cannot score above 1 however important it is"
        )
    # The big model, deliberately. Stage A already removed ~88% of the corpus
    # at zero token cost, so this runs on hundreds of messages rather than
    # thousands — that is what buys the headroom, not a bigger budget.
    client = GroqClient(settings, model=settings.groq_model_large or None)
    log.info(
        "stage B: %d human-sender message(s) with %s (prompt %s)",
        len(human),
        client.model,
        prompt.version,
    )

    def work(view: EventView):
        context_text, slug, tokens = render_context(view, settings)
        parsed, error = score_one(
            client, prompt, view, context_text, owner_label, owner_profile
        )
        sender = view.sender
        base = {
            "event_id": view.event.id,
            "person_slug": slug,
            "context_tokens": tokens,
            "sender_kind": "human",
            "sender_reason": sender.automated_reason if sender else "",
            "model": client.model,
            "prompt_version": prompt.version,
            "scored_at": now,
        }
        if parsed is None:
            return (
                {**base, "score": 0, "why": "", "ok": False, "error": error},
                [],
            )

        sent_by_owner = view.sent_by_owner
        # The counterparty is the other end of the conversation, resolved on
        # the Event. Using the sender would report that the owner owes
        # themselves for every promise made in their own sent mail.
        other = view.counterparty
        other_name = other.display_name if other else ""
        other_email = other.address if other else ""
        other_slug = (other.person_slug if other else "") or ""
        commitments = []
        for item_out in parsed.commitments:
            # A quote that is not actually in the body means the model wrote
            # the evidence itself, which is the one failure mode that makes
            # this feature worse than useless. Drop it.
            if not _quote_is_grounded(item_out.quote, view.event.text):
                log.debug(
                    "dropping ungrounded commitment on event %s: %r",
                    view.event.source_id,
                    item_out.quote[:80],
                )
                continue
            direction = direction_for(item_out.promiser, sent_by_owner)
            commitments.append(
                {
                    "event_id": view.event.id,
                    "direction": direction,
                    "person_slug": other_slug,
                    "person_name": other_name,
                    "person_email": other_email,
                    "what": item_out.what,
                    "what_hash": what_hash(item_out.what),
                    "quote": item_out.quote,
                    "due_text": item_out.due_text,
                    "status": "open",
                    "promised_at": view.event.timestamp,
                    "manually_closed": False,
                    "model": client.model,
                    "prompt_version": prompt.version,
                    "extracted_at": now,
                }
            )

        return (
            {
                **base,
                "score": parsed.relevance,
                "why": parsed.why,
                "ok": True,
                "error": None,
            },
            commitments,
        )

    from concurrent.futures import ThreadPoolExecutor

    # Commit per chunk, not once at the end. Stage B is the expensive path —
    # every row represents a paid LLM call — so a crash at message 450 of 461
    # must not throw away 450 calls' worth of work. Unlike ingest there is no
    # cursor making a re-run cheap; the tokens are simply spent again.
    pool_size = workers or settings.relevance_workers
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        for start in range(0, len(human), SCORE_COMMIT_CHUNK):
            batch = human[start : start + SCORE_COMMIT_CHUNK]
            outputs = list(pool.map(work, batch))

            relevance_rows = [row for row, _ in outputs]
            commitment_rows = [c for _, cs in outputs for c in cs]

            for row in relevance_rows:
                if row["ok"]:
                    result.scored += 1
                    result.by_score[row["score"]] = (
                        result.by_score.get(row["score"], 0) + 1
                    )
                else:
                    result.failed += 1

            with Session(get_engine(settings)) as session:
                _upsert_relevance(session, relevance_rows)
                result.commitments_found += _upsert_commitments(
                    session, commitment_rows
                )
            log.info(
                "stage B %d/%d scored", min(start + len(batch), len(human)), len(human)
            )

    result.usage_summary = client.usage.summary()
    log.info(result.summary())
    return result


def _quote_is_grounded(quote: str, body: str) -> bool:
    """Is this quote actually in the message?

    The commitment feature's entire credibility is "here are the words". A
    model that helpfully tidies a quote produces something the sender never
    wrote, and showing someone a sentence they did not write is worse than
    surfacing nothing at all.

    Compared on collapsed whitespace and casefolded, because the body has been
    through HTML stripping and exact-character matching would reject valid
    quotes for cosmetic reasons.
    """
    if not quote:
        return False
    needle = " ".join(quote.split()).casefold()
    haystack = " ".join((body or "").split()).casefold()
    if len(needle) < 12:
        return False  # too short to be evidence of anything
    return needle in haystack
