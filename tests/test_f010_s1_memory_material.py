"""S1：记忆材料来自显式工具结果，且正文与 /reply 共用同一目标附注。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.memory.models import MemoryCorrectionRequest
from tests.test_f009_memory_turn import (
    dummy_request,
    seed_wrong,
    setup,
    turn_input,
)


@pytest.mark.anyio
async def test_memory_tool_result_and_note_reach_reply_and_regenerate(tmp_path: Path) -> None:
    from tests.tool_fakes import ReplyFactory

    factory = ReplyFactory(["先补循环边界：目标附注里的这一条。"])
    service, repo, scheduler, memory, seed = setup(tmp_path, factory)
    conversation_id, exercise = seed_wrong(seed)
    memory.correct(
        "exercise",
        exercise,
        MemoryCorrectionRequest(note="目标附注：循环边界与终止条件"),
    )
    scheduler.push(
        ScheduleDecision(
            confirmed_course="Python",
            actions=[ActionPlan(action="review_memory", parameters={})],
        )
    )

    prepared = service.prepare_turn(
        conversation_id, turn_input("我最近复习 Python 应该先补什么")
    )
    events = [event async for event in service.stream_prepared(prepared, dummy_request())]
    result = next(event for event in events if event.type == "result")
    assert result.business_status == "succeeded"

    prompt = factory.models[0].calls[0][-1]["content"]
    assert "目标附注：循环边界与终止条件" in prompt
    turn = repo.find_turn(conversation_id, prepared.request_id)
    assert turn is not None

    # /reply 只复用同一份材料，不重读记忆；目标附注仍在正文提示里。
    service._make_reply = ReplyFactory(["重做正文，仍引用目标附注。"])
    regenerated = await service.regenerate_reply(conversation_id, prepared.request_id)
    regen_prompt = service._make_reply.models[0].calls[0][-1]["content"]  # type: ignore[attr-defined]
    assert "目标附注：循环边界与终止条件" in regen_prompt
    assert "目标附注" in regenerated["assistant_message"]


@pytest.mark.anyio
async def test_empty_memory_and_read_failure_are_different_materials(tmp_path: Path) -> None:
    from tests.tool_fakes import ReplyFactory

    factory = ReplyFactory(["先按已有记录说。"])
    service, repo, scheduler, memory, seed = setup(tmp_path, factory)
    conversation_id, _ = seed_wrong(seed)

    # 没有记录：显式工具成功返回 empty，正文看到“没有记录”。
    scheduler.push(
        ScheduleDecision(
            confirmed_course="高等数学",
            actions=[ActionPlan(action="review_memory", parameters={"course": "高等数学"})],
        )
    )
    prepared = service.prepare_turn(conversation_id, turn_input("高数先补什么"))
    events = [event async for event in service.stream_prepared(prepared, dummy_request())]
    result = next(event for event in events if event.type == "result")
    prompt = factory.models[0].calls[0][-1]["content"]
    assert "还没有" in prompt or "没有记录" in prompt or "没有学习记录" in prompt
    assert "无法读取" not in prompt
    assert result.business_status == "succeeded"

    # 读取故障：错误码与文案必须区分，不能伪装成“没有记录”。
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    memory.summary = boom  # type: ignore[method-assign]
    scheduler.push(
        ScheduleDecision(
            confirmed_course="高等数学",
            actions=[ActionPlan(action="review_memory", parameters={"course": "高等数学"})],
        )
    )
    second = service.prepare_turn(conversation_id, turn_input("高数再补什么"))
    events = [event async for event in service.stream_prepared(second, dummy_request())]
    failed = next(event for event in events if event.type == "result")
    failed_prompt = factory.models[-1].calls[0][-1]["content"]
    assert "无法读取" in failed_prompt
    assert "还没有" not in failed_prompt
    assert failed.business_status != "succeeded" or any(
        item.status == "failed" for item in failed.composite_results
    )