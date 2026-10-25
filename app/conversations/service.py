"""会话业务服务：调度查询、辅导、记录和反馈。

职责边界：
- 接受/恢复轮次的前置校验（会话、重复请求、运行冲突、kind、附件、来源）。
- 在数据库中先写入 running 轮次，再执行模型；模型等待期间不持有事务。
- 最终结果或失败先落库，成功后才作为 SSE 事件交给接口层。
- 客户端断开或停止时，把尚未完成的轮次标记为 interrupted。
- 已完成/失败的重复请求直接回放已保存的终止事件，不再调用模型。

本模块不处理 HTTP 状态码；异常由 app/main.py 统一映射。
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, replace, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Request

from app.conversations import repository as conversation_repository
from app.conversations.dispatch import DeepSeekIntentRouter, IntentDecision
from app.conversations.graph import (
    ActionOutcome,
    ActionPlan,
    DynamicReviewGraph,
    ExecutionResult,
    ScheduleDecision,
    SchedulerAgent,
)
from app.tutoring.exercise_llm import (
    AnswerEvaluation,
    DeepSeekExerciseAgent,
    GeneratedExercise,
)
from app.tutoring.exercise_dedup import (
    MAX_GENERATION_ATTEMPTS,
    MAX_RECENT_EXERCISES,
    is_duplicate_statement,
)
from app.conversations.errors import (
    ActiveProblemConflictError,
    AttachmentInvalidError,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationQueryUnavailableError,
    ConversationStorageError,
    InvalidImageError,
    MistakeRecordConflictError,
    ProblemNotFoundError,
    ProblemNotReadyError,
    SourceInvalidError,
    TurnInvalidError,
    TutoringUnavailableError,
)
from app.conversations.inputs import canonical_json, query_input
from app.conversations.models import (
    ActionResultItem,
    ConversationContextView,
    ConversationDetail,
    ConversationListResponse,
    ConversationSummary,
    ExerciseAttemptView,
    ExerciseEvaluation,
    ExerciseView,
    RecordRef,
    SuggestionItem,
    Turn,
    TurnErrorEvent,
    TurnEvent,
    TurnKind,
    TurnProgressEvent,
    TurnResultEvent,
    RECOVERABLE_CODES,
)
from app.conversations.repository import (
    AttachmentRecord,
    ContextRecord,
    ConversationRepository,
    ExerciseAttemptRecord,
    ExerciseRecord,
    NewProblem,
    ProblemRecord,
    UpdatedProblem,
)
from app import images
from app.images import (
    ATTACHMENTS_ROOT,
    PENDING_TTL_SECONDS,
    delete_attachment,
)
from app.mistakes.service import MistakeRecordService
from app.papers.errors import CatalogUnavailableError
from app.papers.service import PaperService
from app.querying.errors import QueryError
from app.querying.models import ErrorEvent, ProgressEvent, ResultEvent
from app.querying.service import QueryService
from app.streaming import SENTINEL, stream_events
from app.tutoring.models import (
    AttachmentView,
    Explanation,
    FeedbackResult,
    FollowupResult,
    IntentClarificationResult,
    NeedsClarificationResult,
    RecordCreatedResult,
    RecordExistsResult,
    Source,
    TutoringResult,
)
from app.tutoring.service import (
    build_explanation_result,
    build_needs_clarification,
    build_problem_view,
    load_images,
    record_title,
)
from app.tutoring.workflow import build_tutoring_workflow

logger = logging.getLogger(__name__)

STORAGE_ERROR_CODE = "STORAGE_UNAVAILABLE"
_STORAGE_FALLBACK_MESSAGE = "本地学习数据暂时不可用，请稍后再试。"
_TUTORING_TIMEOUT_SECONDS = 125.0
_DISPATCH_TIMEOUT_SECONDS = 25.0
_DYNAMIC_TIMEOUT_SECONDS = 180.0
_TUTORING_MESSAGE = "这次辅导没有完成，请重试。"
_TIMEOUT_MESSAGE = "辅导超时，请重试。"
_DISPATCH_MESSAGE = "判断会话意图超时，请重试。"
_RETRY_TEXT_MARKERS = ("重试", "重新评价", "再评价", "再试")
_RETRY_TEXTS = {
    "重试",
    "重试评价",
    "重新评价",
    "重新评价一下",
    "再评价",
    "再评价一次",
    "再试",
    "再试一次",
    "继续评价",
    "评价一下",
    "请评价",
}


@dataclass(frozen=True)
class TurnInput:
    """接口层校验后的轮次输入；服务层只处理领域规则。"""

    request_id: str
    kind: TurnKind | None
    text: str
    attachments: tuple[tuple[str, str], ...]
    student_answer_text: str | None
    solution_text: str | None
    source: Source
    problem_id: str | None
    start_new: bool
    dispatch: bool = False
    exercise_id: str | None = None
    retry_of: str | None = None
    attempt_kind: str | None = None


@dataclass
class _AttemptPlan:
    """一次提交对应的尝试决策；不改写已有尝试。"""

    kind: str
    answer: str | None
    solution: str | None
    attachments: tuple[tuple[str, str], ...]
    answered_after_revealed: bool
    reuse: bool = False
    attempt: Any | None = None
    error: str | None = None


@dataclass(frozen=True)
class ConversationStoreState:
    """启动时固定的数据库状态；存储不可用不阻止应用启动。"""

    repository: ConversationRepository | None
    error: str | None


def build_store_state() -> ConversationStoreState:
    """启动时调用：初始化数据库、恢复遗留轮次、清理过期待提交附件。"""

    repository = ConversationRepository(conversation_repository.DEFAULT_DB_PATH)
    try:
        repository.initialize()
    except ConversationStorageError as exc:
        logger.warning("本地学习数据库不可用：%s", exc)
        return ConversationStoreState(repository=None, error=str(exc))

    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=PENDING_TTL_SECONDS)
        ).isoformat()
        for stored_name in repository.cleanup_pending_attachments(cutoff):
            delete_attachment(images.ATTACHMENTS_ROOT, stored_name)
    except Exception:
        logger.warning("清理过期待提交附件失败，不影响应用启动。", exc_info=True)

    return ConversationStoreState(repository=repository, error=None)


@dataclass(frozen=True)
class ReplayTurn:
    """重复提交：直接回放已保存的终止事件。"""

    conversation_id: str
    turn: Turn


TurnOperation = Literal[
    "query",
    "dispatch",
    "dynamic",
    "problem",
    "supplement",
    "followup",
    "correction",
    "record",
    "feedback",
    "clarify",
    "exercise",
    "exercise_answer",
    "reveal_solution",
]


@dataclass(frozen=True)
class ExecuteTurn:
    """新轮次：已经写入 running，等待执行。"""

    conversation_id: str
    request_id: str
    turn: Turn
    input: TurnInput
    operation: TurnOperation
    problem: ProblemRecord | None = None
    exercise: ExerciseRecord | None = None
    clarify_options: tuple[str, ...] = ()
    clarify_message: str = ""
    recovery: dict[str, Any] = field(default_factory=dict)


PreparedTurn = ReplayTurn | ExecuteTurn


@dataclass
class _RunState:
    saved: bool = False


@dataclass
class _DynamicContext:
    """动态调度一轮内真实执行结果；供后续动作和最终事件共同读取。"""

    conversation_id: str
    confirmed_course: str | None = None
    active_problem: ProblemRecord | None = None
    active_exercise: ExerciseRecord | None = None
    result: ResultEvent | None = None
    tutoring: TutoringResult | None = None
    exercise_view: ExerciseView | None = None
    evaluation_view: ExerciseEvaluation | None = None
    exercise_id: str | None = None
    problem_id: str | None = None
    recovery: dict[str, Any] = field(default_factory=dict)
    action_index: int = -1


class ConversationService:
    def __init__(
        self,
        repository: ConversationRepository,
        make_query: Callable[[], QueryService] | None,
        query_error: str | None,
        *,
        make_tutor: Callable[[], Any] | None = None,
        tutoring_error: str | None = None,
        make_router: Callable[[], DeepSeekIntentRouter] | None = None,
        make_exercise_agent: Callable[[], Any] | None = None,
        make_scheduler: Callable[[], Any] | None = None,
        papers: PaperService | None = None,
        records: MistakeRecordService | None = None,
        attachments_root: Path = ATTACHMENTS_ROOT,
    ) -> None:
        self._repository = repository
        self._make_query = make_query
        self._query_error = query_error
        self._make_tutor = make_tutor
        self._tutoring_error = tutoring_error
        self._make_router = make_router
        self._make_exercise_agent = make_exercise_agent
        self._make_scheduler = make_scheduler
        self._papers = papers
        self._records = records
        self._attachments_root = attachments_root

    @property
    def records(self) -> MistakeRecordService | None:
        return self._records

    def get_active_problem(self, conversation_id: str) -> ProblemRecord | None:
        return self._repository.get_active_problem(conversation_id)

    def _build_exercise_view(
        self, ex: ExerciseRecord, *, allow_solution: bool = False
    ) -> ExerciseView:
        evaluation = self._parse_evaluation(ex.evaluation_json)

        ref_sol = None
        if allow_solution or ex.status in ("submitted", "revealed") or ex.revealed_solution_at is not None:
            ref_sol = ex.reference_solution_text

        attempts = [
            ExerciseAttemptView(
                id=attempt.id,
                attempt_no=attempt.attempt_no,
                kind=attempt.kind,  # type: ignore[arg-type]
                student_answer_text=attempt.student_answer_text,
                student_solution_text=attempt.student_solution_text,
                answered_after_revealed=attempt.answered_after_revealed,
                evaluation=self._parse_evaluation(attempt.evaluation_json),
                request_id=attempt.request_id,
                attachments=self._attachment_views(
                    ex.conversation_id, attempt.attachments
                ),
                created_at=attempt.created_at,
                updated_at=attempt.updated_at,
            )
            for attempt in ex.attempts
        ]

        return ExerciseView(
            id=ex.id,
            conversation_id=ex.conversation_id,
            problem_id=ex.problem_id,
            root_problem_id=ex.root_problem_id,
            statement_text=ex.statement_text,
            knowledge_point=ex.knowledge_point,
            status=ex.status,  # type: ignore[arg-type]
            student_answer_text=ex.student_answer_text,
            student_solution_text=ex.student_solution_text,
            revealed_solution_at=ex.revealed_solution_at,
            answered_after_revealed=ex.answered_after_revealed,
            evaluation=evaluation,
            reference_solution_text=ref_sol,
            attachments=self._attachment_views(ex.conversation_id, ex.attachments),
            attempts=attempts,
            created_at=ex.created_at,
            updated_at=ex.updated_at,
        )

    @staticmethod
    def _parse_evaluation(raw: str | None) -> ExerciseEvaluation | None:
        if not raw:
            return None
        with suppress(Exception):
            return ExerciseEvaluation.model_validate(json.loads(raw))
        return None

    @staticmethod
    def _attachment_views(
        conversation_id: str, attachments: list[AttachmentRecord]
    ) -> list[AttachmentView]:
        views: list[AttachmentView] = []
        for att in attachments:
            role = (
                att.role
                if att.role in ("student_solution", "student_answer")
                else "student_solution"
            )
            views.append(
                AttachmentView(
                    id=att.id,
                    role=role,  # type: ignore[arg-type]
                    mime=att.mime,
                    width=att.width,
                    height=att.height,
                    url=f"/api/conversations/{conversation_id}/attachments/{att.id}",
                )
            )
        return views

    @staticmethod
    def _enforce_independence(
        evaluation: AnswerEvaluation, attempt: ExerciseAttemptRecord
    ) -> AnswerEvaluation:
        """程序兜底：查看解答后的作答不得被表述为独立掌握。"""

        if not attempt.answered_after_revealed:
            if evaluation.independence_assessment is None:
                return evaluation.model_copy(
                    update={
                        "independence_assessment": (
                            "independent"
                            if evaluation.is_correct is True
                            else "unclear"
                        )
                    }
                )
            return evaluation
        note = (evaluation.independence_note or "").strip()
        if "参考解答" not in note and "对照" not in note:
            note = (
                "本次作答前参考解答已经展示，属于对照作答；答案正确不代表独立掌握，"
                "建议遮住解答后完整重做一遍。"
            )
        return evaluation.model_copy(
            update={
                "independence_assessment": "after_reveal",
                "independence_note": note[:800],
            }
        )

    @staticmethod
    def _exercise_needs_supplement(ex: ExerciseRecord | None) -> bool:
        if ex is None or not ex.evaluation_json:
            return False
        with suppress(Exception):
            evaluation = ExerciseEvaluation.model_validate(
                json.loads(ex.evaluation_json)
            )
            return bool(evaluation.needs_supplement)
        return False

    @staticmethod
    def _is_retry_text(text: str) -> bool:
        normalized = "".join(text.split()).strip("。.!！?？")
        if not normalized:
            return False
        if normalized in _RETRY_TEXTS:
            return True
        return any(marker in normalized for marker in _RETRY_TEXT_MARKERS)

    @staticmethod
    def _merge_supplement(prior: str | None, new: str) -> str | None:
        """把补充说明接到原步骤后；用户提交完整过程时不重复拼接。"""

        base = (prior or "").strip()
        addition = (new or "").strip()
        if not addition:
            return base or None
        if not base or addition in base:
            return base or addition
        if base in addition:
            return addition
        return f"{base}\n\n补充：\n{addition}"

    def _plan_exercise_attempt(
        self, ex: ExerciseRecord, request: TurnInput
    ) -> _AttemptPlan:
        """把一次提交归类为复用、首次、补充或订正，不修改任何旧尝试。"""

        latest = ex.current_attempt
        explicit_answer = (request.student_answer_text or "").strip() or None
        explicit_solution = (request.solution_text or "").strip() or None
        text = request.text.strip()
        has_material = bool(explicit_answer or explicit_solution or request.attachments)
        retry_requested = bool(request.retry_of) or (
            not has_material and bool(text) and self._is_retry_text(text)
        )
        revealed = (
            ex.revealed_solution_at is not None
            or ex.status in ("submitted", "revealed")
        )
        if (
            request.kind is None
            and not explicit_answer
            and not explicit_solution
            and not request.attachments
            and text
            and not self._looks_like_exercise_text(text)
            and not self._is_retry_text(text)
        ):
            if latest is not None and latest.evaluation_json is not None:
                return _AttemptPlan(
                    kind="correction",
                    answer=None,
                    solution=None,
                    attachments=(),
                    answered_after_revealed=False,
                    error="这道练习题已经完成评价；如需订正，请写出新的答案或过程，或生成新的类似题。",
                )
            return _AttemptPlan(
                kind="initial",
                answer=None,
                solution=None,
                attachments=(),
                answered_after_revealed=False,
                error="请先写出最终答案或上传解题过程图片，我再进行评价。",
            )

        if request.attempt_kind and request.retry_of:
            return _AttemptPlan(
                kind="initial",
                answer=None,
                solution=None,
                attachments=(),
                answered_after_revealed=False,
                error="重试评价不需要再指定补充或订正。",
            )

        if retry_requested:
            target = None
            if request.retry_of:
                target = self._repository.get_attempt_by_request(
                    ex.id, request.retry_of
                )
                if target is None:
                    # 兼容 B 批之前创建的、没有 request_id 的故障轮次。
                    if latest is not None and latest.evaluation_json is None:
                        target = latest
                    else:
                        return _AttemptPlan(
                            kind="initial",
                            answer=None,
                            solution=None,
                            attachments=(),
                            answered_after_revealed=False,
                            error="找不到这次的作答记录，请刷新后重新提交。",
                        )
                elif target.evaluation_json is not None:
                    return _AttemptPlan(
                        kind="initial",
                        answer=None,
                        solution=None,
                        attachments=(),
                        answered_after_revealed=False,
                        error="这次作答已经完成评价，请刷新查看结果，不要重复提交。",
                    )
            else:
                if latest is None:
                    return _AttemptPlan(
                        kind="initial",
                        answer=None,
                        solution=None,
                        attachments=(),
                        answered_after_revealed=False,
                        error="请先写出最终答案或上传解题过程，我再进行评价。",
                    )
                if latest.evaluation_json is not None:
                    return _AttemptPlan(
                        kind="initial",
                        answer=None,
                        solution=None,
                        attachments=(),
                        answered_after_revealed=False,
                        error=(
                            "这道练习题已经完成评价；如需订正，请写出新的答案或过程。"
                        ),
                    )
                target = latest
            return _AttemptPlan(
                kind=target.kind,
                answer=target.student_answer_text,
                solution=target.student_solution_text,
                attachments=(),
                answered_after_revealed=target.answered_after_revealed,
                reuse=True,
                attempt=target,
            )

        kind_hint = request.attempt_kind
        if kind_hint == "supplement":
            if latest is None or not self._attempt_needs_supplement(latest):
                return _AttemptPlan(
                    kind="supplement",
                    answer=None,
                    solution=None,
                    attachments=(),
                    answered_after_revealed=False,
                    error="当前作答不需要补充；可直接提交订正答案，或生成新的类似题。",
                )
            # 独立作答表单把答案放在 query 里；对话里的“补充：……”只并入过程。
            answer = explicit_answer or (
                text if request.kind in ("exercise", "exercise_answer") else None
            )
            return _AttemptPlan(
                kind="supplement",
                answer=answer or latest.student_answer_text,
                solution=self._merge_supplement(
                    latest.student_solution_text,
                    explicit_solution or text,
                ),
                attachments=tuple(request.attachments),
                answered_after_revealed=revealed,
            )

        if kind_hint == "correction":
            if latest is None:
                return _AttemptPlan(
                    kind="correction",
                    answer=None,
                    solution=None,
                    attachments=(),
                    answered_after_revealed=False,
                    error="请先提交首次作答，再进行订正。",
                )
            if self._attempt_needs_supplement(latest):
                return _AttemptPlan(
                    kind="correction",
                    answer=None,
                    solution=None,
                    attachments=(),
                    answered_after_revealed=False,
                    error="这次作答还需要补充过程，请先补充并重新评价。",
                )
            return _AttemptPlan(
                kind="correction",
                answer=explicit_answer or text or None,
                solution=explicit_solution or None,
                attachments=tuple(request.attachments),
                answered_after_revealed=revealed,
            )

        if latest is None:
            return _AttemptPlan(
                kind="initial",
                answer=explicit_answer or text or None,
                solution=explicit_solution or None,
                attachments=tuple(request.attachments),
                answered_after_revealed=revealed,
            )

        if latest.evaluation_json is None:
            same_content = (
                (latest.student_answer_text or "") == (explicit_answer or "")
                and (latest.student_solution_text or "") == (explicit_solution or "")
                and not request.attachments
            )
            if same_content:
                return _AttemptPlan(
                    kind=latest.kind,
                    answer=latest.student_answer_text,
                    solution=latest.student_solution_text,
                    attachments=(),
                    answered_after_revealed=latest.answered_after_revealed,
                    reuse=True,
                    attempt=latest,
                )
            return _AttemptPlan(
                kind="correction",
                answer=explicit_answer or text or None,
                solution=explicit_solution or None,
                attachments=tuple(request.attachments),
                answered_after_revealed=revealed,
            )

        if self._attempt_needs_supplement(latest):
            answer = explicit_answer or (
                text if request.kind in ("exercise", "exercise_answer") else None
            )
            return _AttemptPlan(
                kind="supplement",
                answer=answer or latest.student_answer_text,
                solution=self._merge_supplement(
                    latest.student_solution_text,
                    explicit_solution or text,
                ),
                attachments=tuple(request.attachments),
                answered_after_revealed=revealed,
            )

        new_answer = explicit_answer or text or None
        new_solution = explicit_solution or None
        if (
            latest.evaluation_json is not None
            and (latest.student_answer_text or "") == (new_answer or "")
            and (latest.student_solution_text or "") == (new_solution or "")
            and not request.attachments
        ):
            return _AttemptPlan(
                kind="correction",
                answer=None,
                solution=None,
                attachments=(),
                answered_after_revealed=False,
                error="这次作答已经完成评价且内容没有变化；如需订正，请修改答案或过程。",
            )
        return _AttemptPlan(
            kind="correction",
            answer=new_answer,
            solution=new_solution,
            attachments=tuple(request.attachments),
            answered_after_revealed=revealed,
        )

    @staticmethod
    def _looks_like_exercise_text(text: str) -> bool:
        """区分“这是新答案/过程”和“再讲一次/再来一道”这类请求。"""

        normalized = "".join(text.split())
        if not normalized:
            return False
        if len(normalized) >= 30:
            return True
        if any(ch.isdigit() for ch in normalized):
            return True
        markers = (
            "答案",
            "选",
            "算",
            "求",
            "得",
            "等于",
            "因为",
            "所以",
            "步骤",
            "证明",
            "代入",
            "公式",
            "定理",
            "导数",
            "积分",
            "极限",
            "函数",
            "=",
        )
        return any(marker in normalized for marker in markers)

    @staticmethod
    def _attempt_needs_supplement(attempt: Any) -> bool:
        if attempt is None or not attempt.evaluation_json:
            return False
        with suppress(Exception):
            evaluation = ExerciseEvaluation.model_validate(
                json.loads(attempt.evaluation_json)
            )
            return bool(evaluation.needs_supplement)
        return False

    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------

    def create_conversation(self) -> ConversationSummary:
        return self._repository.create_conversation()

    def list_conversations(self) -> ConversationListResponse:
        items = self._repository.list_conversations()
        return ConversationListResponse(items=items, total=len(items))

    def get_detail(self, conversation_id: str) -> ConversationDetail:
        conversation = self._repository.get_conversation(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError("会话不存在。")
        active = self._repository.get_active_problem(conversation_id)
        record_ref: RecordRef | None = None
        if active is not None:
            record = self._repository.get_record_by_root(
                conversation_id, active.root_problem_id
            )
            if record is not None:
                record_ref = RecordRef(
                    id=record.id, problem_version=record.problem_version
                )

        active_ex = self._repository.get_active_exercise(conversation_id)
        ex_view = None
        if active_ex is not None:
            ex_view = self._build_exercise_view(active_ex)

        ctx_rec = self._repository.get_context(conversation_id)
        ctx_view = None
        if ctx_rec is not None:
            sugs = []
            if ctx_rec.last_suggestions_json:
                with suppress(Exception):
                    sugs = [
                        SuggestionItem.model_validate(it)
                        for it in json.loads(ctx_rec.last_suggestions_json)
                    ]
            ctx_view = ConversationContextView(
                confirmed_course=ctx_rec.confirmed_course,
                active_problem_id=ctx_rec.active_problem_id,
                active_exercise_id=ctx_rec.active_exercise_id,
                last_suggestions=sugs,
            )

        return ConversationDetail(
            conversation=conversation,
            turns=self._repository.list_turns(conversation_id),
            active_problem=build_problem_view(active) if active is not None else None,
            active_exercise=ex_view,
            context=ctx_view,
            record=record_ref,
        )

    # ------------------------------------------------------------------
    # 新轮次
    # ------------------------------------------------------------------

    def prepare_turn(
        self,
        conversation_id: str,
        data: TurnInput,
    ) -> PreparedTurn:
        """建立 SSE 流之前完成的全部检查；错误按约定返回 HTTP 状态。"""

        if self._repository.get_conversation(conversation_id) is None:
            raise ConversationNotFoundError("会话不存在。")

        if data.retry_of is not None:
            return self._prepare_retry(conversation_id, data)

        normalized = self._normalize(data)
        normalized = replace(normalized, source=self._resolve_source(normalized))
        canonical = self._canonical(normalized)

        existing = self._repository.find_turn(
            conversation_id, normalized.request_id
        )
        if existing is not None:
            if not self._same_input(existing, canonical):
                raise ConversationConflictError(
                    "同一个 request_id 不能提交不同的输入或附件。"
                )
            if existing.status == "running":
                raise ConversationConflictError(
                    "该请求仍在执行，请稍后查看会话记录。"
                )
            if existing.status == "interrupted":
                raise ConversationConflictError(
                    "该请求已中断，请使用新的 request_id 重新提交。"
                )
            return ReplayTurn(conversation_id=conversation_id, turn=existing)

        if self._repository.has_running_turn(conversation_id):
            raise ConversationConflictError(
                "该会话已有一轮正在执行，请等待完成或先停止。"
            )

        active = self._repository.get_active_problem(conversation_id)

        # F004 旧客户端只发送 request_id + query：按查询处理，不额外调用意图模型。
        if (
            normalized.kind is None
            and not normalized.dispatch
            and not normalized.attachments
        ):
            legacy = replace(normalized, kind="query")
            return self._prepare_explicit(
                conversation_id, legacy, query_input(legacy.text), active
            )

        if normalized.kind is None:
            return self._prepare_dispatched(conversation_id, normalized, canonical, active)

        return self._prepare_explicit(
            conversation_id, normalized, canonical, active
        )

    def _prepare_retry(self, conversation_id: str, request: TurnInput) -> PreparedTurn:
        canonical = canonical_json({"retry_of": request.retry_of})
        existing = self._repository.find_turn(conversation_id, request.request_id)
        if existing:
            if not self._same_input(existing, canonical) or existing.status in ("running", "interrupted"):
                raise ConversationConflictError("重试请求仍在执行、已中断或内容发生冲突，请刷新记录。")
            return ReplayTurn(conversation_id, existing)
        origin = self._repository.find_turn(conversation_id, request.retry_of or "")
        if origin is None:
            raise ProblemNotFoundError("原轮次不属于当前会话。")
        if not origin.retryable or origin.status == "running":
            raise ConversationConflictError(origin.retry_hint or "这一轮不能直接重试，请按提示继续。")
        for turn in self._repository.list_turns(conversation_id):
            if turn.input_json and json.loads(turn.input_json).get("retry_of") == origin.request_id:
                raise ConversationConflictError("这一步已经重试过，请查看最新重试轮次的结果。")
        if self._repository.has_running_turn(conversation_id):
            raise ConversationConflictError("当前会话已有请求正在处理，请稍候。")
        payload = origin.recovery.get("input") or json.loads(origin.input_json or "{}")
        if not payload or "retry_of" in payload:
            raise ConversationConflictError("原轮次缺少完整输入，请确认已有结果后重新提出需求。")
        recovered = TurnInput(
            request_id=request.request_id, kind=payload.get("kind"), text=payload.get("text", origin.query),
            attachments=tuple((it["id"], it["role"]) for it in payload.get("attachments", [])),
            student_answer_text=payload.get("student_answer"), solution_text=payload.get("solution_text"),
            source=Source.model_validate(payload.get("source") or {}), problem_id=payload.get("problem_id"),
            exercise_id=payload.get("exercise_id") or origin.exercise_id,
            start_new=payload.get("start_new", False), dispatch=payload.get("dispatch", False),
            retry_of=request.retry_of, attempt_kind=payload.get("attempt_kind"),
        )
        if origin.recovery.get("version") == 1:
            turn = self._repository.start_turn(conversation_id, request.request_id, origin.query,
                                             kind="dynamic", input_json=canonical,
                                             attachments=recovered.attachments)
            recovery = json.loads(json.dumps(origin.recovery))
            self._repository.save_recovery(conversation_id, request.request_id, recovery)
            return ExecuteTurn(conversation_id, request.request_id, turn, recovered, "dispatch", recovery=recovery)
        active = self._repository.get_active_problem(conversation_id)
        prepared = (self._prepare_dispatched(conversation_id, recovered, canonical, active)
                    if recovered.kind is None else self._prepare_explicit(conversation_id, recovered, canonical, active))
        if isinstance(prepared, ExecuteTurn):
            self._repository.save_recovery(conversation_id, request.request_id, {"input": payload})
        return prepared

    def _prepare_dispatched(
        self,
        conversation_id: str,
        data: TurnInput,
        canonical: str,
        active: ProblemRecord | None,
    ) -> ExecuteTurn:
        if data.attachments:
            if active is not None and self._make_scheduler is not None:
                # 动态调度自行判断新题/补充/纠正；不再因为已有题目而
                # 强制拦截附件，否则澄清后的图片补充无法接续同一目标。
                self._validate_attachments(conversation_id, data, "supplement")
                turn = self._repository.start_turn(
                    conversation_id,
                    data.request_id,
                    data.text,
                    kind="query",
                    input_json=canonical,
                    attachments=data.attachments,
                )
                return ExecuteTurn(
                    conversation_id=conversation_id,
                    request_id=data.request_id,
                    turn=turn,
                    input=data,
                    operation="dispatch",
                    problem=active,
                )
            if active is not None:
                self._validate_attachments(conversation_id, data, "supplement")
                turn = self._repository.start_turn(
                    conversation_id,
                    data.request_id,
                    data.text,
                    kind="problem",
                    input_json=canonical,
                    attachments=data.attachments,
                )
                return ExecuteTurn(
                    conversation_id=conversation_id,
                    request_id=data.request_id,
                    turn=turn,
                    input=data,
                    operation="clarify",
                    problem=active,
                    clarify_options=("supplement", "correction", "new_problem"),
                    clarify_message=(
                        "当前已有正在辅导的题目。请明确选择：补充当前题、"
                        "纠正题干，还是开始新题；不要直接混用附件。"
                    ),
                )
            self._validate_attachments(conversation_id, data, "problem")
            self._validate_source(active, data, "problem")
            self._require_tutor()
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="problem",
                input_json=canonical,
                attachments=data.attachments,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="problem",
                problem=None,
            )

        if self._make_router is None and self._make_scheduler is None:
            raise ConversationQueryUnavailableError(
                self._query_error
                or self._tutoring_error
                or "会话所需的模型尚未配置。"
            )

        turn = self._repository.start_turn(
            conversation_id,
            data.request_id,
            data.text,
            kind="query",
            input_json=canonical,
        )
        return ExecuteTurn(
            conversation_id=conversation_id,
            request_id=data.request_id,
            turn=turn,
            input=data,
            operation="dispatch",
            problem=active,
        )

    def _prepare_explicit(
        self,
        conversation_id: str,
        data: TurnInput,
        canonical: str,
        active: ProblemRecord | None,
    ) -> ExecuteTurn:
        kind = data.kind
        assert kind is not None

        if kind == "query":
            self._validate_attachments(conversation_id, data, "query")
            self._validate_source(active, data, "query")
            if data.problem_id is not None or data.start_new:
                raise TurnInvalidError("查询轮次不能携带题目引用或新题标记。")
            if not data.text:
                raise TurnInvalidError("查询内容不能为空。")
            if self._make_query is None:
                raise ConversationQueryUnavailableError(
                    self._query_error or "自然语言查询尚未配置。"
                )
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="query",
                input_json=canonical,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="query",
            )

        if kind == "problem":
            if active is not None and not data.start_new:
                raise ActiveProblemConflictError(
                    "当前已有正在辅导的题目。若要开始新题，请明确确认。"
                )
            self._validate_attachments(conversation_id, data, "problem")
            self._validate_source(active, data, "problem")
            self._require_tutor()
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="problem",
                input_json=canonical,
                attachments=data.attachments,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="problem",
            )

        if kind in ("supplement", "followup"):
            current = self._require_active(active, data.problem_id)

        if kind == "supplement":
            if current.status != "awaiting_supplement":
                raise ProblemNotReadyError(
                    "当前题目已经讲解过；如要改题干，请使用纠正。"
                )
            self._validate_attachments(conversation_id, data, "supplement")
            self._validate_source(current, data, "supplement")
            self._require_tutor()
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="supplement",
                input_json=canonical,
                attachments=data.attachments,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="supplement",
                problem=current,
            )

        if kind == "followup":
            if current.status != "explained" or current.explanation is None:
                raise ProblemNotReadyError("当前题目尚未讲解，暂时不能追问。")
            self._validate_attachments(conversation_id, data, "followup")
            self._validate_source(current, data, "followup")
            if not data.text:
                raise TurnInvalidError("追问内容不能为空。")
            self._require_tutor()
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="followup",
                input_json=canonical,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="followup",
                problem=current,
            )

        if kind == "correction":
            target = self._resolve_target_problem(
                conversation_id,
                active,
                data.problem_id,
                require_explained=False,
            )
            self._validate_attachments(conversation_id, data, "correction")
            self._validate_source(target, data, "correction")
            self._require_tutor()
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="correction",
                input_json=canonical,
                attachments=data.attachments,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="correction",
                problem=target,
            )

        if kind == "record":
            target = self._resolve_target_problem(
                conversation_id,
                active,
                data.problem_id,
                require_explained=True,
            )
            self._validate_attachments(conversation_id, data, "record")
            self._validate_source(target, data, "record")
            if self._records is None:
                raise ConversationStorageError("错题记录服务不可用。")
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="record",
                input_json=canonical,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="record",
                problem=target,
            )

        if kind == "feedback":
            target = self._resolve_target_problem(
                conversation_id,
                active,
                data.problem_id,
                require_explained=True,
            )
            self._validate_attachments(conversation_id, data, "feedback")
            self._validate_source(target, data, "feedback")
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="feedback",
                input_json=canonical,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="feedback",
                problem=target,
            )

        if kind == "reveal_solution":
            ex = self._resolve_exercise_target(conversation_id, data.exercise_id)
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="reveal_solution",
                input_json=canonical,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="reveal_solution",
                exercise=ex,
            )

        if kind in ("exercise", "exercise_answer"):
            ex = self._resolve_exercise_target(conversation_id, data.exercise_id)
            if data.attempt_kind:
                self._validate_attempt_kind(ex, data.attempt_kind)
            self._validate_attachments(conversation_id, data, "tutoring")
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="exercise",
                input_json=canonical,
                attachments=data.attachments,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="exercise_answer",
                exercise=ex,
            )

        raise TurnInvalidError("未知的轮次类型。")

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def stream_prepared(
        self,
        prepared: PreparedTurn,
        request: Request,
    ) -> AsyncIterator[TurnEvent]:
        """把已准备好的轮次转成 SSE 事件；最终事件发出前必须已落库。"""

        if isinstance(prepared, ReplayTurn):
            yield _replay_event(prepared)
            return

        state = _RunState()
        try:
            if prepared.operation == "clarify":
                async for event in self._clarify_events(
                    prepared,
                    state,
                    options=prepared.clarify_options or ("query", "tutoring"),
                    message=(
                        prepared.clarify_message
                        or "请明确要查试卷，还是要辅导一道题。"
                    ),
                ):
                    yield event
                return

            if prepared.operation == "dispatch":
                async for event in self._dispatch_events(prepared, request, state):
                    yield event
                return

            if prepared.operation == "query":
                async for event in self._query_events(prepared, request, state):
                    yield event
                return

            if prepared.operation in ("problem", "supplement", "correction"):
                async for event in self._tutoring_events(
                    prepared, request, state
                ):
                    yield event
                return

            if prepared.operation == "followup":
                async for event in self._followup_events(prepared, state):
                    yield event
                return

            if prepared.operation == "record":
                async for event in self._record_events(prepared, state):
                    yield event
                return

            if prepared.operation == "reveal_solution":
                async for event in self._reveal_solution_events(prepared, state):
                    yield event
                return

            if prepared.operation == "exercise_answer":
                async for event in self._exercise_answer_events(prepared, state):
                    yield event
                return

            if prepared.operation == "feedback":
                async for event in self._feedback_events(prepared, state):
                    yield event
                return
        finally:
            if not state.saved:
                self._interrupt_quietly(prepared)

    async def _dispatch_events(
        self,
        prepared: ExecuteTurn,
        request: Request,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        if self._make_scheduler is not None:
            async for event in self._dynamic_schedule_events(prepared, request, state):
                yield event
            return

        yield TurnProgressEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            stage="dispatching",
            message="正在判断要查试卷还是辅导题目",
        )
        assert self._make_router is not None
        router = self._make_router()
        try:
            decision: IntentDecision = await asyncio.wait_for(
                router(
                    prepared.input.text,
                    active_problem=(
                        prepared.problem.statement_text[:240]
                        if prepared.problem is not None
                        else None
                    ),
                    problem_status=(
                        prepared.problem.status if prepared.problem is not None else None
                    ),
                ),
                timeout=_DISPATCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            yield self._failure_event(
                prepared,
                "MODEL_TIMEOUT",
                _DISPATCH_MESSAGE,
                state,
            )
            return
        except QueryError as exc:
            yield self._failure_event(prepared, exc.code, exc.message, state)
            return
        except Exception:
            logger.exception("会话意图判断失败")
            yield self._failure_event(
                prepared, "TUTORING_FAILED", _TUTORING_MESSAGE, state
            )
            return

        active = self._repository.get_active_problem(prepared.conversation_id)
        if decision.intent == "query":
            if self._make_query is None:
                yield self._failure_event(
                    prepared,
                    "TUTORING_FAILED",
                    "自然语言查询尚未配置。",
                    state,
                )
                return
            async for event in self._query_events(prepared, request, state):
                yield event
            return

        if decision.intent == "problem":
            if active is not None:
                async for event in self._clarify_events(
                    prepared,
                    state,
                    options=("supplement", "correction", "new_problem"),
                    message=(
                        "当前已有正在辅导的题目。请明确选择：补充当前题、"
                        "纠正题干，还是开始新题。"
                    ),
                ):
                    yield event
                return
            async for event in self._tutoring_events(
                replace(prepared, operation="problem", problem=None),
                request,
                state,
            ):
                yield event
            return

        if active is None:
            async for event in self._clarify_events(
                prepared,
                state,
                options=("query", "tutoring"),
                message="我还不确定你是要查试卷，还是要辅导一道题。",
            ):
                yield event
            return

        if decision.intent == "correction":
            async for event in self._tutoring_events(
                replace(prepared, operation="correction", problem=active),
                request,
                state,
            ):
                yield event
            return

        if decision.intent == "followup":
            if active.status != "explained":
                async for event in self._clarify_events(
                    prepared,
                    state,
                    options=("supplement", "correction"),
                    message="当前题目还没有讲解，请先补充或确认题目。",
                ):
                    yield event
                return
            async for event in self._followup_events(
                replace(prepared, operation="followup", problem=active),
                state,
            ):
                yield event
            return

        if decision.intent == "record":
            if active.status != "explained":
                async for event in self._clarify_events(
                    prepared,
                    state,
                    options=("supplement", "correction"),
                    message="当前题目尚未讲解，暂时不能记录。",
                ):
                    yield event
                return
            async for event in self._record_events(
                replace(prepared, operation="record", problem=active),
                state,
            ):
                yield event
            return

        if decision.intent == "feedback":
            if active.status != "explained":
                async for event in self._clarify_events(
                    prepared,
                    state,
                    options=("supplement", "correction"),
                    message="当前题目尚未讲解，暂时不能记录“我懂了”。",
                ):
                    yield event
                return
            async for event in self._feedback_events(
                replace(prepared, operation="feedback", problem=active),
                state,
            ):
                yield event
            return

        async for event in self._clarify_events(
            prepared,
            state,
            options=("query", "tutoring"),
            message="这句话可以理解为查试卷，也可以理解为辅导题目；请明确选择。",
        ):
            yield event

    async def _query_events(
        self,
        prepared: ExecuteTurn,
        request: Request,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        assert self._make_query is not None
        try:
            service = self._make_query()
        except Exception:
            logger.exception("查询服务初始化失败")
            yield self._failure_event(
                prepared, "QUERY_FAILED", "查询过程中发生错误，请重试。", state
            )
            return

        async for event in service.events(prepared.input.text, request):
            if isinstance(event, ProgressEvent):
                yield TurnProgressEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    stage=event.stage,
                    message=event.message,
                )
                continue

            if isinstance(event, ResultEvent):
                try:
                    self._repository.complete_turn(
                        prepared.conversation_id,
                        prepared.request_id,
                        event,
                    )
                except ConversationStorageError:
                    yield _storage_error_event(prepared)
                    return
                state.saved = True
                yield TurnResultEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    kind="query",
                    result=event,
                )
                return

            assert isinstance(event, ErrorEvent)
            try:
                self._repository.fail_turn(
                    prepared.conversation_id,
                    prepared.request_id,
                    event.code,
                    event.message,
                )
            except ConversationStorageError:
                yield _storage_error_event(prepared)
                return
            state.saved = True
            yield TurnErrorEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                code=event.code,
                message=event.message,
            )
            return

    async def _tutoring_events(
        self,
        prepared: ExecuteTurn,
        request: Request,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        if self._make_tutor is None:
            yield self._failure_event(
                prepared,
                "TUTORING_FAILED",
                self._tutoring_error or "辅导模型尚未配置。",
                state,
            )
            return

        selected = self._selected_attachments(prepared)
        timeout_event = TurnErrorEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            code="TUTORING_FAILED",
            message=_TIMEOUT_MESSAGE,
        )

        async for event in stream_events(
            lambda queue: self._produce_tutoring(
                prepared, selected, queue, state
            ),
            request,
            timeout=_TUTORING_TIMEOUT_SECONDS,
            timeout_event=timeout_event,
            is_terminal=lambda item: isinstance(
                item, (TurnResultEvent, TurnErrorEvent)
            ),
        ):
            if isinstance(event, TurnErrorEvent) and not state.saved:
                saved_event = self._failure_event(
                    prepared, event.code, event.message, state
                )
                yield saved_event
                return
            yield event

    async def _produce_tutoring(
        self,
        prepared: ExecuteTurn,
        selected: list[tuple[str, str]],
        queue: "asyncio.Queue[object]",
        state: _RunState,
    ) -> None:
        async def emit(stage: str, message: str) -> None:
            queue.put_nowait(
                TurnProgressEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    stage=stage,  # type: ignore[arg-type]
                    message=message,
                )
            )

        async def finish(payload: TutoringResult) -> None:
            queue.put_nowait(
                TurnResultEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    kind=prepared.operation,  # type: ignore[arg-type]
                    tutoring=payload,
                )
            )
            state.saved = True

        try:
            assert self._make_tutor is not None
            tutor = self._make_tutor()
            attachments = self._attachments_by_ids(
                prepared.conversation_id, selected
            )
            statement_images = load_images(
                attachments,
                root=self._attachments_root,
                roles={"statement", "supplement"},
            )
            solution_images = load_images(
                attachments, root=self._attachments_root, roles={"solution"}
            )

            source, student_answer, solution_text = self._effective_problem_fields(
                prepared
            )
            workflow = build_tutoring_workflow(tutor, emit)
            result_state = await workflow.ainvoke(
                {
                    "text": self._tutoring_input_text(prepared),
                    "statement_images": statement_images,
                    "solution_images": solution_images,
                    "student_answer": student_answer,
                    "solution_text": solution_text,
                }
            )
            understanding = result_state["understanding"]
            unclear = (
                understanding.clarity == "unclear"
                or understanding.multiple_problems
            )
            explanation = result_state.get("explanation")

            if prepared.operation == "supplement":
                assert prepared.problem is not None
                updated = UpdatedProblem(
                    statement_text=understanding.statement,
                    status="awaiting_supplement" if unclear else "explained",
                    clarity=understanding.clarity,
                    unclear_points=understanding.unclear_points,
                    uncertainties=understanding.uncertainties,
                    source=source,
                    student_answer_text=student_answer,
                    solution_text=solution_text,
                    explanation=None if unclear else explanation,
                )
                record_id = self._record_id_for(prepared.problem)
                holder: dict[str, TutoringResult] = {}

                def build_supplement(saved_problem: ProblemRecord) -> str:
                    payload = (
                        build_needs_clarification(saved_problem)
                        if unclear
                        else build_explanation_result(
                            saved_problem,
                            explanation,
                            record_id=record_id,
                        )
                    )
                    holder["payload"] = payload
                    return json.dumps(
                        payload.model_dump(mode="json"), ensure_ascii=False
                    )

                self._repository.save_supplement_turn(
                    problem=prepared.problem,
                    request_id=prepared.request_id,
                    updated=updated,
                    attachments=selected,
                    output_builder=build_supplement,
                )
                await finish(holder["payload"])
                return

            if prepared.operation == "correction" and unclear:
                assert prepared.problem is not None
                questions = [
                    f"请补充或确认：{item}"
                    for item in (
                        understanding.unclear_points
                        or ["题目中的关键条件或符号仍不清楚。"]
                    )
                ]
                payload = NeedsClarificationResult(
                    problem=build_problem_view(prepared.problem),
                    questions=questions[:5],
                    unclear_points=understanding.unclear_points,
                )
                output_json = json.dumps(
                    payload.model_dump(mode="json"),
                    ensure_ascii=False,
                )
                self._repository.complete_output_turn(
                    prepared.conversation_id,
                    prepared.request_id,
                    kind="correction",
                    output_json=output_json,
                )
                await finish(payload)
                return

            new_problem = NewProblem(
                statement_text=understanding.statement,
                status="awaiting_supplement" if unclear else "explained",
                clarity=understanding.clarity,
                unclear_points=understanding.unclear_points,
                uncertainties=understanding.uncertainties,
                source=source,
                student_answer_text=student_answer,
                solution_text=solution_text,
                explanation=None if unclear else explanation,
                root_problem_id=(
                    prepared.problem.root_problem_id
                    if prepared.operation == "correction" and prepared.problem
                    else None
                ),
                supersedes_problem_id=(
                    prepared.problem.id
                    if prepared.operation == "correction" and prepared.problem
                    else None
                ),
            )
            record_id = (
                self._record_id_for(prepared.problem)
                if prepared.operation == "correction" and prepared.problem
                else None
            )
            holder = {}

            def build_problem(saved_problem: ProblemRecord) -> str:
                payload = (
                    build_needs_clarification(saved_problem)
                    if unclear
                    else build_explanation_result(
                        saved_problem,
                        explanation,
                        record_id=record_id,
                    )
                )
                holder["payload"] = payload
                return json.dumps(
                    payload.model_dump(mode="json"), ensure_ascii=False
                )

            self._repository.save_problem_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind=prepared.operation,
                problem=new_problem,
                attachments=selected,
                output_builder=build_problem,
            )
            await finish(holder["payload"])
        except QueryError as exc:
            queue.put_nowait(
                self._failure_event(prepared, exc.code, exc.message, state)
            )
        except ConversationStorageError:
            queue.put_nowait(_storage_error_event(prepared))
        except (AttachmentInvalidError, InvalidImageError, ProblemNotReadyError) as exc:
            queue.put_nowait(
                self._failure_event(
                    prepared, "TUTORING_FAILED", str(exc), state
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("辅导轮次执行失败")
            queue.put_nowait(
                self._failure_event(
                    prepared, "TUTORING_FAILED", _TUTORING_MESSAGE, state
                )
            )
        finally:
            queue.put_nowait(SENTINEL)

    async def _followup_events(
        self,
        prepared: ExecuteTurn,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        assert prepared.problem is not None
        if self._make_tutor is None:
            yield self._failure_event(
                prepared,
                "TUTORING_FAILED",
                self._tutoring_error or "辅导模型尚未配置。",
                state,
            )
            return
        try:
            recent = self._repository.get_problem_followups(prepared.problem.id)
            pairs = [
                (item["question"], item["answer"]) for item in recent[-3:]
            ]
            tutor = self._make_tutor()
            answer = await tutor.answer_followup(
                statement=prepared.problem.statement_text,
                explanation=prepared.problem.explanation,
                recent_followups=pairs,
                question=prepared.input.text,
            )
            payload = FollowupResult(
                problem_id=prepared.problem.id,
                answer=answer.answer,
                related_steps=answer.related_steps,
            )
            self._repository.save_followup_turn(
                problem=prepared.problem,
                request_id=prepared.request_id,
                question=prepared.input.text,
                answer=answer.answer,
                output_json=json.dumps(
                    payload.model_dump(mode="json"), ensure_ascii=False
                ),
            )
            state.saved = True
            yield TurnResultEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="followup",
                tutoring=payload,
            )
        except QueryError as exc:
            yield self._failure_event(prepared, exc.code, exc.message, state)
        except ConversationStorageError:
            yield _storage_error_event(prepared)
        except Exception:
            logger.exception("追问执行失败")
            yield self._failure_event(
                prepared, "TUTORING_FAILED", _TUTORING_MESSAGE, state
            )

    async def _record_events(
        self,
        prepared: ExecuteTurn,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        assert prepared.problem is not None
        if self._records is None:
            yield self._failure_event(
                prepared, "STORAGE_UNAVAILABLE", "错题记录服务不可用。", state
            )
            return
        existing = self._repository.get_record_by_root(
            prepared.conversation_id, prepared.problem.root_problem_id
        )
        needs_update = bool(
            existing is not None
            and existing.problem_version < prepared.problem.version
        )
        holder: dict[str, TutoringResult] = {}

        def build_output(record_row: Any, created: bool) -> str:
            summary = self._records.summary_for(record_row)
            if created:
                payload: TutoringResult = RecordCreatedResult(record=summary)
            else:
                payload = RecordExistsResult(
                    record=summary,
                    needs_update=needs_update,
                    message=(
                        "这道题已有旧版本记录，可在错题本中更新。"
                        if needs_update
                        else "这道题已经记录过了。"
                    ),
                )
            holder["payload"] = payload
            return json.dumps(payload.model_dump(mode="json"), ensure_ascii=False)

        try:
            self._records.create_for_turn(
                conversation_id=prepared.conversation_id,
                problem_id=prepared.problem.id,
                request_id=prepared.request_id,
                output_builder=build_output,
            )
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return
        except (ProblemNotFoundError, ProblemNotReadyError) as exc:
            yield self._failure_event(
                prepared, "TUTORING_FAILED", str(exc), state
            )
            return
        except Exception:
            logger.exception("创建错题记录失败")
            yield self._failure_event(
                prepared, "TUTORING_FAILED", "记录错题时发生错误，请重试。", state
            )
            return

        state.saved = True
        payload = holder.get("payload")
        if payload is None:
            yield self._failure_event(
                prepared, "TUTORING_FAILED", "记录结果不完整，请重试。", state
            )
            return
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind="record",
            tutoring=payload,
        )

    async def _feedback_events(
        self,
        prepared: ExecuteTurn,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        assert prepared.problem is not None
        payload = FeedbackResult(
            problem_id=prepared.problem.id,
            message=(
                "已记录你的自述：本次已经听懂。这只代表本次反馈，"
                "不代表系统认定你已永久掌握。"
            ),
        )
        try:
            self._repository.save_feedback_turn(
                problem=prepared.problem,
                request_id=prepared.request_id,
                output_json=json.dumps(
                    payload.model_dump(mode="json"), ensure_ascii=False
                ),
            )
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return
        except Exception:
            logger.exception("保存学生反馈失败")
            yield self._failure_event(
                prepared, "TUTORING_FAILED", "记录反馈时发生错误，请重试。", state
            )
            return
        state.saved = True
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind="feedback",
            tutoring=payload,
        )

    async def _clarify_events(
        self,
        prepared: ExecuteTurn,
        state: _RunState,
        *,
        options: tuple[str, ...],
        message: str,
    ) -> AsyncIterator[TurnEvent]:
        payload = IntentClarificationResult(
            message=message,
            options=list(options),
        )
        try:
            self._repository.complete_output_turn(
                prepared.conversation_id,
                prepared.request_id,
                kind="problem",
                output_json=json.dumps(
                    payload.model_dump(mode="json"), ensure_ascii=False
                ),
            )
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return
        state.saved = True
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind="problem",
            tutoring=payload,
        )

    async def _dynamic_schedule_events(
        self,
        prepared: ExecuteTurn,
        request: Request,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        """有界调度：决策 → 执行 → 观察 → 再决策或回复。"""

        timeout_event = TurnErrorEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            code="TUTORING_FAILED",
            message=_TIMEOUT_MESSAGE,
        )
        async for event in stream_events(
            lambda queue: self._produce_dynamic_review(prepared, queue, state),
            request,
            timeout=_DYNAMIC_TIMEOUT_SECONDS,
            timeout_event=timeout_event,
            is_terminal=lambda item: isinstance(
                item, (TurnResultEvent, TurnErrorEvent)
            ),
        ):
            if isinstance(event, TurnErrorEvent) and not state.saved:
                yield self._failure_event(prepared, event.code, event.message, state)
                return
            yield event

    async def _produce_dynamic_review(
        self,
        prepared: ExecuteTurn,
        queue: "asyncio.Queue[object]",
        state: _RunState,
    ) -> None:
        def emit(stage: str, message: str) -> None:
            queue.put_nowait(
                TurnProgressEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    stage=stage,  # type: ignore[arg-type]
                    message=message,
                )
            )

        try:
            emit("scheduling", "正在根据上下文分析复习需求")
            assert self._make_scheduler is not None
            if prepared.recovery:
                recovery = json.loads(json.dumps(prepared.recovery))
                retry_plans = [entry["plan"] for entry in recovery.get("actions", [])
                               if not entry.get("outcome") or entry["outcome"].get("details", {}).get("retryable")]
                retry_plans.extend(recovery.get("pending", []))
                retry_plans = list({canonical_json({"action": plan["action"], "parameters": plan.get("parameters", {})}): plan
                                    for plan in retry_plans}.values())
                # 前次的成功记录留存；失败尝试只在原轮次保留，恢复轮次记录新的执行。
                recovery["actions"] = [entry for entry in recovery.get("actions", [])
                                       if entry.get("outcome", {}).get("status") == "success"]
                ctx = self._restore_dynamic_context(prepared.conversation_id, recovery.get("context", {}))
                ctx.recovery = recovery
                plans = [ActionPlan.model_validate(plan) for plan in retry_plans]
                scheduler = None
                max_rounds = 1
            else:
                scheduler = self._make_scheduler()
                ctx = self._load_dynamic_context(prepared.conversation_id)
                ctx.recovery = {"version": 1, "input": json.loads(self._canonical(prepared.input)),
                                "actions": [], "pending": [], "context": self._context_snapshot(ctx)}
                max_rounds = 2
            carried = [ActionResultItem.model_validate(entry["outcome"]) for entry in ctx.recovery["actions"]]
            self._repository.save_recovery(prepared.conversation_id, prepared.request_id, ctx.recovery)
            last_sugs: list[str] = []
            ctx_record = self._repository.get_context(prepared.conversation_id)
            if ctx_record and ctx_record.last_suggestions_json:
                with suppress(Exception):
                    last_sugs = [
                        it["label"]
                        for it in json.loads(ctx_record.last_suggestions_json)
                    ]
            recent_summary = self._recent_turns_summary(prepared.conversation_id)

            async def execute(action: ActionPlan, review_state: dict[str, Any]) -> ExecutionResult:
                return await self._execute_dynamic_action(
                    prepared, action, review_state, ctx, emit
                )

            graph = DynamicReviewGraph(scheduler, execute, max_rounds=max_rounds,
                                       resume_actions=plans if prepared.recovery and plans else None)
            if prepared.recovery and not plans:
                graph = DynamicReviewGraph(self._make_scheduler(), execute)
            final_state = await graph.run(
                {
                    "query": prepared.input.text,
                    "confirmed_course": ctx.recovery.get("requested_course") or ctx.confirmed_course,
                    "active_problem_text": (
                        ctx.active_problem.statement_text
                        if ctx.active_problem is not None
                        else None
                    ),
                    "active_problem_status": (
                        ctx.active_problem.status
                        if ctx.active_problem is not None
                        else None
                    ),
                    "active_exercise_statement": (
                        ctx.active_exercise.statement_text
                        if ctx.active_exercise is not None
                        else None
                    ),
                    "active_exercise_status": (
                        ctx.active_exercise.status
                        if ctx.active_exercise is not None
                        else None
                    ),
                    "active_exercise_needs_supplement": self._exercise_needs_supplement(
                        ctx.active_exercise
                    ),
                    "last_suggestions": last_sugs,
                    "recent_turns_summary": recent_summary,
                    "has_attachments": bool(prepared.input.attachments),
                    "rounds": 0,
                    "executed_count": 0,
                    "pending": [],
                    "observations": [],
                    "assistant_reply": None,
                    "suggested_actions": [],
                    "replan_error": None,
                }
            )

            composite_results = carried + self._dynamic_composite_results(final_state)
            reply_text = self._dynamic_reply(final_state, composite_results)
            suggestions = self._dynamic_suggestions(final_state, composite_results)

            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="dynamic",
                result=ctx.result,
                tutoring=ctx.tutoring,
                exercise=ctx.exercise_view,
                evaluation=ctx.evaluation_view,
                composite_results=composite_results,
                assistant_message=reply_text,
                suggestions=suggestions,
                problem_id=ctx.problem_id,
                exercise_id=ctx.exercise_id,
            )
            self._repository.upsert_context(
                prepared.conversation_id,
                confirmed_course=ctx.confirmed_course,
                active_problem_id=ctx.problem_id,
                active_exercise_id=ctx.exercise_id,
                last_suggestions_json=json.dumps(
                    [it.model_dump(mode="json") for it in suggestions],
                    ensure_ascii=False,
                ),
                last_action_summary=(
                    composite_results[-1].message if composite_results else None
                ),
            )
        except ConversationStorageError:
            queue.put_nowait(_storage_error_event(prepared))
            return
        except QueryError as exc:
            queue.put_nowait(self._failure_event(prepared, exc.code, exc.message, state))
            return
        except Exception:
            logger.exception("动态复习调度执行失败")
            queue.put_nowait(
                self._failure_event(
                    prepared,
                    "TUTORING_FAILED",
                    "处理复习请求时发生错误，请重试。",
                    state,
                )
            )
            return

        state.saved = True
        queue.put_nowait(
            TurnResultEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="dynamic",
                result=ctx.result,
                tutoring=ctx.tutoring,
                exercise=ctx.exercise_view,
                evaluation=ctx.evaluation_view,
                composite_results=composite_results,
                assistant_message=reply_text,
                suggestions=suggestions,
                exercise_id=ctx.exercise_id,
            )
        )
        queue.put_nowait(SENTINEL)

    def _load_dynamic_context(self, conversation_id: str) -> _DynamicContext:
        ctx_record = self._repository.get_context(conversation_id)
        active_problem = self._repository.get_active_problem(conversation_id)
        active_exercise = self._repository.get_active_exercise(conversation_id)
        return _DynamicContext(
            conversation_id=conversation_id,
            confirmed_course=ctx_record.confirmed_course if ctx_record else None,
            active_problem=active_problem,
            active_exercise=active_exercise,
            problem_id=active_problem.id if active_problem is not None else None,
            exercise_id=active_exercise.id if active_exercise is not None else None,
        )

    @staticmethod
    def _context_snapshot(ctx: _DynamicContext) -> dict[str, Any]:
        return {"confirmed_course": ctx.confirmed_course, "problem_id": ctx.problem_id,
                "exercise_id": ctx.exercise_id,
                **{name: getattr(ctx, name).model_dump(mode="json") if getattr(ctx, name) is not None else None
                   for name in ("result", "tutoring", "exercise_view", "evaluation_view")}}

    def _restore_dynamic_context(self, conversation_id: str, snapshot: dict) -> _DynamicContext:
        from pydantic import TypeAdapter
        problem_id, exercise_id = snapshot.get("problem_id"), snapshot.get("exercise_id")
        problem = self._repository.get_problem(problem_id) if problem_id else None
        exercise = self._repository.get_exercise(exercise_id) if exercise_id else None
        if ((problem is not None and problem.conversation_id != conversation_id)
            or (exercise is not None and exercise.conversation_id != conversation_id)):
            raise ProblemNotFoundError("恢复目标题目不属于当前会话。")
        return _DynamicContext(
            conversation_id=conversation_id, confirmed_course=snapshot.get("confirmed_course"),
            active_problem=problem, active_exercise=exercise, problem_id=problem_id, exercise_id=exercise_id,
            result=ResultEvent.model_validate(snapshot["result"]) if snapshot.get("result") else None,
            tutoring=TypeAdapter(TutoringResult).validate_python(snapshot["tutoring"]) if snapshot.get("tutoring") else None,
            exercise_view=ExerciseView.model_validate(snapshot["exercise_view"]) if snapshot.get("exercise_view") else None,
            evaluation_view=ExerciseEvaluation.model_validate(snapshot["evaluation_view"]) if snapshot.get("evaluation_view") else None,
        )

    def _action_result(self, prepared: ExecuteTurn, ctx: _DynamicContext, outcome: ActionOutcome) -> ActionOutcome:
        if ctx.action_index >= 0:
            ctx.recovery["actions"][ctx.action_index]["outcome"] = outcome.to_dict()
            ctx.recovery["context"] = self._context_snapshot(ctx)
            if outcome.needs_user and outcome.status != "failed":
                ctx.recovery["pending"] = []
            self._repository.save_recovery(prepared.conversation_id, prepared.request_id, ctx.recovery)
        return outcome

    def _recent_turns_summary(self, conversation_id: str) -> str:
        recent_turns = self._repository.list_turns(conversation_id)
        if not recent_turns:
            return ""
        summary_lines: list[str] = []
        for turn in recent_turns[-4:]:
            summary_lines.append(f"用户：{turn.query}")
            if turn.assistant_message:
                summary_lines.append(f"助手：{turn.assistant_message}")
            elif turn.tutoring is not None:
                summary_lines.append(f"助手：{turn.tutoring.kind}")
        return "\n".join(summary_lines)

    @staticmethod
    def _dynamic_composite_results(final_state: dict[str, Any]) -> list[ActionResultItem]:
        items: list[ActionResultItem] = []
        for observation in final_state.get("observations") or []:
            status = observation.get("status")
            if status not in ("success", "failed", "clarification"):
                status = "failed"
            items.append(
                ActionResultItem(
                    action=str(observation.get("action") or "unknown"),
                    status=status,  # type: ignore[arg-type]
                    message=str(observation.get("message") or ""),
                    details=dict(observation.get("details") or {}),
                )
            )
        return items

    @staticmethod
    def _dynamic_reply(
        final_state: dict[str, Any],
        composite_results: list[ActionResultItem],
    ) -> str:
        all_failed = [it for it in composite_results if it.status == "failed"]
        failed = [it for it in all_failed if not it.details.get("skipped")] or all_failed
        clarified = [it for it in composite_results if it.status == "clarification"]
        assistant_reply = final_state.get("assistant_reply")
        replan_error = final_state.get("replan_error")

        if failed:
            clean = lambda message: message.rstrip("。.!！;； ")
            parts = ["以下操作未完成：" + "；".join(clean(it.message) for it in failed) + "。"]
            succeeded = [it.message for it in composite_results if it.status == "success"]
            if succeeded:
                parts.append("已完成：" + "；".join(clean(it) for it in succeeded) + "。")
            if any(it.details.get("skipped") for it in all_failed):
                parts.append("依赖未成功步骤的后续操作或重复操作已跳过。")
            if any(it.details.get("retryable") for it in failed):
                parts.append("可以重试未完成步骤；已成功操作会保留。")
            else:
                parts.append("请按上述原因调整输入或选择其他操作。")
            if replan_error:
                parts.append("后续建议也没有生成成功，请稍后再试。")
            return "".join(parts)

        if clarified:
            return clarified[-1].message
        if replan_error:
            succeeded = [it.message for it in composite_results if it.status == "success"]
            prefix = "后续建议没有生成成功，但已完成：" if succeeded else "后续建议没有生成成功："
            return prefix + "；".join(succeeded or [replan_error])
        if any(it.action == "query_papers" and it.status == "success" for it in composite_results):
            # 筛选经过程序核对，不能再沿用规划时可能擅自限定期末的回复。
            return "\n".join(it.message for it in composite_results)
        if assistant_reply:
            return assistant_reply
        if composite_results:
            return "\n".join(it.message for it in composite_results)
        return "已为您处理完成。"

    @staticmethod
    def _dynamic_suggestions(
        final_state: dict[str, Any],
        composite_results: list[ActionResultItem],
    ) -> list[SuggestionItem]:
        if any(it.status == "failed" for it in composite_results):
            return []
        return [
            SuggestionItem(label=label, action=label)
            for label in (final_state.get("suggested_actions") or [])
        ]

    async def _execute_dynamic_action(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        review_state: dict[str, Any],
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ExecutionResult:
        """执行一个动作；失败作为观察结果返回，交给下一轮决策。"""

        if action.action == "clarify_or_reply" and any(
            entry.get("outcome", {}).get("status") == "failed" for entry in ctx.recovery.get("actions", [])
        ):
            # 失败后的解释性回复不覆盖原计划中的依赖步骤，也不成为重试动作。
            index = ctx.action_index
            ctx.action_index = -1
            try:
                outcome = await self._run_dynamic_action(prepared, action, review_state, ctx, emit)
            finally:
                ctx.action_index = index
            return ExecutionResult(outcome, self._dynamic_state_patch(ctx, review_state))

        previous_course = ctx.confirmed_course
        if not ctx.recovery:
            ctx.recovery = {"version": 1, "input": json.loads(self._canonical(prepared.input)), "actions": [], "pending": []}
        ctx.action_index = len(ctx.recovery["actions"])
        ctx.recovery["actions"].append({"id": str(uuid.uuid4()), "plan": action.model_dump(mode="json"),
                                        "before": self._context_snapshot(ctx)})
        remaining = [item.model_dump(mode="json") if isinstance(item, ActionPlan) else item
                     for item in (review_state.get("pending") or [])[1:]]
        carried_pending = [item for item in ctx.recovery.get("pending", []) if item.get("action") != action.action]
        ctx.recovery["pending"] = list({canonical_json({"action": item["action"], "parameters": item.get("parameters", {})}): item
                                        for item in carried_pending + remaining}.values())
        self._repository.save_recovery(prepared.conversation_id, prepared.request_id, ctx.recovery)
        requested_course = review_state.get("confirmed_course") or action.parameters.get(
            "course"
        )
        if isinstance(requested_course, str) and requested_course.strip():
            # 必须在动作内部查询前同步，否则 query_papers 仍会用旧课程。
            ctx.confirmed_course = requested_course.strip()
            ctx.recovery["requested_course"] = requested_course.strip()

        try:
            outcome = await self._run_dynamic_action(
                prepared, action, review_state, ctx, emit
            )
        except ConversationStorageError:
            raise
        except (AttachmentInvalidError, InvalidImageError) as exc:
            outcome = ActionOutcome(action.action, "failed", str(exc), error_code="INVALID_INPUT")
        except (ConversationConflictError, ProblemNotFoundError, ProblemNotReadyError) as exc:
            outcome = ActionOutcome(action.action, "failed", str(exc), error_code="BUSINESS_REJECTED", needs_user=True)
        except QueryError as exc:
            outcome = ActionOutcome(action.action, "failed", exc.message, error_code=exc.code, retryable=exc.code in RECOVERABLE_CODES)
        except Exception:
            logger.exception("动态动作执行失败：%s", action.action)
            outcome = ActionOutcome(
                action.action, "failed", "该操作执行失败，请稍后重试。", error_code="TUTORING_FAILED", retryable=True
            )
        if outcome.status != "success":
            # 失败/澄清不持久化新课程，避免把无效课程写进上下文。
            ctx.confirmed_course = previous_course
        self._action_result(prepared, ctx, outcome)
        return ExecutionResult(outcome, self._dynamic_state_patch(ctx, review_state))

    @staticmethod
    def _dynamic_state_patch(
        ctx: _DynamicContext, review_state: dict[str, Any]
    ) -> dict[str, Any]:
        active_problem = ctx.active_problem
        active_exercise = ctx.active_exercise
        return {
            "confirmed_course": ctx.confirmed_course,
            "active_problem_text": (
                active_problem.statement_text
                if active_problem is not None
                else review_state.get("active_problem_text")
            ),
            "active_problem_status": (
                active_problem.status
                if active_problem is not None
                else review_state.get("active_problem_status")
            ),
            "active_exercise_statement": (
                active_exercise.statement_text
                if active_exercise is not None
                else None
            ),
            "active_exercise_status": (
                active_exercise.status if active_exercise is not None else None
            ),
            "active_exercise_needs_supplement": (
                ConversationService._exercise_needs_supplement(active_exercise)
            ),
        }

    async def _run_dynamic_action(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        review_state: dict[str, Any],
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = action.action
        if name == "introduce":
            course = review_state.get("confirmed_course") or ctx.confirmed_course
            if course:
                ctx.confirmed_course = course
            course_label = course or "所选课程"
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "success",
                f"已确认复习科目为【{course_label}】",
                {"course": course_label},
            ))

        if name == "query_papers":
            return self._dynamic_query(prepared, action, ctx, emit)

        if name in ("explain_problem", "tutoring"):
            return await self._dynamic_explain(prepared, action, ctx, emit)

        if name in ("followup_problem", "followup"):
            return await self._dynamic_followup(prepared, ctx, emit)

        if name == "generate_exercise":
            return await self._dynamic_generate_exercise(prepared, ctx, emit)

        if name == "evaluate_exercise":
            return await self._dynamic_evaluate_exercise(prepared, ctx, emit)

        if name == "reveal_solution":
            with self._repository.action_write():
                return self._action_result(prepared, ctx, self._dynamic_reveal(ctx))

        if name == "record_mistake":
            with self._repository.action_write():
                return self._dynamic_record_mistake(prepared, ctx, emit)

        if name == "record_feedback":
            active = ctx.active_problem
            if active is None or active.status != "explained":
                return self._action_result(prepared, ctx, ActionOutcome(
                    name,
                    "clarification",
                    "当前还没有完成讲解的题目，暂时不能记录“我懂了”。",
                    needs_user=True,
                ))
            with self._repository.action_write():
                self._repository.update_problem_understood(active.id, True)
                ctx.problem_id = active.id
                ctx.tutoring = FeedbackResult(
                    problem_id=active.id,
                    message="已为您记录“我懂了”反馈。随时可以继续追问、练题或保存题目。",
                )
                return self._action_result(prepared, ctx, ActionOutcome(
                    name,
                    "success",
                    "已记录“我懂了”反馈",
                    {"problem_id": active.id},
                ))

        if name == "clarify_or_reply":
            message = review_state.get("assistant_reply") or "请告诉我您希望先做哪一步。"
            return self._action_result(prepared, ctx, ActionOutcome(name, "clarification", message, needs_user=True))

        return self._action_result(prepared, ctx, ActionOutcome(name, "failed", "暂不支持这个操作。"))

    def _dynamic_query(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "query_papers"
        course = action.parameters.get("course") or ctx.confirmed_course
        exam_type = self._requested_exam_type(prepared.input.text, action.parameters.get("exam_type"))
        if not course:
            return ActionOutcome(
                name,
                "clarification",
                "请先告诉我复习哪门课程，我才能查找对应试卷。",
                needs_user=True,
            )
        ctx.confirmed_course = course
        if self._papers is None:
            return ActionOutcome(name, "failed", "试卷目录暂时不可用，请稍后重试。", error_code="CATALOG_UNAVAILABLE", retryable=True)

        emit("retrieving", "正在检索试卷资料")
        try:
            papers = self._papers.list_papers(course=course, exam_type=exam_type)
        except Exception as exc:
            logger.warning("查卷执行失败：%s", exc)
            return ActionOutcome(name, "failed", "试卷检索失败，请稍后重试。", error_code="QUERY_FAILED", retryable=True)

        scope = {None: "全部类型", "midterm": "期中", "final": "期末", "other": "其他类型"}[exam_type]
        if papers.items:
            ctx.result = ResultEvent(
                type="result",
                kind="found",
                message=f"找到 {papers.total} 份【{course}】试卷（{scope}）。",
                filters={"course": course, "exam_type": exam_type},
                items=papers.items,
                total=papers.total,
            )
        else:
            ctx.result = ResultEvent(
                type="result",
                kind="empty",
                message=(
                    f"本次按{scope}筛选，未找到【{course}】匹配的本地试卷，但您可以继续拍照或输入题目"
                    "进行辅导与考点练习。"
                ),
                filters={"course": course, "exam_type": exam_type},
                items=[],
                total=0,
            )
        return ActionOutcome(
            name,
            "success",
            ctx.result.message,
            {"total": papers.total, "course": course, "exam_type": exam_type},
        )

    @staticmethod
    def _requested_exam_type(text: str, planned: Any) -> str | None:
        """模型选择能力，程序检查筛选是否确实出现在本轮请求中。"""
        if re.search(r"不限|不区分|全部类型|所有类型|不要只查|别只查|不只查|不只要", text):
            return None
        markers = {
            "midterm": r"期中|\bmidterm\b",
            "final": r"期末|期终|\bfinal\b",
            "other": r"其[他它]类型|其[他它]考试|\bother\b",
        }
        mentioned = {kind for kind, pattern in markers.items() if re.search(pattern, text, re.IGNORECASE)}
        if not mentioned:
            return None
        # 未指定某一类型时查询全部；“全部期末卷”仍是期末范围。
        if len(mentioned) > 1 and not re.search(r"不要|不查|不找|不看|不是|只要|只查|仅要|仅查", text):
            return None
        if isinstance(planned, str) and planned in mentioned:
            return planned
        if len(mentioned) == 1:
            return next(iter(mentioned))
        return None

    def _dynamic_explain_mode(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        active: ProblemRecord | None,
    ) -> str:
        requested = action.parameters.get("mode")
        if requested not in (None, "new", "supplement", "correction"):
            return "invalid"
        if prepared.input.start_new or active is None:
            return "new"
        if requested == "new":
            return "new"
        if active.status == "awaiting_supplement":
            return "supplement" if requested != "correction" else "correction"
        if requested == "supplement":
            # 已讲解题不接受原地补充，只能建立新版本，避免覆盖旧内容。
            return "correction"
        return "correction"

    async def _dynamic_explain(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "explain_problem"
        if self._make_tutor is None:
            return self._action_result(prepared, ctx, ActionOutcome(name, "failed", "辅导模型尚未配置，暂时不能讲解。"))

        active = ctx.active_problem
        mode = self._dynamic_explain_mode(prepared, action, active)
        if mode == "invalid":
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "我没能确定这是新题、补充还是纠正；请说明要修改哪道题。",
                needs_user=True,
            ))

        selected = list(prepared.input.attachments)
        if active is not None and mode in ("supplement", "correction"):
            existing = [
                (item.id, item.role or "statement") for item in active.attachments
            ]
            if not selected:
                selected = existing
            elif mode == "supplement":
                # 补充图片是在原题基础上追加，旧附件不能丢。
                seen = {item[0] for item in selected}
                selected.extend(item for item in existing if item[0] not in seen)
        records = (
            self._attachments_by_ids(prepared.conversation_id, selected)
            if selected
            else []
        )
        statement_images = load_images(
            records, root=self._attachments_root, roles={"statement", "supplement"}
        )
        solution_images = load_images(
            records, root=self._attachments_root, roles={"solution"}
        )

        if mode == "new":
            problem_text = prepared.input.text
        else:
            assert active is not None
            action_label = "补充" if mode == "supplement" else "纠正"
            if prepared.input.text:
                problem_text = (
                    f"原题干：\n{active.statement_text}\n\n"
                    f"学生{action_label}：\n{prepared.input.text}"
                )
            else:
                problem_text = (
                    f"原题干：\n{active.statement_text}\n\n"
                    f"学生只{action_label}了图片，请结合原题干和新图片整理题目。"
                )

        source = prepared.input.source
        student_answer = prepared.input.student_answer_text
        solution_text = prepared.input.solution_text
        if active is not None and mode in ("supplement", "correction"):
            if source.type == "none":
                source = active.source
            if student_answer is None:
                student_answer = active.student_answer_text
            if solution_text is None:
                solution_text = active.solution_text

        emit("explaining", "正在理解题目并生成分步讲解")
        tutor = self._make_tutor()
        understanding = await tutor.understand_problem(problem_text, statement_images)
        unclear = (
            understanding.clarity == "unclear" or understanding.multiple_problems
        )

        if unclear:
            with self._repository.action_write():
                new_problem = NewProblem(
                    statement_text=understanding.statement,
                    status="awaiting_supplement",
                    clarity="unclear",
                    unclear_points=understanding.unclear_points,
                    uncertainties=understanding.uncertainties,
                    source=source,
                    student_answer_text=student_answer,
                    solution_text=solution_text,
                    explanation=None,
                    root_problem_id=(
                        active.root_problem_id
                        if mode == "correction" and active is not None
                        else None
                    ),
                    supersedes_problem_id=(
                        active.id if mode == "correction" and active is not None else None
                    ),
                )
                if mode == "supplement" and active is not None:
                    saved = self._repository.update_problem_content(
                        active,
                        updated=UpdatedProblem(
                            statement_text=new_problem.statement_text,
                            status=new_problem.status,
                            clarity=new_problem.clarity,
                            unclear_points=new_problem.unclear_points,
                            uncertainties=new_problem.uncertainties,
                            source=new_problem.source,
                            student_answer_text=new_problem.student_answer_text,
                            solution_text=new_problem.solution_text,
                            explanation=None,
                        ),
                        attachments=selected,
                    )
                else:
                    saved = self._repository.create_problem_version(
                        conversation_id=prepared.conversation_id,
                        problem=new_problem,
                        attachments=selected,
                    )
                payload = build_needs_clarification(saved)
                if mode != "supplement":
                    # 新题/纠正已切换题目版本，旧练习不应再作为当前练习。
                    self._repository.clear_active_exercise(prepared.conversation_id)
                    ctx.active_exercise = None
                    ctx.exercise_id = None
                ctx.active_problem = saved
                ctx.problem_id = saved.id
                ctx.tutoring = payload
                questions = "；".join(payload.questions)
                return self._action_result(prepared, ctx, ActionOutcome(
                    name,
                    "clarification",
                    f"题目关键信息还不完整，请补充：{questions}",
                    {"problem_id": saved.id, "version": saved.version},
                    needs_user=True,
                ))

        explanation = await tutor.explain(
            statement=understanding.statement,
            statement_images=statement_images,
            solution_images=solution_images,
            student_answer=student_answer,
            solution_text=solution_text,
        )
        with self._repository.action_write():
            if mode == "supplement" and active is not None:
                saved = self._repository.update_problem_content(
                    active,
                    updated=UpdatedProblem(
                        statement_text=understanding.statement,
                        status="explained",
                        clarity="clear",
                        unclear_points=[],
                        uncertainties=understanding.uncertainties,
                        source=source,
                        student_answer_text=student_answer,
                        solution_text=solution_text,
                        explanation=explanation,
                    ),
                    attachments=selected,
                )
            else:
                saved = self._repository.create_problem_version(
                    conversation_id=prepared.conversation_id,
                    problem=NewProblem(
                        statement_text=understanding.statement,
                        status="explained",
                        clarity="clear",
                        unclear_points=[],
                        uncertainties=understanding.uncertainties,
                        source=source,
                        student_answer_text=student_answer,
                        solution_text=solution_text,
                        explanation=explanation,
                        root_problem_id=(
                            active.root_problem_id
                            if mode == "correction" and active is not None
                            else None
                        ),
                        supersedes_problem_id=(
                            active.id if mode == "correction" and active is not None else None
                        ),
                    ),
                    attachments=selected,
                )
            ctx.active_problem = saved
            ctx.problem_id = saved.id
            if mode != "supplement":
                # 新版本题目不再沿用旧题的练习指针；旧练习记录本身保留。
                self._repository.clear_active_exercise(prepared.conversation_id)
                ctx.active_exercise = None
                ctx.exercise_id = None
            ctx.tutoring = build_explanation_result(
                saved, explanation, record_id=self._record_id_for(saved)
            )
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "success",
                "题目已完成解析与分步讲解",
                {"problem_id": saved.id, "version": saved.version},
            ))

    async def _dynamic_followup(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "followup_problem"
        active = ctx.active_problem
        if active is None or active.status != "explained" or active.explanation is None:
            return ActionOutcome(
                name,
                "clarification",
                "当前还没有完成讲解的题目；请先上传题目并完成讲解，再针对题目追问。",
                needs_user=True,
            )
        if self._make_tutor is None:
            return ActionOutcome(name, "failed", "辅导模型尚未配置，暂时不能回答追问。")

        emit("answering", "正在解答您对当前题目的疑问")
        history = self._repository.get_problem_followups(active.id)
        recent_followups = [
            (item["question"], item["answer"]) for item in history[-3:]
        ]
        tutor = self._make_tutor()
        answer = await tutor.answer_followup(
            statement=active.statement_text,
            explanation=active.explanation,
            recent_followups=recent_followups,
            question=prepared.input.text,
        )
        ctx.problem_id = active.id
        ctx.tutoring = FollowupResult(
            problem_id=active.id,
            answer=answer.answer,
            related_steps=answer.related_steps,
        )
        return ActionOutcome(
            name,
            "success",
            "已解答您的追问疑问",
            {
                "problem_id": active.id,
                "history_count": len(recent_followups),
            },
        )

    async def _dynamic_generate_exercise(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "generate_exercise"
        active = ctx.active_problem
        if active is None or active.status != "explained" or active.explanation is None:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "需要先有一道完成讲解的题目，才能生成类似练习。请先上传或粘贴题目。",
                needs_user=True,
            ))
        if self._make_exercise_agent is None:
            return self._action_result(prepared, ctx, ActionOutcome(name, "failed", "练习生成模型尚未配置。"))

        emit("generating_exercise", "正在根据核心考点生成类似练习题")
        knowledge_points = ", ".join(
            point.name for point in active.explanation.knowledge_points
        )
        steps_summary = "; ".join(
            f"步骤{step.index}:{step.title}" for step in active.explanation.steps
        )
        agent = self._make_exercise_agent()
        recent = self._repository.list_recent_exercise_statements(
            prepared.conversation_id, limit=MAX_RECENT_EXERCISES
        )
        avoid: list[str] = []
        generated: GeneratedExercise | None = None
        attempts_used = 0
        for _ in range(MAX_GENERATION_ATTEMPTS):
            attempts_used += 1
            candidate = await agent.generate_exercise(
                original_statement=active.statement_text,
                knowledge_points=knowledge_points,
                steps_summary=steps_summary,
                course=ctx.confirmed_course,
                recent_statements=recent,
                avoid_statements=avoid or None,
            )
            if not is_duplicate_statement(
                candidate.statement,
                recent,
                original=active.statement_text,
            ):
                generated = candidate
                break
            avoid.append(candidate.statement)
            logger.info("类似题生成与近期题目重复，触发一次有界重生成。")
        if generated is None:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "failed",
                "这次没能生成与近期题目明显不同的新题，未保存重复练习；请稍后重试或换一个知识点。",
                {"reason": "duplicate_exercise", "generation_attempts": attempts_used},
                needs_user=True,
                error_code="MODEL_RESPONSE_INVALID",
                retryable=True,
            ))
        with self._repository.action_write():
            exercise = self._repository.create_exercise(
                id=str(uuid.uuid4()),
                conversation_id=prepared.conversation_id,
                problem_id=active.id,
                root_problem_id=active.root_problem_id,
                statement_text=generated.statement,
                reference_solution_text=generated.reference_solution,
                knowledge_point=generated.knowledge_point,
            )
            ctx.active_exercise = exercise
            ctx.exercise_id = exercise.id
            ctx.exercise_view = self._build_exercise_view(exercise)
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "success",
                "已生成针对性练习题（待作答）",
                {
                    "exercise_id": exercise.id,
                    "knowledge_point": generated.knowledge_point,
                },
                needs_user=True,
            ))

    async def _dynamic_evaluate_exercise(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "evaluate_exercise"
        exercise = ctx.active_exercise
        if exercise is None:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "当前没有可以作答的练习题，请先让我生成一道类似题。",
                needs_user=True,
            ))
        plan = self._plan_exercise_attempt(exercise, prepared.input)
        if plan.error:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "failed",
                plan.error,
                needs_user=True,
                error_code="BUSINESS_REJECTED",
            ))
        if not plan.reuse and not (
            (plan.answer or "").strip()
            or (plan.solution or "").strip()
            or plan.attachments
        ):
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "请先写出最终答案或上传解题过程图片，我再进行评价。",
                needs_user=True,
            ))

        emit("evaluating_exercise", "正在评价您的作答与推导过程")
        saved, attempt = self._save_exercise_attempt(prepared, exercise, plan)
        images = load_images(
            attempt.attachments,
            root=self._attachments_root,
            roles={
                "statement",
                "solution",
                "supplement",
                "student_answer",
                "student_solution",
            },
        )
        ctx.active_exercise = saved
        ctx.exercise_id = saved.id
        ctx.exercise_view = self._build_exercise_view(saved)

        if self._make_exercise_agent is None:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "failed",
                "作答已保存，但练习评价模型尚未配置，请先配置评价模型。",
                {"exercise_id": saved.id, "answer_saved": True},
                needs_user=True,
            ))

        agent = self._make_exercise_agent()
        try:
            evaluation = await agent.evaluate_answer(
                statement=exercise.statement_text,
                reference_solution=exercise.reference_solution_text,
                student_answer=attempt.student_answer_text,
                student_solution_text=attempt.student_solution_text,
                images=images,
                answered_after_revealed=attempt.answered_after_revealed,
                attempt_kind=attempt.kind,
            )
        except Exception as exc:
            logger.warning("作答评价失败：%s", exc)
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "failed",
                "作答已保存，但评价没有完成，请重试。",
                {"exercise_id": saved.id, "answer_saved": True},
                needs_user=True, error_code=exc.code if isinstance(exc, QueryError) else "TUTORING_FAILED",
                retryable=not isinstance(exc, QueryError) or exc.code in RECOVERABLE_CODES,
            ))

        effective = self._enforce_independence(evaluation, attempt)
        evaluation_json = json.dumps(
            effective.model_dump(mode="json"), ensure_ascii=False
        )
        with self._repository.action_write():
            try:
                evaluated = self._repository.save_attempt_evaluation(
                    attempt.id, evaluation_json=evaluation_json
                )
            except ConversationConflictError:
                current = self._repository.get_exercise(saved.id)
                same_attempt = (
                    current.current_attempt
                    if current is not None
                    else None
                )
                if (
                    same_attempt is None
                    or same_attempt.id != attempt.id
                    or same_attempt.evaluation_json is None
                ):
                    return self._action_result(prepared, ctx, ActionOutcome(
                        name,
                        "failed",
                        "评价结果没有保存成功，请刷新后重试。",
                        {"exercise_id": saved.id, "answer_saved": True},
                        needs_user=True,
                    ))
                evaluated = current

            evaluation_view = self._build_evaluation_view(evaluated)
            ctx.active_exercise = evaluated
            ctx.exercise_id = evaluated.id
            ctx.exercise_view = self._build_exercise_view(evaluated)
            ctx.evaluation_view = evaluation_view
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "success",
                "作答已完成评价",
                {
                    "exercise_id": evaluated.id,
                    "is_correct": (
                        evaluation_view.is_correct if evaluation_view is not None else None
                    ),
                    "answered_after_revealed": attempt.answered_after_revealed,
                },
            ))

    def _dynamic_reveal(self, ctx: _DynamicContext) -> ActionOutcome:
        name = "reveal_solution"
        exercise = ctx.active_exercise
        if exercise is None:
            return ActionOutcome(
                name,
                "clarification",
                "当前没有可以查看解答的练习题。",
                needs_user=True,
            )
        try:
            revealed = self._repository.reveal_exercise_solution(
                exercise.id, conversation_id=ctx.conversation_id
            )
        except ProblemNotFoundError as exc:
            return ActionOutcome(name, "failed", str(exc), needs_user=True)
        ctx.active_exercise = revealed
        ctx.exercise_id = revealed.id
        ctx.exercise_view = self._build_exercise_view(
            revealed, allow_solution=True
        )
        return ActionOutcome(
            name,
            "success",
            "已为您展示参考解答",
            {"exercise_id": revealed.id},
            needs_user=True,
        )

    def _dynamic_record_mistake(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "record_mistake"
        active = ctx.active_problem
        if active is None or active.status != "explained" or active.explanation is None:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "当前还没有完成讲解的题目，暂时不能保存到错题本。",
                needs_user=True,
            ))
        if self._records is None:
            return self._action_result(prepared, ctx, ActionOutcome(name, "failed", "错题记录服务不可用。"))

        emit("recording", "正在将本题收录至错题本")
        try:
            detail, created, _ = self._records.create_from_problem(
                request_id=prepared.request_id,
                conversation_id=prepared.conversation_id,
                problem_id=active.id,
            )
        except MistakeRecordConflictError:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "success", "该题目已在错题本中。", {"problem_id": active.id}
            ))
        record_row = self._repository.get_record(detail.id)
        if record_row is None:
            return self._action_result(prepared, ctx, ActionOutcome(name, "failed", "保存结果读取失败，请稍后重试。"))
        summary = self._records.summary_for(record_row)
        if created:
            ctx.tutoring = RecordCreatedResult(record=summary)
            message = f"已成功加入错题本：{detail.title}"
        else:
            ctx.tutoring = RecordExistsResult(
                record=summary, message="这道题已经记录过了。"
            )
            message = f"错题本中已有本题：{detail.title}"
        ctx.problem_id = active.id
        return self._action_result(prepared, ctx, ActionOutcome(
            name, "success", message, {"record_id": detail.id}
        ))

    def _build_evaluation_view(
        self, exercise: ExerciseRecord
    ) -> ExerciseEvaluation | None:
        if not exercise.evaluation_json:
            return None
        with suppress(Exception):
            return ExerciseEvaluation.model_validate(
                json.loads(exercise.evaluation_json)
            )
        return None

    async def _reveal_solution_events(
        self,
        prepared: ExecuteTurn,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        ex = prepared.exercise
        if ex is None:
            yield self._failure_event(prepared, "TUTORING_FAILED", "未找到当前练习题。", state)
            return
        revealed = self._repository.reveal_exercise_solution(
            ex.id, conversation_id=prepared.conversation_id
        )
        ex_view = self._build_exercise_view(revealed, allow_solution=True)
        try:
            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="reveal_solution",
                exercise=ex_view,
                assistant_message="已为您揭晓练习题参考解答。",
                problem_id=ex.problem_id,
                exercise_id=ex.id,
            )
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return
        state.saved = True
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind="reveal_solution",
            exercise=ex_view,
            assistant_message="已为您揭晓练习题参考解答。",
            exercise_id=ex.id,
        )

    async def _exercise_answer_events(
        self,
        prepared: ExecuteTurn,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        ex = prepared.exercise
        if ex is None:
            yield self._failure_event(
                prepared, "TUTORING_FAILED", "未找到当前练习题，无法评价。", state
            )
            return

        plan = self._plan_exercise_attempt(ex, prepared.input)
        if plan.error:
            yield self._failure_event(
                prepared, "BUSINESS_REJECTED", plan.error, state
            )
            return

        # 先保存本次尝试（短事务），再在事务外调用评价模型；模型失败时作答不丢。
        try:
            saved, attempt = self._save_exercise_attempt(prepared, ex, plan)
        except ConversationConflictError as exc:
            yield self._failure_event(
                prepared, "BUSINESS_REJECTED", str(exc), state
            )
            return
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return

        saved_view = self._build_exercise_view(saved)
        yield TurnProgressEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            stage="evaluating_exercise",
            message="作答已保存，正在评价您的答案与推导过程",
        )

        if self._make_exercise_agent is None:
            failure_results = [ActionResultItem(
                action="evaluate_exercise", status="failed", message="作答已保存，但评价模型尚未配置，请先配置评价模型。",
                details={"error_code": "MODEL_NOT_CONFIGURED", "retryable": False},
            )]
            try:
                self._repository.complete_dynamic_turn(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    kind="exercise",
                    exercise=saved_view,
                    composite_results=failure_results,
                    assistant_message="作答已保存，但评价模型尚未配置，请先配置评价模型。",
                    problem_id=ex.problem_id,
                    exercise_id=ex.id,
                )
            except ConversationStorageError:
                yield _storage_error_event(prepared)
                return
            state.saved = True
            yield TurnResultEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="exercise",
                exercise=saved_view,
                composite_results=failure_results,
                assistant_message="作答已保存，但评价模型尚未配置，请先配置评价模型。",
                exercise_id=ex.id,
            )
            return

        try:
            selected_images = load_images(
                attempt.attachments,
                root=self._attachments_root,
                roles={
                    "statement",
                    "solution",
                    "supplement",
                    "student_answer",
                    "student_solution",
                },
            )
        except (AttachmentInvalidError, InvalidImageError) as exc:
            yield self._failure_event(
                prepared, "INVALID_INPUT", str(exc), state
            )
            return

        ex_agent = self._make_exercise_agent()
        try:
            eval_res = await ex_agent.evaluate_answer(
                statement=ex.statement_text,
                reference_solution=ex.reference_solution_text,
                student_answer=attempt.student_answer_text,
                student_solution_text=attempt.student_solution_text,
                images=selected_images,
                answered_after_revealed=attempt.answered_after_revealed,
                attempt_kind=attempt.kind,
            )
        except Exception as exc:
            logger.warning("评价作答失败，作答已保存：%s", exc)
            saved_view = self._build_exercise_view(saved)
            code = exc.code if isinstance(exc, QueryError) else "TUTORING_FAILED"
            can_retry = code in RECOVERABLE_CODES
            message = "作答已保存，但评价没有完成，请重试。" if can_retry else "作答已保存，评价服务拒绝了请求，请先检查模型配置。"
            failure_results = [ActionResultItem(
                action="evaluate_exercise", status="failed", message=message,
                details={"error_code": code, "retryable": can_retry},
            )]
            try:
                self._repository.complete_dynamic_turn(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    kind="exercise",
                    exercise=saved_view,
                    composite_results=failure_results,
                    assistant_message=message,
                    problem_id=ex.problem_id,
                    exercise_id=ex.id,
                )
            except ConversationStorageError:
                yield _storage_error_event(prepared)
                return
            state.saved = True
            yield TurnResultEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="exercise",
                exercise=saved_view,
                composite_results=failure_results,
                assistant_message=message,
                exercise_id=ex.id,
            )
            return

        effective = self._enforce_independence(eval_res, attempt)
        try:
            ex_view, eval_view = self._complete_exercise_evaluation(
                prepared, saved, ex, attempt, effective
            )
        except ConversationConflictError:
            yield self._failure_event(prepared, "BUSINESS_REJECTED", "这份作答已被更新，请刷新查看最新评价。", state)
            return
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return

        state.saved = True
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind="exercise",
            exercise=ex_view,
            evaluation=eval_view,
            assistant_message="已完成作答评价。",
            exercise_id=ex.id,
        )

    def _save_exercise_attempt(
        self, prepared: ExecuteTurn, ex: ExerciseRecord, plan: _AttemptPlan
    ) -> tuple[ExerciseRecord, ExerciseAttemptRecord]:
        """保存尝试或复用已有尝试；复用不写库、不覆盖原答案。"""

        if plan.reuse and plan.attempt is not None:
            return ex, plan.attempt

        mapped = [
            (
                attachment_id,
                "student_answer" if role == "statement" else "student_solution",
            )
            for attachment_id, role in plan.attachments
        ]
        with self._repository.action_write():
            saved = self._repository.create_exercise_attempt(
                ex.id,
                kind=plan.kind,
                student_answer_text=plan.answer,
                student_solution_text=plan.solution,
                attachments=mapped,
                answered_after_revealed=plan.answered_after_revealed,
                request_id=prepared.request_id,
            )
            attempt = saved.current_attempt
            if attempt is None:
                raise ConversationStorageError("作答尝试没有保存成功，请重试。")
            original = self._repository.find_turn(
                prepared.conversation_id, prepared.request_id
            )
            original_input = (
                original.recovery.get("input") if original else None
            ) or json.loads(self._canonical(prepared.input))
            self._repository.save_recovery(
                prepared.conversation_id,
                prepared.request_id,
                {"input": original_input, "exercise_id": ex.id},
            )
        return saved, attempt

    def _complete_exercise_evaluation(
        self,
        prepared: ExecuteTurn,
        saved: ExerciseRecord,
        ex: ExerciseRecord,
        attempt: ExerciseAttemptRecord,
        evaluation: AnswerEvaluation,
    ) -> tuple[ExerciseView, ExerciseEvaluation | None]:
        # 评价与轮次完成一起提交；失败则评价回滚，原作答仍可恢复。
        with self._repository.action_write():
            updated = self._repository.save_attempt_evaluation(
                attempt.id,
                evaluation_json=json.dumps(
                    evaluation.model_dump(mode="json"), ensure_ascii=False
                ),
            )
            view = self._build_exercise_view(updated)
            evaluation_view = self._build_evaluation_view(updated)
            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id, request_id=prepared.request_id, kind="exercise",
                exercise=view, evaluation=evaluation_view, assistant_message="已完成作答评价。",
                problem_id=ex.problem_id, exercise_id=ex.id,
            )
        return view, evaluation_view

    # ------------------------------------------------------------------
    # 保存失败/中断
    # ------------------------------------------------------------------

    def _failure_event(
        self,
        prepared: ExecuteTurn,
        code: str,
        message: str,
        state: _RunState,
    ) -> TurnErrorEvent:
        try:
            self._repository.fail_turn(
                prepared.conversation_id,
                prepared.request_id,
                code,
                message,
            )
        except ConversationStorageError:
            return _storage_error_event(prepared)
        state.saved = True
        return TurnErrorEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            code=code,  # type: ignore[arg-type]
            message=message,
        )

    def _interrupt_quietly(self, prepared: ExecuteTurn) -> None:
        """断流或保存失败时尽力标记 interrupted；失败留给启动恢复。"""

        try:
            self._repository.interrupt_turn(
                prepared.conversation_id,
                prepared.request_id,
            )
        except ConversationStorageError:
            logger.warning(
                "无法把中断的轮次写入数据库，将在下次启动时恢复。",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # 输入校验与规范化
    # ------------------------------------------------------------------

    def _normalize(self, data: TurnInput) -> TurnInput:
        text = data.text.strip()
        kind = data.kind
        # 兼容 F006 修复前的请求别名；保存和事件统一使用 exercise。
        if kind == "exercise_answer":
            kind = "exercise"
            data = replace(data, kind=kind)
        legacy_query = (
            kind is None and not data.dispatch and not data.attachments
        )
        if legacy_query:
            if not text:
                raise TurnInvalidError("查询内容不能为空。")
            if len(text) > 1000:
                raise TurnInvalidError("查询最长为 1000 个字符。")
        elif kind == "query":
            if not text:
                raise TurnInvalidError("查询内容不能为空。")
            if len(text) > 1000:
                raise TurnInvalidError("查询最长为 1000 个字符。")
        else:
            if len(text) > 2000:
                raise TurnInvalidError("本轮文字最长为 2000 个字符。")
            if kind == "record" and not text:
                text = "记录这题"
            elif kind == "feedback" and not text:
                text = "我懂了"
            elif kind == "followup" and not text:
                raise TurnInvalidError("追问内容不能为空。")

        if data.attempt_kind is not None:
            if kind not in ("exercise", "exercise_answer"):
                raise TurnInvalidError("只有练习作答轮次可以指定补充或订正。")
            if data.retry_of is not None:
                raise TurnInvalidError("重试评价不能同时指定补充或订正。")
            if data.attempt_kind not in ("supplement", "correction"):
                raise TurnInvalidError("未知的作答类型。")

        if kind == "query":
            if data.attachments or data.student_answer_text or data.solution_text:
                raise TurnInvalidError("查询轮次不能携带附件或学生解法。")
            if data.source.type != "none":
                raise TurnInvalidError("查询轮次不能携带题目来源。")
            if data.problem_id is not None or data.start_new:
                raise TurnInvalidError("查询轮次不能携带题目引用或新题标记。")
        elif kind in ("followup", "record", "feedback"):
            if data.attachments:
                raise TurnInvalidError("该轮次不能携带图片。")
            if data.student_answer_text or data.solution_text:
                raise TurnInvalidError("该轮次不能携带学生解法。")
            if data.source.type != "none":
                raise TurnInvalidError("该轮次不能携带题目来源。")
            if data.start_new:
                raise TurnInvalidError("只有新题轮次可以使用 start_new。")
        elif kind in ("problem", "supplement", "correction"):
            if data.start_new and kind != "problem":
                raise TurnInvalidError("只有新题轮次可以使用 start_new。")
        elif kind is None:
            if data.start_new:
                raise TurnInvalidError("只有新题轮次可以使用 start_new。")
            if data.problem_id is not None:
                raise TurnInvalidError("未指定轮次类型时不能携带题目引用。")
            if data.attachments:
                if data.source.type != "none":
                    raise TurnInvalidError("带图片提交新题时，请明确轮次类型。")
            elif (
                data.student_answer_text
                or data.solution_text
                or data.source.type != "none"
            ):
                raise TurnInvalidError(
                    "提交学生解法或来源时，请明确选择题目/纠正/补充轮次。"
                )

        return replace(data, text=text)

    def _canonical(self, data: TurnInput) -> str:
        if data.retry_of:
            return canonical_json({"retry_of": data.retry_of})
        if data.kind == "query" or (
            data.kind is None and not data.dispatch and not data.attachments
        ):
            return query_input(data.text)
        attachments = [
            {"id": attachment_id, "role": role}
            for attachment_id, role in sorted(data.attachments)
        ]
        payload = {
            "kind": data.kind,
            "text": data.text,
            "attachments": attachments,
            "student_answer": data.student_answer_text,
            "solution_text": data.solution_text,
            "source": data.source.model_dump(mode="json"),
            "problem_id": data.problem_id,
            "exercise_id": data.exercise_id,
            "start_new": data.start_new,
            "dispatch": data.dispatch,
        }
        if data.attempt_kind:
            payload["attempt_kind"] = data.attempt_kind
        return canonical_json(payload)

    @staticmethod
    def _same_input(existing: Turn, canonical: str) -> bool:
        if existing.input_json is not None:
            return existing.input_json == canonical
        # 兼容极旧的、迁移时未回填 input_json 的行。
        return existing.kind == "query" and query_input(existing.query) == canonical

    def _require_tutor(self) -> None:
        if self._make_tutor is None:
            raise TutoringUnavailableError(
                self._tutoring_error or "辅导模型尚未配置。"
            )

    def _require_active(
        self, active: ProblemRecord | None, problem_id: str | None
    ) -> ProblemRecord:
        if active is None:
            raise ProblemNotFoundError("当前没有正在辅导的题目。")
        if problem_id is not None and problem_id != active.id:
            raise ProblemNotFoundError("指定的题目不是当前活动题目。")
        return active

    def _validate_attempt_kind(
        self, exercise: ExerciseRecord, attempt_kind: str
    ) -> None:
        latest = exercise.current_attempt
        if attempt_kind == "supplement":
            if latest is None or not self._attempt_needs_supplement(latest):
                raise ConversationConflictError(
                    "当前作答不需要补充；可直接提交订正答案，或生成新的类似题。"
                )
            return
        if attempt_kind == "correction":
            if latest is None:
                raise ConversationConflictError("请先提交首次作答，再进行订正。")
            if self._attempt_needs_supplement(latest):
                raise ConversationConflictError(
                    "这次作答还需要补充过程，请先补充并重新评价。"
                )
            return
        raise TurnInvalidError("未知的作答类型。")

    def _resolve_exercise_target(
        self, conversation_id: str, exercise_id: str | None
    ) -> ExerciseRecord:
        """显式指定的练习必须存在且属于本会话；只有未指定时才用活动练习。"""

        if exercise_id is not None:
            exercise = self._repository.get_exercise(exercise_id)
            if exercise is None:
                raise ProblemNotFoundError("指定的练习题不存在。")
            if exercise.conversation_id != conversation_id:
                raise ProblemNotFoundError("练习题不属于当前会话。")
            return exercise
        exercise = self._repository.get_active_exercise(conversation_id)
        if exercise is None:
            raise ProblemNotFoundError("当前没有可以操作的练习题。")
        return exercise

    def _resolve_target_problem(
        self,
        conversation_id: str,
        active: ProblemRecord | None,
        problem_id: str | None,
        *,
        require_explained: bool,
    ) -> ProblemRecord:
        """记录/反馈/纠正可以指定历史题目；不再默认落到当前活动题。"""

        if problem_id is None:
            problem = active
            if problem is None:
                raise ProblemNotFoundError("当前没有正在辅导的题目。")
        else:
            problem = self._repository.get_problem(problem_id)
            if problem is None or problem.conversation_id != conversation_id:
                raise ProblemNotFoundError("题目不存在。")
        if require_explained and (
            problem.status != "explained" or problem.explanation is None
        ):
            raise ProblemNotReadyError("题目尚未讲解，暂时不能执行该操作。")
        return problem

    def _validate_attachments(
        self,
        conversation_id: str,
        data: TurnInput,
        mode: str,
    ) -> None:
        attachments = list(data.attachments)
        if not attachments:
            if mode in ("problem", "correction") and not data.text.strip():
                raise TurnInvalidError("请提供题干文字或至少一张题干图片。")
            if mode == "supplement" and not data.text.strip():
                raise TurnInvalidError("请提供补充文字或图片。")
            return

        # 数量、重复、角色、归属、单张大小和总量统一由仓库校验。
        self._repository.validate_attachment_selection(
            conversation_id, attachments
        )
        roles = [role for _, role in attachments]

        if mode in ("problem", "correction") and not data.text.strip():
            if "statement" not in roles:
                raise TurnInvalidError("请提供题干文字或至少一张题干图片。")
        if mode == "supplement" and not data.text.strip() and not attachments:
            raise TurnInvalidError("请提供补充文字或图片。")

    def _resolve_source(self, data: TurnInput) -> Source:
        """把已登记 paper_id 解析成带标题的来源快照；未登记返回 422。"""

        source = data.source
        if source.type != "registered_paper":
            return source
        if self._papers is None:
            raise SourceInvalidError("试卷目录不可用，暂时不能校验来源。")
        paper = self._papers.get_paper(source.paper_id or "")
        if paper is None:
            raise SourceInvalidError("题目来源未登记，请检查试卷选择。")
        return Source(
            type="registered_paper",
            paper_id=paper.id,
            paper_title=paper.title,
        )

    def _validate_source(
        self,
        active: ProblemRecord | None,
        data: TurnInput,
        mode: str,
    ) -> None:
        if mode not in ("problem", "supplement", "correction"):
            if data.source.type != "none":
                raise TurnInvalidError("该轮次不能携带题目来源。")
            return
        source = data.source
        if source.type != "registered_paper":
            return
        if self._papers is None:
            raise SourceInvalidError("试卷目录不可用，暂时不能校验来源。")
        paper = self._papers.get_paper(source.paper_id or "")
        if paper is None:
            raise SourceInvalidError("题目来源未登记，请检查试卷选择。")

    def _selected_attachments(
        self, prepared: ExecuteTurn
    ) -> list[tuple[str, str]]:
        provided = list(prepared.input.attachments)
        if provided:
            return provided
        if (
            prepared.operation in ("supplement", "correction")
            and prepared.problem is not None
        ):
            return [
                (item.id, item.role or "statement")
                for item in prepared.problem.attachments
            ]
        return []

    def _attachments_by_ids(
        self,
        conversation_id: str,
        entries: list[tuple[str, str]],
    ) -> list[AttachmentRecord]:
        return self._repository.validate_attachment_selection(
            conversation_id, entries
        )

    def _tutoring_input_text(self, prepared: ExecuteTurn) -> str:
        """补充/纠正时把原题干一起交给理解模型，避免只看半句话。"""

        text = prepared.input.text.strip()
        if (
            prepared.problem is None
            or prepared.operation not in ("supplement", "correction")
        ):
            return text

        original = prepared.problem.statement_text.strip()
        action = "补充" if prepared.operation == "supplement" else "纠正"
        if text:
            return f"原题干：\n{original}\n\n学生{action}：\n{text}"
        return (
            f"原题干：\n{original}\n\n"
            f"学生只{action}了图片，请结合原题干和新图片整理题目。"
        )

    def _effective_problem_fields(
        self, prepared: ExecuteTurn
    ) -> tuple[Source, str | None, str | None]:
        source = prepared.input.source
        student_answer = prepared.input.student_answer_text
        solution_text = prepared.input.solution_text
        if prepared.problem is not None and prepared.operation in (
            "supplement",
            "correction",
        ):
            if source.type == "none":
                source = prepared.problem.source
            if student_answer is None:
                student_answer = prepared.problem.student_answer_text
            if solution_text is None:
                solution_text = prepared.problem.solution_text
        return source, student_answer, solution_text

    def _record_id_for(self, problem: ProblemRecord) -> str | None:
        record = self._repository.get_record_by_root(
            problem.conversation_id, problem.root_problem_id
        )
        return record.id if record is not None else None


def _replay_event(prepared: ReplayTurn) -> TurnEvent:
    turn = prepared.turn
    if turn.status == "completed" and (
        turn.kind in ("dynamic", "exercise", "exercise_answer", "reveal_solution")
        or turn.exercise is not None
        or turn.evaluation is not None
        or turn.assistant_message is not None
    ):
        return TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=turn.request_id,
            kind=turn.kind,
            result=turn.result,
            tutoring=turn.tutoring,
            exercise=turn.exercise,
            evaluation=turn.evaluation,
            composite_results=turn.composite_results,
            assistant_message=turn.assistant_message,
            suggestions=turn.suggestions,
            exercise_id=turn.exercise_id,
            business_status=turn.business_status,
            retryable=turn.retryable,
            retry_hint=turn.retry_hint,
        )
    if turn.status == "completed" and turn.kind == "query" and turn.result is not None:
        return TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=turn.request_id,
            kind="query",
            result=turn.result,
        )
    if turn.status == "completed" and turn.tutoring is not None:
        return TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=turn.request_id,
            kind=turn.kind,
            tutoring=turn.tutoring,
        )
    if turn.status == "failed" and turn.error is not None:
        return TurnErrorEvent(
            conversation_id=prepared.conversation_id,
            request_id=turn.request_id,
            code=turn.error.code,
            message=turn.error.message,
        )
    # prepare_turn 已经排除 running/interrupted；这里只做防御。
    raise ConversationConflictError("该请求没有可回放的终止结果。")


def _storage_error_event(prepared: ExecuteTurn) -> TurnErrorEvent:
    return TurnErrorEvent(
        conversation_id=prepared.conversation_id,
        request_id=prepared.request_id,
        code=STORAGE_ERROR_CODE,
        message=_STORAGE_FALLBACK_MESSAGE,
    )
