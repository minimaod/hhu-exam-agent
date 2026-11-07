"""F010 C：持久化与 ``/reply`` 恢复语义。

验证：恢复记录不存答案正文；``/reply`` 只重做正文、按引用重读并逐条重校验；
失效条目丢弃并如实提示；旧 ``reply_input`` 记录仍兼容；不是重新跑工具。
"""

from __future__ import annotations

import json

import pytest

from app.conversations.errors import ConversationConflictError
from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.models import reply_state
from tests.question_fakes import sample_card
from tests.test_f010b_support import (
    dummy_request,
    make_harness,
    seed_question_turn,
    turn_input,
)
from tests.tool_fakes import ReplyFactory

COURSE = "Python语言程序设计"
ANSWER_MARK = "B（测试答案）"


def decision(*actions: ActionPlan) -> ScheduleDecision:
    return ScheduleDecision(actions=list(actions), confirmed_course=COURSE)


async def run(harness, conversation_id: str, text: str):
    prepared = harness.service.prepare_turn(conversation_id, turn_input(text))
    events = [
        event
        async for event in harness.service.stream_prepared(prepared, dummy_request())
    ]
    return prepared, events


async def answer_turn(tmp_path):
    harness = make_harness(tmp_path, cards=[sample_card()], replies=["答案见卡片。"])
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [sample_card()])
    harness.scheduler.push(
        decision(
            ActionPlan(action="select_question", parameters={"index": 1}),
            ActionPlan(action="view_question_answer", parameters={}),
        )
    )
    prepared, _events = await run(harness, conversation, "看第 1 题的答案")
    return harness, conversation, prepared


def test_pending_reply_is_retryable_only_with_material() -> None:
    assert reply_state({"reply": {"mode": "pending"}}) == ("failed", False)
    assert reply_state(
        {"reply": {"mode": "pending"}, "reply_context": {"user_text": "看答案"}}
    ) == ("failed", True)
    assert reply_state(
        {"reply": {"mode": "finalize"}, "reply_context": {"user_text": "看答案"}}
    ) == ("complete", False)


@pytest.mark.anyio
async def test_reply_context_stores_refs_not_answer_text(tmp_path) -> None:
    harness, conversation, prepared = await answer_turn(tmp_path)
    recovery = harness.repo.find_turn(conversation, prepared.request_id).recovery
    context = recovery["reply_context"]
    assert context["version"] == 2
    assert context["teaching"] is None
    assert ANSWER_MARK not in json.dumps(context, ensure_ascii=False)
    ref = next(item for item in context["authorized"] if item["kind"] == "question_answer")
    assert ref["question_id"] == sample_card().question_id
    assert ref["selection_id"] and ref["build_id"] and ref["content_sha256"]


@pytest.mark.anyio
async def test_reply_rereads_and_appends_committed_answer(tmp_path) -> None:
    harness, conversation, prepared = await answer_turn(tmp_path)
    harness.reply = ReplyFactory(["重做后的正文。"])
    harness.service._make_reply = harness.reply
    calls_before = len(harness.scheduler.calls)
    turns_before = len(harness.repo.list_turns(conversation))

    result = await harness.service.regenerate_reply(conversation, prepared.request_id)

    assert result["assistant_message"].startswith("重做后的正文。")
    prompt = json.dumps(harness.reply.models[-1].calls[-1], ensure_ascii=False, default=str)
    assert "原卷答案：B（测试答案）" in prompt
    assert len(harness.scheduler.calls) == calls_before
    assert len(harness.repo.list_turns(conversation)) == turns_before
    assert result["reply_status"] == "complete"


@pytest.mark.anyio
async def test_reply_drops_material_when_version_changed(tmp_path) -> None:
    harness, conversation, prepared = await answer_turn(tmp_path)
    harness.questions.evidence_status = "source_changed"
    harness.reply = ReplyFactory(["重做后的正文。"])
    harness.service._make_reply = harness.reply

    result = await harness.service.regenerate_reply(conversation, prepared.request_id)

    assert "答案版本已变化" in result["assistant_message"]
    prompt = json.dumps(harness.reply.models[-1].calls[-1], ensure_ascii=False, default=str)
    assert ANSWER_MARK not in prompt
    assert result["reply"].get("reason") == "answer_revalidated"
    recovery = harness.repo.find_turn(conversation, prepared.request_id).recovery
    assert recovery["reply"]["reason"] == "answer_revalidated"


@pytest.mark.anyio
async def test_reply_without_any_material_conflicts(tmp_path) -> None:
    harness = make_harness(tmp_path, cards=[sample_card()])
    conversation = harness.service.create_conversation().id
    harness.repo.start_turn(conversation, "req-null", "旧轮次")
    harness.repo.complete_dynamic_turn(
        conversation, "req-null", kind="dynamic", assistant_message="旧正文"
    )
    with pytest.raises(ConversationConflictError):
        await harness.service.regenerate_reply(conversation, "req-null")


