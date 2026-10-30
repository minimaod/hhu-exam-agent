"""SQLite v2 → v3 迁移与兼容性边界（F006：练习、评价与会话上下文）。"""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.conversations import schema
from app.conversations.errors import ConversationStorageError
from app.conversations.repository import ConversationRepository


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_v2(path: Path, *, version: int = 2) -> None:
    """按纯 v2 结构建立测试库。"""
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for stmt in schema._CREATE_V2_STATEMENTS:
            connection.execute(stmt)
        connection.execute(f"PRAGMA user_version = {version}")
        connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?)",
            (
                "conv-v2",
                "高等数学复习",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO problems (
                id, conversation_id, root_problem_id, version,
                statement_text, status, clarity, source_type,
                active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "prob-1",
                "conv-v2",
                "prob-1",
                1,
                "求极限 lim(x->0) sin(x)/x",
                "explained",
                "clear",
                "none",
                1,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_empty_database_initializes_to_current(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    repo = ConversationRepository(path)
    repo.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "conversation_contexts" in tables
    assert "exercises" in tables
    assert "exercise_attachments" in tables
    assert "exercise_attempts" in tables
    assert "exercise_attempt_attachments" in tables


def test_v2_database_migrates_to_current(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    build_v2(path)

    repo = ConversationRepository(path)
    repo.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
        cols = [
            row[1]
            for row in connection.execute("PRAGMA table_info(conversation_turns)")
        ]
        assert "exercise_id" in cols

        # 检查上下文是否已初始化
        ctx = connection.execute(
            "SELECT * FROM conversation_contexts WHERE conversation_id = 'conv-v2'"
        ).fetchone()
        assert ctx is not None
        # active_problem_id 应自动关联到 prob-1
        assert ctx[2] == "prob-1"


def test_v3_migration_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "study.sqlite3"
    build_v2(path)
    before = file_hash(path)

    def broken_statements():
        return (*schema._V3_MIGRATION_STATEMENTS, "CREATE TABLE broken (")

    monkeypatch.setattr(schema, "_v3_migration_statements", broken_statements)

    with pytest.raises(ConversationStorageError):
        ConversationRepository(path).initialize()

    assert file_hash(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "exercises" not in tables
