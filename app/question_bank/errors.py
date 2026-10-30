"""查题服务的业务异常。

参数错误与“题库不可用”分开：前者应把可读原因交给调用方澄清，后者要
明确告诉学生不是“没有这道题”，而是题库/索引暂时不可用。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class QuestionBankRequestError(Exception):
    """请求参数、知识点词表或试卷归属不合法。"""

    field: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"{self.field}: {self.message}"


class QuestionBankNotBuiltError(Exception):
    """当前没有可用的题目库版本。"""


class QuestionBankUnavailableError(Exception):
    """事实库、索引或模型损坏/被占用，无法提供检索。"""


class IndexIncompatibleError(QuestionBankUnavailableError):
    """索引清单与当前代码/模型身份不一致。"""


class EmbeddingUnavailableError(Exception):
    """向量模型缺失或加载失败；关键词检索仍可能可用。"""


class VectorIndexUnavailableError(Exception):
    """Qdrant 目录不可打开、被占用或损坏。"""


class Bm25IndexUnavailableError(Exception):
    """BM25 索引缺失或损坏。"""