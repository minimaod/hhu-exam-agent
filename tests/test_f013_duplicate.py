"""F013：重复 request_id 被拒绝时不能打断原轮次。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.conversations.errors import ConversationConflictError
from tests.f012_support import TEST_USER_ID
from tests.test_f010b_support import dummy_request, make_harness, turn_input


@pytest.mark.anyio
async def test_duplicate_request_does_not_interrupt_running_turn(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path, owner_id=TEST_USER_ID)
    conversation = harness.service.create_conversation().id
    first = turn_input("你好")
    prepared = harness.service.prepare_turn(conversation, first)

    with pytest.raises(ConversationConflictError):
        harness.service.prepare_turn(conversation, first)

    turn = harness.repo.find_turn(conversation, first.request_id)
    assert turn is not None
    assert turn.status == "running"

    events = [
        event
        async for event in harness.service.stream_prepared(
            prepared, dummy_request()
        )
    ]
    assert any(event.type == "result" for event in events)
    turn = harness.repo.find_turn(conversation, first.request_id)
    assert turn is not None
    assert turn.status == "completed"