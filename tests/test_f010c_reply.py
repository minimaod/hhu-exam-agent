"""F010 C：合并决策与正文、终止判定与首片段时延。

验证：工具决策阶段的文字只作内部信息；最终正文在**同一份循环消息**上流式
生成；客户端拿到的只有终局片段；正文失败/截断只影响 reply 状态。
"""

from __future__ import annotations

import json

import pytest

from app.conversations import final_reply
from app.conversations.graph import (
    ActionPlan,
    ScheduleDecision,
    SchedulerAgent,
)
from tests.f010c_doubles import StreamingToolModel
from tests.question_fakes import sample_card
from tests.test_f010b_support import (
    dummy_request,
    make_harness,
    seed_question_turn,
    turn_input,
)
from tests.tool_fakes import ReplyFactory, tool_call

COURSE = "Python语言程序设计"


def decision(*actions: ActionPlan, reply: str | None = None) -> ScheduleDecision:
    return ScheduleDecision(
        actions=list(actions), confirmed_course=COURSE, assistant_reply=reply
    )


async def run(harness, conversation_id: str, text: str):
    prepared = harness.service.prepare_turn(conversation_id, turn_input(text))
    events = [
        event
        async for event in harness.service.stream_prepared(prepared, dummy_request())
    ]
    return prepared, events


def result_of(events):
    return next(event for event in events if event.type == "result")


def test_terminal_reason_prefers_loop_state() -> None:
    assert final_reply.terminal_reason({"terminal_reason": "budget"}) == "budget"
    assert final_reply.terminal_reason(
        {"observations": [{"needs_user": True, "message": "请先选一道题"}]}
    ) == "clarification"
    assert final_reply.terminal_reason({"pending": [1]}) == "budget"
    assert final_reply.terminal_reason({"replan_error": "boom"}) == "replan_error"
    assert final_reply.terminal_reason({}) == "model_finished"
    assert final_reply.clarification_question(
        {"observations": [{"needs_user": True, "message": "请先选一道题"}]}
    ) == "请先选一道题"


@pytest.mark.anyio
async def test_scheduler_streams_final_text_from_the_same_loop(tmp_path) -> None:
    """生产首选路径：决策文字不外发，终局正文复用同一份循环消息。"""

    card = sample_card()
    model = StreamingToolModel(
        [
            {"tool_calls": [tool_call("confirm_course", {"course": COURSE})]},
            {"content": "工具阶段内部说明：只确认科目。"},
        ],
        ["最终", "正文。"],
    )
    agent = SchedulerAgent(model=model)
    harness = make_harness(tmp_path, cards=[card], scheduler=agent)
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [card])

    prepared, events = await run(harness, conversation, "我要复习 Python")
    result = result_of(events)

    assert result.assistant_message == "最终正文。"
    joined = json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )
    assert "工具阶段内部说明" not in joined
    deltas = [event for event in events if event.type == "message_delta"]
    assert result.assistant_message == "".join(event.delta for event in deltas)
    # 终局请求确实带着工具执行结果（同一份循环消息），而不是另起一段材料。
    streamed = json.dumps(model.streamed[-1], ensure_ascii=False, default=str)
    assert "已确认复习科目" in streamed
    assert '"role": "tool"' in streamed

    recovery = harness.repo.find_turn(conversation, prepared.request_id).recovery
    meta = recovery["reply"]
    assert meta["mode"] == "finalize"
    assert isinstance(meta["finalize_first_delta_ms"], int)
    assert isinstance(meta["reply_total_ms"], int)
    assert recovery["reply_context"]["teaching"] is None


@pytest.mark.anyio
async def test_new_conversation_keeps_this_turn_tool_messages(tmp_path) -> None:
    """没有历史的第一轮：本轮的 assistant(tool_calls) 与 tool 结果不能被当成
    跨轮历史裁掉（A 批 03 的回归点）。"""

    card = sample_card()
    model = StreamingToolModel(
        [
            {"tool_calls": [tool_call("confirm_course", {"course": COURSE})]},
            {"content": "好的。"},
        ],
        ["已确认。"],
    )
    agent = SchedulerAgent(model=model)
    harness = make_harness(tmp_path, cards=[card], scheduler=agent)
    conversation = harness.service.create_conversation().id

    prepared, events = await run(harness, conversation, "我要复习 Python")
    result = result_of(events)
    streamed = json.dumps(model.streamed[-1], ensure_ascii=False, default=str)
    assert "已确认复习科目" in streamed
    assert '"role": "tool"' in streamed
    assert result.assistant_message == "已确认。"


