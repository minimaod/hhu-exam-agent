"""基于 LangGraph 的原生工具调用调度器（F008）。

职责边界：
- 组合本轮输入、持久上下文（课程、当前原题、当前练习、上次建议）与最近对话；
- 使用原生工具定义与 ``tool_calls`` 决定下一步能力，按 ``tool_call_id`` 把真实
  执行结果回传给模型，再由模型决定继续或结束；
- 有界执行：最多 4 次工具决策、每次响应最多接受 3 个工具调用、单轮最多尝试
  执行 4 个工具（含被拒绝的尝试）；
- 工具决策阶段不向页面转发模型正文；正文由服务层在业务结束后单独流式生成；
- 保留交互红线：理解自述不自动出题或保存、歧义“好”先澄清、拒绝建议不强推、
  普通找真题先出题等待作答、未授权答案不外泄。

本模块只负责控制流与协议；业务动作、持久化和 SSE 进度仍由注入的
``execute_action`` 完成，执行失败是正常观察结果，不会被吞成成功。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from app.config import DeepSeekSettings
from app.conversations.protocol import ActionPlan, ActionStatus, ActionType
from app.conversations.tools import (
    ToolRejection,
    openai_tools,
    parse_tool_call,
    tool_label,
)
from app.llm import build_chat_model, log_usage, map_provider_error, response_text
from app.papers.courses import detect_course, normalize_course
from app.querying.errors import QueryError

__all__ = [
    "ActionOutcome",
    "ActionPlan",
    "ActionStatus",
    "ActionType",
    "DynamicReviewGraph",
    "ExecutionResult",
    "ReviewState",
    "ScheduleDecision",
    "SchedulerAgent",
    "TOOLING_SYSTEM_PROMPT",
]

# 每轮工具决策模型的调用次数上限（F008 任务书 §五）。
MAX_SCHEDULER_ROUNDS = 4
# 一次模型响应里最多接受的工具调用数；多余的调用会被拒绝并回传结果。
MAX_TOOL_CALLS_PER_RESPONSE = 3
# 每轮最多尝试执行的工具数量；参数无效和被拒绝的尝试也计入。
MAX_EXECUTED_ACTIONS = 4


class ScheduleDecision(BaseModel):
    """一次工具决策的规范化结果。"""

    model_config = ConfigDict(extra="forbid")

    confirmed_course: str | None = None
    actions: list[ActionPlan] = Field(default_factory=list)
    assistant_reply: str | None = Field(default=None, max_length=3000)
    suggested_actions: list[str] = Field(default_factory=list, max_length=5)
    # 参数非法/未知工具/超出单响应上限的调用；已按 tool_call_id 回传结果。
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    # 模型没有请求任何工具，说明它认为本轮可以直接回复。
    finished: bool = False


TOOLING_SYSTEM_PROMPT = """\
你是河海大学考试复习助手的调度核心。你要根据学生本轮发言、当前持久上下文、最近对话，以及你刚调用工具得到的真实结果，决定下一步调用哪个工具。

