"""调度与动态复习循环：动作执行、守卫、结果合成和失败降级。"""

import json
import logging
import re
import time
import uuid
from collections.abc import (
    AsyncIterator,
    Callable,
)
from contextlib import suppress
from typing import Any

from fastapi import Request

from app.conversations import answer_gate, final_reply, tool_projection
from app.conversations.authorization import (
    authorize_action,
    needs_user_for_denial,
)
from app.conversations.basis import validate_basis
from app.conversations.errors import (
    AttachmentInvalidError,
    ConversationConflictError,
    ConversationStorageError,
    InvalidImageError,
    ProblemNotFoundError,
    ProblemNotReadyError,
)
from app.conversations.graph import (
    ActionOutcome,
    ActionPlan,
    DynamicReviewGraph,
    ExecutionResult,
    FinalizeUnavailable,
    MAX_SCHEDULER_ROUNDS,
    _action_label,
)
from app.conversations.inputs import canonical_json
from app.conversations.history import project_step
from app.conversations.models import (
    ActionResultItem,
    RECOVERABLE_CODES,
    TurnErrorEvent,
    TurnEvent,
    TurnMessageDeltaEvent,
    TurnProgressEvent,
    TurnResultEvent,
    reply_state as _reply_state,
)
from app.conversations.reply import ReplyTruncated, material_to_payload
from app.conversations.read_tools import READ_ONLY_ACTIONS
from app.conversations.tools import (
    ACTION_TO_TOOL,
    TOOLS_BY_NAME,
    tool_label as _tool_label,
)
from app.conversations.workspace import workspace_text
from app.conversations.turn_types import (
    ExecuteTurn,
    TurnInputRulesMixin,
    _DYNAMIC_TIMEOUT_SECONDS,
    _DynamicContext,
    _RunState,
    _TIMEOUT_MESSAGE,
    _storage_error_event,
)
from app.memory.models import parse_entry_id
from app.querying.errors import QueryError
from app.questions.errors import QuestionError
from app.streaming import (
    SENTINEL,
    stream_events,
)
from app.tutoring.models import FeedbackResult

logger = logging.getLogger("app.conversations.service")

_RED_LINE_ACTIONS = frozenset({"generate_exercise", "record_mistake"})

# F010 D：这些动作会改学习数据；必须经执行层授权。答案动作不在其中，
# 由 C 的 answer_gate 负责。
_WRITE_ACTIONS = frozenset(
    {"record_mistake", "generate_exercise", "delete_memory", "correct_memory"}
)

# 只点名一份试卷时，查卷只负责定位；没有这些具体条件就不能顺手展开整卷。
_QUESTION_CONTENT_ACTIONS = frozenset({"list_paper_questions", "search_questions"})
_QUESTION_CONTENT_PARAMS = (
    "query",
    "question_type",
    "number_label",
    "knowledge_points",
    "offset",
    "limit",
    "number",
)

# 没有明确请求时，保存/出题通常只需追问“要不要做”；但如果学生已经拒绝，
# 或是在问“保存会怎样”这类元问题，就直接按失败回执，不再推销。
# 这些动作会改变工作区里可见的事实（试卷、浏览页、选中题、练习状态）。
_WORKSPACE_CHANGING_ACTIONS = frozenset(
    {
        "search_questions",
        "list_paper_questions",
        "query_papers",
        "select_question",
        "generate_exercise",
        "evaluate_exercise",
        "reveal_solution",
        "record_mistake",
    }
)

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
        action = str(observation.get("action") or "") or None
        if tool:
            spec = TOOLS_BY_NAME.get(tool)
            action = spec.action if spec is not None else action
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

