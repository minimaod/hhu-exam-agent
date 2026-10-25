"""F006 B 批：作答尝试、补充/订正、查看解答标记与有界去重。"""

import json
from pathlib import Path
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.conversations import TurnRequest
from app.conversations.graph import ActionPlan
from app.conversations.service import ConversationService, TurnInput
from app.conversations.repository import ConversationRepository
from app.querying.errors import QueryError
from app.tutoring.exercise_dedup import (
    MAX_GENERATION_ATTEMPTS,
    MAX_RECENT_EXERCISES,
    is_duplicate_statement,
)
from app.tutoring.exercise_llm import AnswerEvaluation, GeneratedExercise
from app.tutoring.models import Source
from tests.test_f006_review_fixes import (
    answered_input,
    collect,
    create_problem_with_exercise,
    decision,
    dispatch_input,
    setup_service,
)

def retry_input(request_id: str, retry_of: str) -> TurnInput:
    return TurnInput(
        request_id=request_id,
        kind=None,
        text="",
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=False,
        retry_of=retry_of,
    )


def supplement_evaluation() -> AnswerEvaluation:
    return AnswerEvaluation(
        is_correct=None,
        final_answer_assessment="只看到答案 1，未看到推导。",
        step_by_step_analysis="无法确认等价无穷小代换是否掌握。",
        guidance="请补充写出变形与代换步骤。",
        needs_supplement=True,
    )


async def run(service: ConversationService, conversation_id: str, data: TurnInput):
    return await collect(service, service.prepare_turn(conversation_id, data))


def result_of(events):
    return next(event for event in events if event.type == "result")


def error_of(events):
    return next(event for event in events if event.type == "error")


@pytest.mark.anyio
async def test_answer_after_reveal_passes_flag_and_marks_independence(tmp_path: Path) -> None:
    service, repository, _, exercise_agent, _ = setup_service(tmp_path)
    conversation = repository.create_conversation()
    problem, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    repository.reveal_exercise_solution(exercise.id)

    events = await run(
        service,
        conversation.id,
        answered_input(str(uuid4()), "1", exercise_id=exercise.id, student_answer="1"),
    )
    result = result_of(events)
    call = exercise_agent.evaluate_calls[0]
    assert call["answered_after_revealed"] is True
    assert call["attempt_kind"] == "initial"
    assert result.exercise is not None
    assert result.exercise.answered_after_revealed is True
    assert len(result.exercise.attempts) == 1
    assert result.exercise.attempts[0].answered_after_revealed is True
    assert result.evaluation is not None
    assert result.evaluation.independence_assessment == "after_reveal"
    assert "对照" in (result.evaluation.independence_note or "")

    # 刷新/重启后仍能读回同一标记。
    restored = ConversationRepository(tmp_path / "study.sqlite3")
    restored.initialize()
    stored = restored.get_exercise(exercise.id)
    assert stored is not None
    evaluation = json.loads(stored.attempts[0].evaluation_json or "{}")
    assert evaluation["independence_assessment"] == "after_reveal"
    assert stored.attempts[0].answered_after_revealed is True


@pytest.mark.anyio
async def test_correction_keeps_first_answer_and_evaluation(tmp_path: Path) -> None:
    service, repository, _, exercise_agent, _ = setup_service(tmp_path)
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )

    first = result_of(
        await run(
            service,
            conversation.id,
            answered_input(
                str(uuid4()), "1", exercise_id=exercise.id, student_answer="1"
            ),
        )
    )
    assert first.evaluation is not None
    first_eval = first.evaluation

    correction = answered_input(
        str(uuid4()),
        "2",
        exercise_id=exercise.id,
        student_answer="2",
        solution_text="重新计算后答案为 2。",
    )
    correction = replace(correction, attempt_kind="correction")
    second = result_of(await run(service, conversation.id, correction))
    assert second.evaluation is not None
    assert second.exercise is not None
    assert [item.kind for item in second.exercise.attempts] == [
        "initial",
        "correction",
    ]
    assert second.exercise.attempts[0].student_answer_text == "1"
    assert second.exercise.attempts[0].evaluation.is_correct is True
    assert second.exercise.attempts[1].student_answer_text == "2"
    assert second.exercise.attempts[1].answered_after_revealed is True
    assert exercise_agent.evaluate_calls[-1]["attempt_kind"] == "correction"
    assert exercise_agent.evaluate_calls[-1]["answered_after_revealed"] is True

    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert len(stored.attempts) == 2
    assert json.loads(stored.attempts[0].evaluation_json or "{}")[
        "final_answer_assessment"
    ] == first_eval.final_answer_assessment


