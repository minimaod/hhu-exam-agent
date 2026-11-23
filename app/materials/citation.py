"""复习清单页码引用校验器。

只有同时满足以下条件的引用才允许返回前端：
1. page_no 为整数且在选定物理页范围 [start_page, end_page] 内；
2. quote 经过空白/全半角规范化后，确实存在于对应物理页的真实提取文本中。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from app.materials.models import VerifiedCitation

_WHITESPACE_RE = re.compile(r"\s+")
_MIN_QUOTE_LENGTH = 4


def normalize_for_match(text: str) -> str:
    nfkc = unicodedata.normalize("NFKC", text or "")
    collapsed = _WHITESPACE_RE.sub("", nfkc)
    return collapsed.lower()


def verify_citations(
    raw_citations: Iterable[dict[str, object]],
    *,
    page_texts: dict[int, str],
    start_page: int,
    end_page: int,
) -> list[VerifiedCitation]:
    """过滤并仅保留真实存在于对应物理页的引用。"""

    normalized_pages = {
        pno: normalize_for_match(txt)
        for pno, txt in page_texts.items()
        if start_page <= pno <= end_page
    }
    verified: list[VerifiedCitation] = []
    seen: set[tuple[int, str]] = set()

    for item in raw_citations:
        if not isinstance(item, dict):
            continue
        raw_page = item.get("page_no")
        if isinstance(raw_page, bool) or not isinstance(raw_page, int):
            continue
        if not (start_page <= raw_page <= end_page):
            continue
        raw_quote = str(item.get("quote") or "").strip()
        if not raw_quote:
            continue
        norm_quote = normalize_for_match(raw_quote)
        if len(norm_quote) < _MIN_QUOTE_LENGTH:
            continue
        page_norm = normalized_pages.get(raw_page, "")
        if not page_norm or norm_quote not in page_norm:
            continue
        key = (raw_page, norm_quote)
        if key in seen:
            continue
        seen.add(key)
        verified.append(
            VerifiedCitation(
                page_no=raw_page,
                quote=raw_quote[:240],
                verified=True,
            )
        )
    return verified


def derive_verified_excerpt(
    verified: list[VerifiedCitation],
    page_texts: dict[int, str],
) -> str:
    """从已核验的物理页真实文本中派生资料原文摘录；无有效引用时返回空串。"""

    if not verified:
        return ""
    first = verified[0]
    raw_page = page_texts.get(first.page_no, "")
    if not raw_page:
        return ""
    # 在真实页文本中定位引文（先尝试直接子串，再按去空白映射回原串位置）
    idx = raw_page.find(first.quote)
    if idx >= 0:
        start = max(0, idx - 30)
        end = min(len(raw_page), idx + len(first.quote) + 120)
        return raw_page[start:end].strip()
    norm_quote = normalize_for_match(first.quote)
    compact_chars: list[str] = []
    orig_indices: list[int] = []
    for pos, ch in enumerate(raw_page):
        n = normalize_for_match(ch)
        if n:
            compact_chars.append(n)
            orig_indices.append(pos)
    compact_str = "".join(compact_chars)
    pos_in_compact = compact_str.find(norm_quote)
    if pos_in_compact < 0 or not orig_indices:
        return ""
    orig_start = orig_indices[pos_in_compact]
    end_compact_idx = min(len(orig_indices) - 1, pos_in_compact + len(norm_quote) - 1)
    orig_end = min(len(raw_page), orig_indices[end_compact_idx] + 120)
    return raw_page[max(0, orig_start - 30) : orig_end].strip()
