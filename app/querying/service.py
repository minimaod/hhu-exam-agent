"""一次自然语言查询的请求内编排：工作流 → SSE 事件。

SSE 只是传输方式；业务完成以最后一条 result/error 为准。这里负责：
- 把工作流产生的进度与结果编码成 data: 帧；
- 对整次查询施加 45 秒总时限；
- 客户端断开时停止本地消费并取消工作流任务；
- 把领域异常集中映射成对外错误码，不把原始异常返回前端。
"""

import asyncio
import logging
from collections.abc import AsyncIterator

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
from app.sse import encode_sse_event
from app.streaming import SENTINEL, stream_events

TOTAL_TIMEOUT_SECONDS = 45.0

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}

logger = logging.getLogger(__name__)

_CATALOG_MESSAGE = "试卷目录暂时不可用，请稍后再试。"
_QUERY_FAILED_MESSAGE = "查询过程中发生错误，请重试。"
_TIMEOUT_MESSAGE = "查询超时，请重试。"


def encode_event(event: QueryEvent) -> str:
    """一条事件一个 data 帧；编码规则在 app/sse.py。"""

    return encode_sse_event(event)


class QueryService:
    """把理解器与试卷服务组装成一次请求的工作流。"""

    def __init__(self, understand: Understander, papers: PaperService) -> None:
        self._understand = understand
        self._papers = papers

    async def stream(self, query: str, request: Request) -> AsyncIterator[str]:
        """旧 /api/query 使用的 SSE 字符串流；业务逻辑在 events() 中。"""

        async for event in self.events(query, request):
            yield encode_event(event)

    async def events(self, query: str, request: Request) -> AsyncIterator[QueryEvent]:
        """产出类型明确的一次查询事件，供不同接口共同消费。

        业务完成以最后一条 result 或 error 为准；客户端断开时直接结束，
        不产生终止事件。
        """

        async for event in stream_events(
            lambda queue: self._produce(query, queue),
            request,
            timeout=TOTAL_TIMEOUT_SECONDS,
            timeout_event=ErrorEvent(code="QUERY_FAILED", message=_TIMEOUT_MESSAGE),
            is_terminal=lambda item: isinstance(item, (ResultEvent, ErrorEvent)),
        ):
            yield event

    async def _produce(
        self,
        query: str,
        queue: "asyncio.Queue[object]",
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
