"""检索目标的数据结构。

F010 D 完成后，生产侧只接收模型参数与工作区事实；原话解析移到
``evals/f010_d/legacy_intent.py``，仅供冻结评测与测试使用。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchIntent:
    course: str | None
    query: str
    question_type: str | None = None
    exam_type: str | None = None
    paper_id: str | None = None
    number_label: str | None = None
    knowledge_points: tuple[str, ...] = ()
    limit: int = 3
    offset: int = 0
    auto_explain: bool = False
    next_card: bool = False
    page_exhausted: bool = False
    page_error: str | None = None
    paper_unresolved: bool = False