@pytest.mark.anyio
async def test_native_finalize_hides_tool_turn_commentary(tmp_path) -> None:
    """原生 astream 路径：工具调用轮的伴随文字不进入终局提示。"""

    card = sample_card()
    model = StreamingToolModel(
        [
            {"tool_calls": [tool_call("confirm_course", {"course": COURSE})]},
            {"content": "工具阶段内部说明：只确认科目。"},
        ],
        ["最终正文。"],
    )
    harness = make_harness(tmp_path, cards=[card], scheduler=SchedulerAgent(model=model))
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [card])

    prepared, events = await run(harness, conversation, "我要复习 Python")
    result = result_of(events)

    prompt = model.streamed[-1]
    rendered = json.dumps(prompt, ensure_ascii=False, default=str)
    assert "工具阶段内部说明" not in rendered
    assert "已确认复习科目" in rendered
    assert '"role": "tool"' in rendered
    tool_call_messages = [
        message
        for message in prompt
        if isinstance(message, dict)
        and message.get("role") == "assistant"
        and message.get("tool_calls")
    ]
    assert tool_call_messages
    assert all(not (message.get("content") or "").strip() for message in tool_call_messages)
    assert result.assistant_message == "最终正文。"


@pytest.mark.anyio
async def test_native_stream_length_finish_marks_truncated_and_retryable(tmp_path) -> None:
    """原生流给出 finish_reason=length 时必须转成截断状态，可只重做正文。"""

    model = StreamingToolModel(
        [{"tool_calls": [tool_call("confirm_course", {"course": COURSE})]}],
        ["写到一半。"],
        finish_reason="length",
    )
    harness = make_harness(
        tmp_path, cards=[sample_card()], scheduler=SchedulerAgent(model=model)
    )
    conversation = harness.service.create_conversation().id
    prepared, events = await run(harness, conversation, "我要复习 Python")
    result = result_of(events)

    assert result.assistant_message == "写到一半。"
    assert result.reply_status == "truncated"
    assert result.reply_retryable is True
    recovery = harness.repo.find_turn(conversation, prepared.request_id).recovery
    assert recovery["reply"]["mode"] == "truncated"
    assert recovery["reply_context"]["version"] == 2


@pytest.mark.anyio
async def test_native_stream_pieces_join_without_stripping(tmp_path) -> None:
    """首尾空白是正文的一部分：deltas 拼接、result、落库必须完全一致。"""

    model = StreamingToolModel(
        [{"tool_calls": [tool_call("confirm_course", {"course": COURSE})]}],
        ["  开头有空格。", "结尾也有空格  "],
    )
    harness = make_harness(
        tmp_path, cards=[sample_card()], scheduler=SchedulerAgent(model=model)
    )
    conversation = harness.service.create_conversation().id

    prepared, events = await run(harness, conversation, "我要复习 Python")
    result = result_of(events)
    joined = "".join(event.delta for event in events if event.type == "message_delta")

    assert result.assistant_message == "  开头有空格。结尾也有空格  "
    assert joined == result.assistant_message
    turn = harness.repo.find_turn(conversation, prepared.request_id)
    assert turn.assistant_message == result.assistant_message


@pytest.mark.anyio
async def test_fallback_adapter_still_streams_loop_messages(tmp_path) -> None:
    """测试替身没有 astream 时，回退到回复适配器，消息仍是循环消息。"""

    harness = make_harness(tmp_path, cards=[sample_card()], replies=["适配器正文。"])
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [sample_card()])
    harness.scheduler.push(decision(ActionPlan(action="introduce")))

    prepared, events = await run(harness, conversation, "我要复习 Python")
    result = result_of(events)
    assert result.assistant_message == "适配器正文。"
    assert "已确认复习科目" in json.dumps(
        harness.reply.models[0].calls[-1], ensure_ascii=False, default=str
    )
    assert harness.repo.find_turn(conversation, prepared.request_id).recovery["reply"][
        "mode"
    ] == "finalize"


@pytest.mark.anyio
async def test_clarification_question_is_deterministic(tmp_path) -> None:
    """工具要求澄清时直接用问题原文，不再调回复模型改写。"""

    harness = make_harness(tmp_path, cards=[sample_card()])
    conversation = harness.service.create_conversation().id
    harness.scheduler.push(decision(ActionPlan(action="reveal_solution")))

    prepared, events = await run(harness, conversation, "看看参考解答")
    result = result_of(events)
    assert "练习" in result.assistant_message
    assert harness.reply.models == []
    meta = harness.repo.find_turn(conversation, prepared.request_id).recovery["reply"]
    assert meta["mode"] == "clarification"


@pytest.mark.anyio
async def test_truncated_final_text_is_marked_and_retryable(tmp_path) -> None:
    harness = make_harness(tmp_path, cards=[sample_card()])
    harness.reply = ReplyFactory(["写到一半"], finish_reason="length")
    harness.service._make_reply = harness.reply
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [sample_card()])
    harness.scheduler.push(decision(ActionPlan(action="introduce")))

    prepared, events = await run(harness, conversation, "我要复习 Python")
    result = result_of(events)
    assert result.assistant_message.startswith("写到一半")
    assert result.reply_status == "truncated"
    assert result.reply_retryable is True
    recovery = harness.repo.find_turn(conversation, prepared.request_id).recovery
    assert recovery["reply"]["mode"] == "truncated"
    assert recovery["reply_context"]["authorized"] == []