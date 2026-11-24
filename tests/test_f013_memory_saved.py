"""F013：学习记忆已写入后超限，saved 必须为真且重试不重复写入。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.quota.errors import ModelQuotaExceeded
from app.quota.gate import next_reset_at
from tests.test_f009_memory_turn import exclusion_count, setup, turn_input
from tests.tool_fakes import ReplyFactory


@pytest.mark.anyio
async def test_memory_delete_then_quota_reports_saved_and_retry_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repo, scheduler, memory, seed = setup(
        tmp_path, ReplyFactory(["已移除。"])
    )
    original_delete = memory.delete
    delete_calls = {"n": 0}

    def counting_delete(*args, **kwargs):
        delete_calls["n"] += 1
        return original_delete(*args, **kwargs)

    monkeypatch.setattr(memory, "delete", counting_delete)
    conversation_id = seed.conversation()
    problem = seed.problem(
        conversation_id, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(
        conversation_id,
        problem,
        knowledge_point="循环的条件与终止条件",
        reference="参考解答",
    )
    entry_id = f"exercise:{exercise}"
    scheduler.push(
        ScheduleDecision(
            confirmed_course="Python语言程序设计",
            actions=[
                ActionPlan(action="review_memory", parameters={}),
                ActionPlan(action="delete_memory", parameters={"entry_id": entry_id}),
            ],
        )
    )
    scheduler.push(ModelQuotaExceeded("site", 500, 500, next_reset_at()))

    first = turn_input("Python 那条循环的题别算了，删掉")
    prepared = service.prepare_turn(conversation_id, first)
    events = [
        event
        async for event in service.stream_prepared(prepared, _request())
    ]
    error = next(event for event in events if event.type == "error")
    assert error.code == "MODEL_QUOTA_EXCEEDED"
    assert error.saved is True

    turn = repo.find_turn(conversation_id, first.request_id)
    assert turn is not None
    assert turn.status == "failed"
    assert turn.recovery.get("business_saved") is True
    assert turn.retryable is True
    delete_actions = [
        entry
        for entry in turn.recovery.get("actions", [])
        if (entry.get("plan") or {}).get("action") == "delete_memory"
    ]
    assert delete_actions and delete_actions[0]["outcome"]["status"] == "success"
    assert exclusion_count(tmp_path / "study.sqlite3") == 1

    retry = replace(turn_input("重试补齐后续步骤"), retry_of=first.request_id)
    retried = service.prepare_turn(conversation_id, retry)
    retry_events = [
        event
        async for event in service.stream_prepared(retried, _request())
    ]
    assert any(event.type == "result" for event in retry_events)
    assert exclusion_count(tmp_path / "study.sqlite3") == 1
    assert memory.summary("Python语言程序设计", days=30, limit=50).counts.excluded == 1
    assert delete_calls["n"] == 1


def _request():
    from tests.test_f009_memory_turn import dummy_request

    return dummy_request()

@pytest.mark.anyio
async def test_memory_checkpoint_failure_rolls_back_memory_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.conversations.errors import ConversationStorageError
    from app.conversations.repository import ConversationRepository

    service, repo, scheduler, memory, seed = setup(
        tmp_path, ReplyFactory(["不会执行到这里"])
    )
    conversation_id = seed.conversation()
    problem = seed.problem(
        conversation_id, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    exercise = seed.exercise(
        conversation_id,
        problem,
        knowledge_point="循环的条件与终止条件",
        reference="参考解答",
    )
    entry_id = f"exercise:{exercise}"
    scheduler.push(
        ScheduleDecision(
            confirmed_course="Python语言程序设计",
            actions=[
                ActionPlan(action="review_memory", parameters={}),
                ActionPlan(action="delete_memory", parameters={"entry_id": entry_id}),
            ],
        )
    )

    original = ConversationRepository.save_recovery_on
    failed = {"done": False}

    def flaky(self, connection, conversation_id, request_id, recovery, *, merge=False):
        actions = recovery.get("actions") or []
        if (
            not failed["done"]
            and any(
                (item.get("plan") or {}).get("action") == "delete_memory"
                for item in actions
            )
        ):
            failed["done"] = True
            raise ConversationStorageError("注入恢复记录失败")
        return original(
            self, connection, conversation_id, request_id, recovery, merge=merge
        )

    monkeypatch.setattr(ConversationRepository, "save_recovery_on", flaky)
    prepared = service.prepare_turn(
        conversation_id, turn_input("Python 那条循环的题别算了，删掉")
    )
    events = [
        event
        async for event in service.stream_prepared(prepared, _request())
    ]
    error = next(event for event in events if event.type == "error")
    assert error.code == "STORAGE_UNAVAILABLE"
    assert error.saved is False
    # 恢复记录写入失败时，记忆写入必须在同一事务里回滚。
    assert exclusion_count(tmp_path / "study.sqlite3") == 0


@pytest.mark.anyio
async def test_second_memory_checkpoint_failure_keeps_first_saved_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.conversations.errors import ConversationStorageError
    from app.conversations.repository import ConversationRepository

    service, repo, scheduler, memory, seed = setup(
        tmp_path, ReplyFactory(["不会执行到这里"])
    )
    conversation_id = seed.conversation()
    problem = seed.problem(
        conversation_id, paper_id="py-2023-2024-s2-final", source_type="registered_paper"
    )
    first = seed.exercise(
        conversation_id, problem, knowledge_point="循环", reference="参考解答 1"
    )
    second = seed.exercise(
        conversation_id, problem, knowledge_point="函数", reference="参考解答 2"
    )
    first_id = f"exercise:{first}"
    second_id = f"exercise:{second}"
    scheduler.push(
        ScheduleDecision(
            confirmed_course="Python语言程序设计",
            actions=[
                ActionPlan(action="review_memory", parameters={}),
                ActionPlan(action="delete_memory", parameters={"entry_id": first_id}),
                ActionPlan(action="delete_memory", parameters={"entry_id": second_id}),
            ],
        )
    )

    original = ConversationRepository.save_recovery_on
    failed = {"done": False}

    def flaky(self, connection, conversation_id, request_id, recovery, *, merge=False):
        actions = recovery.get("actions") or []
        if not failed["done"] and any(
            (item.get("plan") or {}).get("action") == "delete_memory"
            and (item.get("plan") or {}).get("parameters", {}).get("entry_id") == second_id
            and item.get("outcome")
            for item in actions
        ):
            failed["done"] = True
            raise ConversationStorageError("注入第二项检查点失败")
        return original(
            self, connection, conversation_id, request_id, recovery, merge=merge
        )

    monkeypatch.setattr(ConversationRepository, "save_recovery_on", flaky)
    prepared = service.prepare_turn(
        conversation_id, turn_input("这两条循环和函数的题都别算了，删掉")
    )
    events = [
        event
        async for event in service.stream_prepared(prepared, _request())
    ]
    error = next(event for event in events if event.type == "error")
    assert error.code == "STORAGE_UNAVAILABLE"
    assert error.saved is True
    turn = repo.find_turn(conversation_id, prepared.request_id)
    assert turn is not None
    assert turn.recovery.get("business_saved") is True
    assert exclusion_count(tmp_path / "study.sqlite3") == 1
