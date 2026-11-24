"""ExamPass-Assistant 知识图谱、HTML 净化与客观题判分适配模块。

来源与许可说明：
- `skeleton_to_graph_tree`、`validate_tree_json`、`build_fallback_tree` 复用自
  WUBING2023/ExamPass-Assistant (commit 3812643baf5de8b92bac17fbb4e136adcc19c41b,
  `scripts/knowledge_graph.py`)。
- `tf_answer_index`、`choice_answer_index`、`multi_answer_set`、`norm_text`、
  `fill_blank_count`、`fill_blank_answers` 及客观题判分规则复用自同版本
  `templates/test_js_template.js`。
- 原项目采用 CC BY-NC 4.0 许可证（仅限个人、教育与非商业用途）。
- 本站适配修改：将前端 JS 判分迁移至服务端运行，消除答案直接嵌入前端 HTML 的
  越权问题；新增 HTML 片段白名单净化器，阻止模型输出中的脚本或事件属性执行。
"""

from __future__ import annotations

import html
import json
import math
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# HTML 片段安全白名单净化
# ---------------------------------------------------------------------------

_ALLOWED_TAGS = frozenset(
    {
        "h2",
        "h3",
        "h4",
        "p",
        "div",
        "span",
        "ul",
        "ol",
        "li",
        "blockquote",
        "details",
        "summary",
        "strong",
        "b",
        "em",
        "i",
        "code",
        "pre",
        "br",
        "hr",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "sub",
        "sup",
    }
)
_VOID_TAGS = frozenset({"br", "hr"})
_DROP_CONTENT_TAGS = frozenset({"script", "style", "iframe", "object", "embed", "svg"})
_ALLOWED_ATTRS = frozenset({"class", "data-slides", "data-kc", "open"})


class _NotesHtmlSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._parts: list[str] = []
        self._drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth > 0 or t not in _ALLOWED_TAGS:
            return
        attr_strs: list[str] = []
        for name, val in attrs:
            k = name.lower()
            if k not in _ALLOWED_ATTRS:
                continue
            if k == "data-slides" and val is not None:
                cleaned = re.sub(r"[^0-9,\-–~\s]", "", val).strip()
                if cleaned:
                    attr_strs.append(f'data-slides="{html.escape(cleaned, quote=True)}"')
            elif k == "data-kc" and t == "h3" and val and re.fullmatch(r"[a-zA-Z0-9_.-]{1,56}", val):
                attr_strs.append(f'data-kc="{val}"')
            elif k == "class" and val is not None:
                cleaned = re.sub(r"[^a-zA-Z0-9_\-\s]", "", val).strip()
                if cleaned:
                    attr_strs.append(f'class="{html.escape(cleaned, quote=True)}"')
            elif k == "open":
                attr_strs.append("open")
        attr_suffix = (" " + " ".join(attr_strs)) if attr_strs else ""
        self._parts.append(f"<{t}{attr_suffix}>")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _DROP_CONTENT_TAGS:
            if self._drop_depth > 0:
                self._drop_depth -= 1
            return
        if self._drop_depth > 0 or t not in _ALLOWED_TAGS or t in _VOID_TAGS:
            return
        self._parts.append(f"</{t}>")

    def handle_data(self, data: str) -> None:
        if self._drop_depth > 0:
            return
        self._parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if self._drop_depth > 0:
            return
        self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._drop_depth > 0:
            return
        self._parts.append(f"&#{name};")

    def get_html(self) -> str:
        return "".join(self._parts)


_NOTES_MATH = re.compile(
    r"(?<!\\)(?:\$\$[\s\S]+?(?<!\\)\$\$|\$(?!\$)[^\n]+?(?<!\\)\$"
    r"|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\])"
)


def sanitize_notes_html(raw_html: str) -> str:
    """仅保留安全排版标签与白名单属性，移除脚本与事件处理属性。"""

    if not raw_html:
        return ""
    parser = _NotesHtmlSanitizer()
    # Escape comparison operators before HTMLParser treats them as opening tags.
    safe_math = _NOTES_MATH.sub(lambda m: html.escape(html.unescape(m[0]), quote=False), raw_html)
    parser.feed(safe_math)
    parser.close()
    return parser.get_html().strip()


# ---------------------------------------------------------------------------
# 知识图谱树转换与校验（复用自 scripts/knowledge_graph.py）
# ---------------------------------------------------------------------------

