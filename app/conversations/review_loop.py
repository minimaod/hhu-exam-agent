"""调度与动态复习循环：动作执行、守卫、结果合成和失败降级。"""

import json
import logging
import re
import uuid
from collections.abc import (
    AsyncIterator,
    Callable,
)
from contextlib import suppress
from typing import Any

from fastapi import Request

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
    MAX_SCHEDULER_ROUNDS,
)
from app.conversations.inputs import canonical_json
from app.conversations.models import (
    ActionResultItem,
    RECOVERABLE_CODES,
    TurnErrorEvent,
    TurnEvent,
    TurnProgressEvent,
    TurnResultEvent,
    reply_state as _reply_state,
)
from app.conversations.reply import material_to_payload
from app.conversations.tools import (
    TOOLS_BY_NAME,
    tool_label as _tool_label,
)
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
from app.questions.intent import (
    is_question_lookup,
    question_answer_requested,
)
from app.streaming import (
    SENTINEL,
    stream_events,
)
from app.tutoring.models import FeedbackResult

logger = logging.getLogger("app.conversations.service")

_RED_LINE_ACTIONS = frozenset({"generate_exercise", "record_mistake"})

_SELF_REPORT_RE = re.compile(
    r"(我懂了|我听懂了|我明白了|懂了|听懂了|明白了|理解了|清楚了|会了|学会了|谢谢)"
)

_PRACTICE_REQUEST_RE = re.compile(
    r"(再来|来一道|来道|出一道|出题|练|类似题|变式|做题|刷题)"
)

_SAVE_REQUEST_RE = re.compile(r"(保存|存起来|存一下|记录|错题|收藏|加入)")

_QUESTION_CONTENT_ACTIONS = frozenset({"list_paper_questions", "search_questions"})

_QUESTION_CONTENT_REQUEST_RE = re.compile(
    r"题|考点|考啥|考什么|讲解|讲讲|解释|解析|答案|练习|列一下|列出来"
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
                    "memory_context": memory_context,
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
            fallback_text = self._dynamic_reply(final_state, composite_results, ctx)
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
                assistant_message=fallback_text,
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
        }

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
        if not stripped:
            return None

        # 查卷只负责把试卷定位出来并给下载入口。学生只点名一份试卷（没提题型、
        # 题号、题目、考点或讲解）时，模型不能顺手把整卷题目展开——这是截图里
        # “只发卷名却列出 59 道题”的真实原因。同一轮先查卷再列题时一律拦下；
        # 学生下一轮说“这张卷的选择题”时，本轮并没有新查卷结果，不会被误拦。
        if name in _QUESTION_CONTENT_ACTIONS and not _QUESTION_CONTENT_REQUEST_RE.search(
            stripped
        ):
            located = ctx.result
            if (
                located is not None
                and located.kind == "found"
                and located.total == 1
            ):
                return ActionOutcome(
                    name,
                    "clarification",
                    f"{located.message}要列这份卷的题目、看某一道或讲解，你说一声我再展开。",
                    needs_user=True,
                )

        # 学习记忆的写操作只允许落在本轮真实返回过的条目上；删除还需要学生
        # 明确表达“移除”的意图，避免模型把“这题我其实会”当成删除指令。
        if name in ("correct_memory", "delete_memory"):
            raw = action.parameters.get("entry_id")
            parsed = parse_entry_id(raw if isinstance(raw, str) else "")
            if parsed is None or f"{parsed[0]}:{parsed[1]}" not in ctx.memory_entry_ids:
                return ActionOutcome(
                    name,
                    "clarification",
                    "要先看到这条学习记忆才能改它：我把最近的记录列出来，你再指哪一条。",
                    needs_user=True,
                )
            if name == "delete_memory" and not self._MEMORY_DELETE_INTENT.search(
                stripped
            ):
                return ActionOutcome(
                    name,
                    "clarification",
                    "要把这条从学习记忆和复习建议里移除吗？原会话记录会保留。",
                    needs_user=True,
                )

        if name not in _RED_LINE_ACTIONS:
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