def test_authorized_refs_bind_snapshot_and_attempt() -> None:
    """讲解引用带快照指纹，评价引用带作答尝试，恢复才不会串版本。"""

    from types import SimpleNamespace

    from app.conversations import final_reply

    explanation = SimpleNamespace(
        steps=[SimpleNamespace(index=1, title="步骤", content="讲解")],
        final_answer="B",
        error_analysis=None,
    )
    ctx = SimpleNamespace(
        answer_grant=None,
        tutoring=SimpleNamespace(
            kind="explanation",
            problem=SimpleNamespace(id="problem-1", explanation=explanation),
        ),
        problem_id="problem-1",
        request_id="req-1",
        exercise_view=SimpleNamespace(id="exercise-1", reference_solution_text="解答"),
        evaluation_view=SimpleNamespace(needs_supplement=False),
        exercise_id="exercise-1",
        active_exercise=SimpleNamespace(current_attempt=SimpleNamespace(id="attempt-1")),
    )

    refs = {item["kind"]: item for item in final_reply.authorized_refs(ctx)}

    assert refs["explanation"]["explanation_sha256"] == final_reply.explanation_sha256(
        explanation
    )
    assert refs["evaluation"]["attempt_id"] == "attempt-1"


@pytest.mark.anyio
async def test_reply_requires_completed_turn(tmp_path) -> None:
    """运行中的轮次不能重做正文：入口必须先确认轮次已经完成。"""

    harness = make_harness(tmp_path, cards=[sample_card()])
    conversation = harness.service.create_conversation().id
    harness.repo.start_turn(conversation, "req-running", "还在进行")

    with pytest.raises(ConversationConflictError):
        await harness.service.regenerate_reply(conversation, "req-running")


def _attach_reply_context(harness, conversation: str, request_id: str, context: dict) -> None:
    """把恢复记录直接写进轮次，模拟旧版本/已完成轮次保存的 v2 上下文。"""

    import sqlite3

    connection = sqlite3.connect(str(harness.db))
    try:
        row = connection.execute(
            "SELECT output_json FROM conversation_turns "
            "WHERE conversation_id = ? AND request_id = ?",
            (conversation, request_id),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0] or "{}")
        payload.setdefault("_recovery", {})["reply_context"] = context
        connection.execute(
            "UPDATE conversation_turns SET output_json = ? "
            "WHERE conversation_id = ? AND request_id = ?",
            (json.dumps(payload, ensure_ascii=False), conversation, request_id),
        )
        connection.commit()
    finally:
        connection.close()


def _completed_turn(harness, conversation: str, request_id: str) -> None:
    harness.repo.start_turn(conversation, request_id, "旧问题")
    harness.repo.complete_dynamic_turn(
        conversation, request_id, kind="dynamic", assistant_message="旧正文"
    )


@pytest.mark.anyio
async def test_legacy_reply_input_only_reuses_public_material(tmp_path) -> None:
    """旧 reply_input 无法核对授权：teaching、摘要与披露类结果一律不交给模型。"""

    from app.conversations.reply import ReplyResult, build_material, material_to_payload

    harness = make_harness(tmp_path, cards=[sample_card()])
    conversation = harness.service.create_conversation().id
    _completed_turn(harness, conversation, "req-legacy")
    material = build_material(
        user_text="旧问题", course=COURSE, composite_results=[], waiting_for=None
    )
    material = material.__class__(
        **{
            **material.__dict__,
            "teaching": f"原卷答案：{ANSWER_MARK}",
            "context_summary": f"上一轮助手回答摘录：{ANSWER_MARK}",
            "results": (
                ReplyResult(
                    action="view_question_answer",
                    status="success",
                    message="已展示答案",
                    details={"answer_detail": {"text": ANSWER_MARK}},
                ),
            ),
        }
    )
    from app.conversations import final_reply

    filtered = final_reply.public_material_from_legacy(material)
    assert ANSWER_MARK not in filtered.describe()
    assert ANSWER_MARK not in filtered.describe_context()
    harness.repo.update_turn_reply(
        conversation,
        "req-legacy",
        assistant_message="旧正文",
        reply_meta={"mode": "model", "deltas": 1},
        reply_input=material_to_payload(material),
    )
    harness.reply = ReplyFactory(["新的公开正文。"])
    harness.service._make_reply = harness.reply

    result = await harness.service.regenerate_reply(conversation, "req-legacy")

    prompt = json.dumps(harness.reply.models[-1].calls[-1], ensure_ascii=False, default=str)
    assert ANSWER_MARK not in prompt
    assert ANSWER_MARK not in result["assistant_message"]
    assert result["reply"]["material_scope"] == "public_only"


