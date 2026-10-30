"""受控知识点词表、别名归一与规则候选标签。

规则只产出 `candidate`：每条标签都带命中依据（字段 + 词），供复核工作表
展示。只有导师在 reviews.json 里确认过的标签才会变成 `verified` 并进入
检索文本。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


class TaxonomyError(ValueError):
    """词表缺失、重复或别名冲突。"""


@dataclass(frozen=True)
class KnowledgePoint:
    slug: str
    name: str
    definition: str
    aliases: tuple[str, ...]
    patterns: tuple[str, ...]
    priority: int


@dataclass(frozen=True)
class LabelMatch:
    slug: str
    basis: str
    rule_id: str


def _key(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


class Taxonomy:
    def __init__(self, points: Iterable[KnowledgePoint]) -> None:
        ordered = sorted(points, key=lambda item: (-item.priority, item.slug))
        if not ordered:
            raise TaxonomyError("知识点词表为空")
        self.points = tuple(ordered)
        self.by_slug = {item.slug: item for item in self.points}
        alias_map: dict[str, str] = {}
        for point in self.points:
            for raw in (point.slug, point.name, *point.aliases):
                key = _key(raw)
                if not key:
                    continue
                existing = alias_map.get(key)
                if existing and existing != point.slug:
                    raise TaxonomyError(f"别名 {raw!r} 同时指向 {existing} 与 {point.slug}")
                alias_map[key] = point.slug
        self.alias_map = alias_map

    def normalize(self, value: str) -> str | None:
        return self.alias_map.get(_key(value))

    def name(self, slug: str) -> str:
        return self.by_slug[slug].name

    def label(self, record: dict[str, Any], limit: int = 3) -> list[LabelMatch]:
        fields = {
            "题干": str(record.get("statement") or ""),
            "选项": "\n".join(str(item.get("text") or "") for item in record.get("options") or []),
            "题内代码": "\n".join(str(line) for line in record.get("code_lines") or []),
        }
        matches: list[LabelMatch] = []
        seen: set[str] = set()
        for point in self.points:
            for pattern in point.patterns:
                hit_field = None
                hit_text = ""
                for field, text in fields.items():
                    if not text:
                        continue
                    found = re.search(pattern, text, flags=re.IGNORECASE)
                    if found:
                        hit_field = field
                        hit_text = found.group(0)
                        break
                if hit_field is None:
                    continue
                if point.slug in seen:
                    break
                seen.add(point.slug)
                basis = f"{hit_field}命中「{hit_text[:30]}」"
                matches.append(
                    LabelMatch(slug=point.slug, basis=basis, rule_id=f"rule:{point.slug}")
                )
                break
            if len(matches) >= limit:
                break
        return matches


def load_taxonomy(path: Path) -> Taxonomy:
    if not path.exists():
        raise TaxonomyError(f"知识点词表不存在：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TaxonomyError(f"知识点词表无法读取：{path}") from exc
    rows = payload.get("knowledge_points") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise TaxonomyError("知识点词表缺少 knowledge_points 数组")
    points: list[KnowledgePoint] = []
    slugs: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TaxonomyError("知识点条目必须是对象")
        slug = str(row.get("slug") or "").strip()
        name = str(row.get("name") or "").strip()
        if not slug or not name:
            raise TaxonomyError("知识点缺少 slug 或 name")
        if slug in slugs:
            raise TaxonomyError(f"知识点 slug 重复：{slug}")
        slugs.add(slug)
        aliases = tuple(str(item).strip() for item in row.get("aliases") or [] if str(item).strip())
        patterns = tuple(str(item) for item in row.get("patterns") or [] if str(item).strip())
        for pattern in patterns:
            re.compile(pattern)
        points.append(
            KnowledgePoint(
                slug=slug,
                name=name,
                definition=str(row.get("definition") or ""),
                aliases=aliases,
                patterns=patterns,
                priority=int(row.get("priority") or 0),
            )
        )
    return Taxonomy(points)