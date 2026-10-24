"""FastAPI 应用入口。

浏览器请求先进入这里创建的 app，再按路径分发到 app/api/ 下的路由。
本文件只负责组装应用、挂载路由和统一异常映射；

异常映射放在这里，是因为 503 / 404 是对外契约的一部分，而业务异常本身
不关心 HTTP。这样服务层可以重复使用，接口层也不必在每个函数里写 try/except。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.papers import router as papers_router
from app.api.query import router as query_router
from app.config import load_config_state
from app.papers.errors import CatalogUnavailableError, PaperUnavailableError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时固定一份配置快照；修改 .env 后需重启后端。

    配置缺失或畸形不阻止应用启动：查询接口会返回 503，健康检查与
    F002 的查询/下载不受影响。
    """

    app.state.query_config = load_config_state()
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


@app.exception_handler(CatalogUnavailableError)
async def handle_catalog_unavailable(
    request: Request,
    exc: CatalogUnavailableError,
) -> JSONResponse:
    """目录整体不可用：明确报 503，不伪装成“没有试卷”。"""

    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(PaperUnavailableError)
async def handle_paper_unavailable(
    request: Request,
    exc: PaperUnavailableError,
) -> JSONResponse:
    """单份试卷不可下载（未登记、文件丢失、越界、非 PDF）：404。"""

    return JSONResponse(status_code=404, content={"detail": str(exc)})
