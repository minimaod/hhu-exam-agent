"""类似练习题的去重规则（F006 B 批）。

只做有界、可解释的文本比较：
- 归一化大小写、全半角、空白和首尾标点；
- 完全一致视为重复；
- 长题干在“数字完全相同、只有少量措辞差异”时视为重复；
- 数字发生变化不算重复（换数值本身就是允许的变式）。

模型调用次数由服务层限制，本模块不发起网络请求。
"""

import re
import unicodedata
from difflib import SequenceMatcher

MAX_RECENT_EXERCISES = 5
MAX_GENERATION_ATTEMPTS = 2
LONG_STATEMENT = 40
SIMILARITY_THRESHOLD = 0.98

_PUNCTUATION = "。．.！!？?；;，,：:、\u3000"
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def normalize_statement(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = normalized.lower().replace(" ", "").replace("\n", "").replace("\t", "")
    return normalized.strip(_PUNCTUATION)


def _numbers(text: str) -> tuple[str, ...]:
    return tuple(_NUMBER.findall(text))


def is_duplicate_statement(
    candidate: str,
    recent: list[str],
    *,
    original: str | None = None,
) -> bool:
    """判断新题是否与近期题或原题明显重复。"""

    current = normalize_statement(candidate)
    if not current:
        return True
    if original and current == normalize_statement(original):
        return True
    for item in recent:
        previous = normalize_statement(item)
        if not previous:
            continue
        if current == previous:
            return True
        if len(current) < LONG_STATEMENT or len(previous) < LONG_STATEMENT:
            continue
        # 数字不同说明是有效的数值变式，不判重复。
        if _numbers(current) != _numbers(previous):
            continue
        if SequenceMatcher(None, current, previous).ratio() >= SIMILARITY_THRESHOLD:
            return True
    return False