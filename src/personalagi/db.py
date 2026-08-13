"""Engine, session factory, and schema init.

FTS5 lives here too once search lands; for now this is just the ingest store.
Everything under data/ is derived — deleting it and reindexing is always safe.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from personalagi import models  # noqa: F401  (registers tables on SQLModel.metadata)
from personalagi.config import Settings, get_settings

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


def init_db(settings: Settings | None = None) -> Engine:
    engine = get_engine(settings)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    engine = get_engine(settings)
    with Session(engine) as session:
        yield session
