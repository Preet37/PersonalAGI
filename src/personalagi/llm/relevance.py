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
from email.utils import getaddresses

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from personalagi.config import Settings, get_settings
from personalagi.context.people import slug_for
from personalagi.context.retrieve import get_context
from personalagi.db import get_engine, init_db
from personalagi.identity import classify_sender, shared_addresses
from personalagi.llm.client import GroqClient, LLMError
from personalagi.llm.prompts import load_prompt
from personalagi.llm.schemas import RelevanceOut
from personalagi.models import Commitment, Message, Relevance

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


def _headers_of(message: Message) -> dict[str, str]:
    if not message.headers_json:
        return {}
    try:
        loaded = json.loads(message.headers_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _recipients_of(message: Message) -> str:
    headers = _headers_of(message)
    to = headers.get("to", "")
    cc = headers.get("cc", "")
    joined = ", ".join(p for p in (to, cc) if p)
    return joined[:300] or "(unknown)"


def counterparty(
    message: Message, owner_addresses: set[str]
) -> tuple[str, str]:
    """Who the commitment is WITH. Returns (name, email).

    Not the sender. On mail the owner sent, the sender IS the owner, and using
    it produces "Preet Karia owes Preet Karia" — which is what the first real
    run of `owed` reported for all three commitments it found.

    The counterparty is the other end of the conversation: the sender on
    received mail, and the first non-owner recipient on sent mail.
    """
    if (message.sender_email or "").strip().lower() not in owner_addresses:
        return message.sender_name or "", message.sender_email or ""

    headers = _headers_of(message)
    for field_name in ("to", "cc"):
        for name, address in getaddresses([headers.get(field_name, "")]):
            address = (address or "").strip().lower()
            if address and address not in owner_addresses:
                return (name or "").strip().strip('"'), address

    # Sent mail with no other recipient we can see — a note to self, or the
    # To header was never fetched. Attributing it to the owner would be wrong,
    # so it is left blank and groups under "(unknown)" rather than lying.
    return "", ""


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


def owner_sent(message: Message, owner_addresses: set[str]) -> bool:
    """True when the owner is the SENDER of this message.

    Commitment direction inverts on this. The model is told which side the
    owner is on rather than being asked to work it out, because it is a
    lookup, and asking a model to do a lookup is how you get it wrong 5% of
    the time for no reason.
    """
    return (message.sender_email or "").strip().lower() in owner_addresses


def render_context(message: Message, settings: Settings) -> tuple[str, str, int]:
    """Retrieve the sender's stored context. Returns (text, slug, tokens).

    The query is the message subject, so FTS returns the log lines related to
    *this* thread rather than the person's most recent activity in general.
    """
    slug = slug_for(message.sender_name, message.sender_email)
    try:
        found = get_context(
            slug,
            query=message.subject or "",
            settings=settings,
            k=CONTEXT_LOG_LINES,
            # Without this the message retrieves itself: context-build already
            # folded it into this person's log, so it comes back as a "prior"
            # entry corroborating its own importance.
            exclude_gmail_ids={message.gmail_id} if message.gmail_id else None,
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
    message: Message,
    context_text: str,
    owner_label: str,
    owner_profile: str,
    *,
    max_attempts: int = 2,
) -> tuple[RelevanceOut | None, str | None]:
    """One Stage B call. Returns (result, error)."""
    body = (message.body_text or "").strip()
    if len(body) > BODY_CHAR_LIMIT:
        body = body[:BODY_CHAR_LIMIT] + "\n[...truncated]"

    user = prompt.render_user(
        owner=owner_label,
        owner_profile=owner_profile,
        context=context_text,
        sender_name=message.sender_name or "(unknown)",
        sender_email=message.sender_email or "(unknown)",
        recipients=_recipients_of(message),
        date=message.timestamp.isoformat(sep=" ", timespec="minutes"),
        subject=message.subject or "(no subject)",
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
            index_elements=["message_id"],
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
            index_elements=["message_id", "direction", "what_hash"]
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
        stmt = select(Message)
        if account:
            stmt = stmt.where(Message.account_label == account)
        if not rescore:
            stmt = stmt.where(Message.id.not_in(select(Relevance.message_id)))
        stmt = stmt.order_by(Message.internal_date_ms.desc())
        if limit:
            stmt = stmt.limit(limit)
        messages = list(session.execute(stmt).scalars())

        # Shared-envelope detection needs the whole corpus, not this batch:
        # a bulk address only reveals itself across many messages.
        all_pairs = list(
            session.execute(select(Message.sender_email, Message.sender_name))
        )

    result.considered = len(messages)
    if not messages:
        result.usage_summary = "no messages to score"
        return result

    shared = shared_addresses(all_pairs)

    # --- Stage A: structural, free -------------------------------------
    now = datetime.now(UTC).replace(tzinfo=None)
    automated_rows: list[dict] = []
    human: list[tuple[Message, object]] = []

    for message in messages:
        verdict = classify_sender(message.sender_email, _headers_of(message), shared)
        if verdict.is_human:
            result.stage_a_human += 1
            if verdict.needs_llm:
                result.stage_a_uncertain += 1
            human.append((message, verdict))
            continue

        result.stage_a_automated += 1
        # Machine mail gets a score of 0 written WITHOUT an LLM call. Storing
        # it rather than leaving it absent is what makes "scored" and
        # "considered" reconcile, and keeps the filter auditable.
        automated_rows.append(
            {
                "message_id": message.id,
                "score": 0,
                "why": f"stage A: {verdict.reason}",
                "person_slug": "",
                "context_tokens": 0,
                "sender_kind": "automated",
                "sender_reason": verdict.reason,
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

    def work(item):
        message, verdict = item
        context_text, slug, tokens = render_context(message, settings)
        parsed, error = score_one(
            client, prompt, message, context_text, owner_label, owner_profile
        )
        base = {
            "message_id": message.id,
            "person_slug": slug,
            "context_tokens": tokens,
            "sender_kind": "human",
            "sender_reason": verdict.reason,
            "model": client.model,
            "prompt_version": prompt.version,
            "scored_at": now,
        }
        if parsed is None:
            return (
                {**base, "score": 0, "why": "", "ok": False, "error": error},
                [],
            )

        sent_by_owner = owner_sent(message, owner_addresses)
        other_name, other_email = counterparty(message, owner_addresses)
        # The person file is keyed on the counterparty too, so a promise made
        # in sent mail files under the person it was made TO.
        other_slug = slug_for(other_name, other_email) if other_email else ""
        commitments = []
        for item_out in parsed.commitments:
            # A quote that is not actually in the body means the model wrote
            # the evidence itself, which is the one failure mode that makes
            # this feature worse than useless. Drop it.
            if not _quote_is_grounded(item_out.quote, message.body_text):
                log.debug(
                    "dropping ungrounded commitment on message %s: %r",
                    message.gmail_id,
                    item_out.quote[:80],
                )
                continue
            direction = direction_for(item_out.promiser, sent_by_owner)
            commitments.append(
                {
                    "message_id": message.id,
                    "direction": direction,
                    "person_slug": other_slug,
                    "person_name": other_name,
                    "person_email": other_email,
                    "what": item_out.what,
                    "what_hash": what_hash(item_out.what),
                    "quote": item_out.quote,
                    "due_text": item_out.due_text,
                    "status": "open",
                    "promised_at": message.timestamp,
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
