"""F008：最终回复的流式生成、SSE 事件契约与失败兜底。

这里用真实 ``ConversationService`` + 真实 SSE 事件流，只把“模型”和
“工具决策”换成替身；用例断言的是协议与业务事实，不是替身内部状态。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.models import ActionResultItem
from app.conversations.reply import (
    MAX_DELTA_CHARS,
    MIN_DELTA_CHARS,
    ReplyMaterial,
    ReplyResult,
    StreamingReply,
    build_material,
)
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.mistakes.service import MistakeRecordService
from app.papers.repository import PaperRepository
from app.papers.service import DEFAULT_PAPERS_ROOT, PaperService
from app.tutoring.models import Source
from tests.tool_fakes import ReplyFactory
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


def read_recovery(db_path: Path, conversation_id: str, request_id: str) -> dict:
    """直接读回调恢复记录：它保存在 conversation_turns.output_json._recovery。"""

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT output_json FROM conversation_turns WHERE conversation_id=? AND request_id=?",
            (conversation_id, request_id),
        ).fetchone()
    assert row is not None and row[0]
    return json.loads(row[0])["_recovery"]


def read_turn_message(db_path: Path, conversation_id: str, request_id: str) -> str:
    """读回保存的最终正文（output_json.assistant_message）。"""

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT output_json FROM conversation_turns WHERE conversation_id=? AND request_id=?",
            (conversation_id, request_id),
        ).fetchone()
    assert row is not None and row[0]
    return json.loads(row[0]).get("assistant_message") or ""


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


def _decision() -> ScheduleDecision:
    return ScheduleDecision(
        confirmed_course="Python",
        actions=[ActionPlan(action="introduce", reason="宽泛复习需求")],
        assistant_reply="工具阶段的内部说明，不应出现在正文里。",
        suggested_actions=["找 Python 期末卷"],
    )


# ----------------------------------------------------------------------
# 1. 分块器：长度上限、按句切分、拼接等于原文
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_streaming_reply_chunks_respect_size_and_preserve_text() -> None:
    """每个片段不超过上限、不少于最小长度（尾部除外），拼起来等于原文。"""

    text = (
        "已经确认你的复习科目是 Python 语言程序设计。"
        "我先帮你找到两份期末试卷，可以按年份挑选。"
        "接下来你可以直接说“讲第 3 题”，我会按步骤讲解。"
        "如果想练习，也可以说“来一道类似的题”。"
    )
    # 故意切成很小的片段，模拟供应商逐字返回。
    from tests.tool_fakes import FakeReplyModel

    model = FakeReplyModel([text[i : i + 7] for i in range(0, len(text), 7)])
    reply = StreamingReply(model=model)

    pieces = [piece async for piece in reply.stream(ReplyMaterial(user_text="我要复习 Python"))]

    assert "".join(pieces) == text
    assert all(len(piece) <= MAX_DELTA_CHARS for piece in pieces)
    assert all(len(piece) >= MIN_DELTA_CHARS for piece in pieces[:-1])
    assert len(pieces) > 1


@pytest.mark.anyio
async def test_streaming_reply_passes_only_whitelisted_material() -> None:
    """提示词里只有白名单事实：答案、参考解答不会发给回复模型。"""

    material = build_material(
        user_text="看看答案",
        course="Python",
        composite_results=[
            ActionResultItem(
                action="view_question_answer",
                status="success",
                message="已展示第 1 题答案",
                details={
                    "number_label": "1",
                    "question_type": "choice",
                    "answer": "B",
                    "reference_solution_text": "因为 B 是唯一正确选项",
                },
            )
        ],
        waiting_for=None,
        question_titles=["第 1 题（选择题）"],
        suggestions=[],
    )
    described = material.describe()
    assert "第 1 题（选择题）" in described
    assert "已展示第 1 题答案" in described
    # 白名单外的字段根本进不了材料。
    assert "因为 B 是唯一正确选项" not in described
    assert "answer" not in described

    from tests.tool_fakes import FakeReplyModel

    model = FakeReplyModel(["已为你展开第 1 题的答案解析。"])
    reply = StreamingReply(model=model)
    _ = [piece async for piece in reply.stream(material)]
    prompt = model.calls[0][-1]["content"]
    assert "因为 B 是唯一正确选项" not in prompt


# ----------------------------------------------------------------------
# 2. 服务层：SSE 顺序、正文片段与最终正文一致
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_service_streams_message_delta_then_result(tmp_path: Path) -> None:
    """事件顺序为 progress… → composing_reply → message_delta(seq 升序) → result。"""

    factory = ReplyFactory(["已经确认科目是 Python。", "我先帮你找到两份期末试卷。"])
    svc, repo, scheduler, db_path = setup_service(tmp_path, factory)
    conversation = repo.create_conversation()
    scheduler.push(_decision())

    prepared = svc.prepare_turn(conversation.id, _input())
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]

    kinds = [event.type for event in events]
    assert "message_delta" in kinds
    # 正文片段一定在结果之前，结果之后不再有片段。
    first_delta = kinds.index("message_delta")
    result_index = kinds.index("result")
    assert first_delta < result_index
    assert "message_delta" not in kinds[result_index:]

    deltas = [event for event in events if event.type == "message_delta"]
    assert [event.seq for event in deltas] == list(range(1, len(deltas) + 1))
    assert all(event.delta for event in deltas)

    result = next(event for event in events if event.type == "result")
    assert result.assistant_message == "".join(event.delta for event in deltas)
    assert "工具阶段的内部说明" not in (result.assistant_message or "")

    # 业务结果与正文一起保存，恢复记录里能区分“模型生成”。
    assert read_turn_message(db_path, conversation.id, prepared.request_id) == result.assistant_message
    recovery = read_recovery(db_path, conversation.id, prepared.request_id)
    assert recovery is not None
    assert recovery["reply"]["mode"] == "model"
    assert recovery["version"] == 2


@pytest.mark.anyio
async def test_reply_failure_falls_back_and_is_marked_as_fallback(tmp_path: Path) -> None:
    """回复模型失败时仍保存业务结果，但正文是程序兜底并如实标记。"""

    # 先真实产出半句，然后供应商中断：正文不能把半句当成完整回复。
    factory = ReplyFactory(["前半句。", "后半句。"], fail_after=1)
    svc, repo, scheduler, db_path = setup_service(tmp_path, factory)
    conversation = repo.create_conversation()
    scheduler.push(_decision())

    prepared = svc.prepare_turn(conversation.id, _input())
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]

    result = next(event for event in events if event.type == "result")
    # 兜底正文来自已执行动作，不是模型续写。
    assert "已确认" in (result.assistant_message or "") or "Python" in (
        result.assistant_message or ""
    )
    recovery = read_recovery(db_path, conversation.id, prepared.request_id)
    assert recovery is not None
    assert recovery["reply"]["mode"] == "fallback"
    assert recovery["reply"]["reason"] == "reply_failed"
    # 业务结果照旧落盘，不因为正文失败而丢数据。
    assert read_turn_message(db_path, conversation.id, prepared.request_id) == (
        result.assistant_message
    )


@pytest.mark.anyio
async def test_empty_model_reply_falls_back_instead_of_blank_message(tmp_path: Path) -> None:
    """模型返回空串时不能给用户空白回复，应退回确定性说明。"""

    factory = ReplyFactory([])
    svc, repo, scheduler, db_path = setup_service(tmp_path, factory)
    conversation = repo.create_conversation()
    scheduler.push(_decision())

    prepared = svc.prepare_turn(conversation.id, _input())
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]

    result = next(event for event in events if event.type == "result")
    assert (result.assistant_message or "").strip()
    recovery = read_recovery(db_path, conversation.id, prepared.request_id)
    assert recovery is not None
    assert recovery["reply"]["mode"] == "fallback"
    assert recovery["reply"]["reason"] == "empty_reply"


@pytest.mark.anyio
async def test_reply_not_configured_keeps_deterministic_text(tmp_path: Path) -> None:
    """没有配置回复模型时不发正文片段，行为回到确定性说明。"""

    svc, repo, scheduler, db_path = setup_service(tmp_path, None)
    conversation = repo.create_conversation()
    scheduler.push(_decision())

    prepared = svc.prepare_turn(conversation.id, _input())
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]

    assert all(event.type != "message_delta" for event in events)
    result = next(event for event in events if event.type == "result")
    assert (result.assistant_message or "").strip()
    recovery = read_recovery(db_path, conversation.id, prepared.request_id)
    assert recovery is not None
    assert recovery["reply"]["mode"] == "fallback"
    assert recovery["reply"]["reason"] == "reply_not_configured"