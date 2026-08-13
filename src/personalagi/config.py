"""Settings loaded from .env — see .env.example for the full list."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM (unused by ingest) ---
    groq_api_key: str = "gsk_replace_me"
    groq_model: str = "llama-3.3-70b-versatile"
    # Reserved for the low-volume, high-value path. Stage A removes ~88% of
    # mail for free, so stage B runs on ~460 messages instead of ~4,000 —
    # which is the whole reason a bigger model is affordable there.
    groq_model_large: str = ""

    # --- Gmail ---
    gmail_accounts: str = "personal"
    google_credentials_path: Path = Path("credentials/credentials.json")
    tokens_dir: Path = Path("tokens")

    # --- Storage ---
    context_dir: Path = Path("context")
    # Rendered briefs. Gitignored: they summarise private correspondence.
    briefs_dir: Path = Path("briefs")
    database_url: str = "sqlite:///data/personalagi.db"

    # --- API ---
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # --- Ingest tuning ---
    # First run has no watermark; bound the initial pull instead of fetching
    # the entire mailbox by accident.
    initial_backfill_days: int = 30
    # Safety window subtracted from the date watermark. Mail is not delivered
    # in internalDate order; see README. Free because writes are idempotent.
    ingest_overlap_seconds: int = 86_400
    # 0 = no cap. Useful for a first cautious run.
    ingest_max_messages: int = 0
    # Concurrent messages.get calls. Default 1 (sequential) because it is
    # the proven path; >1 gives each thread its own Gmail client because
    # googleapiclient's http layer is not thread-safe. 2000 sequential
    # fetches measured ~50 minutes, so raising this is the single biggest
    # speedup available to ingest.
    fetch_workers: int = 1

    # --- Identity ---
    # The owner's own addresses, comma-separated. Commitment direction depends
    # on this: a promise in a message the owner SENT is one they made, and the
    # same sentence in a received message is one they were given. With this
    # unset every commitment would be attributed to the wrong side, so the
    # extractor refuses to run rather than guessing.
    owner_emails: str = ""
    owner_name: str = ""

    # --- Relevance (stage B) ---
    # Only human-sender messages reach the expensive pass; on this corpus that
    # is ~500 of 3,998, which is what makes a larger model affordable here.
    relevance_workers: int = 4
    # Days without follow-up before an open commitment is called stale.
    commitment_stale_days: int = 7

    # --- Classification ---
    # Concurrent Groq calls. Kept low by default: Groq rate-limits per model
    # per minute, and 429-then-backoff is slower than never hitting the limit.
    classify_workers: int = 4
    # Body characters sent to the model. See classify.BODY_CHAR_LIMIT.
    classify_body_chars: int = 1500

    @property
    def account_labels(self) -> list[str]:
        return [label.strip() for label in self.gmail_accounts.split(",") if label.strip()]

    @property
    def owner_address_set(self) -> set[str]:
        return {a.strip().lower() for a in self.owner_emails.split(",") if a.strip()}

    def token_path(self, label: str) -> Path:
        return self.tokens_dir / f"{label}.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
