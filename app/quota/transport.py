"""按登录账号记账的 httpx 传输层包装。

放在真正发往供应商的 HTTP 边界：每次 SDK 尝试（包括重试）都会先执行一次
SQLite 事务；超限或用量库不可用时在发送前抛出，供应商收不到请求。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import AsyncIterator

import httpx

from app.quota.gate import UsageGate

_ACTOR: ContextVar[str | None] = ContextVar("hhu_quota_actor", default=None)
_REQUEST_ID: ContextVar[str | None] = ContextVar(
    "hhu_quota_request_id", default=None
)
_CONVERSATION_ID: ContextVar[str | None] = ContextVar(
    "hhu_quota_conversation_id", default=None
)


def current_actor() -> str | None:
    return _ACTOR.get()


def set_actor(owner_id: str | None) -> None:
    """认证依赖成功时绑定账号；流式 worker 会继承当前上下文。"""

    _ACTOR.set(owner_id)


@contextmanager
def bind_actor(
    owner_id: str,
    *,
    request_id: str | None = None,
    conversation_id: str | None = None,
) -> AsyncIterator[None]:
    """在流式生成器/`/reply` 内绑定当前账号；worker 任务会继承该上下文。"""

    tokens = [
        _ACTOR.set(owner_id),
        _REQUEST_ID.set(request_id),
        _CONVERSATION_ID.set(conversation_id),
    ]
    try:
        yield
    finally:
        _CONVERSATION_ID.reset(tokens[2])
        _REQUEST_ID.reset(tokens[1])
        _ACTOR.reset(tokens[0])


class QuotaTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        gate: UsageGate,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._gate = gate
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        owner_id = current_actor()
        if not owner_id:
            # 生产路径必须在请求/流式执行内绑定账号；没有身份时不放行。
            from app.quota.errors import QuotaStorageError

            raise QuotaStorageError()
        self._gate.acquire_model_request(
            owner_id,
            conversation_id=_CONVERSATION_ID.get(),
            request_id=_REQUEST_ID.get(),
        )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()