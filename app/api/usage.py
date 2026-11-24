"""当前账号的今日用量快照。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.dependencies import require_user
from app.auth.models import User
from app.quota.errors import QuotaStorageError

router = APIRouter(tags=["usage"])


@router.get("/usage/today", summary="读取当前账号的今日模型与图片额度")
def usage_today(
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> dict[str, object]:
    config = getattr(request.app.state, "usage_config", None)
    gate = getattr(request.app.state, "usage_gate", None)
    if gate is None:
        detail = getattr(config, "error", None) or "用量限制尚未配置。"
        raise HTTPException(status_code=503, detail=detail)
    try:
        snapshot = gate.snapshot(user.id)
    except QuotaStorageError as exc:
        raise HTTPException(status_code=503, detail=exc.message) from exc
    # 普通用户只看个人剩余次数和全站是否耗尽，不暴露全站精确用量。
    return snapshot