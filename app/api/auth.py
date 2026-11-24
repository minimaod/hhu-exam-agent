"""F012 认证接口：注册、登录、退出与当前用户。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth.dependencies import (
    get_auth_config,
    get_auth_service,
    get_session_token,
    require_public_csrf,
    require_user,
)
from app.auth.errors import (
    AuthStorageError,
    InvalidCredentialsError,
    InviteInvalidError,
    UsernameTakenError,
)
from app.auth.models import User
from app.auth.passwords import AuthPasswordError
from app.auth.service import AuthService
from app.config import AuthConfigState

router = APIRouter(tags=["auth"])


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invite_code: str = Field(min_length=6, max_length=200)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserView(BaseModel):
    id: str
    username: str
    created_at: str


def _view(user: User) -> UserView:
    return UserView(id=user.id, username=user.username, created_at=user.created_at)


def _set_cookie(
    response: Response,
    config: AuthConfigState,
    token: str,
    *,
    max_age: int,
) -> None:
    response.set_cookie(
        key=config.cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure,
        path="/",
    )


@router.post(
    "/auth/register",
    status_code=201,
    response_model=UserView,
    dependencies=[Depends(require_public_csrf)],
    summary="凭一次性邀请码注册",
)
def register(
    payload: RegisterRequest,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    config: Annotated[AuthConfigState, Depends(get_auth_config)],
) -> UserView:
    try:
        user, session = service.register(
            invite_code=payload.invite_code,
            username=payload.username,
            password=payload.password,
        )
    except AuthPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UsernameTakenError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InviteInvalidError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AuthStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _set_cookie(
        response,
        config,
        session.token,
        max_age=config.session_ttl_days * 24 * 3600,
    )
    return _view(user)


@router.post(
    "/auth/login",
    response_model=UserView,
    dependencies=[Depends(require_public_csrf)],
    summary="用户名密码登录",
)
def login(
    payload: LoginRequest,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    config: Annotated[AuthConfigState, Depends(get_auth_config)],
) -> UserView:
    try:
        user, session = service.login(
            username=payload.username,
            password=payload.password,
        )
    except AuthPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except AuthStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _set_cookie(
        response,
        config,
        session.token,
        max_age=config.session_ttl_days * 24 * 3600,
    )
    return _view(user)


@router.post(
    "/auth/logout",
    status_code=204,
    summary="退出当前会话",
)
def logout(
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    config: Annotated[AuthConfigState, Depends(get_auth_config)],
    _user: Annotated[User, Depends(require_user)],
) -> Response:
    token = get_session_token(request)
    if token:
        service.logout(token)
    response.delete_cookie(
        key=config.cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure,
    )
    response.status_code = 204
    return response


@router.get("/me", response_model=UserView, summary="当前登录用户")
def me(user: Annotated[User, Depends(require_user)]) -> UserView:
    return _view(user)