严格守则（必须绝对遵守）：
1. “我懂了”“明白了”“听懂了”：只调用 record_feedback，绝对不能同时调用 generate_exercise（出题）或 save_mistake（保存错题）。
2. 歧义消除：如果上下文里“上次建议”给出多个选项，学生只回答“好”“可以”且没有指明具体做哪件事时，必须只调用 ask_user 澄清，不能擅自出题或保存。
3. 拒绝建议：学生说“不用了”“不需要”“算了”时，调用 ask_user 温和确认，不再强推。
4. 复合指令：学生明确要求两个操作（如“保存这题，再来一道类似题”）时，可以在一次响应里同时调用 save_mistake 和 generate_exercise；也可以先调用一个，看到真实结果后再调用下一个。
5. 依赖关系：需要前一步新生成的对象（例如刚查到的题、刚生成的练习）时，必须先看到本轮的 tool 结果再发起下一次调用，不能凭猜测填题目 ID 或练习 ID。
6. 上下文继承：已确认课程后学生说“给我期末试卷”，调用 query_papers 并带上该课程；考试类型只按学生本轮明确要求填写，未提及就留空，不要默认期末。
7. 没有当前题目时，不要调用 answer_followup、generate_exercise、save_mistake、record_feedback、evaluate_exercise、reveal_solution。
8. 区分题目修改方式：全新题目 mode=new；给尚未讲清的题补充信息 mode=supplement；纠正已讲解题目的题干 mode=correction。程序会保护已讲解的旧版本，不会被原地覆盖。
9. 如果某个工具的结果是 failed 或要求用户补充，不要重复同一个调用，也不要调用依赖它成功的工具；这时应调用 ask_user 如实说明，或直接结束本轮。
10. 课程名必须是学生本轮说出的课程或已确认课程（如“高等数学”“Python”“转专业”），不能写成“通识选修课程”“专业课”等类别。
11. 本地目录与题库事实必须来自工具真实返回。未调用 query_papers 前不能声称某课程没有资料；未调用 search_questions 前不能声称有真题。查询失败或没有结果时，不得假装看到了题目，也不能编造题干、答案或参考代码。
12. 查题与浏览是中间事实，不是“必须结束本轮”的信号。学生指定了试卷和题型（“这张卷的选择题”“所有单选”“前五题”）时用 list_paper_questions；有明确内容主题（“考循环的那几道”）时用 search_questions 并带上 query；“第 5 题”“第五小题”是原卷题号，用 number_label 精确定位，与当前展示到第几道无关。
13. 学生问考点、题意、某个选项的含义，或要讲解：先按上一句定位到具体题目（必要时先查题或选题），然后可以用 explain_problem 讲解；只问考点时也可以根据已返回的公开题干直接回答。不要要求学生先作答，也不要要求学生说“讲解”“直接”这类口令。只有学生明确表示“先自己做、不给答案”时，才展示题目并等他作答。
14. 真题讲解用 explain_problem；基于当前真题生成变式用 generate_exercise 并带 from_question=true；保存真题到错题本用 save_mistake。
15. 学生没有主动查看答案前，不能调用 show_question_answer 或 reveal_solution，也不要在回复里泄露答案或参考代码。
16. 确实需要学生补充信息、或学生要求自己先做时，调用 ask_user 或在结果里说明，然后停止新增工具调用。
17. 如果这一轮只需要自然语言回复（例如学生在闲聊或你已经拿到全部结果），不要调用任何工具。

