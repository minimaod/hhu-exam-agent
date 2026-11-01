"""F009：证据分类、去重单位、课程归属优先级与答案隔离。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.conversations.repository import ConversationRepository
from app.memory.service import LearningMemoryService
from tests.f009_support import Seed, plain_course_of


class FakePapers:
    """两门课的极简目录：用来验证“课程归属来自登记过的卷子”。"""

    DEFAULT = {
        "py-2023-2024-s2-final": "Python语言程序设计",
        "py-2024-2025-s1-final": "Python语言程序设计",
        "math-2024-final": "高等数学",
    }

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping if mapping is not None else dict(self.DEFAULT)

    def get_paper(self, paper_id: str):
        course = self.mapping.get(paper_id)
        if course is None:
            return None
        return SimpleNamespace(course=course, title=f"{paper_id} 真题")


def build(tmp_path: Path, *, taxonomy: Path | None = None) -> tuple[LearningMemoryService, Seed]:
    db = tmp_path / "study.sqlite3"
    ConversationRepository(db).initialize()
    service = LearningMemoryService(
        db,
        papers=FakePapers(),
        taxonomy_path=taxonomy or tmp_path / "absent-taxonomy.json",
        current_file=tmp_path / "absent-current.json",
    )
    return service, Seed(db)


def write_taxonomy(tmp_path: Path) -> Path:
    path = tmp_path / "knowledge-points.json"
    path.write_text(
        json.dumps(
            {
                "knowledge_points": [
                    {
                        "slug": "loops",
                        "name": "循环",
                        "definition": "循环结构",
                        "aliases": ["循环的条件与终止条件"],
                        "patterns": [],
                        "priority": 10,
                    },
                    {
                        "slug": "aggregation",
                        "name": "统计与聚合",
                        "aliases": [],
                        "patterns": [],
                        "priority": 5,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_attempts_are_classified_by_evidence_not_by_encouragement(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(conversation)
    cases = {
        "wrong": {"correct": False},
        "right": {"correct": True},
        "revealed": {"correct": True, "after_reveal": True},
        "supplement": {"correct": False, "needs_supplement": True},
        "unfinished": {"evaluation": False},
    }
    expected = {
        "wrong": "wrong_independent",
        "right": "correct_independent",
        "revealed": "after_reveal",
        "supplement": "insufficient",
        "unfinished": "insufficient",
    }
    for name, kwargs in cases.items():
        exercise = seed.exercise(conversation, problem, knowledge_point=name)
        seed.attempt(exercise, **kwargs)

    exercise = seed.exercise(conversation, problem, knowledge_point="未作答")
    assert exercise

    # 这些条目没有课程归属，但分类规则相同：只是不进课程摘要。
    from app.memory.evidence import collect_all, KnowledgeNormalizer

    with sqlite3.connect(service.db_path) as connection:
        connection.row_factory = sqlite3.Row
        bundle = collect_all(
            connection,
            paper_lookup=lambda paper_id: (None, None),
            normalizer=KnowledgeNormalizer(None),
            corrections={},
            exclusions=set(),
            current_build_id=None,
        )
    verdicts = {entry.knowledge_points[0]: entry.verdict for entry in bundle.entries}
    for label, verdict in expected.items():
        assert verdicts[label] == verdict
    assert verdicts["未作答"] == "no_attempt"


def test_one_exercise_is_one_unit_and_multiple_exercises_share_question(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    for index in range(3):
        exercise = seed.exercise(conversation, problem, knowledge_point="循环")
        seed.attempt(exercise, correct=index == 0)
        seed.attempt(exercise, attempt_no=2, kind="correction", correct=True)

    view = service.summary("Python语言程序设计", days=30, limit=50)
    assert view.counts.evidence == 3
    assert view.counts.questions == 1
    assert view.counts.attempts == 6
    assert "3 条证据" in view.range_text and "1 道题" in view.range_text
    assert view.entries[0].verdict in {"wrong_independent", "correct_independent"}


def test_course_precedence_correction_then_question_then_problem(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    python_conversation = seed.conversation()
    math_conversation = seed.conversation()
    unknown_conversation = seed.conversation()
    python_problem = seed.problem(
        python_conversation,
        paper_id="py-2023-2024-s2-final",
        source_type="registered_paper",
    )
    math_problem = seed.problem(
        math_conversation,
        paper_id="math-2024-final",
        source_type="registered_paper",
    )
    unknown_problem = seed.problem(unknown_conversation)

    from_source = seed.exercise(python_conversation, python_problem, knowledge_point="循环")
    seed.question_source(from_source, paper_id="py-2024-2025-s1-final")
    from_problem = seed.exercise(math_conversation, math_problem, knowledge_point="极限")
    unknown = seed.exercise(unknown_conversation, unknown_problem, knowledge_point="杂项")

    view = service.summary("Python语言程序设计", days=30, limit=50)
    assert [entry.entry_id for entry in view.entries] == [f"exercise:{from_source}"]
    assert view.counts.pending_course == 1

    # 真题关联的卷面课程优先于题目自身登记时的 paper_id。
    assert view.entries[0].course_source == "paper"
    assert view.entries[0].source["paper_id"] == "py-2024-2025-s1-final"

    # 没有真题关联时退回题目登记时带的卷子。
    math_view = service.summary("高等数学", days=30, limit=50)
    assert [entry.entry_id for entry in math_view.entries] == [f"exercise:{from_problem}"]
    assert math_view.entries[0].course_source == "problem"

    # 用户纠正优先于两种推导，并且能把待确认条目移进课程。
    from app.memory.models import MemoryCorrectionRequest

    service.correct(
        "exercise", unknown, MemoryCorrectionRequest(course="Python语言程序设计")
    )
    corrected = service.summary("Python语言程序设计", days=30, limit=50)
    assert {entry.entry_id for entry in corrected.entries} == {
        f"exercise:{from_source}",
        f"exercise:{unknown}",
    }
    assert corrected.counts.pending_course == 0
    corrected_entry = next(
        entry for entry in corrected.entries if entry.entry_id == f"exercise:{unknown}"
    )
    assert corrected_entry.course_source == "user"


def test_confirmed_course_is_never_reverse_inferred(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(conversation)
    seed.exercise(conversation, problem, knowledge_point="杂项")
    with sqlite3.connect(service.db_path) as connection:
        connection.execute(
            "INSERT INTO conversation_contexts (conversation_id, confirmed_course, updated_at) "
            "VALUES (?, ?, ?)",
            (conversation, "高等数学", "2026-11-01T00:00:00+00:00"),
        )
        connection.commit()

    python_view = service.summary("Python语言程序设计", days=30, limit=50)
    math_view = service.summary("高等数学", days=30, limit=50)
    assert python_view.counts.evidence == 0
    assert math_view.counts.evidence == 0
    assert python_view.counts.pending_course == 1


def test_knowledge_points_normalize_only_on_exact_alias(tmp_path: Path) -> None:
    taxonomy = write_taxonomy(tmp_path)
    service, seed = build(tmp_path, taxonomy=taxonomy)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(
        conversation, problem, knowledge_point="循环的条件与终止条件、自定义标签"
    )
    seed.attempt(exercise, correct=False)

    view = service.summary("Python语言程序设计", days=30, limit=50)
    entry = view.entries[0]
    assert entry.knowledge_points == ["循环", "自定义标签"]
    assert entry.knowledge_point_slugs == ["loops", None]
    assert entry.knowledge_points_normalized == [True, False]
    labels = {group.label: group.normalized for group in view.groups}
    assert labels["循环"] is True
    assert labels["自定义标签"] is False


def test_question_snapshot_labels_are_used_before_free_text(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(conversation, problem, knowledge_point="自由文本标签")
    seed.question_source(
        exercise,
        knowledge_points=[
            {"slug": "loops", "name": "循环", "status": "verified"},
            {"slug": "aggregation", "name": "统计与聚合", "status": "candidate"},
        ],
    )
    view = service.summary("Python语言程序设计", days=30, limit=50)
    entry = view.entries[0]
    assert entry.knowledge_points == ["循环", "统计与聚合"]
    assert entry.knowledge_point_slugs == ["loops", "aggregation"]
    assert entry.knowledge_points_normalized == [True, True]


def test_answer_text_never_enters_memory_views(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(
        conversation,
        problem,
        knowledge_point="循环",
        reference="参考解答：课堂上的标准答案正文",
    )
    seed.attempt(exercise, correct=False)
    view = service.summary("Python语言程序设计", days=30, limit=50)

    payload = json.dumps(view.model_dump(mode="json"), ensure_ascii=False)
    assert "参考解答" not in payload
    assert "标准答案正文" not in payload
    assert "我的答案" not in payload
    assert "我的过程" not in payload

    material = service.reply_material("Python语言程序设计") or ""
    assert "参考解答" not in material
    assert "标准答案正文" not in material


def test_superseded_problem_version_is_flagged(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    root = "root-superseded"
    first = seed.problem(
        conversation,
        root_problem_id=root,
        version=1,
        paper_id="py-2023-2024-s2-final",
        source_type="registered_paper",
        active=False,
    )
    seed.exercise(conversation, first, root_problem_id=root, knowledge_point="循环")
    seed.problem(
        conversation,
        root_problem_id=root,
        version=2,
        paper_id="py-2023-2024-s2-final",
        source_type="registered_paper",
    )

    view = service.summary("Python语言程序设计", days=30, limit=50)
    entry = next(entry for entry in view.entries if entry.verdict == "no_attempt")
    assert entry.validity == "superseded"
    assert "第 1 版" in (entry.validity_note or "")


def test_empty_and_unavailable_are_different(tmp_path: Path) -> None:
    from app.conversations.errors import ConversationStorageError

    service, seed = build(tmp_path)
    empty = service.summary("Python语言程序设计", days=30, limit=50)
    assert empty.status == "empty"
    assert "没有学习记录" in empty.message

    session = seed.conversation()
    seed.exercise(session, seed.problem(session), knowledge_point="循环")
    with sqlite3.connect(service.db_path) as connection:
        connection.execute("DROP TABLE memory_corrections")
        connection.commit()
    with pytest.raises(ConversationStorageError):
        service.summary("Python语言程序设计", days=30, limit=50)


def test_fallback_to_recent_five_is_labelled_not_recent(tmp_path: Path) -> None:
    service, seed = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    for _ in range(7):
        exercise = seed.exercise(
            conversation, problem, knowledge_point="循环", days_ago=200
        )
        seed.attempt(exercise, correct=False, days_ago=200)

    view = service.summary("Python语言程序设计", days=30, limit=50)
    assert view.status == "ok"
    assert view.counts.evidence == 5
    assert view.range.fallback is True
    assert "最近" in view.range_text and "没有记录" in view.range_text


def test_days_and_limit_are_clamped(tmp_path: Path) -> None:
    service, _ = build(tmp_path)
    assert service.summary("Python语言程序设计", days=0, limit=0).range.days >= 1
    assert service.summary("Python语言程序设计", days=10_000).range.days <= 365
    assert service.summary("Python语言程序设计", limit=10_000).range.limit <= 50