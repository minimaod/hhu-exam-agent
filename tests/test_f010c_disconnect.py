"""F010 C：业务提交后客户端断开（服务端取消）的持久化与恢复。

与 A/B 的中断用例同一目标：业务结果已经写库，断开的只是正文流；恢复时只
重做正文，不重复执行工具。这里额外记录“终局流被取消”的服务端证据。
"""

from __future__ import annotations

import asyncio

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision, SchedulerAgent
from tests.f010c_doubles import StreamingToolModel
from tests.question_fakes import sample_card
from tests.test_f010b_support import (
    dummy_request,
    make_harness,
    seed_question_turn,
    turn_input,
)
from tests.tool_fakes import FakeToolMessage, ReplyFactory, tool_call

COURSE = "Python语言程序设计"


class BlockingStreamModel(StreamingToolModel):
    """产出第一段后挂起，模拟“正文还在写、学生关掉页面”。"""

    def __init__(self, responses, pieces):
        super().__init__(responses, pieces)
        self.cancelled = False
        self.started = asyncio.Event()

    async def astream(self, messages, **kwargs):
        self.streamed.append(messages)
        self.started.set()
        try:
            yield FakeToolMessage(content="第一段正文。")
            await asyncio.sleep(30)
        except (asyncio.CancelledError, GeneratorExit):
            # 服务端关闭流：GeneratorExit（aclose）或取消任务都会到这里。
            self.cancelled = True
            raise


@pytest.mark.anyio
async def test_cancel_during_final_stream_keeps_business_and_reply_pending(
    tmp_path,
) -> None:
    card = sample_card()
    model = BlockingStreamModel(
        [
            {"tool_calls": [tool_call("confirm_course", {"course": COURSE})]},
            {"content": "工具阶段内部说明。"},
        ],
        ["不会写完的正文"],
    )
    agent = SchedulerAgent(model=model)
    harness = make_harness(tmp_path, cards=[card], scheduler=agent)
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [card])
    prepared = harness.service.prepare_turn(
        conversation, turn_input("我要复习 Python")
    )

    seen: list[str] = []
    stream = harness.service.stream_prepared(prepared, dummy_request())

    async def consume() -> None:
        async for event in stream:
            seen.append(event.type)

    consumer = asyncio.create_task(consume())
    for _ in range(200):
        if "message_delta" in seen:
            break
        await asyncio.sleep(0.01)
    assert "message_delta" in seen
    # 服务端取消：与客户端断开时 Starlette 取消响应任务同一条路径。
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    for _ in range(50):
        if model.cancelled:
            break
        await asyncio.sleep(0.01)

    assert "result" not in seen
    assert model.cancelled, "终局流必须由服务端取消，而不是自然结束"

    turn = harness.repo.find_turn(conversation, prepared.request_id)
    assert turn.status == "completed"
    assert turn.business_status == "succeeded"
    assert [item.action for item in turn.composite_results] == ["introduce"]
    recovery = turn.recovery
    assert recovery["reply"]["reason"] == "reply_pending"
    assert "user_text" in recovery["reply_context"]
    assert turn.assistant_message and turn.assistant_message.strip()

    # 恢复只重做正文：不再决策、不再执行工具。
    calls_before = len(model.calls)
    service_reply = ReplyFactory(["恢复后的正文。"])
    harness.service._make_reply = service_reply
    replay = await harness.service.regenerate_reply(conversation, prepared.request_id)

    assert replay["assistant_message"] == "恢复后的正文。"
    assert replay["reply_status"] == "complete"
    assert len(model.calls) == calls_before
    assert len(harness.repo.list_turns(conversation)) == 2
    assert [item.action for item in harness.repo.find_turn(
        conversation, prepared.request_id
    ).composite_results] == ["introduce"]


@pytest.mark.anyio
async def test_unfinished_body_is_not_persisted_as_complete(tmp_path) -> None:
    """断开时只落了兜底正文；最终正文状态仍是 pending，不会伪装成完整。"""

    card = sample_card()
    model = BlockingStreamModel(
        [
            {"tool_calls": [tool_call("confirm_course", {"course": COURSE})]},
            {"content": "工具阶段内部说明。"},
        ],
        [],
    )
    agent = SchedulerAgent(model=model)
    harness = make_harness(tmp_path, cards=[card], scheduler=agent)
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [card])
    prepared = harness.service.prepare_turn(
        conversation, turn_input("我要复习 Python")
    )

    stream = harness.service.stream_prepared(prepared, dummy_request())
    async for event in stream:
        if event.type == "message_delta":
            break
    await stream.aclose()

    detail = harness.service.get_detail(conversation)
    turn = detail.turns[-1]
    assert turn.reply_status == "failed"
    assert turn.reply_retryable is True