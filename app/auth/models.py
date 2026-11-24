"""认证领域对象。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    id: str
    username: str
    created_at: str
    disabled_at: str | None = None

    @property
    def disabled(self) -> bool:
        return self.disabled_at is not None


@dataclass(frozen=True)
class IssuedSession:
    token: str
    expires_at: str