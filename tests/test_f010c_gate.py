"""F010 C：答案/讲解授权门禁。

覆盖 C-1 验收：跨问题、跨版本的授权不能沿用；模型参数与历史查看事实都
不构成授权；拒绝以 role=tool 信封返回，且信封不含答案正文。
"""

from __future__ import annotations

import json

import pytest

from app.conversations import answer_gate, tool_projection
from app.conversations.graph import ActionOutcome, ActionPlan, ScheduleDecision
from tests.question_fakes import sample_card
from tests.test_f010b_support import (
    dummy_request,
    make_harness,
    raw_turn_row,
    seed_question_turn,
    transcript_of,
    turn_input,
)

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


def result_of(events):
    return next(event for event in events if event.type == "result")


def turn_recovery(harness, conversation_id: str, request_id: str) -> dict:
    turn = harness.repo.find_turn(conversation_id, request_id)
    assert turn is not None and turn.recovery is not None
    return turn.recovery


# ----------------------------------------------------------------------
# 纯函数边界：授权只来自学生原话
# ----------------------------------------------------------------------
def test_scope_and_answer_request_detection() -> None:
    assert answer_gate.scope_of("这道题只给提示") == "hint"
    assert answer_gate.scope_of("第 1 题考什么知识点") == "topic"
    assert answer_gate.scope_of("讲解一下这道题") is None
    assert answer_gate.answer_requested("看第 1 题的答案") is True
    assert answer_gate.answer_requested("讲讲这道题") is True
    assert answer_gate.answer_requested("先自己做，别给答案") is False
    assert answer_gate.wants_to_try_first("先自己做，别给答案") is True


def test_gate_rejects_version_change_and_missing_selection() -> None:
    card = sample_card()
    selection = type(
        "Selection",
        (),
        {
            "id": "sel-1",
            "conversation_id": "conv-1",
            "question_id": card.question_id,
            "build_id": card.build_id,
            "content_sha256": "b" * 64,
            "answer_revealed_at": None,
        },
    )()
    denied = answer_gate.question_answer_gate(
        conversation_id="conv-1",
        request_id="req-1",
        student_text="看答案",
        selection=selection,
        card=card,
    )
    assert denied.code == "ANSWER_VERSION_CHANGED"
    missing = answer_gate.question_answer_gate(
        conversation_id="conv-1",
        request_id="req-1",
        student_text="看答案",
        selection=None,
        card=card,
    )
    assert missing.code == "ANSWER_NOT_SELECTED"


def _attempt(**values):
    base = {
        "student_answer_text": None,
        "student_solution_text": None,
        "attachments": (),
        "evaluation_json": None,
    }
    base.update(values)
    return type("Attempt", (), base)()


def _exercise(attempts, *, status: str = "submitted", **values):
    base = {
        "id": "ex-1",
        "status": status,
        "attempts": list(attempts),
        "student_answer_text": None,
        "student_solution_text": None,
        "evaluation_json": None,
    }
    base.update(values)
    return type("Exercise", (), base)()


def _evaluation(*, needs_supplement: bool) -> str:
    return json.dumps(
        {
            "is_correct": None,
            "final_answer_assessment": "依据不足",
            "step_by_step_analysis": "只写了答案",
            "guidance": "请补充过程",
            "needs_supplement": needs_supplement,
        },
        ensure_ascii=False,
    )


def test_exercise_solution_gate_requires_finished_evaluation() -> None:
    """评价缺失/畸形/字段不全 = 未完成；保存了作答也不等于练习已完成。"""

    malformed = (
        None,
        "",
        "{not json",
        "{}",
        '{"needs_supplement": false}',
        '{"final_answer_assessment": "对", "step_by_step_analysis": "", "guidance": "继续"}',
        '{"final_answer_assessment": "对", "step_by_step_analysis": "步骤完整", "guidance": "继续"}',
    )
    for raw in malformed:
        pending = _exercise([_attempt(student_answer_text="42", evaluation_json=raw)])
        decision = answer_gate.exercise_solution_gate(
            request_id="req-1", student_text="继续", exercise=pending
        )
        assert not decision.allowed, raw
        assert decision.code == "EXERCISE_SOLUTION_NOT_AUTHORIZED"
        mirrored = _exercise([], student_answer_text="42", evaluation_json=raw)
        assert not answer_gate.exercise_solution_gate(
            request_id="req-1", student_text="继续", exercise=mirrored
        ).allowed, raw
    # 对照：完整评价且明确不需要补充时才开放。
    finished = _exercise(
        [_attempt(student_answer_text="42", evaluation_json=_evaluation(needs_supplement=False))]
    )
    assert answer_gate.exercise_solution_gate(
        request_id="req-1", student_text="继续", exercise=finished
    ).allowed


