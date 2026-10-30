"""SQLite v4 → v5：真题选择、变式来源与错题来源表。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.conversations import schema
from app.conversations.errors import ConversationStorageError


def build_v4(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in schema._CREATE_V4_STATEMENTS:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 4")
        connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?)",
            (
                "conv-v4",
                "旧会话",
                "2026-02-01T00:00:00+00:00",
                "2026-02-01T00:00:05+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _selection(question_id: str, *, active: int) -> tuple:
    return (
        f"sel-{question_id}",
        "conv-v4",
        f"req-{question_id}",
        "req-source",
        question_id,
        "py-2024-2025-s1-final",
        "iv-0123456789abcdef",
        "sv-0123456789abcdef",
        "a" * 64,
        "choice",
        "1",
        "{}",
        None,
        None,
        None,
        active,
        "2026-02-01T00:00:00+00:00",
        "2026-02-01T00:00:00+00:00",
    )


def test_v4_migrates_to_v5_additively(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    build_v4(path)
    schema.initialize_database(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"question_selections", "exercise_question_sources", "record_question_sources"} <= tables
        assert connection.execute("SELECT title FROM conversations").fetchone()[0] == "旧会话"
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(question_selections)")
        }
        assert {"content_sha256", "answer_revealed_at", "active"} <= columns
        connection.execute(
            "INSERT INTO question_selections VALUES (" + ",".join("?" * 18) + ")",
            _selection("q-1", active=1),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO question_selections VALUES (" + ",".join("?" * 18) + ")",
                _selection("q-2", active=1),
            )
        connection.execute(
            "UPDATE question_selections SET active=0 WHERE id='sel-q-1'"
        )
        connection.execute(
            "INSERT INTO question_selections VALUES (" + ",".join("?" * 18) + ")",
            _selection("q-2", active=1),
        )
        bad = list(_selection("q-3", active=0))
        bad[9] = "essay"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO question_selections VALUES (" + ",".join("?" * 18) + ")",
                tuple(bad),
            )
    finally:
        connection.close()


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 6")
    connection.close()
    with pytest.raises(ConversationStorageError):
        schema.initialize_database(path)