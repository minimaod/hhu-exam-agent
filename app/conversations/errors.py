"""本地会话、辅导与错题记录领域的业务异常。

只表达“发生了什么”，不携带 HTTP 状态码、磁盘路径或原始 sqlite3 异常文本；
由 app/main.py 统一映射为 404 / 409 / 413 / 415 / 422 / 503。
"""


class ConversationNotFoundError(Exception):
    """会话 ID 未登记；对应 404。"""


class ConversationConflictError(Exception):
    """同一 request_id 内容冲突，或会话已有运行中的轮次；对应 409。"""


class ConversationStorageError(Exception):
    """本地 SQLite 不可用、损坏或结构版本不兼容；对应 503。"""


class ConversationQueryUnavailableError(Exception):
    """查询配置缺失，无法执行新的查询轮次；对应 503。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TurnInvalidError(Exception):
    """轮次的文字、字段组合或 kind 不合法；对应 422。"""


class ProblemNotFoundError(Exception):
    """当前题目或指定题目不存在；对应 404。"""


class ProblemNotReadyError(Exception):
    """题目尚未讲解，不能追问、记录或反馈；对应 409。"""


class ActiveProblemConflictError(Exception):
    """会话已有活动题目，开始新题需要明确确认；对应 409。"""


class AttachmentNotFoundError(Exception):
    """附件不存在或不属于当前会话；对应 404。"""


class AttachmentInvalidError(Exception):
    """附件角色、数量、归属或来源不合法；对应 422。"""


class AttachmentTooLargeError(Exception):
    """图片或本轮附件总量超过限制；对应 413。"""


class UnsupportedImageError(Exception):
    """不是允许的图片格式；对应 415。"""


class InvalidImageError(Exception):
    """图片内容损坏、无法解码或像素超限；对应 422。"""


class AttachmentStorageError(Exception):
    """附件目录不可用或写入失败；对应 503。"""


class TutoringUnavailableError(Exception):
    """辅导所需模型配置缺失；对应 503。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SourceInvalidError(Exception):
    """来源字段不合法或 paper_id 未登记；对应 422。"""


class MistakeRecordNotFoundError(Exception):
    """错题记录不存在；对应 404。"""


class MistakeRecordConflictError(Exception):
    """记录重复、旧版本冲突或正在纠正；对应 409。"""

    def __init__(self, message: str, *, record_id: str | None = None) -> None:
        super().__init__(message)
        self.record_id = record_id
