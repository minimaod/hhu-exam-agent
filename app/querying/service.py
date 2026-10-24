"""一次自然语言查询的请求内编排：工作流 → SSE 事件。

SSE 只是传输方式；业务完成以最后一条 result/error 为准。这里负责：
- 把工作流产生的进度与结果编码成 data: 帧；
- 对整次查询施加 45 秒总时限；
- 客户端断开时停止本地消费并取消工作流任务；
- 把领域异常集中映射成对外错误码，不把原始异常返回前端。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import suppress

from fastapi import Request

from app.papers.errors import CatalogUnavailableError
from app.papers.service import PaperService
from app.querying.errors import QueryError
from app.querying.models import (
    ErrorEvent,
    ProgressEvent,
    ProgressStage,
    QueryEvent,
    ResultEvent,
    Understander,
)
from app.querying.workflow import build_workflow

TOTAL_TIMEOUT_SECONDS = 45.0
DISCONNECT_POLL_SECONDS = 0.2
SENTINEL = None

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}

logger = logging.getLogger(__name__)

_CATALOG_MESSAGE = "试卷目录暂时不可用，请稍后再试。"
_QUERY_FAILED_MESSAGE = "查询过程中发生错误，请重试。"
_TIMEOUT_MESSAGE = "查询超时，请重试。"


def encode_event(event: QueryEvent) -> str:
    """一条事件一个 data 帧；JSON 使用 UTF-8 原文，中文不转义。"""

    payload = event.model_dump(mode="json")
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


class QueryService:
    """把理解器与试卷服务组装成一次请求的工作流。"""

    def __init__(self, understand: Understander, papers: PaperService) -> None:
        self._understand = understand
        self._papers = papers

    async def stream(self, query: str, request: Request) -> AsyncIterator[str]:
        queue: asyncio.Queue[QueryEvent | None] = asyncio.Queue()
        worker = asyncio.create_task(self._produce(query, queue))
        getter = asyncio.create_task(queue.get())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TOTAL_TIMEOUT_SECONDS

        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    yield encode_event(ErrorEvent(code="QUERY_FAILED", message=_TIMEOUT_MESSAGE))
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

                yield encode_event(event)
                if isinstance(event, (ResultEvent, ErrorEvent)):
                    return

                getter = asyncio.create_task(queue.get())
        finally:
            getter.cancel()
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await getter
            with suppress(asyncio.CancelledError):
                await worker

    async def _produce(
        self,
        query: str,
        queue: "asyncio.Queue[QueryEvent | None]",
    ) -> None:
        async def emit(stage: ProgressStage, message: str) -> None:
            # 闭包只暴露“发一条进度”的能力，不把队列交给工作流节点。
            queue.put_nowait(ProgressEvent(stage=stage, message=message))

        try:
            workflow = build_workflow(self._understand, self._papers, emit)
            state = await workflow.ainvoke({"query": query})
            result = state.get("result")
            if not isinstance(result, ResultEvent):
                raise QueryError("QUERY_FAILED", _QUERY_FAILED_MESSAGE)
            queue.put_nowait(result)
        except QueryError as exc:
            queue.put_nowait(ErrorEvent(code=exc.code, message=exc.message))
        except CatalogUnavailableError:
            queue.put_nowait(
                ErrorEvent(code="CATALOG_UNAVAILABLE", message=_CATALOG_MESSAGE)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # 未预期异常只留在服务端日志；前端只看到可读的通用错误。
            logger.exception("自然语言查询执行失败")
            queue.put_nowait(
                ErrorEvent(code="QUERY_FAILED", message=_QUERY_FAILED_MESSAGE)
            )
        finally:
            queue.put_nowait(SENTINEL)
