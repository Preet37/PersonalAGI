"""Person files: the two-layer markdown format that is the source of truth.

Layout (ARCHITECTURE.md D3):

    ---
    name: Dana Okafor
    emails: [dana@example.com]
    ...
    ---

    ## Profile
    3-5 sentences, LLM-maintained by compaction. Always loaded.

    ## Log
    - 2026-08-12 - Dana asks for the harness draft by the 24th. [g:18f0abc]

Every log line carries its source id in a `[g:...]` anchor. That is what
makes appends idempotent, and it is D8's "every proposal carries its
evidence" made concrete at the storage layer: any claim can be traced back
to the message it came from.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import frontmatter
import yaml

PROFILE_HEADING = "## Profile"
LOG_HEADING = "## Log"

PROFILE_PLACEHOLDER = "_No profile yet - run `personalagi compact`._"

# "- 2026-08-12 - summary text [g:18f0abc]"
LOG_LINE_RE = re.compile(
    r"^-\s+(?P<date>\d{4}-\d{2}-\d{2})\s+[-—]\s+(?P<text>.*?)\s*(?:\[g:(?P<gid>[^\]]+)\])?$"
)

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class LogEntry:
    entry_date: date
    text: str
    gmail_id: str = ""

    def render(self) -> str:
        anchor = f" [g:{self.gmail_id}]" if self.gmail_id else ""
        return f"- {self.entry_date.isoformat()} — {self.text}{anchor}"


@dataclass
class PersonFile:
    slug: str
    name: str
    emails: list[str] = field(default_factory=list)
    relationship: str = "unknown"
    current_threads: list[str] = field(default_factory=list)
    # Human-authored, authoritative, and never overwritten by compaction.
    # This is the answer to "the profile is wrong, how do I fix it so the
    # nightly job does not reintroduce the error" - see ARCHITECTURE.md
    # open question 4.
    corrections: list[str] = field(default_factory=list)
    profile: str = ""
    log: list[LogEntry] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def first_seen(self) -> date | None:
        return min((e.entry_date for e in self.log), default=None)

    @property
    def last_seen(self) -> date | None:
        return max((e.entry_date for e in self.log), default=None)

    def known_gmail_ids(self) -> set[str]:
        return {e.gmail_id for e in self.log if e.gmail_id}

    def add_entry(self, entry: LogEntry) -> bool:
        """Append if new. Returns True when something was actually added.

        Idempotent on the gmail_id anchor, so re-running the builder over the
        same mail does not duplicate history.
        """
        if entry.gmail_id and entry.gmail_id in self.known_gmail_ids():
            return False
        self.log.append(entry)
        return True

    def sorted_log(self) -> list[LogEntry]:
        """Newest first — the order you actually read a history in."""
        return sorted(self.log, key=lambda e: (e.entry_date, e.gmail_id), reverse=True)

    def to_markdown(self) -> str:
        meta = {
            "name": self.name,
            "slug": self.slug,
            "emails": sorted(self.emails),
            "relationship": self.relationship,
            "current_threads": self.current_threads,
            "corrections": self.corrections,
            "message_count": len(self.log),
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "last_updated": date.today().isoformat(),
            **self.extra,
        }
        header = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
        profile = self.profile.strip() or PROFILE_PLACEHOLDER
        lines = [
            "---",
            header,
            "---",
            "",
            PROFILE_HEADING,
            "",
            profile,
            "",
            LOG_HEADING,
            "",
        ]
        lines += [entry.render() for entry in self.sorted_log()]
        return "\n".join(lines).rstrip() + "\n"


def slugify(value: str) -> str:
    """Stable, filesystem-safe identifier for a person."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP_RE.sub("-", ascii_only).strip("-")
    return slug or "unknown"


def slug_for(name: str, email: str) -> str:
    """Prefer the display name; fall back to the address's local part.

    Known limitation (ARCHITECTURE.md open question 2): identity is keyed on
    this slug, so one human with two display names becomes two files. Merging
    is a manual edit today.
    """
    if name and name.strip() and "@" not in name:
        return slugify(name)
    local = (email or "").split("@", 1)[0]
    return slugify(local or "unknown")


def parse_markdown(text: str, slug_hint: str = "") -> PersonFile:
    post = frontmatter.loads(text)
    meta = dict(post.metadata)
    body = post.content

    profile, log = _split_body(body)

    known = {
        "name",
        "slug",
        "emails",
        "relationship",
        "current_threads",
        "corrections",
        "message_count",
        "first_seen",
        "last_seen",
        "last_updated",
    }
    return PersonFile(
        slug=str(meta.get("slug") or slug_hint or slugify(str(meta.get("name", "")))),
        name=str(meta.get("name", "")),
        emails=list(meta.get("emails") or []),
        relationship=str(meta.get("relationship", "unknown")),
        current_threads=list(meta.get("current_threads") or []),
        corrections=list(meta.get("corrections") or []),
        profile=profile,
        log=log,
        extra={k: v for k, v in meta.items() if k not in known},
    )


def _split_body(body: str) -> tuple[str, list[LogEntry]]:
    profile_lines: list[str] = []
    log_entries: list[LogEntry] = []
    section = None

    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.lower().startswith(PROFILE_HEADING.lower()):
            section = "profile"
            continue
        if stripped.lower().startswith(LOG_HEADING.lower()):
            section = "log"
            continue

        if section == "profile":
            profile_lines.append(raw)
        elif section == "log" and stripped:
            match = LOG_LINE_RE.match(stripped)
            if match:
                log_entries.append(
                    LogEntry(
                        entry_date=date.fromisoformat(match.group("date")),
                        text=match.group("text").strip(),
                        gmail_id=(match.group("gid") or "").strip(),
                    )
                )

    profile = "\n".join(profile_lines).strip()
    if profile == PROFILE_PLACEHOLDER:
        profile = ""
    return profile, log_entries


def person_path(context_dir: Path, slug: str) -> Path:
    return context_dir / "people" / f"{slug}.md"


def load_person(context_dir: Path, slug: str) -> PersonFile | None:
    path = person_path(context_dir, slug)
    if not path.exists():
        return None
    return parse_markdown(path.read_text(encoding="utf-8"), slug_hint=slug)


def save_person(context_dir: Path, person: PersonFile) -> Path:
    path = person_path(context_dir, person.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: a crash mid-write must not truncate the source of
    # truth. The DB is rebuildable; these files are not.
    temp = path.with_suffix(".md.tmp")
    temp.write_text(person.to_markdown(), encoding="utf-8")
    temp.replace(path)
    return path


def iter_people(context_dir: Path):
    """Yield every person file, sorted for deterministic output."""
    people_dir = context_dir / "people"
    if not people_dir.exists():
        return
    for path in sorted(people_dir.glob("*.md")):
        yield parse_markdown(path.read_text(encoding="utf-8"), slug_hint=path.stem)


def entry_date_from(timestamp: datetime) -> date:
    return timestamp.date()