def test_exercise_solution_gate_requires_attempt_or_explicit_request() -> None:
    practice = _exercise([], status="pending_answer")
    denied = answer_gate.exercise_solution_gate(
        request_id="req-1", student_text="生成一道练习", exercise=practice
    )
    assert denied.code == "EXERCISE_SOLUTION_NOT_AUTHORIZED"
    allowed = answer_gate.exercise_solution_gate(
        request_id="req-1", student_text="看看解答", exercise=practice
    )
    assert allowed.allowed and allowed.grant.kind == "exercise_solution"
    submitted = _exercise(
        [_attempt(student_answer_text="42", evaluation_json=_evaluation(needs_supplement=False))]
    )
    assert answer_gate.exercise_solution_gate(
        request_id="req-1", student_text="我提交了", exercise=submitted
    ).allowed


def test_exercise_solution_gate_blocks_attempt_awaiting_supplement() -> None:
    """待补充的尝试不算“已提交作答”：不能凭它打开参考解答。"""

    pending = _exercise(
        [_attempt(student_answer_text="42", evaluation_json=_evaluation(needs_supplement=True))]
    )
    decision = answer_gate.exercise_solution_gate(
        request_id="req-1", student_text="继续", exercise=pending
    )
    assert not decision.allowed
    assert decision.code == "EXERCISE_SOLUTION_NOT_AUTHORIZED"
    # 顶层镜像同样是待补充时也不能放行。
    mirrored = _exercise(
        [],
        student_answer_text="42",
        evaluation_json=_evaluation(needs_supplement=True),
    )
    assert not answer_gate.exercise_solution_gate(
        request_id="req-1", student_text="继续", exercise=mirrored
    ).allowed
    # 补充完整后（needs_supplement=False）才开放。
    completed = _exercise(
        [
            _attempt(
                student_answer_text="42",
                evaluation_json=_evaluation(needs_supplement=True),
            ),
            _attempt(
                student_solution_text="先设 x，再代入。",
                evaluation_json=_evaluation(needs_supplement=False),
            ),
        ]
    )
    assert answer_gate.exercise_solution_gate(
        request_id="req-1", student_text="我补充完了", exercise=completed
    ).allowed


def test_exercise_solution_gate_ignores_negated_solution_request() -> None:
    """「暂不展示参考解答」是限制，不是主动查看请求。"""

    submitted = _exercise(
        [_attempt(student_answer_text="42", evaluation_json=_evaluation(needs_supplement=False))]
    )
    for text in (
        "暂不展示参考解答",
        "先不急着看参考解答",
        "不用给我解答",
        "别展示答案",
    ):
        decision = answer_gate.exercise_solution_gate(
            request_id="req-1", student_text=text, exercise=submitted
        )
        assert not decision.allowed, text
        assert decision.code == "EXERCISE_SOLUTION_NOT_AUTHORIZED"
    # 同一句话里有否定限制、也有明确的查看请求时仍按请求放行。
    assert answer_gate.exercise_solution_gate(
        request_id="req-1",
        student_text="不用讲解，直接给我看参考解答",
        exercise=submitted,
    ).allowed


# ----------------------------------------------------------------------
# 端到端：练习参考解答门禁不提前放行
# ----------------------------------------------------------------------
SOLUTION_MARK = "参考解答X9：先设 x=1，再代入求值。"


def _seed_exercise(
    harness,
    conversation_id: str,
    *,
    needs_supplement: bool,
    evaluate: bool = True,
) -> str:
    harness.repo.create_problem(
        conversation_id=conversation_id,
        statement_text="求 1+1 的值。",
        id="problem-c1",
    )
    exercise = harness.repo.create_exercise(
        id="ex-c1",
        conversation_id=conversation_id,
        problem_id="problem-c1",
        root_problem_id="problem-c1",
        statement_text="求 1+1 的值。",
        reference_solution_text=SOLUTION_MARK,
        knowledge_point="算术运算",
    )
    harness.repo.create_exercise_attempt(
        exercise.id,
        kind="initial",
        student_answer_text="2",
        student_solution_text=None,
    )
    if evaluate:
        attempt = harness.repo.get_exercise(exercise.id).current_attempt
        assert attempt is not None
        harness.repo.save_attempt_evaluation(
            attempt.id,
            evaluation_json=_evaluation(needs_supplement=needs_supplement),
        )
    return exercise.id


async def _reveal_turn(harness, conversation_id: str, text: str):
    harness.scheduler.push(decision(ActionPlan(action="reveal_solution", parameters={})))
    prepared, events = await run(harness, conversation_id, text)
    return prepared, events


