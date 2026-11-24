"""F017 Fix-005 定向回归测试：查卷去重、PDF 解耦、单题直达与题型校验一致。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.reply import (
    ReplyMaterial,
    ReplyResult,
    material_from_payload,
    material_to_payload,
)
from app.materials.pdf_parser import run_isolated_pdf_parse
from app.papers.models import PaperItem, PaperListResponse
from tests.tutoring_fakes import sample_explanation
from tests.test_question_turns import (
    FakeQuestionRuntime,
    FakeScheduler,
    FakeTutor,
    add_search_turn,
    build_service,
    run,
    sample_card,
    turn_input,
)


class _FakePaperService:
    def __init__(self, items: list[PaperItem]) -> None:
        self._items = items

    def list_papers(self, *, course: str | None = None, exam_type: str | None = None) -> PaperListResponse:
        filtered = [
            item
            for item in self._items
            if (course is None or item.course == course)
            and (exam_type is None or item.exam_type == exam_type)
        ]
        return PaperListResponse(items=filtered, total=len(filtered))


def test_reply_material_carries_has_paper_cards_across_serialization() -> None:
    material = ReplyMaterial(
        user_text="查高数期末真题",
        course="高等数学",
        results=(
            ReplyResult(
                action="query_papers",
                status="success",
                message="找到 2 份【高等数学】试卷（期末）。",
                details={"total": 2, "result_cards_rendered": True},
            ),
        ),
        has_paper_cards=True,
    )
    assert "查卷结果卡片已展示" in material.describe()
    assert "查卷结果卡片已展示" in material.describe_context()
    restored = material_from_payload(material_to_payload(material))
    assert restored.has_paper_cards is True
    assert "严禁生成试卷表格" in restored.describe()


@pytest.mark.anyio
async def test_query_papers_sets_result_cards_rendered_flag(tmp_path: Path) -> None:
    scheduler = FakeScheduler(
        decisions=[
            ScheduleDecision(
                actions=[
                    ActionPlan(
                        action="query_papers",
                        parameters={"course": "高等数学", "exam_type": "final"},
                    )
                ]
            )
        ]
    )
    papers = _FakePaperService(
        [
            PaperItem(
                id="math-final-1",
                title="2024-2025高数AII期末考试试题",
                course="高等数学",
                academic_year="2024-2025",
                exam_type="final",
                exam_date=None,
                download_url="/api/papers/math-final-1/download",
            )
        ]
    )
    service, repository, _ = build_service(tmp_path, scheduler=scheduler)
    service._papers = papers  # type: ignore[assignment]
    conv = service.create_conversation()
    request = turn_input(kind=None, text="查高数期末真题", dispatch=True)
    events = await run(service, conv.id, request)
    result = next(event for event in events if event.type == "result")
    assert result.result is not None
    assert result.result.kind == "found"
    assert len(result.result.items) == 1
    assert result.assistant_message == result.result.message
    assert result.reply_status == "complete"
    assert not any(event.type == "message_delta" for event in events)
    turn = repository.find_turn(conv.id, request.request_id)
    assert turn is not None and turn.assistant_message == result.result.message
    assert service._paper_lookup_reply(
        turn.result, "查高数期末真题并推荐一份", turn.composite_results,
    ) is None
    assert service._paper_lookup_reply(
        turn.result, "查高数期末真题，顺便讲一下等价无穷小", turn.composite_results,
    ) is None
    regenerated = await service.regenerate_reply(conv.id, request.request_id)
    assert regenerated["assistant_message"] == result.result.message
    assert regenerated["reply_status"] == "complete"
    assert result.composite_results[0].details["papers"][0]["paper_id"] == "math-final-1"
    assert result.composite_results[0].details.get("result_cards_rendered") is True

    # 明确要求推荐时保留 papers 供正文给出推荐理由
    scheduler.push(
        ScheduleDecision(
            actions=[
                ActionPlan(
                    action="query_papers",
                    parameters={"course": "高等数学", "exam_type": "final"},
                )
            ]
        )
    )
    events_rec = await run(service, conv.id, turn_input(kind=None, text="查高数期末真题，推荐一份并说明理由", dispatch=True))
    result_rec = next(event for event in events_rec if event.type == "result")
    assert len(result_rec.composite_results[0].details.get("papers") or []) == 1


@pytest.mark.anyio
async def test_single_question_search_auto_selects_without_auto_explain(tmp_path: Path) -> None:
    scheduler = FakeScheduler(
        decisions=[
            ScheduleDecision(
                actions=[
                    ActionPlan(
                        action="search_questions",
                        parameters={"course": "Python", "query": "列表"},
                    )
                ]
            )
        ]
    )
    runtime = FakeQuestionRuntime(cards=[sample_card("py-single-1", number="1")])
    service, repository, _ = build_service(tmp_path, runtime=runtime, scheduler=scheduler)
    conv = service.create_conversation()
    events = await run(service, conv.id, turn_input(kind=None, text="来一道列表真题", dispatch=True))
    result = next(event for event in events if event.type == "result")
    assert result.question is not None
    assert result.question.question_id == "py-single-1"
    assert result.question.selected is True
    active = repository.get_active_question(conv.id)
    assert active is not None and active.question_id == "py-single-1"


@pytest.mark.anyio
async def test_solve_question_and_choice_without_options_accept_text_answer(tmp_path: Path) -> None:
    solve_card = sample_card("math-solve-1", number="1").model_copy(
        update={"question_type": "solve", "options": []}
    )
    choice_no_opts = sample_card("py-choice-no-opts", number="2").model_copy(
        update={"question_type": "choice", "options": []}
    )
    choice_with_opts = sample_card("py-choice-opts", number="3")
    runtime = FakeQuestionRuntime(cards=[solve_card, choice_no_opts, choice_with_opts])
    service, repository, _ = build_service(
        tmp_path,
        runtime=runtime,
        tutor=FakeTutor(explanations=[sample_explanation("1/2"), sample_explanation("B")]),
    )
    conv = service.create_conversation()
    source = add_search_turn(repository, conv.id, [solve_card, choice_no_opts, choice_with_opts])

    # 解答题提交文本答案应放行
    req_solve = replace(
        turn_input(
            kind="explain_question",
            text="我的答案是：1/2，请核对并讲解。",
            question_id=solve_card.question_id,
            source_turn=source,
        ),
        student_answer_text="1/2",
    )
    events_solve = await run(service, conv.id, req_solve)
    assert any(event.type == "result" for event in events_solve)

    # 缺结构化选项的选择题提交原卷选项答案应放行
    req_no_opts = replace(
        turn_input(
            kind="explain_question",
            text="我的答案是：B，请核对并讲解。",
            question_id=choice_no_opts.question_id,
            source_turn=source,
        ),
        student_answer_text="B",
    )
    events_no_opts = await run(service, conv.id, req_no_opts)
    assert any(event.type == "result" for event in events_no_opts)

    # 完整选择题提交非法选项仍应被拦截
    req_bad = replace(
        turn_input(
            kind="explain_question",
            text="这道题我选 Z",
            question_id=choice_with_opts.question_id,
            source_turn=source,
        ),
        student_answer_text="Z",
    )
    with pytest.raises(Exception, match="请选择这道选择题实际提供的选项"):
        await run(service, conv.id, req_bad)


def test_real_gaoshu_final_pdf_parses_without_pipe_deadlock() -> None:
    pdf_path = Path("data/papers/高数试卷/2024-2025高数AII期末考试试题.pdf")
    if not pdf_path.exists():
        pytest.skip("高数原件 PDF 不在当前测试环境")
    page_only = run_isolated_pdf_parse(pdf_path, max_pages=60, timeout_seconds=10.0)
    assert page_only.page_count == 2
    assert page_only.pages == []
    extracted = run_isolated_pdf_parse(
        pdf_path, max_pages=60, start_page=1, end_page=2, timeout_seconds=10.0
    )
    assert len(extracted.pages) == 2
    assert all(page.char_count > 0 for page in extracted.pages)
