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

    # --- Gmail ---
    gmail_accounts: str = "personal"
    google_credentials_path: Path = Path("credentials/credentials.json")
    tokens_dir: Path = Path("tokens")

    # --- Storage ---
    context_dir: Path = Path("context")
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

    @property
    def account_labels(self) -> list[str]:
        return [label.strip() for label in self.gmail_accounts.split(",") if label.strip()]

    def token_path(self, label: str) -> Path:
        return self.tokens_dir / f"{label}.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