def _assert_no_solution_leak(harness, exercise_id: str, prepared, events) -> None:
    joined = json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )
    assert SOLUTION_MARK not in joined
    prompt = json.dumps(harness.scheduler.calls[-1], ensure_ascii=False, default=str)
    assert SOLUTION_MARK not in prompt
    exercise = harness.repo.get_exercise(exercise_id)
    assert exercise is not None
    assert exercise.revealed_solution_at is None
    assert exercise.status != "revealed"
    context = harness.repo.find_turn(
        prepared.conversation_id, prepared.request_id
    ).recovery["reply_context"]
    assert SOLUTION_MARK not in json.dumps(context, ensure_ascii=False)
    assert all(item["kind"] != "exercise_solution" for item in context["authorized"])


@pytest.mark.anyio
async def test_unevaluated_attempt_does_not_open_solution(tmp_path) -> None:
    """保存了作答但评价尚未落库：练习未完成，不能打开参考解答。"""

    harness = make_harness(tmp_path, cards=[sample_card()], replies=["评价还在处理中。"])
    conversation = harness.service.create_conversation().id
    exercise_id = _seed_exercise(
        harness, conversation, needs_supplement=False, evaluate=False
    )
    exercise = harness.repo.get_exercise(exercise_id)
    assert exercise is not None and exercise.status == "pending_answer"
    assert exercise.current_attempt is not None
    assert exercise.current_attempt.evaluation_json is None

    prepared, events = await _reveal_turn(harness, conversation, "继续")

    result = result_of(events)
    assert all(item.action != "reveal_solution" for item in result.composite_results)
    row = raw_turn_row(harness.db, conversation, prepared.request_id)
    assert "EXERCISE_SOLUTION_NOT_AUTHORIZED" in json.dumps(
        transcript_of(row), ensure_ascii=False
    )
    _assert_no_solution_leak(harness, exercise_id, prepared, events)


@pytest.mark.anyio
async def test_exercise_awaiting_supplement_does_not_open_solution(tmp_path) -> None:
    """待补充的尝试不能打开参考解答：不放行、不写揭晓状态、不泄露。"""

    harness = make_harness(tmp_path, cards=[sample_card()], replies=["请先补充完整过程。"])
    conversation = harness.service.create_conversation().id
    exercise_id = _seed_exercise(harness, conversation, needs_supplement=True)

    prepared, events = await _reveal_turn(harness, conversation, "继续")

    result = result_of(events)
    assert all(item.action != "reveal_solution" for item in result.composite_results)
    rows = json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )
    assert "EXERCISE_SOLUTION_NOT_AUTHORIZED" not in rows  # 拒绝不进入学生可见结果
    row = raw_turn_row(harness.db, conversation, prepared.request_id)
    assert "EXERCISE_SOLUTION_NOT_AUTHORIZED" in json.dumps(
        transcript_of(row), ensure_ascii=False
    )
    _assert_no_solution_leak(harness, exercise_id, prepared, events)


@pytest.mark.anyio
async def test_negated_solution_request_does_not_open_solution(tmp_path) -> None:
    """「暂不展示参考解答」是限制；即使已提交作答也不能借这句话打开解答。"""

    harness = make_harness(tmp_path, cards=[sample_card()], replies=["好的，先不展示。"])
    conversation = harness.service.create_conversation().id
    exercise_id = _seed_exercise(harness, conversation, needs_supplement=True)

    prepared, events = await _reveal_turn(harness, conversation, "暂不展示参考解答")

    result = result_of(events)
    assert all(item.action != "reveal_solution" for item in result.composite_results)
    row = raw_turn_row(harness.db, conversation, prepared.request_id)
    assert "EXERCISE_SOLUTION_NOT_AUTHORIZED" in json.dumps(
        transcript_of(row), ensure_ascii=False
    )
    _assert_no_solution_leak(harness, exercise_id, prepared, events)


@pytest.mark.anyio
async def test_completed_attempt_opens_solution_after_request(tmp_path) -> None:
    """已完成评价的作答 + 明确请求：正常打开并写入揭晓状态。"""

    harness = make_harness(tmp_path, cards=[sample_card()], replies=["这是参考解答。"])
    conversation = harness.service.create_conversation().id
    exercise_id = _seed_exercise(harness, conversation, needs_supplement=False)

    prepared, events = await _reveal_turn(harness, conversation, "看看参考解答")

    result = result_of(events)
    outcome = next(item for item in result.composite_results if item.action == "reveal_solution")
    assert outcome.status == "success"
    exercise = harness.repo.get_exercise(exercise_id)
    assert exercise is not None and exercise.revealed_solution_at is not None
    context = harness.repo.find_turn(
        prepared.conversation_id, prepared.request_id
    ).recovery["reply_context"]
    kinds = [item["kind"] for item in context["authorized"]]
    assert "exercise_solution" in kinds
