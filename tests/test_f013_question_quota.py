"""F013：真题卡片讲解/变式入口的额度错误与正文重生成。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.conversations.reply import StreamingReply
from app.quota.errors import ModelQuotaExceeded
from app.quota.gate import next_reset_at
from tests.test_f013_business_saved import QuotaReplyModel
from tests.test_question_turns import (
    add_search_turn,
    build_service,
    run,
    turn_input,
)
from tests.tool_fakes import ReplyFactory
from tests.question_fakes import sample_card
from tests.tutoring_fakes import FakeTutor, clear_understanding, sample_explanation


@pytest.mark.anyio
async def test_explain_question_final_reply_quota_is_terminal_and_regenerable(
    tmp_path: Path,
) -> None:
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, repository, _ = build_service(tmp_path, tutor=tutor)
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])
    await run(
        service,
        conversation.id,
        turn_input(
            kind="select_question", question_id=card.question_id, source_turn=source
        ),
    )

    service._make_reply = lambda: StreamingReply(
        model=QuotaReplyModel("site")
    )
    data = turn_input(
        kind="explain_question", question_id=card.question_id, source_turn=source
    )
    events = await run(service, conversation.id, data)
    assert not any(event.type == "result" for event in events)
    error = next(event for event in events if event.type == "error")
    assert error.code == "MODEL_QUOTA_EXCEEDED"
    assert error.saved is True
    assert error.quota is not None
    assert error.quota.scope == "site"

    turn = repository.find_turn(conversation.id, data.request_id)
    assert turn is not None
    assert turn.status == "completed"
    assert turn.recovery.get("reply_input")

    service._make_reply = ReplyFactory(["完整讲解正文。"])
    regenerated = await service.regenerate_reply(
        conversation.id, data.request_id
    )
    assert regenerated["assistant_message"] == "完整讲解正文。"


@pytest.mark.anyio
async def test_explain_question_model_quota_keeps_code_and_quota(
    tmp_path: Path,
) -> None:
    tutor = FakeTutor(
        understandings=[clear_understanding("题目")],
        explanations=[ModelQuotaExceeded("user", 30, 30, next_reset_at())],
    )
    service, repository, _ = build_service(tmp_path, tutor=tutor)
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])
    await run(
        service,
        conversation.id,
        turn_input(
            kind="select_question", question_id=card.question_id, source_turn=source
        ),
    )
    events = await run(
        service,
        conversation.id,
        turn_input(
            kind="explain_question",
            question_id=card.question_id,
            source_turn=source,
        ),
    )
    error = next(event for event in events if event.type == "error")
    assert error.code == "MODEL_QUOTA_EXCEEDED"
    assert error.quota is not None
    assert error.quota.scope == "user"
    assert error.saved is False

@pytest.mark.anyio
async def test_card_exercise_quota_keeps_code_and_quota(tmp_path: Path) -> None:
    from tests.tutoring_fakes import FakeExerciseAgent

    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, repository, _ = build_service(tmp_path, tutor=tutor)
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])
    await run(
        service,
        conversation.id,
        turn_input(
            kind="explain_question", question_id=card.question_id, source_turn=source
        ),
    )
    service._make_exercise_agent = lambda: FakeExerciseAgent(
        exercises=[ModelQuotaExceeded("site", 500, 500, next_reset_at())]
    )
    events = await run(
        service,
        conversation.id,
        turn_input(
            kind=None,
            text="",
            dispatch=True,
            question_id=card.question_id,
            source_turn=source,
            question_action="exercise",
        ),
    )
    error = next(event for event in events if event.type == "error")
    assert error.code == "MODEL_QUOTA_EXCEEDED"
    assert error.quota is not None
    assert error.quota.scope == "site"
