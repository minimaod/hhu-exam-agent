"""F009 测试用的最小学习记录播种器。

只写 F009 需要的表，字段取真实 schema（v6）。所有时间相对“现在”生成，
这样接口层用真实时钟跑 30 天窗口时也不会因为固定日期而过期。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


class Seed:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def conversation(self, *, title: str = "学习记忆用例", days_ago: float = 0) -> str:
        conversation_id = _new_id()
        moment = _iso(_now() - timedelta(days=days_ago))
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, title, moment, moment),
            )
            connection.commit()
        return conversation_id

    def problem(
        self,
        conversation_id: str,
        *,
        root_problem_id: str | None = None,
        version: int = 1,
        statement: str = "求列表中出现次数最多的元素",
        paper_id: str | None = None,
        source_type: str = "student_text",
        self_reported: bool = False,
        explanation_points: list[dict[str, str]] | None = None,
        days_ago: float = 0,
        active: bool = True,
    ) -> str:
        problem_id = _new_id()
        moment = _iso(_now() - timedelta(days=days_ago))
        explanation = (
            {"knowledge_points": explanation_points}
            if explanation_points is not None
            else None
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO problems (id, conversation_id, root_problem_id, version, "
                "statement_text, status, clarity, source_type, paper_id, source_title, "
                "explanation_json, self_reported_understood, active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'explained', 'clear', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    problem_id,
                    conversation_id,
                    root_problem_id or problem_id,
                    version,
                    statement,
                    source_type,
                    paper_id,
                    None,
                    json.dumps(explanation, ensure_ascii=False) if explanation else None,
                    1 if self_reported else 0,
                    1 if active else 0,
                    moment,
                    moment,
                ),
            )
            connection.commit()
        return problem_id

    def exercise(
        self,
        conversation_id: str,
        problem_id: str,
        *,
        root_problem_id: str | None = None,
        knowledge_point: str = "循环的条件与终止条件",
        statement: str = "写出统计出现次数最多的元素的代码",
        reference: str = "参考解答：这是不该出现在学习记忆里的答案正文",
        status: str = "submitted",
        days_ago: float = 0,
    ) -> str:
        exercise_id = _new_id()
        moment = _iso(_now() - timedelta(days=days_ago))
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO exercises (id, conversation_id, problem_id, root_problem_id, "
                "statement_text, reference_solution_text, knowledge_point, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    exercise_id,
                    conversation_id,
                    problem_id,
                    root_problem_id or problem_id,
                    statement,
                    reference,
                    knowledge_point,
                    status,
                    moment,
                    moment,
                ),
            )
            connection.commit()
        return exercise_id

    def attempt(
        self,
        exercise_id: str,
        *,
        attempt_no: int = 1,
        kind: str = "initial",
        correct: bool | None = None,
        needs_supplement: bool = False,
        after_reveal: bool = False,
        independence: str = "independent",
        evaluation: bool = True,
        days_ago: float = 0,
    ) -> str:
        attempt_id = _new_id()
        moment = _iso(_now() - timedelta(days=days_ago))
        payload = None
        if evaluation:
            payload = json.dumps(
                {
                    "is_correct": correct,
                    "final_answer_assessment": "结论对" if correct else "结论不对",
                    "step_by_step_analysis": "逐步分析",
                    "guidance": "先判断终止条件",
                    "needs_supplement": needs_supplement,
                    "independence_assessment": independence,
                    "independence_note": None,
                },
                ensure_ascii=False,
            )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO exercise_attempts (id, exercise_id, attempt_no, kind, "
                "student_answer_text, student_solution_text, answered_after_revealed, "
                "evaluation_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    exercise_id,
                    attempt_no,
                    kind,
                    "我的答案",
                    "我的过程",
                    1 if after_reveal else 0,
                    payload,
                    moment,
                    moment,
                ),
            )
            connection.commit()
        return attempt_id

    def question_source(
        self,
        exercise_id: str,
        *,
        question_id: str = "py-001",
        paper_id: str = "py-2023-2024-s2-final",
        build_id: str = "iv-0123456789abcdef",
        number_label: str = "12",
        knowledge_points: list[dict[str, str]] | None = None,
        status: str = "submitted",
    ) -> None:
        snapshot = {
            "id": question_id,
            "number_label": number_label,
            "status": status,
            "knowledge_points": knowledge_points or [],
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO exercise_question_sources (exercise_id, question_id, paper_id, "
                "build_id, source_version, content_sha256, snapshot_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    exercise_id,
                    question_id,
                    paper_id,
                    build_id,
                    "1",
                    "0" * 64,
                    json.dumps(snapshot, ensure_ascii=False),
                    _iso(_now()),
                ),
            )
            connection.commit()

    def mistake_record(
        self, conversation_id: str, problem_id: str, *, title: str = "统计出现次数"
    ) -> str:
        record_id = _new_id()
        moment = _iso(_now())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO mistake_records (id, conversation_id, problem_id, "
                "root_problem_id, problem_version, request_id, title, statement_text, "
                "source_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    conversation_id,
                    problem_id,
                    problem_id,
                    _new_id(),
                    title,
                    "求列表中出现次数最多的元素",
                    "{}",
                    moment,
                    moment,
                ),
            )
            connection.commit()
        return record_id


def plain_course_of(paper_id: str | None) -> str | None:
    """测试用极简目录：两张 Python 卷、一张高数卷。"""

    return {
        "py-2023-2024-s2-final": "Python语言程序设计",
        "py-2024-2025-s1-final": "Python语言程序设计",
        "math-2024-final": "高等数学",
    }.get(paper_id or "")