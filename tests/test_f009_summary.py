"""F009：摘要排序、措辞与课程隔离。

核心断言不是“文案好看”，而是：
- 顺序由证据强度决定，不由时间或字母顺序决定；
- 不出现掌握率、百分比或“你已经掌握”；
- 一次答对不会被说成已经掌握；
- 一门课的摘要里不出现另一门课的知识点。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.memory.service import LearningMemoryService
from tests.test_f009_evidence import FakePapers, build


def _seed_ranked(seed) -> None:
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    wrong = seed.exercise(conversation, problem, knowledge_point="循环")
    seed.attempt(wrong, correct=False, days_ago=3)
    revealed = seed.exercise(conversation, problem, knowledge_point="列表")
    seed.attempt(revealed, correct=True, after_reveal=True, days_ago=2)
    unfinished = seed.exercise(conversation, problem, knowledge_point="函数")
    seed.attempt(unfinished, correct=False, needs_supplement=True, days_ago=1)
    seed.exercise(conversation, problem, knowledge_point="字符串")
    reported = seed.problem(
        conversation, self_reported=True, days_ago=1
    )
    assert reported


def test_groups_are_ranked_by_evidence_strength(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    for label, kwargs in (
        ("循环", {"correct": False}),
        ("列表", {"correct": True, "after_reveal": True}),
        ("函数", {"correct": False, "needs_supplement": True}),
        ("字符串", {"correct": True}),
    ):
        exercise = seed.exercise(conversation, problem, knowledge_point=label)
        seed.attempt(exercise, **kwargs)
    seed.exercise(conversation, problem, knowledge_point="元组")

    view = service.summary("Python语言程序设计", days=30, limit=50)
    order = [group.label for group in view.groups]
    assert order[:4] == ["循环", "列表", "函数", "元组"]
    assert order[-1] == "字符串"
    assert len(view.suggestions) <= 3


def test_suggestions_describe_evidence_not_mastery(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    wrong = seed.exercise(conversation, problem, knowledge_point="循环")
    seed.attempt(wrong, correct=True)
    revealed = seed.exercise(conversation, problem, knowledge_point="列表")
    seed.attempt(revealed, correct=True, after_reveal=True)

    view = service.summary("Python语言程序设计", days=30, limit=50)
    text = "\n".join(view.suggestions)
    assert "循环" in text and "一次正确不代表已经掌握" in text
    assert "看过解答后" in text
    for banned in ("掌握率", "百分比", "%", "已完全掌握"):
        assert banned not in text
        assert banned not in view.range_text
    # 一次答对只能提醒确认，不能变成“已掌握”。
    for group in view.groups:
        if group.label == "循环":
            assert "一次正确不代表已经掌握" in (group.suggestion or "")
            assert "再做一道变式题确认" in (group.suggestion or "")


def test_course_summary_is_isolated_between_courses(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    python_conversation = seed.conversation()
    math_conversation = seed.conversation()
    python_problem = seed.problem(
        python_conversation,
        paper_id="py-2023-2024-s2-final",
        source_type="registered_paper",
    )
    math_problem = seed.problem(
        math_conversation, paper_id="math-2024-final", source_type="registered_paper"
    )
    python_exercise = seed.exercise(python_conversation, python_problem, knowledge_point="循环")
    seed.attempt(python_exercise, correct=False)
    math_exercise = seed.exercise(math_conversation, math_problem, knowledge_point="极限")
    seed.attempt(math_exercise, correct=False)

    python_view = service.summary("Python语言程序设计", days=30, limit=50)
    math_view = service.summary("高等数学", days=30, limit=50)
    assert {entry.knowledge_points[0] for entry in python_view.entries} == {"循环"}
    assert {entry.knowledge_points[0] for entry in math_view.entries} == {"极限"}
    assert "极限" not in json.dumps(python_view.model_dump(mode="json"), ensure_ascii=False)
    assert "循环" not in json.dumps(math_view.model_dump(mode="json"), ensure_ascii=False)

    courses = service.courses()
    assert {item.label for item in courses.items} == {"Python语言程序设计", "高等数学"}


def test_unanswered_exercises_do_not_outrank_real_evidence(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    for label in ("甲", "乙", "丙"):
        seed.exercise(conversation, problem, knowledge_point=label, days_ago=1)
    wrong = seed.exercise(conversation, problem, knowledge_point="循环", days_ago=5)
    seed.attempt(wrong, correct=False, days_ago=5)

    view = service.summary("Python语言程序设计", days=30, limit=50)
    assert view.groups[0].label == "循环"
    assert view.groups[0].wrong_count == 1


def test_material_respects_length_and_carries_range(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    for index in range(12):
        exercise = seed.exercise(
            conversation, problem, knowledge_point=f"知识点{index}", days_ago=index
        )
        seed.attempt(exercise, correct=False, days_ago=index)

    material = service.reply_material("Python语言程序设计") or ""
    assert len(material) <= 1300
    assert "近 30 天" in material
    assert "条证据" in material
    assert "掌握率" not in material
    assert "百分比" not in material
    assert "不要下“已掌握”的结论" in material

    view = service.summary("Python语言程序设计", days=30, limit=50)
    assert len(view.entries) <= 50
    assert len(view.suggestions) <= 3


def test_scheduler_context_is_compact_and_names_next_step(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(conversation, problem, knowledge_point="循环")
    seed.attempt(exercise, correct=False)

    line = service.scheduler_context("Python语言程序设计")
    assert line.startswith("【学习记忆】")
    assert "循环" in line
    assert len(line) <= 400
    assert "掌握率" not in line


def test_no_course_means_no_cross_course_summary(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    seed.exercise(conversation, problem, knowledge_point="循环")

    view = service.summary(None, days=30, limit=50)
    assert view.entries == []
    assert "先选一门课" in view.message


def test_duplicate_labels_within_entry_count_once(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(
        conversation, problem, knowledge_point="循环，循环、循环 / 循环"
    )
    seed.attempt(exercise, correct=False)

    view = service.summary("Python语言程序设计", days=30, limit=50)
    assert [group.label for group in view.groups] == ["循环"]
    assert view.groups[0].wrong_count == 1
    assert len(view.groups[0].entry_ids) == 1


def test_excluded_entries_do_not_come_back(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(conversation, problem, knowledge_point="循环")
    seed.attempt(exercise, correct=False)

    assert service.delete("exercise", exercise, reason="不是我的记录") is True
    after = service.summary("Python语言程序设计", days=30, limit=50)
    assert after.counts.evidence == 0
    assert after.counts.excluded == 1
    assert service.delete("exercise", exercise) is False

    # 重新创建服务（模拟重启/刷新）也不能让旧记忆复活。
    restarted = LearningMemoryService(
        service.db_path,
        papers=FakePapers(),
        taxonomy_path=service._taxonomy_path,
        current_file=service._current_file,
    )
    again = restarted.summary("Python语言程序设计", days=30, limit=50)
    assert again.counts.evidence == 0