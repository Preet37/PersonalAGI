"""Engine, session factory, and schema init.

FTS5 lives here too once search lands; for now this is just the ingest store.
Everything under data/ is derived — deleting it and reindexing is always safe.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from personalagi import models  # noqa: F401  (registers tables on SQLModel.metadata)
from personalagi.config import Settings, get_settings

log = logging.getLogger(__name__)

_engine: Engine | None = None


def _ensure_parent_dir(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    path = Path(database_url[len(prefix) :])
    path.parent.mkdir(parents=True, exist_ok=True)


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _ensure_parent_dir(settings.database_url)
        _engine = create_engine(settings.database_url, echo=False)
        _apply_pragmas(_engine)
    return _engine


def _apply_pragmas(engine: Engine) -> None:
    """WAL so a long ingest does not block reads.

    Without it, a backfill holding a write transaction locks out every reader,
    and running `status` mid-ingest fails with 'database is locked'. WAL is
    also more crash-resilient, which matters because the cursor discipline
    assumes a commit either lands or does not.
    """
    if not engine.url.get_backend_name().startswith("sqlite"):
        return
    if engine.url.database in (None, ":memory:"):
        return  # WAL is meaningless for in-memory DBs
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("PRAGMA busy_timeout=10000")
        conn.exec_driver_sql("PRAGMA synchronous=NORMAL")


def _add_missing_columns(engine: Engine) -> list[str]:
    """Additive-only migration for tables that already exist.

    create_all() creates missing tables but never alters existing ones, so a
    DB created before a column was added silently lacks it. Only ever ADDs;
    nothing here drops or rewrites data. If a change ever needs more than
    this, delete data/ and reindex — it is derived (ARCHITECTURE.md D2).
    """
    added: list[str] = []
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            exists = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=:n",
                {"n": table.name},
            ).fetchone()
            if not exists:
                continue
            present = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")
            }
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = column.type.compile(engine.dialect)
                default = "0" if "BOOLEAN" in ddl.upper() or "INTEGER" in ddl.upper() else "NULL"
                if column.nullable:
                    default = "NULL"
                conn.exec_driver_sql(
                    f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl} "
                    f"DEFAULT {default}"
                )
                added.append(f"{table.name}.{column.name}")
    return added


# Stage 8: the three derived tables keyed on message_id now key on event_id.
# Event ids are assigned equal to the Message ids they derive from, so this is
# a pure rename — every existing foreign key stays valid and no row is
# rewritten. SQLite updates dependent indexes as part of RENAME COLUMN.
_COLUMN_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("classification", "message_id", "event_id"),
    ("relevance", "message_id", "event_id"),
    ("commitment", "message_id", "event_id"),
)


def _rename_columns(engine: Engine) -> list[str]:
    """Apply renames to tables that predate them. Idempotent."""
    renamed: list[str] = []
    with engine.begin() as conn:
        for table, old, new in _COLUMN_RENAMES:
            exists = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=:n",
                {"n": table},
            ).fetchone()
            if not exists:
                continue
            columns = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            # Only when the old name is present and the new one is not: running
            # this against an already-migrated table must be a no-op, not an
            # error, because init_db runs on every single command.
            if old in columns and new not in columns:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"
                )
                renamed.append(f"{table}.{old}->{new}")
    if renamed:
        log.info("migrated columns: %s", ", ".join(renamed))
    return renamed


# Columns whose Python default must be written into EXISTING rows, not just
# applied to new ones.
#
# _add_missing_columns adds nullable columns with DEFAULT NULL, which is right
# when "unset" is meaningful (headers_json: unknown != no headers). It is
# wrong for provenance: every row that existed before the column did came from
# a real mailbox or a real phone, and leaving them NULL makes citable_events()
# exclude the entire corpus -- so nothing is citable and every claim silently
# loses its evidence. Caught by checking the count after migrating, not by a
# test, which is the same lesson as the gitignore bug.
_BACKFILL_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("event", "provenance", "external"),
)


def _backfill_defaults(engine: Engine) -> list[str]:
    """Fill NULLs left by an additive migration. Idempotent."""
    filled: list[str] = []
    with engine.begin() as conn:
        for table, column, value in _BACKFILL_DEFAULTS:
            exists = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=:n",
                {"n": table},
            ).fetchone()
            if not exists:
                continue
            columns = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            if column not in columns:
                continue
            result = conn.exec_driver_sql(
                f"UPDATE {table} SET {column} = :v WHERE {column} IS NULL",
                {"v": value},
            )
            if result.rowcount:
                filled.append(f"{table}.{column}={value} x{result.rowcount}")
    if filled:
        log.info("backfilled defaults: %s", ", ".join(filled))
    return filled


def init_db(settings: Settings | None = None) -> Engine:
    engine = get_engine(settings)
    # Rename BEFORE create_all: otherwise create_all sees a table missing
    # event_id, _add_missing_columns adds an empty one, and the rename then
    # finds both names present and silently does nothing — leaving every
    # foreign key NULL.
    _rename_columns(engine)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    # After the columns exist, never before.
    _backfill_defaults(engine)
    return engine


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    engine = get_engine(settings)
    with Session(engine) as session:
        yield session
