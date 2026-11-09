"""FastAPI 认证依赖。"""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.auth.models import User
from app.auth.service import AuthService
from app.config import AuthConfigState
from app.security.csrf import require_csrf


def get_auth_config(request: Request) -> AuthConfigState:
    return request.app.state.auth_config


def get_auth_service(request: Request) -> AuthService:
    service: AuthService | None = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="账号服务暂时不可用，请稍后再试。",
        )
    return service


def get_session_token(request: Request) -> str | None:
    config = get_auth_config(request)
    return request.cookies.get(config.cookie_name)


def require_user(request: Request) -> User:
    """身份只从服务端会话取得；写请求随后校验 CSRF。"""

    config = get_auth_config(request)
    if config.error:
        raise HTTPException(status_code=503, detail=config.error)
    service = get_auth_service(request)
    token = get_session_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="请先登录。")
    user = service.current_user(token)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录。")
    require_csrf(request, config)
    return user


def require_public_csrf(request: Request) -> None:
    """注册/登录没有会话，但仍拒绝没有安全头的写请求。"""

    config = get_auth_config(request)
    if config.error:
        raise HTTPException(status_code=503, detail=config.error)
    require_csrf(request, config)