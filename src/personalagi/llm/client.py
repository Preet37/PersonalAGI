"""Groq client wrapper: JSON completions with retry, backoff, and accounting.

Two things here are not obvious and cost real money if you get them wrong.

1. The gpt-oss models are REASONING models. Reasoning tokens are billed as
   output and count against max_tokens. With a small max_tokens the reasoning
   consumes the whole budget, content comes back empty, and JSON mode fails
   with `json_validate_failed` and an empty `failed_generation` — which looks
   like a prompt bug and is not. Measured on 2026-08-13: the same trivial call
   used 462 output tokens at default effort and 35 at `reasoning_effort="low"`.
   Classification is the highest-volume path in the system, so it uses low.

2. Groq rate-limits per model per minute. 429s carry a `retry-after` header;
   honoring it is much better than guessing.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from groq import APIConnectionError, APIStatusError, Groq, RateLimitError

from personalagi.config import Settings, get_settings

log = logging.getLogger(__name__)

MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    """Token accounting, so cost is measurable rather than assumed."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    failures: int = 0
    _lock: object = field(default=None, repr=False, compare=False)

    def record(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def summary(self) -> str:
        return (
            f"{self.calls} calls  "
            f"{self.prompt_tokens} in / {self.completion_tokens} out "
            f"({self.total_tokens} total)  "
            f"retries={self.retries} failures={self.failures}"
        )


def _retry_after_seconds(exc: Exception, attempt: int) -> float:
    """Prefer the server's retry-after; fall back to exponential + jitter."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw:
            try:
                return min(float(raw), MAX_BACKOFF_SECONDS)
            except (TypeError, ValueError):
                pass
    delay = BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 1)
    return min(delay, MAX_BACKOFF_SECONDS)


class GroqClient:
    """Thin, thread-safe-enough wrapper. The SDK client is safe to share."""

    def __init__(self, settings: Settings | None = None, *, model: str | None = None):
        self.settings = settings or get_settings()
        if not self.settings.groq_api_key or self.settings.groq_api_key.startswith(
            "gsk_replace"
        ):
            raise LLMError("GROQ_API_KEY is unset or still the placeholder in .env")
        self.model = model or self.settings.groq_model
        self._client = Groq(api_key=self.settings.groq_api_key)
        self.usage = Usage()

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1200,
        temperature: float = 0.0,
        reasoning_effort: str | None = "low",
    ) -> str:
        """Return raw JSON text. Parsing and validation are the caller's job."""
        kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.chat.completions.create(**kwargs)
            except (RateLimitError, APIConnectionError) as exc:
                last_exc = exc
                delay = _retry_after_seconds(exc, attempt)
                self.usage.retries += 1
                log.warning(
                    "groq %s, sleeping %.1fs (attempt %d/%d)",
                    type(exc).__name__,
                    delay,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(delay)
                continue
            except APIStatusError as exc:
                # 5xx is worth retrying; 4xx other than 429 is a real error.
                if exc.status_code and 500 <= exc.status_code < 600:
                    last_exc = exc
                    delay = _retry_after_seconds(exc, attempt)
                    self.usage.retries += 1
                    time.sleep(delay)
                    continue
                self.usage.failures += 1
                raise LLMError(f"groq {exc.status_code}: {str(exc)[:300]}") from exc

            usage = response.usage
            if usage:
                self.usage.record(usage.prompt_tokens or 0, usage.completion_tokens or 0)

            content = (response.choices[0].message.content or "").strip()
            if not content:
                # Almost always the reasoning-token budget problem described
                # in the module docstring.
                self.usage.failures += 1
                raise LLMError(
                    "groq returned empty content - reasoning tokens likely consumed "
                    f"max_tokens ({max_tokens}); raise it or lower reasoning_effort"
                )
            return content

        self.usage.failures += 1
        raise LLMError(f"groq unavailable after {MAX_RETRIES} attempts: {last_exc}")
