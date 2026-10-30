"""F008 复核修复：六项问题的回归用例。

对应导师复核意见：
1. 一批工具成功后必须回到决策继续，直到模型收尾或程序要求等待学生；
2. 正文要能承载主要讲解（已授权的讲解内容进入回复材料）；
3. 全部调用被协议拒绝时不能显示“已完成”；
4. 截断（finish_reason=length）要如实标记，不能当成完整回复；
5. 回复失败要有公开状态和“只重做正文”的恢复入口；
6. （前端）切换会话要清掉流式正文，见 ConversationWorkspace 的 DOM 用例。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.reply import build_material
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput, _DynamicContext
from app.conversations.tools import tool_label
from app.mistakes.service import MistakeRecordService
from app.papers.repository import PaperRepository
from app.papers.service import DEFAULT_PAPERS_ROOT, PaperService
from app.tutoring.models import Source
from tests.tool_fakes import FakeReplyModel, ReplyFactory
from tests.tutoring_fakes import FakeExerciseAgent, FakeScheduler, FakeTutor, sample_explanation


def make_dummy_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/conversations/test/turns",
            "headers": [],
        }
    )


def setup_service(tmp_path: Path, reply_factory=None):
    repo = ConversationRepository(tmp_path / "study.sqlite3")
    repo.initialize()
    papers = PaperService(PaperRepository(DEFAULT_PAPERS_ROOT))
    tutor = FakeTutor(explanations=[sample_explanation()])
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
        make_reply=reply_factory,
    )
    return svc, repo, scheduler, tmp_path / "study.sqlite3"


def _input(text: str = "我要复习 Python") -> TurnInput:
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
    )


def _turn_json(db_path: Path, conversation_id: str, request_id: str) -> dict:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT output_json FROM conversation_turns WHERE conversation_id=? AND request_id=?",
            (conversation_id, request_id),
        ).fetchone()
    assert row is not None and row[0]
    return json.loads(row[0])


# ----------------------------------------------------------------------
# P1-1：成功后继续工具循环
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_successful_batch_goes_back_to_model_and_can_chain_actions(
    tmp_path: Path,
) -> None:
    """第一批工具成功后模型还能看到结果并安排下一步，直到自己收尾。"""

    svc, repo, scheduler, _ = setup_service(tmp_path)
    conversation = repo.create_conversation()
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="introduce", reason="先确认科目")],
            confirmed_course="Python",
        )
    )
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="query_papers", reason="看到科目后再查卷")],
            confirmed_course="Python",
        )
    )
    # 第三次决策不再返回工具 = 模型收尾。

    prepared = svc.prepare_turn(conversation.id, _input())
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]
    result = next(e for e in events if e.type == "result")

    assert [item.action for item in result.composite_results] == [
        "introduce",
        "query_papers",
    ]
    assert all(item.status == "success" for item in result.composite_results)
    # 三次决策：规划 → 看到结果后继续 → 模型收尾。
    assert len(scheduler.calls) == 3
    second = scheduler.calls[1]
    observed = [item for item in second["observations"] if item.get("status") == "success"]
    assert observed and observed[0]["action"] == "introduce"


@pytest.mark.anyio
async def test_tool_loop_stops_when_program_waits_for_student(tmp_path: Path) -> None:
    """程序要求等待学生时不再继续决策，避免自问自答。"""

    svc, repo, scheduler, _ = setup_service(tmp_path)
    conversation = repo.create_conversation()
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="clarify_or_reply", reason="需要先确认科目")],
            assistant_reply="你想复习哪一门课？",
        )
    )

    prepared = svc.prepare_turn(conversation.id, _input("复习一下"))
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]
    result = next(e for e in events if e.type == "result")

    assert result.business_status == "needs_input"
    assert len(scheduler.calls) == 1


# ----------------------------------------------------------------------
# P1-3：全部拒绝不能显示成功
# ----------------------------------------------------------------------


def _rejected_observation(tool: str = "no_such_tool") -> dict:
    return {
        "action": f"tool:{tool}",
        "status": "failed",
        "message": "没有名为 no_such_tool 的工具，本次调用没有执行。",
        "details": {
            "tool": tool,
            "error_code": "UNKNOWN_TOOL",
            "retryable": True,
            "rejected": True,
        },
        "needs_user": False,
        "executed": False,
    }


@pytest.mark.anyio
async def test_all_rejected_calls_are_reported_as_failure(tmp_path: Path) -> None:
    """所有调用都被协议拒绝时，业务状态必须是失败，而不是“已处理完成”。"""

    svc, repo, scheduler, _ = setup_service(tmp_path)
    conversation = repo.create_conversation()
    scheduler.push(ScheduleDecision(actions=[], rejected=[_rejected_observation()]))

    prepared = svc.prepare_turn(conversation.id, _input())
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]
    result = next(e for e in events if e.type == "result")

    assert result.business_status == "failed"
    assert result.retryable is True
    assert "已为您处理完成" not in (result.assistant_message or "")
    assert "未完成" in (result.assistant_message or "")
    rejected = [item for item in result.composite_results if item.details.get("rejected")]
    assert len(rejected) == 1
    assert rejected[0].status == "failed"
    assert rejected[0].details["label"] == tool_label("no_such_tool")
    # 前端据此显示中文动作名，而不是内部协议字符串。
    assert rejected[0].action == "tool:no_such_tool"


@pytest.mark.anyio
async def test_rejection_alongside_success_is_partial(tmp_path: Path) -> None:
    """成功一半 + 拒绝一半时表达为“部分完成”，仍可重试。"""

    svc, repo, scheduler, _ = setup_service(tmp_path)
    conversation = repo.create_conversation()
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="introduce", reason="确认科目")],
            confirmed_course="Python",
            rejected=[_rejected_observation()],
        )
    )

    prepared = svc.prepare_turn(conversation.id, _input())
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]
    result = next(e for e in events if e.type == "result")

    assert result.business_status == "partial"
    assert result.retryable is True


# ----------------------------------------------------------------------
# P1-2：正文承载主要讲解
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_authorized_explanation_enters_reply_material() -> None:
    """讲解内容（卡片已展示）要进入正文材料，正文才能承担主要讲解。"""

    explanation = sample_explanation()
    ctx = _DynamicContext(conversation_id="c", tutoring=explanation)
    teaching = ConversationService._teaching_material(ctx)

    assert teaching
    for step in explanation.steps:
        assert step.title in teaching
        assert step.content[:20] in teaching
    assert explanation.final_answer[:20] in teaching

    material = build_material(
        user_text="讲讲这题",
        course="高等数学",
        composite_results=[],
        waiting_for=None,
        teaching=teaching,
    )
    described = material.describe()
    assert "【本轮讲解" in described
    assert explanation.steps[0].content[:20] in described


@pytest.mark.anyio
async def test_reply_prompt_contains_explanation_text() -> None:
    """真正发给回复模型的提示词里能看到讲解，而不是只给一句摘要。"""

    explanation = sample_explanation()
    teaching = ConversationService._teaching_material(_DynamicContext(conversation_id="c", tutoring=explanation))
    material = build_material(
        user_text="讲讲这题",
        course="高等数学",
        composite_results=[],
        waiting_for=None,
        teaching=teaching,
    )
    from app.conversations.reply import StreamingReply

    model = FakeReplyModel(["先看单调性，再求导。"])
    reply = StreamingReply(model=model)
    _ = [piece async for piece in reply.stream(material)]

    prompt = model.calls[0][-1]["content"]
    assert explanation.steps[0].content[:20] in prompt
    # 提示词不再强制“120 字以内”，而是允许讲完。
    system = model.calls[0][0]["content"]
    assert "120 字" not in system
    assert "讲解" in system


# ----------------------------------------------------------------------
# P2-4：截断要如实标记
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_truncated_reply_keeps_text_but_marks_incomplete(tmp_path: Path) -> None:
    """finish_reason=length 时保留已产出的文字，同时标成截断。"""

    factory = ReplyFactory(["已经确认科目是 Python，", "接下来我们"], finish_reason="length")
    svc, repo, scheduler, db_path = setup_service(tmp_path, factory)
    conversation = repo.create_conversation()
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="introduce", reason="确认科目")],
            confirmed_course="Python",
        )
    )

    prepared = svc.prepare_turn(conversation.id, _input())
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]
    result = next(e for e in events if e.type == "result")

    # 业务成功，但正文状态是截断。
    assert result.business_status == "succeeded"
    assert result.reply_status == "truncated"
    assert result.reply_retryable is True
    assert (result.assistant_message or "").startswith("已经确认科目是 Python")
    recovery = _turn_json(db_path, conversation.id, prepared.request_id)["_recovery"]
    assert recovery["reply"]["mode"] == "truncated"
    assert recovery["reply"]["reason"] == "length"
    # 截断的正文不会被替换成兜底文案，保留残文供学生判断。
    assert "以下操作未完成" not in (result.assistant_message or "")


# ----------------------------------------------------------------------
# P2-5：公开状态 + 只重做正文
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_reply_failure_is_public_and_regeneration_skips_actions(
    tmp_path: Path,
) -> None:
    """回复失败对外可见，并且可以只重做正文而不重复执行成功操作。"""

    failing = ReplyFactory(["第一段。"], fail_after=0)
    svc, repo, scheduler, db_path = setup_service(tmp_path, failing)
    conversation = repo.create_conversation()
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="introduce", reason="确认科目")],
            confirmed_course="Python",
            suggested_actions=["找期末卷"],
        )
    )

    prepared = svc.prepare_turn(conversation.id, _input())
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]
    result = next(e for e in events if e.type == "result")

    assert result.business_status == "succeeded"
    assert result.reply_status == "failed"
    assert result.reply_retryable is True
    calls_before = len(scheduler.calls)

    # 换成一个正常工作的回复器，模拟“稍后重试成功”。
    working = ReplyFactory(["已经确认你的复习科目是 Python。"])
    svc._make_reply = working
    reply = await svc.regenerate_reply(conversation.id, prepared.request_id)

    assert reply["assistant_message"] == "已经确认你的复习科目是 Python。"
    assert reply["reply_status"] == "complete"
    assert reply["reply_retryable"] is False
    # 工具没有重跑：决策次数不变，业务结果保持原样。
    assert len(scheduler.calls) == calls_before
    saved = svc.get_detail(conversation.id).turns[-1]
    assert saved.assistant_message == "已经确认你的复习科目是 Python。"
    assert [item.action for item in saved.composite_results] == ["introduce"]


@pytest.mark.anyio
async def test_regeneration_requires_stored_material(tmp_path: Path) -> None:
    """没有回复材料的轮次不能“重做正文”，要明确报错而不是静默成功。"""

    from app.conversations.errors import ConversationConflictError

    svc, repo, scheduler, _ = setup_service(tmp_path, None)
    conversation = repo.create_conversation()
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="introduce", reason="确认科目")],
            confirmed_course="Python",
        )
    )
    prepared = svc.prepare_turn(conversation.id, _input())
    _ = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]

    with pytest.raises(ConversationConflictError):
        await svc.regenerate_reply(conversation.id, prepared.request_id)