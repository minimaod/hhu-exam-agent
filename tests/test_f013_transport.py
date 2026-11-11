"""F013：在真实 SDK/传输边界验证记账、重试与超限不发请求。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.config import DeepSeekSettings
from app.conversations import schema
from app.llm import build_chat_model
from app.quota.errors import ModelQuotaExceeded
from app.quota.gate import UsageGate, UsageLimits
from app.quota.transport import QuotaTransport, bind_actor


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "study.sqlite3"
    schema.initialize_database(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO users (id, username, username_normalized, password_hash, created_at) "
            "VALUES ('u1', 'u1', 'u1', 'x', '2026-01-01')"
        )
        connection.commit()
    finally:
        connection.close()
    return db


def _completion(content: str = "ok") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _model(gate: UsageGate, handler, *, max_retries: int = 0):
    inner = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=QuotaTransport(gate, inner))
    return build_chat_model(
        DeepSeekSettings(api_key="test", base_url="https://api.deepseek.com", model="deepseek-chat"),
        max_tokens=16,
        timeout=5.0,
        json_mode=False,
        http_async_client=client,
        max_retries=max_retries,
    )


def _used(db: Path) -> int:
    connection = sqlite3.connect(db)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM model_usage_events WHERE kind='model_request'"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def test_each_actual_request_is_counted_once(tmp_path: Path) -> None:
    db = _db(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_completion())

    gate = UsageGate(db, limits=UsageLimits(5, 100, 1))
    model = _model(gate, handler)

    async def run() -> None:
        with bind_actor("u1"):
            await model.ainvoke([("human", "hi")])

    asyncio.run(run())
    assert len(calls) == 1
    assert _used(db) == 1


def test_quota_exhausted_does_not_send(tmp_path: Path) -> None:
    db = _db(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_completion())

    gate = UsageGate(db, limits=UsageLimits(0, 100, 1))
    model = _model(gate, handler)

    async def run() -> None:
        with bind_actor("u1"):
            with pytest.raises(ModelQuotaExceeded):
                await model.ainvoke([("human", "hi")])

    asyncio.run(run())
    assert calls == []
    assert _used(db) == 0


def test_retry_counts_each_attempt_and_stops_at_quota(tmp_path: Path) -> None:
    db = _db(tmp_path)
    calls: list[httpx.Request] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, json={"error": {"message": "try again"}})
        return httpx.Response(200, json=_completion())

    gate = UsageGate(db, limits=UsageLimits(5, 100, 1))
    model = _model(gate, flaky, max_retries=1)

    async def run() -> None:
        with bind_actor("u1"):
            await model.ainvoke([("human", "hi")])

    asyncio.run(run())
    assert len(calls) == 2
    assert _used(db) == 2


def test_second_retry_is_not_sent_when_only_one_quota_left(tmp_path: Path) -> None:
    db = _db(tmp_path)
    calls: list[httpx.Request] = []

    def always_fail(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"error": {"message": "try again"}})

    gate = UsageGate(db, limits=UsageLimits(1, 100, 1))
    model = _model(gate, always_fail, max_retries=1)

    async def run() -> None:
        with bind_actor("u1"):
            with pytest.raises(ModelQuotaExceeded):
                await model.ainvoke([("human", "hi")])

    asyncio.run(run())
    assert len(calls) == 1
    assert _used(db) == 1

def test_quota_storage_error_stops_before_send(tmp_path: Path) -> None:
    from app.quota.errors import QuotaStorageError

    db = _db(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_completion())

    gate = UsageGate(db, limits=UsageLimits(5, 100, 1))

    def broken(*args, **kwargs):
        raise QuotaStorageError()

    gate.acquire_model_request = broken  # type: ignore[method-assign]
    model = _model(gate, handler)

    async def run() -> None:
        with bind_actor("u1"):
            with pytest.raises(QuotaStorageError):
                await model.ainvoke([("human", "hi")])

    asyncio.run(run())
    assert calls == []
