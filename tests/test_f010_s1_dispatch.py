"""S1：正常调度器路径不进入旧 IntentRouter。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from tests.test_f008_ux_repair import build_service, result_of, run, turn_input
from tests.tutoring_fakes import FakeScheduler


@pytest.mark.anyio
async def test_scheduler_path_never_calls_legacy_router(tmp_path: Path) -> None:
    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[ActionPlan(
        action="query_papers", parameters={"course": "Python"},
    )])])
    service, _, _ = build_service(tmp_path, scheduler=scheduler)
    calls: list[object] = []

    async def exploding_router(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("正常调度路径不得调用旧 IntentRouter")

    service._make_router = lambda: exploding_router
    result = result_of(await run(
        service, service.create_conversation().id,
        turn_input(text="给我 Python 试卷"),
    ))
    assert calls == []
    assert result.result is not None
    assert scheduler.calls, "调度器应实际参与本轮"