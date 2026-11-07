"""F010 D 反例：受保护动作不能靠省略/伪造 basis 绕过授权。

这些用例直接调用执行层守卫，断言拒绝形状与业务零写入；不依赖真实模型。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4

from app.conversations.authorization import authorize_action
from app.conversations.basis import validate_basis
from app.conversations.graph import ActionPlan
from app.conversations.service import _DynamicContext

from tests.test_f006_review_fixes import (
    collect,
    create_problem_with_exercise,
    decision,
    dispatch_input,
    setup_service,
)
from tests.test_f009_agent_memory import build, seed_wrong
from tests.tutoring_fakes import clear_understanding, sample_explanation


def _count(db: Path, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_negated_quote_cannot_authorize_save(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)
    action = ActionPlan(
        action="record_mistake",
        parameters={
            "basis": {"kind": "student_request", "quote": "保存这题"},
        },
    )
    before = _count(tmp_path / "study.sqlite3", "mistake_records")
    outcome = service._guard_dynamic_action(action, ctx, {"last_suggestions": []}, "别保存这题")
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.needs_user is False
    assert outcome.details["rejected"] is True
    assert outcome.error_code == "AUTHORIZATION_REQUIRED"
    assert _count(tmp_path / "study.sqlite3", "mistake_records") == before


def test_omitted_basis_cannot_bypass_authorization(tmp_path: Path) -> None:
    """省略 basis 不是“默认放行”：仍要过执行层原话授权，且零写入。"""

    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)
    action = ActionPlan(action="record_mistake", parameters={})
    before = _count(tmp_path / "study.sqlite3", "mistake_records")
    outcome = service._guard_dynamic_action(action, ctx, {"last_suggestions": []}, "这题我懂了")
    assert outcome is not None
    assert outcome.status in ("failed", "clarification")
    assert outcome.error_code == "AUTHORIZATION_REQUIRED"
    assert _count(tmp_path / "study.sqlite3", "mistake_records") == before


def _run_end_to_end(
    tmp_path: Path,
    *,
    text: str,
    action: ActionPlan,
    reply: str,
    seed_problem: bool = False,
) -> tuple[Any, Any, Path]:
    """真实服务栈跑一轮，返回 (结果事件, 该动作结果, 数据库路径)。"""

    service, repository, _, _, _ = setup_service(
        tmp_path,
        scheduler_decisions=[decision(action, reply=reply)],
    )
    conversation = repository.create_conversation().id
    if seed_problem:
        problem, _ = create_problem_with_exercise(
            repository, conversation_id=conversation
        )
        repository.upsert_context(conversation, active_problem_id=problem.id)
    prepared = service.prepare_turn(
        conversation, dispatch_input(str(uuid4()), text)
    )
    events = asyncio.run(collect(service, prepared))
    result = next(event for event in events if event.type == "result")
    item = next(
        item
        for item in (result.composite_results or [])
        if item.action == action.action
    )
    return result, item, tmp_path / "study.sqlite3"


def test_hypothetical_save_question_does_not_write(tmp_path: Path) -> None:
    """“保存这题会发生什么？”是元问题，不是保存请求。"""

    result, item, db = _run_end_to_end(
        tmp_path,
        text="保存这题会发生什么？",
        action=ActionPlan(action="record_mistake"),
        reply="只是问会发生什么，我不会保存。",
        seed_problem=True,
    )
    assert result.business_status in ("failed", "needs_input")
    assert item.status == "failed"
    assert item.details.get("error_code") == "AUTHORIZATION_REQUIRED"
    assert item.details.get("rejected") is True
    assert _count(db, "mistake_records") == 0


def test_informational_exercise_lookup_does_not_create(tmp_path: Path) -> None:
    """“查看上次练习的结果”是查看，不是再出一道。"""

    result, item, db = _run_end_to_end(
        tmp_path,
        text="我想查看上次练习的结果",
        action=ActionPlan(action="generate_exercise"),
        reply="这里只查看上次结果，不生成新练习。",
        seed_problem=False,
    )
    assert result.business_status in ("failed", "needs_input")
    assert item.status == "failed"
    assert item.details.get("error_code") == "AUTHORIZATION_REQUIRED"
    assert _count(db, "exercises") == 0


def test_memory_recall_question_does_not_delete(tmp_path: Path) -> None:
    """“这条记忆是什么时候删除的？”是回忆，不是删除指令。"""

    result, item, db = _run_end_to_end(
        tmp_path,
        text="这条记忆是什么时候删除的？",
        action=ActionPlan(action="delete_memory", parameters={"entry_id": "exercise:missing"}),
        reply="这里只回答记录，不会删除。",
    )
    assert result.business_status in ("failed", "needs_input")
    assert item.status in ("failed", "clarification")
    assert item.details.get("error_code") == "AUTHORIZATION_REQUIRED"
    assert _count(db, "memory_exclusions") == 0


def test_empty_text_still_hits_execution_authorization(tmp_path: Path) -> None:
    """空文本也必须进入执行层，不能因为“没话”就默认允许保存。"""

    result, item, db = _run_end_to_end(
        tmp_path,
        text="",
        action=ActionPlan(action="record_mistake"),
        reply="这轮没有可保存的请求。",
        seed_problem=True,
    )
    assert result.business_status in ("failed", "needs_input")
    assert item.status == "failed"
    assert item.details.get("error_code") == "AUTHORIZATION_REQUIRED"
    assert item.details.get("rejected") is True
    assert _count(db, "mistake_records") == 0


def test_failed_tool_result_is_not_a_basis() -> None:
    transcript = [
        {
            "call_id": "call_failed",
            "plan": {"action": "search_questions"},
            "outcome": {"status": "failed"},
        }
    ]
    error = validate_basis(
        {"kind": "tool_result", "depends_on_call_id": "call_failed"},
        current_text="就这道",
        transcript=transcript,
        suggestions=[],
    )
    assert error is not None and error[0] == "BASIS_UNVERIFIED"


def test_forged_suggestion_id_is_rejected() -> None:
    suggestions = [{"id": "s-10", "label": "练一道"}, {"id": "s-11", "label": "保存错题"}]
    error = validate_basis(
        {"kind": "confirmed_suggestion", "suggestion_id": "s-1"},
        current_text="好",
        transcript=[],
        suggestions=suggestions,
    )
    assert error is not None and error[0] == "AMBIGUOUS_BASIS"


def test_feedback_then_save_is_blocked_by_real_step_shape(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)
    ctx.transcript = [
        {
            "call_id": "call_feedback",
            "plan": {"action": "record_feedback", "parameters": {}},
            "outcome": {"status": "success"},
        }
    ]
    action = ActionPlan(action="record_mistake", parameters={})
    before = _count(tmp_path / "study.sqlite3", "mistake_records")
    outcome = service._guard_dynamic_action(action, ctx, {"last_suggestions": []}, "保存这道题")
    assert outcome is not None and outcome.status == "failed"
    assert outcome.error_code == "RED_LINE_CONFLICT"
    assert outcome.needs_user is False
    assert _count(tmp_path / "study.sqlite3", "mistake_records") == before


def test_hint_scope_from_earlier_step_blocks_answer(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)
    ctx.answer_deferred = True
    action = ActionPlan(action="view_question_answer", parameters={})
    outcome = service._guard_dynamic_action(action, ctx, {"last_suggestions": []}, "看答案")
    assert outcome is not None and outcome.status == "failed"
    assert outcome.error_code == "ANSWER_SCOPE_DENIED"
    assert outcome.needs_user is False
    # 拒绝结果不带答案正文，且顶层信封可被模型读取。
    payload = outcome.to_tool_payload()
    assert payload["status"] == "failed"
    assert payload["error_code"] == "ANSWER_SCOPE_DENIED"
    assert payload["remedies"]
    assert "答案正文" not in payload["message"]


def test_invalid_basis_is_correctable_in_same_turn(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)
    bad = ActionPlan(
        action="record_mistake",
        parameters={"basis": {"kind": "tool_result", "depends_on_call_id": "missing"}},
    )
    rejected = service._guard_dynamic_action(bad, ctx, {"last_suggestions": []}, "保存这道题")
    assert rejected is not None
    assert rejected.status == "failed"
    assert rejected.needs_user is False
    assert rejected.error_code == "BASIS_UNVERIFIED"
    assert "record_mistake" in rejected.details["remedies"]

    fixed = ActionPlan(
        action="record_mistake",
        parameters={"basis": {"kind": "student_request", "quote": "保存这道题"}},
    )
    assert service._guard_dynamic_action(fixed, ctx, {"last_suggestions": []}, "保存这道题") is None


def test_authorize_action_only_uses_student_words() -> None:
    assert authorize_action(
        "record_mistake", "保存这道题", transcript=[]
    ) is None
    denied = authorize_action("record_mistake", "别保存这题", transcript=[])
    assert denied is not None and denied.code == "AUTHORIZATION_REQUIRED"
    assert authorize_action(
        "view_question_answer", "现在告诉我答案", transcript=[]
    ) is None
    denied = authorize_action("view_question_answer", "讲讲这道题", transcript=[])
    assert denied is not None and denied.code == "ANSWER_NOT_REQUESTED"
    denied = authorize_action("view_question_answer", "先自己做，别给答案", transcript=[])
    assert denied is not None and denied.code == "ANSWER_NOT_REQUESTED"


def test_same_turn_correction_after_rejection_succeeds(tmp_path: Path) -> None:
    """D14：错误依据 → 工具可纠正拒绝 → 同轮改正 → 业务成功。"""

    bad = decision(ActionPlan(
        action="record_mistake",
        parameters={"basis": {"kind": "tool_result", "depends_on_call_id": "missing"}},
    ))
    good = decision(ActionPlan(
        action="record_mistake",
        parameters={"basis": {"kind": "student_request", "quote": "保存这道题"}},
    ))
    explain = decision(ActionPlan(action="explain_problem", parameters={"mode": "new"}))
    service, repo, scheduler, _, _ = setup_service(
        tmp_path,
        scheduler_decisions=[explain, bad, good],
        understandings=[clear_understanding()],
        explanations=[sample_explanation()],
    )
    conversation = repo.create_conversation()
    create_problem_with_exercise(repo, conversation_id=conversation.id)
    request = dispatch_input(str(uuid4()), "讲这道题，保存这道题")
    asyncio.run(collect(service, service.prepare_turn(conversation.id, request)))

    turn = service.get_detail(conversation.id).turns[-1]
    assert turn.business_status == "succeeded"
    assert len(scheduler.calls) >= 3
    first, rejected, second = turn.recovery["actions"]
    assert first["outcome"]["status"] == "success"
    assert rejected["outcome"]["details"]["error_code"] == "BASIS_UNVERIFIED"
    assert rejected["outcome"]["needs_user"] is False
    assert second["outcome"]["status"] == "success"