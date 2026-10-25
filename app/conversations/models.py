"""本地会话与查询/辅导轮次的对外契约。

ConversationSummary 只承载列表和接口需要的摘要；Turn 保存一轮输入、状态和
已完成结果。查询结果复用 F003 的 ResultEvent；辅导结果使用 tutoring 模块的
结构化联合。事件模型是 POST /turns 的 SSE 承诺，页面据此判断该轮是否真正结束。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.querying.models import ErrorCode, ResultEvent
from app.tutoring.models import AttachmentView, ProblemView, TutoringResult

TurnStatus = Literal["running", "completed", "failed", "interrupted"]
BusinessStatus = Literal["succeeded", "partial", "failed", "needs_input"]
RECOVERABLE_CODES = {
    "MODEL_TIMEOUT", "MODEL_RATE_LIMITED", "MODEL_UNAVAILABLE",
    "MODEL_RESPONSE_INVALID", "CATALOG_UNAVAILABLE", "QUERY_FAILED", "TUTORING_FAILED",
}


def business_summary(value: Any) -> tuple[BusinessStatus | None, bool, str | None]:
    """处理终止和业务完成分别判断；旧轮次按结构化证据推导。"""
    status = getattr(value, "status", "completed")
    if status == "running":
        return None, False, None
    actions = value.composite_results
    failures = [item for item in actions if item.status == "failed"]
    if failures:
        partial = any(item.status == "success" for item in actions)
        retryable = any(item.details.get("retryable") is True for item in failures)
        return ("partial" if partial else "failed", retryable,
                "可重试未完成步骤，已成功操作会保留。" if retryable else "请按失败原因调整输入或选择其他操作。")
    if status in ("failed", "interrupted"):
        error = getattr(value, "error", None)
        allowed = status == "interrupted" or (error is not None and error.code in RECOVERABLE_CODES)
        partial = any(item.status == "success" for item in actions)
        return ("partial" if partial else "failed"), allowed, "可使用原输入重试。" if allowed else "请先处理错误原因。"
    tutoring = value.tutoring
    if ((tutoring is not None and tutoring.kind in ("needs_clarification", "intent_clarification"))
        or any(item.status == "clarification" for item in actions)
        or (value.evaluation is not None and value.evaluation.needs_supplement)):
        return "needs_input", False, "请补充信息后继续。"
    if value.kind in ("exercise", "exercise_answer") and value.evaluation is None:
        return "failed", True, "作答已保存，可重试评价。"
    return "succeeded", False, None
TurnKind = Literal[
    "query",
    "problem",
    "supplement",
    "followup",
    "correction",
    "record",
    "feedback",
    "exercise",
    # 仅用于接受 F006 修复前的请求别名；读取时归一为 exercise，新写入不再使用。
    "exercise_answer",
    "reveal_solution",
    "dynamic",
]

# 会话轮次的错误码在查询错误码之外，增加本地存储故障。
TurnErrorCode = ErrorCode | Literal["STORAGE_UNAVAILABLE", "BUSINESS_REJECTED"]

TurnStage = Literal[
    "dispatching",
    "understanding",
    "retrieving",
    "reading_problem",
    "checking_clarity",
    "explaining",
    "answering",
    "recording",
    "updating_record",
    "saving",
    "scheduling",
    "generating_exercise",
    "evaluating_exercise",
]

ExerciseStatus = Literal["pending_answer", "submitted", "revealed"]


class ExerciseEvaluation(BaseModel):
    is_correct: bool | None = None
    final_answer_assessment: str
    step_by_step_analysis: str
    guidance: str
    needs_supplement: bool = False
    # 查看参考解答后的对照作答：答案仍可正确，但不代表独立完成。
    independence_assessment: Literal["independent", "after_reveal", "unclear"] | None = None
    independence_note: str | None = None


class ExerciseAttemptView(BaseModel):
    """一次不可变作答尝试；补充和订正按 attempt_no 顺序保留。"""

    id: str
    attempt_no: int
    kind: Literal["initial", "supplement", "correction"]
    student_answer_text: str | None = None
    student_solution_text: str | None = None
    answered_after_revealed: bool = False
    evaluation: ExerciseEvaluation | None = None
    request_id: str | None = None
    attachments: list[AttachmentView] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ExerciseView(BaseModel):
    id: str
    conversation_id: str
    problem_id: str
    root_problem_id: str
    statement_text: str
    knowledge_point: str
    status: ExerciseStatus
    student_answer_text: str | None = None
    student_solution_text: str | None = None
    revealed_solution_at: datetime | None = None
    answered_after_revealed: bool = False
    evaluation: ExerciseEvaluation | None = None
    # 核心安全控制：仅在已主动查看或作答后由服务层决定是否回传
    reference_solution_text: str | None = None
    attachments: list[AttachmentView] = Field(default_factory=list)
    # 首次、补充与订正记录；顶层字段始终镜像最新一次尝试。
    attempts: list[ExerciseAttemptView] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class SuggestionItem(BaseModel):
    label: str
    action: str
    target_id: str | None = None


class ActionResultItem(BaseModel):
    action: str
    status: Literal["success", "failed", "clarification"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ConversationContextView(BaseModel):
    confirmed_course: str | None = None
    active_problem_id: str | None = None
    active_exercise_id: str | None = None
    last_suggestions: list[SuggestionItem] = Field(default_factory=list)


class ConversationSummary(BaseModel):
    """会话摘要：列表、创建响应和详情外层共用。"""

    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    items: list[ConversationSummary]
    total: int


class TurnError(BaseModel):
    """一轮失败的稳定错误码与可展示文案，不保存供应商原始异常。"""

    code: TurnErrorCode
    message: str


class RecordRef(BaseModel):
    id: str
    problem_version: int


class Turn(BaseModel):
    """一轮已保存的会话输入；status、kind 与结果必须自洽。"""

    request_id: str
    seq: int
    kind: TurnKind = "query"
    query: str
    status: TurnStatus
    result: ResultEvent | None = None
    tutoring: TutoringResult | None = None
    exercise: ExerciseView | None = None
    evaluation: ExerciseEvaluation | None = None
    composite_results: list[ActionResultItem] = Field(default_factory=list)
    assistant_message: str | None = None
    suggestions: list[SuggestionItem] = Field(default_factory=list)
    exercise_id: str | None = None
    error: TurnError | None = None
    started_at: datetime
    finished_at: datetime | None = None
    # 仅用于重复请求比较，不进入接口响应。
    input_json: str | None = Field(default=None, exclude=True, repr=False)
    recovery: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)
    retried_by_seq: int | None = Field(default=None, exclude=True, repr=False)
    business_status: BusinessStatus | None = None
    retryable: bool = False
    retry_hint: str | None = None

    @model_validator(mode="after")
    def _check_payload(self) -> "Turn":
        self.business_status, self.retryable, self.retry_hint = business_summary(self)
        # 无检查点的旧动态失败不能安全重放组合动作。
        if self.kind == "dynamic" and self.status != "completed" and not self.recovery:
            self.retryable = False
            self.retry_hint = "旧轮次缺少动作恢复记录，请确认已有结果后重新提出需求。"
        if self.recovery and self.status in ("failed", "interrupted"):
            self.retryable = any(not entry.get("outcome") or entry["outcome"].get("details", {}).get("retryable")
                                 for entry in self.recovery.get("actions", [])) or bool(self.recovery.get("pending")) or not self.recovery.get("actions")
            entries = self.recovery.get("actions", [])
            if entries and not self.recovery.get("pending") and all(entry.get("outcome", {}).get("status") == "success" for entry in entries):
                self.business_status = "succeeded"
                self.retryable = False
                self.retry_hint = "业务操作已保存；本轮连接未完整结束，可刷新查看已保存结果。"
        if self.status == "completed":
            has_payload = any(
                [
                    self.result is not None,
                    self.tutoring is not None,
                    self.exercise is not None,
                    bool(self.composite_results),
                    bool(self.assistant_message),
                ]
            )
            if not has_payload:
                raise ValueError("completed 轮次必须包含至少一项有效结果。")
            if self.kind == "query" and self.result is None:
                raise ValueError("query 轮次必须包含查询结果。")
            if self.error is not None:
                raise ValueError("completed 轮次不能包含错误。")
            if self.finished_at is None:
                raise ValueError("completed 轮次必须有结束时间。")
        elif self.status == "failed":
            if self.error is None or ((self.result is not None or self.tutoring is not None) and not self.recovery):
                raise ValueError("failed 轮次必须且只能包含 error。")
            if self.finished_at is None:
                raise ValueError("failed 轮次必须有结束时间。")
        elif self.status == "interrupted":
            if ((self.result is not None or self.tutoring is not None) and not self.recovery) or self.error is not None:
                raise ValueError("interrupted 轮次不能包含结果或错误。")
            if self.finished_at is None:
                raise ValueError("interrupted 轮次必须有结束时间。")
        else:  # running
            if self.result is not None or self.tutoring is not None or self.error is not None:
                raise ValueError("running 轮次不能包含结果或错误。")
            if self.finished_at is not None:
                raise ValueError("running 轮次不能有结束时间。")

        if self.retried_by_seq is not None:
            self.retryable = False
            self.retry_hint = f"该轮次已重试，请查看第 {self.retried_by_seq} 轮的结果。"
        return self


class ConversationDetail(BaseModel):
    conversation: ConversationSummary
    turns: list[Turn]
    active_problem: ProblemView | None = None
    active_exercise: ExerciseView | None = None
    context: ConversationContextView | None = None
    record: RecordRef | None = None


class TurnProgressEvent(BaseModel):
    type: Literal["progress"] = "progress"
    conversation_id: str
    request_id: str
    stage: TurnStage
    message: str


class TurnResultEvent(BaseModel):
    type: Literal["result"] = "result"
    conversation_id: str
    request_id: str
    kind: TurnKind
    result: ResultEvent | None = None
    tutoring: TutoringResult | None = None
    exercise: ExerciseView | None = None
    evaluation: ExerciseEvaluation | None = None
    composite_results: list[ActionResultItem] = Field(default_factory=list)
    assistant_message: str | None = None
    suggestions: list[SuggestionItem] = Field(default_factory=list)
    exercise_id: str | None = None
    business_status: BusinessStatus | None = None
    retryable: bool = False
    retry_hint: str | None = None

    @model_validator(mode="after")
    def _check_payload(self) -> "TurnResultEvent":
        derived_status, derived_retry, derived_hint = business_summary(self)
        provided = self.model_fields_set.copy()
        if "business_status" not in provided:
            self.business_status = derived_status
        if "retryable" not in provided:
            self.retryable = derived_retry
        if "retry_hint" not in provided:
            self.retry_hint = derived_hint
        has_payload = any(
            [
                self.result is not None,
                self.tutoring is not None,
                self.exercise is not None,
                bool(self.composite_results),
                bool(self.assistant_message),
            ]
        )
        if not has_payload:
            raise ValueError("最终结果必须包含至少一项有效结果。")
        if self.kind == "query" and self.result is None:
            raise ValueError("query 轮次必须包含查询结果。")
        return self


class TurnErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    conversation_id: str
    request_id: str
    code: TurnErrorCode
    message: str


TurnEvent = TurnProgressEvent | TurnResultEvent | TurnErrorEvent
