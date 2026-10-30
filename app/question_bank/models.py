"""查题服务的对外数据模型。

公开检索结果只带“答案是否存在”的元信息；原答案正文、参考代码和旁注
只能通过显式 `detail(include_...)` 取得，避免查题卡片泄露答案。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.question_bank.config import MAX_BROWSE_LIMIT

QuestionType = Literal["choice", "fill", "code"]
ExamType = Literal["midterm", "final"]
SearchStatus = Literal["ok", "no_match", "bank_not_built", "bank_unavailable"]
SearchMode = Literal["hybrid", "keyword_only", "vector_only", "browse"]
ReviewStatus = Literal["candidate", "approved", "rejected", "quarantined"]
AnswerState = Literal["original", "missing", "pending_review", "conflict"]


class Option(BaseModel):
    label: str
    text: str


class FormulaRef(BaseModel):
    text: str
    page: str | int | None = None


class AssetRef(BaseModel):
    asset_id: str
    page: int | None = None
    bbox: list[float] = Field(default_factory=list)
    description: str | None = None


class PageRefs(BaseModel):
    statement: list[int] = Field(default_factory=list)
    answer: list[int] = Field(default_factory=list)


class AnswerSummary(BaseModel):
    state: AnswerState
    available: bool
    label: str | None = None


class ReviewSummary(BaseModel):
    status: ReviewStatus
    visual: str
    approved: bool
    stale: bool = False


class KnowledgePointRef(BaseModel):
    slug: str
    name: str
    status: Literal["candidate", "verified", "rejected"]


class PaperRef(BaseModel):
    paper_id: str
    title: str
    course: str
    exam_type: ExamType | None = None
    academic_year: str | None = None


class QuestionSummary(BaseModel):
    question_id: str
    paper: PaperRef
    question_type: QuestionType
    number_label: str
    statement: str
    content_sha256: str = ""
    options: list[Option] = Field(default_factory=list)
    code_lines: list[str] = Field(default_factory=list)
    blank_marks: list[int] = Field(default_factory=list)
    formulas: list[FormulaRef] = Field(default_factory=list)
    assets: list[AssetRef] = Field(default_factory=list)
    pages: PageRefs
    knowledge_points: list[KnowledgePointRef] = Field(default_factory=list)
    answer: AnswerSummary
    review: ReviewSummary
    matched_by: Literal["bm25", "vector", "both", "browse"] = "bm25"
    rank: int = 0


class AnswerSource(BaseModel):
    kind: str
    text: str | None = None
    detail: str | None = None
    pairing_verified: bool | None = None
    pages: list[int] = Field(default_factory=list)


class AnswerDetail(BaseModel):
    state: AnswerState
    text: str | None = None
    note: str | None = None
    origin: str | None = None
    label: str | None = None
    sources: list[AnswerSource] = Field(default_factory=list)


class IndependentSolution(BaseModel):
    author: str | None = None
    text: str
    note: str | None = None


class CodeBlock(BaseModel):
    block_id: str | None = None
    method: int | None = None
    method_label: str | None = None
    column: int | None = None
    kind: str | None = None
    name: str | None = None
    text: str
    page: int | None = None
    pages: list[int] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class CodeAnnotation(BaseModel):
    role: str | None = None
    text: str
    page: int | None = None


class QuestionDetail(QuestionSummary):
    answer_detail: AnswerDetail | None = None
    independent_solutions: list[IndependentSolution] = Field(default_factory=list)
    reference_code: str | None = None
    reference_code_pages: list[int] = Field(default_factory=list)
    code_blocks: list[CodeBlock] = Field(default_factory=list)
    code_annotations: list[CodeAnnotation] = Field(default_factory=list)
    code_examples: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    """一次查题请求；课程必填，其余条件都可缺省。"""

    model_config = ConfigDict(extra="forbid")

    course: str
    query: str = ""
    knowledge_points: list[str] = Field(default_factory=list)
    question_type: QuestionType | None = None
    exam_type: ExamType | None = None
    paper_id: str | None = None
    number_label: str | None = Field(default=None, pattern=r"^[0-9]+$", max_length=6)
    # 分页位置：只对整卷浏览（没有相关性查询词）生效，用来“继续看下一批”。
    # 相关性检索永远从第 0 条开始返回排名结果。
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=MAX_BROWSE_LIMIT)
    preview_candidates: bool = False

    @field_validator("course")
    @classmethod
    def _course_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("course 不能为空")
        return value

    @field_validator("query")
    @classmethod
    def _query_stripped(cls, value: str) -> str:
        return value.strip()

    @field_validator("knowledge_points")
    @classmethod
    def _kp_stripped(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]

    @field_validator("paper_id")
    @classmethod
    def _paper_stripped(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class CourseRef(BaseModel):
    input: str
    canonical: str | None
    known: bool


class BuildInfo(BaseModel):
    build_id: str
    source_version: str
    index_version: str
    created_at: str | None = None


class Degradation(BaseModel):
    code: str
    message: str


class SearchDiagnostics(BaseModel):
    elapsed_ms: float = 0.0
    bm25_ms: float = 0.0
    vector_ms: float = 0.0
    bm25_candidates: int = 0
    vector_candidates: int = 0
    filter_violations: int = 0
    gate: dict[str, Any] = Field(default_factory=dict)
    cold_start: bool = False


class SearchResponse(BaseModel):
    status: SearchStatus
    mode: SearchMode
    degraded: bool = False
    degradation: Degradation | None = None
    message: str | None = None
    course: CourseRef
    build: BuildInfo | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    preview: bool = False
    matched_total: int | None = None
    offset: int = 0
    returned: int = 0
    # 下一批的起始位置；``None`` 表示已经没有更多（不是“未知”）。
    next_offset: int | None = None
    items: list[QuestionSummary] = Field(default_factory=list)
    diagnostics: SearchDiagnostics = Field(default_factory=SearchDiagnostics)


class CourseCapability(BaseModel):
    course: str
    known: bool
    status: Literal["ready", "catalog_only", "unknown", "error", "unreviewed"]
    question_count: int = 0
    searchable_count: int = 0
    approved_count: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    answer_coverage: dict[str, int] = Field(default_factory=dict)
    verified_label_count: int = 0
    build_id: str | None = None
    source_version: str | None = None
    verified: bool = False
    reason: str | None = None
    paper_count: int | None = None


class BankStatus(BaseModel):
    available: bool
    build: BuildInfo | None = None
    question_count: int = 0
    fragment_count: int = 0
    searchable_count: int = 0
    degraded: bool = False
    degradation: Degradation | None = None
    message: str | None = None
