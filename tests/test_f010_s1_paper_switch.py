"""S1 试卷切换：显式新条件覆盖旧 paper_id；仅旧 paper_id 不推断切换。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from tests.test_f008_ux_repair import (
    PAPER,
    build_service,
    result_of,
    run,
    turn_input,
    two_papers,
)
from tests.tutoring_fakes import FakeScheduler


@pytest.mark.anyio
async def test_explicit_new_year_with_stale_paper_id_relocates(tmp_path: Path) -> None:
    scheduler = FakeScheduler()
    service, _, runtime = build_service(tmp_path, scheduler=scheduler)
    old, new = two_papers(service, runtime)
    scheduler.push(ScheduleDecision(actions=[ActionPlan(
        action="query_papers",
        parameters={
            "course": "Python",
            "exam_type": "final",
            # 模型沿用了上一轮 paper_id，但学生本轮明确给了新学年/学期。
            "paper_id": PAPER,
            "academic_year": "2023-2024",
            "term": "s1",
            "variant": "A",
        },
    )]))

    result = result_of(await run(
        service, service.create_conversation().id,
        turn_input(text="换成 2023-2024 学年第一学期 Python 期末 A 卷"),
    ))
    assert result.result is not None
    assert [item.id for item in result.result.items] == ["py-2023-2024-s1-final"]
    assert result.result.items[0].id != old.source.paper_id
    assert new.source.paper_id == "py-2023-2024-s1-final"


@pytest.mark.anyio
async def test_stale_paper_id_alone_cannot_infer_a_switch(tmp_path: Path) -> None:
    """覆盖缺口：只有旧 paper_id 时，程序不能从参数推断“学生想换卷”。

    这是 S1 的参数契约边界：模型必须把学生本轮的新学年/学期/卷别传进来；
    只补一个旧 paper_id，程序只能按旧卷回答，不能凭原话猜。
    """

    scheduler = FakeScheduler()
    service, _, runtime = build_service(tmp_path, scheduler=scheduler)
    old, _ = two_papers(service, runtime)
    scheduler.push(ScheduleDecision(actions=[ActionPlan(
        action="query_papers",
        parameters={"course": "Python", "exam_type": "final", "paper_id": PAPER},
    )]))

    result = result_of(await run(
        service, service.create_conversation().id,
        turn_input(text="换成 2023-2024 学年第一学期 Python 期末 A 卷"),
    ))
    assert result.result is not None
    assert [item.id for item in result.result.items] == [PAPER]
    assert result.result.items[0].id == old.source.paper_id