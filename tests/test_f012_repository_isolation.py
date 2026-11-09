"""F012：仓库层按 owner 拒绝轮次与作答评价读写。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.conversations import schema
from app.conversations.errors import (
    ConversationNotFoundError,
    ProblemNotFoundError,
)
from app.conversations.repository import ConversationRepository
from tests.f012_support import ensure_owner, owner_snapshot

A = "owner-a"
B = "owner-b"
REQUEST = "11111111-1111-4111-8111-111111111111"


def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "study.sqlite3"
    schema.initialize_database(db)
    ensure_owner(db, A)
    ensure_owner(db, B)
    a_repo = ConversationRepository(db, owner_id=A)
    conversation = a_repo.create_conversation()
    a_repo.start_turn(conversation.id, REQUEST, "A 的轮次")
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            """
            INSERT INTO exercises (
                id, conversation_id, problem_id, root_problem_id, statement_text,
                reference_solution_text, knowledge_point, status, created_at, updated_at
            ) VALUES ('ex-a', ?, 'p-a', 'p-a', 'A 的练习', '参考', '循环',
                      'pending_answer', '2026-01-01', '2026-01-01')
            """,
            (conversation.id,),
        )
        connection.execute(
            """
            INSERT INTO exercise_attempts (
                id, exercise_id, attempt_no, kind, student_answer_text,
                answered_after_revealed, created_at, updated_at
            ) VALUES ('att-a', 'ex-a', 1, 'initial', 'A 的答案', 0,
                      '2026-01-01', '2026-01-01')
            """,
            (),
        )
        connection.commit()
    finally:
        connection.close()
    return db


def test_scoped_repository_rejects_turn_reads_and_writes(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    b_repo = ConversationRepository(db, owner_id=B)
    a_repo = ConversationRepository(db, owner_id=A)
    a_conv = a_repo.list_conversations()[0].id
    before = owner_snapshot(db, A)

    assert b_repo.get_conversation(a_conv) is None
    assert b_repo.list_turns(a_conv) == []
    assert b_repo.find_turn(a_conv, REQUEST) is None
    assert b_repo.has_running_turn(a_conv) is False

    with pytest.raises(ConversationNotFoundError):
        b_repo.save_recovery(a_conv, REQUEST, {"version": 2})
    with pytest.raises(ConversationNotFoundError):
        b_repo.complete_output_turn(
            a_conv, REQUEST, kind="dynamic", output_json="{}"
        )
    with pytest.raises(ConversationNotFoundError):
        b_repo.fail_turn(a_conv, REQUEST, "X", "x")

    assert owner_snapshot(db, A)["digest"] == before["digest"]


def test_scoped_repository_rejects_attempt_evaluation_writes(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    b_repo = ConversationRepository(db, owner_id=B)
    assert b_repo.get_exercise_attempt("att-a") is None
    assert b_repo.get_attempt_by_request("ex-a", REQUEST) is None
    with pytest.raises(ProblemNotFoundError):
        b_repo.save_attempt_evaluation("att-a", evaluation_json='{"is_correct": true}')
    with pytest.raises(ProblemNotFoundError):
        b_repo.update_pending_exercise_attempt(
            "att-a", student_answer_text="B 改写", student_solution_text=None
        )
    connection = sqlite3.connect(db)
    try:
        row = connection.execute(
            "SELECT evaluation_json, student_answer_text FROM exercise_attempts "
            "WHERE id = 'att-a'"
        ).fetchone()
    finally:
        connection.close()
    assert row == (None, "A 的答案")