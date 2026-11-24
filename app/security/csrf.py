"""Cookie 登录下的写请求 CSRF 校验。

SameSite=Lax 已经阻止跨站表单携带 Cookie；这里再加自定义头，跨站脚本/表单
无法在没有 CORS 许可的情况下伪造。若浏览器带 Origin，则必须命中允许列表。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.config import AuthConfigState

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "x-hhu-csrf"


def require_csrf(request: Request, config: AuthConfigState) -> None:
    if request.method.upper() not in UNSAFE_METHODS:
        return
    if request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status_code=403, detail="请求缺少安全标记。")
    origin = request.headers.get("origin")
    if origin and config.allowed_origins:
        normalized = origin.rstrip("/")
        if normalized not in config.allowed_origins:
            raise HTTPException(status_code=403, detail="请求来源不被允许。")