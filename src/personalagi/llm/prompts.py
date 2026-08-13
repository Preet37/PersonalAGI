"""Load prompts from prompts/*.md so they can be iterated without code edits.

Format is two markdown H1 sections, `# System` and `# User`. The user section
is a str.format template filled from the record being classified.

Each load carries a short content hash. Eval results record it, so a metric
is always attributable to the exact prompt that produced it — otherwise you
cannot tell an improved prompt from a re-run.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"

_SECTION_RE = re.compile(r"^#\s+(System|User)\s*$", re.IGNORECASE | re.MULTILINE)


class PromptError(RuntimeError):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    system: str
    user_template: str
    version: str  # short content hash

    def render_user(self, **fields: object) -> str:
        try:
            return self.user_template.format(**fields)
        except KeyError as exc:
            raise PromptError(
                f"prompt '{self.name}' references {exc} but it was not supplied"
            ) from exc


def _split_sections(text: str, name: str) -> tuple[str, str]:
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        raise PromptError(f"prompt '{name}' has no '# System' / '# User' sections")

    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[match.group(1).lower()] = text[start:end].strip()

    missing = {"system", "user"} - sections.keys()
    if missing:
        raise PromptError(f"prompt '{name}' is missing section(s): {', '.join(sorted(missing))}")
    return sections["system"], sections["user"]


@lru_cache(maxsize=16)
def load_prompt(name: str, prompts_dir: Path | None = None) -> Prompt:
    directory = prompts_dir or PROMPTS_DIR
    path = directory / f"{name}.md"
    if not path.exists():
        raise PromptError(f"no prompt at {path}")

    text = path.read_text(encoding="utf-8")
    system, user_template = _split_sections(text, name)
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return Prompt(name=name, system=system, user_template=user_template, version=version)
