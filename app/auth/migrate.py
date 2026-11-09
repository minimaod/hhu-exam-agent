"""v6→v7 备份、迁移与恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.auth.errors import AuthStorageError
from app.conversations import schema
from app.conversations.errors import ConversationStorageError


@dataclass(frozen=True)
class MigrationReport:
    version: int
    already_migrated: bool
    backup_path: Path | None
    conversations: int
    memory_rows: int


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_version(path: Path) -> int:
    if not path.is_file():
        return 0
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def backup_database(db_path: Path, backup_dir: Path) -> Path:
    """Checkpoint WAL 后用 SQLite backup API 生成自包含备份。"""

    source = sqlite3.connect(db_path, timeout=5.0)
    try:
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target_dir = backup_dir / f"f012-v6-{_now_tag()}-{uuid.uuid4().hex[:6]}"
        target_dir.mkdir(parents=True, exist_ok=False)
        target = target_dir / "study.sqlite3"
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE").fetchone()
        finally:
            destination.close()
    finally:
        source.close()

    for suffix in ("-wal", "-shm"):
        stale = Path(str(target) + suffix)
        if stale.exists():
            stale.unlink()

    check = sqlite3.connect(target)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
        version = check.execute("PRAGMA user_version").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        raise AuthStorageError("备份完整性检查失败，迁移已中止。")

    manifest = {
        "source": str(db_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_version": version,
        "size_bytes": target.stat().st_size,
        "sha256": _sha256(target),
    }
    (target_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def _assert_v7(db_path: Path) -> tuple[int, int]:
    connection = schema._open(db_path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != 7 or not schema._is_compatible_v7(connection):
            raise ConversationStorageError("v7 结构校验失败。")
        unowned = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE owner_id IS NULL"
        ).fetchone()[0]
        if unowned:
            raise ConversationStorageError("v7 仍存在无归属会话。")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ConversationStorageError("v7 外键检查失败。")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ConversationStorageError("v7 完整性检查失败。")
        conversations = connection.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()[0]
        memory_rows = connection.execute(
            "SELECT COUNT(*) FROM memory_corrections"
        ).fetchone()[0] + connection.execute(
            "SELECT COUNT(*) FROM memory_exclusions"
        ).fetchone()[0]
        return int(conversations), int(memory_rows)
    finally:
        connection.close()


def migrate_database(
    db_path: Path,
    *,
    owner: schema.SchemaOwner,
    backup_dir: Path,
) -> MigrationReport:
    version = _read_version(db_path)
    if version == 7:
        conversations, memory_rows = _assert_v7(db_path)
        return MigrationReport(7, True, None, conversations, memory_rows)
    if version != 6:
        raise ConversationStorageError(
            "F012 迁移只支持 v6 数据库；请先用原版本升级到 v6。"
        )

    backup_path = backup_database(db_path, backup_dir)
    try:
        schema.initialize_database(db_path, migration_owner=owner)
    except ConversationStorageError as exc:
        raise ConversationStorageError(
            f"迁移失败，原库已回滚保留；备份：{backup_path}；原因：{exc}"
        ) from exc
    try:
        conversations, memory_rows = _assert_v7(db_path)
    except ConversationStorageError as exc:
        raise ConversationStorageError(
            f"迁移已提交但提交后校验失败：{exc}；请从备份恢复：{backup_path}"
        ) from exc
    return MigrationReport(
        7, False, backup_path, conversations, memory_rows
    )


def restore_backup(backup_path: Path, db_path: Path) -> None:
    """恢复备份，并清除目标旁的旧 WAL/SHM，避免旧日志覆盖新库。"""

    if not backup_path.is_file():
        raise ConversationStorageError("备份文件不存在。")
    check = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ConversationStorageError("备份完整性检查失败。")
    finally:
        check.close()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            stale.unlink()

    if db_path.exists():
        pre = db_path.with_name(
            f"{db_path.name}.pre-restore-{_now_tag()}"
        )
        os.replace(db_path, pre)

    temp = db_path.with_name(f"{db_path.name}.restore-tmp")
    if temp.exists():
        temp.unlink()
    temp.write_bytes(backup_path.read_bytes())
    os.replace(temp, db_path)

    for suffix in ("-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            stale.unlink()