_TYPE_CN = {"fact": "事实", "concept": "概念", "procedure": "过程", "principle": "原理"}
_IMPORTANCE_CN = {"must": "核心", "key": "重点", "freq": "重点", "info": "了解"}
_TYPE_GROUP_LABEL = {
    "concept": "📐 核心概念",
    "principle": "🔬 原理与推导",
    "procedure": "⚙ 算法与过程",
    "fact": "📋 关键事实",
}


def validate_tree_json(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("根必须是 JSON 对象")
    if "title" not in data:
        raise ValueError("缺少 'title' 字段")
    if "nodes" not in data or not isinstance(data["nodes"], list):
        raise ValueError("缺少或非法的 'nodes' 数组")

    ids: set[str] = set()

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                raise ValueError(f"节点必须是对象: {node}")
            for req in ("id", "label", "summary"):
                if req not in node:
                    raise ValueError(f"节点缺少 {req}: {node}")
            nid = str(node["id"])
            if nid in ids:
                raise ValueError(f"重复 id: {nid}")
            ids.add(nid)
            children = node.get("children")
            if children is not None:
                if not isinstance(children, list):
                    raise ValueError(f"节点 {nid} 的 children 必须是数组")
                walk(children)

    walk(data["nodes"])


def _next_id(counter: list[int]) -> str:
    nid = f"n{counter[0]}"
    counter[0] += 1
    return nid


def _kc_summary(kc: dict[str, Any], label_by_id: dict[str, str]) -> str:
    parts: list[str] = []
    typ = _TYPE_CN.get(str(kc.get("type", "")), str(kc.get("type", "")))
    imp = _IMPORTANCE_CN.get(str(kc.get("importance", "")), "")
    if typ:
        parts.append(typ)
    if imp:
        parts.append(imp)
    deps = [label_by_id.get(str(d), str(d)) for d in (kc.get("deps") or [])]
    if deps:
        parts.append("前置：" + "、".join(deps))
    if kc.get("is_hub"):
        parts.append("枢纽概念")
    return " · ".join(parts)


def skeleton_to_graph_tree(skeleton: dict[str, Any]) -> dict[str, Any]:
    """将 knowledge_skeleton 转为左根右叶知识图谱树结构。"""

    label_by_id: dict[str, str] = {}
    for ch in skeleton.get("chapters", []) or []:
        for kc in ch.get("kcs", []) or []:
            if kc.get("id") and kc.get("label"):
                label_by_id[str(kc["id"])] = str(kc["label"])

    counter = [1]
    nodes: list[dict[str, Any]] = []
    for ch in skeleton.get("chapters", []) or []:
        kcs = ch.get("kcs", []) or []
        groups: dict[str, list[dict[str, Any]]] = {}
        for kc in kcs:
            typ = str(kc.get("type") or "concept")
            groups.setdefault(typ, []).append(kc)

        use_groups = len(groups) >= 2 and len(kcs) >= 4
        children: list[dict[str, Any]] = []
        if use_groups:
            for typ, group_kcs in groups.items():
                group_node: dict[str, Any] = {
                    "id": _next_id(counter),
                    "label": _TYPE_GROUP_LABEL.get(typ, typ),
                    "summary": f"{len(group_kcs)} 个知识组件",
                    "children": [],
                }
                for kc in group_kcs:
                    group_node["children"].append(
                        {
                            "id": _next_id(counter),
                            "label": kc.get("label", kc.get("id")),
                            "summary": _kc_summary(kc, label_by_id),
                            "kc_id": kc.get("id"),
                            "deps": list(kc.get("deps") or []),
                            "source_refs": list(kc.get("source_refs") or []),
                            "is_hub": bool(kc.get("is_hub", False)),
                            "children": [],
                        }
                    )
                children.append(group_node)
        else:
            for kc in kcs:
                children.append(
                    {
                        "id": _next_id(counter),
                        "label": kc.get("label", kc.get("id")),
                        "summary": _kc_summary(kc, label_by_id),
                        "kc_id": kc.get("id"),
                        "deps": list(kc.get("deps") or []),
                        "source_refs": list(kc.get("source_refs") or []),
                        "is_hub": bool(kc.get("is_hub", False)),
                        "children": [],
                    }
                )

        nodes.append(
            {
                "id": _next_id(counter),
                "label": ch.get("label", ch.get("id", "章节")),
                "summary": ch.get("summary", ""),
                "children": children,
            }
        )

    tree = {"title": skeleton.get("title", "课程复习"), "nodes": nodes}
    validate_tree_json(tree)
    return tree


# ---------------------------------------------------------------------------
# 客观题服务端自动判分（复用自 templates/test_js_template.js）
# ---------------------------------------------------------------------------


def tf_answer_index(ans: Any) -> int:
    if isinstance(ans, bool):
        return 0 if ans else 1
    if isinstance(ans, (int, float)):
        return 0 if ans else 1
    s = str(ans).strip().lower()
    if s in {"t", "true", "正确", "对", "是", "yes", "y", "√", "✓"}:
        return 0
    if s in {"f", "false", "错误", "错", "否", "no", "n", "×", "x"}:
        return 1
    return 0 if ans else 1


def choice_answer_index(ans: Any, options: list[str] | None = None) -> int:
    if isinstance(ans, int) and not isinstance(ans, bool):
        return ans
    s = str(ans).strip()
    if re.fullmatch(r"[A-Za-z]", s):
        return ord(s.upper()) - 65
    if re.fullmatch(r"\d+", s):
        return int(s)
    if options:
        for idx, opt in enumerate(options):
            if str(opt).strip() == s:
                return idx
    raise ValueError(f"无法解析选择题答案：{ans!r}")


def multi_answer_set(ans: Any, options: list[str] | None = None) -> set[int]:
    arr = ans if isinstance(ans, list) else [ans]
    return {choice_answer_index(item, options) for item in arr}


def norm_text(s: Any) -> str:
    text = str(s or "").strip().lower()
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[.,;:!?。，、；：！？“”\"'（）()【】\[\]<>《》$]", "", text)
    out_chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out_chars.append(chr(code - 0xFEE0))
        else:
            out_chars.append(ch)
    return "".join(out_chars)


def fill_blank_count(question: str) -> int:
    matches = re.findall(r"(_{2,}|＿+)", str(question or ""))
    return len(matches)


def fill_blank_answers(ans: Any, n_blanks: int) -> list[list[str]]:
    if n_blanks < 1:
        n_blanks = 1
    if not isinstance(ans, list):
        out = [[str(ans)]]
    elif ans and isinstance(ans[0], list):
        out = [[str(x) for x in grp] for grp in ans]
    elif n_blanks == 1:
        out = [[str(x) for x in ans]]
    elif len(ans) == n_blanks:
        out = [[str(x)] for x in ans]
    else:
        out = [[str(x) for x in ans]]
    while len(out) < n_blanks:
        out.append([""])
    return out[:n_blanks]


def _round_score(val: float) -> float:
    return math.floor(val * 100 + 0.5) / 100


EXAMPASS_PHYSICS_SAMPLE_SHA256 = (
    "2ed1126963ac6d8ff44dbd7f0a119eca22e7b38119d39d90c77d82394f4a62a7"
)

_MATH_EXPR_RE = re.compile(r"[\\/^{}_=πΔε√∫∑±×·]")


def _has_math_expression(text: str) -> bool:
    return bool(_MATH_EXPR_RE.search(str(text or "")))


def grade_objective_question(
    *,
    q_type: str,
    stem: str,
    options: list[str] | None,
    correct_answer: Any,
    user_answer: Any,
    max_points: float,
) -> tuple[str, bool | None, float | None, str]:
    """服务端客观题判分，返回 (grading_status, is_correct, score, feedback)。"""

    if q_type == "choice":
        expected = choice_answer_index(correct_answer, options)
        actual = choice_answer_index(user_answer, options) if user_answer is not None else -1
        ok = actual == expected
        return (
            "auto_graded",
            ok,
            (float(max_points) if ok else 0.0),
            ("回答正确。" if ok else "选择有误，请查看解析。"),
        )

    if q_type == "tf":
        expected = tf_answer_index(correct_answer)
        actual = tf_answer_index(user_answer) if user_answer is not None else -1
        ok = actual == expected
        return (
            "auto_graded",
            ok,
            (float(max_points) if ok else 0.0),
            ("判断正确。" if ok else "判断有误，请查看解析。"),
        )

    if q_type == "multi":
        expected_set = multi_answer_set(correct_answer, options)
        actual_set = multi_answer_set(user_answer or [], options)
        if actual_set == expected_set:
            return "auto_graded", True, float(max_points), "多选全部正确。"
        if actual_set and actual_set.issubset(expected_set):
            partial = _round_score(max_points * len(actual_set) / max(1, len(expected_set)))
            return "auto_graded", False, partial, "漏选部分正确选项。"
        return "auto_graded", False, 0.0, "存在错选或未作答。"

    if q_type == "fill":
        n = max(1, fill_blank_count(stem))
        blanks = fill_blank_answers(correct_answer, n)
        user_vals = user_answer if isinstance(user_answer, list) else [user_answer]
        hit = 0
        ambiguous_math = False
        for idx, accepted in enumerate(blanks):
            raw_u = str(user_vals[idx] if idx < len(user_vals) else "")
            u = norm_text(raw_u)
            if u and any(a != "" and norm_text(a) == u for a in accepted):
                hit += 1
            elif u and (
                _has_math_expression(raw_u)
                or any(_has_math_expression(a) for a in accepted)
            ):
                ambiguous_math = True

        ok = hit == n
        if not ok and ambiguous_math:
            return (
                "pending_review",
                None,
                None,
                "包含数学表达式，写法可能与标准答案等价但形式不同，已标为待核对，请对照解析或前往对话确认。",
            )
        score = _round_score(max_points * hit / n)
        msg = "填空全部正确。" if ok else (f"答对 {hit}/{n} 空。" if hit > 0 else "填空有误，请核对规范写法或解析。")
        return "auto_graded", ok, score, msg

    raise ValueError(f"非客观题类型不能自动判分：{q_type}")


def parse_source_pages(source_refs: list[str] | None, max_page: int) -> list[int]:
    pages: set[int] = set()
    for ref in source_refs or []:
        s = str(ref)
        if "/" in s:
            s = s.split("/", 1)[1]
        for m in re.finditer(r"(\d+)\s*[-~–]\s*(\d+)", s):
            a, b = int(m.group(1)), int(m.group(2))
            if 1 <= a <= b <= max_page and b - a < 200:
                pages.update(range(a, b + 1))
        no_range = re.sub(r"\d+\s*[-~–]\s*\d+", " ", s)
        for m in re.finditer(r"\d+", no_range):
            p = int(m.group(0))
            if 1 <= p <= max_page:
                pages.add(p)
    return sorted(pages)


def load_sample_exampass_bundle(sample_dir: Path, *, max_page: int) -> dict[str, Any]:
    """只读读取本地 ExamPass 样例中间产物，供检查点 2 导入验证展示。"""

    skeleton_path = sample_dir / "knowledge_skeleton.json"
    notes_path = sample_dir / "notes" / "ch1.html"
    questions_path = sample_dir / "questions" / "ch1.json"

    if not skeleton_path.is_file() or not notes_path.is_file() or not questions_path.is_file():
        raise FileNotFoundError("样例目录缺少 knowledge_skeleton.json / notes/ch1.html / questions/ch1.json。")

    skeleton = json.loads(skeleton_path.read_text(encoding="utf-8"))
    raw_notes_html = notes_path.read_text(encoding="utf-8")
    # 清洗无可靠考纲依据的确定性“必考/高频”标签
    raw_notes_html = raw_notes_html.replace(
        '<span class="tag tag-must">必考</span>',
        '<span class="tag tag-key">核心</span>',
    ).replace(
        '<span class="tag tag-freq">高频</span>',
        '<span class="tag tag-info">重点</span>',
    ).replace(
        r"\frac{Q^2}{4\pi\varepsilon_0}\ln\frac{R_2}{R_1}",
        r"\frac{Q^2}{4\pi\varepsilon_0 l}\ln\frac{R_2}{R_1}",
    )
    notes_html = sanitize_notes_html(raw_notes_html)
    questions_payload = json.loads(questions_path.read_text(encoding="utf-8"))
    graph_tree = skeleton_to_graph_tree(skeleton)

    kc_pages: dict[str, list[int]] = {}
    for ch in skeleton.get("chapters", []) or []:
        for kc in ch.get("kcs", []) or []:
            kc_pages[str(kc.get("id", ""))] = parse_source_pages(kc.get("source_refs"), max_page)

    normalized_questions: list[dict[str, Any]] = []
    for idx, q in enumerate(questions_payload.get("questions", []) or []):
        kc_id = str(q.get("kc_id") or "")
        normalized_questions.append(
            {
                "sort_order": idx + 1,
                "kc_id": kc_id,
                "q_type": str(q.get("type") or "choice"),
                "stem": str(q.get("question") or ""),
                "options": q.get("options"),
                "answer": q.get("answer"),
                "explanation": str(q.get("explanation") or ""),
                "pitfall": str(q.get("pitfall") or ""),
                "cognitive": str(q.get("cognitive") or ""),
                "difficulty": str(q.get("difficulty") or ""),
                "points": float(q.get("points") or 5),
                "source_pages": kc_pages.get(kc_id, []),
            }
        )

    return {
        "title": str(skeleton.get("title") or "导入样例"),
        "skeleton": skeleton,
        "notes_html": notes_html,
        "graph_tree": graph_tree,
        "questions": normalized_questions,
    }
