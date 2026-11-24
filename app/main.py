"""FastAPI 应用入口。

浏览器请求先进入这里创建的 app，再按路径分发到 app/api/ 下的路由。
本文件只负责组装应用、挂载路由和统一异常映射。

异常映射放在这里，是因为 4xx / 5xx 是对外契约的一部分，而业务异常本身
不关心 HTTP。这样服务层可以重复使用，接口层也不必在每个函数里写 try/except。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

from app.api.attachments import router as attachments_router
from app.api.auth import router as auth_router
from app.api.contributions import router as contributions_router
from app.api.conversations import router as conversations_router
from app.api.health import router as health_router
from app.api.materials import router as materials_router
from app.api.memory import router as memory_router
from app.api.mistakes import router as mistakes_router
from app.api.papers import router as papers_router
from app.api.query import router as query_router
from app.api.usage import router as usage_router
from app.auth.dependencies import require_user
from app.auth.service import AuthService, AuthSettings
from app.config import (
    MaterialConfigState,
    load_auth_config_state,
    load_config_state,
    load_contribution_config_state,
    load_usage_config_state,
)
from app.contributions.errors import (
    ContributionInvalidError,
    ContributionParseTimeoutError,
    ContributionStorageError,
    ContributionTooLargeError,
)
from app.materials.errors import (
    GuideInsufficientError,
    GuideNotFoundError,
    MaterialConflictError,
    MaterialInvalidError,
    MaterialNotFoundError,
    MaterialPageLimitError,
    MaterialParseTimeoutError,
    MaterialStorageError,
    MaterialTooLargeError,
)
from app.quota.errors import (
    ImageQuotaExceeded,
    ModelQuotaExceeded,
    QuotaStorageError,
    TurnInProgressError,
)
from app.quota.gate import UsageGate, UsageLimits
from app.quota.lease import LeaseRegistry
from app.quota.transport import QuotaTransport
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
    MemoryCorrectionUnsupportedError,
    MemoryEntryNotFoundError,
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
from app.questions.errors import QuestionError
from app.questions.runtime import build_question_runtime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时固定配置快照并初始化本地学习数据库。

    配置缺失、数据库损坏或结构版本不兼容都不阻止应用启动：对应接口返回
    503，健康检查、手动检索与下载仍可用。修改 .env 后需重启后端。
    """

    app.state.query_config = load_config_state()
    app.state.auth_config = load_auth_config_state()
    app.state.usage_config = load_usage_config_state()
    app.state.material_config = MaterialConfigState()
    app.state.contribution_config = load_contribution_config_state()
    app.state.conversation_store = build_store_state()
    app.state.question_runtime = build_question_runtime()
    app.state.usage_gate = None
    app.state.model_http_client = None
    app.state.lease_registry = LeaseRegistry()
    store = app.state.conversation_store
    auth_config = app.state.auth_config
    usage_config = app.state.usage_config
    app.state.auth_service = None

    if store.db_path is not None and store.error is None:
        try:
            from app.materials.service import MaterialService

            MaterialService.cleanup_pending_files_on_startup(store.db_path)
        except Exception:
            pass
    if (
        store.db_path is not None
        and store.error is None
        and usage_config.error is None
    ):
        app.state.usage_gate = UsageGate(
            store.db_path,
            limits=UsageLimits(
                model_requests=usage_config.model_requests,
                site_requests=usage_config.site_requests,
                images=usage_config.images,
            ),
            unlimited_owner_id=usage_config.unlimited_owner_id,
        )
        app.state.model_http_client = httpx.AsyncClient(
            transport=QuotaTransport(app.state.usage_gate),
            timeout=None,
        )
    if (
        store.db_path is not None
        and store.error is None
        and auth_config.error is None
    ):
        app.state.auth_service = AuthService(
            store.db_path,
            AuthSettings(
                cookie_name=auth_config.cookie_name,
                cookie_secure=auth_config.cookie_secure,
                session_ttl_days=auth_config.session_ttl_days,
                allowed_origins=auth_config.allowed_origins,
            ),
        )
        try:
            app.state.auth_service.prune_sessions()
        except Exception:
            pass
    try:
        yield
    finally:
        client = getattr(app.state, "model_http_client", None)
        if client is not None:
            await client.aclose()
        runtime = getattr(app.state, "question_runtime", None)
        if runtime is not None:
            runtime.close()


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
app.include_router(auth_router, prefix="/api")
app.include_router(
    query_router, prefix="/api", dependencies=[Depends(require_user)]
)
app.include_router(
    conversations_router, prefix="/api", dependencies=[Depends(require_user)]
)
app.include_router(
    attachments_router, prefix="/api", dependencies=[Depends(require_user)]
)
app.include_router(
    mistakes_router, prefix="/api", dependencies=[Depends(require_user)]
)
app.include_router(
    memory_router, prefix="/api", dependencies=[Depends(require_user)]
)
app.include_router(
    usage_router, prefix="/api", dependencies=[Depends(require_user)]
)
app.include_router(
    materials_router, prefix="/api", dependencies=[Depends(require_user)]
)
app.include_router(
    contributions_router, prefix="/api", dependencies=[Depends(require_user)]
)


