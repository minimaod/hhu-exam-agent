"""基于 LangGraph 的动态复习对话调度器（F006）。

职责边界：
- 结合本轮输入、持久上下文（已确认课程、当前原题、当前练习、上次建议）及最近对话；
- 动态规划能力调用序列，并在动作执行后读取真实结果，再决定继续、重新决策或回复；
- 有界执行：最多两轮决策、每轮最多三个动作、单轮最多四个实际执行动作；
- 严格遵循交互边界：“我懂了”不自动出题或保存，“好”产生歧义时先澄清，拒绝建议时温和确认不强推；
- 支持复合指令执行与分别反馈；
- 协调查询服务、辅导服务、练习服务与记录服务。
"""

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import DeepSeekSettings
from app.llm import build_chat_model, log_usage, map_provider_error, response_text
from app.papers.courses import detect_course, normalize_course
from app.querying.errors import QueryError
from app.tutoring.prompts import parse_json_object

ActionType = Literal[
    "introduce",
    "query_papers",
    "explain_problem",
    "followup_problem",
    "generate_exercise",
    "evaluate_exercise",
    "reveal_solution",
    "record_mistake",
    "record_feedback",
    "clarify_or_reply",
]

ActionStatus = Literal["success", "failed", "clarification"]

MAX_SCHEDULER_ROUNDS = 2
MAX_ACTIONS_PER_DECISION = 3
MAX_EXECUTED_ACTIONS = 4


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionType
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=200)


class ScheduleDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed_course: str | None = None
    actions: list[ActionPlan] = Field(min_length=1, max_length=3)
    assistant_reply: str | None = Field(default=None, max_length=3000)
    suggested_actions: list[str] = Field(default_factory=list, max_length=5)


