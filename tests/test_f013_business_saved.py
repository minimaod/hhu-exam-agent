"""F013：业务已保存但后续模型超限时的事件与恢复标记。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.reply import StreamingReply
from app.quota.errors import ModelQuotaExceeded
from app.quota.gate import next_reset_at
from tests.f012_support import TEST_USER_ID
from tests.test_f010b_support import dummy_request, make_harness, turn_input

COURSE = "Python语言程序设计"


class QuotaReplyModel:
    def __init__(self, scope: str = "site") -> None:
        self.scope = scope

    async def astream(self, messages, **kwargs):
        raise ModelQuotaExceeded(self.scope, 500, 500, next_reset_at())
        if False:  # pragma: no cover - 让函数成为异步生成器
            yield ""


async def _run(harness, conversation_id: str, text: str):
    prepared = harness.service.prepare_turn(
        conversation_id, turn_input(text)
    )
    return [
        event
        async for event in harness.service.stream_prepared(
            prepared, dummy_request()
        )
    ]


@pytest.mark.anyio
async def test_quota_after_explanation_keeps_failed_recovery(
    tmp_path: Path,
) -> None:
    scheduler = type(
        "QuotaScheduler",
        (),
        {
            "plan": None,
            "observe": lambda self, *args, **kwargs: None,
        },
    )()
    calls = {"n": 0}

    async def plan(query: str, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ScheduleDecision(
                actions=[ActionPlan(action="explain_problem")],
                confirmed_course=COURSE,
            )
        raise ModelQuotaExceeded("site", 500, 500, next_reset_at())

    scheduler.plan = plan
    harness = make_harness(
        tmp_path, owner_id=TEST_USER_ID, scheduler=scheduler
    )
    conversation = harness.service.create_conversation().id
    events = await _run(harness, conversation, "这题怎么做")

    error = next(event for event in events if event.type == "error")
    assert error.code == "MODEL_QUOTA_EXCEEDED"
    assert error.saved is True
    assert error.quota is not None

    turn = harness.repo.find_turn(conversation, error.request_id)
    assert turn is not None
    assert turn.status == "failed"
    assert turn.recovery.get("business_saved") is True
    assert any(
        item.get("outcome", {}).get("status") == "success"
        for item in turn.recovery.get("actions", [])
    )


@pytest.mark.anyio
async def test_quota_only_during_final_reply_keeps_completed(
    tmp_path: Path,
) -> None:
    scheduler = type(
        "ConfirmScheduler",
        (),
        {
            "plan": None,
            "observe": lambda self, *args, **kwargs: None,
        },
    )()

    async def plan(query: str, **kwargs):
        return ScheduleDecision(
            actions=[ActionPlan(action="introduce")],
            confirmed_course=COURSE,
        )

    scheduler.plan = plan
    harness = make_harness(
        tmp_path, owner_id=TEST_USER_ID, scheduler=scheduler
    )
    harness.service._make_reply = lambda: StreamingReply(
        model=QuotaReplyModel("site")
    )
    conversation = harness.service.create_conversation().id
    events = await _run(harness, conversation, "我要复习 Python")

    error = next(event for event in events if event.type == "error")
    assert error.code == "MODEL_QUOTA_EXCEEDED"
    assert error.saved is True

    turn = harness.repo.find_turn(conversation, error.request_id)
    assert turn is not None
    assert turn.status == "completed"