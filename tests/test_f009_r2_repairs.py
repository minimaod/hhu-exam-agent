"""F009-R2 返修回归：归一去重、逐次事实、附注传递、排除评价与练习计数。"""

from __future__ import annotations

import json
from pathlib import Path

from app.conversations.graph import ActionPlan
from app.conversations.reply import _describe_details
from app.conversations.service import _DynamicContext
from app.memory.models import MemoryCorrectionRequest
from app.memory.summary import material_block
from tests.test_f009_agent_memory import build, prepared_for
from tests.test_f009_evidence import build as build_memory

PY = "Python语言程序设计"


def same_slug_taxonomy(tmp_path: Path) -> Path:
    """list 与 tuple 指向同一个知识点，用来复现“归一后重复计数”。"""

    path = tmp_path / "knowledge-points.json"
    path.write_text(
        json.dumps(
            {
                "knowledge_points": [
                    {
                        "slug": "sequence-types",
                        "name": "序列类型",
                        "definition": "列表与元组",
                        "aliases": ["list", "tuple"],
                        "patterns": [],
                        "priority": 10,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def seed_exercise(seed, *, knowledge_point="循环", days_ago=0):
    conversation = seed.conversation()
    problem = seed.problem(
        conversation,
        paper_id="py-2023-2024-s2-final",
        source_type="registered_paper",
        days_ago=days_ago,
    )
    exercise = seed.exercise(
        conversation, problem, knowledge_point=knowledge_point, days_ago=days_ago
    )
    return conversation, exercise


def test_two_aliases_for_one_knowledge_point_count_once(tmp_path: Path) -> None:
    memory, seed = build_memory(tmp_path, taxonomy=same_slug_taxonomy(tmp_path))
    _, exercise = seed_exercise(seed, knowledge_point="list，tuple")
    seed.attempt(exercise, correct=False)

    view = memory.summary(PY, days=30, limit=50)
    group = next(item for item in view.groups if item.label == "序列类型")
    assert group.wrong_count == 1
    assert len(group.entry_ids) == 1
    assert view.counts.wrong_independent == 1
    assert "判错 1 次" in (group.suggestion or "")
    assert "判错 2 次" not in (group.suggestion or "")

    material = material_block(view)
    assert "判错 1 次" in material
    assert "判错 2 次" not in material


def test_material_keeps_every_attempt_that_changes_the_judgement(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    _, exercise = seed_exercise(seed)
    seed.attempt(exercise, correct=False, days_ago=3)
    seed.attempt(
        exercise,
        attempt_no=2,
        kind="correction",
        correct=True,
        after_reveal=True,
        days_ago=2,
    )
    seed.attempt(
        exercise,
        attempt_no=3,
        kind="supplement",
        correct=None,
        needs_supplement=True,
        days_ago=1,
    )

    view = memory.summary(PY, days=30, limit=50)
    group = next(item for item in view.groups if item.label == "循环")
    assert group.wrong_count == 1
    assert group.after_reveal_count == 1
    assert group.insufficient_count == 1

    material = material_block(view)
    assert "独立作答被判错" in material
    assert "看过解答后对照作答" in material
    assert "过程不足" in material
    # 三次作答都要有日期，不能只剩首末。
    assert material.count("→") >= 2


def test_material_prefers_the_entry_student_asked_about(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    notes = [f"第{index}条附注：这里是我当时真实的错误原因" for index in range(1, 6)]
    for index, note in enumerate(notes, start=1):
        _, exercise = seed_exercise(
            seed, knowledge_point=f"知识点{index}", days_ago=index
        )
        memory.correct("exercise", exercise, MemoryCorrectionRequest(note=note))

    view = memory.summary(PY, days=30, limit=50)
    assert len(view.entries) == 5

    # 不指定 focus 时，预算内五条都能展开（不再固定只取前四条）。
    plain = material_block(view)
    for note in notes:
        assert note in plain

    # 再补五条长附注，把逐条预算挤满：目标条目仍要优先展开。
    target_note = "目标附注：" + "这条是学生本轮明确问到的记录" * 2
    for index in range(6, 11):
        _, exercise = seed_exercise(
            seed, knowledge_point=f"知识点{index}", days_ago=index
        )
        memory.correct(
            "exercise", exercise, MemoryCorrectionRequest(note=f"第{index}条补充：" + "细节" * 20)
        )
    _, target_exercise = seed_exercise(seed, knowledge_point="目标知识点", days_ago=11)
    memory.correct(
        "exercise", target_exercise, MemoryCorrectionRequest(note=target_note)
    )
    view = memory.summary(PY, days=30, limit=50)
    for entry in view.entries:
        entry.statement_excerpt = "很长的题干" * 12
    target = f"exercise:{target_exercise}"
    focused = material_block(view, focus_entry_ids={target})
    assert target_note in focused
    assert "还有" in focused
    assert focused.index(target_note) < focused.index("还有")


def test_focus_ids_come_from_explicit_tool_parameters(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, exercise = seed_exercise(seed, knowledge_point="函数参数")
    seed.attempt(exercise, correct=False)

    # 模型明确点名“这一条”时，用 entry_ids 传进来，程序只核对编号是否存在。
    ctx = _DynamicContext(conversation_id=conversation)
    text = "函数参数那道题我总错"
    service._memory_review_outcome(
        prepared_for(conversation, "req-focus-1", text),
        ActionPlan(
            action="review_memory",
            parameters={"course": PY, "entry_ids": [f"exercise:{exercise}"]},
        ),
        ctx,
        text,
    )
    assert ctx.memory_focus_entry_ids == {f"exercise:{exercise}"}

    ctx2 = _DynamicContext(conversation_id=conversation)
    service._memory_review_outcome(
        prepared_for(conversation, "req-focus-2", "随便看看"),
        ActionPlan(action="review_memory", parameters={"course": PY}),
        ctx2,
        "随便看看",
    )
    assert ctx2.memory_focus_entry_ids == set()


def test_tool_result_notes_reach_the_reply_prompt() -> None:
    text = _describe_details(
        {
            "status": "ok",
            "memory_entries": [
                {
                    "entry_id": "exercise:11111111-1111-4111-8111-111111111111",
                    "note": "第五条附注：这是我当时看错的符号",
                }
            ],
        }
    )
    assert "第五条附注" in text
    assert "exercise:11111111-1111-4111-8111-111111111111" in text


def test_all_evaluations_excluded_is_not_reported_as_no_attempt(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    _, exercise = seed_exercise(seed)
    attempt = seed.attempt(exercise, correct=False)
    assert memory.delete("attempt", attempt) is True

    view = memory.summary(PY, days=30, limit=50)
    group = next(item for item in view.groups if item.label == "循环")
    assert group.no_attempt_count == 0
    assert group.excluded_evaluation_count == 1
    assert view.counts.excluded_evaluation == 1
    assert "评价已被你排除" in (group.suggestion or "")
    assert "还没有作答" not in (group.suggestion or "")

    material = material_block(view)
    assert "已有作答，不按未作答计" in material
    assert "还没有作答" not in material


def test_practice_count_deduplicates_exercises(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    for index in range(5):
        _, exercise = seed_exercise(seed, knowledge_point=f"知识点{index}")
        seed.attempt(exercise, correct=False, days_ago=2)
        seed.attempt(exercise, attempt_no=2, kind="supplement", correct=True, days_ago=1)

    view = memory.summary(PY, days=30, limit=50)
    assert view.counts.evidence == 5
    assert view.counts.practices == 5
    assert view.counts.attempts == 10