@pytest.mark.anyio
async def test_each_turn_binds_its_own_attempt(tmp_path: Path) -> None:
    first_eval = AnswerEvaluation(
        is_correct=True,
        final_answer_assessment="第一次评价",
        step_by_step_analysis="第一次过程",
        guidance="继续",
        needs_supplement=False,
    )
    second_eval = AnswerEvaluation(
        is_correct=True,
        final_answer_assessment="第二次评价",
        step_by_step_analysis="第二次过程",
        guidance="继续",
        needs_supplement=False,
    )
    service, repository, _, _, _ = setup_service(
        tmp_path, evaluations=[first_eval, second_eval]
    )
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    await run(
        service,
        conversation.id,
        answered_input(str(uuid4()), "1", exercise_id=exercise.id, student_answer="1"),
    )
    await run(
        service,
        conversation.id,
        answered_input(str(uuid4()), "2", exercise_id=exercise.id, student_answer="2"),
    )

    turns = repository.list_turns(conversation.id)
    assert len(turns) == 2
    assert turns[0].exercise is not None and turns[1].exercise is not None
    assert turns[0].exercise.student_answer_text == "1"
    assert len(turns[0].exercise.attempts) == 1
    assert turns[0].exercise.evaluation is not None
    assert turns[0].exercise.evaluation.final_answer_assessment == "第一次评价"
    assert turns[0].exercise.answered_after_revealed is False
    assert turns[0].exercise.revealed_solution_at is None
    assert turns[1].exercise.student_answer_text == "2"
    assert len(turns[1].exercise.attempts) == 2
    assert turns[1].exercise.evaluation is not None
    assert turns[1].exercise.evaluation.final_answer_assessment == "第二次评价"
    assert turns[1].exercise.answered_after_revealed is True

    restored = ConversationRepository(tmp_path / "study.sqlite3")
    restored.initialize()
    restored_turns = restored.list_turns(conversation.id)
    assert [item.exercise.student_answer_text for item in restored_turns] == ["1", "2"]
    assert [len(item.exercise.attempts) for item in restored_turns] == [1, 2]


@pytest.mark.anyio
async def test_failed_evaluation_turn_hides_solution_and_keeps_attempt(tmp_path: Path) -> None:
    service, repository, _, _, _ = setup_service(
        tmp_path, evaluations=[QueryError("MODEL_TIMEOUT", "模型响应超时，请重试。")]
    )
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    await run(
        service,
        conversation.id,
        answered_input(str(uuid4()), "1", exercise_id=exercise.id, student_answer="1"),
    )
    turn = repository.list_turns(conversation.id)[0]
    assert turn.exercise is not None
    assert turn.exercise.student_answer_text == "1"
    assert len(turn.exercise.attempts) == 1
    assert turn.exercise.evaluation is None
    assert turn.exercise.status == "pending_answer"
    assert turn.exercise.reference_solution_text is None


@pytest.mark.anyio
async def test_supplement_hides_solution_and_keeps_answer(tmp_path: Path) -> None:
    service, repository, _, exercise_agent, _ = setup_service(
        tmp_path, evaluations=[supplement_evaluation()]
    )
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )

    first = result_of(
        await run(
            service,
            conversation.id,
            answered_input(
                str(uuid4()), "1", exercise_id=exercise.id, student_answer="1"
            ),
        )
    )
    assert first.exercise is not None
    assert first.exercise.status == "pending_answer"
    assert first.exercise.reference_solution_text is None
    assert first.exercise.evaluation is not None
    assert first.exercise.evaluation.needs_supplement is True

    supplement = answered_input(
        str(uuid4()),
        "补充：先做等价无穷小代换，再做极限。",
        exercise_id=exercise.id,
        student_answer="1",
        solution_text="补充：先做等价无穷小代换，再做极限。",
    )
    supplement = replace(supplement, attempt_kind="supplement")
    second = result_of(await run(service, conversation.id, supplement))
    assert second.exercise is not None
    assert second.exercise.reference_solution_text is not None
    assert [item.kind for item in second.exercise.attempts] == [
        "initial",
        "supplement",
    ]
    assert second.exercise.attempts[0].student_solution_text is None
    assert "补充" in (second.exercise.attempts[1].student_solution_text or "")
    assert second.exercise.attempts[1].student_answer_text == "1"
    assert exercise_agent.evaluate_calls[-1]["attempt_kind"] == "supplement"
    assert exercise_agent.evaluate_calls[-1]["answered_after_revealed"] is False