class ReviewLoopMixin:
    """动态调度的一轮执行、动作守卫和结果合成。"""

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
                # 已成功的动作不再执行，只把它们的公开记录带进本轮 transcript。
                ctx.transcript = self._carried_steps(
                    recovery, prepared.input.retry_of or ""
                )
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
                ctx.transcript = []
                max_rounds = MAX_SCHEDULER_ROUNDS
            ctx.request_id = prepared.request_id
            ctx.input_text = prepared.input.text
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
            # F010 B：跨轮历史与工作区都在这里一次性读时派生；正文模型不读它们。
            ctx.history = self._history_for(
                prepared.conversation_id, prepared.request_id
            )
            ctx.workspace = {}
            ctx.ref_table = None
            ctx.workspace_text = workspace_text(self._workspace(ctx))
            memory_context = self._prepare_memory_context(prepared, ctx)

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
                scheduler = self._make_scheduler()
                graph = DynamicReviewGraph(scheduler, execute)
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
                    "memory_context": memory_context,
                    "last_suggestions": last_sugs,
                    "recent_turns_summary": recent_summary,
                    "history": ctx.history,
                    "workspace_text": ctx.workspace_text,
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
                ctx.answer_scope = answer_gate.scope_of(prepared.input.text)
            composite_results = carried + self._dynamic_composite_results(final_state)
            suggestions = self._dynamic_suggestions(final_state, composite_results)
            reason = final_reply.terminal_reason(final_state)
            fallback_text = self._dynamic_reply(final_state, composite_results, ctx)
            # 澄清轮的问题原文就是要说的话，不再调模型改写；其余轮次在同一份
            # 循环消息上生成终局正文。材料先落盘，客户端中断后可只重做正文。
            clarification = final_reply.clarification_question(final_state)
            material = self._build_reply_material(
                prepared, ctx, composite_results, suggestions, recent_summary
            )
            direct_clarification = material is None
            if direct_clarification:
                provisional_text = fallback_text or clarification or ""
                if clarification and clarification not in provisional_text:
                    provisional_text = (
                        provisional_text.rstrip() + "\n\n" + clarification
                    ).strip()
                provisional_text = provisional_text[:3000]
            else:
                provisional_text = fallback_text
            reply_context = final_reply.build_context(
                prepared=prepared,
                ctx=ctx,
                final_state=final_state,
                composite_results=composite_results,
                suggestions=suggestions,
                recent_summary=recent_summary,
                material=material,
                reason=reason,
                clarification=clarification,
            )
            if ctx.recovery is not None:
                ctx.recovery["reply_context"] = reply_context
                ctx.recovery["reply"] = {
                    "mode": "pending",
                    "reason": "reply_pending",
                    "terminal_reason": reason,
                    "deltas": 0,
                }
                self._repository.save_recovery(
                    prepared.conversation_id, prepared.request_id, ctx.recovery
                )
            # 历史轮次的题卡只是“上次看到哪”，不能冒充本轮结果：否则学生只查一次
            # 试卷，界面会把上一轮的整卷题卡再渲染一遍，正文还会照着历史标题
            # 编出“先列出第 1 到第 5 题”。
            own_questions = ctx.question_source_turn == prepared.request_id
            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="dynamic",
                result=ctx.result,
                tutoring=ctx.tutoring,
                exercise=ctx.exercise_view,
                evaluation=ctx.evaluation_view,
                composite_results=composite_results,
                assistant_message=provisional_text,
                suggestions=suggestions,
                question_results=ctx.question_results if own_questions else [],
                question_result_status=(
                    ctx.question_result_status if own_questions else None
                ),
                question_result_message=(
                    ctx.question_result_message if own_questions else None
                ),
                question=ctx.question_card,
                problem_id=ctx.problem_id,
                exercise_id=ctx.exercise_id,
                question_context=ctx.question_context or None,
                transcript=(
                    {"version": 1, "steps": ctx.transcript}
                    if ctx.transcript
                    else None
                ),
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
            # 工具决策阶段不转发正文；业务结果确定后才在同一份循环消息上
            # 终局流式生成最终回复（F010 C）。
            if direct_clarification:
                reply_text = provisional_text
                reply_meta = {"mode": "clarification", "reason": "needs_user", "deltas": 0}
            else:
                reply_text, reply_meta = await self._finalize_dynamic_reply(
                    prepared,
                    ctx,
                    final_state,
                    material,
                    composite_results=composite_results,
                    scheduler=scheduler,
                    emit=emit,
                    push=queue.put_nowait,
                    fallback_text=fallback_text,
                )
            reply_status: str | None = None
            reply_retryable = False
            # 轮次此刻已经是 completed，不能再走 save_recovery（它只接受运行中的
            # 轮次）；update_turn_reply 同时写正文、回复状态与材料，且没有状态守卫。
            self._repository.update_turn_reply(
                prepared.conversation_id,
                prepared.request_id,
                assistant_message=reply_text,
                reply_meta=reply_meta,
                reply_input=None,
            )
            reply_status, reply_retryable = _reply_state(
                {"reply": reply_meta, "reply_context": reply_context}
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

        own_questions = ctx.question_source_turn == prepared.request_id
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
                question_results=ctx.question_results if own_questions else [],
                question_result_status=(
                    ctx.question_result_status if own_questions else None  # type: ignore[arg-type]
                ),
                question_result_message=(
                    ctx.question_result_message if own_questions else None
                ),
                question=ctx.question_card,
                exercise_id=ctx.exercise_id,
                reply_status=reply_status,  # type: ignore[arg-type]
                reply_retryable=reply_retryable,
            )
        )
        queue.put_nowait(SENTINEL)

    def _action_result(self, prepared: ExecuteTurn, ctx: _DynamicContext, outcome: ActionOutcome) -> ActionOutcome:
        if ctx.action_index >= 0:
            ctx.recovery["actions"][ctx.action_index]["outcome"] = outcome.to_dict()
            ctx.recovery["context"] = self._context_snapshot(ctx)
            if outcome.needs_user and outcome.status != "failed":
                ctx.recovery["pending"] = []
            self._repository.save_recovery(prepared.conversation_id, prepared.request_id, ctx.recovery)
        return outcome

    # ------------------------------------------------------------------
    # F010 C：终局回复
    # ------------------------------------------------------------------
    @staticmethod
    def _finalize_note(ctx: _DynamicContext) -> str | None:
        notes: list[str] = []
        if ctx.answer_scope == "hint":
            notes.append("本轮学生只要提示：给出与题干相关的具体提示，不写完整解法或答案。")
        elif ctx.answer_scope == "topic":
            notes.append("本轮学生只问考点：说明考查的知识点及理由，不展开完整解法。")
        if ctx.tutoring is not None and getattr(ctx.tutoring, "needs_clarification", False):
            notes.append("先按辅导结果里的问题向学生澄清，不要替学生作答。")
        if ctx.evaluation_view is not None and getattr(
            ctx.evaluation_view, "needs_supplement", False
        ):
            notes.append("评价结果需要补充过程：请学生补写步骤，不要直接判对错。")
        return "；".join(notes) or None

    async def _finalize_dynamic_reply(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        final_state: dict[str, Any],
        material: Any,
        *,
        composite_results: list[ActionResultItem],
        scheduler: Any,
        emit: Callable[[str, str], None],
        push: Callable[[Any], None],
        fallback_text: str,
    ) -> tuple[str, dict[str, Any]]:
        """在业务提交之后生成最终正文；失败只影响正文，不影响业务结果。

        顺序：工具写 → complete_dynamic_turn → 本方法流式输出 → update_turn_reply。
        生产路径用调度器同一份循环消息；测试替身没有流式接口时退回回复适配器
        （同样是循环消息），再退回材料适配器（恢复路径）。
        """

        started = time.monotonic()
        pieces: list[str] = []
        first_delta_ms: int | None = None
        seq = 0

        def emit_delta(chunk: str) -> None:
            nonlocal seq
            if not chunk:
                return
            seq += 1
            pieces.append(chunk)
            push(
                TurnMessageDeltaEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    seq=seq,
                    delta=chunk[:400],
                )
            )

        def on_piece(piece: str) -> None:
            nonlocal first_delta_ms
            if not piece:
                return
            if first_delta_ms is None:
                first_delta_ms = int((time.monotonic() - started) * 1000)
            for start in range(0, len(piece), 400):
                emit_delta(piece[start : start + 400])

        emit("composing_reply", "正在整理回复")
        mode = "finalize"
        reason = final_reply.terminal_reason(final_state)
        failure_reason: str | None = None
        note = self._finalize_note(ctx)
        # 同一循环的消息里没有单独的“材料区”，学习记忆等跨轮文字上下文
        # 由材料补齐；工具结果与讲解仍只来自本轮真实消息。
        if material is not None:
            context_note = material.describe_context()
            note = "\n".join(part for part in (note, context_note) if part) or None
        # 纯选中确认是程序回执（不调模型、不流式），保持既有产品行为。
        selection_ack = self._selection_reply(material) if material is not None else None
        try:
            if selection_ack:
                emit_delta(selection_ack)
                mode = None  # 程序回执：没有调用回复模型
                reason = "selection_ack"
            else:
                await self._stream_final_messages(
                    scheduler, material, on_piece, note=note
                )
        except ReplyTruncated:
            mode = "truncated"
            reason = "length"
        except QueryError as exc:
            logger.warning("终局回复不可用：%s", exc.code)
            mode = "fallback" if not pieces else "incomplete"
            failure_reason = (
                "reply_not_configured"
                if exc.code == "MODEL_NOT_CONFIGURED"
                else "reply_failed"
            )
        except Exception:
            logger.warning("终局回复生成失败", exc_info=True)
            mode = "fallback" if not pieces else "incomplete"
            failure_reason = "reply_failed" if not pieces else "reply_interrupted"

        text = "".join(pieces)
        if not text.strip():
            # 模型没有产出任何文字：保留确定性说明，正文状态按兜底处理。
            text = fallback_text or "本轮操作已完成。"
            mode = "fallback"
            reason = failure_reason or "empty_reply"
        elif failure_reason:
            reason = failure_reason
        # 澄清问题与记忆说明是程序事实：模型换了说法也不能漏掉。
        extras: list[str] = []
        question = final_reply.clarification_question(final_state)
        if question and question not in text:
            extras.append("\n\n" + question)
        notice = self._memory_notice(composite_results)
        if notice and notice not in text and notice not in extras:
            extras.append("\n\n" + notice)
        for extra in extras:
            for start in range(0, len(extra), 400):
                emit_delta(extra[start : start + 400])
            text += extra
        if mode == "finalize":
            reason = reason or "model_finished"
        meta = {
            "mode": mode,
            "reason": reason,
            "deltas": seq,
            "finalize_first_delta_ms": first_delta_ms,
            "reply_total_ms": int((time.monotonic() - started) * 1000),
        }
        return text, meta

    async def _stream_final_messages(
        self,
        scheduler: Any,
        material: Any,
        on_piece: Callable[[str], None],
        *,
        note: str | None,
    ) -> None:
        messages = None
        finalize = getattr(scheduler, "finalize_stream", None)
        if finalize is not None:
            try:
                async for piece in finalize(note=note):
                    on_piece(piece)
            except FinalizeUnavailable:
                messages = getattr(scheduler, "conversation_messages", lambda: None)()
            else:
                # 原生流已经出站片段：达到输出上限时同样要标截断，允许只重做正文。
                if str(getattr(scheduler, "last_finish_reason", "") or "") == "length":
                    raise ReplyTruncated("终局正文达到输出上限")
                return
        else:
            messages = getattr(scheduler, "conversation_messages", lambda: None)()
        if messages:
            reply = self._make_reply() if self._make_reply is not None else None
            if reply is None or not hasattr(reply, "stream_messages"):
                raise QueryError("MODEL_NOT_CONFIGURED", "回复模型尚未配置。")
            async for piece in reply.stream_messages(list(messages), note=note):
                on_piece(piece)
            return
        if material is None:
            raise FinalizeUnavailable("没有可用的回复材料")
        reply = self._make_reply() if self._make_reply is not None else None
        if reply is None:
            raise QueryError("MODEL_NOT_CONFIGURED", "回复模型尚未配置。")
        async for piece in reply.stream(material):
            on_piece(piece)

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
            if details.get("gate_rejected"):
                # 答案门禁拒绝是模型的内部纠偏：既不是业务失败，也不该出现在
                # 学生看到的“以下操作未完成”里；保留在 transcript 供审计。
                continue
            if details.get("rejected"):
                # 协议层拒绝确实“没有执行”，不能因为消息不适合原样展示就删掉，
                # 否则全部调用被拒时会变成“已完成”。这里改成一条可读的失败记录。
                tool = str(details.get("tool") or "")
                action = str(observation.get("action") or "")
                if action:
                    label = _action_label(action)
                else:
                    action = f"tool:{tool}" if tool else "unknown_tool"
                    label = _tool_label(tool) if tool else "未知工具"
                items.append(
                    ActionResultItem(
                        action=action,
                        status="failed",
                        message=str(
                            observation.get("message")
                            or "有一次工具调用没有通过参数校验，因此没有执行。"
                        ),
                        details={
                            **details,
                            "rejected": True,
                            "retryable": True,
                            "label": label,
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

    def _dynamic_reply(
        self,
        final_state: dict[str, Any],
        composite_results: list[ActionResultItem],
        ctx: "_DynamicContext | None" = None,
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
        if ctx is not None and ctx.memory_view is not None:
            # 没有调用工具、只凭本轮记忆上下文作答：用程序统计好的事实兜底，
            # 不让空回复或“已为您处理完成”盖掉真实记录。
            view = ctx.memory_view
            parts = [view.range_text + "。"]
            if view.suggestions:
                parts.append("建议先补：" + "；".join(view.suggestions))
            elif view.counts.evidence == 0:
                parts.append(view.message)
            return "".join(parts)
        return "已为您处理完成。"

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
        if isinstance(requested_course, str) and requested_course.strip() and action.action not in READ_ONLY_ACTIONS:
            # 必须在动作内部查询前同步，否则 query_papers 仍会用旧课程。
            # 只读工具（尤其 view_learning_summary）不允许借 course 参数切换课程。
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
        if (
            outcome.status == "success"
            and ctx.answer_grant is not None
            and action.action in tool_projection.DISCLOSURE_ACTIONS
        ):
            # 本轮已授权的答案/讲解只随 tool 消息交给模型；to_dict() 不带它，
            # 所以 transcript、恢复记录、历史与 SSE 都拿不到正文。
            outcome.model_details = self._authorized_model_details(action.action, ctx)
        self._record_transcript(ctx, action, outcome)
        self._refresh_workspace(ctx, action)
        self._action_result(prepared, ctx, outcome)
        return ExecutionResult(outcome, self._dynamic_state_patch(ctx, review_state))

    def _gate_rejection(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        name: str,
        decision: answer_gate.GateDecision,
        *,
        facts: dict[str, Any] | None = None,
    ) -> ActionOutcome:
        """把门禁决定转成 role=tool 的拒绝信封（不含答案正文/URL）。"""

        details = {
            "rejected": True,
            "gate_rejected": True,
            "error_code": decision.code,
            "retryable": decision.retryable,
            "needs_user": decision.needs_user,
            "remedies": list(decision.remedies),
            "facts": dict(facts or decision.facts or {}),
        }
        return self._action_result(
            prepared,
            ctx,
            ActionOutcome(
                name,
                "failed",
                decision.message,
                details=details,
                needs_user=decision.needs_user,
                error_code=decision.code,
                retryable=decision.retryable,
            ),
        )

    def _authorized_model_details(
        self, action: str, ctx: _DynamicContext
    ) -> dict[str, Any]:
        kind = {
            "view_question_answer": "question_answer",
            "explain_problem": "explanation",
            "tutoring": "explanation",
            "followup_problem": "followup_answer",
            "followup": "followup_answer",
            "reveal_solution": "exercise_solution",
            "evaluate_exercise": "evaluation",
        }.get(action, "question_answer")
        text = ctx.authorized_text or self._teaching_material(ctx)
        if text is None:
            return {}
        return tool_projection.authorized_facts(
            kind=kind, grant=ctx.answer_grant, text=text
        )

    def _carried_steps(
        self, recovery: dict[str, Any], retry_of: str
    ) -> list[dict[str, Any]]:
        """把恢复记录里已成功的动作转成本轮 transcript 的 carried 步骤。

        这些动作**不会再次执行**：它们只是让新轮次的历史与审计能看到
        “之前已经做过什么”，因此不会产生重复写入。
        """

        steps: list[dict[str, Any]] = []
        for index, entry in enumerate(recovery.get("actions") or [], start=1):
            if not isinstance(entry, dict):
                continue
            plan = entry.get("plan") if isinstance(entry.get("plan"), dict) else {}
            action = str(plan.get("action") or "")
            if not action:
                continue
            outcome = (
                entry.get("outcome") if isinstance(entry.get("outcome"), dict) else {}
            )
            steps.append(
                project_step(
                    step_id=str(entry.get("id") or f"carried-{index}"),
                    call_id=f"carried-{index}",
                    tool=plan.get("tool") or ACTION_TO_TOOL.get(action, action),
                    action=action,
                    parameters=plan.get("parameters") or {},
                    outcome=outcome,
                    carried=True,
                    from_request_id=retry_of,
                )
            )
        return steps

    def _record_transcript(
        self, ctx: _DynamicContext, action: ActionPlan, outcome: ActionOutcome
    ) -> None:
        """把本次工具调用与公开结果记进本轮 transcript（随轮次一起提交）。"""

        index = len(ctx.transcript) + 1
        ctx.transcript.append(
            project_step(
                step_id=action.tool_call_id or f"step-{index}",
                call_id=action.tool_call_id or f"call-{index}",
                tool=action.tool
                or ACTION_TO_TOOL.get(action.action, action.action),
                action=action.action,
                parameters=action.parameters,
                outcome=outcome.to_dict(),
            )
        )

    def _refresh_workspace(self, ctx: _DynamicContext, action: ActionPlan) -> None:
        """动作改变了可见事实后重建工作区；只读工具自身已保证表是最新的。"""

        if action.action not in _WORKSPACE_CHANGING_ACTIONS:
            return
        ctx.workspace = {}
        ctx.ref_table = None
        ctx.workspace_text = workspace_text(self._workspace(ctx))

    def _dynamic_state_patch(
        self, ctx: _DynamicContext, review_state: dict[str, Any]
    ) -> dict[str, Any]:
        active_problem = ctx.active_problem
        active_exercise = ctx.active_exercise
        return {
            "confirmed_course": ctx.confirmed_course,
            "question_context": self._question_context_line(ctx, review_state),
            "memory_context": ctx.memory_text,
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
                TurnInputRulesMixin._exercise_needs_supplement(active_exercise)
            ),
            "workspace_text": ctx.workspace_text,
        }

    def _guard_dynamic_action(
        self,
        action: ActionPlan,
        ctx: _DynamicContext,
        review_state: dict[str, Any],
        text: str,
    ) -> ActionOutcome | None:
        """执行层红线：写操作必须有事前请求，答案动作交给 C 门禁。

        ``basis`` 只做追溯；即使模型给了看似合理的依据，也不能替代本轮学生
        原话或系统可核验的交互。被可纠正拒绝的动作允许同轮改参数重试。
        """

        name = action.action
        stripped = (text or "").strip()

        def reject(
            code: str,
            message: str,
            *,
            needs_user: bool = False,
            remedies: list[str] | None = None,
            facts: dict[str, Any] | None = None,
            rejected: bool = True,
        ) -> ActionOutcome:
            details: dict[str, Any] = {
                "remedies": list(remedies or ["ask_user"]),
                "facts": dict(facts or {"action": name}),
                "needs_user": needs_user,
            }
            if rejected:
                details["rejected"] = True
            return ActionOutcome(
                name,
                "clarification" if needs_user else "failed",
                message,
                details=details,
                needs_user=needs_user,
                error_code=code,
                executed=False,
            )

        # 查卷只负责把试卷定位出来并给下载入口。学生只点名一份试卷（没有给题型、
# 题号、关键词、位置等具体条件）时，模型不能顺手把整卷题目展开。
        if name in _QUESTION_CONTENT_ACTIONS:
            specific = any(
                action.parameters.get(key) not in (None, "", [], ())
                for key in _QUESTION_CONTENT_PARAMS
            ) or action.parameters.get("scope") == "bank"
            located = ctx.result
            if (
                not specific
                and located is not None
                and getattr(located, "kind", None) == "found"
                and getattr(located, "total", None) == 1
            ):
                return reject(
                    "QUESTION_CONTENT_REQUIRED",
                    f"{located.message}要列这份卷的题目、看某一道或讲解，你说一声我再展开。",
                    needs_user=True,
                    remedies=["ask_user"],
                    rejected=False,
                )

        # F010 D：只有写操作走独立授权；答案/讲解/练习解答由 C 的
        # question_answer_gate / question_explain_gate / exercise_solution_gate
        # 在具体动作里按版本、来源和本轮原话判断。
        if name in _WRITE_ACTIONS:
            basis = action.parameters.get("basis")
            basis_error = validate_basis(
                basis,
                current_text=stripped,
                transcript=list(ctx.transcript),
                suggestions=list(review_state.get("last_suggestions") or []),
            )
            if basis_error is not None:
                code, message = basis_error
                return reject(
                    code,
                    message,
                    needs_user=code == "AMBIGUOUS_BASIS",
                    remedies=[name, "ask_user"],
                )

            if not stripped:
                return reject(
                    "AUTHORIZATION_REQUIRED",
                    "这轮没有可授权的操作请求，我不会执行写入。",
                    remedies=["ask_user"],
                    facts={"action": name, "reason": "empty_text"},
                )

            suggestions = list(review_state.get("last_suggestions") or [])
            if _is_bare_affirmative(stripped) and len(suggestions) >= 2:
                options = "、".join(str(item) for item in suggestions[:5])
                return reject(
                    "AMBIGUOUS_BASIS",
                    f"上一轮有这些选项：{options}。你要先做哪一条？",
                    needs_user=True,
                    remedies=["ask_user"],
                    facts={"action": name, "suggestions": suggestions[:5]},
                    rejected=False,
                )

            decision = authorize_action(
                name,
                stripped,
                transcript=list(ctx.transcript),
                entry_id=action.parameters.get("entry_id"),
            )
            if decision is not None:
                needs_user = needs_user_for_denial(
                    name, stripped, decision.needs_user
                )
                return reject(
                    decision.code,
                    decision.message,
                    needs_user=needs_user,
                    remedies=decision.remedies,
                    facts=decision.facts,
                    rejected=not needs_user,
                )

        # 学习记忆写操作必须落在本轮真实返回过的条目上；这是对象归属校验，
        # 与“学生是否授权”分开。
        if name in ("correct_memory", "delete_memory"):
            raw = action.parameters.get("entry_id")
            parsed = parse_entry_id(raw if isinstance(raw, str) else "")
            if parsed is None or f"{parsed[0]}:{parsed[1]}" not in ctx.memory_entry_ids:
                return reject(
                    "MEMORY_ENTRY_UNAVAILABLE",
                    "要先看到这条学习记忆才能改它：我把最近的记录列出来，你再指哪一条。",
                    needs_user=True,
                    remedies=["review_learning_memory", "ask_user"],
                )

        # 同轮“我懂了”之后又出题/保存：真实步骤的动作在 plan.action。
        if name in _RED_LINE_ACTIONS and any(
            isinstance(step, dict)
            and (
                (step.get("plan") or {}).get("action")
                if isinstance(step.get("plan"), dict)
                else step.get("action")
            )
            == "record_feedback"
            and (step.get("outcome") or {}).get("status") == "success"
            for step in ctx.transcript
        ):
            return reject(
                "RED_LINE_CONFLICT",
                "已记录你的理解。要练一道类似题，或把这道题存进错题本时，直接告诉我。",
                remedies=["ask_user"],
            )

        # 同一轮前一步已经确定“先自己做”，后续答案动作不能再公开；只问提示/考点
        # 交给 C 门禁换成公开材料回答。
        if name in {"view_question_answer", "reveal_solution"} and ctx.answer_deferred:
            return reject(
                "ANSWER_SCOPE_DENIED",
                "这轮你只要提示，我不会公布答案或完整解法。",
                remedies=["explain_problem", "ask_user"],
            )
        if (
            name == "explain_problem"
            and ctx.answer_deferred
            and action.parameters.get("scope") not in {"hint", "topic"}
        ):
            return reject(
                "ANSWER_NOT_REQUESTED",
                "这轮你只要找题，没有要求讲解；我不会公布答案或完整解法。",
                remedies=["ask_user"],
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
        ctx.authorized_text = None
        scope_param = action.parameters.get("scope")
        if scope_param in {"hint", "topic"}:
            ctx.answer_scope = scope_param
        if action.parameters.get("defer_answer") is True:
            ctx.answer_deferred = True
        # 回答范围只由学生本轮的话决定：提示、考点都不等于“完整讲解/看答案”。
        if ctx.answer_scope is None:
            ctx.answer_scope = answer_gate.scope_of(prepared.input.text)
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
            selected = ctx.question_card or ctx.question_selection
            ctx.answer_grant = answer_gate.AnswerGrant(
                kind="question_explain",
                scope=ctx.answer_scope or "hint",
                request_id=prepared.request_id,
                target_id=str(
                    getattr(ctx.question_card, "question_id", None)
                    or getattr(ctx.question_selection, "question_id", "")
                    or ""
                ),
                build_id=getattr(selected, "build_id", None),
                content_sha256=getattr(selected, "content_sha256", None),
                selection_id=getattr(ctx.question_selection, "id", None),
            )
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
            return await self._dynamic_followup(prepared, ctx, emit, action.parameters.get("target", "auto"))

        if name == "generate_exercise":
            return await self._dynamic_generate_exercise(prepared, ctx, emit)

        if name == "evaluate_exercise":
            return await self._dynamic_evaluate_exercise(prepared, ctx, emit)

        if name == "reveal_solution":
            with self._repository.action_write():
                return self._action_result(prepared, ctx, self._dynamic_reveal(ctx))

        if name == "record_mistake":
            with self._repository.action_write():
                return self._dynamic_record_mistake(prepared, ctx, emit, action.parameters.get("user_feedback"))

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

        if name == "review_memory":
            return self._action_result(
                prepared,
                ctx,
                self._memory_review_outcome(
                    prepared,
                    action,
                    ctx,
                    prepared.input.text,
                    confirmed_course=review_state.get("confirmed_course"),
                ),
            )

        if name == "correct_memory":
            # 记忆写入走独立连接，不占用会话库的写事务，避免与恢复记录互锁。
            return self._action_result(
                prepared, ctx, self._memory_correct_outcome(prepared, action, ctx)
            )

        if name == "delete_memory":
            return self._action_result(
                prepared, ctx, self._memory_delete_outcome(prepared, action, ctx)
            )

        if name == "view_workspace":
            return self._view_workspace_outcome(prepared, action, ctx)

        if name == "view_question_detail":
            return self._view_question_detail_outcome(prepared, action, ctx)

        if name == "list_shown_questions":
            return self._list_shown_questions_outcome(prepared, action, ctx)

        if name == "view_learning_summary":
            return self._view_learning_summary_outcome(prepared, action, ctx)

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
