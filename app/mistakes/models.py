"""错题记录的对外契约：列表、详情、修订与纠正事件。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.conversations.models import ExerciseView
from app.querying.models import ErrorCode
from app.tutoring.models import (
    AttachmentView,
    Explanation,
    RecordSummary,
    Source,
)

RecordCorrectionErrorCode = ErrorCode | Literal["STORAGE_UNAVAILABLE"]


class MistakeRecordListResponse(BaseModel):
    items: list[RecordSummary]
    total: int


class FollowupView(BaseModel):
    question: str
    answer: str
    created_at: str


class FeedbackView(BaseModel):
    text: str | None = None
    self_reported_understood: bool = False
    self_reported_at: str | None = None
    updated_at: str | None = None


class RevisionView(BaseModel):
    id: int
    request_id: str
    reason: Literal["correction", "retutor", "adopt_problem"]
    statement_text: str
    source: Source
    explanation: Explanation | None
    created_at: datetime


class ProblemRef(BaseModel):
    id: str
    version: int


class MistakeRecordDetail(BaseModel):
    id: str
    title: str
    conversation_id: str
    problem_id: str
    root_problem_id: str
    problem_version: int
    statement_text: str
    student_answer_text: str | None
    solution_text: str | None
    source: Source
    attachments: list[AttachmentView]
    explanation: Explanation | None
    followups: list[FollowupView]
    feedback: FeedbackView
    self_reported_understood: bool
    revisions: list[RevisionView]
    conversation_has_newer_version: bool
    newer_problem: ProblemRef | None
    exercises: list[ExerciseView] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class RecordUpdatedResult(BaseModel):
    kind: Literal["record_updated"] = "record_updated"
    record: MistakeRecordDetail
    revision_created: bool


class RecordNeedsClarificationResult(BaseModel):
    kind: Literal["record_needs_clarification"] = "record_needs_clarification"
    statement_text: str
    questions: list[str] = Field(min_length=1, max_length=5)
    unclear_points: list[str]


RecordCorrectionResult = RecordUpdatedResult | RecordNeedsClarificationResult


class RecordCorrectionProgressEvent(BaseModel):
    type: Literal["progress"] = "progress"
    record_id: str
    request_id: str
    stage: Literal["reading_problem", "checking_clarity", "explaining", "saving"]
    message: str


class RecordCorrectionResultEvent(BaseModel):
    type: Literal["result"] = "result"
    record_id: str
    request_id: str
    result: RecordCorrectionResult


class RecordCorrectionErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    record_id: str
    request_id: str
    code: RecordCorrectionErrorCode
    message: str


RecordCorrectionEvent = (
    RecordCorrectionProgressEvent
    | RecordCorrectionResultEvent
    | RecordCorrectionErrorEvent
)
