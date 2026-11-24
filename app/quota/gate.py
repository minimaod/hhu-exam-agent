"""模型与图片用量的 SQLite 记账门禁。

所有“检查 + 追加”都在一个 ``BEGIN IMMEDIATE`` 事务中完成；用量记录只追加，
不更新、不删除。日期按北京时间自然日计算。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.quota.errors import (
    ImageQuotaExceeded,
    ModelQuotaExceeded,
    QuotaStorageError,
)

BEIJING = timezone(timedelta(hours=8))
CONNECT_TIMEOUT_SECONDS = 5.0


def beijing_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(BEIJING)


def beijing_day(now: datetime | None = None) -> date:
    return beijing_now(now).date()


def next_reset_at(now: datetime | None = None) -> datetime:
    today = beijing_day(now)
    return datetime.combine(today + timedelta(days=1), time.min, tzinfo=BEIJING)


@dataclass(frozen=True)
class UsageLimits:
    model_requests: int
    site_requests: int
    images: int


class UsageGate:
    """按登录账号检查并记录用量。"""

    def __init__(
        self,
        db_path: Path,
        *,
        limits: UsageLimits,
        now_fn: Callable[[], datetime] | None = None,
        unlimited_owner_id: str | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._limits = limits
        self._unlimited_owner_id = unlimited_owner_id
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    def _open(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self._db_path, timeout=CONNECT_TIMEOUT_SECONDS
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
        except (sqlite3.Error, OSError) as exc:
            raise QuotaStorageError() from exc
        return connection

    def _user_limit(
        self, connection: sqlite3.Connection, owner_id: str, day: str
    ) -> int:
        row = connection.execute(
            "SELECT daily_requests, expires_on FROM model_quota_overrides "
            "WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()
        if row is not None and row["daily_requests"] is not None:
            expires = row["expires_on"]
            if expires is None or str(expires) >= day:
                return int(row["daily_requests"])
        return self._limits.model_requests

    def _user_image_limit(
        self, connection: sqlite3.Connection, owner_id: str, day: str
    ) -> int:
        row = connection.execute(
            "SELECT daily_images, expires_on FROM model_quota_overrides "
            "WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()
        if row is not None and row["daily_images"] is not None:
            expires = row["expires_on"]
            if expires is None or str(expires) >= day:
                return int(row["daily_images"])
        return self._limits.images

    # ------------------------------------------------------------------
    # 模型请求
    # ------------------------------------------------------------------
    def ensure_available(self, owner_id: str) -> None:
        """只读预检；真正的封顶仍由 acquire_model_request 的事务决定。"""

        now = self._now()
        connection = self._open()
        try:
            self._check_model_limits(connection, owner_id, beijing_day(now).isoformat(), now)
        except sqlite3.Error as exc:
            raise QuotaStorageError() from exc
        finally:
            connection.close()

    def _check_model_limits(
        self, connection: sqlite3.Connection, owner_id: str, day: str, now: datetime
    ) -> None:
        if owner_id == self._unlimited_owner_id:
            return
        user_limit = self._user_limit(connection, owner_id, day)
        user_used = self._count(
            connection, "owner_id = ? AND usage_date = ?", (owner_id, day)
        )
        if user_used >= user_limit:
            raise ModelQuotaExceeded("user", user_limit, user_used, next_reset_at(now))
        site_limit = self._limits.site_requests
        site_used = self._site_count(connection, day)
        if site_used >= site_limit:
            raise ModelQuotaExceeded("site", site_limit, site_used, next_reset_at(now))

    def _site_count(self, connection: sqlite3.Connection, day: str) -> int:
        owner_id = self._unlimited_owner_id
        return self._count(
            connection, "usage_date = ? AND (? IS NULL OR owner_id <> ?)", (day, owner_id, owner_id)
        )

    @staticmethod
    def _count(
        connection: sqlite3.Connection, where: str, params: tuple[object, ...]
    ) -> int:
        row = connection.execute(
            "SELECT COUNT(*) FROM model_usage_events "
            f"WHERE kind = 'model_request' AND {where}",
            params,
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def acquire_model_request(
        self,
        owner_id: str,
        *,
        conversation_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """发送前记账。事务失败时停止发送，不返回“放行”。"""

        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # 等锁期间可能已经跨过北京时间零点；在取得写锁后重新读取时间，
            # 避免用前一天已满的额度拒绝新一天的第一条请求。
            now = self._now()
            day = beijing_day(now).isoformat()
            self._check_model_limits(connection, owner_id, day, now)
            connection.execute(
                """
                INSERT INTO model_usage_events (
                    owner_id, usage_date, created_at, kind,
                    conversation_id, request_id
                ) VALUES (?, ?, ?, 'model_request', ?, ?)
                """,
                (
                    owner_id,
                    day,
                    now.astimezone(timezone.utc).isoformat(),
                    conversation_id,
                    request_id,
                ),
            )
            connection.commit()
        except (ModelQuotaExceeded, ImageQuotaExceeded):
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise QuotaStorageError() from exc
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # 图片
    # ------------------------------------------------------------------
    def check_and_insert_image(
        self, connection: sqlite3.Connection, owner_id: str
    ) -> None:
        """在调用方已经开启的写事务内检查并追加一张图片用量。"""

        now = self._now()
        day = beijing_day(now).isoformat()
        if owner_id != self._unlimited_owner_id:
            limit = self._user_image_limit(connection, owner_id, day)
            row = connection.execute(
                "SELECT COUNT(*) FROM model_usage_events "
                "WHERE kind = 'image_upload' AND owner_id = ? AND usage_date = ?",
                (owner_id, day),
            ).fetchone()
            used = int(row[0]) if row is not None else 0
            if used >= limit:
                raise ImageQuotaExceeded(limit, used, next_reset_at(now))
        connection.execute(
            """
            INSERT INTO model_usage_events (
                owner_id, usage_date, created_at, kind
            ) VALUES (?, ?, ?, 'image_upload')
            """,
            (owner_id, day, now.astimezone(timezone.utc).isoformat()),
        )

    # ------------------------------------------------------------------
    # 查询与运营
    # ------------------------------------------------------------------
    def snapshot(self, owner_id: str) -> dict[str, object]:
        now = self._now()
        day = beijing_day(now).isoformat()
        connection = self._open()
        try:
            unlimited = owner_id == self._unlimited_owner_id
            user_limit = None if unlimited else self._user_limit(connection, owner_id, day)
            image_limit = None if unlimited else self._user_image_limit(connection, owner_id, day)
            user_used = self._count(
                connection, "owner_id = ? AND usage_date = ?", (owner_id, day)
            )
            site_exhausted = not unlimited and self._site_count(connection, day) >= self._limits.site_requests
            image_row = connection.execute(
                "SELECT COUNT(*) FROM model_usage_events "
                "WHERE kind = 'image_upload' AND owner_id = ? AND usage_date = ?",
                (owner_id, day),
            ).fetchone()
            image_used = int(image_row[0]) if image_row is not None else 0
            active = connection.execute(
                "SELECT 1 FROM user_active_turns WHERE owner_id = ? LIMIT 1",
                (owner_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise QuotaStorageError() from exc
        finally:
            connection.close()
        return {
            "date": day,
            "reset_at": next_reset_at(now).isoformat(),
            "requests": {
                "limit": user_limit,
                "used": user_used,
                "remaining": None if user_limit is None else max(0, user_limit - user_used),
            },
            "site_exhausted": site_exhausted,
            "images": {
                "limit": image_limit,
                "used": image_used,
                "remaining": None if image_limit is None else max(0, image_limit - image_used),
            },
            "active_turn": active is not None,
        }

    def report(self, day: str | None = None) -> dict[str, object]:
        now = self._now()
        target = day or beijing_day(now).isoformat()
        connection = self._open()
        try:
            rows = connection.execute(
                """
                SELECT owner_id, kind, COUNT(*) AS used
                FROM model_usage_events
                WHERE usage_date = ?
                GROUP BY owner_id, kind
                ORDER BY owner_id, kind
                """,
                (target,),
            ).fetchall()
            overrides = connection.execute(
                "SELECT owner_id, daily_requests, daily_images, expires_on "
                "FROM model_quota_overrides WHERE expires_on IS NULL OR expires_on >= ?",
                (target,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise QuotaStorageError() from exc
        finally:
            connection.close()
        users: dict[str, dict[str, int]] = {}
        for row in rows:
            bucket = users.setdefault(
                str(row["owner_id"]), {"model_request": 0, "image_upload": 0}
            )
            bucket[str(row["kind"])] = int(row["used"])
        total_models = sum(item["model_request"] for item in users.values())
        unlimited_models = users.get(self._unlimited_owner_id or "", {}).get("model_request", 0)
        site_models = total_models - unlimited_models
        site_images = sum(item["image_upload"] for item in users.values())
        return {
            "date": target,
            "reset_at": next_reset_at(now).isoformat(),
            "site": {
                "model_requests": {
                    "used": site_models,
                    "limit": self._limits.site_requests,
                    "remaining": max(0, self._limits.site_requests - site_models),
                },
                "unlimited_model_requests": unlimited_models,
                "images": site_images,
            },
            "users": [
                {
                    "owner_id": owner_id,
                    "model_requests": item["model_request"],
                    "images": item["image_upload"],
                    "limit": None if owner_id == self._unlimited_owner_id else self._limits.model_requests,
                    "image_limit": None if owner_id == self._unlimited_owner_id else self._limits.images,
                }
                for owner_id, item in sorted(users.items())
            ],
            "overrides": [dict(row) for row in overrides],
        }

    def set_override(
        self,
        owner_id: str,
        *,
        daily_requests: int | None,
        daily_images: int | None,
        expires_on: str,
    ) -> None:
        now = self._now().astimezone(timezone.utc).isoformat()
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO model_quota_overrides (
                    owner_id, daily_requests, daily_images, expires_on, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    daily_requests = excluded.daily_requests,
                    daily_images = excluded.daily_images,
                    expires_on = excluded.expires_on,
                    updated_at = excluded.updated_at
                """,
                (owner_id, daily_requests, daily_images, expires_on, now),
            )
            connection.commit()
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise QuotaStorageError() from exc
        finally:
            connection.close()

    def clear_override(self, owner_id: str) -> None:
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM model_quota_overrides WHERE owner_id = ?",
                (owner_id,),
            )
            connection.commit()
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise QuotaStorageError() from exc
        finally:
            connection.close()