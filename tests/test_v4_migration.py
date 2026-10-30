"""SQLite v3 → v4 迁移：旧作答回填为第 1 次尝试（F006 B 批）。"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.conversations import schema
from app.conversations.errors import ConversationStorageError
from app.conversations.repository import ConversationRepository


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_v3(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in schema._CREATE_V3_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?)",
            (
                "conv-v3",
                "线性代数复习",
                "2026-02-01T00:00:00+00:00",
                "2026-02-01T00:00:05+00:00",
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
                "prob-v3",
                "conv-v3",
                "prob-v3",
                1,
                "求矩阵 A 的特征值",
                "explained",
                "clear",
                "none",
                1,
                "2026-02-01T00:00:00+00:00",
                "2026-02-01T00:00:01+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO conversation_contexts (
                conversation_id, confirmed_course, active_problem_id,
                active_exercise_id, last_suggestions_json, last_action_summary,
                updated_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                "conv-v3",
                None,
                "prob-v3",
                "ex-v3",
                "2026-02-01T00:00:05+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO exercises (
                id, conversation_id, problem_id, root_problem_id,
                statement_text, reference_solution_text, knowledge_point,
                status, student_answer_text, student_solution_text,
                revealed_solution_at, answered_after_revealed,
                evaluation_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ex-v3",
                "conv-v3",
                "prob-v3",
                "prob-v3",
                "求矩阵 B 的特征值",
                "特征值为 1 和 2。",
                "特征值",
                "submitted",
                "1 和 2",
                "先算特征多项式。",
                "2026-02-01T00:00:03+00:00",
                1,
                json.dumps(
                    {
                        "is_correct": True,
                        "final_answer_assessment": "答案正确。",
                        "step_by_step_analysis": "步骤完整。",
                        "guidance": "继续保持。",
                        "needs_supplement": False,
                    },
                    ensure_ascii=False,
                ),
                "2026-02-01T00:00:02+00:00",
                "2026-02-01T00:00:04+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO attachments (
                id, conversation_id, stored_name, mime, width, height,
                size_bytes, sha256, pending, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                "att-v3",
                "conv-v3",
                "att-v3.png",
                "image/png",
                320,
                140,
                128,
                "a" * 64,
                "2026-02-01T00:00:02+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO exercise_attachments (exercise_id, attachment_id, role)
            VALUES ('ex-v3', 'att-v3', 'student_solution')
            """
        )
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
    finally:
        connection.close()


def test_v3_database_backfills_first_attempt(tmp_path: Path) -> None:
    path = tmp_path / "study.sqlite3"
    build_v3(path)

    ConversationRepository(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
        row = connection.execute(
            "SELECT * FROM exercise_attempts WHERE exercise_id = 'ex-v3'"
        ).fetchone()
        assert row is not None
        columns = [item[0] for item in connection.execute(
            "SELECT * FROM exercise_attempts LIMIT 0"
        ).description]
        data = dict(zip(columns, row))
        assert data["attempt_no"] == 1
        assert data["kind"] == "initial"
        assert data["student_answer_text"] == "1 和 2"
        assert data["answered_after_revealed"] == 1
        assert data["request_id"] is None
        assert json.loads(data["evaluation_json"])["is_correct"] is True
        linked = connection.execute(
            """
            SELECT attachment_id, role FROM exercise_attempt_attachments
            WHERE attempt_id = ?
            """,
            (data["id"],),
        ).fetchall()
        assert linked == [("att-v3", "student_solution")]

    repo = ConversationRepository(path)
    repo.initialize()
    exercise = repo.get_exercise("ex-v3")
    assert exercise is not None
    assert len(exercise.attempts) == 1
    assert exercise.attempts[0].attempt_no == 1
    assert exercise.attempts[0].student_answer_text == "1 和 2"
    assert exercise.attempts[0].answered_after_revealed is True
    assert [item.id for item in exercise.attempts[0].attachments] == ["att-v3"]


def test_v4_migration_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "study.sqlite3"
    build_v3(path)
    before = file_hash(path)

    def broken_statements():
        return (*schema._V4_MIGRATION_STATEMENTS, "CREATE TABLE broken (")

    monkeypatch.setattr(schema, "_v4_migration_statements", broken_statements)

    with pytest.raises(ConversationStorageError):
        ConversationRepository(path).initialize()

    assert file_hash(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "exercise_attempts" not in tables