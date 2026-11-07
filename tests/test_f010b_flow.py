"""F010 B：跨轮历史的端到端行为（注入、恢复、重新生成、答案边界）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.models import TurnEvent
from tests.f009_support import Seed
from tests.question_fakes import sample_card
from tests.tutoring_fakes import sample_explanation
from tests.test_f010b_support import (
    dummy_request,
    make_harness,
    mistake_rows,
    raw_turn_row,
    seed_question_turn,
    transcript_of,
    turn_input,
)


def decision(*actions: ActionPlan) -> ScheduleDecision:
    return ScheduleDecision(actions=list(actions), confirmed_course="Python语言程序设计")


async def run(harness, conversation_id: str, text: str, *, retry_of: str | None = None):
    prepared = harness.service.prepare_turn(
        conversation_id, turn_input(text, retry_of=retry_of)
    )
    events = [
        event async for event in harness.service.stream_prepared(prepared, dummy_request())
    ]
    return prepared, events


@pytest.mark.anyio
async def test_previous_turn_reaches_decision_but_not_reply_prompt(tmp_path: Path) -> None:
    card = sample_card(statement="下列哪个是列表？")
    harness = make_harness(tmp_path, cards=[card])
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [card], assistant="已列出这份卷的选择题。")

    harness.scheduler.push(decision(ActionPlan(action="view_workspace", parameters={})))
    _, events = await run(harness, conversation, "刚才那张卷是哪份？")
    result = next(event for event in events if event.type == "result")
    assert result.composite_results[0].status == "success"

    call = harness.scheduler.calls[0]
    history = call["history"]
    assert any(
        item["role"] == "assistant" and "已列出这份卷的选择题。" in (item.get("content") or "")
        for item in history
    )
    assert (call.get("workspace_text") or "").startswith("课程：")
    assert "2024-2025 学年第一学期 Python 期末试卷" in call["workspace_text"]

    # 正文模型只拿业务材料，不拿历史消息（决策/正文分离）。
    reply_prompt = json.dumps(
        [call for call in harness.reply.models[0].calls], ensure_ascii=False
    )
    assert "已列出这份卷的选择题。" not in reply_prompt


@pytest.mark.anyio
async def test_crash_after_commit_retries_without_duplicate_mistake(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    seed = Seed(harness.db)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation,
        statement="求列表中出现次数最多的元素",
        paper_id="py-2024-2025-s1-final",
        source_type="registered_paper",
    )
    with sqlite3.connect(harness.db) as connection:
        connection.execute(
            "UPDATE problems SET explanation_json=? WHERE id=?",
            (
                json.dumps(
                    sample_explanation().model_dump(mode="json"), ensure_ascii=False
                ),
                problem,
            ),
        )
    harness.repo.upsert_context(
        conversation,
        confirmed_course="Python语言程序设计",
        active_problem_id=problem,
    )
    harness.scheduler.push(
        decision(
            ActionPlan(action="explain_problem", parameters={"mode": "new"}),
            ActionPlan(action="record_mistake", parameters={}),
            ActionPlan(action="view_workspace", parameters={}),
        )
    )

    # 故障注入：讲解与保存都已提交，轮到“读工作区”时进程消失。
    original = harness.repo.save_recovery

    def flaky(*args, **kwargs):
        state = args[2]
        actions = state.get("actions") or []
        pending = state.get("pending") or []
        # 讲解、保存都已成功，轮到“读工作区”的日志写入时中断。
        if (
            len(actions) == 3
            and actions[-1].get("plan", {}).get("action") == "view_workspace"
            and actions[-1].get("outcome") is None
        ):
            raise RuntimeError("模拟进程中断")
        return original(*args, **kwargs)

    harness.repo.save_recovery = flaky  # type: ignore[method-assign]
    text = "讲讲这道题，然后保存，再看看工作区"
    prepared, _ = await run(harness, conversation, text)
    harness.repo.save_recovery = original  # type: ignore[method-assign]

    before = mistake_rows(harness.db, conversation)
    assert len(before) == 1
    crashed_row = raw_turn_row(harness.db, conversation, prepared.request_id)
    assert crashed_row["status"] == "failed"
    recovery = json.loads(crashed_row["output_json"])["_recovery"]
    assert [entry["plan"]["action"] for entry in recovery["actions"]] == [
        "explain_problem",
        "record_mistake",
    ]
    assert [item["action"] for item in recovery["pending"]] == ["view_workspace"]

    retried, events = await run(
        harness, conversation, text, retry_of=prepared.request_id
    )
    result = next(event for event in events if event.type == "result")
    assert result.kind == "dynamic"

    after = mistake_rows(harness.db, conversation)
    assert after == before  # 同一条记录：版本、时间、请求号都没有被重写
    retry_row = raw_turn_row(harness.db, conversation, retried.request_id)
    assert retry_row["status"] == "completed"
    steps = transcript_of(retry_row)["steps"]
    carried = [step for step in steps if step.get("carried")]
    assert len(carried) == 2
    assert {step["from_request_id"] for step in carried} == {prepared.request_id}
    assert [step["plan"]["action"] for step in steps] == [
        "explain_problem",
        "record_mistake",
        "view_workspace",
    ]


@pytest.mark.anyio
async def test_regenerate_reply_keeps_transcript_and_tools(tmp_path: Path) -> None:
    card = sample_card()
    harness = make_harness(tmp_path, cards=[card], replies=["这是第一次正文。"])
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [card])
    harness.scheduler.push(decision(ActionPlan(action="view_workspace", parameters={})))
    prepared, _ = await run(harness, conversation, "看看现在的题目")

    row_before = raw_turn_row(harness.db, conversation, prepared.request_id)
    harness.service._make_reply = type(harness.reply)(["这是重做的正文。"])
    regenerated = await harness.service.regenerate_reply(conversation, prepared.request_id)
    row_after = raw_turn_row(harness.db, conversation, prepared.request_id)

    assert regenerated["assistant_message"].startswith("这是重做的正文。")
    assert transcript_of(row_after) == transcript_of(row_before)
    assert harness.scheduler.calls[-1] is not None
    # 没有新增轮次，也没有新的工具执行。
    with sqlite3.connect(harness.db) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM conversation_turns WHERE conversation_id=?",
            (conversation,),
        ).fetchone()[0]
    assert count == 2


@pytest.mark.anyio
async def test_answer_marker_never_leaks_into_next_turn(tmp_path: Path) -> None:
    marker = "F010B-ANSWER-A"
    card = sample_card(question_id="q-a", number="1", statement="第一题题干")
    other = sample_card(question_id="q-b", number="2", statement="第二题题干")
    harness = make_harness(tmp_path, cards=[card, other], replies=["第一题答案是 B。"])
    harness.questions.answer_text = f"B（{marker}）"
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [card, other], question_type=None)

    harness.scheduler.push(
        decision(
            ActionPlan(action="select_question", parameters={"index": 1}),
            ActionPlan(action="view_question_answer", parameters={}),
        )
    )
    first_prepared, first_events = await run(harness, conversation, "看第 1 题的答案")
    # 第一轮确实把答案给了学生（业务事实），但答案正文不进入历史或后续 transcript。
    first_row = raw_turn_row(harness.db, conversation, first_prepared.request_id)
    assert marker in json.dumps(
        [event.model_dump(mode="json") for event in first_events], ensure_ascii=False
    )
    assert marker not in json.dumps(transcript_of(first_row), ensure_ascii=False)

    harness.scheduler.push(decision(ActionPlan(action="view_question_detail", parameters={"ref": "Q2"})))
    prepared, events = await run(harness, conversation, "第二题呢")
    history = json.dumps(harness.scheduler.calls[-1]["history"], ensure_ascii=False)
    assert marker not in history
    assert "答案" not in history or marker not in history
    sse = json.dumps([event.model_dump(mode="json") for event in events], ensure_ascii=False)
    assert marker not in sse
    assert marker not in json.dumps(
        transcript_of(raw_turn_row(harness.db, conversation, prepared.request_id)),
        ensure_ascii=False,
    )