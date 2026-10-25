"""LangGraph 辅导工作流：整理题目 → 判断清晰度 → 澄清或讲解。

图只负责模型步骤，不写数据库；保存、来源、记录和事件由服务层处理。
一次工作流只处理一道题，状态只在本次请求内使用。
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.tutoring.models import Explanation, ImagePayload, ProblemUnderstanding
from app.tutoring.llm import DeepSeekTutor

TutoringStage = Literal[
    "reading_problem",
    "checking_clarity",
    "explaining",
    "answering",
    "recording",
    "updating_record",
]

ProgressEmitter = Callable[[TutoringStage, str], Awaitable[None]]


class TutoringState(TypedDict, total=False):
    text: str
    images: list[ImagePayload]
    student_answer: str | None
    solution_text: str | None
    statement_images: list[ImagePayload]
    solution_images: list[ImagePayload]
    understanding: ProblemUnderstanding
    explanation: Explanation


def build_tutoring_workflow(
    tutor: DeepSeekTutor,
    emit: ProgressEmitter,
) -> Any:
    """按依赖组装一次题目理解/讲解工作流。"""

    async def understand_node(state: TutoringState) -> dict[str, ProblemUnderstanding]:
        await emit("reading_problem", "正在读取题目和图片")
        await emit("checking_clarity", "正在检查题目是否清楚")
        understanding = await tutor.understand_problem(
            state.get("text", ""),
            state.get("statement_images", []),
        )
        return {"understanding": understanding}

    async def explain_node(state: TutoringState) -> dict[str, Explanation]:
        await emit("explaining", "正在整理知识点和解题步骤")
        understanding = state["understanding"]
        explanation = await tutor.explain(
            statement=understanding.statement,
            student_answer=state.get("student_answer"),
            solution_text=state.get("solution_text"),
            statement_images=state.get("statement_images", []),
            solution_images=state.get("solution_images", []),
        )
        return {"explanation": explanation}

    async def clarify_node(state: TutoringState) -> dict[str, Any]:
        # 不调用讲解模型；服务层把 understanding 转成 needs_clarification。
        return {}

    def route(state: TutoringState) -> str:
        understanding = state["understanding"]
        if understanding.clarity == "unclear" or understanding.multiple_problems:
            return "clarify"
        return "explain"

    graph = StateGraph(TutoringState)
    graph.add_node("understand", understand_node)
    graph.add_node("explain", explain_node)
    graph.add_node("clarify", clarify_node)

    graph.add_edge(START, "understand")
    graph.add_conditional_edges(
        "understand",
        route,
        {"clarify": "clarify", "explain": "explain"},
    )
    graph.add_edge("clarify", END)
    graph.add_edge("explain", END)

    return graph.compile()