@pytest.mark.anyio
async def test_reply_binds_explanation_snapshot(tmp_path) -> None:
    """讲解按原快照重读；题目讲解被改版后不得把新内容写回旧轮次。"""

    from types import SimpleNamespace

    from app.conversations import final_reply

    harness = make_harness(tmp_path, cards=[sample_card()])
    conversation = harness.service.create_conversation().id
    _completed_turn(harness, conversation, "req-exp")
    explanation = SimpleNamespace(
        steps=[SimpleNamespace(index=1, title="第一步", content="原讲解内容")],
        final_answer="B",
        error_analysis=None,
    )
    problem = SimpleNamespace(
        id="problem-exp", conversation_id=conversation, explanation=explanation
    )
    harness.repo.get_problem = lambda problem_id: (
        problem if problem_id == "problem-exp" else None
    )
    context = {
        "version": 2,
        "reason": "model_finished",
        "user_text": "讲解一下这道题",
        "course": COURSE,
        "authorized": [
            {
                "kind": "explanation",
                "problem_id": "problem-exp",
                "request_id": "req-exp",
                "explanation_sha256": final_reply.explanation_sha256(explanation),
            }
        ],
        "teaching": None,
    }
    _attach_reply_context(harness, conversation, "req-exp", context)
    harness.reply = ReplyFactory(["重做后的讲解正文。"])
    harness.service._make_reply = harness.reply

    result = await harness.service.regenerate_reply(conversation, "req-exp")
    prompt = json.dumps(harness.reply.models[-1].calls[-1], ensure_ascii=False, default=str)
    assert "原讲解内容" in prompt
    assert result["reply"].get("reason") != "answer_revalidated"

    # 同一题目的讲解被改版：旧轮次不能跟着变。
    problem.explanation = SimpleNamespace(
        steps=[SimpleNamespace(index=1, title="第一步", content="改版后的讲解")],
        final_answer="C",
        error_analysis=None,
    )
    result = await harness.service.regenerate_reply(conversation, "req-exp")
    prompt = json.dumps(harness.reply.models[-1].calls[-1], ensure_ascii=False, default=str)
    assert "改版后的讲解" not in prompt
    assert "答案版本已变化" in result["assistant_message"]
    assert result["reply"].get("reason") == "answer_revalidated"


@pytest.mark.anyio
async def test_reply_uses_original_attempt_evaluation(tmp_path) -> None:
    """评价按原作答尝试重读，不取“最新一次”评价。"""

    from types import SimpleNamespace

    harness = make_harness(tmp_path, cards=[sample_card()])
    conversation = harness.service.create_conversation().id
    _completed_turn(harness, conversation, "req-eval")
    first = SimpleNamespace(
        id="attempt-1",
        evaluation_json=json.dumps(
            {
                "is_correct": True,
                "final_answer_assessment": "第一次评价",
                "step_by_step_analysis": "步骤完整",
                "guidance": "保持",
                "needs_supplement": False,
            },
            ensure_ascii=False,
        ),
    )
    second = SimpleNamespace(
        id="attempt-2",
        evaluation_json=json.dumps(
            {
                "is_correct": False,
                "final_answer_assessment": "订正后的评价",
                "step_by_step_analysis": "订正步骤",
                "guidance": "订正建议",
                "needs_supplement": False,
            },
            ensure_ascii=False,
        ),
    )
    exercise = SimpleNamespace(
        id="exercise-eval",
        conversation_id=conversation,
        attempts=[first, second],
        reference_solution_text=None,
    )
    harness.repo.get_exercise = lambda exercise_id: (
        exercise if exercise_id == "exercise-eval" else None
    )
    context = {
        "version": 2,
        "reason": "model_finished",
        "user_text": "评价我的作答",
        "course": COURSE,
        "authorized": [
            {
                "kind": "evaluation",
                "exercise_id": "exercise-eval",
                "attempt_id": "attempt-1",
                "request_id": "req-eval",
            }
        ],
        "teaching": None,
    }
    _attach_reply_context(harness, conversation, "req-eval", context)
    harness.reply = ReplyFactory(["重做后的评价正文。"])
    harness.service._make_reply = harness.reply

    result = await harness.service.regenerate_reply(conversation, "req-eval")

    prompt = json.dumps(harness.reply.models[-1].calls[-1], ensure_ascii=False, default=str)
    assert "第一次评价" in prompt
    assert "订正后的评价" not in prompt
    assert result["assistant_message"] == "重做后的评价正文。"


@pytest.mark.anyio
async def test_legacy_reply_input_record_still_regenerates(tmp_path) -> None:
    """旧版本只存 reply_input 的轮次仍能只重做正文（兼容路径）。"""

    from app.conversations.reply import build_material, material_to_payload

    harness = make_harness(tmp_path, cards=[sample_card()])
    conversation = harness.service.create_conversation().id
    harness.repo.start_turn(conversation, "req-old", "旧轮次")
    harness.repo.complete_dynamic_turn(
        conversation, "req-old", kind="dynamic", assistant_message="旧正文"
    )
    material = build_material(
        user_text="旧问题", course=COURSE, composite_results=[], waiting_for=None
    )
    harness.repo.update_turn_reply(
        conversation,
        "req-old",
        assistant_message="旧正文",
        reply_meta={"mode": "model", "deltas": 1},
        reply_input=material_to_payload(material),
    )
    harness.service._make_reply = ReplyFactory(["兼容正文。"])

    result = await harness.service.regenerate_reply(conversation, "req-old")
    assert result["assistant_message"] == "兼容正文。"
    assert result["reply_status"] == "complete"