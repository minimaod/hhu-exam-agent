"""认证用例：注册、登录、退出和管理员命令共用。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.auth.errors import (
    InvalidCredentialsError,
    UsernameTakenError,
)
from app.auth.models import IssuedSession, User
from app.auth.passwords import (
    AuthPasswordError,
    dummy_verify,
    hash_password,
    validate_password,
    validate_username,
    verify_password,
)
from app.auth.repository import AuthRepository

_INVALID_CREDENTIALS = "用户名或密码错误。"


@dataclass(frozen=True)
class AuthSettings:
    cookie_name: str = "hhu_session"
    cookie_secure: bool = False
    session_ttl_days: int = 14
    allowed_origins: tuple[str, ...] = ()


class AuthService:
    def __init__(self, db_path: Path, settings: AuthSettings) -> None:
        self._repository = AuthRepository(db_path)
        self._settings = settings

    @property
    def settings(self) -> AuthSettings:
        return self._settings

    @property
    def repository(self) -> AuthRepository:
        return self._repository

    # ------------------------------------------------------------------
    def register(
        self, *, invite_code: str, username: str, password: str
    ) -> tuple[User, IssuedSession]:
        clean_username = validate_username(username)
        validate_password(password)
        user = self._repository.register_with_invite(
            invite_code=invite_code,
            username=clean_username,
            password_hash=hash_password(password),
        )
        return user, self._issue(user)

    def login(self, *, username: str, password: str) -> tuple[User, IssuedSession]:
        user = self._repository.find_user_by_username(username)
        if user is None:
            dummy_verify(password)
            raise InvalidCredentialsError(_INVALID_CREDENTIALS)
        stored = self._repository.get_password_hash(user.id)
        if user.disabled or stored is None or not verify_password(password, stored):
            dummy_verify(password)
            raise InvalidCredentialsError(_INVALID_CREDENTIALS)
        return user, self._issue(user)

    def logout(self, token: str) -> None:
        self._repository.revoke_session(token)

    def current_user(self, token: str) -> User | None:
        return self._repository.resolve_session(token)

    def _issue(self, user: User) -> IssuedSession:
        return self._repository.create_session(
            user.id, ttl_days=self._settings.session_ttl_days
        )

    # ------------------------------------------------------------------
    # 管理员命令行
    # ------------------------------------------------------------------
    def create_invite(
        self,
        *,
        label: str | None = None,
        expires_in_days: int | None = None,
        created_by: str | None = None,
    ) -> tuple[str, str]:
        return self._repository.create_invite(
            label=label,
            expires_in_days=expires_in_days,
            created_by=created_by,
        )

    def list_users(self) -> list[User]:
        return self._repository.list_users()

    def disable_user(self, username: str) -> User | None:
        return self._repository.disable_user(username)

    def prune_sessions(self) -> int:
        return self._repository.prune_sessions()


__all__ = [
    "AuthService",
    "AuthSettings",
    "AuthPasswordError",
    "InvalidCredentialsError",
    "UsernameTakenError",
]