"""跨请求纠正互斥、取消释放和陈旧结果保护；不调用真实模型。"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing
from threading import Event
from uuid import uuid4

import pytest

from app.api.conversations import ConversationServiceState, get_conversation_service_state
from app.conversations.errors import ConversationStorageError, MistakeRecordNotFoundError
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService
from app.main import app
from app.mistakes.service import CorrectionInput, MistakeRecordService
from app.quota.errors import TurnInProgressError
from tests.f012_support import TEST_USER_ID, ensure_owner
from tests.test_f010b_support import dummy_request
from tests.test_mistake_records import env, _last_event, _problem, _record  # noqa: F401
from tests.tutoring_fakes import clear_understanding, sample_explanation


@pytest.fixture
def case(env):
    conversation = env.client.post("/api/conversations", json={}).json()["id"]
    _problem(env, conversation, "原题")
    record = _record(env, conversation)
    registry = app.state.lease_registry
    services = []

    def new_service(owner=TEST_USER_ID):
        repo = ConversationRepository(env.repo.db_path).scoped(owner)
        records = MistakeRecordService(
            repo, env.records._papers, make_tutor=lambda: env.tutor,
            tutoring_error=None, attachments_root=env.records._attachments_root,
        )
        service = ConversationService(
            repo, make_query=None, query_error=None,
            make_tutor=lambda: env.tutor, records=records,
            lease_registry=registry,
        )
        services.append(service)
        return service

    # 和生产相同：每次请求构造新服务，只有数据库与应用登记表共享。
    app.dependency_overrides[get_conversation_service_state] = lambda: (
        ConversationServiceState(service=new_service(), error=None)
    )
    return env, conversation, record, new_service, registry, services


def payload():
    return {"request_id": str(uuid4()), "statement_text": "纠正后的题干"}


def correction():
    return CorrectionInput("纠正后的题干", None, None, None, None)


def queue_success(env):
    env.tutor.push_understanding(clear_understanding("纠正后的题干"))
    env.tutor.push_explanation(sample_explanation())


def test_fresh_http_requests_share_user_slot_and_release_after_success(case):
    env, conversation, record, _, _, services = case
    started, release = Event(), Event()
    original = env.tutor.understand_problem

    async def blocked(text, images):
        started.set()
        assert await asyncio.to_thread(release.wait, 10)
        return await original(text, images)

    env.tutor.understand_problem = blocked
    queue_success(env)
    body = payload()
    path = f"/api/mistake-records/{record}/corrections"
    before = len(env.tutor.understand_calls)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(env.client.post, path, json=body)
        try:
            assert started.wait(5)
            for second_body in (body, payload()):
                second = env.client.post(path, json=second_body)
                assert second.status_code == 409, second.text
                assert second.json()["code"] == "TURN_IN_PROGRESS"
            turn = env.client.post(
                f"/api/conversations/{conversation}/turns",
                json={"request_id": str(uuid4()), "kind": "record", "query": "保存"},
            )
            assert turn.status_code == 409, turn.text
            assert env.repo.has_active_lease()
            assert env.repo.list_record_revisions(record) == []
        finally:
            release.set()
        assert _last_event(first.result(timeout=10))["type"] == "result"
    assert len({id(s) for s in services}) >= 4
    assert len(env.tutor.understand_calls) == before + 1
    assert len(env.repo.list_record_revisions(record)) == 1
    assert not env.repo.has_active_lease()
    env.tutor.understand_problem = original
    queue_success(env)
    assert _last_event(env.client.post(path, json=payload()))["type"] == "result"


@pytest.mark.parametrize("active", ["turn", "reply"])
def test_active_turn_or_reply_rejects_correction_before_model(case, active):
    env, conversation, record, _, _, _ = case
    if active == "turn":
        env.repo.start_turn(conversation, str(uuid4()), "已有轮次", lease_token="other")
    else:
        env.repo.acquire_reply_lease(conversation, str(uuid4()), "other")
    calls = len(env.tutor.understand_calls)
    response = env.client.post(f"/api/mistake-records/{record}/corrections", json=payload())
    assert response.status_code == 409, response.text
    assert len(env.tutor.understand_calls) == calls
    assert env.repo.has_active_lease()  # 被拒绝的请求不得释放原请求。
    env.repo.release_turn_slot("other")


def test_replay_is_read_only_even_with_active_slot_and_zero_quota(case):
    env, conversation, record, _, _, _ = case
    queue_success(env)
    body = payload()
    path = f"/api/mistake-records/{record}/corrections"
    assert _last_event(env.client.post(path, json=body))["type"] == "result"
    calls = len(env.tutor.understand_calls)
    env.repo.acquire_reply_lease(conversation, str(uuid4()), "other")
    app.state.usage_gate.set_override(
        TEST_USER_ID, daily_requests=0, daily_images=None, expires_on="2099-01-01"
    )
    response = env.client.post(path, json=body)
    assert _last_event(response)["result"]["revision_created"] is False
    assert len(env.tutor.understand_calls) == calls
    assert env.repo.has_active_lease()
    env.repo.release_turn_slot("other")


def test_another_account_cannot_lock_record_and_can_run_own_turn(case):
    env, _, record, new_service, _, _ = case
    owner = str(uuid4())
    ensure_owner(env.repo.db_path, owner)
    other = new_service(owner)
    with pytest.raises(MistakeRecordNotFoundError):
        other.prepare_record_correction(record, str(uuid4()))
    assert not other._repository.has_active_lease()
    first = new_service()
    token = first.prepare_record_correction(record, str(uuid4()))
    conversation = other.create_conversation().id
    other._repository.start_turn(conversation, str(uuid4()), "B 的轮次", lease_token="b")
    assert env.repo.has_active_lease()
    assert other._repository.has_active_lease()
    with pytest.raises(TurnInProgressError):
        env.repo.acquire_reply_lease(case[1], str(uuid4()), "a-reply")
    first._release_lease(token)
    other._repository.release_turn_slot("b")


def test_metadata_changed_during_generation_is_not_overwritten(case):
    env, _, record, _, _, _ = case
    original = env.tutor.understand_problem

    async def edited(text, images):
        env.repo.update_record_metadata(record, title="并发修改的新标题")
        return await original(text, images)

    env.tutor.understand_problem = edited
    queue_success(env)
    event = _last_event(env.client.post(
        f"/api/mistake-records/{record}/corrections", json=payload()
    ))
    assert event["type"] == "error"
    assert "已被修改" in event["message"]
    saved = env.repo.get_record(record)
    assert saved.title == "并发修改的新标题"
    assert saved.statement_text == "原题"
    assert env.repo.list_record_revisions(record) == []
    assert not env.repo.has_active_lease()


@pytest.mark.anyio
@pytest.mark.parametrize("ending", ["cancel", "close", "disconnect", "timeout"])
async def test_worker_stops_before_slot_release(case, monkeypatch, ending):
    env, _, record, new_service, registry, _ = case
    service = new_service()
    started, stopped = asyncio.Event(), asyncio.Event()
    active_at_stop = []

    async def blocked(text, images):
        started.set()
        try:
            await asyncio.Future()
        finally:
            active_at_stop.append(env.repo.has_active_lease())
            stopped.set()

    env.tutor.understand_problem = blocked
    request = dummy_request()
    if ending == "disconnect":
        async def disconnected():
            return started.is_set()
        monkeypatch.setattr(request, "is_disconnected", disconnected)
    if ending == "timeout":
        monkeypatch.setattr("app.mistakes.service.CORRECTION_TIMEOUT_SECONDS", 0.05)
    token = service.prepare_record_correction(record, str(uuid4()))
    stream = service.stream_record_correction(record, str(uuid4()), correction(), request, token)
    events = []

    async def consume():
        async with aclosing(stream):
            async for event in stream:
                events.append(event)
                if ending == "close":
                    await started.wait()
                    break

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 2)
    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await asyncio.wait_for(task, 2)
    assert stopped.is_set()
    assert active_at_stop == [True]
    assert not env.repo.has_active_lease()
    assert not registry.is_active(token)
    assert env.repo.list_record_revisions(record) == []
    if ending == "timeout":
        assert events[-1].type == "error"


@pytest.mark.anyio
@pytest.mark.parametrize("spec", ["2.3", "2.4"])
async def test_http_disconnect_closes_worker_and_releases_slot(case, spec):
    env, _, record, _, _, _ = case
    started, stopped = asyncio.Event(), asyncio.Event()
    active_at_stop = []

    async def blocked(text, images):
        started.set()
        try:
            await asyncio.Future()
        finally:
            # 清理可能含异步操作，必须等待它完成后才能允许下一次请求。
            await asyncio.sleep(0.01)
            active_at_stop.append(env.repo.has_active_lease())
            stopped.set()

    env.tutor.understand_problem = blocked
    body = json.dumps(payload()).encode()
    first_receive = True

    async def receive():
        nonlocal first_receive
        if first_receive:
            first_receive = False
            return {"type": "http.request", "body": body, "more_body": False}
        if spec == "2.3":
            await started.wait()
            return {"type": "http.disconnect"}
        await asyncio.Future()

    async def send(message):
        if spec == "2.4" and message["type"] == "http.response.body":
            await started.wait()
            raise OSError("客户端关闭连接")

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": f"/api/mistake-records/{record}/corrections",
        "query_string": b"", "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    from starlette.requests import ClientDisconnect
    try:
        await asyncio.wait_for(app(scope, receive, send), 3)
    except ClientDisconnect:
        assert spec == "2.4"
    assert stopped.is_set()
    assert active_at_stop == [True]
    assert not env.repo.has_active_lease()


def test_release_failure_is_recovered_by_next_request(case, monkeypatch):
    env, _, record, new_service, registry, _ = case
    original = ConversationRepository.release_turn_slot
    unavailable = True

    def flaky(self, token):
        if unavailable:
            raise ConversationStorageError("释放故障")
        return original(self, token)

    monkeypatch.setattr(ConversationRepository, "release_turn_slot", flaky)
    queue_success(env)
    path = f"/api/mistake-records/{record}/corrections"
    assert _last_event(env.client.post(path, json=payload()))["type"] == "result"
    assert env.repo.has_active_lease()
    assert registry.orphans()
    unavailable = False
    service = new_service()
    token = service.prepare_record_correction(record, str(uuid4()))
    assert registry.orphans() == []
    service._release_lease(token)
    assert not env.repo.has_active_lease()


@pytest.mark.anyio
async def test_send_fails_before_stream_starts_releases_reserved_slot(case):
    from app.api.mistakes import CorrectionRequest, correct_record
    from app.auth.models import User
    from uuid import UUID
    from starlette.requests import ClientDisconnect

    env, _, record, new_service, registry, _ = case
    service = new_service()
    request = dummy_request()
    request.scope["app"] = app
    request.scope["asgi"] = {"spec_version": "2.4"}
    response = await correct_record(
        UUID(record), CorrectionRequest(**payload()), request,
        service.records, service,
        User(id=TEST_USER_ID, username="test", created_at="2026-01-01T00:00:00Z"),
    )
    calls = len(env.tutor.understand_calls)
    assert env.repo.has_active_lease()

    async def send(message):
        raise OSError("响应头发送失败")

    with pytest.raises(ClientDisconnect):
        await response(request.scope, request.receive, send)
    assert not env.repo.has_active_lease()
    assert registry._leases == {}
    assert len(env.tutor.understand_calls) == calls


def test_unstarted_response_and_failed_preparation_are_reclaimable(case, monkeypatch):
    env, _, record, new_service, registry, _ = case
    registry.clock = lambda: 0.0
    service = new_service()
    abandoned = service.prepare_record_correction(record, str(uuid4()))
    registry.clock = lambda: 10000.0
    service2 = new_service()
    token = service2.prepare_record_correction(record, str(uuid4()))
    assert not registry.is_active(abandoned)
    service2._release_lease(token)

    original = service._repository.acquire_correction_lease
    original_release = service._repository.release_turn_slot

    def broken(*args):
        original(*args)
        raise ConversationStorageError("占用已写入，准备失败")

    def cannot_release(token):
        raise ConversationStorageError("释放故障")

    monkeypatch.setattr(service._repository, "acquire_correction_lease", broken)
    monkeypatch.setattr(service._repository, "release_turn_slot", cannot_release)
    with pytest.raises(ConversationStorageError):
        service.prepare_record_correction(record, str(uuid4()))
    assert env.repo.has_active_lease()
    assert registry.orphans()
    monkeypatch.setattr(service._repository, "release_turn_slot", original_release)
    recovered = new_service()
    token = recovered.prepare_record_correction(record, str(uuid4()))
    recovered._release_lease(token)
    assert not env.repo.has_active_lease()
