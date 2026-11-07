"""S1 分页：首页、续页、列完、显式 offset 与冲突参数。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.conversations.tools import parse_tool_call
from tests.test_f008_ux_repair import (
    PAPER,
    build_service,
    result_of,
    run,
    search_decision,
    seed_page,
    turn_input,
)


def test_continue_flag_survives_tool_parsing() -> None:
    plan, rejection = parse_tool_call(
        "search_questions",
        {"course": "Python", "continue_from_previous": True},
        tool_call_id="call-1",
    )
    assert rejection is None and plan is not None
    assert plan.parameters["continue_from_previous"] is True


def test_offset_and_continue_are_mutually_exclusive() -> None:
    plan, rejection = parse_tool_call(
        "search_questions",
        {"course": "Python", "offset": 5, "continue_from_previous": True},
        tool_call_id="call-2",
    )
    assert plan is None
    assert rejection is not None and rejection.code == "INVALID_ARGUMENTS"


@pytest.mark.anyio
async def test_first_page_then_continue_then_exhausted(tmp_path: Path) -> None:
    from tests.tutoring_fakes import FakeScheduler

    scheduler = FakeScheduler(decisions=[search_decision(
        paper_id=PAPER, question_type="choice", limit=5,
    )])
    service, _, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()

    first = result_of(await run(
        service, conversation.id, turn_input(text="第一页选择题")
    ))
    request = runtime.search_calls[0]["request"]
    assert (request.offset, request.limit) == (0, 5)
    assert first.composite_results[0].details["next_offset"] == 5

    scheduler.push(search_decision(
        continue_from_previous=True, question_type="choice", limit=5,
    ))
    second = result_of(await run(
        service, conversation.id, turn_input(text="继续")
    ))
    request = runtime.search_calls[-1]["request"]
    assert request.offset == 5
    assert [card.number_label for card in second.question_results] == ["6", "7", "8", "9", "10"]


@pytest.mark.anyio
async def test_continue_after_exhaustion_does_not_restart(tmp_path: Path) -> None:
    from tests.tutoring_fakes import FakeScheduler

    scheduler = FakeScheduler(decisions=[search_decision(
        continue_from_previous=True,
    )])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    seed_page(
        repository, conversation.id, offset=0, returned=40, next_offset=None,
    )

    result = result_of(await run(
        service, conversation.id, turn_input(text="继续")
    ))
    assert runtime.search_calls == []
    assert "已经全部列完" in result.composite_results[0].message


@pytest.mark.anyio
async def test_explicit_offset_is_used_verbatim(tmp_path: Path) -> None:
    from tests.tutoring_fakes import FakeScheduler

    scheduler = FakeScheduler(decisions=[search_decision(
        paper_id=PAPER, question_type="choice", offset=10, limit=5,
    )])
    service, _, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()

    result = result_of(await run(
        service, conversation.id, turn_input(text="从第 11 道开始")
    ))
    request = runtime.search_calls[0]["request"]
    assert request.offset == 10
    assert [card.number_label for card in result.question_results] == ["11", "12", "13", "14", "15"]


@pytest.mark.anyio
async def test_continue_with_changed_scope_is_rejected(tmp_path: Path) -> None:
    from tests.tutoring_fakes import FakeScheduler

    scheduler = FakeScheduler(decisions=[search_decision(
        paper_id=PAPER, question_type="choice", limit=5,
    )])
    service, _, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    await run(service, conversation.id, turn_input(text="第一页选择题"))
    calls = list(runtime.search_calls)

    # 学生换题型却仍说“继续”：不能拿旧页码查新题型。
    scheduler.push(search_decision(
        continue_from_previous=True, question_type="fill", limit=5,
    ))
    result = result_of(await run(
        service, conversation.id, turn_input(text="继续看填空题")
    ))
    assert runtime.search_calls == calls
    assert result.business_status == "needs_input"
    assert "条件已经变化" in result.composite_results[0].message