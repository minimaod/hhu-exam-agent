"""用量限制的领域异常。

对外只暴露可读文案和额度信息，不携带 sqlite 原始异常、密钥或请求头。
"""

from __future__ import annotations

from datetime import datetime

from app.querying.errors import QueryError

QUOTA_STORAGE_MESSAGE = "暂时无法核对用量，请稍后重试。"


class QuotaError(QueryError):
    """额度相关错误；供上层统一识别，避免被普通模型错误吞掉。"""


class ModelQuotaExceeded(QuotaError):
    """个人或全站模型请求额度已满。"""

    def __init__(
        self,
        scope: str,
        limit: int,
        used: int,
        reset_at: datetime,
    ) -> None:
        self.scope = scope
        self.limit = limit
        self.used = used
        self.reset_at = reset_at
        label = "你的" if scope == "user" else "全站的"
        message = (
            f"{label}今日模型请求额度已用完（{used}/{limit}），"
            f"北京时间 {reset_at:%m月%d日 %H:%M} 重置。"
        )
        super().__init__("MODEL_QUOTA_EXCEEDED", message)

    def quota(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "limit": self.limit,
            "used": self.used,
            "reset_at": self.reset_at.isoformat(),
        }


class ImageQuotaExceeded(QuotaError):
    """个人当日图片上传额度已满。"""

    def __init__(self, limit: int, used: int, reset_at: datetime) -> None:
        self.scope = "image"
        self.limit = limit
        self.used = used
        self.reset_at = reset_at
        message = (
            f"今日已上传 {used}/{limit} 张图片，"
            f"北京时间 {reset_at:%m月%d日 %H:%M} 重置。"
        )
        super().__init__("IMAGE_QUOTA_EXCEEDED", message)

    def quota(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "limit": self.limit,
            "used": self.used,
            "reset_at": self.reset_at.isoformat(),
        }


class QuotaStorageError(QuotaError):
    """用量库不可用；必须停止发送，不能放行。"""

    def __init__(self) -> None:
        super().__init__("QUOTA_STORAGE_UNAVAILABLE", QUOTA_STORAGE_MESSAGE)


class TurnInProgressError(Exception):
    """同一账号已有一个正在执行的轮次或回复重生成。"""


def quota_of(exc: BaseException) -> dict[str, object] | None:
    """取出业务异常携带的公开额度信息。"""

    method = getattr(exc, "quota", None)
    if callable(method):
        value = method()
        return value if isinstance(value, dict) else None
    return None