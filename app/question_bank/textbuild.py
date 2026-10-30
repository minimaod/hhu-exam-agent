"""检索文本构造与长题分片。

只使用题面侧内容：题干、选项、题内代码、公式转写、题图说明/转写、示例和
已验证知识点。原答案、参考代码、旁注、教师解析一律不进入检索片段。

分片按真实 tokenizer 计数（含特殊 token），代码只在行边界切；切完必须能
逐字符拼回原文，不允许靠模型右截断掩盖超长。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from app.question_bank.config import CONTENT_BUDGET


class FragmentTooLongError(ValueError):
    """单行本身超过 token 预算，构建必须失败而不是静默截断。"""

    def __init__(self, line_no: int, line: str) -> None:
        self.line_no = line_no
        self.line = line
        super().__init__(f"第 {line_no + 1} 行超过 {CONTENT_BUDGET} token，需要人工处理")


@dataclass(frozen=True)
class Fragment:
    ordinal: int
    text: str
    token_count: int

    @property
    def fragment_id(self) -> str:
        return f"f{self.ordinal:02d}"


def _section(title: str, body: str) -> str:
    body = body.strip()
    if not body:
        return ""
    return f"{title}：{body}"


def build_retrieval_text(
    record: dict,
    verified_labels: Iterable[str] = (),
) -> str:
    """把一条题目记录拼成检索文本；调用方负责确认标签确实是 verified。"""

    parts: list[str] = []
    statement = str(record.get("statement") or "").strip()
    if statement:
        parts.append(statement)

    options = record.get("options") or []
    if options:
        lines = [f"{item.get('label', '')}. {str(item.get('text') or '').strip()}" for item in options]
        parts.append(_section("选项", "\n".join(lines)))

    code_lines = record.get("code_lines") or []
    if code_lines:
        parts.append(_section("题内代码", "\n".join(str(line) for line in code_lines)))

    formulas = record.get("formulas") or []
    if formulas:
        text = "\n".join(str(item.get("text") or "").strip() for item in formulas if item.get("text"))
        parts.append(_section("公式", text))

    assets = record.get("assets") or []
    if assets:
        blocks = []
        for item in assets:
            description = str(item.get("description") or "").strip()
            transcription = str(item.get("transcription") or "").strip()
            block = "\n".join(part for part in (description, transcription) if part)
            if block:
                blocks.append(block)
        parts.append(_section("图示", "\n".join(blocks)))

    examples = record.get("code_examples") or []
    if examples:
        text = "\n".join(str(item.get("text") or "").strip() for item in examples if item.get("text"))
        parts.append(_section("示例", text))

    labels = [str(item).strip() for item in verified_labels if str(item).strip()]
    if labels:
        parts.append(_section("知识点", "、".join(dict.fromkeys(labels))))

    return "\n\n".join(part for part in parts if part).strip()


def fragment_text(
    text: str,
    token_count: Callable[[str], int],
    budget: int = CONTENT_BUDGET,
) -> list[str]:
    """按行边界贪心切分，输出片段按顺序拼接后与输入完全一致。"""

    if not text:
        return []
    if token_count(text) <= budget:
        return [text]

    lines = text.splitlines(keepends=True)
    fragments: list[str] = []
    current = ""
    for index, line in enumerate(lines):
        if token_count(line) > budget:
            raise FragmentTooLongError(index, line)
        candidate = current + line
        if current and token_count(candidate) > budget:
            fragments.append(current)
            current = line
        else:
            current = candidate
    if current:
        fragments.append(current)
    if "".join(fragments) != text:  # pragma: no cover - 防御
        raise ValueError("分片拼接后与原文不一致")
    return fragments


def make_fragments(text: str, token_count: Callable[[str], int]) -> list[Fragment]:
    return [
        Fragment(ordinal=index, text=piece, token_count=token_count(piece))
        for index, piece in enumerate(fragment_text(text, token_count), start=1)
    ]


def assert_fragment_budget(
    fragments: Sequence[Fragment],
    budget: int = CONTENT_BUDGET,
) -> None:
    for fragment in fragments:
        if fragment.token_count > budget:
            raise ValueError(f"{fragment.fragment_id} 超过 token 预算：{fragment.token_count}")