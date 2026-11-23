"""个人资料、可读性报告、复习清单与引用模型。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, computed_field

GuideCategory = Literal["concept", "formula", "method", "pitfall"]
GuideStatus = Literal["generating", "ready", "failed"]
ArtifactInputMode = Literal["full", "recognize", "partial"]


class MaterialSummary(BaseModel):
    id: str
    display_name: str
    file_sha256: str
    size_bytes: int
    page_count: int
    dedicated_conversation_id: str | None = None
    created_at: datetime
    updated_at: datetime


    @computed_field
    @property
    def is_exam(self) -> bool:
        return bool(re.search(r"试卷|考卷|真题|考试题|期中.{0,4}卷|期末.{0,4}卷", self.display_name))


class MaterialListResponse(BaseModel):
    items: list[MaterialSummary]


class PageReadability(BaseModel):
    page_no: int
    char_count: int
    scanned_suspect: bool = False
    garbled_ratio: float = 0.0
    formula_warning: bool = False
    warnings: list[str] = Field(default_factory=list)


class MaterialPageStatus(BaseModel):
    page_no: int
    status: Literal["unchecked", "usable", "pending_ocr", "ocr_failed", "excluded"]
    source: Literal["pdf_text", "ocr"] | None = None
    version: str | None = None
    needs_review: bool = False
    reason: str | None = None


class ReadabilityReport(BaseModel):
    pages: list[MaterialPageStatus] = Field(default_factory=list)
    material_id: str
    start_page: int
    end_page: int
    sufficient: bool
    reason: str
    recommendation: str | None = None
    total_chars: int = 0
    affected_pages: list[PageReadability] = Field(default_factory=list)


class VerifiedCitation(BaseModel):
    page_no: int
    quote: str
    verified: Literal[True] = True


class GuideItemView(BaseModel):
    id: str
    sort_order: int
    category: GuideCategory
    title: str
    source_excerpt: str
    ai_explanation: str
    practice_prompt: str
    verified_citations: list[VerifiedCitation] = Field(default_factory=list)


class GuideSummary(BaseModel):
    id: str
    material_id: str
    start_page: int
    end_page: int
    rule_version: str
    conversation_id: str | None = None
    status: GuideStatus
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ReviewGuideView(BaseModel):
    id: str
    material_id: str
    file_sha256: str
    start_page: int
    end_page: int
    rule_version: str
    conversation_id: str | None = None
    status: GuideStatus
    reused: bool = False
    readability: ReadabilityReport | None = None
    items: list[GuideItemView] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class MaterialDetailResponse(BaseModel):
    material: MaterialSummary
    guides: list[GuideSummary] = Field(default_factory=list)
    artifacts: list[ArtifactSummary] = Field(default_factory=list)


class MaterialPageView(BaseModel):
    material_id: str
    page_no: int
    source: Literal["pdf_text", "ocr"] = "pdf_text"
    char_count: int
    scanned_suspect: bool
    garbled_ratio: float
    formula_warning: bool
    warnings: list[str] = Field(default_factory=list)
    extracted_text: str


class DeleteImpactView(BaseModel):
    material_id: str
    display_name: str
    guide_count: int
    artifact_count: int = 0
    quiz_attempt_count: int = 0
    graph_note_count: int = 0
    conversation_id: str | None = None
    linked_conversation_count: int = 0
    preserves_regular_conversations: bool = True
    exercise_count: int
    mistake_count: int
    attachment_count: int


class DeleteMaterialResponse(BaseModel):
    deleted: bool = True
    material_id: str


class StudyFromItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["explain", "exercise"]


class StudyFromItemResponse(BaseModel):
    conversation_id: str
    mode: Literal["explain", "exercise"]
    guide_id: str
    item_id: str
    prompt: str


class ReadabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)


class CreateGuideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    request_id: str | None = Field(default=None, max_length=120)
    course_hint: str | None = Field(default=None, max_length=80)


# ---------------------------------------------------------------------------
# F019 第二阶段新版：ExamPass 完整复习产物、测试与图谱笔记模型
# ---------------------------------------------------------------------------

ArtifactStatus = Literal["generating", "ready", "failed"]
OutputKind = Literal["notes", "quiz", "graph"]
OutputStatus = Literal["none", "generating", "ready", "failed"]
QuizQuestionType = Literal["choice", "multi", "tf", "fill", "calc", "code", "short"]
QuizGradingStatus = Literal["auto_graded", "pending_review", "evaluated", "unanswered"]


class QuizAttemptView(BaseModel):
    question_id: str
    user_answer: object | None = None
    is_correct: bool | None = None
    score: float | None = None
    max_points: float
    grading_status: QuizGradingStatus
    feedback: str | None = None
    revealed_solution_at: datetime | None = None
    answered_after_revealed: bool = False
    first_is_correct: bool | None = None
    first_score: float | None = None
    first_grading_status: QuizGradingStatus | None = None
    updated_at: datetime


class QuizQuestionPublicView(BaseModel):
    id: str
    sort_order: int
    kc_id: str
    q_type: QuizQuestionType
    stem: str
    options: list[str] | None = None
    pitfall: str = ""
    cognitive: str = ""
    difficulty: str = ""
    points: float
    source_pages: list[int] = Field(default_factory=list)
    attempt: QuizAttemptView | None = None
    # 答案与解析边界：仅当已作答判分或用户主动查看解析后才下发
    revealed_answer: object | None = None
    revealed_explanation: str | None = None


class ArtifactCoverage(BaseModel):
    input_pages: list[MaterialPageStatus]
    included_pages: list[int]
    missing_pages: list[int]
    review_pages: list[int]
    excluded_pages: list[int]
    partial: bool


def describe_artifact_input(
    coverage: ArtifactCoverage | None, *, quiz_question_count: int | None = None
) -> str:
    text = ""
    if coverage is not None:
        included = len(coverage.included_pages)
        if coverage.missing_pages:
            text = f"本次使用 {included} 页正文，还有 {len(coverage.missing_pages)} 页未纳入。"
        else:
            text = f"所选范围内的 {included} 页正文均已纳入。"
        if coverage.excluded_pages:
            excluded = "、".join(str(page) for page in coverage.excluded_pages)
            text += f"第 {excluded} 页已确认无需复习。"
    if quiz_question_count is not None:
        text += f"测试按知识点抽题，共 {quiz_question_count} 题。"
    return text


class ArtifactSummary(BaseModel):
    coverage: ArtifactCoverage | None = None
    id: str
    material_id: str
    file_sha256: str
    start_page: int
    end_page: int
    rule_version: str
    course_hint: str = ""
    title: str
    is_sample: bool = False
    status: ArtifactStatus
    current_step: str = "pending"
    requested_outputs: list[OutputKind] = Field(default_factory=lambda: ["notes", "quiz", "graph"])
    notes_status: OutputStatus = "none"
    notes_error_code: str | None = None
    notes_error_message: str | None = None
    quiz_status: OutputStatus = "none"
    quiz_error_code: str | None = None
    quiz_error_message: str | None = None
    graph_status: OutputStatus = "none"
    graph_error_code: str | None = None
    graph_error_message: str | None = None
    conversation_id: str | None = None
    question_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ArtifactOutputCoverage(BaseModel):
    cited_pages: list[int] = Field(default_factory=list)
    missing_knowledge_points: list[str] = Field(default_factory=list)
    uncited_pages: list[int] = Field(default_factory=list)
    partial: bool = False


class KnowledgePointIndex(BaseModel):
    label: str
    importance: str = ""
    module: str = ""
    note_anchor: str | None = None
    graph_node_id: str | None = None
    question_ids: list[str] = Field(default_factory=list)
    pages: list[int] = Field(default_factory=list)
    deps: list[str] = Field(default_factory=list)
    dependents: list[str] = Field(default_factory=list)


class ArtifactDetailView(BaseModel):
    coverage: ArtifactCoverage | None = None
    output_coverage: dict[OutputKind, ArtifactOutputCoverage] = Field(default_factory=dict)
    id: str
    material_id: str
    file_sha256: str
    start_page: int
    end_page: int
    rule_version: str
    course_hint: str = ""
    title: str
    is_sample: bool = False
    status: ArtifactStatus
    current_step: str = "pending"
    requested_outputs: list[OutputKind] = Field(default_factory=lambda: ["notes", "quiz", "graph"])
    notes_status: OutputStatus = "none"
    notes_error_code: str | None = None
    notes_error_message: str | None = None
    quiz_status: OutputStatus = "none"
    quiz_error_code: str | None = None
    quiz_error_message: str | None = None
    graph_status: OutputStatus = "none"
    graph_error_code: str | None = None
    graph_error_message: str | None = None
    reused: bool = False
    conversation_id: str | None = None
    skeleton: dict[str, object] | None = None
    notes_html: str = ""
    graph_tree: dict[str, object] | None = None
    coverage_warnings: list[str] = Field(default_factory=list)
    questions: list[QuizQuestionPublicView] = Field(default_factory=list)
    graph_notes: dict[str, str] = Field(default_factory=dict)
    kc_index: dict[str, KnowledgePointIndex] = Field(default_factory=dict)
    kc_notes: dict[str, str] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ImportSampleArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_name: Literal["exampass-physics"] = "exampass-physics"


class CreateArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage_mode: ArtifactInputMode = "full"

    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    course_hint: str | None = Field(default=None, max_length=80)
    outputs: list[OutputKind] = Field(
        default_factory=lambda: ["notes", "quiz", "graph"],
        min_length=1,
        max_length=3,
    )
    conversation_id: str | None = Field(default=None, max_length=80)


class ReadabilityOverview(BaseModel):
    pages: list[MaterialPageStatus] = Field(default_factory=list)
    material_id: str
    page_count: int
    readable_pages: int
    total_chars: int = 0
    scanned_pages: list[int] = Field(default_factory=list)
    ocr_pages: list[int] = Field(default_factory=list)
    formula_warning_pages: list[int] = Field(default_factory=list)
    checked_pages: list[int] = Field(default_factory=list)
    sufficient: bool
    summary: str
    available_outputs: list[OutputKind] = Field(default_factory=list)


MAX_OCR_PAGES_PER_REQUEST = 8


class MaterialOcrRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: list[StrictInt] = Field(min_length=1, max_length=MAX_OCR_PAGES_PER_REQUEST)


class MaterialOcrFailure(BaseModel):
    page_no: int
    message: str


class MaterialOcrResponse(BaseModel):
    recognized_pages: list[int]
    excluded_pages: list[int] = Field(default_factory=list)
    reused_pages: list[int]
    skipped_pages: list[int]
    failures: list[MaterialOcrFailure]
    readability: ReadabilityOverview | None


class ConversationMaterialItem(BaseModel):
    material: MaterialSummary
    readability: ReadabilityOverview | None = None
    artifacts: list[ArtifactSummary] = Field(default_factory=list)
    attached_at: datetime


class ConversationMaterialsResponse(BaseModel):
    conversation_id: str
    items: list[ConversationMaterialItem] = Field(default_factory=list)


class ConversationMaterialUploadResponse(BaseModel):
    conversation_id: str
    item: ConversationMaterialItem
    assistant_notice: str
    turn_request_id: str | None = None


class MaterialLibraryItem(BaseModel):
    material: MaterialSummary
    readability: ReadabilityOverview | None = None
    guides: list[GuideSummary] = Field(default_factory=list)
    artifacts: list[ArtifactSummary] = Field(default_factory=list)
    conversation_ids: list[str] = Field(default_factory=list)


class MaterialLibraryResponse(BaseModel):
    items: list[MaterialLibraryItem] = Field(default_factory=list)


class ConversationDeleteImpactView(BaseModel):
    conversation_id: str
    title: str
    linked_material_count: int = 0
    artifact_count: int = 0
    materials_still_accessible: bool = True
    summary: str


class QuizAttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_answer: object


class GraphNoteView(BaseModel):
    artifact_id: str
    node_id: str
    note_text: str
    updated_at: datetime | None = None


class GraphNoteUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note_text: str = Field(max_length=10000)


class StudyFromArtifactQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["explain", "evaluate"] = "explain"
    conversation_id: str | None = Field(default=None, max_length=80)


class StudyFromArtifactQuestionResponse(BaseModel):
    conversation_id: str
    artifact_id: str
    question_id: str
    mode: Literal["explain", "evaluate"]
    prompt: str


class StudyFromArtifactKcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["explain", "exercise"] = "explain"
    conversation_id: str | None = Field(default=None, max_length=80)


class StudyFromArtifactKcResponse(BaseModel):
    conversation_id: str
    artifact_id: str
    kc_id: str
    mode: Literal["explain", "exercise"]
    prompt: str


class MaterialProgressItem(BaseModel):
    task_id: str
    conversation_id: str
    material_id: str
    material_name: str
    kind: Literal["generate", "ocr"]
    start_page: int
    end_page: int
    outputs: list[OutputKind] = Field(default_factory=list)
    stage: str
    stage_label: str
    ocr_done: int = 0
    ocr_total: int = 0
    ocr_failed_pages: list[int] = Field(default_factory=list)
    updated_at: datetime


class ConversationMaterialProgressResponse(BaseModel):
    conversation_id: str
    items: list[MaterialProgressItem] = Field(default_factory=list)
