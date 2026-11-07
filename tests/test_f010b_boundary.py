"""F010 B 返修：答案边界与实际模型输入、transcript 落盘投影。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.conversations.graph import (
    ActionOutcome,
    ActionPlan,
    ScheduleDecision,
    SchedulerAgent,
)
from app.conversations.models import ActionResultItem, ExerciseView
from tests.question_fakes import sample_card
from tests.test_f010b_support import (
    dummy_request,
    make_harness,
    raw_turn_row,
    seed_question_turn,
    transcript_of,
    turn_input,
)
from tests.tool_fakes import FakeToolModel


ANSWER_MARKER = "F010B-BOUNDARY-ANSWER"
EXERCISE_MARKER = "F010B-BOUNDARY-SOLUTION"


def decision(*actions: ActionPlan) -> ScheduleDecision:
    return ScheduleDecision(actions=list(actions), confirmed_course="Python语言程序设计")


async def run(harness, conversation_id: str, text: str):
    prepared = harness.service.prepare_turn(conversation_id, turn_input(text))
    events = [
        event
        async for event in harness.service.stream_prepared(prepared, dummy_request())
    ]
    return prepared, events


def seed_answer_turn(repo, conversation_id: str, card) -> None:
    """落一轮“已展示答案”，助手正文故意带合成标记。"""

    repo.start_turn(conversation_id, "req-answer", "第一题答案是什么", kind="dynamic")
    repo.complete_dynamic_turn(
        conversation_id,
        "req-answer",
        kind="dynamic",
        assistant_message=f"答案是 B（{ANSWER_MARKER}）。",
        composite_results=[
            ActionResultItem(
                action="view_question_answer",
                status="success",
                message="已显示原卷参考答案。",
            )
        ],
        question_results=[card],
        question=card,
        transcript={"version": 1, "steps": []},
    )


def test_scheduler_messages_do_not_receive_old_answer_marker(tmp_path: Path) -> None:
    card = sample_card()
    harness = make_harness(tmp_path, cards=[card])
    conversation = harness.service.create_conversation().id
    seed_answer_turn(harness.repo, conversation, card)

    summary = harness.service._recent_turns_summary(conversation)
    assert ANSWER_MARKER not in summary
    assert "已向学生展示" in summary

    history = harness.service._history_for(conversation, "req-next")
    assert ANSWER_MARKER not in json.dumps(history, ensure_ascii=False)

    model = FakeToolModel([{"content": "", "tool_calls": []}])
    scheduler = SchedulerAgent(model=model)
    import asyncio

    asyncio.run(
        scheduler.plan(
            "第二题讲讲",
            confirmed_course="Python语言程序设计",
            history=history,
            recent_turns_summary=summary,
            workspace_text="课程：Python语言程序设计",
        )
    )
    sent = json.dumps(model.calls, ensure_ascii=False, default=str)
    assert ANSWER_MARKER not in sent


@pytest.mark.anyio
async def test_exercise_solution_marker_never_reaches_next_reply_messages(
    tmp_path: Path,
) -> None:
    card = sample_card()
    harness = make_harness(tmp_path, cards=[card])
    conversation = harness.service.create_conversation().id
    now = datetime.now(timezone.utc)
    harness.repo.start_turn(conversation, "req-exercise", "看看这道练习", kind="dynamic")
    harness.repo.complete_dynamic_turn(
        conversation,
        "req-exercise",
        kind="dynamic",
        assistant_message=f"参考解答：{EXERCISE_MARKER}",
        exercise=ExerciseView(
            id="ex-boundary",
            conversation_id=conversation,
            problem_id="p-boundary",
            root_problem_id="p-boundary",
            statement_text="写一个求和函数",
            knowledge_point="循环",
            status="revealed",
            revealed_solution_at=now,
            reference_solution_text=f"参考解答：{EXERCISE_MARKER}",
            created_at=now,
            updated_at=now,
        ),
        exercise_id="ex-boundary",
    )

    harness.scheduler.push(decision(ActionPlan(action="view_workspace", parameters={})))
    await run(harness, conversation, "现在工作区有什么")

    summary = harness.service._recent_turns_summary(conversation)
    assert EXERCISE_MARKER not in summary
    reply_calls = harness.reply.models[-1].calls
    assert reply_calls, "正文模型没有收到消息"
    assert EXERCISE_MARKER not in json.dumps(reply_calls, ensure_ascii=False, default=str)


@pytest.mark.anyio
async def test_authorized_answer_visible_now_but_not_next_turn(tmp_path: Path) -> None:
    card = sample_card()
    harness = make_harness(tmp_path, cards=[card])
    harness.questions.answer_text = f"B（{ANSWER_MARKER}）"
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [card])

    harness.scheduler.push(
        decision(
            ActionPlan(action="select_question", parameters={"index": 1}),
            ActionPlan(action="view_question_answer", parameters={}),
        )
    )
    _, events = await run(harness, conversation, "第一题答案是什么")
    result = next(event for event in events if event.type == "result")
    assert result.question is not None
    assert ANSWER_MARKER in json.dumps(
        result.question.model_dump(mode="json"), ensure_ascii=False
    )
    assert ANSWER_MARKER in json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )

    harness.scheduler.push(decision(ActionPlan(action="view_workspace", parameters={})))
    await run(harness, conversation, "第二题讲讲")
    summary = harness.service._recent_turns_summary(conversation)
    assert ANSWER_MARKER not in summary
    assert ANSWER_MARKER not in json.dumps(
        harness.reply.models[-1].calls, ensure_ascii=False, default=str
    )


def test_transcript_is_projected_before_sqlite_write(tmp_path: Path) -> None:
    card = sample_card()
    harness = make_harness(tmp_path, cards=[card])
    conversation = harness.service.create_conversation().id
    ctx = harness.service._load_dynamic_context(conversation)
    ctx.transcript = []
    action = ActionPlan(
        action="explain_problem",
        parameters={"mode": "new", "private_marker": ANSWER_MARKER},
    )
    outcome = ActionOutcome(
        "explain_problem",
        "success",
        "已讲解。",
        {"question_id": card.question_id, "private_marker": EXERCISE_MARKER},
    )
    harness.service._record_transcript(ctx, action, outcome)
    harness.repo.start_turn(conversation, "req-transcript", "讲讲", kind="dynamic")
    harness.repo.complete_dynamic_turn(
        conversation,
        "req-transcript",
        kind="dynamic",
        transcript={"version": 1, "steps": ctx.transcript},
    )

    row = raw_turn_row(harness.db, conversation, "req-transcript")
    raw = row["output_json"]
    assert ANSWER_MARKER not in raw
    assert EXERCISE_MARKER not in raw
    stored = transcript_of(row)["steps"][0]
    assert stored["plan"]["parameters"] == {"mode": "new"}
    assert stored["outcome"]["details"] == {"question_id": card.question_id}
    assert stored["outcome"]["status"] == "success"


def test_carried_steps_are_projected_before_transcript(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    conversation = harness.service.create_conversation().id
    ctx = harness.service._load_dynamic_context(conversation)
    recovery = {
        "actions": [
            {
                "id": "step-1",
                "plan": {
                    "action": "view_question_detail",
                    "tool": "view_question_detail",
                    "parameters": {"ref": "Q1", "private_marker": ANSWER_MARKER},
                },
                "outcome": {
                    "status": "success",
                    "message": "已读取。",
                    "details": {
                        "question_id": "q-1",
                        "private_marker": EXERCISE_MARKER,
                    },
                },
            }
        ]
    }
    steps = harness.service._carried_steps(recovery, "req-old")
    assert steps[0]["carried"] is True
    assert steps[0]["from_request_id"] == "req-old"
    assert steps[0]["plan"]["parameters"] == {"ref": "Q1"}
    assert steps[0]["outcome"]["details"] == {"question_id": "q-1"}
    assert ANSWER_MARKER not in json.dumps(steps, ensure_ascii=False)
    assert EXERCISE_MARKER not in json.dumps(steps, ensure_ascii=False)