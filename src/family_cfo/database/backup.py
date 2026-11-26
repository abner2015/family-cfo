"""Backup and restore with verification.

Uses the sqlite3 online backup API so a WAL database is copied consistently,
then verifies the copy with ``PRAGMA integrity_check`` and table counts.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..core.errors import BackupError
from ..core.ids import file_sha256
from ..core.logging import get_logger

log = get_logger("db.backup")

KEY_TABLES = (
    "households",
    "transactions",
    "accounts",
    "assets",
    "liabilities",
    "budgets",
    "financial_goals",
)


@dataclass
class BackupInfo:
    path: Path
    size: int
    sha256: str
    created_at: str
    table_counts: dict[str, int]
    integrity: str


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in KEY_TABLES:
        try:
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:
            counts[table] = -1
    return counts


def verify_backup(path: str | Path) -> BackupInfo:
    path = Path(path)
    if not path.exists():
        raise BackupError(f"备份文件不存在: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BackupError(f"无法打开备份: {path}", detail=str(exc)) from exc
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        counts = _counts(conn)
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"备份文件不是有效的 SQLite 数据库: {path}", detail=str(exc)) from exc
    finally:
        conn.close()
    if integrity != "ok":
        raise BackupError(f"备份完整性校验失败: {path}", detail=str(integrity))
    if fk:
        raise BackupError(f"备份外键校验失败: {path}", detail=str(fk[:5]))
    return BackupInfo(
        path=path,
        size=path.stat().st_size,
        sha256=file_sha256(path),
        created_at=datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        table_counts=counts,
        integrity=integrity,
    )


def backup_database(db_path: str | Path, dest_dir: str | Path, *, label: str | None = None) -> BackupInfo:
    db_path = Path(db_path)
    if not db_path.exists():
        raise BackupError(f"数据库不存在: {db_path}")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{label}" if label else ""
    dest = dest_dir / f"family_cfo-{stamp}{suffix}.db"
    counter = 1
    while dest.exists():
        counter += 1
        dest = dest_dir / f"family_cfo-{stamp}{suffix}-{counter}.db"
    try:
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except sqlite3.Error as exc:
        raise BackupError(f"备份失败: {db_path}", detail=str(exc)) from exc
    info = verify_backup(dest)
    manifest = {
        "source": str(db_path),
        "backup": dest.name,
        "sha256": info.sha256,
        "size": info.size,
        "created_at": info.created_at,
        "table_counts": info.table_counts,
        "integrity": info.integrity,
    }
    dest.with_suffix(".json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("backup created: %s (%s bytes)", dest.name, info.size)
    return info


def restore_database(
    backup_path: str | Path,
    db_path: str | Path,
    *,
    backup_dir: str | Path | None = None,
    safety_backup: bool = True,
    engine=None,
) -> dict:
    """Restore ``backup_path`` over ``db_path`` after verifying both.

    A safety backup of the current ledger is taken first unless disabled.
    Pass ``engine`` so open connections are disposed before the swap.
    """
    backup_path = Path(backup_path)
    db_path = Path(db_path)
    info = verify_backup(backup_path)
    safety: Path | None = None
    if db_path.exists() and safety_backup:
        target_dir = Path(backup_dir) if backup_dir else db_path.parent / "backups"
        safety = backup_database(db_path, target_dir, label="pre-restore").path
    if engine is not None:
        engine.dispose()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    try:
        src = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
        dst = sqlite3.connect(str(db_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except sqlite3.Error as exc:
        raise BackupError(f"恢复失败: {backup_path} -> {db_path}", detail=str(exc)) from exc
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    restored = verify_backup(db_path)
    return {
        "restored_from": str(backup_path),
        "database": str(db_path),
        "safety_backup": str(safety) if safety else None,
        "sha256": restored.sha256,
        "table_counts": restored.table_counts,
        "backup_created_at": info.created_at,
    }


def prune_backups(dest_dir: str | Path, keep: int = 20) -> list[Path]:
    dest_dir = Path(dest_dir)
    if keep < 1 or not dest_dir.exists():
        return []
    files = sorted(
        [p for p in dest_dir.glob("family_cfo-*.db") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for path in files[keep:]:
        manifest = path.with_suffix(".json")
        path.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        removed.append(path)
    return removed
