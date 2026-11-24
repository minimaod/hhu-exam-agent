"""自愿贡献资料模块的业务异常定义。"""


class ContributionError(Exception):
    """贡献模块基类异常。"""


class ContributionInvalidError(ContributionError):
    """提交参数缺失、格式不合法或文件非有效 PDF。"""


class ContributionTooLargeError(ContributionError):
    """上传的 PDF 超出大小上限。"""


class ContributionParseTimeoutError(ContributionError):
    """PDF 校验解析超时。"""


class ContributionStorageError(ContributionError):
    """私有待审目录不可用或写入失败。"""
