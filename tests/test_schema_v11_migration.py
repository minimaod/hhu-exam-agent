"""v10 → v11 数据库结构与多学科题型 CHECK 约束迁移测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.auth.migrate import migrate_database
from app.conversations import schema


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_v10_with_selection(db_path: Path) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for stmt in schema._CREATE_V10_STATEMENTS:
            conn.execute(stmt)
        conn.execute("PRAGMA user_version = 10")
        conn.execute(
            "INSERT INTO users (id, username, username_normalized, password_hash, created_at) "
            "VALUES ('u1', 'alice', 'alice', 'hash', '2026-11-18T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, owner_id) "
            "VALUES ('c1', '线代复习', '2026-11-18T00:00:00Z', '2026-11-18T00:00:00Z', 'u1')"
        )
        conn.execute(
            "INSERT INTO question_selections ("
            "id, conversation_id, request_id, source_request_id, question_id, paper_id, "
            "build_id, source_version, content_sha256, question_type, number_label, "
            "snapshot_json, active, created_at, updated_at"
            ") VALUES ("
            "'sel-old', 'c1', 'req-1', 'req-1', 'py-q1', 'py-p1', "
            "'iv-1111111111111111', 'sv-1111111111111111', 'sha-1', 'choice', '1', "
            "'{}', 0, '2026-11-18T00:00:00Z', '2026-11-18T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()


def test_empty_db_creates_v11_and_accepts_new_question_types(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh_v11.sqlite3"
    schema.initialize_database(db_path)
    conn = _connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
        assert schema._is_compatible_v12(conn)
        conn.execute(
            "INSERT INTO users (id, username, username_normalized, password_hash, created_at) "
            "VALUES ('u1', 'bob', 'bob', 'hash', '2026-11-18T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, owner_id) "
            "VALUES ('c1', '高数复习', '2026-11-18T00:00:00Z', '2026-11-18T00:00:00Z', 'u1')"
        )
        for idx, qtype in enumerate(("solve", "proof", "judge"), start=1):
            conn.execute(
                "INSERT INTO question_selections ("
                "id, conversation_id, request_id, source_request_id, question_id, paper_id, "
                "build_id, source_version, content_sha256, question_type, number_label, "
                "snapshot_json, active, created_at, updated_at"
                ") VALUES (?, 'c1', ?, ?, ?, 'math-p1', 'iv-1111111111111111', "
                "'sv-1111111111111111', 'sha', ?, ?, '{}', 0, '2026-11-18T00:00:00Z', '2026-11-18T00:00:00Z')",
                (f"sel-{idx}", f"req-{idx}", f"req-{idx}", f"math-q-{idx}", qtype, str(idx)),
            )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO question_selections ("
                "id, conversation_id, request_id, source_request_id, question_id, paper_id, "
                "build_id, source_version, content_sha256, question_type, number_label, "
                "snapshot_json, active, created_at, updated_at"
                ") VALUES ('sel-bad', 'c1', 'req-bad', 'req-bad', 'math-bad', 'math-p1', "
                "'iv-1111111111111111', 'sv-1111111111111111', 'sha', 'essay', '9', "
                "'{}', 0, '2026-11-18T00:00:00Z', '2026-11-18T00:00:00Z')"
            )
    finally:
        conn.close()


def test_v10_migrates_to_v11_preserving_existing_selections(tmp_path: Path) -> None:
    db_path = tmp_path / "v10.sqlite3"
    backup_dir = tmp_path / "backups"
    _create_v10_with_selection(db_path)

    report = migrate_database(db_path, backup_dir=backup_dir)
    assert report.version == schema.SCHEMA_VERSION
    assert report.already_migrated is False
    assert report.backup_path is not None and report.backup_path.is_file()

    conn = _connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
        assert schema._is_compatible_v12(conn)
        old = conn.execute(
            "SELECT id, question_id, question_type FROM question_selections WHERE id = 'sel-old'"
        ).fetchone()
        assert tuple(old) == ("sel-old", "py-q1", "choice")

        conn.execute(
            "INSERT INTO question_selections ("
            "id, conversation_id, request_id, source_request_id, question_id, paper_id, "
            "build_id, source_version, content_sha256, question_type, number_label, "
            "snapshot_json, active, created_at, updated_at"
            ") VALUES ('sel-new', 'c1', 'req-2', 'req-2', 'geo-q1', 'geo-p1', "
            "'iv-1111111111111111', 'sv-1111111111111111', 'sha-2', 'proof', '5', "
            "'{}', 1, '2026-11-18T00:00:00Z', '2026-11-18T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    second = migrate_database(db_path, backup_dir=backup_dir)
    assert second.version == schema.SCHEMA_VERSION
    assert second.already_migrated is True
    assert second.backup_path is None
