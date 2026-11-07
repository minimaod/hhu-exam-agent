"""F009：一次真实轮次里的学习记忆（决策 → 执行 → 正文 → 重做正文）。

用真实 ``ConversationService``、真实 SSE 事件流和真实学习库，只把“决策/正文
模型”换成替身。验证的是：

* 决策阶段拿到程序统计的记忆上下文，正文阶段拿到同一份事实；
* 移除操作的文案来自程序，回复模型失败也不改变业务结果；
* “重新生成正文”只复用材料，不重复执行记忆写入。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.memory.service import LearningMemoryService
from app.mistakes.service import MistakeRecordService
from app.papers.repository import PaperRepository
from app.papers.service import DEFAULT_PAPERS_ROOT, PaperService
from app.tutoring.models import Source
from tests.f009_support import Seed
from tests.test_f009_evidence import FakePapers
from tests.tool_fakes import ReplyFactory
from tests.tutoring_fakes import FakeExerciseAgent, FakeScheduler, FakeTutor, sample_explanation


def dummy_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/conversations/test/turns",
            "headers": [],
        }
    )


def setup(tmp_path: Path, reply_factory=None):
    db = tmp_path / "study.sqlite3"
    repo = ConversationRepository(db)
    repo.initialize()
    papers = PaperService(PaperRepository(DEFAULT_PAPERS_ROOT))
    memory = LearningMemoryService(
        db,
        papers=FakePapers(),
        taxonomy_path=tmp_path / "absent-taxonomy.json",
        current_file=tmp_path / "absent-current.json",
    )
    tutor = FakeTutor(explanations=[sample_explanation()])
    records = MistakeRecordService(
        repository=repo,
        papers=papers,
        make_tutor=lambda: tutor,
        tutoring_error=None,
        attachments_root=tmp_path / "attachments",
    )
    scheduler = FakeScheduler()
    service = ConversationService(
        repository=repo,
        make_query=None,
        query_error=None,
        make_tutor=lambda: tutor,
        make_router=None,
        make_exercise_agent=lambda: FakeExerciseAgent(),
        make_scheduler=lambda: scheduler,
        papers=papers,
        records=records,
        memory=memory,
        attachments_root=tmp_path / "attachments",
        make_reply=reply_factory,
    )
    return service, repo, scheduler, memory, Seed(db)


def turn_input(text: str) -> TurnInput:
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


def seed_wrong(seed: Seed) -> tuple[str, str]:
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(
        conversation,
        problem,
        knowledge_point="循环的条件与终止条件",
        reference="参考解答：不能出现在正文材料里的答案",
    )
    seed.attempt(exercise, correct=False)
    return conversation, exercise


def exclusion_count(db: Path) -> int:
    with sqlite3.connect(db) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM memory_exclusions").fetchone()[0])


@pytest.mark.anyio
async def test_turn_gives_decision_and_reply_the_same_memory_facts(tmp_path: Path) -> None:
    factory = ReplyFactory(["建议先补循环：条件与终止条件。"])
    service, repo, scheduler, memory, seed = setup(tmp_path, factory)
    conversation_id, _ = seed_wrong(seed)
    scheduler.push(
        ScheduleDecision(
            confirmed_course="Python",
            actions=[ActionPlan(action="review_memory", parameters={})],
            assistant_reply=None,
            suggested_actions=[],
        )
    )

    prepared = service.prepare_turn(conversation_id, turn_input("我最近复习 Python 应该先补什么"))
    events = [event async for event in service.stream_prepared(prepared, dummy_request())]

    # S1：调度阶段不再预读记忆；材料只来自下面显式 review_memory 的工具结果。
    plan_call = scheduler.calls[0]
    assert not (plan_call.get("memory_context") or "")

    result = next(event for event in events if event.type == "result")
    assert result.assistant_message == "建议先补循环：条件与终止条件。"
    prompt = factory.models[0].calls[0][-1]["content"]
    assert "【学习记忆（程序统计，只描述已有记录）】" in prompt
    assert "条证据" in prompt
    assert "参考解答" not in prompt
    # 记忆读取不影响业务结果落盘。
    turn = repo.find_turn(conversation_id, prepared.request_id)
    assert turn is not None and turn.status == "completed"


@pytest.mark.anyio
async def test_delete_is_program_owned_and_regenerate_does_not_repeat_it(
    tmp_path: Path,
) -> None:
    factory = ReplyFactory(["已按你的要求从学习记忆里移除这一条。"])
    service, repo, scheduler, memory, seed = setup(tmp_path, factory)
    conversation_id, exercise = seed_wrong(seed)
    entry_id = f"exercise:{exercise}"
    scheduler.push(
        ScheduleDecision(
            confirmed_course="Python",
            actions=[
                ActionPlan(action="review_memory", parameters={}),
                ActionPlan(action="delete_memory", parameters={"entry_id": entry_id}),
            ],
            assistant_reply=None,
            suggested_actions=[],
        )
    )

    prepared = service.prepare_turn(conversation_id, turn_input("Python 那条循环的题别算了，删掉"))
    events = [event async for event in service.stream_prepared(prepared, dummy_request())]

    result = next(event for event in events if event.type == "result")
    assert "从学习记忆和复习建议中移除" in (result.assistant_message or "")
    assert exclusion_count(tmp_path / "study.sqlite3") == 1
    view = memory.summary("Python语言程序设计", days=30, limit=50)
    assert view.counts.excluded == 1
    assert entry_id not in {entry.entry_id for entry in view.entries}

    # 正文重做只复用材料：模型换个说法，但不会再次写移除标记。
    service._make_reply = ReplyFactory(["已移除，会话记录仍然保留。"])
    regenerated = await service.regenerate_reply(conversation_id, prepared.request_id)
    assert regenerated["assistant_message"].startswith("已移除，会话记录仍然保留。")
    assert "从学习记忆和复习建议中移除，原会话记录保留。" in regenerated["assistant_message"]
    assert exclusion_count(tmp_path / "study.sqlite3") == 1
    assert memory.summary("Python语言程序设计", days=30, limit=50).counts.excluded == 1


@pytest.mark.anyio
async def test_memory_read_failure_does_not_break_the_turn(tmp_path: Path) -> None:
    factory = ReplyFactory(["我先把这道题讲清楚。"])
    service, repo, scheduler, memory, seed = setup(tmp_path, factory)
    conversation_id, exercise = seed_wrong(seed)
    with sqlite3.connect(tmp_path / "study.sqlite3") as connection:
        connection.execute("DROP TABLE memory_corrections")
        connection.commit()
    scheduler.push(
        ScheduleDecision(
            confirmed_course="Python",
            actions=[ActionPlan(action="review_memory", parameters={})],
            assistant_reply=None,
            suggested_actions=[],
        )
    )

    prepared = service.prepare_turn(conversation_id, turn_input("我最近复习 Python 该先补什么"))
    events = [event async for event in service.stream_prepared(prepared, dummy_request())]

    result = next(event for event in events if event.type == "result")
    assert result.assistant_message == "我先把这道题讲清楚。"
    prompt = factory.models[0].calls[0][-1]["content"]
    assert "暂时无法读取" in prompt
    assert "没有记录" not in prompt
    turn = repo.find_turn(conversation_id, prepared.request_id)
    assert turn is not None and turn.status == "completed"