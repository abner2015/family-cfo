"""Alembic integration.

The v0.1 initial migration materialises the SQLAlchemy metadata (single source
of truth) and stamps the revision.  Later schema changes should be added as
ordinary, explicit Alembic revisions on top of this one.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from ..core.logging import get_logger

log = get_logger("db.migrate")

MIGRATIONS_DIR_NAME = "migrations"


def _project_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "migrations").is_dir():
            return candidate
    return start


def _migrations_dir(start: Path | None = None) -> Path | None:
    """Locate migrations in a wheel install first, then in a source checkout."""
    package_root = Path(__file__).resolve().parents[1]
    candidates = [package_root / MIGRATIONS_DIR_NAME]
    candidates.append(_project_root(start or Path(__file__).resolve()) / MIGRATIONS_DIR_NAME)
    candidates.append(_project_root(Path.cwd()) / MIGRATIONS_DIR_NAME)
    for candidate in candidates:
        if (candidate / "env.py").exists():
            return candidate
    return None


def alembic_config(db_path: str | Path, *, project_root: Path | None = None) -> Config:
    migrations = _migrations_dir(project_root)
    if migrations is None:
        raise RuntimeError(
            "找不到 Alembic migrations 目录；请使用源码仓库或重新安装包含 migrations 的 wheel。"
        )
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{Path(db_path)}")
    cfg.set_main_option("prepend_sys_path", "src")
    cfg.set_main_option("path_separator", "os")
    return cfg


def run_migrations(db) -> None:
    """Upgrade the database to head and ensure the revision is stamped.

    ``db`` is a :class:`family_cfo.database.base.Database`; its engine URL is
    reused so the caller's PRAGMAs/connections stay consistent.
    """
    db_path = db.engine.url.database
    if not db_path:
        raise RuntimeError("SQLite engine without a database file")
    try:
        cfg = alembic_config(db_path)
    except RuntimeError as exc:  # pragma: no cover - wheel without migrations
        log.debug("migrations unavailable (%s); using create_all", exc)
        db.create_all()
        return
    try:
        command.upgrade(cfg, "head")
    except Exception as exc:  # pragma: no cover - defensive fallback
        log.warning("Alembic upgrade failed (%s); falling back to create_all", exc)
        db.create_all()
    # The initial revision materialises metadata itself; some SQLite/Alembic
    # combinations do not persist the version row inside the same DDL
    # transaction, so stamp the head explicitly with a plain write.
    try:
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(cfg).get_current_head()
        if head:
            with db.engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS alembic_version ("
                    "version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
                conn.exec_driver_sql("DELETE FROM alembic_version")
                conn.exec_driver_sql(
                    "INSERT INTO alembic_version (version_num) VALUES (?)", (head,)
                )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Alembic stamp failed: %s", exc)


def stamp_head(db) -> None:
    db_path = db.engine.url.database
    if not db_path:
        raise RuntimeError("SQLite engine without a database file")
    command.stamp(alembic_config(db_path), "head")
