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


DIRECTIONS: tuple[str, ...] = ("i_owe", "they_owe")

# The model is asked who made the promise in terms of the MESSAGE — author or
# recipient — never in terms of the owner. Whether the author is the owner is a
# lookup on the From address, and doing that lookup in code makes the mapping
# deterministic. Asking the model to reason about it instead breaks on the
# common case of the owner reading a message they themselves sent, where "the
# sender promised it" and "the owner promised it" are the same sentence.
_PROMISER_ALIASES = {
    "sender": "author",
    "writer": "author",
    "me": "author",
    "them": "recipient",
    "receiver": "recipient",
    "addressee": "recipient",
}


class CommitmentOut(BaseModel):
    """One promise found in a message."""

    model_config = ConfigDict(extra="ignore")

    # Who made the promise, relative to the message itself.
    promiser: Literal["author", "recipient"]
    what: str = Field(default="", max_length=300)
    # Verbatim source text. The whole mechanism rests on being able to show
    # the words that created the obligation, so a paraphrase here is a bug.
    quote: str = Field(default="", max_length=500)
    due_text: str = Field(default="", max_length=120)

    @field_validator("promiser", mode="before")
    @classmethod
    def _normalize_promiser(cls, value: object) -> object:
        if isinstance(value, str):
            key = value.strip().lower()
            return _PROMISER_ALIASES.get(key, key)
        return value

    @field_validator("what", "quote", "due_text", mode="before")
    @classmethod
    def _flatten(cls, value: object) -> object:
        if isinstance(value, str):
            return " ".join(value.split())
        return value


class RelevanceOut(BaseModel):
    """Stage B output: relevance judged WITH the person's context in hand."""

    model_config = ConfigDict(extra="ignore")

    relevance: int = Field(default=0, ge=0, le=3)
    why: str = Field(default="", max_length=400)
    commitments: list[CommitmentOut] = Field(default_factory=list)

    @field_validator("relevance", mode="before")
    @classmethod
    def _coerce_relevance(cls, value: object) -> object:
        # Models reach for words even when asked for an integer.
        if isinstance(value, str):
            words = {"none": 0, "low": 1, "medium": 2, "med": 2, "high": 3}
            key = value.strip().lower()
            if key in words:
                return words[key]
            try:
                return int(float(key))
            except ValueError:
                return value
        return value

    @field_validator("why", mode="before")
    @classmethod
    def _flatten_why(cls, value: object) -> object:
        if isinstance(value, str):
            return " ".join(value.split())[:400]
        return value

    @field_validator("commitments", mode="before")
    @classmethod
    def _drop_malformed_commitments(cls, value: object) -> object:
        """A bad commitment must not cost us the relevance score.

        These two outputs come back in one call for cost reasons, but they are
        independent findings. Dropping one malformed commitment beats
        tombstoning the whole message.
        """
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict) and item.get("what")]
        return []


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
