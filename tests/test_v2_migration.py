"""SQLite v1 → v2 迁移与兼容性边界。"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.conversations import schema
from app.conversations.errors import ConversationStorageError
from app.conversations.repository import ConversationRepository
from app.conversations.inputs import query_input

V1_DDL = """
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    query TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('running', 'completed', 'failed', 'interrupted')),
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (conversation_id, request_id),
    UNIQUE (conversation_id, seq)
);
CREATE UNIQUE INDEX turns_one_running
    ON conversation_turns(conversation_id)
    WHERE status = 'running';
PRAGMA user_version = 1;
"""

RESULT_JSON = json.dumps(
    {
        "type": "result",
        "kind": "empty",
        "message": "没有匹配的试卷",
        "filters": {"course": "高等数学", "exam_type": None},
        "items": [],
        "total": 0,
    },
    ensure_ascii=False,
)


def build_v1(path: Path, *, version: int = 1) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(V1_DDL)
        connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?)",
            (
                "conv-1",
                "我要复习高数",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO conversation_turns (
                conversation_id, request_id, seq, query, status, result_json,
                started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "conv-1",
                "req-1",
                1,
                "我要复习高数",
                "completed",
                RESULT_JSON,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO conversation_turns (
                conversation_id, request_id, seq, query, status, error_code,
                error_message, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "conv-1",
                "req-2",
                2,
                "找大学物理试卷",
                "failed",
                "MODEL_TIMEOUT",
                "超时",
                "2026-01-01T00:00:02+00:00",
                "2026-01-01T00:00:03+00:00",
            ),
        )
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_v1_upgrades_and_preserves_conversation_turns(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    build_v1(path)

    repository = ConversationRepository(path)
    repository.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(conversation_turns)")
        ]
    assert "kind" in columns and "input_json" in columns

    turns = repository.list_turns("conv-1")
    assert [turn.request_id for turn in turns] == ["req-1", "req-2"]
    assert turns[0].kind == "query"
    assert turns[0].status == "completed"
    assert turns[0].result is not None and turns[0].result.kind == "empty"
    assert turns[0].input_json == query_input("我要复习高数")
    assert turns[1].status == "failed"
    assert turns[1].error is not None and turns[1].error.code == "MODEL_TIMEOUT"


def test_v0_complete_v1_schema_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    build_v1(path, version=0)

    repository = ConversationRepository(path)
    repository.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    assert repository.list_turns("conv-1")[0].result is not None


def test_migration_failure_rolls_back_and_keeps_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "study.sqlite3"
    build_v1(path)
    before = file_hash(path)

    def broken_statements():
        return (*schema._MIGRATION_STATEMENTS, "CREATE TABLE broken (")

    monkeypatch.setattr(schema, "_migration_statements", broken_statements)

    with pytest.raises(ConversationStorageError):
        ConversationRepository(path).initialize()

    assert file_hash(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert tables == {"conversations", "conversation_turns", "sqlite_sequence"}
    assert "problems" not in tables


def test_v1_with_extra_table_is_not_modified(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    build_v1(path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE notes (body TEXT)")
        connection.commit()
    before = file_hash(path)

    with pytest.raises(ConversationStorageError):
        ConversationRepository(path).initialize()

    assert file_hash(path) == before


def test_v1_with_broken_running_index_is_not_migrated(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    build_v1(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX turns_one_running")
        connection.commit()
    before = file_hash(path)

    with pytest.raises(ConversationStorageError):
        ConversationRepository(path).initialize()

    assert file_hash(path) == before


def test_v2_reopens_and_rejects_damaged_index(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    ConversationRepository(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
        connection.execute("DROP INDEX problems_one_active")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()

    with pytest.raises(ConversationStorageError):
        ConversationRepository(path).initialize()


def test_migration_recovers_running_turn(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    build_v1(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO conversation_turns (
                conversation_id, request_id, seq, query, status, started_at
            ) VALUES (?, ?, ?, ?, 'running', ?)
            """,
            ("conv-1", "req-running", 3, "进行中的查询", "2026-01-01T00:00:04+00:00"),
        )
        connection.commit()

    repository = ConversationRepository(path)
    repository.initialize()

    turn = repository.find_turn("conv-1", "req-running")
    assert turn is not None and turn.status == "interrupted"


def test_v2_rejects_unknown_version(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    ConversationRepository(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")
        connection.commit()

    with pytest.raises(ConversationStorageError):
        ConversationRepository(path).initialize()
