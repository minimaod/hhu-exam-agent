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
import os
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
    MAX_SCHEDULER_ROUNDS,
    ActionOutcome,
    ActionPlan,
    DynamicReviewGraph,
    ExecutionResult,
    ScheduleDecision,
    SchedulerAgent,
)
from app.conversations.reply import (
    ReplyMaterial,
    ReplyTruncated,
    build_material,
    material_from_payload,
    material_to_payload,
)
from app.conversations.tools import TOOLS_BY_NAME
from app.conversations.tools import tool_label as _tool_label
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
    reply_state as _reply_state,
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
    TurnMessageDeltaEvent,
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
from app.question_bank.models import SearchRequest
from app.papers.courses import normalize_course
from app.questions.cards import answer_url as answer_url_for
from app.questions.cards import attach_answer, compose_statement
from app.questions.errors import (
    QuestionBankUnavailableError,
    QuestionError,
    QuestionNotFoundError,
    QuestionNotRevealedError,
    QuestionSelectionInvalidError,
    QuestionSourceChangedError,
    QuestionSourcePrunedError,
)
from app.questions.intent import (
    SearchIntent,
    candidate_number,
    detect_question_type,
    extract_intent,
    looks_like_new_problem,
    mentions_question_target,
    is_question_lookup,
    question_answer_requested,
    wants_next_card,
)
from app.questions.models import (
    ExerciseSourceView,
    QuestionCard,
    QuestionSearchResult,
    QuestionSelection,
)
from app.questions.runtime import QuestionMaterial, QuestionRuntime, bank_root_from_env
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
_DYNAMIC_TIMEOUT_SECONDS = 240.0
_TUTORING_MESSAGE = "这次辅导没有完成，请重试。"
_TIMEOUT_MESSAGE = "辅导超时，请重试。"
_DISPATCH_MESSAGE = "判断会话意图超时，请重试。"
_RETRY_TEXT_MARKERS = ("重试", "重新评价", "再评价", "再试")
_QUESTION_KINDS = ("select_question", "explain_question", "view_question_answer")
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

# F008 程序侧红线：出题与正式保存只在学生明确要求时执行，不依赖提示词自觉。
_RED_LINE_ACTIONS = frozenset({"generate_exercise", "record_mistake"})
_SELF_REPORT_RE = re.compile(
    r"(我懂了|我听懂了|我明白了|懂了|听懂了|明白了|理解了|清楚了|会了|学会了|谢谢)"
)
_PRACTICE_REQUEST_RE = re.compile(
    r"(再来|来一道|来道|出一道|出题|练|类似题|变式|做题|刷题)"
)
_SAVE_REQUEST_RE = re.compile(r"(保存|存起来|存一下|记录|错题|收藏|加入)")
_AFFIRMATIVE_ONLY = frozenset(
    {
        "好",
        "好的",
        "好呀",
        "好哒",
        "可以",
        "行",
        "嗯",
        "嗯嗯",
        "ok",
        "okay",
        "好吧",
        "都行",
        "随便",
        "就这么办",
    }
)


def _is_bare_affirmative(text: str) -> bool:
    """只有整句就是一个肯定词时才算“无法判断指向”的歧义回答。"""

    cleaned = re.sub(r"[\s，。！？、,.!?~～]+", "", text or "")
    return cleaned in _AFFIRMATIVE_ONLY


def _is_dynamic_recovery(recovery: dict[str, Any] | None) -> bool:
    """动态轮次的恢复记录；F008 起版本号为 2，旧记录是 1，都必须能恢复。"""

    if not isinstance(recovery, dict):
        return False
    version = recovery.get("version")
    return isinstance(version, int) and version >= 1


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
    question_id: str | None = None
    question_source_turn: str | None = None
    question_action: str | None = None


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

    raw_path = os.environ.get("HHU_STUDY_DB")
    db_path = (
        Path(raw_path).expanduser()
        if raw_path
        else conversation_repository.DEFAULT_DB_PATH
    )
    repository = ConversationRepository(db_path)
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
    "question_exercise",
    "question_record",
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


_PAGE_WORDS = re.compile(
    r"继续|接着|下一批|下一页|下一部分|剩下的|其余的|后面那些|还没(看|显示|列)|"
    r"换一道|换一题|换一个|换个题|换道题|再来一道|再换一道|不要这道|别来这道|下一道|下一个题|"
    r"选择题|填空题|编程题|判断题|计算题|简答题|问答题|大题|小题|运算题|"
    r"给我|再看|再列|再显示|看看|一下|这些|那些|这|那|份|张|道|题|卷|个|的|吧|呢|啊|呀|了|我|你|它|"
    r"再|还|都|全|列|显示|看|来|出|给|要|有|几"
)


def _page_topic_words(text: str) -> str:
    """去掉翻页/客套词后剩下的话题约束；空表示这一轮只是翻页。"""

    return _PAGE_WORDS.sub("", text or "").strip()


