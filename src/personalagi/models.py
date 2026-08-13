"""SQLModel tables for the derived index.

Markdown is the source of truth for context; these tables are the ingest
landing zone and the retrieval index. Nothing here is authoritative.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel, UniqueConstraint


class Message(SQLModel, table=True):
    """A normalized Gmail message.

    Unique on (account_label, gmail_id), not gmail_id alone: the same message
    can legitimately appear in two of your accounts and each copy has its own
    labels and visibility. This constraint is what makes re-ingest a no-op.
    """

    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("account_label", "gmail_id", name="uq_message_account_gmail"),
    )

    id: int | None = Field(default=None, primary_key=True)
    gmail_id: str = Field(index=True)
    thread_id: str = Field(index=True)
    account_label: str = Field(index=True)

    sender_name: str = ""
    sender_email: str = Field(default="", index=True)
    subject: str = ""
    body_text: str = ""

    # Whitelisted headers as a JSON object (see normalize.HEADER_WHITELIST).
    # Nullable because rows ingested before this column existed have none —
    # `refresh-headers` backfills them. A missing value means "not fetched",
    # NOT "no bulk headers", and the two must never be conflated: treating
    # unknown as clean would silently reclassify 4,000 messages as human.
    headers_json: str | None = Field(default=None)

    # timestamp is the human-facing value; internal_date_ms is Gmail's raw
    # epoch-ms, kept because the watermark advances on it exactly.
    timestamp: datetime = Field(index=True)
    internal_date_ms: int = Field(index=True)
    ingested_at: datetime


class Classification(SQLModel, table=True):
    """One classification per message.

    Unique on message_id so re-classifying updates in place rather than
    accumulating rows. `prompt_version` and `model` are stored on every row
    because an eval number is meaningless without knowing which prompt and
    model produced it.
    """

    __tablename__ = "classification"
    __table_args__ = (UniqueConstraint("message_id", name="uq_classification_message"),)

    id: int | None = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="message.id", index=True)

    # needs_response | fyi | promotional | spam | unclassified
    category: str = Field(index=True)
    urgency: str = Field(default="low", index=True)
    summary: str = ""

    model: str = ""
    prompt_version: str = ""
    # False when the model failed twice and the row is a tombstone, so a
    # parse failure is visible in the data instead of silently absent.
    ok: bool = True
    error: str | None = None
    classified_at: datetime


class Relevance(SQLModel, table=True):
    """Stage B output: does this message matter to the owner?

    Deliberately a separate table from Classification rather than more columns
    on it. Classification answers "what kind of message is this", which is a
    property of the message alone. Relevance answers "does this matter to me",
    which is a property of the message PLUS the context store — different
    inputs, different cost, different refresh cadence. A job alert's category
    never changes; its relevance changes the day you meet someone who works
    there.
    """

    __tablename__ = "relevance"
    __table_args__ = (UniqueConstraint("message_id", name="uq_relevance_message"),)

    id: int | None = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="message.id", index=True)

    # 0 bulk-no-connection, 1 unknown-but-plausible, 2 known-person,
    # 3 touches an open commitment or a tracked person. See evals/TAXONOMY.md.
    score: int = Field(default=0, index=True)
    # The model's justification, which must cite the retrieved context. Stored
    # because a relevance score with no reason is unauditable, and D8 says
    # every proposal carries its evidence.
    why: str = ""
    # Which person file was retrieved, so the reason can be traced back.
    person_slug: str = Field(default="", index=True)
    context_tokens: int = 0

    # Stage A's structural verdict, kept so the cheap path is auditable and
    # a bad filter is diagnosable without re-running it.
    sender_kind: str = Field(default="", index=True)  # human | automated
    sender_reason: str = ""

    model: str = ""
    prompt_version: str = ""
    ok: bool = True
    error: str | None = None
    scored_at: datetime


class Commitment(SQLModel, table=True):
    """Something someone promised, in either direction.

    The actual product. Triage tells you what arrived; this tells you what you
    said you would do and have not done.

    Unique on (message_id, direction, what_hash) so re-extracting the same
    message does not duplicate a commitment, while still allowing one message
    to create several ("I'll send the deck and introduce you to Sam").
    """

    __tablename__ = "commitment"
    __table_args__ = (
        UniqueConstraint(
            "message_id", "direction", "what_hash", name="uq_commitment_dedup"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="message.id", index=True)

    # "i_owe" — the owner promised someone something.
    # "they_owe" — someone promised the owner something.
    direction: str = Field(index=True)
    # The counterparty: whoever is not the owner.
    person_slug: str = Field(default="", index=True)
    person_name: str = ""
    person_email: str = Field(default="", index=True)

    what: str = ""
    # Stable hash of `what`, because SQLite cannot index a long free-text
    # column usefully and the dedup constraint needs a fixed-width key.
    what_hash: str = Field(default="", index=True)
    # The sentence that created the obligation, verbatim. Never paraphrased:
    # "you promised X" is only credible if you can show the words.
    quote: str = ""
    due_text: str = ""

    # open | done | stale. `stale` is derived from age, not asserted by the
    # model, so the rule is inspectable and cheap to change.
    status: str = Field(default="open", index=True)
    promised_at: datetime = Field(index=True)
    resolved_at: datetime | None = None
    # Human override. A commitment marked done by hand must survive the next
    # extraction run, exactly like a person file's `corrections`.
    manually_closed: bool = False

    model: str = ""
    prompt_version: str = ""
    extracted_at: datetime


class IngestState(SQLModel, table=True):
    """Per-account sync cursors.

    Two cursors, not one. Gmail's messages.list returns newest-first, so a
    capped bootstrap keeps the NEWEST n and leaves a hole in the past. A
    single forward watermark cannot represent that: it correctly says "caught
    up with new mail" while older mail sits permanently unreachable.

    - forward cursor  (last_history_id / last_internal_date_ms) -> new mail
    - backfill cursor (oldest_internal_date_ms)                 -> old mail

    Both advance only after the message batch commits, so a crash re-fetches
    (cheap, deduped) rather than skips (silent, permanent).
    """

    __tablename__ = "ingest_state"

    account_label: str = Field(primary_key=True)

    # --- forward cursor: everything NEWER than this is fetched ---
    # Primary watermark. Gmail expires history after ~1 week; when that
    # happens this is cleared and the date watermark takes over.
    last_history_id: str | None = None
    # Fallback watermark. Never expires.
    last_internal_date_ms: int | None = None

    # --- backfill cursor: everything NEWER than this is fetched, back to
    # here. Walking further back is `personalagi backfill`. ---
    oldest_internal_date_ms: int | None = None
    # True only once a backfill pass returns nothing older. Until then there
    # is, or may be, unreachable history.
    backfill_complete: bool = False
    # Set when a message cap truncated a listing. Purely informational, but
    # it is the flag that would have made the original bug visible.
    last_run_truncated: bool = False

    last_synced_at: datetime | None = None
    message_count: int = 0
