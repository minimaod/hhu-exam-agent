"""F008：恢复记录兼容性与重试语义。

关注三件事：

1. 新轮次写入 ``version=2``，并且带上 ``dispatch`` / ``reply``；
2. 旧 ``version=1`` 记录仍然能恢复执行（不需要数据库迁移）；
3. 恢复轮次不重新调用调度模型，也不重复执行已经成功的动作。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.mistakes.service import MistakeRecordService
from app.papers.repository import PaperRepository
from app.papers.service import DEFAULT_PAPERS_ROOT, PaperService
from app.tutoring.models import Source
from tests.tool_fakes import ReplyFactory
from tests.tutoring_fakes import (
    FakeExerciseAgent,
    FakeScheduler,
    FakeTutor,
    clear_understanding,
    sample_explanation,
)


class FlakyTutor(FakeTutor):
    """第一次讲解调用直接异常，用来制造“可重试失败”。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_next = True

    async def understand_problem(self, text, images):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("模拟模型中断")
        return await super().understand_problem(text, images)


def make_dummy_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/conversations/test/turns",
            "headers": [],
        }
    )


def setup_service(tmp_path: Path):
    db_path = tmp_path / "study.sqlite3"
    repo = ConversationRepository(db_path)
    repo.initialize()
    papers = PaperService(PaperRepository(DEFAULT_PAPERS_ROOT))
    tutor = FlakyTutor(
        understandings=[clear_understanding()], explanations=[sample_explanation()]
    )
    records = MistakeRecordService(
        repository=repo,
        papers=papers,
        make_tutor=lambda: tutor,
        tutoring_error=None,
        attachments_root=tmp_path / "attachments",
    )
    scheduler = FakeScheduler()
    svc = ConversationService(
        repository=repo,
        make_query=None,
        query_error=None,
        make_tutor=lambda: tutor,
        make_router=None,
        make_exercise_agent=lambda: FakeExerciseAgent(),
        make_scheduler=lambda: scheduler,
        papers=papers,
        records=records,
        attachments_root=tmp_path / "attachments",
        make_reply=ReplyFactory(["先确认科目，再讲这道题。"]),
    )
    return svc, repo, scheduler, db_path


def _input(text: str, *, retry_of: str | None = None) -> TurnInput:
    return TurnInput(
        request_id=str(uuid4()),
        kind=None,
        dispatch=True,
        text=text,
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=False,
        retry_of=retry_of,
    )


def read_recovery(db_path: Path, conversation_id: str, request_id: str) -> dict:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT output_json FROM conversation_turns WHERE conversation_id=? AND request_id=?",
            (conversation_id, request_id),
        ).fetchone()
    assert row is not None and row[0]
    return json.loads(row[0])["_recovery"]


def write_recovery(db_path: Path, conversation_id: str, request_id: str, payload: dict) -> None:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT output_json FROM conversation_turns WHERE conversation_id=? AND request_id=?",
            (conversation_id, request_id),
        ).fetchone()
        assert row is not None
        output = json.loads(row[0])
        output["_recovery"] = payload
        connection.execute(
            "UPDATE conversation_turns SET output_json=? WHERE conversation_id=? AND request_id=?",
            (json.dumps(output, ensure_ascii=False), conversation_id, request_id),
        )


async def _run_turn(svc, conversation_id: str, payload: TurnInput):
    prepared = svc.prepare_turn(conversation_id, payload)
    return [event async for event in svc.stream_prepared(prepared, make_dummy_request())]


def _failed_decision() -> ScheduleDecision:
    """先成功确认课程，再让讲题失败，制造一个可重试的恢复记录。"""

    return ScheduleDecision(
        confirmed_course="高等数学",
        actions=[
            ActionPlan(action="introduce", reason="确认课程"),
            ActionPlan(action="explain_problem", reason="讲解题目"),
        ],
        suggested_actions=[],
    )


@pytest.mark.anyio
async def test_new_recovery_record_is_version_two_with_dispatch_and_reply(
    tmp_path: Path,
) -> None:
    svc, repo, scheduler, db_path = setup_service(tmp_path)
    conversation = repo.create_conversation()
    scheduler.push(
        ScheduleDecision(
            confirmed_course="高等数学",
            actions=[ActionPlan(action="introduce", reason="确认课程")],
            suggested_actions=[],
        )
    )

    first = _input("我要复习高数")
    await _run_turn(svc, conversation.id, first)

    recovery = read_recovery(db_path, conversation.id, first.request_id)
    assert recovery["version"] == 2
    assert recovery["dispatch"] == "tools"
    assert recovery["reply"] == {"mode": "model", "deltas": 1}
    assert recovery["actions"][0]["plan"]["tool_call_id"] is None  # 由替身计划写入
    assert recovery["input"]["text"] == "我要复习高数"


@pytest.mark.anyio
async def test_version_one_record_can_still_be_retried(tmp_path: Path) -> None:
    """把记录改回 v1 模拟旧版本留下的行，重试仍然可执行。"""

    svc, repo, scheduler, db_path = setup_service(tmp_path)
    conversation = repo.create_conversation()
    scheduler.push(_failed_decision())

    first = _input("讲讲这道极限题")
    events = await _run_turn(svc, conversation.id, first)
    assert any(event.type == "result" for event in events)

    recovery = read_recovery(db_path, conversation.id, first.request_id)
    recovery["version"] = 1
    recovery.pop("dispatch", None)
    recovery.pop("reply", None)
    write_recovery(db_path, conversation.id, first.request_id, recovery)

    # 重试时不允许再调用调度模型：旧计划已经足够恢复。
    scheduler.calls.clear()
    retry = _input("讲讲这道极限题", retry_of=first.request_id)
    retry_events = await _run_turn(svc, conversation.id, retry)

    result = next(event for event in retry_events if event.type == "result")
    assert result.business_status in ("succeeded", "partial", "failed")
    assert scheduler.calls == []
    assert read_recovery(db_path, conversation.id, retry.request_id)["version"] == 1


@pytest.mark.anyio
async def test_retry_reuses_plan_without_recalling_scheduler(tmp_path: Path) -> None:
    """v2 记录重试：只重跑失败的步骤，已成功的动作直接带过来。"""

    svc, repo, scheduler, db_path = setup_service(tmp_path)
    conversation = repo.create_conversation()
    scheduler.push(_failed_decision())

    first = _input("讲讲这道极限题")
    await _run_turn(svc, conversation.id, first)

    recovery = read_recovery(db_path, conversation.id, first.request_id)
    statuses = {
        entry["plan"]["action"]: entry["outcome"]["status"]
        for entry in recovery["actions"]
        if entry.get("outcome")
    }
    assert statuses.get("introduce") == "success"
    assert statuses.get("explain_problem") == "failed"

    scheduler.calls.clear()
    retry = _input("讲讲这道极限题", retry_of=first.request_id)
    retry_events = await _run_turn(svc, conversation.id, retry)

    result = next(event for event in retry_events if event.type == "result")
    actions = [item.action for item in (result.composite_results or [])]
    # 已成功的引入结果被带入本轮，失败步骤重跑后也只出现一次。
    assert actions.count("introduce") == 1
    assert scheduler.calls == []

    retried = read_recovery(db_path, conversation.id, retry.request_id)
    assert retried["version"] == 2
    assert any(
        entry["plan"]["action"] == "explain_problem"
        and entry.get("outcome", {}).get("status") == "success"
        for entry in retried["actions"]
    )