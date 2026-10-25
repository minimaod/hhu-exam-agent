"""FastAPI 应用入口。

浏览器请求先进入这里创建的 app，再按路径分发到 app/api/ 下的路由。
本文件只负责组装应用、挂载路由和统一异常映射。

异常映射放在这里，是因为 4xx / 5xx 是对外契约的一部分，而业务异常本身
不关心 HTTP。这样服务层可以重复使用，接口层也不必在每个函数里写 try/except。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.attachments import router as attachments_router
from app.api.conversations import router as conversations_router
from app.api.health import router as health_router
from app.api.mistakes import router as mistakes_router
from app.api.papers import router as papers_router
from app.api.query import router as query_router
from app.config import load_config_state
from app.conversations.errors import (
    ActiveProblemConflictError,
    AttachmentInvalidError,
    AttachmentNotFoundError,
    AttachmentStorageError,
    AttachmentTooLargeError,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationQueryUnavailableError,
    ConversationStorageError,
    InvalidImageError,
    MistakeRecordConflictError,
    MistakeRecordNotFoundError,
    ProblemNotFoundError,
    ProblemNotReadyError,
    SourceInvalidError,
    TurnInvalidError,
    TutoringUnavailableError,
    UnsupportedImageError,
)
from app.conversations.service import build_store_state
from app.papers.errors import CatalogUnavailableError, PaperUnavailableError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时固定配置快照并初始化本地学习数据库。

    配置缺失、数据库损坏或结构版本不兼容都不阻止应用启动：对应接口返回
    503，健康检查、手动检索与下载仍可用。修改 .env 后需重启后端。
    """

    app.state.query_config = load_config_state()
    app.state.conversation_store = build_store_state()
    yield


app = FastAPI(
    title="河海大学考试复习助手",
    version="0.1.0",
    description="河海大学历年试卷复习助手的后端服务。",
    lifespan=lifespan,
)

# 各路由在自己的模块里只写 /health、/papers 这类相对路径，
# 统一在 /api 下对外暴露；前端开发时由 Vite 代理到同一路径。
app.include_router(health_router, prefix="/api")
app.include_router(papers_router, prefix="/api")
app.include_router(query_router, prefix="/api")
app.include_router(conversations_router, prefix="/api")
app.include_router(attachments_router, prefix="/api")
app.include_router(mistakes_router, prefix="/api")


def _detail_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": message})


@app.exception_handler(CatalogUnavailableError)
async def handle_catalog_unavailable(
    request: Request,
    exc: CatalogUnavailableError,
) -> JSONResponse:
    """目录整体不可用：明确报 503，不伪装成“没有试卷”。"""

    return _detail_response(503, str(exc))


@app.exception_handler(PaperUnavailableError)
async def handle_paper_unavailable(
    request: Request,
    exc: PaperUnavailableError,
) -> JSONResponse:
    """单份试卷不可下载（未登记、文件丢失、越界、非 PDF）：404。"""

    return _detail_response(404, str(exc))


@app.exception_handler(ConversationNotFoundError)
async def handle_conversation_not_found(
    request: Request,
    exc: ConversationNotFoundError,
) -> JSONResponse:
    """会话 ID 未登记：404。"""

    return _detail_response(404, str(exc))


@app.exception_handler(ConversationConflictError)
async def handle_conversation_conflict(
    request: Request,
    exc: ConversationConflictError,
) -> JSONResponse:
    """重复 request_id 内容冲突或已有运行中轮次：409。"""

    return _detail_response(409, str(exc))


@app.exception_handler(ConversationStorageError)
async def handle_conversation_storage(
    request: Request,
    exc: ConversationStorageError,
) -> JSONResponse:
    """本地学习数据库不可用、损坏或版本不兼容：503。"""

    return _detail_response(503, str(exc))


@app.exception_handler(ConversationQueryUnavailableError)
async def handle_conversation_query_unavailable(
    request: Request,
    exc: ConversationQueryUnavailableError,
) -> JSONResponse:
    """新轮次缺少模型配置：503；读取历史不受影响。"""

    return _detail_response(503, exc.message)


@app.exception_handler(TurnInvalidError)
async def handle_turn_invalid(
    request: Request,
    exc: TurnInvalidError,
) -> JSONResponse:
    return _detail_response(422, str(exc))


@app.exception_handler(ProblemNotFoundError)
async def handle_problem_not_found(
    request: Request,
    exc: ProblemNotFoundError,
) -> JSONResponse:
    return _detail_response(404, str(exc))


@app.exception_handler(ProblemNotReadyError)
async def handle_problem_not_ready(
    request: Request,
    exc: ProblemNotReadyError,
) -> JSONResponse:
    return _detail_response(409, str(exc))


@app.exception_handler(ActiveProblemConflictError)
async def handle_active_problem_conflict(
    request: Request,
    exc: ActiveProblemConflictError,
) -> JSONResponse:
    return _detail_response(409, str(exc))


@app.exception_handler(AttachmentNotFoundError)
async def handle_attachment_not_found(
    request: Request,
    exc: AttachmentNotFoundError,
) -> JSONResponse:
    return _detail_response(404, str(exc))


@app.exception_handler(AttachmentInvalidError)
async def handle_attachment_invalid(
    request: Request,
    exc: AttachmentInvalidError,
) -> JSONResponse:
    return _detail_response(422, str(exc))


@app.exception_handler(AttachmentTooLargeError)
async def handle_attachment_too_large(
    request: Request,
    exc: AttachmentTooLargeError,
) -> JSONResponse:
    return _detail_response(413, str(exc))


@app.exception_handler(UnsupportedImageError)
async def handle_unsupported_image(
    request: Request,
    exc: UnsupportedImageError,
) -> JSONResponse:
    return _detail_response(415, str(exc))


@app.exception_handler(InvalidImageError)
async def handle_invalid_image(
    request: Request,
    exc: InvalidImageError,
) -> JSONResponse:
    return _detail_response(422, str(exc))


@app.exception_handler(AttachmentStorageError)
async def handle_attachment_storage(
    request: Request,
    exc: AttachmentStorageError,
) -> JSONResponse:
    return _detail_response(503, str(exc))


@app.exception_handler(TutoringUnavailableError)
async def handle_tutoring_unavailable(
    request: Request,
    exc: TutoringUnavailableError,
) -> JSONResponse:
    return _detail_response(503, exc.message)


@app.exception_handler(SourceInvalidError)
async def handle_source_invalid(
    request: Request,
    exc: SourceInvalidError,
) -> JSONResponse:
    return _detail_response(422, str(exc))


@app.exception_handler(MistakeRecordNotFoundError)
async def handle_mistake_record_not_found(
    request: Request,
    exc: MistakeRecordNotFoundError,
) -> JSONResponse:
    return _detail_response(404, str(exc))


@app.exception_handler(MistakeRecordConflictError)
async def handle_mistake_record_conflict(
    request: Request,
    exc: MistakeRecordConflictError,
) -> JSONResponse:
    content: dict[str, object] = {"detail": str(exc)}
    if exc.record_id is not None:
        content["record_id"] = exc.record_id
    return JSONResponse(status_code=409, content=content)
