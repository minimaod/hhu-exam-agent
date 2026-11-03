"""共用轮次类型、常量、事件构造与输入规则；不依赖任何 Mixin 或 service。"""

import json
import logging
import os
from contextlib import suppress
from dataclasses import (
    dataclass,
    field,
    replace,
)
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import Path
from typing import (
    Any,
    Literal,
)

from app import images
from app.conversations import repository as conversation_repository
from app.conversations.errors import (
    ConversationConflictError,
    ConversationStorageError,
    TurnInvalidError,
)
from app.conversations.inputs import (
    canonical_json,
    query_input,
)
from app.conversations.models import (
    ExerciseEvaluation,
    ExerciseView,
    Turn,
    TurnErrorEvent,
    TurnEvent,
    TurnKind,
    TurnResultEvent,
)
from app.conversations.repository import (
    ConversationRepository,
    ExerciseRecord,
    ProblemRecord,
)
from app.images import (
    PENDING_TTL_SECONDS,
    delete_attachment,
)
from app.querying.models import ResultEvent
from app.questions.models import (
    QuestionCard,
    QuestionSelection,
)
from app.tutoring.models import (
    Source,
    TutoringResult,
)

logger = logging.getLogger("app.conversations.service")

STORAGE_ERROR_CODE = "STORAGE_UNAVAILABLE"

_STORAGE_FALLBACK_MESSAGE = "本地学习数据暂时不可用，请稍后再试。"

_TUTORING_TIMEOUT_SECONDS = 125.0

_DISPATCH_TIMEOUT_SECONDS = 25.0

_DYNAMIC_TIMEOUT_SECONDS = 240.0

_TUTORING_MESSAGE = "这次辅导没有完成，请重试。"

_TIMEOUT_MESSAGE = "辅导超时，请重试。"

_DISPATCH_MESSAGE = "判断会话意图超时，请重试。"

_QUESTION_KINDS = ("select_question", "explain_question", "view_question_answer")

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
    # F009：本轮的学习记忆上下文。由程序统计后交给决策与正文；读取失败时
    # 用“暂时无法读取”如实说明，不能变成“没有记录”。
    memory_course: str | None = None
    memory_text: str | None = None
    memory_status: str | None = None
    memory_view: Any = None
    memory_entry_ids: set[str] = field(default_factory=set)
    # 本轮学生明确提到/操作的记忆条目：正文材料优先展开它们的附注与作答序列。
    memory_focus_entry_ids: set[str] = field(default_factory=set)
    recovery: dict[str, Any] = field(default_factory=dict)
    action_index: int = -1

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

class TurnInputRulesMixin:
    """输入规范化、重复判定与共用的静态校验；由入口与各处理器共同继承。"""

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

    @staticmethod
    def _same_number(label: str | None, number: int) -> bool:
        """``"05"`` 与 ``5`` 视为同一原卷题号。"""

        raw = str(label or "").strip()
        return raw.isdigit() and int(raw) == number

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