def _detail_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": message})


@app.exception_handler(MaterialNotFoundError)
@app.exception_handler(GuideNotFoundError)
async def handle_material_not_found(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    return _detail_response(404, str(exc))


@app.exception_handler(MaterialTooLargeError)
async def handle_material_too_large(
    request: Request,
    exc: MaterialTooLargeError,
) -> JSONResponse:
    return _detail_response(413, str(exc))


@app.exception_handler(MaterialInvalidError)
@app.exception_handler(MaterialPageLimitError)
@app.exception_handler(GuideInsufficientError)
async def handle_material_unprocessable(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    return _detail_response(422, str(exc))


@app.exception_handler(MaterialConflictError)
async def handle_material_conflict(
    request: Request,
    exc: MaterialConflictError,
) -> JSONResponse:
    return _detail_response(409, str(exc))


@app.exception_handler(MaterialParseTimeoutError)
async def handle_material_timeout(
    request: Request,
    exc: MaterialParseTimeoutError,
) -> JSONResponse:
    return _detail_response(504, str(exc))


@app.exception_handler(MaterialStorageError)
async def handle_material_storage(
    request: Request,
    exc: MaterialStorageError,
) -> JSONResponse:
    return _detail_response(503, str(exc))


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


@app.exception_handler(MemoryEntryNotFoundError)
async def handle_memory_entry_not_found(
    request: Request,
    exc: MemoryEntryNotFoundError,
) -> JSONResponse:
    """学习记忆条目不存在（或原记录已删）：404。"""

    return _detail_response(404, str(exc))


@app.exception_handler(MemoryCorrectionUnsupportedError)
async def handle_memory_correction_unsupported(
    request: Request,
    exc: MemoryCorrectionUnsupportedError,
) -> JSONResponse:
    """单次作答只能整体移除，或纠正内容为空：422。"""

    return _detail_response(422, str(exc))


@app.exception_handler(ModelQuotaExceeded)
async def handle_model_quota(
    request: Request,
    exc: ModelQuotaExceeded,
) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "detail": exc.message,
            "code": exc.code,
            "quota": exc.quota(),
        },
    )


@app.exception_handler(ImageQuotaExceeded)
async def handle_image_quota(
    request: Request,
    exc: ImageQuotaExceeded,
) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "detail": exc.message,
            "code": exc.code,
            "quota": exc.quota(),
        },
    )


@app.exception_handler(TurnInProgressError)
async def handle_turn_in_progress(
    request: Request,
    exc: TurnInProgressError,
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "code": "TURN_IN_PROGRESS"},
    )


@app.exception_handler(QuotaStorageError)
async def handle_quota_storage(
    request: Request,
    exc: QuotaStorageError,
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": exc.message, "code": exc.code},
    )


@app.exception_handler(QuestionError)
async def handle_question_error(
    request: Request,
    exc: QuestionError,
) -> JSONResponse:
    """题库集成错误：404/409/410/503 由异常自身给出，正文不含内部路径。"""

    return _detail_response(exc.status_code, exc.message)


@app.exception_handler(ContributionTooLargeError)
async def handle_contribution_too_large(
    request: Request,
    exc: ContributionTooLargeError,
) -> JSONResponse:
    return _detail_response(413, str(exc))


@app.exception_handler(ContributionInvalidError)
async def handle_contribution_invalid(
    request: Request,
    exc: ContributionInvalidError,
) -> JSONResponse:
    return _detail_response(422, str(exc))


@app.exception_handler(ContributionParseTimeoutError)
async def handle_contribution_timeout(
    request: Request,
    exc: ContributionParseTimeoutError,
) -> JSONResponse:
    return _detail_response(504, str(exc))


@app.exception_handler(ContributionStorageError)
async def handle_contribution_storage(
    request: Request,
    exc: ContributionStorageError,
) -> JSONResponse:
    return _detail_response(503, str(exc))
