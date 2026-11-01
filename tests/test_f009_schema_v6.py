"""F009：v6 只追加两张记忆覆盖表，迁移失败必须整体回滚。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.conversations import schema
from app.conversations.errors import ConversationStorageError


def build_v5(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in schema._CREATE_V5_STATEMENTS:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 5")
        connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?)",
            (
                "conv-v5",
                "升级前的会话",
                "2026-02-01T00:00:00+00:00",
                "2026-02-01T00:00:05+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()


def test_fresh_database_creates_v6_memory_tables(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    schema.initialize_database(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert {"memory_corrections", "memory_exclusions"} <= _tables(path)
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(memory_corrections)")
        ]
        assert columns == [
            "entry_key",
            "course",
            "knowledge_point",
            "note",
            "created_at",
            "updated_at",
        ]
        assert connection.execute("SELECT COUNT(*) FROM memory_corrections").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_exclusions").fetchone()[0] == 0
    finally:
        connection.close()


def test_v5_migrates_to_v6_without_touching_old_rows(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    build_v5(path)
    schema.initialize_database(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("SELECT title FROM conversations").fetchone()[0] == "升级前的会话"
        # 只建表、不回填：旧练习不会凭空出现在记忆覆盖表里。
        assert connection.execute("SELECT COUNT(*) FROM memory_corrections").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_exclusions").fetchone()[0] == 0
        connection.execute(
            "INSERT INTO memory_corrections VALUES (?, ?, ?, ?, ?, ?)",
            ("exercise:x", None, None, "备注", "2026-11-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO memory_corrections VALUES (?, ?, ?, ?, ?, ?)",
                ("exercise:x", None, None, "重复", "2026-11-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00"),
            )
    finally:
        connection.close()


def test_v6_migration_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "study.sqlite3"
    build_v5(path)
    monkeypatch.setattr(
        schema,
        "_v6_migration_statements",
        lambda: ("CREATE TABLE broken (",),
    )
    with pytest.raises(ConversationStorageError):
        schema.initialize_database(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert "memory_corrections" not in _tables(path)
        assert connection.execute("SELECT title FROM conversations").fetchone()[0] == "升级前的会话"
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() != "wal", "迁移失败后不应切换 WAL"
    finally:
        connection.close()


def test_unknown_v6_structure_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    schema.initialize_database(path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE memory_exclusions")
    connection.commit()
    connection.close()

    with pytest.raises(ConversationStorageError) as error:
        schema.initialize_database(path)
    assert "版本" in str(error.value) or "结构" in str(error.value)
    assert "memory_exclusions" not in _tables(path)


def test_study_endpoints_fail_closed_but_papers_still_work(
    study_db_path: Path,
    sample_root: Path,
    make_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_v5(study_db_path)
    monkeypatch.setattr(
        schema,
        "_v6_migration_statements",
        lambda: ("CREATE TABLE broken (",),
    )
    client: TestClient = make_client(sample_root)

    assert client.get("/api/learning-memory").status_code == 503
    assert client.get("/api/conversations").status_code == 503
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/papers").status_code == 200
    # 失败后原库保持 v5，不留下半张表。
    connection = sqlite3.connect(study_db_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert "memory_corrections" not in _tables(study_db_path)
    finally:
        connection.close()