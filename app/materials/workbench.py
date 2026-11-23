"""Index the stored review outputs by the knowledge skeleton's stable IDs."""

from collections import Counter, defaultdict
from difflib import SequenceMatcher
from html import unescape
import re
import unicodedata

from app.materials.exampass_adapter import parse_source_pages
from app.materials.models import KnowledgePointIndex, QuizQuestionPublicView

_HEADING = re.compile(r"<h3\b(?P<attrs>[^>]*)>(?P<title>.*?)</h3>", re.S)
_KC_ATTR = re.compile(r'\sdata-kc="([^"]+)"')
_PAGE_ATTR = re.compile(r'\bdata-slides="([^"]+)"')
_MARKUP = re.compile(r"<[^>]+>")
_TITLE_BADGES = re.compile(r'<span class="[^"]*\btag\b[^"]*"[^>]*>.*?</span>', re.S)
NOTE_WARNING = "笔记锚点："


def _label(value: str, *, strip_number: bool = False) -> str:
    value = unicodedata.normalize("NFKC", unescape(_MARKUP.sub("", value))).casefold()
    if strip_number:
        value = re.sub(r"^\s*\d+(?:\.\d+)*(?:[.、:：]\s*|\s+)", "", value)
    return "".join(ch for ch in value if ch.isalnum())


def _points(skeleton: dict | None):
    for chapter in (skeleton or {}).get("chapters") or []:
        for point in chapter.get("kcs") or []:
            kc_id = point.get("id")
            if isinstance(kc_id, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,56}", kc_id):
                yield chapter, point, kc_id


def ensure_note_anchors(notes: str, skeleton: dict | None) -> tuple[str, list[str]]:
    points = {kc_id: point for _, point, kc_id in _points(skeleton)}
    if not points:
        return notes, []
    labels = {kc_id: str(point.get("label") or kc_id) for kc_id, point in points.items()}
    names: dict[str, list[str]] = defaultdict(list)
    for kc_id, label in labels.items():
        names[_label(label)].append(kc_id)
    used: set[str] = set()
    reserved = {
        attr[1] for heading in _HEADING.finditer(notes)
        if (attr := _KC_ATTR.search(heading["attrs"])) and attr[1] in points
    }
    unmatched: list[str] = []

    def repair(heading: re.Match[str]) -> str:
        attrs = heading["attrs"]
        title = heading["title"]
        existing = _KC_ATTR.search(attrs)
        if existing and existing[1] in reserved and existing[1] not in used:
            used.add(existing[1])
            return heading[0]
        plain = unescape(_MARKUP.sub("", _TITLE_BADGES.sub("", title))).strip()
        matches = names.get(_label(plain)) or names.get(_label(plain, strip_number=True), [])
        if len(matches) > 1:
            pages = set(parse_source_pages(_PAGE_ATTR.findall(heading[0]), 10000))
            matches = [
                kc_id for kc_id in matches
                if pages.intersection(parse_source_pages(points[kc_id].get("source_refs"), 10000))
            ]
        if len(matches) == 1 and matches[0] not in reserved | used:
            kc_id = matches[0]
            used.add(kc_id)
            return f'<h3{_KC_ATTR.sub("", attrs)} data-kc="{kc_id}">{title}</h3>'
        unmatched.append(plain[:100] or "无标题")
        return f'<h3{_KC_ATTR.sub("", attrs)}>{title}</h3>'

    repaired = _HEADING.sub(repair, notes)
    warnings = [NOTE_WARNING + "未匹配小节「" + title + "」" for title in unmatched]
    warnings.extend(NOTE_WARNING + "缺少「" + label + "」的小节" for kc_id, label in labels.items() if kc_id not in used)
    return repaired, list(dict.fromkeys(warnings))


def graph_nodes(tree: dict | None):
    stack = list(reversed((tree or {}).get("nodes") or []))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.get("children") or []))


def build_kc_index(
    skeleton: dict | None,
    notes: str,
    tree: dict | None,
    questions: list[QuizQuestionPublicView],
    start: int,
    end: int,
) -> tuple[dict[str, KnowledgePointIndex], list[str]]:
    index: dict[str, KnowledgePointIndex] = {}
    warnings: list[str] = []
    for chapter, point, kc_id in _points(skeleton):
        if kc_id in index:
            warnings.append(f"知识点索引：骨架重复使用 {kc_id}")
            continue
        index[kc_id] = KnowledgePointIndex(
            label=str(point.get("label") or kc_id),
            importance=str(point.get("importance") or ""),
            module=str(chapter.get("label") or ""),
            pages=[page for page in parse_source_pages(point.get("source_refs"), end) if page >= start],
            deps=list(dict.fromkeys(str(dep) for dep in (point.get("deps") or []) if str(dep) != kc_id)),
        )

    graph_counts: Counter[str] = Counter()
    for node in graph_nodes(tree):
        kc_id = node.get("kc_id")
        node_id = node.get("id")
        if not isinstance(kc_id, str) or not kc_id or not node_id:
            continue
        point = index.get(kc_id)
        if point is None and skeleton:
            warnings.append(f"图谱覆盖：节点 {node_id} 引用了未知知识点 {kc_id}")
            continue
        if point is None:
            point = index[kc_id] = KnowledgePointIndex(
                label=str(node.get("label") or kc_id),
                deps=list(dict.fromkeys(str(dep) for dep in (node.get("deps") or []) if str(dep) != kc_id)),
                pages=[page for page in parse_source_pages(node.get("source_refs"), end) if page >= start],
            )
        graph_counts[kc_id] += 1
        if point.graph_node_id is None:
            point.graph_node_id = str(node_id)
    if tree:
        for kc_id, point in index.items():
            if graph_counts[kc_id] != 1:
                warnings.append(f"图谱覆盖：知识点「{point.label}」有 {graph_counts[kc_id]} 个节点")
    roots = [(str(node.get("label") or ""), _label(str(node.get("label") or "")))
             for node in (tree or {}).get("nodes") or []]
    for i, (label, name) in enumerate(roots):
        for other_label, other_name in roots[:i]:
            if not name or not other_name:
                continue
            similar = len(name) >= 4 and len(other_name) >= 4 and SequenceMatcher(None, name, other_name).ratio() >= .86
            if name == other_name or similar:
                warnings.append(f"图谱覆盖：根节点「{other_label}」与「{label}」名称相近")

    for heading in _HEADING.finditer(notes):
        anchor = _KC_ATTR.search(heading["attrs"])
        if anchor and (point := index.get(anchor[1])) and point.note_anchor is None:
            point.note_anchor = anchor[1]
            pages = [page for page in parse_source_pages(_PAGE_ATTR.findall(heading[0]), end) if page >= start]
            point.pages = list(dict.fromkeys([*pages, *point.pages]))
    for question in questions:
        if point := index.get(question.kc_id):
            point.question_ids.append(question.id)
    for kc_id, point in index.items():
        point.deps = [dep for dep in point.deps if dep in index]
        for dep in point.deps:
            index[dep].dependents.append(kc_id)
    return index, list(dict.fromkeys(warnings))

