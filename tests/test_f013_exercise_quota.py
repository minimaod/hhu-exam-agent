"""F013：练习评价超限必须返回 saved=true 的终止 error，并可复用原作答重试。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.quota.errors import ModelQuotaExceeded
from app.quota.gate import next_reset_at
from app.tutoring.exercise_llm import AnswerEvaluation
from tests.test_f006_b_attempts import retry_input
from tests.test_f006_review_fixes import (
    answered_input,
    collect,
    create_problem_with_exercise,
    setup_service,
)


def _successful_evaluation() -> AnswerEvaluation:
    return AnswerEvaluation(
        is_correct=True,
        final_answer_assessment="答案正确，极限值为 1。",
        step_by_step_analysis="正确完成等价无穷小代换。",
        guidance="继续保持。",
        needs_supplement=False,
    )


@pytest.mark.anyio
async def test_exercise_evaluation_quota_keeps_attempt_and_retries_only_evaluation(
    tmp_path: Path,
) -> None:
    service, repository, _, agent, _ = setup_service(
        tmp_path,
        evaluations=[ModelQuotaExceeded("site", 500, 500, next_reset_at())],
    )
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    request_id = str(uuid4())
    prepared = service.prepare_turn(
        conversation.id,
        answered_input(
            request_id,
            "1",
            exercise_id=exercise.id,
            student_answer="1",
        ),
    )
    events = await collect(service, prepared)
    assert not any(event.type == "result" for event in events)
    error = next(event for event in events if event.type == "error")
    assert error.code == "MODEL_QUOTA_EXCEEDED"
    assert error.saved is True
    assert error.quota is not None
    assert error.quota.scope == "site"

    turn = repository.find_turn(conversation.id, request_id)
    assert turn is not None
    assert turn.status == "failed"
    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert len(stored.attempts) == 1

    # 额度恢复后只补评价：复用原 attempt，不重复插入作答。
    agent.push_evaluation(_successful_evaluation())
    retried = service.prepare_turn(
        conversation.id,
        retry_input(str(uuid4()), request_id),
    )
    retry_events = await collect(service, retried)
    assert any(event.type == "result" for event in retry_events)
    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert len(stored.attempts) == 1
    assert stored.attempts[0].evaluation_json is not None
    assert len(agent.evaluate_calls) == 2