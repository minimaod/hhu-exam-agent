"""题干摘要截断工具：截断时不把 LaTeX 公式从中间切开，并补省略号。"""

from __future__ import annotations

import re

# 与前端 MathText 的定界规则保持一致：$$…$$、\[…\]、\(…\)、$…$
_MATH_TOKEN = re.compile(
    r"(?<!\\)\$\$[\s\S]+?\$\$"
    r"|(?<!\\)\\\[[\s\S]+?\\\]"
    r"|(?<!\\)\\\([\s\S]+?\\\)"
    r"|(?<![\\$])\$(?!\$)(?:\\.|[^\\$\n])+?\$"
    r"|(?P<opener>(?<!\\)(?:\$\$|\\\[|\\\(|(?<!\$)\$(?!\$)))"
)


def math_safe_excerpt(text: str, limit: int, ellipsis: str = "…") -> str:
    """把 ``text`` 压缩空白后截到 ``limit`` 个字符以内（含省略号）。

    截断点若落在公式内部，则退到该公式之前；若公式位于开头导致退无可退，
    则保留整段公式（允许略超长度），保证前端总能完整渲染。
    """

    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    cut = max(limit - len(ellipsis), 1)
    for match in _MATH_TOKEN.finditer(compact):
        start, end = match.span()
        if start >= cut:
            break
        if match.group("opener"):
            cut = start
            break
        if cut < end:
            cut = start if start > 0 else end
            break
    head = compact[:cut].rstrip()
    if len(head) >= len(compact):
        return compact
    return f"{head}{ellipsis}"
