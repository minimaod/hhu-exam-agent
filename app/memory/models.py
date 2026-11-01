"""F009：学习记忆的对外视图契约。

这一层只定义“能给学生看、能进正文材料”的字段，**不含任何答案正文**：
练习的参考解答、真题答案、独立解答、参考代码都不在本模块出现。证据投影
（``evidence``）从既有表读取事实，本模块负责把它们整理成稳定的结构。

标识约定：``entry_id`` / ``entry_key`` 直接复用原始记录主键，形如
``exercise:<uuid>``、``attempt:<uuid>``、``selfreport:<uuid>``。刷新、重启、
重新汇总都不会改变它，因此删除排除能一直命中同一条旧来源。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MemoryEntryKind = Literal["exercise", "attempt", "selfreport"]

# 一条证据（或一次作答）的判断标签。这里只描述“记录是什么”，
# 不表达“学生会不会”。
MemoryVerdict = Literal[
    "wrong_independent",
    "correct_independent",
    "after_reveal",
    "insufficient",
    "unclear",
    "self_report",
    "no_attempt",
]

MemoryValidity = Literal["ok", "superseded", "unverified", "unavailable"]
MemoryStatus = Literal["ok", "empty", "unavailable"]
MemoryCourseSource = Literal["user", "paper", "problem", "unknown"]

VERDICT_LABELS: dict[str, str] = {
    "wrong_independent": "独立作答被判错",
    "correct_independent": "独立作答正确",
    "after_reveal": "看过解答后对照作答",
    "insufficient": "过程不足，未确认对错",
    "unclear": "独立性未确认，不按独立对错统计",
    "self_report": "自述理解",
    "no_attempt": "还没有作答",
}

VALIDITY_NOTES: dict[str, str] = {
    "superseded": "原题已有更新版本，这条依据来自旧版本",
    "unverified": "题库当前不可用，无法核对这条依据的版本",
    "unavailable": "这条依据对应的内容已经无法读取",
}

_ENTRY_ID = re.compile(r"^(exercise|attempt|selfreport):([0-9a-fA-F-]{36})$")


def parse_entry_id(raw: str) -> tuple[MemoryEntryKind, str] | None:
    """把 ``exercise:<uuid>`` 拆成 (kind, source_id)；不合法返回 None。"""

    match = _ENTRY_ID.match((raw or "").strip())
    if match is None:
        return None
    return match.group(1), match.group(2).lower()  # type: ignore[return-value]


def build_entry_id(kind: str, source_id: str) -> str:
    return f"{kind}:{source_id}"


class MemoryAttemptView(BaseModel):
    """一次不可变作答尝试的判断结果。"""

    attempt_id: str
    attempt_no: int
    kind: str
    verdict: MemoryVerdict
    label: str
    is_correct: bool | None = None
    needs_supplement: bool = False
    answered_after_revealed: bool = False
    independence: str | None = None
    recorded_at: datetime
    excluded: bool = False


class MemoryEntryView(BaseModel):
    """一条学习证据；来源字段都能回到会话、题目版本、练习或作答。"""

    entry_id: str
    kind: MemoryEntryKind
    course: str | None = None
    course_source: MemoryCourseSource = "unknown"
    knowledge_points: list[str] = Field(default_factory=list)
    knowledge_point_slugs: list[str | None] = Field(default_factory=list)
    knowledge_points_normalized: list[bool] = Field(default_factory=list)
    knowledge_points_truncated: bool = False
    statement_excerpt: str | None = None
    verdict: MemoryVerdict
    first_verdict: MemoryVerdict | None = None
    recorded_at: datetime
    first_at: datetime | None = None
    wrong_at: datetime | None = None
    validity: MemoryValidity = "ok"
    validity_note: str | None = None
    note: str | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    attempts: list[MemoryAttemptView] = Field(default_factory=list)
    record_id: str | None = None
    excluded: bool = False


class MemoryGroupView(BaseModel):
    """按知识点分组的汇总；没有归一成功时保留原标签。"""

    label: str
    slug: str | None = None
    normalized: bool = False
    wrong_count: int = 0
    correct_count: int = 0
    after_reveal_count: int = 0
    insufficient_count: int = 0
    unclear_count: int = 0
    no_attempt_count: int = 0
    self_report_count: int = 0
    # 已有作答、但评价被学生全部排除：不能算“还没有作答”。
    excluded_evaluation_count: int = 0
    # 判错发生在统计窗口之前：可以展示，但不写成“近期判错”，也不驱动建议。
    earlier_wrong_count: int = 0
    entry_ids: list[str] = Field(default_factory=list)
    latest_at: datetime | None = None
    latest_wrong_at: datetime | None = None
    suggestion: str | None = None


class MemoryCounts(BaseModel):
    evidence: int = 0
    practices: int = 0
    questions: int = 0
    attempts: int = 0
    excluded: int = 0
    pending_course: int = 0
    wrong_independent: int = 0
    unclear: int = 0
    excluded_evaluation: int = 0
    # 判错早于本窗口；与窗口内判错分开，避免把历史写成“近期”。
    earlier_wrong: int = 0
    # 依据来自旧版本/旧题库版本：保留展示，但不计入统计与建议。
    superseded: int = 0
    unverified: int = 0


class MemoryRange(BaseModel):
    days: int = 30
    from_at: datetime | None = None
    to_at: datetime | None = None
    fallback: bool = False
    limit: int = 50


class LearningMemoryView(BaseModel):
    """页面与 Agent 共用的学习摘要。"""

    status: MemoryStatus = "ok"
    course: str | None = None
    range: MemoryRange = Field(default_factory=MemoryRange)
    range_text: str = ""
    counts: MemoryCounts = Field(default_factory=MemoryCounts)
    groups: list[MemoryGroupView] = Field(default_factory=list)
    entries: list[MemoryEntryView] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    pending_course_entries: list[MemoryEntryView] = Field(default_factory=list)
    message: str = ""


class MemoryCourseCount(BaseModel):
    course: str | None = None
    label: str
    evidence: int = 0


class MemoryCoursesView(BaseModel):
    items: list[MemoryCourseCount] = Field(default_factory=list)
    pending: int = 0


class MemoryCorrectionRequest(BaseModel):
    """用户纠正：课程归属、知识点或补充说明，至少提供一项。"""

    model_config = ConfigDict(extra="forbid")

    course: str | None = Field(default=None, max_length=40)
    knowledge_point: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("course", "knowledge_point", "note")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text

    def has_change(self) -> bool:
        # 课程允许显式写成空串，表示“改为待确认”，因此空串也算一次修改。
        return any(
            value is not None
            for value in (self.course, self.knowledge_point, self.note)
        )