"""通用 SSE 业务事件运行器。

查询和辅导都需要：在后台执行模型/工作流、把进度事件交给接口层、限制总时长、
在客户端断开时取消任务。这里只处理调度，不认识具体事件类型；终止事件和
超时事件由调用方提供。
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import TypeVar

from fastapi import Request

T = TypeVar("T")

DISCONNECT_POLL_SECONDS = 0.2
SENTINEL = object()


async def stream_events(
    produce: Callable[[asyncio.Queue[object]], Awaitable[None]],
    request: Request,
    *,
    timeout: float,
    timeout_event: T,
    is_terminal: Callable[[T], bool],
) -> AsyncIterator[T]:
    """执行 produce 并把事件逐条产出；断开或超时结束。

    produce 必须在结束时向队列放入 SENTINEL；它产生的异常由 produce 自己
    转成业务错误事件，未捕获异常只终止流，不把原始异常交给客户端。
    """

    queue: asyncio.Queue[object] = asyncio.Queue()
    worker = asyncio.create_task(produce(queue))
    getter = asyncio.create_task(queue.get())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                yield timeout_event
                return

            done, _ = await asyncio.wait(
                {getter},
                timeout=min(DISCONNECT_POLL_SECONDS, remaining),
            )

            if getter not in done:
                if await request.is_disconnected():
                    return
                continue

            event = getter.result()
            if event is SENTINEL:
                return

            yield event  # type: ignore[misc]
            if is_terminal(event):  # type: ignore[arg-type]
                return

            getter = asyncio.create_task(queue.get())
    finally:
        getter.cancel()
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await getter
        with suppress(asyncio.CancelledError):
            await worker
