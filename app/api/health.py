"""健康检查接口。

只确认后端应用可访问、服务标识符合约定，不依赖数据库、模型或 PDF 文件。
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """健康检查的成功响应，也是接口契约的代码表达。"""

    status: Literal["ok"]
    service: Literal["hhu-exam-review"]


@router.get("/health", response_model=HealthResponse, summary="检查后端服务")
def read_health() -> HealthResponse:
    """返回固定的服务状态；能到达这里就说明后端进程与路由均已就绪。"""

    return HealthResponse(status="ok", service="hhu-exam-review")
