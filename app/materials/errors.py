"""个人资料领域的业务异常。"""


class MaterialError(Exception):
    """个人资料领域基类异常。"""


class MaterialNotFoundError(MaterialError):
    """资料或物理页不存在、或不属于当前账号（404）。"""


class GuideNotFoundError(MaterialError):
    """复习清单或清单条目不存在、或不属于当前账号（404）。"""


class MaterialTooLargeError(MaterialError):
    """文件大小超过限制（413）。"""


class MaterialInvalidError(MaterialError):
    """非 PDF 文件、损坏或不可解析（422）。"""


class MaterialPageLimitError(MaterialError):
    """页数或选定范围超出限制（422）。"""


class GuideInsufficientError(MaterialError):
    """选定范围文本不足（如纯扫描/乱码），提示缩小范围或换文件（422）。"""


class MaterialConflictError(MaterialError):
    """删除中或有活跃任务冲突（409）。"""


class MaterialCoverageRequiredError(MaterialConflictError):
    """The default generation path requires usable input for each selected study page."""

    code = "MATERIAL_COVERAGE_REQUIRED"

    def __init__(self, missing_pages: list[int], *, can_generate_partial: bool) -> None:
        self.missing_pages = missing_pages
        self.can_generate_partial = can_generate_partial
        self.message = f"还有 {len(missing_pages)} 页待识别或重试。可在学习会话点「识别并生成」，补齐所选范围。"
        super().__init__(self.message)

    def detail(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "missing_pages": self.missing_pages,
            "can_generate_partial": self.can_generate_partial,
        }


class MaterialParseTimeoutError(MaterialError):
    """PDF 隔离解析超时（504）。"""


class MaterialStorageError(MaterialError):
    """存储或数据库不可用（503）。"""
