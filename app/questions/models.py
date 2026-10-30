"""P4 题目卡片、检索结果、能力与选中记录的数据模型。

公开卡片只含题面与“答案是否可用”；答案正文放在 ``answer_detail``，
只有显式查看或讲解时才由服务端填充。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.question_bank.models import (
    AnswerDetail,
    CodeAnnotation,
    CodeBlock,
    IndependentSolution,
)

QuestionType = Literal["choice", "fill", "code"]
AnswerState = Literal["original", "missing", "pending_review", "conflict"]
SearchStatus = Literal[
    "ok",
    "no_match",
    "unreviewed",
    "bank_not_built",
    "bank_unavailable",
    "degraded",
]
CapabilityStatus = Literal[
    "ready",
    "catalog_only",
    "unreviewed",
    "not_built",
    "unknown",
    "error",
]


class QuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    text: str


class QuestionFormula(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    page: int | None = None


class QuestionAssetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    page: int | None = None
    description: str | None = None
    url: str


class QuestionPages(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: list[int] = Field(default_factory=list)
    answer: list[int] = Field(default_factory=list)


class QuestionKnowledgePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    status: str = "candidate"


class AnswerAvailability(BaseModel):
    """只表示答案状态与可用性，不携带答案正文。"""

    model_config = ConfigDict(extra="forbid")

    state: AnswerState
    available: bool = False


class QuestionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    approved: bool = False
    stale: bool = False


class QuestionSourceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str
    paper_title: str | None = None
    exam_type: str | None = None
    academic_year: str | None = None
    question_number: str
    download_url: str | None = None
    # 原卷 PDF 的答案页可能包含答案，下载入口必须带提醒。
    may_show_answer: bool = True


class QuestionSourceVisit(BaseModel):
    kind: Literal["statement", "answer"]
    page: int
    viewed_at: str
    may_show_answer: bool = True


class QuestionCard(BaseModel):
    """一道题的公开卡片；答案正文默认不出现。"""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    build_id: str = ""
    selection_id: str | None = None
    source_request_id: str | None = None
    source_url: str | None = None
    question_type: QuestionType
    number_label: str
    statement: str
    content_sha256: str = ""
    options: list[QuestionOption] = Field(default_factory=list)
    code_lines: list[str] = Field(default_factory=list)
    blank_marks: list[int] = Field(default_factory=list)
    formulas: list[QuestionFormula] = Field(default_factory=list)
    assets: list[QuestionAssetView] = Field(default_factory=list)
    pages: QuestionPages
    knowledge_points: list[QuestionKnowledgePoint] = Field(default_factory=list)
    answer: AnswerAvailability
    review: QuestionReview
    source: QuestionSourceView
    selected: bool = False
    answer_revealed: bool = False
    answer_revealed_at: str | None = None
    source_views: list[QuestionSourceVisit] = Field(default_factory=list)
    content_issues: list[str] = Field(default_factory=list)
    answer_url: str | None = None

    # 仅在显式查看答案或讲解后填充；不进入公开快照。
    answer_detail: AnswerDetail | None = None
    reference_code: str | None = None
    reference_code_pages: list[int] = Field(default_factory=list)
    code_blocks: list[CodeBlock] = Field(default_factory=list)
    code_annotations: list[CodeAnnotation] = Field(default_factory=list)
    code_examples: list[str] = Field(default_factory=list)
    independent_solutions: list[IndependentSolution] = Field(default_factory=list)

    def public_snapshot(self) -> dict[str, object]:
        """入轮/选中快照：去掉答案正文与临时 URL。"""

        return self.model_dump(
            mode="json",
            exclude={
                "answer_detail",
                "reference_code",
                "reference_code_pages",
                "code_blocks",
                "code_annotations",
                "code_examples",
                "independent_solutions",
                "answer_url",
                "selection_id",
                "source_request_id",
                "selected",
                "answer_revealed",
                "answer_revealed_at",
            },
        )


class QuestionSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SearchStatus
    message: str = ""
    cards: list[QuestionCard] = Field(default_factory=list)
    mode: str | None = None
    degraded: bool = False
    # 整卷浏览的“总量 / 本次范围 / 下一批位置”。``total`` 是符合条件的
    # 已审核题总数，不是本次返回的条数；``next_offset`` 为 None 表示已经到底。
    total: int | None = None
    offset: int = 0
    returned: int = 0
    next_offset: int | None = None
    paper_id: str | None = None
    paper_title: str | None = None
    question_type: str | None = None


class QuestionCapabilityView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course: str
    status: CapabilityStatus
    searchable: int = 0
    approved: int = 0
    message: str = ""


class ExerciseSourceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    paper_id: str
    paper_title: str | None = None
    number_label: str | None = None
    question_type: QuestionType | None = None
    build_id: str
    source_version: str
    content_sha256: str


class RecordSourceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    paper_id: str
    build_id: str
    source_version: str
    content_sha256: str


@dataclass(frozen=True)
class QuestionSelection:
    """学习库里的一次选中事实；snapshot 只含公开字段。"""

    id: str
    conversation_id: str
    request_id: str
    source_request_id: str
    question_id: str
    paper_id: str
    build_id: str
    source_version: str
    content_sha256: str
    question_type: str
    number_label: str
    snapshot: dict
    problem_id: str | None = None
    answer_revealed_at: str | None = None
    answer_reveal_source: str | None = None
    active: bool = False
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class EvidenceBundle:
    """一次答案/代码/题图回读的结果；status 供 UI 显示具体原因。"""

    status: Literal[
        "ok",
        "missing_answer",
        "source_changed",
        "source_pruned",
        "unavailable",
    ]
    answer_detail: AnswerDetail | None = None
    reference_code: str | None = None
    reference_code_pages: list[int] = field(default_factory=list)
    code_blocks: list[CodeBlock] = field(default_factory=list)
    code_annotations: list[CodeAnnotation] = field(default_factory=list)
    code_examples: list[str] = field(default_factory=list)
    independent_solutions: list[IndependentSolution] = field(default_factory=list)
    content_issues: list[str] = field(default_factory=list)
    message: str | None = None


@dataclass(frozen=True)
class AssetPayload:
    path: Path
    mime: str
    page: int | None
    description: str | None
    source_version: str | None = None
