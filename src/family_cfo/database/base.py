"""Engine / session management for the SQLite ledger."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from ..core.errors import ConfigError
from ..core.logging import get_logger
from .models import Base

log = get_logger("db")


def _apply_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - driver callback
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def create_db_engine(db_path: str | Path, *, echo: bool = False) -> Engine:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{path}"
    engine = create_engine(
        url,
        echo=echo,
        future=True,
        connect_args={"timeout": 30},
        # CLI/Skill processes are short lived; no connections may linger and
        # block backup/restore file swaps.
        poolclass=NullPool,
    )
    event.listen(engine, "connect", _apply_pragmas)
    return engine


@dataclass
class Database:
    engine: Engine
    session_factory: sessionmaker[Session]

    @classmethod
    def open(cls, db_path: str | Path, *, echo: bool = False) -> "Database":
        engine = create_db_engine(db_path, echo=echo)
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        return cls(engine=engine, session_factory=factory)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))

    def has_table(self, name: str) -> bool:
        from sqlalchemy import inspect

        return name in inspect(self.engine).get_table_names()

    def close(self) -> None:
        self.engine.dispose()


def open_database(db_path: str | Path) -> Database:
    if not Path(db_path).exists():
        raise ConfigError(
            f"数据库不存在: {db_path}。请先运行 `family-cfo init`。",
        )
    return Database.open(db_path)
