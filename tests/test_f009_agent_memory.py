"""F009：学习记忆接入 Agent（工具、护栏、正文材料）。

这一层不验证模型“说得像不像”，而验证程序侧约束：
- 记忆事实由服务层算好再交给决策/正文；
- 纠正与移除只能落在本轮真实返回过的条目；
- 移除必须学生明确表达意图，并原样保留“原会话记录保留”的说明；
- 回复材料包含记忆事实但不含答案，且能随恢复记录往返。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.conversations.graph import ActionOutcome, DynamicReviewGraph, ExecutionResult, SchedulerAgent
from app.conversations.protocol import ActionPlan
from app.conversations.reply import material_from_payload, material_to_payload
from app.conversations.service import ConversationService, _DynamicContext
from app.conversations.repository import ConversationRepository
from app.memory.service import LearningMemoryService
from app.questions.runtime import QuestionRuntime
from tests.f009_support import Seed
from tests.test_f009_evidence import FakePapers
from tests.tool_fakes import FakeToolModel, tool_call


def build(tmp_path: Path) -> tuple[ConversationService, Seed, LearningMemoryService]:
    db = tmp_path / "study.sqlite3"
    repository = ConversationRepository(db)
    repository.initialize()
    memory = LearningMemoryService(
        db,
        papers=FakePapers(),
        taxonomy_path=tmp_path / "absent-taxonomy.json",
        current_file=tmp_path / "absent-current.json",
    )
    service = ConversationService(
        repository=repository,
        make_query=None,
        query_error="测试环境未启用模型",
        papers=FakePapers(),
        memory=memory,
        question_runtime=QuestionRuntime(None, error="测试环境未启用题目库"),
    )
    return service, Seed(db), memory


def prepared_for(conversation: str, request_id: str, text: str):
    return SimpleNamespace(
        conversation_id=conversation,
        request_id=request_id,
        input=SimpleNamespace(text=text, attachments=[], student_answer_text=None),
    )


def seed_wrong(seed: Seed) -> tuple[str, str, str]:
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(
        conversation,
        problem,
        knowledge_point="循环的条件与终止条件",
        reference="参考解答：不能进入学习记忆的答案正文",
    )
    attempt = seed.attempt(exercise, correct=False)
    return conversation, exercise, attempt


def review(service, ctx, text="我最近该先补什么"):
    prepared = prepared_for(ctx.conversation_id, "req-1", text)
    ctx.confirmed_course = ctx.confirmed_course or "Python语言程序设计"
    outcome = service._memory_review_outcome(
        prepared, ActionPlan(action="review_memory", parameters={}), ctx, text
    )
    return prepared, outcome


def test_review_returns_evidence_and_traceable_ids(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, exercise, attempt = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)

    _, outcome = review(service, ctx)

    assert outcome.status == "success"
    assert "条证据" in outcome.message
    assert "建议先补" in outcome.message
    assert outcome.details["memory_course"] == "Python语言程序设计"
    assert outcome.details["memory_entries"][0]["entry_id"] == f"exercise:{exercise}"
    assert outcome.details["memory_entries"][0]["source"]["conversation_id"] == conversation
    assert f"exercise:{exercise}" in ctx.memory_entry_ids
    assert f"attempt:{attempt}" in ctx.memory_entry_ids
    assert (ctx.memory_text or "").startswith("【学习记忆】")
    # 答案正文绝不能进入工具结果。
    assert "参考解答" not in str(outcome.details)
    assert "不能进入学习记忆" not in str(outcome.details)


def test_review_without_course_asks_before_guessing(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)

    outcome = service._memory_review_outcome(
        prepared_for(conversation, "req-1", "我先补什么"),
        ActionPlan(action="review_memory", parameters={}),
        ctx,
        "我先补什么",
    )

    assert outcome.status == "clarification"
    assert outcome.needs_user is True
    assert "哪一门" in outcome.message


def test_review_failure_is_reported_as_unavailable_not_empty(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation, confirmed_course="Python语言程序设计")
    import sqlite3

    with sqlite3.connect(service._repository.db_path) as connection:
        connection.execute("DROP TABLE memory_corrections")
        connection.commit()

    outcome = service._memory_review_outcome(
        prepared_for(conversation, "req-1", "我最近学得怎么样"),
        ActionPlan(action="review_memory", parameters={}),
        ctx,
        "我最近学得怎么样",
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "MEMORY_UNAVAILABLE"
    assert outcome.retryable is True
    assert "无法读取" in outcome.message
    assert ctx.memory_status == "unavailable"
    assert "不要编造" in (ctx.memory_text or "")


def test_memory_context_is_injected_only_when_relevant(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation, confirmed_course="Python语言程序设计")

    plain = prepared_for(conversation, "req-1", "这道题怎么解")
    assert service._prepare_memory_context(plain, ctx) is None

    relevant = prepared_for(conversation, "req-2", "我最近该先补哪块")
    text = service._prepare_memory_context(relevant, ctx)
    assert text is not None and text.startswith("【学习记忆】")


def test_guard_blocks_writes_without_reviewed_entry(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, exercise, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)

    action = ActionPlan(
        action="delete_memory", parameters={"entry_id": f"exercise:{exercise}"}
    )
    outcome = service._guard_dynamic_action(action, ctx, {"last_suggestions": []}, "这条别算了")
    assert outcome is not None and outcome.status == "clarification"

    review(service, ctx)
    invented = ActionPlan(
        action="delete_memory",
        parameters={"entry_id": "exercise:00000000-0000-4000-8000-000000000000"},
    )
    outcome = service._guard_dynamic_action(
        invented, ctx, {"last_suggestions": []}, "这条别算了"
    )
    assert outcome is not None and outcome.status == "clarification"

    real = ActionPlan(action="delete_memory", parameters={"entry_id": f"exercise:{exercise}"})
    assert service._guard_dynamic_action(real, ctx, {"last_suggestions": []}, "这条别算了") is None
    # 只是不同意评价、没有说“移除”时，先问一句，不直接删。
    blocked = service._guard_dynamic_action(
        real, ctx, {"last_suggestions": []}, "这题我其实会做"
    )
    assert blocked is not None and blocked.status == "clarification"
    assert "原会话记录会保留" in blocked.message


def test_correct_updates_memory_and_refreshes_context(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(conversation)
    exercise = seed.exercise(
        conversation, problem, knowledge_point="杂项", statement="自定义练习"
    )
    prepared, outcome = review(service, _DynamicContext(conversation_id=conversation))
    assert outcome.status == "success"
    ctx = _DynamicContext(conversation_id=conversation)
    ctx.memory_entry_ids = {f"exercise:{exercise}"}

    fixed = service._memory_correct_outcome(
        prepared,
        ActionPlan(
            action="correct_memory",
            parameters={
                "entry_id": f"exercise:{exercise}",
                "course": "Python语言程序设计",
                "note": "这是我当时看错条件",
            },
        ),
        ctx,
    )
    assert fixed.status == "success"
    assert "Python语言程序设计" in fixed.message
    assert fixed.details["memory_entry"]["course_source"] == "user"
    view = memory.summary("Python语言程序设计", days=30, limit=50)
    assert [entry.note for entry in view.entries] == ["这是我当时看错条件"]


def test_correct_rejects_attempt_entries(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, _, attempt = seed_wrong(seed)
    prepared, _ = review(service, _DynamicContext(conversation_id=conversation))
    ctx = _DynamicContext(conversation_id=conversation)
    ctx.memory_entry_ids = {f"attempt:{attempt}"}

    outcome = service._memory_correct_outcome(
        prepared,
        ActionPlan(
            action="correct_memory",
            parameters={"entry_id": f"attempt:{attempt}", "note": "x"},
        ),
        ctx,
    )
    assert outcome.status == "failed"
    assert "只能整体移除" in outcome.message


def test_delete_uses_exact_copy_and_persists(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    conversation, exercise, _ = seed_wrong(seed)
    prepared, _ = review(service, _DynamicContext(conversation_id=conversation))
    ctx = _DynamicContext(conversation_id=conversation)
    ctx.memory_entry_ids = {f"exercise:{exercise}"}

    first = service._memory_delete_outcome(
        prepared,
        ActionPlan(action="delete_memory", parameters={"entry_id": f"exercise:{exercise}"}),
        ctx,
    )
    assert first.status == "success"
    assert first.message == "从学习记忆和复习建议中移除，原会话记录保留。"
    assert first.details["memory_removed"] is True
    assert memory.summary("Python语言程序设计", days=30, limit=50).counts.evidence == 0

    second = service._memory_delete_outcome(
        prepared,
        ActionPlan(action="delete_memory", parameters={"entry_id": f"exercise:{exercise}"}),
        ctx,
    )
    assert second.message == "这条记忆之前已经移除，原会话记录保留。"


def test_deleting_one_attempt_keeps_the_exercise(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(conversation, problem, knowledge_point="循环")
    first = seed.attempt(exercise, attempt_no=1, correct=False)
    seed.attempt(exercise, attempt_no=2, kind="correction", correct=True)

    prepared, outcome = review(service, _DynamicContext(conversation_id=conversation))
    ctx = _DynamicContext(conversation_id=conversation)
    ctx.memory_entry_ids = {f"exercise:{exercise}", f"attempt:{first}"}

    removed = service._memory_delete_outcome(
        prepared,
        ActionPlan(action="delete_memory", parameters={"entry_id": f"attempt:{first}"}),
        ctx,
    )
    assert removed.status == "success"

    view = memory.summary("Python语言程序设计", days=30, limit=50)
    assert view.counts.evidence == 1
    entry = view.entries[0]
    assert entry.verdict == "correct_independent"
    assert [attempt.excluded for attempt in entry.attempts] == [True, False]


def test_reply_material_carries_memory_but_not_answers(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    prepared, _ = review(service, _DynamicContext(conversation_id=conversation))
    ctx = _DynamicContext(
        conversation_id=conversation, confirmed_course="Python语言程序设计"
    )
    review(service, ctx)

    material = service._build_reply_material(
        prepared,
        ctx,
        composite_results=[],
        suggestions=[],
        recent_summary=None,
    )
    assert material is not None
    described = material.describe()
    assert "【学习记忆（程序统计，只描述已有记录）】" in described
    assert "条证据" in described
    assert "参考解答" not in described
    assert "不能进入学习记忆" not in described

    payload = material_to_payload(material)
    assert payload["learning_memory"] == material.learning_memory
    restored = material_from_payload(payload)
    assert restored.learning_memory == material.learning_memory


def test_dynamic_reply_falls_back_to_memory_facts(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)
    review(service, ctx)

    text = service._dynamic_reply({"assistant_reply": None}, [], ctx)
    assert "条证据" in text
    assert "建议先补" in text


def test_scheduler_prompt_includes_memory_context() -> None:
    model = FakeToolModel([{"content": "好"}])
    scheduler = SchedulerAgent(model=model)
    import asyncio

    asyncio.run(
        scheduler.plan("我先补什么", memory_context="【学习记忆】Python 近30天 2 条证据")
    )
    first_user = model.calls[0][1]["content"]
    assert "【学习记忆】Python 近30天 2 条证据" in first_user


@pytest.mark.anyio
async def test_correct_is_skipped_when_review_failed() -> None:
    """依赖表：review 失败时同轮 correct 必须被跳过，不能凭猜测改记忆。"""

    calls: list[str] = []

    class Recorder:
        async def __call__(self, action, state):
            calls.append(action.action)
            if action.action == "review_memory":
                return ExecutionResult(
                    ActionOutcome(
                        "review_memory", "failed", "暂时无法读取", error_code="MEMORY_UNAVAILABLE"
                    )
                )
            return ExecutionResult(ActionOutcome(action.action, "success", "已更正"))

    model = FakeToolModel(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call("review_learning_memory", {}, call_id="c1"),
                    tool_call(
                        "correct_learning_memory",
                        {"entry_id": "exercise:abc", "note": "x"},
                        call_id="c2",
                    ),
                ],
            },
            {"content": "读不到学习记忆。"},
        ]
    )
    graph = DynamicReviewGraph(SchedulerAgent(model=model), Recorder())
    state = await graph.run({"query": "把那条改成高数"})

    assert calls == ["review_memory"]
    skipped = [
        item for item in state["observations"] if item["action"] == "correct_memory"
    ]
    assert skipped and skipped[0]["status"] == "failed"
    assert "前置步骤" in skipped[0]["message"]