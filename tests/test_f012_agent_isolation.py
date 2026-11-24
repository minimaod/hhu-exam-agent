"""F012：Agent/服务层不能借用 B 的身份引用 A 的资源。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.conversations import schema
from app.conversations.errors import (
    AttachmentNotFoundError,
    ConversationNotFoundError,
    MemoryEntryNotFoundError,
    ProblemNotFoundError,
)
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.conversations.graph import SchedulerAgent
from app.memory.models import MemoryCorrectionRequest
from app.memory.service import LearningMemoryService
from app.tutoring.models import Source
from tests.f012_support import (
    TEST_USER_ID,
    ensure_owner,
    new_id,
    owner_snapshot,
    scoped_repository,
)
from tests.test_f010b_support import (
    dummy_request,
    make_harness,
    seed_question_turn,
    turn_input,
)
from tests.question_fakes import sample_card
from tests.tool_fakes import FakeToolModel, tool_call
from tests.test_f012_route_matrix import _seed_a_world


class CountingScheduler:
    def __init__(self) -> None:
        self.calls = 0


def _b_service(db: Path, owner: str) -> tuple[ConversationService, CountingScheduler]:
    scheduler = CountingScheduler()
    repo = ConversationRepository(db, owner_id=owner)
    memory = LearningMemoryService(db, owner_id=owner)
    service = ConversationService(
        repository=repo,
        make_query=None,
        query_error="测试不调用模型",
        make_scheduler=lambda: scheduler,
        memory=memory,
    )
    return service, scheduler


def test_cross_user_ids_are_rejected_without_model_or_writes(
    tmp_path: Path,
) -> None:
    db = tmp_path / "study.sqlite3"
    schema.initialize_database(db)
    a_repo = scoped_repository(db, owner_id="owner-a")
    b_repo = scoped_repository(db, owner_id="owner-b")
    a_conv = a_repo.create_conversation().id
    b_conv = b_repo.create_conversation().id
    world = _seed_a_world(db, "owner-a")

    connection = sqlite3.connect(db)
    try:
        connection.execute(
            """
            INSERT INTO attachments (
                id, conversation_id, stored_name, mime, width, height,
                size_bytes, sha256, pending, created_at
            ) VALUES ('att-a', ?, 'owner-a/att-a.png', 'image/png', 1, 1, 10, 'x', 0, ?)
            """,
            (a_conv, "2026-11-01T00:00:00+00:00"),
        )
        connection.execute(
            """
            INSERT INTO question_selections (
                id, conversation_id, request_id, source_request_id, question_id,
                paper_id, build_id, source_version, content_sha256, question_type,
                number_label, snapshot_json, active, created_at, updated_at
            ) VALUES ('sel-a', ?, 'req-a', 'req-a', 'q-1', 'p-1', 'b-1', 'sv', ?, 'choice', '1', '{}', 0, ?, ?)
            """,
            (a_conv, "0" * 64, "2026-11-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()

    assert b_repo.get_conversation(a_conv) is None
    assert b_repo.get_problem(world["problem"]) is None
    assert b_repo.get_exercise(world["exercise"]) is None
    assert b_repo.get_record(world["record"]) is None
    assert b_repo.get_attachment(a_conv, "att-a") is None
    with pytest.raises(ConversationNotFoundError):
        b_repo.bind_question_problem("sel-a", world["problem"])

    # 反向组合：B 自己的 selection + A 的 problem 也必须拒绝。
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            """
            INSERT INTO question_selections (
                id, conversation_id, request_id, source_request_id, question_id,
                paper_id, build_id, source_version, content_sha256, question_type,
                number_label, snapshot_json, active, created_at, updated_at
            ) VALUES ('sel-b', ?, 'req-b', 'req-b', 'q-1', 'p-1', 'b-1', 'sv', ?, 'choice', '1', '{}', 0, ?, ?)
            """,
            (b_conv, "1" * 64, "2026-11-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ProblemNotFoundError):
        b_repo.bind_question_problem("sel-b", world["problem"])

    service, scheduler = _b_service(db, "owner-b")
    before = owner_snapshot(db, "owner-a")

    with pytest.raises(AttachmentNotFoundError):
        service.prepare_turn(
            b_conv,
            TurnInput(
                request_id=new_id(),
                kind=None,
                dispatch=True,
                text="带 A 的附件",
                attachments=(("att-a", "statement"),),
                student_answer_text=None,
                solution_text=None,
                source=Source(),
                problem_id=None,
                start_new=False,
            ),
        )
    with pytest.raises(ProblemNotFoundError):
        service.prepare_turn(
            b_conv,
            TurnInput(
                request_id=new_id(),
                kind="exercise_answer",
                dispatch=False,
                text="A 的练习",
                attachments=(),
                student_answer_text="答案",
                solution_text=None,
                source=Source(),
                problem_id=None,
                start_new=False,
                exercise_id=world["exercise"],
            ),
        )
    assert scheduler.calls == 0
    assert owner_snapshot(db, "owner-a")["digest"] == before["digest"]


def test_memory_correct_and_delete_are_owner_scoped(tmp_path: Path) -> None:
    db = tmp_path / "study.sqlite3"
    schema.initialize_database(db)
    ensure_owner(db, "owner-a")
    world = _seed_a_world(db, "owner-a")
    b_memory = LearningMemoryService(db, owner_id="owner-b")
    with pytest.raises(MemoryEntryNotFoundError):
        b_memory.correct(
            "exercise",
            world["exercise"],
            MemoryCorrectionRequest(note="B 想改 A"),
        )
    with pytest.raises(MemoryEntryNotFoundError):
        b_memory.delete("exercise", world["exercise"])


@pytest.mark.anyio
async def test_agent_tool_dispatch_rejects_a_workspace_reference(
    tmp_path: Path,
) -> None:
    """真实 SchedulerAgent 走工具分发；B 的工作区没有 A 的 Q1 引用。"""

    db = tmp_path / "study.sqlite3"
    schema.initialize_database(db)
    ensure_owner(db, "owner-a")
    world = _seed_a_world(db, "owner-a")
    before = owner_snapshot(db, "owner-a")

    model = FakeToolModel(
        [
            {
                "tool_calls": [
                    tool_call("view_question_detail", {"ref": "Q1"}, call_id="c1")
                ]
            },
            {"content": "这道题不在当前工作区。"},
        ]
    )
    harness = make_harness(
        tmp_path,
        owner_id="owner-b",
        scheduler=SchedulerAgent(model=model),
    )
    conversation = harness.service.create_conversation().id
    prepared = harness.service.prepare_turn(
        conversation, turn_input("看看第一题")
    )
    events = [
        event
        async for event in harness.service.stream_prepared(
            prepared, dummy_request()
        )
    ]
    result = next(event for event in events if event.type == "result")
    assert result.composite_results
    assert result.composite_results[0].status in ("failed", "clarification")
    assert model.calls, "真实 SchedulerAgent 应至少调用一次工具模型"
    assert owner_snapshot(db, "owner-a")["digest"] == before["digest"]
    assert harness.service.list_conversations().total == 1


@pytest.mark.anyio
async def test_a_workspace_q1_and_source_are_usable_b_denied(
    tmp_path: Path,
) -> None:
    """先证明 A 的 Q1/来源/重生成可用，再证明 B 使用同一资源被拒绝。"""

    card = sample_card()
    a_harness = make_harness(tmp_path, owner_id="owner-a")
    a_conv = a_harness.service.create_conversation().id
    seed_question_turn(a_harness.repo, a_conv, [card])
    a_harness.repo.update_turn_reply(
        a_conv,
        "req-seed",
        assistant_message="A 的旧正文",
        reply_meta={},
        reply_input={"user_text": "看看这道题", "results": []},
    )

    # A 能打开自己工作区的来源，并能重生成正文。
    source = a_harness.service.question_source(
        a_conv, card.question_id, card.build_id, "statement", 1
    )
    assert source.page == 1
    regenerated = await a_harness.service.regenerate_reply(a_conv, "req-seed")
    assert regenerated["assistant_message"]

    a_model = FakeToolModel(
        [
            {
                "tool_calls": [
                    tool_call("view_question_detail", {"ref": "Q1"}, call_id="a1")
                ]
            },
            {"content": "A 可以看到这道题。"},
        ]
    )
    a_harness.service._make_scheduler = lambda: SchedulerAgent(model=a_model)
    prepared = a_harness.service.prepare_turn(
        a_conv, turn_input("看看第一题")
    )
    a_events = [
        event
        async for event in a_harness.service.stream_prepared(
            prepared, dummy_request()
        )
    ]
    a_result = next(event for event in a_events if event.type == "result")
    assert a_result.composite_results[0].status == "success"

    b_model = FakeToolModel(
        [
            {
                "tool_calls": [
                    tool_call("view_question_detail", {"ref": "Q1"}, call_id="b1")
                ]
            },
            {"content": "B 看不到这道题。"},
        ]
    )
    b_harness = make_harness(
        tmp_path,
        owner_id="owner-b",
        scheduler=SchedulerAgent(model=b_model),
    )
    b_conv = b_harness.service.create_conversation().id
    with pytest.raises(ConversationNotFoundError):
        b_harness.service.question_source(
            a_conv, card.question_id, card.build_id, "statement", 1
        )
    with pytest.raises(ConversationNotFoundError):
        await b_harness.service.regenerate_reply(a_conv, "req-seed")

    b_prepared = b_harness.service.prepare_turn(
        b_conv, turn_input("看看第一题")
    )
    b_events = [
        event
        async for event in b_harness.service.stream_prepared(
            b_prepared, dummy_request()
        )
    ]
    b_result = next(event for event in b_events if event.type == "result")
    assert b_result.composite_results[0].status in ("failed", "clarification")