调用风格：
- 一次响应最多 3 个工具调用；总体不超过 4 次工具尝试。
- 每次只做真实必要的事，不要为了凑步骤重复调用同一个工具。
- 工具参数只填工具定义里存在的字段。
"""


@dataclass
class ActionOutcome:
    """单个业务动作的真实执行结果；由服务层填写，图只读取状态。"""

    action: str
    status: ActionStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    needs_user: bool = False
    executed: bool = True
    error_code: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "status": self.status,
            "message": self.message,
            "details": {**self.details, "error_code": self.error_code, "retryable": self.retryable},
            "needs_user": self.needs_user,
            "executed": self.executed,
        }

    def to_tool_payload(self) -> dict[str, Any]:
        """回传给模型的工具结果；只含公开事实，不含答案正文。"""

        return {
            "status": "ok" if self.status == "success" else (
                "needs_user" if self.status == "clarification" else "failed"
            ),
            "message": self.message,
            "needs_user": self.needs_user,
            "details": dict(self.details),
            "error_code": self.error_code,
            "retryable": self.retryable,
        }


@dataclass
class ExecutionResult:
    """执行器返回：结果 + 供下一次决策使用的上下文更新。"""

    outcome: ActionOutcome
    context: dict[str, Any] = field(default_factory=dict)


class ReviewState(TypedDict, total=False):
    query: str
    confirmed_course: str | None
    active_problem_text: str | None
    active_problem_status: str | None
    active_exercise_statement: str | None
    active_exercise_status: str | None
    active_exercise_needs_supplement: bool
    question_context: str | None
    last_suggestions: list[str]
    recent_turns_summary: str
    has_attachments: bool
    rounds: int
    executed_count: int
    rejected_count: int
    last_rejected: int
    pending: list[ActionPlan]
    observations: list[dict[str, Any]]
    assistant_reply: str | None
    suggested_actions: list[str]
    replan_error: str | None


# 同一轮里，右侧动作只有在左侧动作存在且成功时才允许执行。
# 左侧动作未出现在本轮时，代表依赖已经存在于持久上下文，由服务层前置校验兜底。
ACTION_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "followup_problem": ("explain_problem",),
    "generate_exercise": ("explain_problem",),
    "record_mistake": ("explain_problem",),
    "evaluate_exercise": ("generate_exercise",),
    "reveal_solution": ("generate_exercise",),
    # 同一轮里先查真题再讲解/看答案时，查题失败就不允许假装讲题。
    "explain_problem": ("search_questions",),
    "view_question_answer": ("search_questions",),
}

# 这些动作在一轮内最多尝试一次，避免重新决策时重复副作用。
_SINGLE_USE_ACTIONS = {
    "introduce",
    "query_papers",
    "explain_problem",
    "followup_problem",
    "generate_exercise",
    "evaluate_exercise",
    "reveal_solution",
    "record_mistake",
    "record_feedback",
    "search_questions",
    "select_question",
    "view_question_answer",
}

_ACTION_LABELS = {
    "introduce": "复习介绍", "query_papers": "查找试卷", "explain_problem": "题目讲解",
    "followup_problem": "回答追问", "generate_exercise": "生成练习", "evaluate_exercise": "评价作答",
    "reveal_solution": "查看解答", "record_mistake": "保存错题", "record_feedback": "记录反馈",
    "search_questions": "查找真题", "select_question": "选择真题", "view_question_answer": "查看真题答案",
    "clarify_or_reply": "澄清提问",
}


def _action_label(action: str) -> str:
    return _ACTION_LABELS.get(action) or tool_label(action.removeprefix("tool:"))


class SchedulerAgent:
    """原生工具调用调度器。

    与 F006 的差别只在“模型怎么表达下一步”：不再要求模型输出动作 JSON，而是
    使用 ``bind_tools`` 的原生 ``tool_calls``。执行结果通过 ``observe()`` 以
    ``role="tool"`` 按 ``tool_call_id`` 回传，因此模型能真正看到上一步的结果。

    实例按轮次创建（`ConversationService._make_scheduler()`），所以对话转录
    只存在于这一轮内；跨轮上下文仍由会话持久上下文和最近对话摘要提供。
    """

    def __init__(
        self,
        settings: DeepSeekSettings | None = None,
        *,
        model: Any | None = None,
        http_async_client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._model = model
        self._http_async_client = http_async_client
        self._messages: list[Any] | None = None
        self._observed: set[str] = set()

    # ------------------------------------------------------------------
    # 协议：把执行结果按 tool_call_id 回传给模型
    # ------------------------------------------------------------------

    def observe(self, tool_call_id: str | None, payload: dict[str, Any]) -> None:
        if not tool_call_id or self._messages is None:
            return
        if tool_call_id in self._observed:
            return
        self._observed.add(tool_call_id)
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(payload, ensure_ascii=False),
            }
        )

    # ------------------------------------------------------------------
    # 决策
    # ------------------------------------------------------------------

    async def plan(
        self,
        query: str,
        *,
        confirmed_course: str | None = None,
        active_problem_text: str | None = None,
        active_problem_status: str | None = None,
        active_exercise_statement: str | None = None,
        active_exercise_status: str | None = None,
        active_exercise_needs_supplement: bool = False,
        question_context: str | None = None,
        last_suggestions: list[str] | None = None,
        recent_turns_summary: str | None = None,
        has_attachments: bool = False,
        observations: list[dict[str, Any]] | None = None,
        iteration: int = 1,
    ) -> ScheduleDecision:
        # 纯图片输入没有文字可判断意图：当前练习可作答/待补充时是作答，
        # 否则才是新题或补充题目。不能一律当成 explain_problem，否则会把
        # 图片作答误判成纠正原题。只在第一次决策时使用。
        if has_attachments and not query.strip() and iteration == 1:
            if (
                active_exercise_status in ("pending_answer", "revealed")
                or active_exercise_needs_supplement
            ):
                return ScheduleDecision(
                    actions=[
                        ActionPlan(
                            action="evaluate_exercise",
                            reason="用户上传了当前练习的作答图片",
                        )
                    ],
                    suggested_actions=[],
                )
            return ScheduleDecision(
                actions=[ActionPlan(action="explain_problem", reason="用户上传了题目图片")],
                suggested_actions=[],
            )

        detected_course = detect_course(query)
        model = self._model or build_chat_model(
            self._settings,  # type: ignore[arg-type]
            max_tokens=900,
            timeout=25.0,
            json_mode=False,
            http_async_client=self._http_async_client,
        )

        if self._messages is None:
            context_parts: list[str] = []
            if detected_course:
                context_parts.append(
                    f"学生本轮提到的课程：{detected_course}（工具里的 course 必须使用该课程名，"
                    "不能改写成“通识选修课程”“专业课”等类别）"
                )
            if confirmed_course:
                context_parts.append(f"已确认复习科目：{confirmed_course}")
            if active_problem_text:
                snippet = active_problem_text[:200] + ("..." if len(active_problem_text) > 200 else "")
                status_text = f"（状态：{active_problem_status}）" if active_problem_status else ""
                context_parts.append(f"当前关注原题{status_text}：{snippet}")
            if active_exercise_statement:
                ex_snippet = active_exercise_statement[:150] + (
                    "..." if len(active_exercise_statement) > 150 else ""
                )
                context_parts.append(
                    f"当前练习题状态：{active_exercise_status}，题干：{ex_snippet}"
                )
                if active_exercise_needs_supplement:
                    context_parts.append("当前练习还需要学生补充解题过程。")
            if question_context:
                context_parts.append(question_context[:200])
            if last_suggestions:
                context_parts.append(f"上次助手提出的建议选项：{', '.join(last_suggestions)}")
            if recent_turns_summary:
                context_parts.append(f"最近对话摘要：\n{recent_turns_summary}")
            context_text = "\n".join(context_parts) if context_parts else "（当前无特殊上下文）"
            self._messages = [
                {"role": "system", "content": TOOLING_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"【当前业务上下文】：\n{context_text}\n\n"
                        f"【学生本轮发言】：\n{query}\n\n"
                        "请决定下一步调用哪些工具；如果本轮不需要任何工具，就不要调用。"
                    ),
                },
            ]

        bound = model.bind_tools(openai_tools()) if hasattr(model, "bind_tools") else model
        try:
            response = await bound.ainvoke(self._messages)
        except Exception as exc:
            mapped = map_provider_error(exc)
            if mapped:
                raise mapped from exc
            raise QueryError("MODEL_UNAVAILABLE", "会话调度服务暂时不可用，请稍后重试。") from exc

        log_usage("scheduler_tools", response)
        return self._decision_from_response(response, detected_course, iteration)

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    def _decision_from_response(
        self,
        response: Any,
        detected_course: str | None,
        iteration: int,
    ) -> ScheduleDecision:
        raw_calls = list(getattr(response, "tool_calls", None) or [])
        content = _safe_text(response)

        if self._messages is not None:
            self._messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": call.get("id") or "",
                            "type": "function",
                            "function": {
                                "name": call.get("name") or "",
                                "arguments": json.dumps(
                                    call.get("args") or {}, ensure_ascii=False
                                ),
                            },
                        }
                        for call in raw_calls
                    ],
                }
            )

        if not raw_calls:
            return ScheduleDecision(
                assistant_reply=content or None,
                finished=True,
                confirmed_course=detected_course,
            )

        actions: list[ActionPlan] = []
        rejected: list[dict[str, Any]] = []
        for position, call in enumerate(raw_calls):
            call_id = str(call.get("id") or f"call-{iteration}-{position}")
            name = str(call.get("name") or "")
            if position >= MAX_TOOL_CALLS_PER_RESPONSE:
                rejection = ToolRejection(
                    tool_call_id=call_id,
                    tool=name,
                    action=f"tool:{name}",
                    code="TOO_MANY_CALLS",
                    message=(
                        f"一次响应最多接受 {MAX_TOOL_CALLS_PER_RESPONSE} 个工具调用，"
                        f"“{tool_label(name)}”没有执行；如需继续请在下一轮再调用。"
                    ),
                )
                self.observe(call_id, rejection.to_payload())
                rejected.append(rejection.to_observation())
                continue
            plan, rejection = parse_tool_call(
                name, call.get("args") or {}, tool_call_id=call_id
            )
            if rejection is not None:
                self.observe(call_id, rejection.to_payload())
                rejected.append(rejection.to_observation())
                continue
            assert plan is not None
            actions.append(plan)

        suggested = _suggested_from_actions(actions)
        question = _question_from_actions(actions)
        param_course: str | None = None
        for plan in actions:
            candidate = plan.parameters.get("course")
            if isinstance(candidate, str) and candidate.strip():
                param_course = candidate.strip()
                break
        # 确定性识别的课程优先：模型把“转专业考试”当成“通识选修课程”时纠正；
        # 写法与识别结果等价时保留模型原话（例如都指 Python 语言程序设计）。
        course: str | None = detected_course
        if detected_course is None:
            course = normalize_course(param_course) or param_course if param_course else None
        elif param_course and normalize_course(param_course) == detected_course:
            course = param_course
        return ScheduleDecision(
            confirmed_course=course,
            actions=actions,
            assistant_reply=question,
            suggested_actions=suggested,
            rejected=rejected,
            finished=False,
        )


def _model_reply_text(decision: ScheduleDecision) -> str | None:
    """决定模型这一轮的文字能否当作给学生的正文。

    F008 的约定：工具决策阶段的文字是内部说明，不转发；只有两种情况例外——
    本轮没有调用任何工具（模型直接回答），或模型正在向学生提问（澄清问题
    本身就是要说的话）。
    """

    if not decision.assistant_reply:
        return None
    if not decision.actions and not decision.rejected:
        return decision.assistant_reply
    if any(action.action == "clarify_or_reply" for action in decision.actions):
        return decision.assistant_reply
    return None


def _safe_text(response: Any) -> str:
    try:
        return response_text(response)
    except QueryError:
        return ""


def _question_from_actions(actions: list[ActionPlan]) -> str | None:
    for plan in actions:
        if plan.action == "clarify_or_reply":
            question = plan.parameters.get("question")
            if isinstance(question, str) and question.strip():
                return question.strip()[:3000]
    return None


def _suggested_from_actions(actions: list[ActionPlan]) -> list[str]:
    for plan in actions:
        if plan.action == "clarify_or_reply":
            options = plan.parameters.get("options")
            if isinstance(options, list):
                return [str(item)[:80] for item in options[:5]]
    return []


class DynamicReviewGraph:
    """有界的“工具决策 → 执行 → 观察 → 再决策或回复”LangGraph。

    图只负责控制流和依赖判断；具体业务动作、持久化和 SSE 进度由注入的
    ``execute_action`` 完成。每执行或跳过一次工具调用，都会按 ``tool_call_id``
    把真实结果回传给调度器，供下一次决策使用。
    """

    def __init__(
        self,
        scheduler: Any,
        execute_action: Any,
        *,
        max_rounds: int = MAX_SCHEDULER_ROUNDS,
        max_actions: int = MAX_EXECUTED_ACTIONS,
        resume_actions: list[ActionPlan] | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._execute_action = execute_action
        self._max_rounds = max_rounds
        self._max_actions = max_actions
        self._resume_actions = resume_actions
        self._graph = self._build()

    def _build(self) -> Any:
        graph = StateGraph(ReviewState)
        graph.add_node("decide", self._decide)
        graph.add_node("execute", self._execute)
        graph.add_node("respond", self._respond)
        graph.add_edge(START, "decide")
        graph.add_conditional_edges(
            "decide",
            self._after_decide,
            {"execute": "execute", "decide": "decide", "respond": "respond"},
        )
        graph.add_conditional_edges(
            "execute",
            self._after_execute,
            {"execute": "execute", "decide": "decide", "respond": "respond"},
        )
        graph.add_edge("respond", END)
        return graph.compile()

    async def run(self, initial: ReviewState) -> ReviewState:
        if self._resume_actions is not None:
            # 恢复已批准的计划，不重新决策；最多保留原四轮的十二个计划步骤。
            state = dict(initial)
            state["pending"] = self._resume_actions[: self._max_rounds * MAX_TOOL_CALLS_PER_RESPONSE]
            while state.get("pending"):
                state.update(await self._execute(state))
                if any(item.get("needs_user") for item in state.get("observations", [])):
                    break
            return state
        return await self._graph.ainvoke(initial)

    async def _decide(self, state: ReviewState) -> dict[str, Any]:
        rounds = int(state.get("rounds", 0)) + 1
        if rounds > self._max_rounds:
            return {"rounds": rounds, "pending": [], "last_rejected": 0}

        try:
            decision: ScheduleDecision = await self._scheduler.plan(
                state["query"],
                confirmed_course=state.get("confirmed_course"),
                active_problem_text=state.get("active_problem_text"),
                active_problem_status=state.get("active_problem_status"),
                active_exercise_statement=state.get("active_exercise_statement"),
                active_exercise_status=state.get("active_exercise_status"),
                active_exercise_needs_supplement=bool(
                    state.get("active_exercise_needs_supplement")
                ),
                question_context=state.get("question_context"),
                last_suggestions=state.get("last_suggestions") or [],
                recent_turns_summary=state.get("recent_turns_summary"),
                has_attachments=bool(state.get("has_attachments")),
                observations=list(state.get("observations") or []),
                iteration=rounds,
            )
        except Exception as exc:
            # 第一次决策失败说明本轮无法开始；重决策失败则保留已执行结果，
            # 由回复节点如实说明“后续建议失败”，而不是丢弃已完成操作。
            if rounds == 1:
                raise
            return {
                "rounds": rounds,
                "pending": [],
                "replan_error": str(exc) or "后续决策失败",
            }

        pending = list(decision.actions[:MAX_TOOL_CALLS_PER_RESPONSE])
        rejected = list(getattr(decision, "rejected", []) or [])
        return {
            "rounds": rounds,
            "pending": pending,
            "observations": list(state.get("observations") or []) + rejected,
            "rejected_count": int(state.get("rejected_count", 0)) + len(rejected),
            "last_rejected": len(rejected),
            "assistant_reply": _model_reply_text(decision),
            # 收尾决策常常不再重复建议；沿用最近一次非空建议，避免完成动作后
            # 建议按钮凭空消失。
            "suggested_actions": (
                list(decision.suggested_actions)
                or list(state.get("suggested_actions") or [])
            ),
            "confirmed_course": decision.confirmed_course or state.get("confirmed_course"),
            "replan_error": None,
        }

    async def _execute(self, state: ReviewState) -> dict[str, Any]:
        pending = list(state.get("pending") or [])
        observations = list(state.get("observations") or [])
        executed_count = int(state.get("executed_count", 0))
        rejected_count = int(state.get("rejected_count", 0))
        if not pending:
            return {}

        action = pending[0]
        blocked = self._blocked_reason(action.action, observations)
        if blocked is None and executed_count + rejected_count >= self._max_actions:
            blocked = "本轮动作数量已达上限，该步骤没有执行。"

        if blocked is not None:
            outcome = ActionOutcome(
                action=action.action,
                status="failed",
                message=blocked,
                details={
                    "skipped": True,
                    "parameters": action.parameters,
                    "tool": action.tool,
                },
                executed=False,
            )
            self._report(action, outcome)
            return {
                "pending": pending[1:],
                "observations": observations + [outcome.to_dict()],
            }

        result: ExecutionResult = await self._execute_action(action, dict(state))
        self._report(action, result.outcome)
        patch: dict[str, Any] = {
            "pending": [] if result.outcome.needs_user else pending[1:],
            "observations": observations + [result.outcome.to_dict()],
            "executed_count": executed_count + 1,
        }
        if result.outcome.needs_user:
            # 一次响应里的其余调用没有执行，也必须给模型一个真实结果，
            # 否则下次决策的 tool 消息会缺少 tool_call_id。
            for remaining in pending[1:]:
                self._report(
                    remaining,
                    ActionOutcome(
                        action=remaining.action,
                        status="failed",
                        message="本轮已停止，等待学生回答后再继续。",
                        details={"skipped": True, "waiting_user": True, "tool": remaining.tool},
                        executed=False,
                    ),
                )
        patch.update(result.context)
        return patch

    def _report(self, action: ActionPlan, outcome: ActionOutcome) -> None:
        observe = getattr(self._scheduler, "observe", None)
        if observe is None:
            return
        observe(action.tool_call_id, outcome.to_tool_payload())

    def _after_decide(self, state: ReviewState) -> str:
        if state.get("pending"):
            # 即使已达执行上限，也进入 execute 把剩余动作标记为 skipped，
            # 避免回复把未执行的动作当成已完成。
            return "execute"
        if (
            int(state.get("last_rejected", 0)) > 0
            and int(state.get("rounds", 0)) < self._max_rounds
            and not any(item.get("needs_user") for item in state.get("observations") or [])
        ):
            # 参数非法/未知工具后允许模型用正确参数再试一次，仍受决策次数约束。
            return "decide"
        return "respond"

    def _after_execute(self, state: ReviewState) -> str:
        observations = list(state.get("observations") or [])
        if any(item.get("needs_user") for item in observations):
            return "respond"
        if state.get("pending"):
            return "execute"
        if state.get("replan_error"):
            return "respond"
        if any(
            (item.get("details") or {}).get("waiting_user") for item in observations
        ):
            # 程序已经要求“等学生回答”，不再自行继续。
            return "respond"
        if int(state.get("rounds", 0)) < self._max_rounds:
            # 工具批次执行完就回到决策：模型可以根据真实结果决定下一步，
            # 也可以直接结束（不再返回 tool_calls）。这是原生工具循环的核心，
            # 不能因为“这一批都成功了”就跳过后面的步骤。
            return "decide"
        return "respond"

    def _respond(self, state: ReviewState) -> dict[str, Any]:
        return {}

    @staticmethod
    def _blocked_reason(action: str, observations: list[dict[str, Any]]) -> str | None:
        for dependency in ACTION_DEPENDENCIES.get(action, ()):
            for item in observations:
                if item.get("action") == dependency and item.get("status") != "success":
                    return (
                        f"前置步骤“{_action_label(dependency)}”未成功，"
                        f"已跳过“{_action_label(action)}”。"
                    )
        if action in _SINGLE_USE_ACTIONS:
            for item in observations:
                if item.get("action") == action:
                    return f"“{_action_label(action)}”本轮已经处理过，未重复执行。"
        return None