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
            INSERT INTO problems (
                id, conversation_id, root_problem_id, version, statement_text,
                status, clarity, source_type, active, created_at, updated_at
            ) VALUES ('p-a', ?, 'p-a', 1, 'A 的题目', 'awaiting_supplement',
                      'unclear', 'none', 1, '2026-01-01', '2026-01-01')
            """,
            (conversation.id,),
        )
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
        connection.execute(
            "INSERT INTO conversation_contexts (conversation_id, active_exercise_id, updated_at) "
            "VALUES (?, 'ex-a', '2026-01-01')",
            (conversation.id,),
        )
        connection.execute(
            """
            INSERT INTO mistake_records (
                id, conversation_id, problem_id, root_problem_id, problem_version,
                request_id, title, statement_text, source_json, created_at, updated_at
            ) VALUES ('rec-a', ?, 'p-a', 'p-a', 1, 'req-a', 'A 的错题',
                      'A 的题目', '{}', '2026-01-01', '2026-01-01')
            """,
            (conversation.id,),
        )
        connection.execute(
            "INSERT INTO mistake_record_revisions "
            "(id, record_id, request_id, reason, snapshot_json, created_at) "
            "VALUES (1, 'rec-a', 'req-r', 'correction', '{}', '2026-01-01')"
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


def test_repository_rejects_cross_user_revisions_active_pointer_and_exercise(
    tmp_path: Path,
) -> None:
    db = _seed(tmp_path)
    a_repo = ConversationRepository(db, owner_id=A)
    b_repo = ConversationRepository(db, owner_id=B)
    a_conv = a_repo.list_conversations()[0].id
    b_conv = b_repo.create_conversation().id
    before = owner_snapshot(db, A)

    assert b_repo.list_record_revisions("rec-a") == []
    assert b_repo.find_record_revision("rec-a", "req-r") is None
    assert b_repo.list_attachments_for_problem("p-a") == []
    assert b_repo.list_attachments_for_record("rec-a") == []

    with pytest.raises(ConversationNotFoundError):
        b_repo.clear_active_exercise(a_conv)
    with pytest.raises(ProblemNotFoundError):
        b_repo.create_exercise(
            id="ex-b",
            conversation_id=b_conv,
            problem_id="p-a",
            root_problem_id="p-a",
            statement_text="B 伪造练习",
            reference_solution_text="参考",
            knowledge_point="循环",
        )
    with pytest.raises(ProblemNotFoundError):
        b_repo.upsert_context(b_conv, active_problem_id="p-a")
    with pytest.raises(ProblemNotFoundError):
        b_repo.upsert_context(b_conv, active_exercise_id="ex-a")

    assert a_repo.get_active_exercise(a_conv) is not None
    assert a_repo.list_record_revisions("rec-a")
    assert owner_snapshot(db, A)["digest"] == before["digest"]