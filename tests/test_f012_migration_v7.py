"""F012/F013：v6→v8 迁移、备份、重复执行与 WAL 恢复。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.auth.migrate import (
    MigrationCommittedCheckError,
    MigrationRolledBackError,
    backup_database,
    migrate_database,
    restore_backup,
)
from app.conversations import schema
from app.conversations.errors import ConversationStorageError


def build_v6(path: Path, *, wal: bool = False) -> None:
    connection = sqlite3.connect(path)
    try:
        if wal:
            connection.execute("PRAGMA journal_mode=WAL").fetchone()
        connection.execute("BEGIN IMMEDIATE")
        for statement in schema._CREATE_V6_STATEMENTS:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=6")
        connection.execute(
            "INSERT INTO conversations VALUES ('conv-1','旧会话','2026-01-01','2026-01-01')"
        )
        connection.execute(
            "INSERT INTO memory_corrections VALUES "
            "('exercise:old',NULL,NULL,'旧备注','2026-01-01','2026-01-01')"
        )
        connection.commit()
    finally:
        connection.close()


def owner() -> schema.SchemaOwner:
    return schema.SchemaOwner(
        id="admin-1",
        username="admin",
        username_normalized="admin",
        password_hash="hash",
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_v6_copy_migrates_to_admin(tmp_path: Path) -> None:
    db = tmp_path / "study.sqlite3"
    build_v6(db)
    report = migrate_database(
        db, owner=owner(), backup_dir=tmp_path / "backups"
    )
    assert not report.already_migrated
    assert report.backup_path is not None and report.backup_path.is_file()
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert connection.execute(
            "SELECT owner_id FROM conversations"
        ).fetchone()[0] == "admin-1"
        assert connection.execute(
            "SELECT owner_id, entry_key FROM memory_corrections"
        ).fetchone() == ("admin-1", "exercise:old")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    finally:
        connection.close()


def test_v7_repeat_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "study.sqlite3"
    build_v6(db)
    backup_dir = tmp_path / "backups"
    first = migrate_database(db, owner=owner(), backup_dir=backup_dir)
    second = migrate_database(db, owner=owner(), backup_dir=backup_dir)
    assert first.already_migrated is False
    assert second.already_migrated is True
    assert second.backup_path is None
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    finally:
        connection.close()


def test_migration_failure_rolls_back_and_backup_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "study.sqlite3"
    build_v6(db)
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(
        schema, "_v7_migration_statements", lambda: ("CREATE TABLE broken (",)
    )
    with pytest.raises(MigrationRolledBackError):
        migrate_database(db, owner=owner(), backup_dir=backup_dir)

    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT title FROM conversations"
        ).fetchone()[0] == "旧会话"
    finally:
        connection.close()

    backups = list(backup_dir.glob("study-v6-*/study.sqlite3"))
    assert len(backups) == 1
    restored = tmp_path / "restored.sqlite3"
    restore_backup(backups[0], restored)
    connection = sqlite3.connect(restored)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT title FROM conversations"
        ).fetchone()[0] == "旧会话"
    finally:
        connection.close()


def test_restore_drops_stale_wal(tmp_path: Path) -> None:
    db = tmp_path / "study.sqlite3"
    build_v6(db, wal=True)
    backup = backup_database(db, tmp_path / "backups")

    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA journal_mode=WAL").fetchone()
        connection.execute(
            "INSERT INTO conversations VALUES ('conv-2','WAL 新行','2026-01-02','2026-01-02')"
        )
        connection.commit()
    finally:
        connection.close()

    restore_backup(backup, db)
    connection = sqlite3.connect(db)
    try:
        ids = {row[0] for row in connection.execute("SELECT id FROM conversations")}
        assert ids == {"conv-1"}
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        assert not Path(str(db) + suffix).exists() or Path(
            str(db) + suffix
        ).stat().st_size == 0


def test_old_database_startup_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "study.sqlite3"
    build_v6(db)
    with pytest.raises(ConversationStorageError):
        schema.initialize_database(db)
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        connection.close()


def test_fresh_empty_database_creates_v8(tmp_path: Path) -> None:
    db = tmp_path / "study.sqlite3"
    schema.initialize_database(db)
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert connection.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_startup_refuses_old_database_and_keeps_public_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    import app.conversations.repository as conversation_repository
    from app.main import app

    db = tmp_path / "study.sqlite3"
    build_v6(db)
    monkeypatch.setattr(conversation_repository, "DEFAULT_DB_PATH", db)
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/papers").status_code == 200
        assert client.get("/api/conversations").status_code == 503
        assert client.post(
            "/api/auth/login",
            json={"username": "x", "password": "yyyyyyyy"},
            headers={"X-HHU-CSRF": "1"},
        ).status_code == 503
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        connection.close()


def test_restore_write_failure_keeps_current_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "study.sqlite3"
    build_v6(db)
    backup = backup_database(db, tmp_path / "backups")
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO conversations VALUES ('current-row','当前行','2026-01-03','2026-01-03')"
        )
        connection.commit()
    finally:
        connection.close()

    real_replace = __import__("os").replace

    def fail_temp_replace(src, dst):
        if str(src).endswith(".restore-tmp"):
            raise OSError("injected restore write failure")
        return real_replace(src, dst)

    monkeypatch.setattr("app.auth.migrate.os.replace", fail_temp_replace)
    with pytest.raises(OSError):
        restore_backup(backup, db)

    assert db.is_file()
    connection = sqlite3.connect(db)
    try:
        ids = {row[0] for row in connection.execute("SELECT id FROM conversations")}
        assert "current-row" in ids
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        connection.close()


def test_restore_checkpoints_current_wal_before_replacing(
    tmp_path: Path,
) -> None:
    import shutil

    db = tmp_path / "study.sqlite3"
    build_v6(db, wal=True)
    backup = backup_database(db, tmp_path / "backups")

    # 写入一行的已提交页留在 WAL；在写入连接关闭前把主文件和 WAL 一起
    # 复制成“当前库”，得到确实存在未合并 WAL 的恢复目标。
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL").fetchone()
        writer.execute("PRAGMA wal_autocheckpoint=0").fetchone()
        writer.execute(
            "INSERT INTO conversations VALUES ('wal-current','WAL 当前行','2026-01-04','2026-01-04')"
        )
        writer.commit()
        current = tmp_path / "current.sqlite3"
        shutil.copyfile(db, current)
        shutil.copyfile(Path(str(db) + "-wal"), Path(str(current) + "-wal"))
    finally:
        writer.close()
    assert Path(str(current) + "-wal").stat().st_size > 0

    restore_backup(backup, current)
    connection = sqlite3.connect(current)
    try:
        ids = {row[0] for row in connection.execute("SELECT id FROM conversations")}
        assert ids == {"conv-1"}
    finally:
        connection.close()

    pre_files = list(tmp_path.glob("current.sqlite3.pre-restore-*"))
    candidates = [p for p in pre_files if not p.name.endswith(("-wal", "-shm"))]
    assert candidates
    connection = sqlite3.connect(candidates[0])
    try:
        ids = {row[0] for row in connection.execute("SELECT id FROM conversations")}
        assert "wal-current" in ids
    finally:
        connection.close()


def test_restore_first_move_failure_keeps_current_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "study.sqlite3"
    build_v6(db)
    backup = backup_database(db, tmp_path / "backups")
    real_replace = __import__("os").replace

    def fail_first_move(src, dst):
        if Path(src) == db and ".pre-restore-" in str(dst):
            raise OSError("injected first move failure")
        return real_replace(src, dst)

    monkeypatch.setattr("app.auth.migrate.os.replace", fail_first_move)
    with pytest.raises(ConversationStorageError):
        restore_backup(backup, db)

    # 首步移动失败时，异常处理不能再次移动当前库。
    assert db.is_file()
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT title FROM conversations"
        ).fetchone()[0] == "旧会话"
    finally:
        connection.close()


def test_restore_stops_when_checkpoint_is_busy(
    tmp_path: Path,
) -> None:
    db = tmp_path / "study.sqlite3"
    build_v6(db, wal=True)
    backup = backup_database(db, tmp_path / "backups")
    reader = sqlite3.connect(db)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM conversations").fetchone()
    try:
        with pytest.raises(ConversationStorageError):
            restore_backup(backup, db)
    finally:
        reader.rollback()
        reader.close()
    assert db.is_file()
    connection = sqlite3.connect(db)
    try:
        assert connection.execute(
            "SELECT title FROM conversations"
        ).fetchone()[0] == "旧会话"
    finally:
        connection.close()


def test_restore_stops_when_checkpoint_returns_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "study.sqlite3"
    build_v6(db)
    backup = backup_database(db, tmp_path / "backups")
    real_connect = sqlite3.connect

    class BusyConnection:
        def execute(self, sql, *args):
            if "wal_checkpoint" in sql:
                class Cursor:
                    def fetchone(self):
                        return (1, 0, 0)

                return Cursor()
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self):
            return None

    def connect(path, *args, **kwargs):
        if str(path) == str(db):
            return BusyConnection()
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr("app.auth.migrate.sqlite3.connect", connect)
    with pytest.raises(ConversationStorageError):
        restore_backup(backup, db)
    assert db.is_file()


def test_post_commit_startup_failure_reports_v8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "study.sqlite3"
    build_v6(db)

    def fail_recover(connection):
        raise ConversationStorageError("注入轮次恢复失败")

    monkeypatch.setattr(schema, "_recover_running", fail_recover)
    with pytest.raises(MigrationCommittedCheckError):
        migrate_database(db, owner=owner(), backup_dir=tmp_path / "backups")
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
    finally:
        connection.close()


def test_post_commit_check_reports_committed_v8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "study.sqlite3"
    build_v6(db)
    monkeypatch.setattr(
        "app.auth.migrate._assert_v8",
        lambda path: (_ for _ in ()).throw(ConversationStorageError("注入检查失败")),
    )
    with pytest.raises(MigrationCommittedCheckError):
        migrate_database(db, owner=owner(), backup_dir=tmp_path / "backups")
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
    finally:
        connection.close()