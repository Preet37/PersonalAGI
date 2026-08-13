"""Nightly compaction: fold new log entries into the Profile (D5).

Three invariants, all load-bearing:

1. **Raw log lines are never deleted.** Compaction only rewrites the summary.
   If a profile drops something that later matters, the evidence is still on
   disk and the next compaction can pick it up.
2. **Every version is kept**, one JSON line per compaction in
   context/history/<slug>.jsonl, so a profile's evolution is diffable and you
   can see exactly when a claim appeared.
3. **Corrections are authoritative and survive compaction.** This is the
   answer to ARCHITECTURE.md open question 4 — "if the profile is wrong, how
   do you correct it so compaction doesn't reintroduce the error?" A
   correction lives in frontmatter, is human-authored, is injected into every
   compaction prompt as overriding, and is never rewritten by the model. The
   log keeps saying the wrong thing; the correction keeps beating it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from personalagi.config import Settings, get_settings
from personalagi.context.people import PersonFile, iter_people, load_person, save_person
from personalagi.llm.client import GroqClient, LLMError
from personalagi.llm.prompts import load_prompt

log = logging.getLogger(__name__)

PROFILE_WORD_CAP = 150
# Entries shown to the model per compaction. The profile is meant to be
# durable identity, not a rolling digest, so a bounded window is correct.
ENTRY_WINDOW = 40


class ProfileOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    profile: str


@dataclass
class CompactResult:
    compacted: int = 0
    skipped_unchanged: int = 0
    failed: int = 0
    people: list[str] = field(default_factory=list)
    usage_summary: str = ""

    def summary(self) -> str:
        return (
            f"compacted={self.compacted}  unchanged={self.skipped_unchanged}  "
            f"failed={self.failed}\n  {self.usage_summary}"
        )


def history_path(context_dir: Path, slug: str) -> Path:
    return context_dir / "history" / f"{slug}.jsonl"


def _fingerprint(person: PersonFile) -> str:
    """What the profile was derived from. Changes only when inputs change."""
    ids = sorted(person.known_gmail_ids())
    corrections = "|".join(person.corrections)
    return f"{len(person.log)}:{hash(tuple(ids)) & 0xFFFFFFFF:08x}:{hash(corrections) & 0xFFFF:04x}"


def _last_fingerprint(context_dir: Path, slug: str) -> str | None:
    path = history_path(context_dir, slug)
    if not path.exists():
        return None
    last = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                continue
    return (last or {}).get("fingerprint")


def append_history(
    context_dir: Path,
    person: PersonFile,
    *,
    profile: str,
    fingerprint: str,
    model: str,
    prompt_version: str,
    entries_considered: int,
) -> None:
    path = history_path(context_dir, person.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "slug": person.slug,
        "profile": profile,
        "fingerprint": fingerprint,
        "model": model,
        "prompt_version": prompt_version,
        "log_entries": len(person.log),
        "entries_considered": entries_considered,
        "corrections": list(person.corrections),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_history(context_dir: Path, slug: str) -> list[dict]:
    path = history_path(context_dir, slug)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def enforce_word_cap(text: str, cap: int = PROFILE_WORD_CAP) -> str:
    """Hard cap, because the prompt asking nicely is not a guarantee.

    Truncates at a sentence boundary when one is available, so the profile
    does not end mid-clause.
    """
    words = text.split()
    if len(words) <= cap:
        return text.strip()

    truncated = " ".join(words[:cap])
    for terminator in (". ", "! ", "? "):
        idx = truncated.rfind(terminator)
        if idx > len(truncated) * 0.6:
            return truncated[: idx + 1].strip()
    return truncated.rstrip(",;: ") + "."


def compact_person(
    person: PersonFile,
    client: GroqClient,
    prompt,
    *,
    window: int = ENTRY_WINDOW,
) -> tuple[str | None, str | None]:
    """Produce an updated profile. Returns (profile, error)."""
    recent = person.sorted_log()[:window]
    entries = "\n".join(entry.render() for entry in recent) or "(no entries)"
    corrections = (
        "\n".join(f"- {c}" for c in person.corrections)
        if person.corrections
        else "(none)"
    )

    user = prompt.render_user(
        name=person.name or person.slug,
        emails=", ".join(person.emails) or "(unknown)",
        relationship=person.relationship,
        corrections=corrections,
        profile=person.profile.strip() or "(no profile yet)",
        new_count=len(recent),
        entries=entries,
    )

    for attempt in range(2):
        try:
            attempt_user = user
            if attempt:
                attempt_user = f'{user}\n\nReturn ONLY {{"profile": "..."}} as JSON.'
            raw = client.complete_json(prompt.system, attempt_user, max_tokens=1500)
            parsed = ProfileOut.model_validate_json(raw)
            text = enforce_word_cap(" ".join(parsed.profile.split()))
            if text:
                return text, None
        except (ValidationError, LLMError, json.JSONDecodeError) as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            if attempt:
                return None, error
    return None, "empty profile returned"


def compact_all(
    settings: Settings | None = None,
    *,
    person: str | None = None,
    force: bool = False,
    context_dir: Path | None = None,
    min_entries: int = 1,
) -> CompactResult:
    """Compact every person whose inputs changed since the last run."""
    settings = settings or get_settings()
    root = context_dir or settings.context_dir

    prompt = load_prompt("compact")
    client = GroqClient(settings)
    result = CompactResult()

    if person:
        loaded = load_person(root, person)
        people = [loaded] if loaded else []
    else:
        people = list(iter_people(root))

    for entry in people:
        if len(entry.log) < min_entries:
            result.skipped_unchanged += 1
            continue

        fingerprint = _fingerprint(entry)
        if not force and fingerprint == _last_fingerprint(root, entry.slug):
            # Nothing new since the last compaction; re-running would spend a
            # call to produce the same paragraph.
            result.skipped_unchanged += 1
            continue

        profile, error = compact_person(entry, client, prompt)
        if profile is None:
            log.warning("compaction failed for %s: %s", entry.slug, error)
            result.failed += 1
            continue

        entry.profile = profile
        save_person(root, entry)
        append_history(
            root,
            entry,
            profile=profile,
            fingerprint=fingerprint,
            model=client.model,
            prompt_version=prompt.version,
            entries_considered=min(len(entry.log), ENTRY_WINDOW),
        )
        result.compacted += 1
        result.people.append(entry.slug)

    result.usage_summary = client.usage.summary()
    log.info(result.summary())
    return result


def add_correction(
    slug_or_name: str,
    correction: str,
    settings: Settings | None = None,
    *,
    context_dir: Path | None = None,
) -> PersonFile:
    """Record an authoritative correction on a person file.

    Deliberately does NOT rewrite the profile itself. The correction is the
    durable artifact; the profile is regenerated from it on the next
    compaction. Editing the profile directly would work once and then be
    silently undone by the nightly job — which is exactly the failure this
    mechanism exists to prevent.
    """
    from personalagi.context.retrieve import resolve_person

    settings = settings or get_settings()
    root = context_dir or settings.context_dir

    found = resolve_person(root, slug_or_name)
    if found is None:
        raise LookupError(f"no person file matching '{slug_or_name}'")

    text = correction.strip()
    if text and text not in found.corrections:
        found.corrections.append(text)
        save_person(root, found)
    return found
