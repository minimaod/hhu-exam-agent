"""LangGraph 工作流：理解 → 选择分支 → 检索或提示 → 结果。

模型只负责理解条件；检索、消息、数量与下载入口全部由程序根据真实结果生成。
clarify / unsupported 分支不会访问试卷目录，避免“先查全部再让模型挑”。
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from app.papers.courses import normalize_course
from app.papers.models import ExamType
from app.papers.service import PaperService
from app.querying.models import (
    ProgressStage,
    QueryFilters,
    QueryState,
    ResultEvent,
    Understanding,
    Understander,
)

ProgressEmitter = Callable[[ProgressStage, str], Awaitable[None]]

_CLARIFY_MESSAGES = {
    "missing_course": "请补充要复习的课程，例如“高数”。",
    "ambiguous_course": "本次一次只查询一门课程，请提供单门课程的完整需求。",
}

_UNSUPPORTED_MESSAGES = {
    "unsupported_constraint": "当前只支持按课程和期中/期末查询试卷，暂不支持年份、学年等筛选条件。",
    "out_of_scope": "当前支持查询试卷和下载原文件；复习计划、题型分析等功能尚未实现。",
}

_PROGRESS = {
    "understanding": "正在理解复习需求",
    "retrieving": "正在查找真实试卷",
}


def _exam_label(exam_type: ExamType | None) -> str:
    if exam_type == "midterm":
        return "期中试卷"
    if exam_type == "final":
        return "期末试卷"
    return "试卷"


def _search_result(understanding: Understanding, response: Any) -> ResultEvent:
    """把真实检索结果转成对外结果；课程使用 F002 的别名归一规则。"""

    assert understanding.course is not None
    course = normalize_course(understanding.course) or understanding.course
    filters = QueryFilters(course=course, exam_type=understanding.exam_type)
    label = _exam_label(understanding.exam_type)

    if response.total == 0:
        return ResultEvent(
            kind="empty",
            message=f"已查询“{course}”，本地目录中暂时没有匹配的{label}。",
            filters=filters,
            items=[],
            total=0,
        )

    return ResultEvent(
        kind="found",
        message=f"已按“{course}”找到 {response.total} 份{label}，下载入口来自本地已登记的原文件。",
        filters=filters,
        items=response.items,
        total=response.total,
    )


def _non_search_result(
    understanding: Understanding,
    kind: Literal["clarification", "unsupported"],
) -> ResultEvent:
    if kind == "clarification":
        message = _CLARIFY_MESSAGES.get(
            understanding.reason or "",
            "请补充要复习的课程。",
        )
    else:
        message = _UNSUPPORTED_MESSAGES.get(
            understanding.reason or "",
            "当前还不支持这个需求。",
        )

    return ResultEvent(
        kind=kind,
        message=message,
        filters=None,
        items=[],
        total=0,
    )


def build_workflow(
    understand: Understander,
    papers: PaperService,
    emit: ProgressEmitter,
) -> Any:
    """按依赖组装一次请求的工作流。

    图是每次请求现建的：状态里只有问题、理解和结果，模型、服务与进度回调
    通过闭包传入，不进入对外状态，也不需要长期 checkpointer。
    """

    async def understand_node(state: QueryState) -> dict[str, Understanding]:
        await emit("understanding", _PROGRESS["understanding"])
        understanding = await understand(state["query"])
        return {"understanding": understanding}

    async def retrieve_node(state: QueryState) -> dict[str, ResultEvent]:
        await emit("retrieving", _PROGRESS["retrieving"])
        understanding = state["understanding"]
        response = papers.list_papers(
            course=understanding.course,
            exam_type=understanding.exam_type,
        )
        return {"result": _search_result(understanding, response)}

    async def clarify_node(state: QueryState) -> dict[str, ResultEvent]:
        return {"result": _non_search_result(state["understanding"], "clarification")}

    async def unsupported_node(state: QueryState) -> dict[str, ResultEvent]:
        return {"result": _non_search_result(state["understanding"], "unsupported")}

    def route(state: QueryState) -> str:
        return state["understanding"].action

    graph = StateGraph(QueryState)
    graph.add_node("understand", understand_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("clarify", clarify_node)
    graph.add_node("unsupported", unsupported_node)

    graph.add_edge(START, "understand")
    graph.add_conditional_edges(
        "understand",
        route,
        {
            "search": "retrieve",
            "clarify": "clarify",
            "unsupported": "unsupported",
        },
    )
    graph.add_edge("retrieve", END)
    graph.add_edge("clarify", END)
    graph.add_edge("unsupported", END)

    return graph.compile()
