"""Validated shapes for LLM output.

The model is asked for JSON, but "asked for" is not "guaranteed". Everything
crossing the boundary from a model into the database goes through pydantic
first, and anything that fails validation becomes an explicit `unclassified`
tombstone rather than a silently missing row.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Category = Literal["needs_response", "fyi", "promotional", "spam"]
Urgency = Literal["low", "med", "high"]

CATEGORIES: tuple[str, ...] = ("needs_response", "fyi", "promotional", "spam")
URGENCIES: tuple[str, ...] = ("low", "med", "high")

UNCLASSIFIED = "unclassified"

# Common ways a model renders the right idea in the wrong token.
_CATEGORY_ALIASES = {
    "needs response": "needs_response",
    "needs-response": "needs_response",
    "needsresponse": "needs_response",
    "reply_needed": "needs_response",
    "action_required": "needs_response",
    "informational": "fyi",
    "info": "fyi",
    "f.y.i.": "fyi",
    "marketing": "promotional",
    "promo": "promotional",
    "promotion": "promotional",
    "junk": "spam",
    "phishing": "spam",
}
_URGENCY_ALIASES = {
    "medium": "med",
    "normal": "med",
    "moderate": "med",
    "none": "low",
    "urgent": "high",
    "critical": "high",
}


class ClassificationOut(BaseModel):
    """What the classifier prompt must return."""

    model_config = ConfigDict(extra="ignore")

    category: Category
    urgency: Urgency = "low"
    summary: str = Field(default="", max_length=400)

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, value: object) -> object:
        if isinstance(value, str):
            key = value.strip().lower()
            return _CATEGORY_ALIASES.get(key, key)
        return value

    @field_validator("urgency", mode="before")
    @classmethod
    def _normalize_urgency(cls, value: object) -> object:
        if isinstance(value, str):
            key = value.strip().lower()
            return _URGENCY_ALIASES.get(key, key)
        return value

    @field_validator("summary", mode="before")
    @classmethod
    def _flatten_summary(cls, value: object) -> object:
        if isinstance(value, str):
            # "one line" is a contract; enforce it rather than hoping.
            flat = " ".join(value.split())
            return flat[:400]
        return value
