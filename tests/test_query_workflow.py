"""LangGraph 分支：谁调用检索、谁不调用，以及结果如何组织。"""

import asyncio
from datetime import date
from unittest.mock import Mock

import pytest

from app.papers.courses import CANONICAL_COURSE, COURSE_FAMILY
from app.papers.errors import CatalogUnavailableError
from app.papers.models import PaperItem, PaperListResponse
from app.papers.service import PaperService
from app.querying.errors import QueryError
from app.querying.models import Understanding
from app.querying.workflow import build_workflow
from tests.fakes import FakeUnderstander, understanding


def paper_item(paper_id: str, exam_type: str) -> PaperItem:
    return PaperItem(
        id=paper_id,
        title=f"样例 {paper_id}",
        course=CANONICAL_COURSE,
        academic_year="2024-2025",
        exam_type=exam_type,
        exam_date=date(2025, 1, 16),
        download_url=f"/api/papers/{paper_id}/download",
    )


def run_workflow(
    understander: FakeUnderstander,
    papers: PaperService,
    query: str = "我要复习高数",
) -> tuple[dict, list[tuple[str, str]]]:
    progress: list[tuple[str, str]] = []

    async def emit(stage: str, message: str) -> None:
        progress.append((stage, message))

    async def go() -> dict:
        workflow = build_workflow(understander, papers, emit)
        return await workflow.ainvoke({"query": query})

    return asyncio.run(go()), progress


def test_search_calls_paper_service_and_returns_found() -> None:
    papers = Mock(spec=PaperService)
    papers.list_papers.return_value = PaperListResponse(
        items=[
            paper_item("math-bi-20250116-final", "final"),
            paper_item("math-bi-20221107-midterm", "midterm"),
        ],
        total=2,
    )

    state, progress = run_workflow(FakeUnderstander([understanding()]), papers)

    papers.list_papers.assert_called_once_with(course="高数", exam_type=None)
    result = state["result"]
    assert result.kind == "found"
    assert result.total == 2
    assert result.filters.course == COURSE_FAMILY
    assert result.filters.exam_type is None
    assert [item.id for item in result.items] == [
        "math-bi-20250116-final",
        "math-bi-20221107-midterm",
    ]
    assert "2 份" in result.message
    assert progress == [
        ("understanding", "正在理解复习需求"),
        ("retrieving", "正在查找真实试卷"),
    ]


def test_search_with_exam_type_passes_filter() -> None:
    papers = Mock(spec=PaperService)
    papers.list_papers.return_value = PaperListResponse(
        items=[paper_item("math-bi-20250116-final", "final")],
        total=1,
    )

    state, _ = run_workflow(
        FakeUnderstander([understanding(exam_type="final")]),
        papers,
    )

    papers.list_papers.assert_called_once_with(course="高数", exam_type="final")
    assert state["result"].filters.exam_type == "final"


def test_unknown_course_returns_empty_not_alias() -> None:
    papers = Mock(spec=PaperService)
    papers.list_papers.return_value = PaperListResponse(items=[], total=0)

    state, _ = run_workflow(
        FakeUnderstander([understanding(course="大学化学")]),
        papers,
    )

    papers.list_papers.assert_called_once_with(course="大学化学", exam_type=None)
    result = state["result"]
    assert result.kind == "empty"
    assert result.total == 0
    assert result.filters.course == "大学化学"


def test_explicit_course_version_is_not_replaced() -> None:
    papers = Mock(spec=PaperService)
    papers.list_papers.return_value = PaperListResponse(items=[], total=0)

    state, _ = run_workflow(
        FakeUnderstander([understanding(course="高等数学 AⅠ")]),
        papers,
    )

    papers.list_papers.assert_called_once_with(course="高等数学 AⅠ", exam_type=None)
    assert state["result"].kind == "empty"
    assert state["result"].filters.course == "高等数学"


def test_clarification_does_not_touch_catalog() -> None:
    papers = Mock(spec=PaperService)

    state, progress = run_workflow(
        FakeUnderstander([understanding(action="clarify", course=None, reason="missing_course")]),
        papers,
    )

    papers.list_papers.assert_not_called()
    result = state["result"]
    assert result.kind == "clarification"
    assert result.filters is None
    assert result.items == []
    assert result.total == 0
    assert "补充" in result.message
    assert progress == [("understanding", "正在理解复习需求")]


def test_ambiguous_course_is_clarification() -> None:
    papers = Mock(spec=PaperService)

    state, _ = run_workflow(
        FakeUnderstander(
            [understanding(action="clarify", course=None, reason="ambiguous_course")]
        ),
        papers,
    )

    papers.list_papers.assert_not_called()
    assert state["result"].kind == "clarification"
    assert "一门课程" in state["result"].message


def test_unsupported_constraint_does_not_touch_catalog() -> None:
    papers = Mock(spec=PaperService)

    state, _ = run_workflow(
        FakeUnderstander(
            [
                understanding(
                    action="unsupported",
                    course="高数",
                    reason="unsupported_constraint",
                )
            ]
        ),
        papers,
    )

    papers.list_papers.assert_not_called()
    assert state["result"].kind == "unsupported"
    assert "年份" in state["result"].message


def test_out_of_scope_does_not_touch_catalog() -> None:
    papers = Mock(spec=PaperService)

    state, _ = run_workflow(
        FakeUnderstander(
            [
                understanding(
                    action="unsupported",
                    course="高数",
                    reason="out_of_scope",
                )
            ]
        ),
        papers,
    )

    papers.list_papers.assert_not_called()
    assert state["result"].kind == "unsupported"
    assert "复习计划" in state["result"].message


def test_catalog_failure_is_not_swallowed() -> None:
    papers = Mock(spec=PaperService)
    papers.list_papers.side_effect = CatalogUnavailableError("目录坏了")

    with pytest.raises(CatalogUnavailableError):
        run_workflow(FakeUnderstander([understanding()]), papers)


def test_understanding_failure_is_not_swallowed() -> None:
    papers = Mock(spec=PaperService)

    with pytest.raises(QueryError) as excinfo:
        run_workflow(
            FakeUnderstander([QueryError("MODEL_TIMEOUT", "超时")]),
            papers,
        )

    assert excinfo.value.code == "MODEL_TIMEOUT"
    papers.list_papers.assert_not_called()


def test_state_keeps_only_request_scoped_values() -> None:
    papers = Mock(spec=PaperService)
    papers.list_papers.return_value = PaperListResponse(
        items=[paper_item("math-bi-20250116-final", "final")],
        total=1,
    )

    state, _ = run_workflow(
        FakeUnderstander([understanding(exam_type="final")]),
        papers,
    )

    assert set(state) == {"query", "understanding", "result"}
    assert isinstance(state["understanding"], Understanding)
