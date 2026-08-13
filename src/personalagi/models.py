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

    # timestamp is the human-facing value; internal_date_ms is Gmail's raw
    # epoch-ms, kept because the watermark advances on it exactly.
    timestamp: datetime = Field(index=True)
    internal_date_ms: int = Field(index=True)
    ingested_at: datetime


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
