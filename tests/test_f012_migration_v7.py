"""F012：v6→v7 迁移、备份、重复执行与 WAL 恢复。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.auth.migrate import backup_database, migrate_database, restore_backup
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
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
    with pytest.raises(ConversationStorageError):
        migrate_database(db, owner=owner(), backup_dir=backup_dir)

    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT title FROM conversations"
        ).fetchone()[0] == "旧会话"
    finally:
        connection.close()

    backups = list(backup_dir.glob("f012-v6-*/study.sqlite3"))
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


def test_fresh_empty_database_creates_v7(tmp_path: Path) -> None:
    db = tmp_path / "study.sqlite3"
    schema.initialize_database(db)
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
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