@pytest.mark.anyio
async def test_evaluation_failure_retry_reuses_same_attempt(tmp_path: Path) -> None:
    service, repository, _, exercise_agent, _ = setup_service(
        tmp_path, evaluations=[QueryError("MODEL_TIMEOUT", "模型响应超时，请重试。")]
    )
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    first_request = str(uuid4())
    first = result_of(
        await run(
            service,
            conversation.id,
            answered_input(
                first_request, "1", exercise_id=exercise.id, student_answer="1"
            ),
        )
    )
    assert first.evaluation is None
    assert first.business_status == "failed"
    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert len(stored.attempts) == 1
    assert stored.attempts[0].evaluation_json is None
    assert stored.attempts[0].student_answer_text == "1"

    retried = result_of(
        await run(service, conversation.id, retry_input(str(uuid4()), first_request))
    )
    assert retried.evaluation is not None
    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert len(stored.attempts) == 1
    assert stored.attempts[0].evaluation_json is not None
    assert exercise_agent.evaluate_calls[-1]["student_answer"] == "1"


@pytest.mark.anyio
async def test_retry_targets_origin_attempt_not_later_correction(tmp_path: Path) -> None:
    service, repository, _, exercise_agent, _ = setup_service(
        tmp_path, evaluations=[QueryError("MODEL_TIMEOUT", "模型响应超时，请重试。")]
    )
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    first_request = str(uuid4())
    result_of(
        await run(
            service,
            conversation.id,
            answered_input(
                first_request, "1", exercise_id=exercise.id, student_answer="1"
            ),
        )
    )

    # 未评价的首次作答之后，学生直接提交了新的答案：形成订正尝试。
    result_of(
        await run(
            service,
            conversation.id,
            answered_input(
                str(uuid4()), "2", exercise_id=exercise.id, student_answer="2"
            ),
        )
    )
    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert [item.kind for item in stored.attempts] == ["initial", "correction"]
    assert stored.attempts[0].evaluation_json is None
    assert stored.attempts[1].evaluation_json is not None

    # 再重试最初失败的轮次：只能评价第一次尝试，不能误写订正尝试。
    retried = result_of(
        await run(service, conversation.id, retry_input(str(uuid4()), first_request))
    )
    assert retried.evaluation is not None
    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert stored.attempts[0].evaluation_json is not None
    assert exercise_agent.evaluate_calls[-1]["student_answer"] == "1"
    assert stored.attempts[1].student_answer_text == "2"


@pytest.mark.anyio
async def test_identical_resubmission_after_evaluation_is_not_a_new_attempt(
    tmp_path: Path,
) -> None:
    service, repository, _, _, _ = setup_service(tmp_path)
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    payload = answered_input(
        str(uuid4()), "1", exercise_id=exercise.id, student_answer="1"
    )
    result_of(await run(service, conversation.id, payload))

    events = await run(
        service,
        conversation.id,
        answered_input(str(uuid4()), "1", exercise_id=exercise.id, student_answer="1"),
    )
    error = error_of(events)
    assert error.code == "BUSINESS_REJECTED"
    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert len(stored.attempts) == 1


