"""F010 B：只读工具（工作区、详情、已展示列表、学习摘要）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.models import Turn
from app.questions.cards import attach_answer
from app.questions.models import AnswerDetail, EvidenceBundle
from tests.question_fakes import sample_card
from tests.test_f010b_support import (
    dummy_request,
    make_harness,
    raw_turn_row,
    seed_question_turn,
    transcript_of,
    turn_input,
)


def decision(*actions: ActionPlan) -> ScheduleDecision:
    return ScheduleDecision(
        confirmed_course="Python语言程序设计", actions=list(actions)
    )


async def run_turn(harness, conversation_id: str, text: str):
    prepared = harness.service.prepare_turn(conversation_id, turn_input(text))
    events = [
        event async for event in harness.service.stream_prepared(prepared, dummy_request())
    ]
    return prepared, events


def answer_card():
    card = sample_card(statement="下列哪个是 Python 的列表？", answer_text="B")
    return attach_answer(
        card,
        EvidenceBundle(
            status="ok",
            answer_detail=AnswerDetail(state="original", text="B（合成标记 F010B-ANSWER-A）"),
        ),
    )


@pytest.mark.anyio
async def test_question_detail_is_public_only(tmp_path: Path) -> None:
    card = answer_card()
    harness = make_harness(tmp_path, cards=[card])
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [card])
    harness.scheduler.push(decision(ActionPlan(action="view_question_detail", parameters={"ref": "Q1"})))

    prepared, events = await run_turn(harness, conversation, "刚才第一题考什么")
    result = next(event for event in events if event.type == "result")
    assert result.composite_results[0].status == "success"
    details = result.composite_results[0].details
    assert details["question_id"] == card.question_id
    assert details["build_id"] == card.build_id
    assert set(details["answer"]) == {"available", "state"}
    assert "answer_detail" not in details and "reference_code" not in details
    assert "F010B-ANSWER-A" not in json.dumps(details, ensure_ascii=False)
    assert details["from_snapshot"] is True

    row = raw_turn_row(harness.db, conversation, prepared.request_id)
    steps = transcript_of(row)["steps"]
    assert steps[0]["tool"] == "view_question_detail"
    assert "F010B-ANSWER-A" not in json.dumps(steps, ensure_ascii=False)


@pytest.mark.anyio
async def test_unknown_ref_is_failed_and_self_correctable(tmp_path: Path) -> None:
    card = sample_card()
    harness = make_harness(tmp_path, cards=[card])
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [card])
    harness.scheduler.push(decision(ActionPlan(action="view_question_detail", parameters={"ref": "Q99"})))

    _, events = await run_turn(harness, conversation, "看第 99 题")
    result = next(event for event in events if event.type == "result")
    item = result.composite_results[0]
    assert item.status == "failed"
    assert item.details["error_code"] == "INVALID_REF"
    assert result.suggestions is not None  # 轮次正常结束，不强制学生回答


@pytest.mark.anyio
async def test_version_change_refuses_even_if_selection_moved(tmp_path: Path) -> None:
    first = sample_card(question_id="q-1", number="1")
    second = sample_card(question_id="q-2", number="2")
    harness = make_harness(tmp_path, cards=[first, second])
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [first, second])

    # 模拟题库重建：引用绑定的那张卡记录的是旧 build。
    import sqlite3

    with sqlite3.connect(harness.db) as connection:
        raw = connection.execute(
            "SELECT output_json FROM conversation_turns WHERE conversation_id=? AND request_id='req-seed'",
            (conversation,),
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["question_results"][0]["build_id"] = "iv-other-build"
        connection.execute(
            "UPDATE conversation_turns SET output_json=? WHERE conversation_id=? AND request_id='req-seed'",
            (json.dumps(payload, ensure_ascii=False), conversation),
        )

    harness.scheduler.push(decision(ActionPlan(action="view_question_detail", parameters={"ref": "Q1"})))
    _, events = await run_turn(harness, conversation, "再看第一题")
    item = next(event for event in events if event.type == "result").composite_results[0]
    assert item.status == "failed"
    assert item.details["error_code"] == "VERSION_CHANGED"

    # 当前选中题换成另一道，不能代替版本校验：旧引用仍被拒绝。
    harness.scheduler.push(decision(ActionPlan(action="select_question", parameters={"index": 2})))
    await run_turn(harness, conversation, "选第二题")
    harness.scheduler.push(decision(ActionPlan(action="view_question_detail", parameters={"ref": "Q1"})))
    _, events = await run_turn(harness, conversation, "把第一题再给我看看")
    item = next(event for event in events if event.type == "result").composite_results[0]
    assert item.status == "failed" and item.details["error_code"] == "VERSION_CHANGED"


@pytest.mark.anyio
async def test_learning_summary_does_not_switch_course(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    conversation = harness.service.create_conversation().id
    harness.repo.upsert_context(conversation, confirmed_course="Python语言程序设计")
    harness.scheduler.push(
        decision(
            ActionPlan(
                action="view_learning_summary",
                parameters={"course": "高等数学", "days": 30, "limit": 10},
            )
        )
    )

    _, events = await run_turn(harness, conversation, "高数最近学得怎么样")
    result = next(event for event in events if event.type == "result")
    # 没有配置记忆服务：如实失败且可重试，但课程没有被切换。
    assert result.composite_results[0].status == "failed"
    assert harness.repo.get_context(conversation).confirmed_course == "Python语言程序设计"
    assert harness.scheduler.calls[0]["confirmed_course"] == "Python语言程序设计"


@pytest.mark.anyio
async def test_select_by_ref_binds_and_explain_uses_the_same_card(tmp_path: Path) -> None:
    cards = [
        sample_card(question_id=f"fill-{index}", number=str(index)).model_copy(
            update={"question_type": "fill"}
        )
        for index in (1, 2, 3)
    ]
    fifth = sample_card(
        question_id="choice-5", number="5", statement="第五题：这段代码输出什么？"
    )
    cards += [fifth]
    harness = make_harness(tmp_path, cards=cards)
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, cards)

    harness.scheduler.push(
        decision(
            ActionPlan(action="select_question", parameters={"ref": "Q4"}),
            ActionPlan(action="explain_problem", parameters={"mode": "new"}),
        )
    )
    prepared, events = await run_turn(harness, conversation, "刚才那张卷第五题给我讲讲")
    result = next(event for event in events if event.type == "result")
    assert [item.status for item in result.composite_results] == ["success", "success"]
    select = result.composite_results[0].details
    assert select["question_id"] == "choice-5"
    assert select["number_label"] == "5"
    assert select["paper_id"] == fifth.source.paper_id
    assert select["content_sha256"] == fifth.content_sha256
    assert "第五题：这段代码输出什么？" in harness.tutor.explain_calls[0]["statement"]

    steps = transcript_of(raw_turn_row(harness.db, conversation, prepared.request_id))["steps"]
    assert [step["plan"]["action"] for step in steps] == [
        "select_question",
        "explain_problem",
    ]


@pytest.mark.anyio
async def test_same_number_two_types_requires_clarification(tmp_path: Path) -> None:
    choice = sample_card(question_id="choice-5", number="5")
    fill = sample_card(question_id="fill-5", number="5").model_copy(
        update={"question_type": "fill"}
    )
    harness = make_harness(tmp_path, cards=[choice, fill])
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [choice, fill], question_type=None)

    harness.scheduler.push(
        decision(
            ActionPlan(action="select_question", parameters={"ref": "Q1"}),
            ActionPlan(action="explain_problem", parameters={"mode": "new"}),
        )
    )
    _, events = await run_turn(harness, conversation, "第五题给我讲讲")
    result = next(event for event in events if event.type == "result")
    first = result.composite_results[0]
    assert first.status == "clarification"
    assert first.details["error_code"] == "AMBIGUOUS_TARGET"
    assert harness.tutor.explain_calls == []
    # 澄清后没有建立新的选中事实。
    assert harness.repo.get_active_question(conversation) is None