_DISCLOSURE_ACTIONS = frozenset(
    {
        "explain_problem",
        "tutoring",
        "view_question_answer",
        "reveal_solution",
        "followup_problem",
        "followup",
    }
)


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
    question_selection: QuestionSelection | None = None
    question_card: QuestionCard | None = None
    question_source_turn: str | None = None
    question_result_status: str | None = None
    question_result_message: str | None = None
    question_results: list[QuestionCard] = field(default_factory=list)
    # 最近一次成功查题/浏览留下的试卷、题型、总数与位置；用以下一轮“刚刚那张
    # 卷”“继续”时不再要求学生重复信息。读取顺序：本轮 → 上一轮持久记录。
    question_context: dict[str, Any] = field(default_factory=dict)
    # 本轮回答范围：hint=只给提示，topic=只问考点。都由学生的话决定，
    # 用来阻止模型把“提示/考点”当成完整讲解，并约束正文范围。
    answer_scope: str | None = None
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
        make_reply: Callable[[], Any] | None = None,
        papers: PaperService | None = None,
        records: MistakeRecordService | None = None,
        attachments_root: Path = ATTACHMENTS_ROOT,
        question_runtime: QuestionRuntime | None = None,
    ) -> None:
        self._repository = repository
        self._make_query = make_query
        self._query_error = query_error
        self._make_tutor = make_tutor
        self._tutoring_error = tutoring_error
        self._make_router = make_router
        self._make_exercise_agent = make_exercise_agent
        self._make_scheduler = make_scheduler
        self._make_reply = make_reply
        self._papers = papers
        self._records = records
        self._attachments_root = attachments_root
        self._owns_question_runtime = question_runtime is None
        self.questions = question_runtime or QuestionRuntime(
            None, error="题目库未启用", bank_root=bank_root_from_env()
        )

    def close(self) -> None:
        """只关闭本服务显式持有的题库运行时；共享运行时由应用生命周期关闭。"""

        if getattr(self, "_owns_question_runtime", False):
            self.questions.close()

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
            source_question=self._repository.get_exercise_question_source(ex.id),
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

        active_question = None
        active_selection = self._repository.get_active_question(conversation_id)
        if active_selection is not None:
            active_question = self._question_card_for_selection(
                active_selection, include_answer=True
            )

        turns = [
            self._enrich_question_turn(conversation_id, turn)
            for turn in self._repository.list_turns(conversation_id)
        ]

        return ConversationDetail(
            conversation=conversation,
            turns=turns,
            active_problem=build_problem_view(active) if active is not None else None,
            active_exercise=ex_view,
            active_question=active_question,
            context=ctx_view,
            record=record_ref,
        )

    def _question_card_for_selection(
        self, selection: QuestionSelection, *, include_answer: bool = False
    ) -> QuestionCard:
        card = self.questions.card_for_selection(selection)
        if include_answer and selection.answer_revealed_at:
            try:
                card, _ = self.questions.answer_view(selection)
            except QuestionError:
                pass
        return card

    def _require_question_selection(
        self, conversation_id: str, question_id: str, build_id: str
    ) -> QuestionSelection:
        selection = self._repository.get_question_selection(
            conversation_id, question_id, build_id=build_id
        )
        if selection is None:
            if self._repository.get_question_selection(conversation_id, question_id) is not None:
                raise QuestionSourceChangedError("题目来源版本不一致，请重新查题。")
            raise QuestionNotFoundError("这道题不属于当前会话，或尚未被选中。")
        if build_id and selection.build_id != build_id:
            raise QuestionSourceChangedError("题目来源版本不一致，请重新查题。")
        return selection

    def question_asset(
        self, conversation_id: str, question_id: str, asset_id: str, build_id: str
    ):
        try:
            selection = self._require_question_selection(
                conversation_id, question_id, build_id
            )
        except (QuestionNotFoundError, QuestionSourceChangedError):
            # 候选卡片在选中前就要显示题面图；只允许本会话查题结果里出现过的
            # 卡片，答案正文仍必须走选中 + 首次查看门禁。
            card = self._find_candidate_card(
                conversation_id, question_id, build_id
            )
            if card is None:
                raise
            return self.questions.asset_for_card(card, asset_id)
        return self.questions.asset(selection, asset_id)

    def _find_candidate_card(
        self, conversation_id: str, question_id: str, build_id: str
    ) -> QuestionCard | None:
        for turn in self._repository.list_turns(conversation_id):
            for card in turn.question_results:
                if (
                    card.question_id == question_id
                    and (not build_id or card.build_id == build_id)
                ):
                    return card.model_copy(update={"source_request_id": turn.request_id})
            if (
                turn.question is not None
                and turn.question.question_id == question_id
                and (not build_id or turn.question.build_id == build_id)
            ):
                return turn.question
        return None

    def question_source(
        self, conversation_id: str, question_id: str, build_id: str, kind: str, page: int
    ):
        """用户主动打开来源：验证原卷后才记查看事实，不改变当前选题。"""
        card = self._find_candidate_card(conversation_id, question_id, build_id)
        if card is None:
            raise QuestionNotFoundError("该来源不属于当前会话的题目。")
        payload = self.questions.source_for_card(card, kind, page)
        with self._repository.action_write():
            selection = self._repository.create_question_selection(
                conversation_id=conversation_id,
                request_id=f"source:{uuid.uuid4()}",
                source_request_id=card.source_request_id or "",
                card=card, build_id=card.build_id,
                source_version=payload.source_version or "",
                activate=False,
            )
            self._repository.record_question_source_view(
                selection.id, kind=kind, page=page,
                may_show_answer=card.source.may_show_answer,
            )
            if card.source.may_show_answer:
                self._repository.reveal_question_answer(
                    conversation_id, question_id, source="view", selection_id=selection.id
                )
        return payload

    def question_answer(
        self, conversation_id: str, question_id: str, build_id: str
    ) -> QuestionCard:
        selection = self._require_question_selection(
            conversation_id, question_id, build_id
        )
        if not selection.answer_revealed_at:
            raise QuestionNotRevealedError("还没有查看这道题的答案。")
        card, evidence = self.questions.answer_view(selection)
        if evidence.status == "source_changed":
            raise QuestionSourceChangedError(
                evidence.message or "题目内容已变化，请重新查题。"
            )
        if evidence.status == "source_pruned":
            raise QuestionSourcePrunedError(
                evidence.message or "原来源版本已不在本机。"
            )
        return card

    def _enrich_question_turn(self, conversation_id: str, turn: Turn) -> Turn:
        """详情页只从快照重建卡片；仅为已开放对象补答案正文。"""

        updates: dict[str, Any] = {}
        if turn.question_results:
            results = []
            for card in turn.question_results:
                selection = self._repository.find_question_selection_for_card(
                    conversation_id, card.question_id, card.build_id, card.content_sha256
                )
                results.append(QuestionCard.model_validate({**card.model_dump(),
                    "source_url": answer_url_for(conversation_id, card.question_id, card.build_id).replace("/answer?", "/source?"),
                    "source_views": (selection.snapshot.get("source_views", []) if selection else []),
                    "answer_url": answer_url_for(conversation_id, card.question_id, card.build_id) if card.answer.available else None,
                }))
            updates["question_results"] = results
        if turn.question is not None:
            card = turn.question
            selection = None
            if card.selection_id:
                selection = self._repository.get_question_selection_by_id(
                    conversation_id, card.selection_id
                )
            if selection is None:
                # 旧轮次没有 selection_id 时，按当时的 build + 内容哈希回找；
                # 不能只按题号取最新选择，否则历史题干会被新版本替换。
                selection = self._repository.find_question_selection_for_card(
                    conversation_id,
                    card.question_id,
                    card.build_id,
                    card.content_sha256,
                )
            if selection is not None:
                card = self._question_card_for_selection(
                    selection, include_answer=True
                )
                # 以历史卡片自己的快照为准，避免来源更新时把旧轮次换成新题面。
                if (
                    card.content_sha256 != turn.question.content_sha256
                    or card.build_id != turn.question.build_id
                ):
                    card = turn.question
            card = card.model_copy(
                update={
                    "source_url": answer_url_for(conversation_id, card.question_id, card.build_id).replace("/answer?", "/source?"),
                    "answer_url": (
                        answer_url_for(
                            conversation_id, card.question_id, card.build_id
                        )
                        if card.answer.available
                        else None
                    )
                }
            )
            updates["question"] = card
        return turn.model_copy(update=updates) if updates else turn

    # ------------------------------------------------------------------
    # 只重做正文
    # ------------------------------------------------------------------

    async def regenerate_reply(
        self, conversation_id: str, request_id: str
    ) -> dict[str, Any]:
        """重新生成某一轮的正文，不重跑任何工具操作。

        业务结果（保存的错题、生成的练习、写入的作答）已经在上一轮完成，
        这里只复用当时的正文材料重新写一遍，因此不会产生重复副作用。
        """

        turn = self._repository.find_turn(conversation_id, request_id)
        if turn is None:
            raise ConversationNotFoundError("该轮次不存在。")
        recovery = turn.recovery or {}
        payload = recovery.get("reply_input")
        if not payload:
            raise ConversationConflictError("这一轮没有可复用的回复材料，请重新提出需求。")
        if self._make_reply is None:
            raise ConversationConflictError("回复模型尚未配置。")

        material = material_from_payload(payload)
        text, meta = await self._stream_reply(self._make_reply(), material)
        if not text:
            raise ConversationConflictError("这次仍然没能生成回复，请稍后再试。")

        recovery["reply"] = meta
        self._repository.update_turn_reply(
            conversation_id,
            request_id,
            assistant_message=text,
            reply_meta=meta,
        )
        reply_status, reply_retryable = _reply_state(recovery)
        return {
            "request_id": request_id,
            "assistant_message": text,
            "reply_status": reply_status,
            "reply_retryable": reply_retryable,
            "reply": meta,
        }

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
            and not normalized.question_action
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
            question_id=payload.get("question_id"),
            question_source_turn=payload.get("question_source_turn"),
            question_action=payload.get("question_action"),
        )
        if _is_dynamic_recovery(origin.recovery):
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

    def _prepare_question_turn(
        self,
        conversation_id: str,
        data: TurnInput,
        canonical: str,
        active: ProblemRecord | None,
    ) -> ExecuteTurn:
        kind = data.kind
        assert kind in _QUESTION_KINDS
        if not data.question_id or not data.question_source_turn:
            raise TurnInvalidError("缺少题目或查题轮次标识。")
        if data.attachments or data.solution_text or (
            data.student_answer_text and kind != "explain_question"
        ):
            raise TurnInvalidError("仅真题讲解可以携带所选答案；不支持附件或解题过程。")
        if data.source.type != "none":
            raise TurnInvalidError("真题轮次不能另外指定题目来源。")
        if data.problem_id is not None or data.start_new:
            raise TurnInvalidError("真题轮次不能携带题目引用或新题标记。")
        if kind == "explain_question":
            self._require_tutor()
        if not self.questions.ready:
            raise QuestionBankUnavailableError("题目库暂时不可用，请稍后重试。")
        # 先确认候选题仍属于该查题轮次且内容哈希一致，再写入 running 轮次。
        card = self._resolve_question_card(
            conversation_id, data.question_source_turn, data.question_id
        )
        if data.student_answer_text and (
            card.question_type != "choice"
            or data.student_answer_text not in {option.label for option in card.options}
        ):
            raise TurnInvalidError("请选择这道选择题实际提供的选项。")
        turn = self._repository.start_turn(
            conversation_id,
            data.request_id,
            data.text,
            kind=kind,
            input_json=canonical,
        )
        return ExecuteTurn(
            conversation_id=conversation_id,
            request_id=data.request_id,
            turn=turn,
            input=data,
            operation=kind,
            problem=active,
        )

    def _resolve_question_card(
        self, conversation_id: str, source_request_id: str, question_id: str
    ) -> QuestionCard:
        turn = self._repository.find_turn(conversation_id, source_request_id)
        if turn is None:
            raise QuestionSelectionInvalidError("查题轮次不属于当前会话，请重新查题。")
        card = next(
            (item for item in turn.question_results if item.question_id == question_id),
            None,
        )
        if card is None:
            raise QuestionSelectionInvalidError("该题目不在这次查题结果中，请重新查题。")
        if not card.build_id or not card.content_sha256:
            raise QuestionSourceChangedError("题目来源信息不完整，请重新查题。")
        if self.questions.build_id and card.build_id != self.questions.build_id:
            raise QuestionSourceChangedError("题目库已更新，请重新查题后再选。")
        if not self.questions.is_approved(question_id):
            raise QuestionSelectionInvalidError("这道题尚未通过审核，暂不能使用。")
        if not self.questions.verify_card(card):
            raise QuestionSourceChangedError("题目内容已变化，请重新查题。")
        return card

    def _select_question_card(
        self,
        conversation_id: str,
        request_id: str,
        source_request_id: str,
        card: QuestionCard,
    ) -> QuestionSelection:
        return self._repository.create_question_selection(
            conversation_id=conversation_id,
            request_id=request_id,
            source_request_id=source_request_id,
            card=card,
            build_id=card.build_id,
            source_version=self.questions.source_version or "",
        )

    def _prepare_dispatched(
        self,
        conversation_id: str,
        data: TurnInput,
        canonical: str,
        active: ProblemRecord | None,
    ) -> ExecuteTurn:
        if data.question_action is not None:
            if not self.questions.ready:
                raise QuestionBankUnavailableError("题目库暂时不可用，请稍后重试。")
            if not data.question_id or not data.question_source_turn:
                raise TurnInvalidError("真题动作必须指定题目和查题轮次。")
            card = self._resolve_question_card(
                conversation_id, data.question_source_turn, data.question_id
            )
            selection = self._select_question_card(
                conversation_id, data.request_id, data.question_source_turn, card
            )
            problem = (
                self._repository.get_problem(selection.problem_id)
                if selection.problem_id
                else None
            )
            if problem is not None and problem.conversation_id != conversation_id:
                problem = None
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text or ("生成变式" if data.question_action == "exercise" else "保存错题"),
                kind="dynamic",
                input_json=canonical,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation=(
                    "question_exercise"
                    if data.question_action == "exercise"
                    else "question_record"
                ),
                problem=problem,
            )
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

        if kind in _QUESTION_KINDS:
            return self._prepare_question_turn(
                conversation_id, data, canonical, active
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

            if prepared.operation in _QUESTION_KINDS:
                async for event in self._question_events(prepared, state):
                    yield event
                return

            if prepared.operation in ("question_exercise", "question_record"):
                async for event in self._question_action_events(prepared, state):
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

    async def _question_action_events(
        self, prepared: ExecuteTurn, state: _RunState
    ) -> AsyncIterator[TurnEvent]:
        """卡片按钮的确定性动作：目标只由 question_id 决定，不读当前题。"""

        data = prepared.input
        action = data.question_action or ""
        try:
            yield TurnProgressEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                stage="searching_questions",
                message="正在核对题目来源与内容",
            )
            card = self._resolve_question_card(
                prepared.conversation_id,
                data.question_source_turn or "",
                data.question_id or "",
            )
            selection = self._select_question_card(
                prepared.conversation_id,
                prepared.request_id,
                data.question_source_turn or "",
                card,
            )
        except QuestionError as exc:
            yield self._failure_event(prepared, exc.code, exc.message, state)
            return

        problem = (
            self._repository.get_problem(selection.problem_id)
            if selection.problem_id
            else None
        )
        if problem is not None and problem.conversation_id != prepared.conversation_id:
            problem = None
        ctx = self._load_dynamic_context(prepared.conversation_id)
        ctx.active_problem = problem
        ctx.problem_id = problem.id if problem is not None else None
        ctx.question_selection = selection
        ctx.question_card = card
        ctx.question_source_turn = data.question_source_turn
        ctx.question_results = [card]
        if action == "exercise":
            try:
                outcome = await self._dynamic_generate_exercise(
                    prepared,
                    ctx,
                    lambda stage, message: None,
                )
            except QueryError as exc:
                yield self._failure_event(prepared, exc.code, exc.message, state)
                return
            except Exception:
                logger.exception("真题变式生成失败")
                yield self._failure_event(
                    prepared, "TUTORING_FAILED", _TUTORING_MESSAGE, state
                )
                return
        else:
            try:
                outcome = self._dynamic_record_mistake(
                    prepared,
                    ctx,
                    lambda stage, message: None,
                )
            except Exception:
                logger.exception("真题错题保存失败")
                yield self._failure_event(
                    prepared, "TUTORING_FAILED", _TUTORING_MESSAGE, state
                )
                return
        composite = [
            ActionResultItem(
                action="generate_exercise" if action == "exercise" else "record_mistake",
                status=outcome.status,  # type: ignore[arg-type]
                message=outcome.message,
                details=dict(outcome.details or {}),
            )
        ]
        try:
            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="dynamic",
                tutoring=ctx.tutoring,
                exercise=ctx.exercise_view,
                composite_results=composite,
                assistant_message=outcome.message,
                suggestions=[],
                question=ctx.question_card,
                problem_id=ctx.problem_id,
                exercise_id=ctx.exercise_id,
            )
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return
        state.saved = True
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind="dynamic",
            tutoring=ctx.tutoring,
            exercise=ctx.exercise_view,
            composite_results=composite,
            assistant_message=outcome.message,
            question=ctx.question_card,
            exercise_id=ctx.exercise_id,
        )

    async def _question_events(
        self, prepared: ExecuteTurn, state: _RunState
    ) -> AsyncIterator[TurnEvent]:
        data = prepared.input
        kind = data.kind
        assert kind in _QUESTION_KINDS
        try:
            yield TurnProgressEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                stage="searching_questions",
                message="正在核对题目来源与内容",
            )
            card = self._resolve_question_card(
                prepared.conversation_id,
                data.question_source_turn or "",
                data.question_id or "",
            )
            selection = self._select_question_card(
                prepared.conversation_id,
                prepared.request_id,
                data.question_source_turn or "",
                card,
            )
        except QuestionError as exc:
            yield self._failure_event(prepared, exc.code, exc.message, state)
            return

        if kind == "select_question":
            question = self.questions.card_for_selection(selection, selected=True)
            message = (
                f"已选中《{question.source.paper_title or question.source.paper_id}》"
                f"第 {question.number_label} 题。"
            )
            suggestions = _question_suggestions(question)
            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind=kind,
                question=question,
                question_result_status="ok",
                assistant_message=message,
                suggestions=suggestions,
            )
            state.saved = True
            yield TurnResultEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind=kind,
                question=question,
                question_result_status="ok",
                assistant_message=message,
                suggestions=suggestions,
            )
            return

        if kind == "view_question_answer":
            try:
                yield TurnProgressEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    stage="viewing_question",
                    message="正在读取原卷答案",
                )
                selection = (
                    self._repository.reveal_question_answer(
                        prepared.conversation_id,
                        selection.question_id,
                        source="view",
                        selection_id=selection.id,
                    )
                    or selection
                )
                question, evidence = self.questions.answer_view(selection)
            except QuestionError as exc:
                yield self._failure_event(prepared, exc.code, exc.message, state)
                return
            status = "ok"
            if evidence.status == "missing_answer":
                status = "missing_answer"
            elif evidence.status == "source_changed":
                status = "source_changed"
            elif evidence.status == "source_pruned":
                status = "source_pruned"
            message = evidence.message or "已显示原卷参考答案。"
            suggestions = _question_suggestions(question)
            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind=kind,
                question=question,
                question_result_status=status,
                question_result_message=message,
                assistant_message=message,
                suggestions=suggestions,
            )
            state.saved = True
            yield TurnResultEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind=kind,
                question=question,
                question_result_status=status,  # type: ignore[arg-type]
                question_result_message=message,
                assistant_message=message,
                suggestions=suggestions,
            )
            return

        # explain_question
        try:
            payload, problem, question = await self._explain_selection(
                prepared, selection
            )
        except QuestionError as exc:
            yield self._failure_event(prepared, exc.code, exc.message, state)
            return
        except Exception:
            logger.exception("真题讲解失败")
            yield self._failure_event(
                prepared, "TUTORING_FAILED", _TUTORING_MESSAGE, state
            )
            return
        # 真题卡片的“讲解/提交并讲解”与普通动态轮次共用同一个正文阶段：
        # 讲解内容已经在卡片里展示，因此允许正文完整讲一遍。
        fallback_message = "已结合原卷材料完成题目讲解。"
        composite = [
            ActionResultItem(
                action="explain_question",
                status="success",
                message=f"已完成第 {question.number_label} 题的讲解。",
                details={
                    "number_label": question.number_label,
                    "question_type": question.question_type,
                },
            )
        ]
        suggestions = _question_suggestions(question)
        ctx = _DynamicContext(
            conversation_id=prepared.conversation_id,
            tutoring=payload,
            problem_id=problem.id,
            question_card=question,
            question_results=[question],
        )
        message = fallback_message
        material = None
        material_payload: dict[str, Any] | None = None
        reply_meta: dict[str, Any] = {
            "mode": "fallback",
            "reason": "reply_not_configured",
            "deltas": 0,
        }
        if self._make_reply is not None:
            material = build_material(
                user_text=prepared.input.text,
                course=None,
                composite_results=composite,
                waiting_for=None,
                question_titles=self._question_titles([question]),
                suggestions=[item.label for item in suggestions],
                context_summary=self._recent_turns_summary(prepared.conversation_id),
                teaching=self._teaching_material(ctx),
            )
            material_payload = material_to_payload(material)
            # 正文还没开始输出就先记成“待生成”：若客户端此刻断开或进程被
            # 重启，恢复记录里已经有讲解和材料，重试只需重做正文。
            reply_meta = {"mode": "fallback", "reason": "reply_pending", "deltas": 0}

        # 顺序很重要：先把讲解、目标绑定、回复材料全部落盘，再开始流式输出。
        # 这样“讲完题但正文没写完”不会变成需要重新讲题的中断轮次。
        self._repository.complete_dynamic_turn(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind=kind,
            tutoring=payload,
            composite_results=composite,
            question=question,
            question_result_status="ok",
            assistant_message=message,
            suggestions=suggestions,
            problem_id=problem.id,
        )
        if self._make_reply is not None:
            self._repository.update_turn_reply(
                prepared.conversation_id,
                prepared.request_id,
                assistant_message=message,
                reply_meta=reply_meta,
                reply_input=material_payload,
            )
        self._repository.upsert_context(
            prepared.conversation_id,
            active_problem_id=problem.id,
            last_suggestions_json=json.dumps(
                [it.model_dump(mode="json") for it in suggestions],
                ensure_ascii=False,
            ),
            last_action_summary=message,
        )
        state.saved = True

        if material is not None:
            yield TurnProgressEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                stage="composing_reply",
                message="正在组织讲解",
            )
            meta: dict[str, Any] = {}
            async for piece in self._reply_pieces(self._make_reply(), material, meta):
                yield TurnMessageDeltaEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    seq=int(meta["deltas"]),
                    delta=piece[:400],
                )
            text = str(meta.pop("text", "") or "")
            if text:
                message = text
            reply_meta = (
                meta
                if meta.get("mode")
                else {"mode": "fallback", "reason": "empty_reply", "deltas": 0}
            )
            # 正文结束（或失败）后再更新一次：正文与状态一起变成最终值。
            self._repository.update_turn_reply(
                prepared.conversation_id,
                prepared.request_id,
                assistant_message=message,
                reply_meta=reply_meta,
                reply_input=material_payload,
            )

        if self._make_reply is None:
            # 没有配置回复模型：正文是确定性说明，不标正文状态。
            reply_status, reply_retryable = None, False
        else:
            reply_status, reply_retryable = _reply_state(
                {"reply": reply_meta, "reply_input": material_payload}
            )
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind=kind,
            tutoring=payload,
            composite_results=composite,
            question=question,
            question_result_status="ok",
            assistant_message=message,
            reply_status=reply_status,  # type: ignore[arg-type]
            reply_retryable=reply_retryable,
            suggestions=suggestions,
        )

    async def _explain_selection(
        self, prepared: ExecuteTurn, selection: QuestionSelection
    ) -> tuple[TutoringResult, ProblemRecord, QuestionCard]:
        """用真实题目材料讲解，并把题目绑定为当前原题。"""

        if self._make_tutor is None:
            raise TutoringUnavailableError(
                self._tutoring_error or "辅导模型尚未配置。"
            )
        material = await asyncio.to_thread(self.questions.material, selection)
        card = material.card or self.questions.card_for_selection(selection)
        student_answer = prepared.input.student_answer_text
        if student_answer and (
            card.question_type != "choice"
            or student_answer not in {option.label for option in card.options}
        ):
            raise TurnInvalidError("请选择这道选择题实际提供的选项。")
        tutor = self._make_tutor()
        explanation = await tutor.explain(
            statement=material.text,
            statement_images=material.images,
            solution_images=[],
            student_answer=student_answer,
            solution_text=None,
        )
        links = self._import_question_images(
            prepared.conversation_id, material.images
        )
        source = Source(
            type="registered_paper",
            paper_id=card.source.paper_id,
            paper_title=card.source.paper_title,
        )
        with self._repository.action_write():
            saved = self._repository.create_problem_version(
                conversation_id=prepared.conversation_id,
                problem=NewProblem(
                    statement_text=compose_statement(card),
                    status="explained",
                    clarity="clear",
                    unclear_points=[],
                    uncertainties=[],
                    source=source,
                    student_answer_text=student_answer,
                    solution_text=None,
                    explanation=explanation,
                ),
                attachments=links,
            )
            self._repository.bind_question_problem(selection.id, saved.id)
            self._repository.activate_question_selection(prepared.conversation_id, selection.id)
            self._repository.reveal_question_answer(
                prepared.conversation_id,
                selection.question_id,
                source="explain",
                selection_id=selection.id,
            )
            self._repository.clear_active_exercise(prepared.conversation_id)
        payload = build_explanation_result(
            saved, explanation, record_id=self._record_id_for(saved)
        )
        question = self.questions.card_for_selection(selection, selected=True)
        question = question.model_copy(update={"answer_revealed": True})
        if material.evidence is not None and material.evidence.status == "ok":
            question = attach_answer(question, material.evidence)
        return payload, saved, question

    def _import_question_images(
        self, conversation_id: str, payloads: list
    ) -> list[tuple[str, str]]:
        """把题面图复制为本次讲解的附件；超限或无效图跳过，不阻止讲解。"""

        links: list[tuple[str, str]] = []
        for payload in payloads[: images.MAX_IMAGES_PER_TURN]:
            try:
                validated = images.validate_image(payload.data)
            except Exception:
                logger.warning("题面图校验失败，已跳过", exc_info=True)
                continue
            attachment_id = str(uuid.uuid4())
            try:
                stored_name = images.save_attachment(
                    self._attachments_root,
                    conversation_id,
                    attachment_id,
                    validated,
                )
            except Exception:
                logger.warning("题面图保存失败，已跳过", exc_info=True)
                continue
            try:
                self._repository.create_attachment(
                    conversation_id=conversation_id,
                    attachment_id=attachment_id,
                    stored_name=stored_name,
                    mime=validated.mime,
                    width=validated.width,
                    height=validated.height,
                    size_bytes=len(validated.data),
                    sha256=validated.sha256,
                )
            except Exception:
                images.delete_attachment(self._attachments_root, stored_name)
                logger.warning("题面图登记失败，已跳过", exc_info=True)
                continue
            links.append((attachment_id, "statement"))
        return links

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
                # 恢复轮次同样允许在预算内继续决策，不能只执行一次就收尾。
                max_rounds = MAX_SCHEDULER_ROUNDS
            else:
                scheduler = self._make_scheduler()
                ctx = self._load_dynamic_context(prepared.conversation_id)
                ctx.recovery = {"version": 2, "input": json.loads(self._canonical(prepared.input)),
                                "actions": [], "pending": [], "context": self._context_snapshot(ctx),
                                "dispatch": "tools", "reply": None}
                max_rounds = MAX_SCHEDULER_ROUNDS
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

            capability = self.questions.capability(
                ctx.confirmed_course or "Python语言程序设计"
            )
            question_context = f"【真题库】{capability.message}"[:200]
            if ctx.question_selection is not None:
                question_context = (
                    question_context
                    + f" 当前选中第 {ctx.question_selection.number_label} 题，可讲解或查看答案。"
                )[:200]

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
                    "question_context": question_context,
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

            # 有些轮次模型不调用任何工具（例如直接问“第一题考啥”）：回答范围
            # 也要按学生原话确定，否则正文可能自己展开完整解答。
            if ctx.answer_scope is None:
                ctx.answer_scope = self._answer_scope(prepared.input.text)
            composite_results = carried + self._dynamic_composite_results(final_state)
            suggestions = self._dynamic_suggestions(final_state, composite_results)
            # 先算好正文材料并落盘，再开始流式输出：客户端中断或进程重启时，
            # 业务结果和“只重做正文”的材料都已经在库里。
            fallback_text = self._dynamic_reply(final_state, composite_results)
            material = self._build_reply_material(
                prepared, ctx, composite_results, suggestions, recent_summary
            )
            if ctx.recovery is not None and material is not None:
                ctx.recovery["reply_input"] = material_to_payload(material)
                ctx.recovery["reply"] = {
                    "mode": "fallback",
                    "reason": "reply_pending",
                    "deltas": 0,
                }
                self._repository.save_recovery(
                    prepared.conversation_id, prepared.request_id, ctx.recovery
                )
            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="dynamic",
                result=ctx.result,
                tutoring=ctx.tutoring,
                exercise=ctx.exercise_view,
                evaluation=ctx.evaluation_view,
                composite_results=composite_results,
                assistant_message=fallback_text,
                suggestions=suggestions,
                question_results=ctx.question_results,
                question_result_status=ctx.question_result_status,
                question_result_message=ctx.question_result_message,
                question=ctx.question_card,
                problem_id=ctx.problem_id,
                exercise_id=ctx.exercise_id,
                question_context=ctx.question_context or None,
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
            state.saved = True
            # 工具决策阶段不转发正文；业务结果确定后才单独流式生成最终回复。
            reply_text, reply_meta = await self._compose_reply(
                prepared,
                ctx,
                final_state,
                composite_results,
                suggestions,
                material=material,
                emit=emit,
                push=queue.put_nowait,
                recent_summary=recent_summary,
            )
            reply_status: str | None = None
            reply_retryable = False
            reply_input = ctx.recovery.get("reply_input") if ctx.recovery else None
            # 轮次此刻已经是 completed，不能再走 save_recovery（它只接受运行中的
            # 轮次）；update_turn_reply 同时写正文、回复状态与材料，且没有状态守卫。
            self._repository.update_turn_reply(
                prepared.conversation_id,
                prepared.request_id,
                assistant_message=reply_text,
                reply_meta=reply_meta,
                reply_input=reply_input,
            )
            reply_status, reply_retryable = _reply_state(
                {"reply": reply_meta, "reply_input": reply_input}
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
                question_results=ctx.question_results,
                question_result_status=ctx.question_result_status,  # type: ignore[arg-type]
                question_result_message=ctx.question_result_message,
                question=ctx.question_card,
                exercise_id=ctx.exercise_id,
                reply_status=reply_status,  # type: ignore[arg-type]
                reply_retryable=reply_retryable,
            )
        )
        queue.put_nowait(SENTINEL)

    def _load_dynamic_context(self, conversation_id: str) -> _DynamicContext:
        ctx_record = self._repository.get_context(conversation_id)
        active_problem = self._repository.get_active_problem(conversation_id)
        active_exercise = self._repository.get_active_exercise(conversation_id)
        selection = self._repository.get_active_question(conversation_id)
        latest_question = self._latest_question_turn(conversation_id)
        return _DynamicContext(
            conversation_id=conversation_id,
            confirmed_course=ctx_record.confirmed_course if ctx_record else None,
            active_problem=active_problem,
            active_exercise=active_exercise,
            problem_id=active_problem.id if active_problem is not None else None,
            exercise_id=active_exercise.id if active_exercise is not None else None,
            question_selection=selection,
            question_card=(
                self.questions.card_for_selection(selection)
                if selection is not None
                else None
            ),
            question_results=(
                list(latest_question.question_results) if latest_question else []
            ),
            question_source_turn=(
                latest_question.request_id if latest_question else None
            ),
        )

    @staticmethod
    def _context_snapshot(ctx: _DynamicContext) -> dict[str, Any]:
        return {"confirmed_course": ctx.confirmed_course, "problem_id": ctx.problem_id,
                "exercise_id": ctx.exercise_id,
                "question": {
                    "selection_id": (
                        ctx.question_selection.id
                        if ctx.question_selection is not None
                        else None
                    ),
                    "source_turn": ctx.question_source_turn,
                    "status": ctx.question_result_status,
                    "message": ctx.question_result_message,
                    "cards": [card.public_snapshot() for card in ctx.question_results],
                },
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
        question = snapshot.get("question") or {}
        cards: list[QuestionCard] = []
        for item in question.get("cards") or []:
            with suppress(Exception):
                cards.append(QuestionCard.model_validate(item))
        selection: QuestionSelection | None = None
        selection_id = question.get("selection_id")
        if selection_id:
            with suppress(Exception):
                selection = self._repository.get_question_selection_by_id(
                    conversation_id, str(selection_id)
                )
        if selection is None:
            selection = self._repository.get_active_question(conversation_id)
        return _DynamicContext(
            conversation_id=conversation_id, confirmed_course=snapshot.get("confirmed_course"),
            active_problem=problem, active_exercise=exercise, problem_id=problem_id, exercise_id=exercise_id,
            question_selection=selection,
            question_card=(
                self.questions.card_for_selection(selection)
                if selection is not None
                else (cards[0] if cards else None)
            ),
            question_results=cards,
            question_source_turn=question.get("source_turn"),
            question_result_status=question.get("status"),
            question_result_message=question.get("message"),
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
        observations = list(final_state.get("observations") or [])
        resolved = _rejections_fixed_later(observations)
        items: list[ActionResultItem] = []
        for index, observation in enumerate(observations):
            details = dict(observation.get("details") or {})
            if index in resolved:
                # 这次调用确实被拒了，但模型随后用正确参数重做成功；
                # 拒绝只是诊断历史，不能让业务状态停在“部分完成”。
                continue
            if details.get("waiting_user"):
                # “因为要等学生回答所以没执行”不是业务结果，也不是失败：
                # 这一轮的澄清条目会单独表达“等待学生”。
                continue
            if details.get("rejected"):
                # 协议层拒绝确实“没有执行”，不能因为消息不适合原样展示就删掉，
                # 否则全部调用被拒时会变成“已完成”。这里改成一条可读的失败记录。
                tool = str(details.get("tool") or "")
                items.append(
                    ActionResultItem(
                        action=f"tool:{tool}" if tool else "unknown_tool",
                        status="failed",
                        message=str(
                            observation.get("message")
                            or "有一次工具调用没有通过参数校验，因此没有执行。"
                        ),
                        details={
                            **details,
                            "rejected": True,
                            "retryable": True,
                            "label": _tool_label(tool) if tool else "未知工具",
                        },
                    )
                )
                continue
            status = observation.get("status")
            if status not in ("success", "failed", "clarification"):
                status = "failed"
            items.append(
                ActionResultItem(
                    action=str(observation.get("action") or "unknown"),
                    status=status,  # type: ignore[arg-type]
                    message=str(observation.get("message") or ""),
                    details=details,
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
            if clarified:
                # 失败后模型选择“问学生一句”时，问题本身必须出现在正文里，
                # 否则界面标成“待回答”却看不到要回答什么。
                parts.append(clarified[-1].message)
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
        if any(it.action == "search_questions" and it.status == "success" for it in composite_results):
            return "\n".join(it.message for it in composite_results)
        if composite_results:
            # 有真实执行结果时以结果为准：模型收尾的那句话（例如“好的。”）
            # 不能覆盖“到底做了什么”，否则学生看不到确认信息。
            return "\n".join(it.message for it in composite_results)
        if assistant_reply:
            return assistant_reply
        return "已为您处理完成。"

    def _build_reply_material(
        self,
        prepared: ExecuteTurn,
        ctx: "_DynamicContext",
        composite_results: list[ActionResultItem],
        suggestions: list[SuggestionItem],
        recent_summary: str | None,
    ) -> ReplyMaterial | None:
        """构建正文材料；纯澄清轮不需要回复模型，返回 ``None``。

        单独拆出来的原因：材料要在正文开始输出之前就存进恢复记录，否则
        客户端中途断开时，就只剩“重新执行一遍工具”这一条路。
        """

        clarified = [it for it in composite_results if it.status == "clarification"]
        teaching = self._teaching_material(ctx)
        if clarified and not teaching and ctx.answer_scope is None:
            # 纯澄清：问题是要学生逐字回答的，不能让回复模型改写或替学生作答，
            # 因此直接用工具给出的问题原文，也不再消耗一次模型调用。
            # 但如果这一轮已经完成讲解，就必须先把讲解写进正文（见下）。
            # “只要提示/只问考点”不是纯澄清：正文必须真的给出提示或考点。
            return None
        return self._restrict_reply_material(build_material(
            user_text=prepared.input.text,
            course=ctx.confirmed_course,
            composite_results=composite_results,
            waiting_for=(
                None
                if ctx.answer_scope is not None
                else (clarified[-1].message if clarified else self._waiting_for(composite_results))
            ),
            question_titles=self._question_titles(ctx.question_results),
            question_materials=self._question_materials(ctx, prepared.input.text),
            question_page=(
                ctx.question_context if ctx.question_context.get("paper_id") else None
            ),
            answer_scope=ctx.answer_scope,
            suggestions=[item.label for item in suggestions],
            context_summary=recent_summary,
            teaching=teaching,
        ))

    @classmethod
    def _restrict_reply_material(cls, material: ReplyMaterial) -> ReplyMaterial:
        """公开题干仍可被模型自行解出；纯选题/浏览不需要把它交给正文。

        同一投影也在生成前应用，防止旧恢复材料绕过本轮回答范围。
        前端题卡不受影响；考点、提示、题意或明确解题请求仍获得公开材料。
        """
        if (
            material.teaching or material.answer_scope is not None
            or (
                not cls._wants_to_try_first(material.user_text)
                and cls._wants_explanation(material.user_text)
            )
        ):
            return material
        return replace(material, question_materials=(), context_summary=None)

    @classmethod
    def _selection_reply(cls, material: ReplyMaterial) -> str | None:
        """纯选题已完成时只确认真实目标，不让正文擅自解题或补猜题量。

        这只决定已完成操作的展示方式；Agent 的继续决策和用户的后续请求不受限。
        """
        if (
            material.teaching or material.answer_scope or cls._wants_explanation(material.user_text)
            or not material.results
            or any(item.action != "select_question" or item.status != "success" for item in material.results)
        ):
            return None
        details = material.results[-1].details
        title, number = details.get("paper_title"), details.get("number_label")
        kind = {"choice": "选择题", "fill": "填空题", "code": "编程题"}.get(details.get("question_type"))
        if not title or not number or not kind:
            return None
        return f"已选中《{title}》的{kind}第 {number} 题。"

    @staticmethod
    def _question_materials(ctx: _DynamicContext, text: str = "") -> list[str]:
        """本轮公开题面材料：题干、选项、代码、知识点与来源（不含答案）。

        只放本轮真实返回、并且已经在卡片上展示给学生的题；“第 5 题”这类
        明确题号会被排到最前面，保证提示和考点回答针对学生问的那一道。
        超出长度时截断并注明，不静默丢题。
        """

        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        cards: list[QuestionCard] = []
        if ctx.question_card is not None:
            cards.append(ctx.question_card)
        for card in ctx.question_results:
            if all(item.question_id != card.question_id for item in cards):
                cards.append(card)
        wanted = candidate_number(text)
        if wanted is not None:
            preferred = [card for card in cards if ConversationService._same_number(card.number_label, wanted)]
            if preferred:
                cards = preferred + [card for card in cards if card not in preferred]
        lines: list[str] = []
        used = 0
        budget = 4000
        for index, card in enumerate(cards):
            pieces = [
                f"第 {card.number_label} 题（{kinds.get(card.question_type, '题目')}"
                + (f"，{card.source.paper_title}" if card.source.paper_title else "")
                + "）",
                f"题干：{card.statement[:300]}",
            ]
            if card.options:
                options = "；".join(
                    f"{item.label}. {item.text[:60]}" for item in card.options[:8]
                )
                pieces.append("选项：" + options)
            if card.code_lines:
                pieces.append("代码：\n" + "\n".join(card.code_lines[:15])[:600])
            approved = [
                item.name
                for item in card.knowledge_points
                if getattr(item, "status", "") == "verified"
            ]
            candidates = [
                item.name
                for item in card.knowledge_points
                if getattr(item, "status", "") != "verified"
            ]
            if approved:
                pieces.append("已审核知识点：" + "、".join(approved[:6]))
            if candidates:
                pieces.append("题库候选知识点（未审核，仅供参考）：" + "、".join(candidates[:6]))
            text = "｜".join(pieces)
            if used + len(text) > budget and lines:
                lines.append(f"（另有 {len(cards) - index} 道题面没有列出，正文可以让学生按题号继续问）")
                break
            lines.append(text)
            used += len(text)
        return lines

    async def _compose_reply(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        final_state: dict[str, Any],
        composite_results: list[ActionResultItem],
        suggestions: list[SuggestionItem],
        *,
        material: ReplyMaterial | None = None,
        emit: Callable[[str, str], None],
        push: Callable[[object], None],
        recent_summary: str | None,
    ) -> tuple[str, dict[str, Any]]:
        """业务结果确定后单独流式生成正文；失败时退回确定性说明。

        返回值第二项写入恢复记录的 ``reply`` 字段，用于区分“模型真实生成”
        与“程序兜底”，报告不能把兜底算作自然回复验证通过。
        """

        fallback = self._dynamic_reply(final_state, composite_results)
        clarified = [it for it in composite_results if it.status == "clarification"]
        if material is None:
            material = self._build_reply_material(
                prepared, ctx, composite_results, suggestions, recent_summary
            )
        if material is None:
            return fallback, {"mode": "clarification", "deltas": 0}

        if ctx.recovery is not None:
            # 留着材料，回复失败/截断时可以只重做正文，不重跑已经成功的操作。
            ctx.recovery["reply_input"] = material_to_payload(material)
        if self._make_reply is None:
            return fallback, {
                "mode": "fallback",
                "reason": "reply_not_configured",
                "deltas": 0,
            }

        emit("composing_reply", "正在组织回复")

        def on_piece(piece: str, seq: int) -> None:
            push(
                TurnMessageDeltaEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    seq=seq,
                    delta=piece[:400],
                )
            )

        text, meta = await self._stream_reply(self._make_reply(), material, on_piece)
        if text and clarified and material.answer_scope is None:
            # 讲解之后再问下一步：讲解已经由模型写出，问题按工具原文补在末尾，
            # 既不会丢讲解，也不会让模型改写要学生回答的问题。
            question = clarified[-1].message.strip()
            if question and question not in text:
                seq = int(meta.get("deltas") or 0) + 1
                extra = "\n\n" + question
                on_piece(extra, seq)
                text = text + extra
                meta["deltas"] = seq
        if text:
            return text, meta
        return fallback, dict(meta, mode="fallback")

    async def _stream_reply(
        self,
        reply: Any,
        material: ReplyMaterial,
        on_piece: Callable[[str, int], None] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """流式生成正文，并如实报告“完整 / 截断 / 失败”。

        已经产出的文字不会丢弃：截断或中途出错时保留残文，由公开状态
        告诉前端“正文未完整生成”，而不是把它伪装成完整回复。
        """

        meta: dict[str, Any] = {}
        async for piece in self._reply_pieces(reply, material, meta):
            if on_piece is not None:
                on_piece(piece, meta["deltas"])
        text = str(meta.pop("text", "") or "")
        return text, meta

    async def _reply_pieces(
        self,
        reply: Any,
        material: ReplyMaterial,
        meta: dict[str, Any],
    ) -> AsyncIterator[str]:
        """逐块产出正文，并把“完整/截断/失败”写进 ``meta``。

        做成异步生成器的原因：普通路径要把片段推给 SSE 队列，真题讲解路径
        要直接 ``yield`` 事件。两条路径共用同一套截断与兜底判定，避免出现
        “一条路径有正文、另一条没有”的分叉。
        """

        pieces: list[str] = []
        material = self._restrict_reply_material(material)
        selection_reply = self._selection_reply(material)
        if selection_reply:
            # 没尝试回复模型，保持公开 reply_status=None；reason 标明确定性回执，
            # 不伪称真实模型生成，也不把已经完整的确认语标成失败。
            meta.update(mode=None, reason="selection_ack", deltas=1, text=selection_reply)
            yield selection_reply
            return
        seq = 0
        meta.update({"mode": "model", "deltas": 0, "text": ""})
        try:
            async for piece in reply.stream(material):
                if not piece:
                    continue
                seq += 1
                pieces.append(piece)
                meta["deltas"] = seq
                yield piece
        except ReplyTruncated as exc:
            logger.warning("最终回复被截断：%s", exc.reason)
            meta.update(
                mode="truncated",
                reason=exc.reason,
                deltas=seq,
                text="".join(pieces).strip(),
            )
            return
        except Exception:
            logger.warning("最终回复生成失败", exc_info=True)
            text = "".join(pieces).strip()
            if text:
                meta.update(mode="incomplete", reason="stream_error", deltas=seq, text=text)
            else:
                meta.update(mode="fallback", reason="reply_failed", deltas=seq, text="")
            return

        text = "".join(pieces).strip()
        if not text:
            meta.update(mode="fallback", reason="empty_reply", deltas=seq, text="")
            return
        meta.update(mode="model", deltas=seq, text=text)

    @staticmethod
    def _waiting_for(composite_results: list[ActionResultItem]) -> str | None:
        """推断“本轮结束后在等学生做什么”，供正文使用。"""

        hints = {
            "search_questions": "选择题号，或直接作答这道真题",
            "select_question": "作答这道题，或要求讲解",
            "generate_exercise": "作答当前练习",
        }
        for item in reversed(composite_results):
            if item.status != "success":
                continue
            if item.details.get("needs_supplement"):
                return "补充解题过程后重新提交"
            if item.action == "search_questions" and item.details.get("selected"):
                # 学生已经明确要求直接讲解/看答案，本次已自动选中，不再等待选择。
                continue
            hint = hints.get(item.action)
            if hint:
                return hint
        return None

    @staticmethod
    def _teaching_material(ctx: "_DynamicContext") -> str | None:
        """把学生已经能看到的讲解整理成正文材料。

        只取卡片上已经展示过的内容：讲解、追问回答、学生主动查看后的参考
        解答与评价。未查看的答案不会出现在这里，因此正文不会提前泄题。
        """

        tutoring = ctx.tutoring
        kind = getattr(tutoring, "kind", None) if tutoring is not None else None
        # 讲解结果有显式 kind；测试替身/旧记录可能直接给讲解内容本身，
        # 因此再按结构判断一次（有步骤和最终答案即是讲解）。
        if kind == "explanation" or (
            kind is None
            and tutoring is not None
            and getattr(tutoring, "steps", None)
            and getattr(tutoring, "final_answer", None)
        ):
            problem = getattr(tutoring, "problem", None)
            statement = getattr(problem, "statement_text", None)
            lines = [f"题目：{statement[:400]}"] if statement else []
            points = getattr(tutoring, "knowledge_points", None) or []
            if points:
                lines.append(
                    "知识点："
                    + "；".join(
                        f"{point.name}（{point.explanation[:80]}）" for point in points
                    )
                )
            for step in getattr(tutoring, "steps", None) or []:
                lines.append(f"第{step.index}步 {step.title}：{step.content[:400]}")
            final_answer = getattr(tutoring, "final_answer", None)
            if final_answer:
                lines.append(f"最终答案：{final_answer[:600]}")
            error_analysis = getattr(tutoring, "error_analysis", None)
            if error_analysis:
                lines.append(f"易错点：{error_analysis[:400]}")
            return "\n".join(lines)
        if kind == "followup_answer":
            answer = getattr(tutoring, "answer", None)
            if answer:
                return f"追问回答：{answer[:900]}"

        exercise = ctx.exercise_view
        if exercise is not None:
            solution = getattr(exercise, "reference_solution_text", None)
            if solution:
                # 只有学生主动“查看参考解答”之后，这里才会有内容。
                return f"参考解答（学生已主动查看）：{solution[:900]}"
            evaluation = getattr(exercise, "evaluation", None)
            if evaluation is not None:
                # 判定与“是否需要补步骤”是两件事：判错不等于要补充，
                # 依据不足也不等于答错。分别表达，正文才不会改写原评价含义。
                if evaluation.is_correct is True:
                    verdict = "正确"
                elif evaluation.is_correct is False:
                    verdict = "错误"
                else:
                    verdict = "依据不足，暂时无法判定对错"
                if getattr(evaluation, "needs_supplement", False):
                    verdict += "；需要补充解题过程后才能判断"
                parts = [
                    f"判定：{verdict}",
                    f"点评：{evaluation.final_answer_assessment[:300]}",
                    f"步骤分析：{evaluation.step_by_step_analysis[:400]}",
                    f"建议：{evaluation.guidance[:300]}",
                ]
                return "\n".join(parts)
        return None

    @staticmethod
    def _question_titles(cards: list[QuestionCard]) -> list[str]:
        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        return [
            f"第 {card.number_label} 题（{kinds.get(card.question_type, '题目')}）"
            for card in cards[:5]
        ]

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
            guarded = self._guard_dynamic_action(
                action, ctx, review_state, prepared.input.text
            )
            outcome = (
                guarded
                if guarded is not None
                else await self._run_dynamic_action(
                    prepared, action, review_state, ctx, emit
                )
            )
        except ConversationStorageError:
            raise
        except (AttachmentInvalidError, InvalidImageError) as exc:
            outcome = ActionOutcome(action.action, "failed", str(exc), error_code="INVALID_INPUT")
        except (ConversationConflictError, ProblemNotFoundError, ProblemNotReadyError) as exc:
            outcome = ActionOutcome(action.action, "failed", str(exc), error_code="BUSINESS_REJECTED", needs_user=True)
        except QueryError as exc:
            outcome = ActionOutcome(action.action, "failed", exc.message, error_code=exc.code, retryable=exc.code in RECOVERABLE_CODES)
        except QuestionError as exc:
            outcome = ActionOutcome(
                action.action,
                "failed",
                exc.message,
                error_code=exc.code,
                retryable=exc.status_code == 503,
                needs_user=exc.status_code in (404, 409, 410),
            )
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

    def _dynamic_state_patch(
        self, ctx: _DynamicContext, review_state: dict[str, Any]
    ) -> dict[str, Any]:
        active_problem = ctx.active_problem
        active_exercise = ctx.active_exercise
        return {
            "confirmed_course": ctx.confirmed_course,
            "question_context": self._question_context_line(ctx, review_state),
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

    def _question_context_line(
        self, ctx: _DynamicContext, review_state: dict[str, Any]
    ) -> str:
        """给决策模型的结构化试卷/浏览事实。

        这一段决定模型知不知道“刚刚那张卷是哪张、现在看到第几道”。只写真实
        数字；本轮还没有新查题时，从库里最近一次成功记录恢复。
        """

        context = ctx.question_context or self._question_context(ctx)
        if not context and not ctx.question_results:
            return review_state.get("question_context") or ""
        parts: list[str] = []
        if context.get("paper_id"):
            parts.append(f"当前试卷ID：{context['paper_id']}")
        title = context.get("paper_title")
        if title:
            parts.append(f"当前试卷：《{title}》")
        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        kind = kinds.get(context.get("question_type") or "")
        if kind:
            parts.append(f"当前题型：{kind}")
        total = context.get("total")
        offset = context.get("offset") or 0
        returned = context.get("returned") or 0
        if total is not None:
            parts.append(f"共 {total} 道已审核题目")
        if returned:
            parts.append(
                f"已展示第 {offset + 1}–{offset + returned} 道"
                + (f"，下一批从第 {offset + returned + 1} 道开始" if context.get("next_offset") else "，已全部展示")
            )
        if ctx.question_selection is not None:
            parts.append(
                f"已选中第 {ctx.question_selection.number_label} 题，可直接讲解或看答案"
            )
        elif ctx.question_results:
            parts.append("学生还没指定哪一道；他来问考点或要讲解时按题号定位")
        parts.append(
            "学生说“刚刚那张卷”“这张卷”时沿用当前试卷；说“继续/下一批”时从上一句的位置往后"
        )
        return "；".join(parts)[:400]

    def _guard_dynamic_action(
        self,
        action: ActionPlan,
        ctx: _DynamicContext,
        review_state: dict[str, Any],
        text: str,
    ) -> ActionOutcome | None:
        """程序侧红线：模型“顺手”出题或保存时在这里被拦下。

        - 学生只在自述理解（“我懂了”），没有明确要求练习或保存时，拒绝出题和
          正式保存；
        - 助手刚给过多个建议，学生只回一个“好”，无法判断指向时先澄清。
        参数、对象归属等其余校验仍在 ``_run_dynamic_action`` 内完成。
        """

        name = action.action
        stripped = (text or "").strip()
        if not stripped or name not in _RED_LINE_ACTIONS:
            return None

        explicit = bool(
            _PRACTICE_REQUEST_RE.search(stripped) or _SAVE_REQUEST_RE.search(stripped)
        )
        if _SELF_REPORT_RE.search(stripped) and not explicit:
            return ActionOutcome(
                name,
                "clarification",
                "已记录你的理解。要练一道类似题，或把这道题存进错题本时，直接告诉我。",
                needs_user=True,
            )

        suggestions = review_state.get("last_suggestions") or []
        if len(suggestions) >= 2 and not explicit and _is_bare_affirmative(stripped):
            options = "、".join(str(item) for item in suggestions[:5])
            return ActionOutcome(
                "clarify_or_reply",
                "clarification",
                f"你刚才的选项有：{options}。想先做哪一步？",
                needs_user=True,
            )
        return None

    async def _run_dynamic_action(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        review_state: dict[str, Any],
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = action.action
        # 回答范围只由学生本轮的话决定：提示、考点都不等于“完整讲解/看答案”。
        if ctx.answer_scope is None:
            ctx.answer_scope = self._answer_scope(prepared.input.text)
        if (
            name in _DISCLOSURE_ACTIONS
            and ctx.answer_scope is not None
            and not prepared.input.student_answer_text
        ):
            # 先按同一入口确定题目，再把请求转换为公开材料问答。
            # 题目唯一且材料齐备时是可完成的请求，不标成“等待学生作答”。
            cards, _ = self._question_candidates(ctx)
            if cards or ctx.question_selection is not None:
                _, problem = await self._resolve_question_selection(prepared, ctx, use_active=True)
                if problem:
                    ctx.answer_scope = None
                    return self._action_result(prepared, ctx, ActionOutcome(
                        name, "clarification", problem, needs_user=True,
                    ))
            ctx.tutoring = None
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "success",
                (
                    "已取得公开题面，按你的要求只给提示，不公布答案或完整解法。"
                    if ctx.answer_scope == "hint"
                    else "已取得公开题面，只回答这道题考什么，不展开完整解法或公开答案。"
                ),
                {"mode": "public_material"},
                needs_user=False,
            ))
        if (
            name in {"explain_problem", "tutoring", "view_question_answer", "reveal_solution",
                     "followup_problem", "followup"}
            and self._wants_to_try_first(prepared.input.text)
            and not prepared.input.student_answer_text
        ):
            # 学生明确要自己先做：这时不讲解、不公开答案，等他自己写。
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                (
                    "好，我只说到思路这一层，不公布答案；你先试试，卡住了再告诉我。"
                    if self._wants_hint_only(prepared.input.text)
                    else "好，这道题先留给你自己做。写完把答案或思路发我，我再批改或按你的思路讲。"
                ),
                needs_user=True,
            ))
        if (
            name in {"view_question_answer", "reveal_solution"}
            and is_question_lookup(prepared.input.text)
            and not question_answer_requested(prepared.input.text)
            and not prepared.input.student_answer_text
        ):
            # 学生明确在找新题、还没说要看答案时，不能借旧题目泄露答案。
            ctx.question_card = None
            ctx.tutoring = None
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "先确认要看的题目；学生明确要答案时再展示，不做默认公开。",
                needs_user=True,
            ))
        if (
            name in {"explain_problem", "tutoring", "followup_problem", "followup"}
            and is_question_lookup(prepared.input.text)
            and not prepared.input.student_answer_text
            and not self._wants_explanation(prepared.input.text)
            and not question_answer_requested(prepared.input.text)
        ):
            # 学生只是找题、没说要讲：程序不允许模型自作主张开始讲解。
            ctx.question_card = None
            ctx.tutoring = None
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "先按你找到的题目列出来；要讲哪一道、讲什么，你说一声我再展开。",
                needs_user=True,
            ))
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

        if name == "search_questions":
            return await self._dynamic_search_questions(prepared, action, ctx, emit)

        if name == "list_paper_questions":
            return await self._dynamic_list_paper_questions(prepared, action, ctx, emit)

        if name == "select_question":
            return await self._dynamic_select_question(prepared, action, ctx)

        if name == "view_question_answer":
            return await self._dynamic_view_question_answer(prepared, ctx, emit)

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
            # 原生工具把澄清问题放在 ask_user 的 question 参数里；旧动作记录
            # 仍从 assistant_reply 读取，两种来源都兼容。
            raw_question = action.parameters.get("question")
            message = (
                raw_question.strip()
                if isinstance(raw_question, str) and raw_question.strip()
                else (review_state.get("assistant_reply") or "请告诉我您希望先做哪一步。")
            )
            return self._action_result(prepared, ctx, ActionOutcome(name, "clarification", message, needs_user=True))

        return self._action_result(prepared, ctx, ActionOutcome(name, "failed", "暂不支持这个操作。"))

    async def _dynamic_search_questions(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "search_questions"
        emit("searching_questions", "正在检索已审核的 Python 真题")
        intent = self._search_intent(prepared.input.text, ctx, action)
        if not intent.course:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "请先告诉我课程，例如“找一道 Python 期末真题讲讲”。",
                needs_user=True,
            ))
        if intent.paper_unresolved:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "我无法从这些条件唯一确定一份试卷，请补充年份、学期或考试类型；"
                "不会扩大到其他试卷。",
                needs_user=True,
            ))
        if intent.page_exhausted:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "clarification", self._exhausted_text(ctx, intent), needs_user=True
            ))
        result = await self._run_question_search(
            prepared, ctx, name, intent, emit
        )
        if isinstance(result, ActionOutcome):
            return result
        return self._question_search_outcome(prepared, ctx, name, intent, result)

    def _exhausted_text(self, ctx: _DynamicContext, intent: Any) -> str:
        """这一条件下已经全部展示过：说明范围，而不是从第 1 道重放。"""

        context = self._question_context(ctx)
        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        kind = kinds.get(context.get("question_type") or "", "")
        total = context.get("total")
        title = context.get("paper_title") or "这份试卷"
        scope = f"{title}的{kind}" if kind else f"{title}的题目"
        if total:
            return f"{scope}已经全部列完了（共 {total} 道），没有更多没显示的题。"
        return f"{scope}已经全部列完了，没有更多没显示的题。"

    async def _run_question_search(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        name: str,
        intent: Any,
        emit: Callable[[str, str], None],
    ) -> Any:
        """执行一次题库检索；失败时返回可以直接回传的 ``ActionOutcome``。"""

        request = SearchRequest(
            course=intent.course,
            query=intent.query,
            question_type=intent.question_type,  # type: ignore[arg-type]
            exam_type=intent.exam_type,  # type: ignore[arg-type]
            paper_id=intent.paper_id,
            number_label=intent.number_label,
            knowledge_points=list(intent.knowledge_points),
            offset=intent.offset,
            limit=intent.limit,
            preview_candidates=False,
        )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self.questions.search,
                    request,
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                ),
                timeout=20.0,
            )
        except asyncio.TimeoutError:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "failed",
                "查题超时，请稍后重试；仍可查看或下载原卷。",
                error_code="QUESTION_SEARCH_FAILED",
                retryable=True,
            ))
        except QuestionError as exc:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "failed", exc.message, error_code=exc.code,
                retryable=exc.status_code == 503,
            ))

    def _question_search_outcome(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        name: str,
        intent: Any,
        result: QuestionSearchResult,
    ) -> ActionOutcome:
        """把一次真实检索结果变成动作结果：公开总数、本次范围与下一步。"""

        shown = self._shown_ids(ctx)
        if intent.next_card and not intent.paper_id and result.cards:
            # 相关度检索没有“位置”可翻：按已经展示过的题排除，给出下一道没看过的。
            seen = set(shown)
            fresh = [card for card in result.cards if card.question_id not in seen]
            if not fresh:
                # 不能把旧题（可能已经公开答案）回显成这次的结果。
                ctx.question_results = []
                ctx.question_card = None
                ctx.question_selection = None
                ctx.tutoring = None
                return self._action_result(prepared, ctx, ActionOutcome(
                    name,
                    "clarification",
                    "这条条件下的题我都已经给你看过了；换个条件或去掉关键词再找，我就能给你新题。",
                    needs_user=True,
                ))
            result = result.model_copy(
                update={"cards": fresh[:1], "returned": 1, "next_offset": None}
            )
        ctx.question_results = list(result.cards)
        # Do not echo an already-revealed previous selection into a fresh search.
        ctx.question_card = None
        ctx.question_selection = None
        ctx.question_source_turn = prepared.request_id
        ctx.question_result_status = result.status
        ctx.question_result_message = result.message
        if intent.number_label is not None and intent.question_type is None and len(result.cards) > 1:
            kinds_all = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
            types = "、".join(
                dict.fromkeys(kinds_all.get(card.question_type, "题目") for card in result.cards)
            )
            ctx.question_result_message = (
                f"这份试卷的{types}均有第 {intent.number_label} 题，请按题型选择下面的题目。"
            )
        if result.status == "bank_unavailable":
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "failed",
                result.message,
                error_code="QUESTION_BANK_UNAVAILABLE",
                retryable=True,
            ))
        if result.status != "ok":
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "clarification", result.message, needs_user=True
            ))
        paper_id, paper_title = self._result_paper(result)
        if intent.paper_id:
            # 只有本轮真的锁定了某份试卷才记成“当前试卷”。跨卷相关度检索可能
            # 命中不同试卷的题，不能随手把其中一张记成学生选中的卷。
            ctx.question_context = {
                "paper_id": intent.paper_id,
                "paper_title": paper_title or intent.paper_id,
                "question_type": intent.question_type,
                "number_label": intent.number_label,
                "total": result.total,
                "offset": result.offset,
                "returned": result.returned,
                "next_offset": result.next_offset,
                # 记下这一页的大小：“继续”沿用同一页宽，而不是改成整卷。
                "limit": intent.limit,
                "request_id": prepared.request_id,
                "query": intent.query,
            }
        else:
            # 没有锁定试卷的相关度检索也要留下检索条件，供“换一道”继承。
            ctx.question_context = {
                **ctx.question_context,
                "query": intent.query,
                "question_type": intent.question_type,
                "number_label": intent.number_label,
                "request_id": prepared.request_id,
            }
        ctx.question_context["shown_ids"] = list(
            dict.fromkeys(shown + [card.question_id for card in result.cards])
        )[-200:]

        selected = False
        wants_explain = self._wants_explanation(prepared.input.text) or question_answer_requested(
            prepared.input.text
        )
        if intent.auto_explain and wants_explain and len(result.cards) == 1:
            card = result.cards[0]
            selection = self._select_question_card(
                prepared.conversation_id,
                prepared.request_id,
                prepared.request_id,
                card,
            )
            ctx.question_selection = selection
            ctx.question_card = card
            selected = True
        range_text = self._range_text(result, intent)
        if selected:
            message = f"找到 1 道已审核真题，并已选中第 {result.cards[0].number_label} 题。"
        elif intent.number_label is not None and intent.question_type is None and len(result.cards) > 1:
            message = ctx.question_result_message or result.message
        else:
            message = range_text
        details: dict[str, Any] = {
            "status": result.status,
            "count": len(result.cards),
            "question_ids": [card.question_id for card in result.cards],
            "selected": selected,
            "total": result.total,
            "offset": result.offset,
            "returned": result.returned,
            "next_offset": result.next_offset,
            "has_more": result.next_offset is not None,
            "paper_id": paper_id,
            "paper_title": paper_title,
            "question_type": intent.question_type,
            "number_label": intent.number_label,
        }
        return self._action_result(prepared, ctx, ActionOutcome(
            name,
            "success",
            message,
            details,
            # 查题成功是中间事实：是否结束本轮由模型根据学生真实要求决定，
            # 不再因为“这是查题动作”就一律要求先作答。
            needs_user=False,
        ))

    def _last_search_filter(self, ctx: _DynamicContext) -> dict[str, Any]:
        """上一次成功检索的条件（关键词、题型），供“换一道”继承。"""

        repository = getattr(self, "_repository", None)
        if repository is None:  # 隔离替身没有仓库
            return {}
        try:
            stored = repository.find_question_context(
                ctx.conversation_id, require_paper=False
            )
        except Exception:  # pragma: no cover - 读取失败不影响本轮
            logger.warning("读取上次检索条件失败", exc_info=True)
            return {}
        return dict(stored or {})

    def _shown_ids(self, ctx: _DynamicContext) -> list[str]:
        """本会话已经展示过的题：最近几轮的题卡 + 本轮已返回的题。

        “换一道”要靠它排除旧题；旧会话也能从题卡本身恢复，不依赖新增字段。
        """

        ids = [str(item) for item in (self._question_context(ctx).get("shown_ids") or [])]
        ids.extend(card.question_id for card in ctx.question_results)
        repository = getattr(self, "_repository", None)
        if repository is not None:
            try:
                for turn in repository.list_turns(ctx.conversation_id)[-4:]:
                    ids.extend(card.question_id for card in turn.question_results)
            except Exception:  # pragma: no cover - 读取失败不影响本轮
                logger.warning("读取已展示题目失败", exc_info=True)
        return list(dict.fromkeys(ids))[-200:]

    @staticmethod
    def _result_paper(result: QuestionSearchResult) -> tuple[str | None, str | None]:
        if result.paper_id:
            return result.paper_id, result.paper_title
        for card in result.cards:
            if card.source.paper_id:
                return card.source.paper_id, card.source.paper_title
        return None, None

    def _range_text(self, result: QuestionSearchResult, intent: Any) -> str:
        """“共 N 道，本次第 a–b 道，还有多少没显示”。"""

        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        paper_title = result.paper_title
        scope = f"{paper_title}的" if paper_title else ""
        kind = kinds.get(intent.question_type or "", "")
        label = f"{scope}{kind}" if kind else (f"{scope}题目" if paper_title else "已审核真题")
        total = result.total if result.total is not None else len(result.cards)
        if intent.number_label is not None:
            if intent.question_type is None and len(result.cards) > 1:
                kinds_all = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
                types = "、".join(
                    dict.fromkeys(kinds_all.get(card.question_type, "题目") for card in result.cards)
                )
                return f"这份试卷的{types}均有第 {intent.number_label} 题，请按题型选择下面的题目。"
            numbers = "、".join(f"第 {card.number_label} 题" for card in result.cards)
            return f"已定位到{label}{numbers}，共 {len(result.cards)} 道。"
        start = result.offset + 1
        end = result.offset + result.returned
        if total <= 0:
            return f"{label}没有符合条件的已审核题目。"
        text = f"{label}共 {total} 道，本次显示第 {start}–{end} 道。"
        if result.next_offset is not None:
            text += (
                f"还有 {total - end} 道没有显示，想接着看说“继续”即可。"
            )
        else:
            text += "已经全部列出。"
        return text

    def _search_intent(
        self, text: str, ctx: _DynamicContext, action: ActionPlan
    ) -> SearchIntent:
        papers: list[dict[str, Any]] = []
        if self._papers is not None:
            try:
                papers = [
                    item.model_dump()
                    for item in self._papers.list_papers(course="Python语言程序设计").items
                ]
            except Exception:
                logger.warning("读取试卷目录失败，查题不带试卷筛选", exc_info=True)
        confirmed = normalize_course(ctx.confirmed_course) if ctx.confirmed_course else None
        context = self._question_context(ctx, trace=True)
        current_paper = context.get("paper_id")
        if context.get("derived") and not self._refers_to_current_paper(text):
            # 旧会话里只有题卡时，不能把任意一次查题都算成“这张卷”。
            current_paper = None
        parameters = dict(action.parameters)
        if parameters.get("paper_id"):
            # 模型给的试卷 ID 必须真实存在；不存在就当没给，由上下文或原文决定。
            known = {str(item.get("id")) for item in papers}
            if str(parameters["paper_id"]) not in known:
                parameters.pop("paper_id")
        if parameters.get("offset") is None:
            parameters.pop("offset", None)
        intent = extract_intent(
            text,
            confirmed_course=confirmed,
            planned=parameters,
            papers=papers,
            current_paper_id=current_paper,
        )
        course = normalize_course(intent.course) if intent.course else None
        if course is None:
            # 学生说“这张卷/继续”时不会重复课程名：沿用已确认课程，或按当前
            # 试卷所属课程补齐，避免工具因为没课程而要求重复信息。
            course = confirmed or self._course_for_paper(current_paper)
        # 明确换了课程（例如从 Python 改问高数）时，不能带着旧试卷 ID。
        if course and intent.paper_id and self._course_for_paper(intent.paper_id) not in (None, course):
            intent = replace(intent, paper_id=None)
        if course and not intent.paper_id and current_paper:
            if self._course_for_paper(current_paper) not in (None, course):
                current_paper = None
                ctx.question_context = {}
        query = re.sub(
            r"(来|找|出|给|查|搜)(一道|一题|道题|个题)|一道|一题|真题|题目|讲讲|讲解|分析|练练|练习|一下",
            " ",
            intent.query,
        )
        query = re.sub(r"\s+", " ", query).strip()[:120]
        # “直接讲解”只由学生的话决定：模型可以建议，但不能替学生开启讲解。
        auto = bool(parameters.get("auto_explain") or action.parameters.get("auto_explain"))
        auto = auto and self._wants_explanation(text)
        intent = replace(intent, course=course, query=query, auto_explain=auto)
        return self._apply_page_position(text, intent, context, ctx)

    def _apply_page_position(
        self,
        text: str,
        intent: SearchIntent,
        context: dict[str, Any],
        ctx: _DynamicContext,
    ) -> SearchIntent:
        """把“继续/下一批/换一道”落到正确位置，并绑定筛选条件。

        续页位置只对同一 (试卷, 题型, 无关键词) 的浏览成立：换了题型
        （例如看完选择题又问填空题）就从第 1 道重新开始，不沿用旧位置；
        已经列完时标记 ``page_exhausted``，而不是回到第 1 道重放。
        """

        wants_next = self._wants_continuation(text) or wants_next_card(text)
        if intent.next_card is False and wants_next_card(text):
            intent = replace(intent, next_card=True)
        if not wants_next or intent.number_label is not None:
            return intent
        if not _page_topic_words(text):
            # 这一轮没有新的内容约束（没说“考循环的选择题”），只是翻页：
            # 不能把“继续/下一批/换一道”当成检索关键词，否则会变成相关度检索。
            intent = replace(intent, query="")
        if intent.next_card:
            # “换一道”是换题不是换条件：沿用上一次的关键词与题型，
            # 再排除已经展示过的题。
            if not _page_topic_words(text):
                last = self._last_search_filter(ctx)
                if last.get("query") and not intent.query:
                    intent = replace(intent, query=str(last["query"]))
                if last.get("question_type") and intent.question_type is None:
                    intent = replace(intent, question_type=last["question_type"])
            if intent.paper_id is None:
                # 没有锁定试卷时要先有一小池候选，才能排除看过的、给出新题。
                intent = replace(intent, limit=max(intent.limit, 10))
        if (
            intent.question_type is None
            and context.get("paper_id")
            and context["paper_id"] == intent.paper_id
            and context.get("question_type")
        ):
            # 只说“继续”时沿用上次的题型，否则会被当成整卷而要重列一遍。
            intent = replace(intent, question_type=context["question_type"])
        if not self._same_page_filter(context, intent):
            return intent
        if intent.offset:
            return intent
        next_offset = context.get("next_offset")
        if next_offset is None:
            return replace(intent, page_exhausted=True)
        limited = intent.limit
        if context.get("limit"):
            # 沿用上一页页宽，避免“前五题”之后突然变成整卷。
            limited = min(limited, int(context["limit"])) if limited else int(context["limit"])
        return replace(intent, offset=int(next_offset), limit=limited)

    @staticmethod
    def _same_page_filter(context: dict[str, Any], intent: SearchIntent) -> bool:
        """续页只能在同一个筛选条件下成立。"""

        if not context.get("paper_id") or context["paper_id"] != intent.paper_id:
            return False
        if intent.query:
            return False
        stored_kind = context.get("question_type") or None
        return stored_kind == (intent.question_type or None)

    @staticmethod
    def _refers_to_current_paper(text: str) -> bool:
        """学生的话是否指向刚刚那份卷（而不是另起一个检索条件）。"""

        return bool(
            re.search(
                r"这张|这份|这卷|该卷|本卷|刚才|刚刚|上面|之前那|继续|下一批|下一页|剩下的|"
                r"第\s*[0-9一二三四五六七八九十]+\s*(?:道|个)?题|这道|这个题",
                text or "",
            )
        )

    @staticmethod
    def _wants_continuation(text: str) -> bool:
        return bool(
            re.search(r"继续|接着|下一批|下一页|下一部分|剩下的|其余的|后面那些|还没(看|显示|列)", text or "")
        )

    def _course_for_paper(self, paper_id: str | None) -> str | None:
        """按试卷反查课程；目录不可用时返回 None。"""

        if not paper_id or self._papers is None:
            return None
        try:
            paper = self._papers.get_paper(paper_id)
        except Exception:
            logger.warning("按试卷反查课程失败", exc_info=True)
            return None
        return normalize_course(paper.course) if paper is not None else None

    @staticmethod
    def _wants_explanation(text: str) -> bool:
        """学生是否在本轮要求讲解、问考点或问题意。"""

        return bool(
            re.search(
                r"讲|解释|说明|分析|啥意思|什么意思|考(点|啥|什么|哪个)|怎么做|为什么|"
                r"思路|步骤|看不懂|看不明白|不会|错在|对不对|选哪个|哪个选项",
                text or "",
            )
        )

    @staticmethod
    def _wants_to_try_first(text: str) -> bool:
        """学生是否明确要求先自己做、暂不给答案。

        这是唯一保留“先作答”的合法理由；不能拿“这是真题请求”当理由。
        """

        return bool(
            re.search(
                r"先自己做|自己做|别给(我)?答案|不要答案|先不给答案|先别说答案|"
                r"不公布答案|先不(要)?讲解|先别讲|只看题|先看题|先自己(想|写|做)|自己想|"
                r"只给提示|只提示|给点提示|提示一下|先别公布|别公布|不要公布",
                text or "",
            )
        )

    @staticmethod
    def _wants_hint_only(text: str) -> bool:
        """学生只要提示、不要完整答案。"""

        return bool(
            re.search(r"只给提示|只提示|给点提示|给个提示|提示一下|稍作提示|稍微提示", text or "")
        )

    @classmethod
    def _answer_scope(cls, text: str) -> str | None:
        """本轮回答范围：``hint``（只要提示）、``topic``（只问考点）。

        两者都**不是**“完整讲解”授权，因此不能执行讲解或看答案。学生同时
        要求讲解/答案时返回 ``None``，按完整讲解处理。
        """

        value = text or ""
        if cls._wants_hint_only(value):
            return "hint"
        # 判断“解释/讲”的对象与范围：说的是“考什么/考点/知识点”，就只是考点，
        # 不能因为出现“解释”两个字就当成完整讲解。按小句判断，避免“讲讲…的
        # 考点和解题步骤”被整句吞掉。
        topic_object = re.compile(
            r"考点|知识点|考查(什么|点|哪些|哪个)"
            r"|考[了的]?(什么|啥|哪些|哪个|哪几个)"
            r"|考核?[^。；，,！？]{0,3}(什么|啥|哪些|哪个|哪几个)"
            r"|查[^。；，,！？]{0,3}(什么|啥|哪个|哪些)"
        )
        if not topic_object.search(value):
            return None
        # 问“这段代码/解题过程考什么”时，代码和过程是提问对象，不是
        # 请求的输出。完整解答须由独立请求或明确的解答动作来授权。
        full_marker = re.compile(
            r"步骤|答案|解答|过程|代码|完整|怎么做|为什么|解析|推导|思路|例子|举例"
        )
        solution_request = re.compile(
            r"(?:给出|展示|写出|补全|实现|解释|讲解|分析).{0,8}"
            r"(?:答案|解答|解法|步骤|解题过程|参考代码|完整代码)"
            r"|(?:代码|程序)(?:怎么写|如何写)|为什么(?:选|答案|结果)"
        )
        clause_marker = re.compile(r"讲|解释|说明|分析|说说|聊聊")
        has_topic = False
        wants_more = False
        for clause in re.split(r"[，,。；;！!？?\s]+|和|以及|与|并|然后|再|顺便|另外", value):
            # “不用给答案/不必讲步骤”是限制，不是要求。
            clause = re.sub(r"(不用|不需要|不必|不要|别|无需)[^。；，,]{0,8}", " ", clause)
            if not clause.strip():
                continue
            if topic_object.search(clause):
                has_topic = True
                if solution_request.search(clause):
                    wants_more = True
            elif clause_marker.search(clause) or full_marker.search(clause):
                wants_more = True
        return "topic" if has_topic and not wants_more else None

    @staticmethod
    def _mentions_list_position(text: str) -> bool:
        """学生是否在说“列表里的第 N 个”，而不是原卷题号。"""

        return bool(
            re.search(
                r"(列表|上面|其中|这些|那几个|这几道|这几个|结果|候选)[^。；，]{0,6}"
                r"第?\s*[0-9一二三四五六七八九十]+\s*(个|项|条|道题?)",
                text or "",
            )
            or re.search(r"第\s*[0-9]+\s*(个|项|条)", text or "")
        )

    @staticmethod
    def _same_number(label: str | None, number: int) -> bool:
        """``"05"`` 与 ``5`` 视为同一原卷题号。"""

        raw = str(label or "").strip()
        return raw.isdigit() and int(raw) == number

    def _question_context(self, ctx: _DynamicContext, *, trace: bool = False) -> dict[str, Any]:
        """本轮已确定的试卷/位置；本轮没有就用库里最近一次成功的记录。"""

        if ctx.question_context:
            return dict(ctx.question_context)
        try:
            stored = self._repository.find_question_context(ctx.conversation_id)
        except Exception:  # pragma: no cover - 读取失败不影响本轮
            logger.warning("读取试卷上下文失败", exc_info=True)
            stored = None
        if not stored:
            # 旧会话没有 question_context：从历史题卡恢复“这是哪份卷”。
            stored = self._context_from_cards(ctx.conversation_id)
            if stored and trace:
                logger.debug("由历史题卡恢复试卷上下文：%s", stored.get("paper_id"))
        elif trace:
            logger.debug("沿用上一次试卷上下文：%s", stored.get("paper_id"))
        return dict(stored or {})

    def _context_from_cards(self, conversation_id: str) -> dict[str, Any] | None:
        """返修前的记录只有题卡：能唯一确定试卷时补出试卷上下文。"""

        if getattr(self, "_repository", None) is None:  # 隔离替身没有仓库
            return None
        turn = self._latest_question_turn(conversation_id)
        if turn is None or not turn.question_results:
            return None
        papers = {
            str(card.source.paper_id)
            for card in turn.question_results
            if card.source.paper_id
        }
        if len(papers) != 1:
            # 多份试卷混在一起时不猜，宁可让学生说明。
            return None
        paper_id = papers.pop()
        types = {card.question_type for card in turn.question_results}
        title = next(
            (card.source.paper_title for card in turn.question_results if card.source.paper_title),
            None,
        )
        shown = [card.question_id for card in turn.question_results]
        return {
            "paper_id": paper_id,
            "paper_title": title,
            "question_type": types.pop() if len(types) == 1 else None,
            "number_label": None,
            # 历史卡片只证明“已经展示过这么多”，不能证明总量或是否翻完；
            # total 留空，继续时从已展示条数往后查，由真实检索给出总数。
            "total": None,
            "offset": 0,
            "returned": len(shown),
            "next_offset": len(shown),
            "limit": len(shown),
            "request_id": turn.request_id,
            "shown_ids": shown,
            # 由历史题卡推断出来的上下文：只有学生的话指向“这份卷”时才沿用，
            # 否则一次普通关键词检索会被旧试卷绑住。
            "derived": True,
        }

    def _question_candidates(
        self, ctx: _DynamicContext
    ) -> tuple[list[QuestionCard], str | None]:
        if ctx.question_results and ctx.question_source_turn:
            return list(ctx.question_results), ctx.question_source_turn
        turn = self._latest_question_turn(ctx.conversation_id)
        if turn is None:
            return [], None
        ctx.question_results = list(turn.question_results)
        ctx.question_source_turn = turn.request_id
        return list(turn.question_results), turn.request_id

    def _latest_question_turn(self, conversation_id: str) -> Turn | None:
        for turn in reversed(self._repository.list_turns(conversation_id)):
            if turn.question_results:
                return turn
        return None

    def _select_candidate(
        self,
        conversation_id: str,
        request_id: str,
        source_turn: str,
        card: QuestionCard,
    ) -> QuestionSelection:
        verified = self._resolve_question_card(
            conversation_id, source_turn, card.question_id
        )
        return self._select_question_card(
            conversation_id, request_id, source_turn, verified
        )

    @staticmethod
    def _target_ambiguity(cards: list[QuestionCard]) -> str:
        if len({card.source.paper_id for card in cards}) > 1:
            return "这个题号对应多份试卷，请说明要哪一份试卷。"
        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        labels = list(dict.fromkeys(kinds.get(card.question_type, "题目") for card in cards))
        if len(labels) > 1:
            return "这份试卷的" + "、".join(labels) + "都有这个题号，请说明题型。"
        return "这个题号对应多个来源或版本，请重新查题并指定具体题目。"

    def _adopt_question_target(
        self, prepared: ExecuteTurn, ctx: _DynamicContext,
        selection: QuestionSelection, card: QuestionCard,
    ) -> None:
        previous = self._question_context(ctx)
        ctx.question_selection = selection
        ctx.question_card = card
        ctx.question_result_status = "ok"
        ctx.question_result_message = ""
        if previous.get("paper_id") != card.source.paper_id:
            # 选中一题不代表已经浏览完该卷。新卷的浏览范围尚未知，
            # “继续列题”应从第 0 条真实读取，而不是沿用旧卷游标。
            ctx.question_context = {
                "paper_id": card.source.paper_id,
                "paper_title": card.source.paper_title,
                "question_type": card.question_type,
                "number_label": card.number_label,
                "total": None, "offset": 0, "returned": 0,
                "next_offset": 0, "limit": 64, "query": "",
                "request_id": prepared.request_id,
            }
        course = self._course_for_paper(card.source.paper_id)
        if course:
            ctx.confirmed_course = course

    async def _resolve_question_selection(
        self, prepared: ExecuteTurn, ctx: _DynamicContext,
        parameters: dict[str, Any] | None = None, *, use_active: bool = False,
    ) -> tuple[QuestionSelection | None, str | None]:
        """选题与讲解共用：本轮试卷约束 → 题型/题号 → 唯一性 → 来源校验。

        先解析本轮明确目标，再查看旧卡片；旧卡恰好同号不能覆盖换卷要求。
        列表位置只在位置请求中使用，题号匹配始终要求唯一。
        """
        parameters = dict(parameters or {})
        text = prepared.input.text or ""
        cards, source_turn = self._question_candidates(ctx)

        def unresolved(message: str) -> tuple[None, str]:
            # 澄清轮没有新查题时，沿用旧卡片的显示位置即可，不能把旧结果
            # 再保存成一批“本轮新找到的题”，否则页面会重复展示整套卡片。
            if source_turn != prepared.request_id:
                ctx.question_results = []
                ctx.question_card = None
            return None, message

        intent = self._search_intent(
            text, ctx, ActionPlan(action="search_questions", parameters=parameters)
        )
        positional = self._mentions_list_position(text)
        if intent.paper_unresolved and not positional:
            return unresolved("我无法从这些条件唯一确定一份试卷，请补充年份、学期或考试类型；不会改选旧试卷。")

        paper_id = intent.paper_id
        wanted_type = detect_question_type(text) or parameters.get("question_type")
        number = (
            int(intent.number_label) if intent.number_label is not None
            else self._requested_number(parameters, text)
        )
        position = None
        if positional or number is None:
            raw_position = number if positional and number is not None else parameters.get("index")
            if raw_position is not None:
                try:
                    position = int(raw_position)
                except (TypeError, ValueError):
                    return unresolved("请说明列表中的有效位置。")
            if positional:
                number = None

        def matches(card: QuestionCard) -> bool:
            return (
                (not paper_id or card.source.paper_id == paper_id)
                and (not wanted_type or card.question_type == wanted_type)
                and (
                    not intent.course
                    or self._course_for_paper(card.source.paper_id) in (None, intent.course)
                )
            )

        if position is not None:
            if not 1 <= position <= len(cards):
                return unresolved(f"列表里没有第 {position} 个题目，请确认位置。")
            chosen = cards[position - 1]
            if not matches(chosen):
                return unresolved("该列表位置的题目不属于本轮指定的试卷或题型，请按目标试卷重新查题。")
            candidates = [chosen]
        else:
            candidates = [
                card for card in cards
                if matches(card) and (number is None or self._same_number(card.number_label, number))
            ]

        if number is None and position is None and use_active:
            active = ctx.question_selection or self._repository.get_active_question(ctx.conversation_id)
            if active is not None:
                active_card = self.questions.card_for_selection(active)
                if matches(active_card):
                    self._adopt_question_target(prepared, ctx, active, active_card)
                    return active, None

        if len(candidates) > 1:
            return unresolved(self._target_ambiguity(candidates))

        # 当前页没有目标，按本轮已解析的卷与题号查找。题型未指定且当前
        # 浏览上下文也未限定时，查事实库确认唯一性，不能由局部页推断。
        context = self._question_context(ctx)
        needs_lookup = number is not None and paper_id and (
            not candidates or (wanted_type is None and not context.get("question_type"))
        )
        if needs_lookup:
            located, problem = await self._locate_question_by_number(
                ctx, number, wanted_type, paper_id=paper_id
            )
            if located is None:
                return unresolved(problem)
            try:
                selection = self._select_located_card(prepared, ctx, located)
            except QuestionError as exc:
                return unresolved(exc.message)
            self._adopt_question_target(prepared, ctx, selection, located)
            return selection, None

        if not candidates:
            if number is not None:
                return unresolved(f"没有找到符合本轮试卷、题型和题号条件的第 {number} 题，请确认目标。")
            return unresolved("请先查找并说明要选哪一道题。")
        if source_turn is None:
            return unresolved("找不到这次题目的来源记录，请重新查题。")
        chosen = candidates[0]
        try:
            if source_turn == prepared.request_id:
                # 本轮刚查到的卡尚未写入最终轮次，不能按“历史轮次卡片”读取。
                # 仍核验审核/build/哈希，并保留本轮已展示的完整结果集合。
                current_results = ctx.question_results
                selection = self._select_located_card(prepared, ctx, chosen)
                ctx.question_results = current_results
            else:
                selection = self._select_candidate(
                    prepared.conversation_id, prepared.request_id, source_turn, chosen
                )
        except QuestionError as exc:
            return unresolved(exc.message)
        self._adopt_question_target(prepared, ctx, selection, chosen)
        return selection, None

    async def _dynamic_select_question(
        self, prepared: ExecuteTurn, action: ActionPlan, ctx: _DynamicContext,
    ) -> ActionOutcome:
        selection, problem = await self._resolve_question_selection(prepared, ctx, action.parameters)
        if selection is None:
            return self._action_result(prepared, ctx, ActionOutcome(
                "select_question", "clarification", problem or "请说明要选哪一道题。",
                needs_user=True,
            ))
        card = ctx.question_card
        assert card is not None
        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        return self._action_result(prepared, ctx, ActionOutcome(
            "select_question", "success",
            f"已选中第 {card.number_label} 题（{kinds.get(card.question_type, '题目')}）。",
            {
                "question_id": card.question_id,
                "number_label": card.number_label,
                "question_type": card.question_type,
                "paper_id": card.source.paper_id,
                "paper_title": card.source.paper_title,
            },
            needs_user=False,
        ))

    @staticmethod
    def _requested_number(parameters: dict[str, Any], text: str) -> int | None:
        # “第五道选择题”与“第5题”都是原卷题号；本轮原话优先于模型参数。
        match = re.search(
            r"第\s*([0-9]+|[一二三四五六七八九十]+)\s*(?:道|个)?\s*"
            r"(?:(?:选择|单选|多选|填空|编程|代码|程序设计)题|小题|题)", text or "",
        )
        if match:
            raw = match.group(1)
            if raw.isdigit():
                return int(raw)
            digits = {char: number for number, char in enumerate("零一二三四五六七八九")}
            if "十" in raw:
                left, right = raw.split("十", 1)
                if (not left or left in digits) and (not right or right in digits):
                    return digits.get(left, 1) * 10 + digits.get(right, 0)
            if raw in digits:
                return digits[raw]
        raw = parameters.get("number_label")
        if isinstance(raw, str):
            match = re.search(r"([0-9]+)", raw)
            if match:
                return int(match.group(1))
        return candidate_number(text)

    async def _locate_question_by_number(
        self,
        ctx: _DynamicContext,
        number: int,
        question_type: str | None,
        *, paper_id: str | None = None,
    ) -> tuple[QuestionCard | None, str | None]:
        """按原卷题号在已确定的那份试卷里定位一道题（与当前页无关）。"""

        context = self._question_context(ctx)
        paper_id = paper_id or context.get("paper_id")
        if not paper_id:
            return None, "现在还没有确定的试卷；请先说明是哪份卷，我再按题号定位。"
        course = (
            self._course_for_paper(paper_id)
            or ctx.confirmed_course
            or "Python语言程序设计"
        )
        request = SearchRequest(
            course=course,
            query="",
            paper_id=paper_id,
            number_label=str(number),
            question_type=question_type,  # type: ignore[arg-type]
            limit=3,
        )
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.questions.search,
                    request,
                    conversation_id=ctx.conversation_id,
                    request_id=None,
                ),
                timeout=20.0,
            )
        except asyncio.TimeoutError:
            return None, "按题号定位超时，请稍后再试。"
        except QuestionError as exc:
            return None, exc.message
        if not result.cards:
            return None, f"这份试卷里没有第 {number} 题，请确认题号。"
        if len(result.cards) > 1:
            kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
            types = "、".join(
                dict.fromkeys(kinds.get(card.question_type, "题目") for card in result.cards)
            )
            return None, f"这份试卷的{types}都有第 {number} 题，请说明要哪一种题型。"
        # 只做定位，不改本轮状态：选中与否由调用方决定并落库。
        return result.cards[0], None

    def _select_located_card(
        self, prepared: ExecuteTurn, ctx: _DynamicContext, card: QuestionCard
    ) -> QuestionSelection:
        """选中本轮刚定位到的题：按来源/审核/哈希校验后落库。"""

        if not card.build_id or not card.content_sha256:
            raise QuestionSourceChangedError("题目来源信息不完整，请重新查题。")
        if self.questions.build_id and card.build_id != self.questions.build_id:
            raise QuestionSourceChangedError("题目库已更新，请重新查题后再选。")
        if not self.questions.is_approved(card.question_id):
            raise QuestionSelectionInvalidError("这道题尚未通过审核，暂不能使用。")
        if not self.questions.verify_card(card):
            raise QuestionSourceChangedError("题目内容已变化，请重新查题。")
        ctx.question_results = [card]
        ctx.question_source_turn = prepared.request_id
        return self._select_question_card(
            prepared.conversation_id,
            prepared.request_id,
            prepared.request_id,
            card,
        )

    async def _dynamic_list_paper_questions(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        """整卷题型浏览：给总数、本次范围和下一批位置。"""

        name = "list_paper_questions"
        emit("searching_questions", "正在读取这份试卷的题目")
        intent = self._search_intent(prepared.input.text, ctx, action)
        context = self._question_context(ctx)
        if not intent.course:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "请先告诉我课程，我再按整卷题型列给你。",
                needs_user=True,
            ))
        if intent.paper_id is None:
            options = self._paper_options(intent.course)
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "要列哪一份试卷的题目？" + (f"目前有：{options}。" if options else ""),
                needs_user=True,
            ))
        wanted = action.parameters.get("question_type")
        if wanted and not intent.question_type:
            intent = replace(intent, question_type=wanted)
        if intent.page_exhausted:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "clarification", self._exhausted_text(ctx, intent), needs_user=True
            ))
        intent = replace(intent, query="")
        result = await self._run_question_search(prepared, ctx, name, intent, emit)
        if isinstance(result, ActionOutcome):
            return result
        return self._question_search_outcome(prepared, ctx, name, intent, result)

    def _paper_options(self, course: str | None) -> str:
        if self._papers is None:
            return ""
        try:
            items = self._papers.list_papers(course=course or "Python语言程序设计").items
        except Exception:
            logger.warning("读取试卷目录失败", exc_info=True)
            return ""
        return "、".join(f"《{item.title}》" for item in items[:5])

    async def _dynamic_view_question_answer(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "view_question_answer"
        selection = ctx.question_selection or self._repository.get_active_question(
            prepared.conversation_id
        )
        if selection is None:
            cards, source_turn = self._question_candidates(ctx)
            if len(cards) == 1 and source_turn is not None:
                try:
                    selection = self._select_candidate(
                        prepared.conversation_id,
                        prepared.request_id,
                        source_turn,
                        cards[0],
                    )
                except QuestionError as exc:
                    return self._action_result(prepared, ctx, ActionOutcome(
                        name, "failed", exc.message, error_code=exc.code,
                        needs_user=True,
                    ))
            else:
                return self._action_result(prepared, ctx, ActionOutcome(
                    name,
                    "clarification",
                    "请先选中一道真题，再查看答案。",
                    needs_user=True,
                ))
        emit("viewing_question", "正在读取原卷答案")
        try:
            selection = (
                self._repository.reveal_question_answer(
                    prepared.conversation_id,
                    selection.question_id,
                    source="view",
                    selection_id=selection.id,
                )
                or selection
            )
            card, evidence = self.questions.answer_view(selection)
        except QuestionError as exc:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "failed", exc.message, error_code=exc.code,
                needs_user=True,
            ))
        ctx.question_selection = selection
        ctx.question_card = card
        if evidence.status in ("source_changed", "source_pruned"):
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                evidence.message or "来源已变化，暂时不能显示答案。",
                needs_user=True,
            ))
        message = evidence.message or "已显示原卷参考答案。"
        ctx.question_result_status = "ok"
        ctx.question_result_message = message
        return self._action_result(prepared, ctx, ActionOutcome(
            name,
            "success",
            message,
            {"question_id": selection.question_id, "answer_state": card.answer.state},
        ))

    async def _dynamic_explain_selection(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        selection: QuestionSelection,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        emit("explaining", "正在结合原卷材料生成分步讲解")
        try:
            payload, problem, question = await self._explain_selection(
                prepared, selection
            )
        except QuestionError as exc:
            return self._action_result(prepared, ctx, ActionOutcome(
                "explain_problem", "failed", exc.message, error_code=exc.code,
                retryable=exc.status_code == 503,
            ))
        ctx.active_problem = problem
        ctx.problem_id = problem.id
        ctx.active_exercise = None
        ctx.exercise_id = None
        ctx.tutoring = payload
        ctx.question_selection = (
            self._repository.get_question_selection_by_id(
                prepared.conversation_id, selection.id
            )
            or selection
        )
        ctx.question_card = question
        ctx.question_result_status = "ok"
        ctx.question_result_message = ""
        return self._action_result(prepared, ctx, ActionOutcome(
            "explain_problem",
            "success",
            "已结合原卷材料完成题目讲解",
            {"problem_id": problem.id, "question_id": question.question_id},
        ))

    async def _resolve_real_question_target(
        self, prepared: ExecuteTurn, ctx: _DynamicContext
    ) -> tuple[QuestionSelection | None, str | None]:
        """讲解入口采用与选题相同的目标解析，不自行匹配第一张同号卡片。"""
        if prepared.input.attachments or prepared.input.kind in ("problem", "supplement", "correction"):
            return None, None
        text = prepared.input.text or ""
        cards, _ = self._question_candidates(ctx)
        selection = ctx.question_selection or self._repository.get_active_question(ctx.conversation_id)
        if not cards and selection is None:
            return None, None
        targetish = (
            mentions_question_target(text) or not text.strip()
            or bool(re.search(r"第\s*[0-9一二三四五六七八九十]+", text))
            or (ctx.question_result_status == "ok" and not looks_like_new_problem(text))
        )
        if not targetish:
            return None, None
        return await self._resolve_question_selection(prepared, ctx, use_active=True)

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

        real_selection, ambiguity = await self._resolve_real_question_target(prepared, ctx)
        if ambiguity:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "clarification", ambiguity, needs_user=True
            ))
        if real_selection is not None:
            return await self._dynamic_explain_selection(
                prepared, ctx, real_selection, emit
            )

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

    def _question_operation_problem(self, ctx: _DynamicContext) -> ProblemRecord | None:
        """自然语言和卡片动作都跟随持久选题；未讲解时绝不退回旧题。"""
        selection = self._repository.get_active_question(ctx.conversation_id)
        ctx.question_selection = selection
        if selection is None:
            ctx.question_card = None
            return ctx.active_problem
        ctx.question_card = self.questions.card_for_selection(selection)
        problem = (
            self._repository.get_problem(selection.problem_id) if selection.problem_id else None
        )
        if problem is not None and problem.conversation_id != ctx.conversation_id:
            problem = None
        ctx.active_problem = problem
        ctx.problem_id = problem.id if problem is not None else None
        return problem

    async def _dynamic_followup(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "followup_problem"
        active = self._question_operation_problem(ctx)
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
        statement = active.statement_text
        if (
            ctx.question_selection is not None
            and ctx.question_card is not None
            and ctx.question_selection.problem_id == active.id
        ):
            statement = compose_statement(ctx.question_card)
        answer = await tutor.answer_followup(
            statement=statement,
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
        active = self._question_operation_problem(ctx)
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
        from_question = (
            ctx.question_selection is not None
            and ctx.question_card is not None
            and ctx.question_selection.problem_id == active.id
        )
        original_statement = (
            compose_statement(ctx.question_card)
            if from_question and ctx.question_card is not None
            else active.statement_text
        )
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
                original_statement=original_statement,
                knowledge_points=knowledge_points,
                steps_summary=steps_summary,
                course=ctx.confirmed_course,
                recent_statements=recent,
                avoid_statements=avoid or None,
            )
            if not is_duplicate_statement(
                candidate.statement,
                recent,
                original=original_statement,
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
            if from_question and ctx.question_card is not None:
                self._repository.save_exercise_question_source(
                    exercise_id=exercise.id,
                    card=ctx.question_card,
                    build_id=ctx.question_card.build_id,
                    source_version=self.questions.source_version or "",
                )
            ctx.exercise_view = self._build_exercise_view(exercise)
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "success",
                "已根据真题生成针对性练习题（待作答）"
                if from_question
                else "已生成针对性练习题（待作答）",
                {
                    "exercise_id": exercise.id,
                    "knowledge_point": generated.knowledge_point,
                    "from_question": from_question,
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
        active = self._question_operation_problem(ctx)
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
            if (
                ctx.question_selection is not None
                and ctx.question_card is not None
                and ctx.question_selection.problem_id == active.id
            ):
                self._repository.save_record_question_source(
                    record_id=detail.id,
                    card=ctx.question_card,
                    build_id=ctx.question_card.build_id,
                    source_version=self.questions.source_version or "",
                )
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

        if data.question_action is not None:
            if kind is not None:
                raise TurnInvalidError("真题动作只能用于自动轮次。")
            if not data.question_id or not data.question_source_turn:
                raise TurnInvalidError("真题动作必须指定题目和查题轮次。")
            if (
                data.attachments
                or data.student_answer_text
                or data.solution_text
                or data.source.type != "none"
                or data.problem_id is not None
                or data.start_new
            ):
                raise TurnInvalidError("真题动作不能携带作答、图片或另一个题目引用。")
        elif kind in _QUESTION_KINDS:
            if not data.question_id or not data.question_source_turn:
                raise TurnInvalidError("使用真题轮次时必须指定题目和查题轮次。")
            if data.attachments or data.solution_text or (
                data.student_answer_text and kind != "explain_question"
            ):
                raise TurnInvalidError("仅真题讲解可以携带所选答案；不支持附件或解题过程。")
            if data.source.type != "none":
                raise TurnInvalidError("真题轮次不能另外指定题目来源。")
            if data.problem_id is not None or data.start_new:
                raise TurnInvalidError("真题轮次不能携带题目引用或新题标记。")
        elif data.question_id is not None or data.question_source_turn is not None:
            raise TurnInvalidError("只有真题轮次可以携带题目引用。")

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
        if data.question_id is not None:
            payload["question_id"] = data.question_id
        if data.question_source_turn is not None:
            payload["question_source_turn"] = data.question_source_turn
        if data.question_action is not None:
            payload["question_action"] = data.question_action
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


def _question_suggestions(card: QuestionCard) -> list[SuggestionItem]:
    return [
        SuggestionItem(label="讲解这道题", action="explain_question", target_id=card.question_id),
        SuggestionItem(label="查看参考答案", action="view_question_answer", target_id=card.question_id),
        SuggestionItem(label="生成一道变式", action="exercise", target_id=card.question_id),
        SuggestionItem(label="保存到错题本", action="record", target_id=card.question_id),
    ]


def _rejections_fixed_later(observations: list[dict[str, Any]]) -> set[int]:
    """找出“先被拒绝、后来同一工具执行成功”的观测量下标。

    这些拒绝要留在上下文里让模型知道刚才错在哪，但不能进入业务结果：
    学生看到“部分完成、可重试”会去重试一个已经完成的动作。
    """

    resolved: set[int] = set()
    for index, observation in enumerate(observations):
        details = observation.get("details") or {}
        if not details.get("rejected"):
            continue
        tool = str(details.get("tool") or "")
        action = None
        if tool:
            spec = TOOLS_BY_NAME.get(tool)
            action = spec.action if spec is not None else None
        for later in observations[index + 1 :]:
            if later.get("status") != "success":
                continue
            later_details = later.get("details") or {}
            if (tool and later_details.get("tool") == tool) or (
                action and later.get("action") == action
            ):
                resolved.add(index)
                break
    return resolved


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
            reply_status=turn.reply_status,
            reply_retryable=turn.reply_retryable,
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
