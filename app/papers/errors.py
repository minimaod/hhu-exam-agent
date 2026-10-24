"""试卷领域的业务异常。

这些异常只表达“业务上发生了什么”，不携带 HTTP 状态码，也不携带磁盘路径或
原始异常文本。由 app/main.py 中的异常处理器统一映射为 503 / 404，
这样仓库层和接口层都不需要互相知道对方的细节。
"""


class CatalogUnavailableError(Exception):
    """目录整体不可用：文件缺失、不可读、JSON 非法、字段不合法或 id 重复。

    对应 HTTP 503，不能伪装成“查询结果为空”。
    """


class PaperUnavailableError(Exception):
    """单份试卷不可下载：ID 未登记、文件缺失、路径越界或不是 PDF。

    对应 HTTP 404。
    """
