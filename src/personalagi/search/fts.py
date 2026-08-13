"""FTS5 index over person-file log lines.

Strictly derived (ARCHITECTURE.md D2): the index is built by parsing
context/people/*.md, never by writing to them. `reindex` drops and rebuilds
from the files, so the recovery story is always "delete data/, reindex".

FTS5 is a virtual table, which SQLModel cannot express, so the DDL is raw SQL.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, text

from personalagi.context.people import iter_people

log = logging.getLogger(__name__)

TABLE = "log_fts"

CREATE_SQL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5(
    body,
    person_slug UNINDEXED,
    person_name UNINDEXED,
    entry_date  UNINDEXED,
    source_id    UNINDEXED,
    tokenize = "porter unicode61"
)
"""

# FTS5 treats these as query syntax; a raw user string containing them is a
# syntax error, not a search. Quoting each term is the safe default.
_TERM_RE = re.compile(r"[\w']+", re.UNICODE)


@dataclass(frozen=True)
class SearchHit:
    person_slug: str
    person_name: str
    entry_date: str
    body: str
    source_id: str
    rank: float

    def render(self) -> str:
        anchor = f" [g:{self.source_id}]" if self.source_id else ""
        return f"- {self.entry_date} — {self.body}{anchor}"


# Bumped whenever the FTS column list changes. `CREATE VIRTUAL TABLE IF NOT
# EXISTS` silently keeps an old table with the old columns, so a rename lands
# as "no such column" at INSERT time — long after the change looked fine.
FTS_COLUMNS = ("body", "person_slug", "person_name", "entry_date", "source_id")


def ensure_fts(engine: Engine) -> None:
    """Create the index, rebuilding it if its columns are out of date.

    Dropping is safe by construction: the index is derived from the markdown
    vault (D2), so the recovery story is always "delete it and reindex". The
    caller is expected to reindex after a rebuild — `reindex` does both.
    """
    with engine.begin() as conn:
        existing = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=:n",
            {"n": TABLE},
        ).fetchone()
        if existing and any(column not in existing[0] for column in FTS_COLUMNS):
            log.info("%s has stale columns; rebuilding the index", TABLE)
            conn.exec_driver_sql(f"DROP TABLE {TABLE}")
        conn.execute(text(CREATE_SQL))


def build_match_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Every term is double-quoted so characters FTS5 reads as operators (-, *,
    ^, :, quotes, parentheses) cannot turn a search into a syntax error or an
    unintended operator. Terms are OR-ed, then ranked, which beats AND for
    recall on short log lines.
    """
    terms = _TERM_RE.findall(query or "")
    if not terms:
        return ""
    return " OR ".join(f'"{term}"' for term in terms)


def reindex(engine: Engine, context_dir: Path) -> int:
    """Rebuild the whole index from the markdown vault. Returns row count."""
    ensure_fts(engine)
    rows = 0
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {TABLE}"))
        for person in iter_people(context_dir):
            for entry in person.log:
                conn.execute(
                    text(
                        f"INSERT INTO {TABLE} "
                        "(body, person_slug, person_name, entry_date, source_id) "
                        "VALUES (:body, :slug, :name, :date, :gid)"
                    ),
                    {
                        "body": entry.text,
                        "slug": person.slug,
                        "name": person.name,
                        "date": entry.entry_date.isoformat(),
                        "gid": entry.source_id,
                    },
                )
                rows += 1
    log.info("indexed %d log line(s) from %s", rows, context_dir)
    return rows


def search(
    engine: Engine,
    query: str,
    *,
    person_slug: str | None = None,
    limit: int = 5,
) -> list[SearchHit]:
    """Rank log lines by FTS relevance, optionally scoped to one person."""
    match = build_match_query(query)
    if not match:
        return []

    ensure_fts(engine)
    sql = (
        f"SELECT body, person_slug, person_name, entry_date, source_id, rank "
        f"FROM {TABLE} WHERE {TABLE} MATCH :match"
    )
    params: dict[str, object] = {"match": match, "limit": limit}
    if person_slug:
        sql += " AND person_slug = :slug"
        params["slug"] = person_slug
    sql += " ORDER BY rank LIMIT :limit"

    with engine.connect() as conn:
        result = conn.execute(text(sql), params)
        return [
            SearchHit(
                body=row[0],
                person_slug=row[1],
                person_name=row[2],
                entry_date=row[3],
                source_id=row[4],
                rank=float(row[5]),
            )
            for row in result
        ]