@pytest.mark.anyio
async def test_invalid_attempt_kind_is_rejected_before_writing(tmp_path: Path) -> None:
    service, repository, _, _, _ = setup_service(tmp_path)
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    result_of(
        await run(
            service,
            conversation.id,
            answered_input(
                str(uuid4()), "1", exercise_id=exercise.id, student_answer="1"
            ),
        )
    )

    from app.conversations.errors import ConversationConflictError

    with pytest.raises(ConversationConflictError):
        service.prepare_turn(
            conversation.id,
            replace(
                answered_input(
                    str(uuid4()), "2", exercise_id=exercise.id, student_answer="2"
                ),
                attempt_kind="supplement",
            ),
        )
    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert len(stored.attempts) == 1


@pytest.mark.anyio
async def test_generation_dedup_is_bounded_and_reports_honestly(tmp_path: Path) -> None:
    duplicate = GeneratedExercise(
        statement="计算极限 lim x->0 tan(x)/x",
        knowledge_point="等价无穷小",
        reference_solution="tan x ~ x，所以极限为 1。",
    )
    service, repository, scheduler, exercise_agent, _ = setup_service(
        tmp_path,
        scheduler_decisions=[decision(ActionPlan(action="generate_exercise"))],
        exercises=[duplicate, duplicate],
    )
    conversation = repository.create_conversation()
    problem, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )

    events = await run(
        service, conversation.id, dispatch_input(str(uuid4()), "再生成一道类似题")
    )
    result = result_of(events)
    assert result.business_status == "failed"
    assert result.retryable is True
    assert "未保存" in (result.assistant_message or "")
    assert len(exercise_agent.generate_calls) == MAX_GENERATION_ATTEMPTS
    first_call = exercise_agent.generate_calls[0]
    assert exercise.statement_text in first_call["recent_statements"]
    assert duplicate.statement in exercise_agent.generate_calls[1]["avoid_statements"]
    assert len(repository.list_exercises_by_problem(problem.id)) == 1
    failed = [
        item for item in result.composite_results if item.action == "generate_exercise"
    ]
    assert failed and "未保存" in failed[0].message


@pytest.mark.anyio
async def test_generation_saves_numeric_variation(tmp_path: Path) -> None:
    from app.conversations.graph import ActionPlan

    variation = GeneratedExercise(
        statement="计算极限 lim x->0 tan(3x)/(3x)",
        knowledge_point="等价无穷小",
        reference_solution="tan(3x) ~ 3x，所以极限为 1。",
    )
    service, repository, _, exercise_agent, _ = setup_service(
        tmp_path,
        scheduler_decisions=[decision(ActionPlan(action="generate_exercise"))],
        exercises=[variation],
    )
    conversation = repository.create_conversation()
    problem, _ = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    result = result_of(
        await run(service, conversation.id, dispatch_input(str(uuid4()), "再生成一道类似题"))
    )
    assert result.business_status == "succeeded"
    assert len(exercise_agent.generate_calls) == 1
    assert len(repository.list_exercises_by_problem(problem.id)) == 2


def test_recent_statements_are_bounded(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "study.sqlite3")
    repository.initialize()
    conversation = repository.create_conversation()
    problem, _ = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    for index in range(6):
        repository.create_exercise(
            id=str(uuid4()),
            conversation_id=conversation.id,
            problem_id=problem.id,
            root_problem_id=problem.root_problem_id,
            statement_text=f"练习题 {index}",
            reference_solution_text="略",
            knowledge_point="极限",
        )
    recent = repository.list_recent_exercise_statements(
        conversation.id, limit=MAX_RECENT_EXERCISES
    )
    assert len(recent) == MAX_RECENT_EXERCISES
    assert all(item.startswith("练习题") for item in recent)


def test_dedup_rules_allow_number_changes_but_reject_copies() -> None:
    base = "已知函数 f(x)=x^2+1，求 f(2) 的值并说明单调性。"
    assert is_duplicate_statement(base, [base])
    assert not is_duplicate_statement(
        "已知函数 f(x)=x^2+3，求 f(2) 的值并说明单调性。", [base]
    )


def test_api_rejects_attempt_kind_outside_exercise() -> None:
    with pytest.raises(ValidationError):
        TurnRequest(request_id=uuid4(), kind="query", query="你好", attempt_kind="correction")
    with pytest.raises(ValidationError):
        TurnRequest(request_id=uuid4(), query="你好", retry_of=uuid4(), attempt_kind="correction")