@pytest.mark.anyio
async def test_topic_only_turn_gets_public_material_not_answer(tmp_path) -> None:
    """只要考点时，查看答案动作降级为公开材料：不 reveal、不读答案。"""

    harness = make_harness(tmp_path, cards=[sample_card()])
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [sample_card()])
    harness.scheduler.push(
        decision(
            ActionPlan(action="select_question", parameters={"index": 1}),
            ActionPlan(action="view_question_answer", parameters={}),
        )
    )

    prepared, events = await run(harness, conversation, "第 1 题考什么知识点")
    result = result_of(events)
    outcome = {item.action: item for item in result.composite_results}["view_question_answer"]
    assert outcome.status == "success"
    assert outcome.details["mode"] == "public_material"
    assert result.question is None or result.question.answer_detail is None
    assert ANSWER_MARK not in json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )
    recovery = turn_recovery(harness, conversation, prepared.request_id)
    context = recovery["reply_context"]
    assert context["teaching"] is None
    assert ANSWER_MARK not in json.dumps(context, ensure_ascii=False)
    assert all("question_answer" != item["kind"] for item in context["authorized"])


@pytest.mark.anyio
async def test_answer_tool_without_student_request_is_rejected(tmp_path) -> None:
    """没有任何“要答案/讲解”的发言时，查看答案被拒绝，且不进入学生可见结果。"""

    harness = make_harness(tmp_path, cards=[sample_card()])
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [sample_card()])
    harness.scheduler.push(
        decision(
            ActionPlan(action="select_question", parameters={"index": 1}),
            ActionPlan(action="view_question_answer", parameters={}),
        )
    )

    prepared, events = await run(harness, conversation, "继续")
    result = result_of(events)
    assert all(item.action != "view_question_answer" for item in result.composite_results)
    assert ANSWER_MARK not in json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )
    # 拒绝信封留在 transcript（给下一轮/审计看），但不作为学生可见的失败步骤。
    row = raw_turn_row(harness.db, conversation, prepared.request_id)
    transcript = json.dumps(transcript_of(row), ensure_ascii=False)
    assert "ANSWER_NOT_REQUESTED" in transcript
    assert ANSWER_MARK not in transcript


@pytest.mark.anyio
async def test_revealed_answer_is_authorized_only_for_this_turn(tmp_path) -> None:
    harness = make_harness(tmp_path, cards=[sample_card()], replies=["答案见卡片。"])
    conversation = harness.service.create_conversation().id
    seed_question_turn(harness.repo, conversation, [sample_card()])
    harness.scheduler.push(
        decision(
            ActionPlan(action="select_question", parameters={"index": 1}),
            ActionPlan(action="view_question_answer", parameters={}),
        )
    )

    prepared, events = await run(harness, conversation, "看第 1 题的答案")
    result = result_of(events)
    outcome = {item.action: item for item in result.composite_results}[
        "view_question_answer"
    ]
    assert outcome.status == "success"
    assert result.question is not None and result.question.answer_detail is not None
    recovery = turn_recovery(harness, conversation, prepared.request_id)
    context = recovery["reply_context"]
    assert context["teaching"] is None
    assert ANSWER_MARK not in json.dumps(context, ensure_ascii=False)
    kinds = [item["kind"] for item in context["authorized"]]
    assert "question_answer" in kinds
    # 跨轮历史里没有答案正文，也不会凭空获得授权。
    assert ANSWER_MARK not in json.dumps(recovery.get("_transcript"), ensure_ascii=False)


def test_rejection_envelope_contains_no_answer_material() -> None:
    outcome = ActionOutcome(
        "view_question_answer",
        "failed",
        answer_gate.REJECTION_MESSAGES["ANSWER_SCOPE_DENIED"],
        details={
            "rejected": True,
            "gate_rejected": True,
            "error_code": "ANSWER_SCOPE_DENIED",
            "remedies": ["explain_problem"],
            "facts": {"question_id": "q-a"},
            "answer_detail": {"text": ANSWER_MARK},
            "answer_url": "/api/answer",
        },
        error_code="ANSWER_SCOPE_DENIED",
    )
    payload = outcome.to_tool_payload()
    text = json.dumps(payload, ensure_ascii=False)
    assert payload["details"]["rejected"] is True
    assert payload["details"]["remedies"] == ["explain_problem"]
    assert ANSWER_MARK not in text and "/api/answer" not in text
    assert tool_projection.project_details("view_question_answer", outcome.details).get(
        "answer_detail"
    ) is None