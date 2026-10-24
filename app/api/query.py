"""自然语言查询接口。

接口层只做三件事：校验问题文本、读取启动时固定的配置状态、把服务产生的
SSE 帧交给 FastAPI 输出。参数错误和配置缺失在建立流之前返回 HTTP 错误；
流开始后的故障通过 error 事件结束。

配置在 app/main.py 的 lifespan 中读取一次并保存到 app.state；请求阶段只读
快照，所以修改 .env 后需要重启后端。配置错误不在这里抛异常，而是等请求体
校验通过后再决定是否返回 503，保证非法请求仍先得到 422。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from app.config import QueryConfigState
from app.papers.service import PaperService, get_paper_service
from app.querying.llm import DeepSeekUnderstanding
from app.querying.service import SSE_HEADERS, QueryService

router = APIRouter(tags=["query"])

MAX_QUERY_LENGTH = 1000


class QueryRequest(BaseModel):
    """POST /api/query 的请求体。"""

    query: str

    @field_validator("query")
    @classmethod
    def _normalize(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("query 去除首尾空白后不能为空。")
        if len(text) > MAX_QUERY_LENGTH:
            raise ValueError(f"query 最长为 {MAX_QUERY_LENGTH} 个字符。")
        return text


def get_query_config(request: Request) -> QueryConfigState:
    """读取启动时固定的配置快照。"""

    return request.app.state.query_config


def get_query_service(
    config: Annotated[QueryConfigState, Depends(get_query_config)],
    papers: Annotated[PaperService, Depends(get_paper_service)],
) -> QueryService | None:
    """配置可用时按查询需要初始化模型适配器；否则返回 None。"""

    if config.settings is None:
        return None
    return QueryService(
        understand=DeepSeekUnderstanding(config.settings),
        papers=papers,
    )


@router.post("/query", summary="用自然语言查询试卷")
async def query_papers(
    payload: QueryRequest,
    request: Request,
    config: Annotated[QueryConfigState, Depends(get_query_config)],
    service: Annotated[QueryService | None, Depends(get_query_service)],
) -> StreamingResponse:
    """返回 text/event-stream：progress 若干，最后一条 result 或 error。"""

    if config.settings is None or service is None:
        raise HTTPException(
            status_code=503,
            detail=config.error or "自然语言查询尚未配置。",
        )

    return StreamingResponse(
        service.stream(payload.query, request),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
