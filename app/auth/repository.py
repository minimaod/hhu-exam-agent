"""用户、邀请码和登录会话的 SQLite 读写。

注册事务顺序（F012 导师意见）：先验证邀请码，再插入用户，最后条件更新
邀请码为已使用。这样 ``used_by`` 外键指向的用户已经存在；用户名冲突时
回滚，邀请码仍可使用。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from app.auth.errors import (
    AuthStorageError,
    InviteInvalidError,
    UsernameTakenError,
)
from app.auth.models import IssuedSession, User
from app.auth.passwords import normalize_username

_STORAGE_MESSAGE = "账号数据暂时不可用，请稍后再试。"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AuthRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        if not self._db_path.is_file():
            raise AuthStorageError(_STORAGE_MESSAGE)
        try:
            connection = sqlite3.connect(self._db_path, timeout=5.0)
        except (sqlite3.Error, OSError) as exc:
            raise AuthStorageError(_STORAGE_MESSAGE) from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        except sqlite3.Error as exc:
            raise AuthStorageError(_STORAGE_MESSAGE) from exc
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def find_user_by_username(self, username: str) -> User | None:
        normalized = normalize_username(username)
        with self._open() as connection:
            row = connection.execute(
                "SELECT id, username, created_at, disabled_at FROM users "
                "WHERE username_normalized = ?",
                (normalized,),
            ).fetchone()
        return None if row is None else self._row_to_user(row)

    def find_user_by_id(self, user_id: str) -> User | None:
        with self._open() as connection:
            row = connection.execute(
                "SELECT id, username, created_at, disabled_at FROM users "
                "WHERE id = ?",
                (user_id,),
            ).fetchone()
        return None if row is None else self._row_to_user(row)

    def list_users(self) -> list[User]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT id, username, created_at, disabled_at FROM users "
                "ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def _password_hash(self, user_id: str) -> str | None:
        with self._open() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return None if row is None else str(row["password_hash"])

    def get_password_hash(self, user_id: str) -> str | None:
        return self._password_hash(user_id)

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register_with_invite(
        self,
        *,
        invite_code: str,
        username: str,
        password_hash: str,
        user_id: str | None = None,
    ) -> User:
        """验证邀请码 → 插入用户 → 条件更新邀请码；任一步失败回滚。"""

        now = _now()
        code_digest = _hash_secret(invite_code.strip())
        normalized = normalize_username(username)
        new_id = user_id or str(uuid.uuid4())

        with self._open() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                invite = connection.execute(
                    "SELECT id, used_at, expires_at FROM invite_codes "
                    "WHERE code_sha256 = ?",
                    (code_digest,),
                ).fetchone()
                if invite is None or invite["used_at"] is not None:
                    raise InviteInvalidError("邀请码无效、已使用或已过期。")
                if invite["expires_at"] is not None and str(
                    invite["expires_at"]
                ) <= now:
                    raise InviteInvalidError("邀请码无效、已使用或已过期。")

                exists = connection.execute(
                    "SELECT 1 FROM users WHERE username_normalized = ?",
                    (normalized,),
                ).fetchone()
                if exists is not None:
                    raise UsernameTakenError("用户名已被使用。")

                connection.execute(
                    """
                    INSERT INTO users (
                        id, username, username_normalized, password_hash,
                        created_at, disabled_at
                    ) VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (new_id, username.strip(), normalized, password_hash, now),
                )
                cursor = connection.execute(
                    """
                    UPDATE invite_codes
                    SET used_at = ?, used_by = ?
                    WHERE id = ?
                      AND used_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    """,
                    (now, new_id, invite["id"], now),
                )
                if cursor.rowcount != 1:
                    raise InviteInvalidError("邀请码无效、已使用或已过期。")
                connection.commit()
            except (InviteInvalidError, UsernameTakenError):
                connection.rollback()
                raise
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                message = str(exc).lower()
                if "username_normalized" in message or "users.username" in message:
                    raise UsernameTakenError("用户名已被使用。") from exc
                raise AuthStorageError(_STORAGE_MESSAGE) from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise AuthStorageError(_STORAGE_MESSAGE) from exc

        user = self.find_user_by_id(new_id)
        if user is None:
            raise AuthStorageError(_STORAGE_MESSAGE)
        return user

    # ------------------------------------------------------------------
    # 邀请码
    # ------------------------------------------------------------------
    def create_invite(
        self,
        *,
        label: str | None = None,
        expires_in_days: int | None = None,
        created_by: str | None = None,
    ) -> tuple[str, str]:
        raw = secrets.token_urlsafe(24)
        invite_id = str(uuid.uuid4())
        now = _now()
        expires_at = None
        if expires_in_days is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=expires_in_days)
            ).isoformat()
        with self._open() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO invite_codes (
                        id, code_sha256, label, created_by, created_at,
                        expires_at, used_at, used_by
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        invite_id,
                        _hash_secret(raw),
                        label,
                        created_by,
                        now,
                        expires_at,
                    ),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise AuthStorageError(_STORAGE_MESSAGE) from exc
        return invite_id, raw

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    def create_session(self, user_id: str, *, ttl_days: int) -> IssuedSession:
        raw = secrets.token_urlsafe(32)
        now_dt = datetime.now(timezone.utc)
        expires_at = (now_dt + timedelta(days=ttl_days)).isoformat()
        with self._open() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO login_sessions (
                        id, user_id, token_sha256, created_at, expires_at,
                        revoked_at
                    ) VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        str(uuid.uuid4()),
                        user_id,
                        _hash_secret(raw),
                        now_dt.isoformat(),
                        expires_at,
                    ),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise AuthStorageError(_STORAGE_MESSAGE) from exc
        return IssuedSession(token=raw, expires_at=expires_at)

    def resolve_session(self, token: str) -> User | None:
        digest = _hash_secret(token)
        now = _now()
        with self._open() as connection:
            row = connection.execute(
                """
                SELECT u.id, u.username, u.created_at, u.disabled_at
                FROM login_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_sha256 = ?
                  AND s.revoked_at IS NULL
                  AND s.expires_at > ?
                  AND u.disabled_at IS NULL
                """,
                (digest, now),
            ).fetchone()
        return None if row is None else self._row_to_user(row)

    def revoke_session(self, token: str) -> bool:
        digest = _hash_secret(token)
        now = _now()
        with self._open() as connection:
            try:
                cursor = connection.execute(
                    "UPDATE login_sessions SET revoked_at = ? "
                    "WHERE token_sha256 = ? AND revoked_at IS NULL",
                    (now, digest),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise AuthStorageError(_STORAGE_MESSAGE) from exc
        return cursor.rowcount > 0

    def disable_user(self, username: str) -> User | None:
        user = self.find_user_by_username(username)
        if user is None:
            return None
        now = _now()
        with self._open() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE users SET disabled_at = ? WHERE id = ?",
                    (now, user.id),
                )
                connection.execute(
                    "UPDATE login_sessions SET revoked_at = ? "
                    "WHERE user_id = ? AND revoked_at IS NULL",
                    (now, user.id),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise AuthStorageError(_STORAGE_MESSAGE) from exc
        return self.find_user_by_id(user.id)

    def prune_sessions(self, *, keep_days: int = 30) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=keep_days)
        ).isoformat()
        with self._open() as connection:
            try:
                cursor = connection.execute(
                    "DELETE FROM login_sessions WHERE expires_at < ?", (cutoff,)
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise AuthStorageError(_STORAGE_MESSAGE) from exc
        return int(cursor.rowcount)

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            id=str(row["id"]),
            username=str(row["username"]),
            created_at=str(row["created_at"]),
            disabled_at=(
                str(row["disabled_at"]) if row["disabled_at"] is not None else None
            ),
        )