SCHEDULER_SYSTEM_PROMPT = """\\
你是河海大学考试复习助手的调度核心。你负责根据学生本轮发言、当前持久上下文、最近对话以及已经执行过的动作结果，决定下一步要执行的 1~3 个业务能力动作。

可用动作清单（action）：
1. introduce：学生提出宽泛的复习需求（如“我要复习 Python”“我想复习线代”“我要复习转专业考试”），确认课程，介绍查试卷、题目辅导、按需练习和错题整理能力，并建议下一步。转专业是课程，不是考试类型。
2. query_papers：学生要求查找、下载试卷或真题（例如“查看高等数学试卷”“查看 Python 试卷”“给我期中试卷”“给我期末试卷”）。未指定考试类型时查全部类型，不默认期末。
3. explain_problem：学生输入了新题目，或对当前题目补充题干/纠正题干，需要讲解。
4. followup_problem：学生针对当前正在辅导的题目解答提问追问（例如“第二步为什么这么做”）。
5. generate_exercise：学生明确要求练题（例如“来一道类似题”“出道题练练”“再来一道”）。
6. evaluate_exercise：学生提交对当前练习题的答案或解题过程（例如“我的答案是 1”“选 C”）。
7. reveal_solution：学生主动要求查看当前练习题的参考答案（例如“直接看答案”“看解析”）。
8. record_mistake：学生明确要求保存当前题目到错题本（例如“先存起来”“记录这题”“加入错题本”）。
9. record_feedback：学生表示听懂了、理解了、谢谢（例如“我懂了”“明白了”“清楚了”）。
10. clarify_or_reply：针对歧义进行澄清，或温和回应拒绝（例如用户说“不用了”时确认，或多个选项后只说“好”时澄清），或纯寒暄。

严格守则（必须绝对遵守）：
1. “我懂了”“明白了”“听懂了”：只输出 action="record_feedback"，绝对不能自动附带 generate_exercise（出题）或 record_mistake（保存错题）！
2. 歧义消除：如果助手上次给出了多个可选建议（如练题或保存），学生仅回答“好”“可以”且未指定动作时，必须输出 action="clarify_or_reply"，并在 assistant_reply 中简要询问“您是想先做练习题还是保存到错题本？”，绝对不能擅自决定出题或保存！
3. 拒绝建议：学生说“不用了”“不需要”“算了”时，输出 action="clarify_or_reply"，确认不执行，不再强推。
4. 复合指令：学生明确要求两个操作（如“保存这题，再来一道类似题”），请依次输出两个动作：record_mistake，然后 generate_exercise。
5. 上下文继承：如果上下文已有已确认课程（如 Python），学生随后说“给我期末试卷”，action="query_papers"，parameters 中带上 course="Python", exam_type="final"。
6. 没有当前题目时，不能执行 followup_problem, generate_exercise, record_mistake, record_feedback。
7. 区分题目修改方式：开始一道完全不同的新题时，explain_problem 的 parameters 带 "mode": "new"；给尚未讲清的题目补充信息时带 "mode": "supplement"；纠正已讲解题目的题干时带 "mode": "correction"。不写 mode 时程序会保护旧版本，绝不原地覆盖已讲解题。
8. 已经执行结果：如果“已执行结果”里某个动作失败或需要用户补充，不要重复执行同一个动作，也不要执行依赖它成功的动作（例如纠正失败后不要再出题）。此时应输出 clarify_or_reply，如实说明失败或改为等待用户补充。
9. 单轮最多规划 3 个动作；任何需要学生作答或补充的动作执行后会结束本轮，不要继续规划后续动作。
10. 课程名称：confirmed_course 必须是学生本轮说出的课程或已确认课程（如“高等数学”“Python”“转专业”），不能写成“通识选修课程”“专业课”“英语类”等类别。学生说“我要复习转专业考试”时，confirmed_course 为“转专业”。学生已明确说出课程时，assistant_reply 不得再问“要复习哪门课程”，并应在 suggested_actions 中给出“查看<课程>试卷”。
11. 考试类型必须依据学生本轮要求：未提期中/期末/其他类型时，query_papers.parameters.exam_type 为 null；“查看高等数学试卷”“查看 Python 试卷”“找全部试卷”都不能擅自加 final。明确期中为 midterm，期末为 final，其他类型为 other；期中期末都要时为 null。课程可以继承，考试类型不默认为上一轮类型。
12. 本地目录事实必须来自实际查询结果。能力介绍应说明可查询已登记的期中、期末和其他类型资料；未执行对应查询前，不能声称“只有期末”“期中未登记”或某课程没有资料。用户质疑遗漏期中时，应查询核实，而不是编造缺失原因。

输出要求：只输出一个 JSON 对象，不要任何 Markdown 标记：
{
  "confirmed_course": "若本轮明确或确认了课程则填写（如 Python、高等数学），否则为 null",
  "actions": [
    {"action": "动作名", "parameters": {}, "reason": "一句话理由"}
  ],
  "assistant_reply": "可选的自然语言补充说明，若业务动作自带卡片则可简短或为 null",
  "suggested_actions": ["可选的下一步按钮文字，例如'练一道类似题', '保存到错题本'"]
}
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
    last_suggestions: list[str]
    recent_turns_summary: str
    has_attachments: bool
    rounds: int
    executed_count: int
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
}

_ACTION_LABELS = {
    "introduce": "复习介绍", "query_papers": "查找试卷", "explain_problem": "题目讲解",
    "followup_problem": "回答追问", "generate_exercise": "生成练习", "evaluate_exercise": "评价作答",
    "reveal_solution": "查看解答", "record_mistake": "保存错题", "record_feedback": "记录反馈",
}


class SchedulerAgent:
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
        last_suggestions: list[str] | None = None,
        recent_turns_summary: str | None = None,
        has_attachments: bool = False,
        observations: list[dict[str, Any]] | None = None,
        iteration: int = 1,
    ) -> ScheduleDecision:
        # 纯图片输入没有文字可判断意图：当前练习可作答/待补充时是作答，
        # 否则才是新题或补充题目。不能一律当成 explain_problem，否则会把
        # 图片作答误判成纠正原题。
        if has_attachments and not query.strip():
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

        model = self._model or build_chat_model(
            self._settings,  # type: ignore[arg-type]
            max_tokens=700,
            timeout=25.0,
            json_mode=True,
            http_async_client=self._http_async_client,
        )

        context_parts = []
        detected_course = detect_course(query)
        if detected_course:
            # 模型偶尔会把“转专业考试”之类理解成“通识选修课程”等类别，
            # 这里把确定性识别结果作为约束传给模型。
            context_parts.append(
                f"学生本轮提到的课程：{detected_course}（confirmed_course 必须使用该课程名，"
                "不能改写成“通识选修课程”“专业课”等类别）"
            )
        if confirmed_course:
            context_parts.append(f"已确认复习科目：{confirmed_course}")
        if active_problem_text:
            snippet = active_problem_text[:200] + ("..." if len(active_problem_text) > 200 else "")
            status_text = f"（状态：{active_problem_status}）" if active_problem_status else ""
            context_parts.append(f"当前关注原题{status_text}：{snippet}")
        if active_exercise_statement:
            ex_snippet = active_exercise_statement[:150] + ("..." if len(active_exercise_statement) > 150 else "")
            context_parts.append(f"当前练习题状态：{active_exercise_status}，题干：{ex_snippet}")
        if last_suggestions:
            context_parts.append(f"上次助手提出的建议选项：{', '.join(last_suggestions)}")
        if recent_turns_summary:
            context_parts.append(f"最近对话摘要：\n{recent_turns_summary}")
        if observations:
            lines = []
            for item in observations[-4:]:
                label = "已执行" if item.get("executed", True) else "已跳过"
                lines.append(
                    f"- {item.get('action')}：{label}，状态 {item.get('status')}，说明：{item.get('message')}"
                )
            context_parts.append("已执行结果（本轮）：\n" + "\n".join(lines))
        if iteration > 1:
            context_parts.append(f"注意：这是第 {iteration} 次决策，前面的动作已经执行，不要重复失败动作。")

        context_text = "\n".join(context_parts) if context_parts else "（当前无特殊上下文）"

        human_prompt = (
            f"【当前业务上下文】：\n{context_text}\n\n"
            f"【学生本轮发言】：\n{query}\n\n"
            "请分析意图并规划下一步动作，输出 JSON。"
        )

        messages = [
            {"role": "system", "content": SCHEDULER_SYSTEM_PROMPT},
            {"role": "user", "content": human_prompt},
        ]

        try:
            response = await model.ainvoke(messages)
            log_usage("scheduler_plan", response)
            raw = response_text(response)
            payload = parse_json_object(raw)
            decision = ScheduleDecision.model_validate(payload)
            if (
                detected_course
                and normalize_course(decision.confirmed_course) != detected_course
            ):
                decision.confirmed_course = detected_course
            return decision
        except ValidationError as exc:
            raise QueryError(
                "MODEL_RESPONSE_INVALID",
                "调度模型返回的规划不符合约定，请重试。",
            ) from exc
        except Exception as exc:
            mapped = map_provider_error(exc)
            if mapped:
                raise mapped from exc
            raise QueryError("MODEL_UNAVAILABLE", "会话调度服务暂时不可用，请稍后重试。") from exc


class DynamicReviewGraph:
    """有界的“决策 → 执行 → 观察 → 再决策或回复”LangGraph。

    图只负责控制流和依赖判断；具体业务动作、持久化和 SSE 进度由注入的
    ``execute_action`` 完成。执行失败是正常观察结果，不会被吞成成功。
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
            {"execute": "execute", "respond": "respond"},
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
            # 恢复已批准的计划，不重新决策；最多保留原两轮的六个计划步骤。
            state = dict(initial)
            state["pending"] = self._resume_actions[:MAX_SCHEDULER_ROUNDS * MAX_ACTIONS_PER_DECISION]
            while state.get("pending"):
                state.update(await self._execute(state))
                if any(item.get("needs_user") for item in state.get("observations", [])):
                    break
            return state
        return await self._graph.ainvoke(initial)

    async def _decide(self, state: ReviewState) -> dict[str, Any]:
        rounds = int(state.get("rounds", 0)) + 1
        if rounds > self._max_rounds:
            return {"rounds": rounds, "pending": []}

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

        pending = list(decision.actions[:MAX_ACTIONS_PER_DECISION])
        return {
            "rounds": rounds,
            "pending": pending,
            "assistant_reply": decision.assistant_reply,
            "suggested_actions": list(decision.suggested_actions),
            "confirmed_course": decision.confirmed_course or state.get("confirmed_course"),
            "replan_error": None,
        }

    async def _execute(self, state: ReviewState) -> dict[str, Any]:
        pending = list(state.get("pending") or [])
        observations = list(state.get("observations") or [])
        executed_count = int(state.get("executed_count", 0))
        if not pending:
            return {}

        action = pending[0]
        blocked = self._blocked_reason(action.action, observations)
        if blocked is None and executed_count >= self._max_actions:
            blocked = "本轮动作数量已达上限，该步骤没有执行。"

        if blocked is not None:
            outcome = ActionOutcome(
                action=action.action,
                status="failed",
                message=blocked,
                details={"skipped": True, "parameters": action.parameters},
                executed=False,
            )
            return {
                "pending": pending[1:],
                "observations": observations + [outcome.to_dict()],
            }

        result: ExecutionResult = await self._execute_action(action, dict(state))
        patch: dict[str, Any] = {
            "pending": [] if result.outcome.needs_user else pending[1:],
            "observations": observations + [result.outcome.to_dict()],
            "executed_count": executed_count + 1,
        }
        patch.update(result.context)
        return patch

    def _after_decide(self, state: ReviewState) -> str:
        if state.get("pending"):
            # 即使已达执行上限，也进入 execute 把剩余动作标记为 skipped，
            # 避免回复把未执行的动作当成已完成。
            return "execute"
        return "respond"

    def _after_execute(self, state: ReviewState) -> str:
        observations = list(state.get("observations") or [])
        if any(item.get("needs_user") for item in observations):
            return "respond"
        if state.get("pending"):
            return "execute"
        if state.get("replan_error"):
            return "respond"
        has_problem = any(item.get("status") != "success" for item in observations)
        if has_problem and int(state.get("rounds", 0)) < self._max_rounds:
            # 失败后重新决策；澄清本身已经要求用户回答，不再额外调用模型。
            if any(item.get("status") == "clarification" for item in observations):
                return "respond"
            return "decide"
        return "respond"

    def _respond(self, state: ReviewState) -> dict[str, Any]:
        return {}

    @staticmethod
    def _blocked_reason(action: str, observations: list[dict[str, Any]]) -> str | None:
        for dependency in ACTION_DEPENDENCIES.get(action, ()):
            for item in observations:
                if item.get("action") == dependency and item.get("status") != "success":
                    return f"前置步骤“{_ACTION_LABELS.get(dependency, '相关操作')}”未成功，已跳过“{_ACTION_LABELS.get(action, '后续操作')}”。"
        if action in _SINGLE_USE_ACTIONS:
            for item in observations:
                if item.get("action") == action:
                    return f"“{_ACTION_LABELS.get(action, '该操作')}”本轮已经处理过，未重复执行。"
        return None
