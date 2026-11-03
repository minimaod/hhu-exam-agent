"""会话服务入口：组装依赖、公开方法、轮次执行入口与调度衔接。"""

import json
import logging
from collections.abc import (
    AsyncIterator,
    Callable,
)
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import Request

from app.conversations.context import DynamicContextMixin
from app.conversations.dispatch import DeepSeekIntentRouter
from app.conversations.errors import (
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationQueryUnavailableError,
    ConversationStorageError,
    ProblemNotFoundError,
    SourceInvalidError,
    TurnInvalidError,
    TutoringUnavailableError,
)
from app.conversations.explain import ExplainMixin
from app.conversations.inputs import (
    canonical_json,
    query_input,
)
from app.conversations.legacy import LegacyFlowMixin
from app.conversations.memory_handlers import MemoryHandlerMixin
from app.conversations.models import (
    ConversationContextView,
    ConversationDetail,
    ConversationListResponse,
    ConversationSummary,
    RecordRef,
    SuggestionItem,
    TurnErrorEvent,
    TurnEvent,
    reply_state as _reply_state,
)
from app.conversations.narration import NarrationMixin
from app.conversations.practice import PracticeMixin
from app.conversations.question_flow import QuestionFlowMixin
from app.conversations.reply import material_from_payload
from app.conversations.repository import (
    AttachmentRecord,
    ConversationRepository,
    ProblemRecord,
)
from app.conversations.review_loop import ReviewLoopMixin
from app.conversations.search import SearchMixin
from app.conversations.selection import SelectionMixin
from app.conversations.turn_types import (
    ConversationStoreState,
    ExecuteTurn,
    PreparedTurn,
    ReplayTurn,
    TurnInput,
    TurnInputRulesMixin,
    _DynamicContext,
    _QUESTION_KINDS,
    _RunState,
    _replay_event,
    _storage_error_event,
    build_store_state,
)
from app.images import ATTACHMENTS_ROOT
from app.mistakes.service import MistakeRecordService
from app.papers.service import PaperService
from app.querying.service import QueryService
from app.questions.errors import QuestionBankUnavailableError
from app.questions.runtime import (
    QuestionRuntime,
    bank_root_from_env,
)
from app.tutoring.models import Source
from app.tutoring.service import build_problem_view

logger = logging.getLogger("app.conversations.service")

def _is_dynamic_recovery(recovery: dict[str, Any] | None) -> bool:
    """动态轮次的恢复记录；F008 起版本号为 2，旧记录是 1，都必须能恢复。"""

    if not isinstance(recovery, dict):
        return False
    version = recovery.get("version")
    return isinstance(version, int) and version >= 1

class ConversationService(
    TurnInputRulesMixin,
    DynamicContextMixin,
    NarrationMixin,
    LegacyFlowMixin,
    QuestionFlowMixin,
    SearchMixin,
    SelectionMixin,
    ExplainMixin,
    PracticeMixin,
    MemoryHandlerMixin,
    ReviewLoopMixin,
):
    """会话服务入口；按职责混入上下文、正文、旧流程和各工具处理器。"""

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
        memory: Any | None = None,
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
        self._memory = memory
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
        notice = self._memory_notice_from_material(material)
        if notice and notice not in text:
            text = text + "\n\n" + notice

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
