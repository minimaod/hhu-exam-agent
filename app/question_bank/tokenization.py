"""中文 + 代码词元的分词。

建索引和查询必须走同一个函数，否则 BM25 的统计口径会漂移。jieba 只在
第一次调用时加载；停用词表只含自然语言虚词，Python 关键字（for/if/while
/return 等）必须保留。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from app.question_bank.config import DEFAULT_BANK_ROOT

_WORD = re.compile(r"^[\w\u3400-\u4dbf\u4e00-\u9fff]+$")
_OPERATOR = re.compile(r"==|!=|<=|>=|//|\*\*|\+=|-=|\*=|/=|%")
_JIEBA = None


def _jieba():
    global _JIEBA
    if _JIEBA is None:
        import jieba  # 延迟导入：没有 qb 依赖时仍可导入本模块

        jieba.setLogLevel(20)
        _JIEBA = jieba
    return _JIEBA


def load_stopwords(path: Path | None = None) -> frozenset[str]:
    target = path or (DEFAULT_BANK_ROOT / "taxonomy" / "stopwords-zh.txt")
    if not target.exists():
        return frozenset()
    words: set[str] = set()
    for line in target.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            words.add(value.lower())
    return frozenset(words)


def tokenize(text: str, stopwords: Iterable[str] | None = None) -> list[str]:
    """返回保留重复（供 BM25 统计词频）的小写词元序列。"""

    if not text or not text.strip():
        return []
    stop = set(stopwords) if stopwords is not None else set()
    tokens: list[str] = []
    for raw in _jieba().cut_for_search(text):
        value = raw.strip().lower()
        if not value or value in stop:
            continue
        if not _WORD.match(value):
            continue
        tokens.append(value)
    for match in _OPERATOR.finditer(text):
        tokens.append(match.group(0))
    return tokens


def query_terms(text: str, stopwords: Iterable[str] | None = None) -> list[str]:
    """查询侧去重并保序，避免重复词抬高分数。"""

    seen: set[str] = set()
    result: list[str] = []
    for token in tokenize(text, stopwords):
        if token not in seen:
            seen.add(token)
            result.append(token)
    return result