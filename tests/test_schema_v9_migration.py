"""v8 → v9 数据库结构与迁移兼容测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.conversations import schema
from app.conversations.errors import ConversationStorageError


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_v8_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        for stmt in schema._CREATE_V8_STATEMENTS:
            conn.execute(stmt)
        conn.execute("PRAGMA user_version = 8")
        conn.execute(
            "INSERT INTO users (id, username, username_normalized, password_hash, created_at) "
            "VALUES ('u1', 'alice', 'alice', 'hash', '2026-11-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, owner_id) "
            "VALUES ('c1', '高数复习', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z', 'u1')"
        )
        conn.commit()
    finally:
        conn.close()


def _create_v7_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        for stmt in schema._CREATE_V7_STATEMENTS:
            conn.execute(stmt)
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
    finally:
        conn.close()


def _create_v6_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        for stmt in schema._CREATE_V6_STATEMENTS:
            conn.execute(stmt)
        conn.execute("PRAGMA user_version = 6")
        conn.commit()
    finally:
        conn.close()


def test_empty_db_creates_v9(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.sqlite3"
    schema.initialize_database(db_path)
    conn = _connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == schema.SCHEMA_VERSION
        assert schema._is_compatible_v12(conn)
    finally:
        conn.close()


def test_v8_migrates_to_v9_preserving_data(tmp_path: Path) -> None:
    db_path = tmp_path / "v8.sqlite3"
    _create_v8_db(db_path)

    schema.initialize_database(db_path)

    conn = _connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
        assert schema._is_compatible_v12(conn)
        row = conn.execute(
            "SELECT id, title, owner_id FROM conversations WHERE id = 'c1'"
        ).fetchone()
        assert tuple(row) == ("c1", "高数复习", "u1")
    finally:
        conn.close()


def test_v7_migrates_through_v8_to_v9(tmp_path: Path) -> None:
    db_path = tmp_path / "v7.sqlite3"
    _create_v7_db(db_path)
    schema.initialize_database(db_path)
    conn = _connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
        assert schema._is_compatible_v12(conn)
    finally:
        conn.close()


def test_v6_requires_owner_and_migrates_to_v9(tmp_path: Path) -> None:
    db_path = tmp_path / "v6.sqlite3"
    _create_v6_db(db_path)

    with pytest.raises(ConversationStorageError):
        schema.initialize_database(db_path)

    owner = schema.SchemaOwner(
        id="admin-1",
        username="admin",
        username_normalized="admin",
        password_hash="hash",
        created_at="2026-11-01T00:00:00Z",
    )
    schema.initialize_database(db_path, migration_owner=owner)
    conn = _connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
        assert schema._is_compatible_v12(conn)
    finally:
        conn.close()


def test_v8_to_v9_rollback_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "v8_fail.sqlite3"
    _create_v8_db(db_path)

    monkeypatch.setattr(
        schema,
        "_v9_migration_statements",
        lambda: (*schema._V9_MIGRATION_STATEMENTS[:1], "INVALID SQL STATEMENT"),
    )

    with pytest.raises(ConversationStorageError):
        schema.initialize_database(db_path)

    conn = _connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
        assert schema._is_compatible_v8(conn)
    finally:
        conn.close()


def test_unknown_schema_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE strange (x TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(ConversationStorageError):
        schema.initialize_database(db_path)


def test_recover_running_marks_generating_guides_failed(tmp_path: Path) -> None:
    db_path = tmp_path / "recover.sqlite3"
    schema.initialize_database(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO users (id, username, username_normalized, password_hash, created_at) "
            "VALUES ('u1', 'alice', 'alice', 'hash', '2026-11-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO personal_materials (id, owner_id, display_name, stored_name, file_sha256, size_bytes, page_count, status, created_at, updated_at) "
            "VALUES ('m1', 'u1', 'ch1.pdf', 'u1/m1.pdf', 'abc', 100, 5, 'active', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO material_review_guides (id, owner_id, material_id, file_sha256, start_page, end_page, rule_version, status, generation_token, readability_json, created_at, updated_at) "
            "VALUES ('g1', 'u1', 'm1', 'abc', 1, 5, 'v1', 'generating', 'tok1', '{}', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    schema.initialize_database(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, error_code FROM material_review_guides WHERE id = 'g1'"
        ).fetchone()
        assert row == ("failed", "INTERRUPTED")
    finally:
        conn.close()


def test_migrate_database_cli_backup_upgrade_and_idempotent(tmp_path: Path) -> None:
    from app.auth.migrate import migrate_database

    db_path = tmp_path / "v8_cli.sqlite3"
    backup_dir = tmp_path / "backups"
    _create_v8_db(db_path)

    first = migrate_database(db_path, backup_dir=backup_dir)
    assert first.version == schema.SCHEMA_VERSION
    assert first.already_migrated is False
    assert first.backup_path is not None and first.backup_path.is_file()
    assert first.conversations == 1

    second = migrate_database(db_path, backup_dir=backup_dir)
    assert second.version == schema.SCHEMA_VERSION
    assert second.already_migrated is True
    assert second.backup_path is None
