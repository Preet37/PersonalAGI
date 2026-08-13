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
    return _engine


def init_db(settings: Settings | None = None) -> Engine:
    engine = get_engine(settings)
    SQLModel.metadata.create_all(engine)
    return engine


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    engine = get_engine(settings)
    with Session(engine) as session:
        yield session
