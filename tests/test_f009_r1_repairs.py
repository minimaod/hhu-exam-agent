"""F009-R1 返修回归：课程归一、换课、时间依据、旧版本与工具目标编号。

每条用例对应导师复核里的一个具体缺陷；断言的是程序行为，不是文案。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.conversations.protocol import ActionPlan
from app.conversations.service import _DynamicContext
from app.memory.models import MemoryCorrectionRequest
from tests.test_f009_agent_memory import build, prepared_for

PY = "Python语言程序设计"
MATH = "高等数学"


def seed_python_wrong(seed, *, knowledge_point: str = "循环的条件与终止条件"):
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(conversation, problem, knowledge_point=knowledge_point)
    attempt = seed.attempt(exercise, correct=False)
    return conversation, problem, exercise, attempt


def test_course_alias_correction_is_readable_by_canonical_name(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(conversation)
    exercise = seed.exercise(conversation, problem, knowledge_point="循环")
    seed.attempt(exercise, correct=False)

    view = memory.correct(
        "exercise", exercise, MemoryCorrectionRequest(course="Python")
    )
    assert view.course == PY
    assert view.course_source == "user"

    summary = memory.summary(PY, days=30, limit=50)
    assert [item.entry_id for item in summary.entries] == [f"exercise:{exercise}"]
    assert summary.counts.evidence == 1
    courses = memory.courses()
    assert [item.label for item in courses.items] == [PY]
    assert courses.items[0].evidence == 1
    assert courses.pending == 0


def test_legacy_alias_correction_row_is_readable(tmp_path: Path) -> None:
    """返修前写入的别名行（如 “Python”）也要能按规范名读到。"""

    service, seed, memory = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(conversation)
    exercise = seed.exercise(conversation, problem, knowledge_point="循环")
    seed.attempt(exercise, correct=False)
    with sqlite3.connect(memory.db_path) as connection:
        connection.execute(
            "INSERT INTO memory_corrections (entry_key, course, knowledge_point, "
            "note, created_at, updated_at) VALUES (?, 'Python', NULL, NULL, ?, ?)",
            (f"exercise:{exercise}", "2026-10-31T00:00:00+00:00", "2026-10-31T00:00:00+00:00"),
        )
        connection.commit()

    summary = memory.summary(PY, days=30, limit=50)
    assert [item.entry_id for item in summary.entries] == [f"exercise:{exercise}"]
    assert summary.entries[0].course == PY
    assert summary.entries[0].course_source == "user"


def test_clearing_course_returns_the_entry_to_pending(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    conversation, _, exercise, _ = seed_python_wrong(seed)
    assert memory.summary(PY, days=30, limit=50).counts.evidence == 1

    view = memory.correct(
        "exercise", exercise, MemoryCorrectionRequest(course="")
    )
    assert view.course is None
    assert view.course_source == "user"

    assert memory.summary(PY, days=30, limit=50).counts.evidence == 0
    pending_view = memory.summary(None, days=30, limit=50)
    assert [item.entry_id for item in pending_view.pending_course_entries] == [
        f"exercise:{exercise}"
    ]
    assert memory.courses().pending == 1


def test_student_course_switch_beats_the_previous_memory_course(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    python_conversation, _, python_exercise, _ = seed_python_wrong(seed)
    math_conversation = seed.conversation()
    math_problem = seed.problem(
        math_conversation, paper_id="math-2024-final", source_type="registered_paper"
    )
    math_exercise = seed.exercise(math_conversation, math_problem, knowledge_point="极限")
    seed.attempt(math_exercise, correct=False)

    ctx = _DynamicContext(conversation_id=python_conversation)
    # 上一轮读的是 Python；本轮学生明说换课。
    ctx.memory_course = PY
    text = "改看高等数学的复习建议，先补哪块"
    outcome = service._memory_review_outcome(
        prepared_for(python_conversation, "req-1", text),
        ActionPlan(action="review_memory", parameters={}),
        ctx,
        text,
    )
    assert outcome.status == "success"
    assert outcome.details["memory_course"] == MATH
    ids = {item["entry_id"] for item in outcome.details["memory_entries"]}
    assert f"exercise:{math_exercise}" in ids
    assert f"exercise:{python_exercise}" not in ids
    assert ctx.memory_course == MATH


def test_scheduler_confirmed_course_wins_over_previous_memory(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    python_conversation, _, python_exercise, _ = seed_python_wrong(seed)
    math_conversation = seed.conversation()
    math_problem = seed.problem(
        math_conversation, paper_id="math-2024-final", source_type="registered_paper"
    )
    math_exercise = seed.exercise(math_conversation, math_problem, knowledge_point="极限")
    seed.attempt(math_exercise, correct=False)

    ctx = _DynamicContext(conversation_id=python_conversation)
    ctx.memory_course = PY
    text = "看看我最近学得怎么样，先补哪块"
    outcome = service._memory_review_outcome(
        prepared_for(python_conversation, "req-2", text),
        ActionPlan(action="review_memory", parameters={}),
        ctx,
        text,
        confirmed_course=MATH,
    )
    assert outcome.details["memory_course"] == MATH
    ids = {item["entry_id"] for item in outcome.details["memory_entries"]}
    assert f"exercise:{math_exercise}" in ids
    assert f"exercise:{python_exercise}" not in ids


def test_superseded_evidence_is_history_only(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    conversation = seed.conversation()
    old_problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(conversation, old_problem, knowledge_point="循环")
    seed.attempt(exercise, correct=False)
    # 同一道原题出了新版本：旧证据只能回看。
    seed.problem(
        seed.conversation(),
        root_problem_id=old_problem,
        version=2,
        paper_id="py-2023-2024-s2-final",
        source_type="registered_paper",
    )

    view = memory.summary(PY, days=30, limit=50)
    assert view.counts.evidence == 1
    assert view.counts.superseded == 1
    assert view.counts.wrong_independent == 0
    assert view.groups == []
    assert view.suggestions == []
    assert view.entries[0].validity == "superseded"

    material = memory.reply_material(PY) or ""
    assert "旧版本" in material
    assert "未计入以上统计" in material
    assert "只作历史回看" in material


def test_old_wrong_keeps_its_own_time_instead_of_becoming_recent(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(conversation, problem, knowledge_point="循环")
    seed.attempt(exercise, correct=False, days_ago=100)
    seed.attempt(
        exercise,
        attempt_no=2,
        kind="correction",
        correct=True,
        after_reveal=True,
        days_ago=1,
    )

    view = memory.summary(PY, days=30, limit=50)
    entry = next(
        item for item in view.entries if item.entry_id == f"exercise:{exercise}"
    )
    assert entry.verdict == "after_reveal"
    assert entry.first_verdict == "wrong_independent"
    assert entry.wrong_at is not None
    age = (datetime.now(timezone.utc) - entry.wrong_at).days
    assert age >= 99
    assert view.counts.wrong_independent == 0
    assert view.counts.earlier_wrong >= 1

    group = next(item for item in view.groups if item.label == "循环")
    assert group.wrong_count == 0
    assert group.earlier_wrong_count == 1
    assert group.after_reveal_count == 1
    assert group.latest_wrong_at is None

    material = memory.reply_material(PY) or ""
    assert "早于本窗口" in material
    assert "最近一次判错" not in material
    assert "首次" in material


def test_supplemented_correct_answer_uses_the_latest_state(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(conversation, problem, knowledge_point="循环的条件与终止条件")
    seed.attempt(
        exercise, correct=None, needs_supplement=True, days_ago=2
    )
    seed.attempt(exercise, attempt_no=2, kind="supplement", correct=True, days_ago=1)

    view = memory.summary(PY, days=30, limit=50)
    group = next(item for item in view.groups if item.label == "循环的条件与终止条件")
    assert group.correct_count == 1
    assert group.insufficient_count == 0
    assert group.wrong_count == 0
    assert "一次正确不代表已经掌握" in (group.suggestion or "")
    assert view.counts.wrong_independent == 0

    material = memory.reply_material(PY) or ""
    assert "首次" in material
    assert "过程不足" in material
    assert "过程不足或评价未完成 1 次" not in material


def test_wrong_then_after_reveal_keeps_both_attempts(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    conversation, _, exercise, _ = seed_python_wrong(seed)
    seed.attempt(
        exercise,
        attempt_no=2,
        kind="correction",
        correct=True,
        after_reveal=True,
        days_ago=0,
    )

    view = memory.summary(PY, days=30, limit=50)
    assert view.counts.wrong_independent == 1
    group = next(item for item in view.groups if item.label == "循环的条件与终止条件")
    assert group.wrong_count == 1

    material = memory.reply_material(PY) or ""
    assert "独立作答被判错" in material
    assert "看过解答后对照作答" in material
    assert "首次" in material


def test_unclear_independence_is_not_counted_as_independent_wrong(
    tmp_path: Path,
) -> None:
    service, seed, memory = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    unclear = seed.exercise(conversation, problem, knowledge_point="循环")
    seed.attempt(unclear, correct=False, independence="unclear")
    legacy = seed.exercise(conversation, problem, knowledge_point="列表")
    # 旧记录没有独立性字段：同样不能断言“独立答错”。
    seed.attempt(legacy, correct=False, independence=None)

    view = memory.summary(PY, days=30, limit=50)
    assert view.counts.wrong_independent == 0
    assert view.counts.unclear == 2
    loop = next(item for item in view.groups if item.label == "循环")
    assert loop.wrong_count == 0
    assert loop.unclear_count == 1
    assert "独立性没有确认" in (loop.suggestion or "")

    material = memory.reply_material(PY) or ""
    assert "独立性未确认" in material
    assert "独立作答被判错" not in material
    assert {item.verdict for item in view.entries} == {"unclear"}


def test_review_exposes_pending_entries_and_attempt_ids(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    conversation, _, python_exercise, python_attempt = seed_python_wrong(seed)
    pending_conversation = seed.conversation()
    pending_problem = seed.problem(pending_conversation)
    pending_exercise = seed.exercise(
        pending_conversation, pending_problem, knowledge_point="杂项"
    )
    pending_attempt = seed.attempt(pending_exercise, correct=False)

    ctx = _DynamicContext(conversation_id=conversation)
    prepared = prepared_for(conversation, "req-3", "我该先补什么")
    outcome = service._memory_review_outcome(
        prepared,
        ActionPlan(action="review_memory", parameters={}),
        ctx,
        "我该先补什么",
        confirmed_course=PY,
    )
    entries = {item["entry_id"]: item for item in outcome.details["memory_entries"]}
    pending_id = f"exercise:{pending_exercise}"
    assert entries[pending_id]["pending_course"] is True
    assert entries[pending_id]["attempts"][0]["attempt_id"] == pending_attempt
    assert entries[f"exercise:{python_exercise}"]["attempts"][0]["attempt_id"] == (
        python_attempt
    )
    assert pending_id in ctx.memory_entry_ids
    assert f"attempt:{python_attempt}" in ctx.memory_entry_ids

    corrected = service._memory_correct_outcome(
        prepared,
        ActionPlan(
            action="correct_memory",
            parameters={"entry_id": pending_id, "course": "高等数学"},
        ),
        ctx,
    )
    assert corrected.status == "success"
    assert corrected.details["memory_course"] == MATH

    removed = service._memory_delete_outcome(
        prepared,
        ActionPlan(
            action="delete_memory",
            parameters={
                "entry_id": f"attempt:{python_attempt}",
                "reason": "这次不算",
            },
        ),
        ctx,
    )
    assert removed.status == "success"
    after = memory.summary(PY, days=30, limit=50)
    remaining = next(
        item for item in after.entries if item.entry_id == f"exercise:{python_exercise}"
    )
    assert remaining.attempts[0].excluded is True


def test_student_note_reaches_material_and_tool_details(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    conversation, _, exercise, _ = seed_python_wrong(seed)
    memory.correct(
        "exercise", exercise, MemoryCorrectionRequest(note="这条是我算错了符号")
    )

    material = memory.reply_material(PY) or ""
    assert "附注：这条是我算错了符号" in material

    ctx = _DynamicContext(conversation_id=conversation)
    outcome = service._memory_review_outcome(
        prepared_for(conversation, "req-4", "我该先补什么"),
        ActionPlan(action="review_memory", parameters={}),
        ctx,
        "我该先补什么",
        confirmed_course=PY,
    )
    detail = next(
        item
        for item in outcome.details["memory_entries"]
        if item["entry_id"] == f"exercise:{exercise}"
    )
    assert detail["note"] == "这条是我算错了符号"