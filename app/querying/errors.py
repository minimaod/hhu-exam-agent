"""自然语言查询的业务异常。

只携带对外错误码和可展示文案，不携带上游原始响应、请求头或磁盘路径。
"""

from app.querying.models import ErrorCode


class QueryError(Exception):
    """查询流程中可以明确归类的一次失败。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
