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
        checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise ConversationStorageError(
                "当前数据库仍在使用，WAL 无法合并；请停止服务后再备份。"
            )
        target_dir = backup_dir / f"study-v{_read_version(db_path)}-{_now_tag()}-{uuid.uuid4().hex[:6]}"
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


def _assert_v8(db_path: Path) -> tuple[int, int]:
    connection = schema._open(db_path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != 8 or not schema._is_compatible_v8(connection):
            raise ConversationStorageError("v8 结构校验失败。")
        unowned = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE owner_id IS NULL"
        ).fetchone()[0]
        if unowned:
            raise ConversationStorageError("v8 仍存在无归属会话。")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ConversationStorageError("v8 外键检查失败。")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ConversationStorageError("v8 完整性检查失败。")
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


def _assert_v9(db_path: Path) -> tuple[int, int]:
    connection = schema._open(db_path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != 9 or not schema._is_compatible_v9(connection):
            raise ConversationStorageError("v9 结构校验失败。")
        unowned = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE owner_id IS NULL"
        ).fetchone()[0]
        if unowned:
            raise ConversationStorageError("v9 仍存在无归属会话。")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ConversationStorageError("v9 外键检查失败。")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ConversationStorageError("v9 完整性检查失败。")
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


def _assert_v11(db_path: Path) -> tuple[int, int]:
    connection = schema._open(db_path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != 11 or not schema._is_compatible_v11(connection):
            raise ConversationStorageError("v11 结构校验失败。")
        unowned = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE owner_id IS NULL"
        ).fetchone()[0]
        if unowned:
            raise ConversationStorageError("v11 仍存在无归属会话。")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ConversationStorageError("v11 外键检查失败。")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ConversationStorageError("v11 完整性检查失败。")
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


def _assert_v12(db_path: Path) -> tuple[int, int]:
    connection = schema._open(db_path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != 12 or not schema._is_compatible_v12(connection):
            raise ConversationStorageError("v12 结构校验失败。")
        unowned = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE owner_id IS NULL"
        ).fetchone()[0]
        if unowned:
            raise ConversationStorageError("v12 仍存在无归属会话。")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ConversationStorageError("v12 外键检查失败。")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ConversationStorageError("v12 完整性检查失败。")
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
    owner: schema.SchemaOwner | None = None,
    backup_dir: Path,
) -> MigrationReport:
    """把 v6/v7/v8/v9/v10/v11 迁移到 v12；v7–v11→v12 不需要管理员账号。"""

    version = _read_version(db_path)
    if version == 12:
        conversations, memory_rows = _assert_v12(db_path)
        return MigrationReport(12, True, None, conversations, memory_rows)
    if version not in (6, 7, 8, 9, 10, 11):
        raise ConversationStorageError(
            "迁移只支持 v6、v7、v8、v9、v10 或 v11 数据库；请先升级到 v6。"
        )
    if version == 6 and owner is None:
        raise ConversationStorageError("v6 升级需要管理员账号。")

    backup_path = backup_database(db_path, backup_dir)
    try:
        schema.initialize_database(db_path, migration_owner=owner)
    except ConversationStorageError as exc:
        actual_version = _read_version(db_path)
        if actual_version == 12:
            raise MigrationCommittedCheckError(
                "迁移事务已提交为 v12，但提交后的启动处理失败；"
                f"请检查数据库后再决定是否从备份恢复：{backup_path}；原因：{exc}"
            ) from exc
        raise MigrationRolledBackError(
            f"迁移事务已回滚，数据库仍为 v{actual_version}；备份：{backup_path}；原因：{exc}"
        ) from exc
    try:
        conversations, memory_rows = _assert_v12(db_path)
    except ConversationStorageError as exc:
        raise MigrationCommittedCheckError(
            f"迁移已提交为 v12，但提交后校验失败：{exc}；请从备份恢复：{backup_path}"
        ) from exc
    return MigrationReport(
        12, False, backup_path, conversations, memory_rows
    )


class MigrationRolledBackError(ConversationStorageError):
    """迁移事务失败，原库仍为 v6。"""


class MigrationCommittedCheckError(ConversationStorageError):
    """事务已提交为 v7，但提交后校验失败。"""


def restore_backup(backup_path: Path, db_path: Path) -> None:
    """安全恢复：先复制并校验待恢复副本，再替换；失败恢复原库。"""

    if not backup_path.is_file():
        raise ConversationStorageError("备份文件不存在。")

    def check(path: Path) -> None:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ConversationStorageError("数据库完整性检查失败。")
        finally:
            connection.close()

    check(backup_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temp = db_path.with_name(f"{db_path.name}.restore-tmp")
    if temp.exists():
        temp.unlink()
    try:
        temp.write_bytes(backup_path.read_bytes())
        check(temp)
    except Exception:
        if temp.exists():
            temp.unlink()
        raise

    pre: Path | None = None
    pre_wal: Path | None = None
    pre_shm: Path | None = None
    moved_current = False
    moved_wal = False
    moved_shm = False
    installed = False
    try:
        if db_path.exists():
            # 先把当前库 WAL 中已提交的数据合并进主文件；失败则不动目标。
            current = sqlite3.connect(db_path, timeout=5.0)
            try:
                checkpoint = current.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
                if checkpoint is not None and int(checkpoint[0]) != 0:
                    raise ConversationStorageError(
                        "当前数据库仍在使用，WAL 无法合并；请停止服务后再恢复。"
                    )
            finally:
                current.close()
            pre = db_path.with_name(f"{db_path.name}.pre-restore-{_now_tag()}")
            try:
                os.replace(db_path, pre)
            except OSError as exc:
                raise ConversationStorageError(
                    "当前数据库仍被占用，无法替换；请停止服务后再恢复。"
                ) from exc
            moved_current = True
            for suffix, target in (("-wal", "wal"), ("-shm", "shm")):
                source = Path(str(db_path) + suffix)
                if source.exists():
                    moved = Path(str(pre) + suffix)
                    os.replace(source, moved)
                    if target == "wal":
                        pre_wal = moved
                        moved_wal = True
                    else:
                        pre_shm = moved
                        moved_shm = True
        os.replace(temp, db_path)
        installed = True
        check(db_path)
    except Exception:
        if temp.exists():
            temp.unlink()
        # 只撤销已经成功的移动；首步失败时绝不再次移动当前库。
        if moved_current:
            if installed and db_path.exists():
                failed = db_path.with_name(
                    f"{db_path.name}.failed-restore-{_now_tag()}"
                )
                os.replace(db_path, failed)
            os.replace(pre, db_path)  # type: ignore[arg-type]
            if moved_wal and pre_wal is not None and pre_wal.exists():
                os.replace(pre_wal, Path(str(db_path) + "-wal"))
            if moved_shm and pre_shm is not None and pre_shm.exists():
                os.replace(pre_shm, Path(str(db_path) + "-shm"))
        elif installed and db_path.exists():
            failed = db_path.with_name(
                f"{db_path.name}.failed-restore-{_now_tag()}"
            )
            os.replace(db_path, failed)
        raise

    # 新库旁不应残留旧日志；恢复前的库保留在 pre-restore 路径。
    for suffix in ("-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            stale.unlink()