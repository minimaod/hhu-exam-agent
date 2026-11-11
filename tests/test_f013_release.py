"""F013：释放失败后保留登记并在下一次请求回收；/reply 共用回收流程。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.conversations.errors import ConversationStorageError
from app.quota.lease import LeaseRegistry
from tests.f012_support import TEST_USER_ID
from tests.test_f010b_support import (
    dummy_request,
    make_harness,
    turn_input,
)


@pytest.mark.anyio
async def test_failed_release_is_retried_by_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, owner_id=TEST_USER_ID)
    registry = LeaseRegistry(reclaim_seconds=0, clock=lambda: 0.0)
    harness.service._lease_registry = registry
    conversation = harness.service.create_conversation().id
    first = turn_input("第一轮")
    prepared = harness.service.prepare_turn(conversation, first)
    token = harness.service._lease_by_request[first.request_id]

    original = harness.repo.release_turn_slot
    failed = {"done": False}

    def flaky(release_token: str) -> bool:
        if release_token == token and not failed["done"]:
            failed["done"] = True
            raise ConversationStorageError("注入释放失败")
        return original(release_token)

    monkeypatch.setattr(harness.repo, "release_turn_slot", flaky)
    events = [
        event
        async for event in harness.service.stream_prepared(
            prepared, dummy_request()
        )
    ]
    assert any(event.type == "result" for event in events)
    # 数据库释放失败：内存登记仍然保留，等待下一次请求回收。
    assert registry.is_active(token) is True

    second = turn_input("第二轮")
    prepared_second = harness.service.prepare_turn(conversation, second)
    assert prepared_second.request_id == second.request_id
    assert registry.is_active(token) is False


@pytest.mark.anyio
async def test_reply_sweeps_stale_lease_before_acquiring(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path, owner_id=TEST_USER_ID)
    registry = LeaseRegistry(reclaim_seconds=0, clock=lambda: 0.0)
    harness.service._lease_registry = registry
    conversation = harness.service.create_conversation().id
    first = turn_input("第一轮")
    prepared = harness.service.prepare_turn(conversation, first)
    events = [
        event
        async for event in harness.service.stream_prepared(
            prepared, dummy_request()
        )
    ]
    assert any(event.type == "result" for event in events)

    # 模拟一个执行已结束、但数据库释放失败的 /reply 占用。
    harness.repo.acquire_reply_lease(
        conversation, first.request_id, "stale-reply-lease"
    )
    registry.prepare("stale-reply-lease")
    registry.clock = lambda: 10**6

    regenerated = await harness.service.regenerate_reply(
        conversation, first.request_id
    )
    assert regenerated["assistant_message"]
    assert registry.is_active("stale-reply-lease") is False

@pytest.mark.anyio
async def test_prepare_failure_then_release_failure_can_be_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, owner_id=TEST_USER_ID)
    registry = LeaseRegistry(reclaim_seconds=0, clock=lambda: 0.0)
    harness.service._lease_registry = registry
    conversation = harness.service.create_conversation().id
    original_start = harness.repo.start_turn
    original_release = harness.repo.release_turn_slot
    failed = {"start": False, "release": False}

    def broken_start(*args, **kwargs):
        if not failed["start"]:
            failed["start"] = True
            original_start(*args, **kwargs)
            raise ConversationStorageError("注入轮次准备失败")
        return original_start(*args, **kwargs)

    def broken_release(token: str) -> bool:
        if not failed["release"]:
            failed["release"] = True
            raise ConversationStorageError("注入首次释放失败")
        return original_release(token)

    monkeypatch.setattr(harness.repo, "start_turn", broken_start)
    monkeypatch.setattr(harness.repo, "release_turn_slot", broken_release)
    first = turn_input("准备失败的轮次")
    with pytest.raises(ConversationStorageError):
        harness.service.prepare_turn(conversation, first)

    # 数据库恢复后，同一账号可以自行回收并开启下一轮。
    second = turn_input("恢复后的轮次")
    prepared = harness.service.prepare_turn(conversation, second)
    assert prepared.request_id == second.request_id
