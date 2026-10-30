"""读取任务书第十八节的导师视觉判读数据（机器可读块）。

第十八节把逐卷页码、题量、编号裁定、选择序列、填空校录、公式转写和图像区域
写成了五个带标记的 JSON 块。这里是唯一的解析入口：抽取器只从任务书读取，
不在代码里另抄一份，避免"代码里的数和任务书里的数"不一致。

任务书改版时校验会立刻报错（块缺失、JSON 不合法、试卷 ID 未知、页码越界、
序列长度与题量不符），而不是悄悄用旧数据继续跑。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import common as C

TASK_DOC = C.DATA_ROOT.parent / "docs" / "tasks" / "F007-Python真题题库.md"

MARKERS = {
    "profiles": "F007_SOURCE_VISUAL_PROFILES",
    "choice_gold": "F007_SOURCE_VISUAL_CHOICE_GOLD",
    "fill_gold": "F007_SOURCE_VISUAL_FILL_GOLD",
    "figures": "F007_SOURCE_VISUAL_FIGURES",
    "samples": "F007_SOURCE_VISUAL_SAMPLES",
}

# 18.3 里以散文写出的空号映射（小题号 → 空号列表），由 overrides.json 引用
FORMULA_HEAD = "### 18.6"
_QUESTION_LABEL = re.compile(r"^(choice|fill|code)\s*[-–—]\s*(\d{1,2})")


def parse_formulas(text: str) -> list[dict[str, Any]]:
    """解析 18.6 的公式校录表（Markdown 表格）。"""

    if FORMULA_HEAD not in text:
        return []
    body = text[text.index(FORMULA_HEAD) :]
    end = body.find("\n## ", 1)
    body = body if end < 0 else body[:end]
    rows: list[dict[str, Any]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("| py-"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        paper_id, question, page, formula = cells[:4]
        rows.append(
            {
                "paper_id": paper_id,
                "question": question,
                "page": page,
                "formula": formula,
            }
        )
    return rows


class MentorDataError(RuntimeError):
    """任务书里的结构化块缺失或不合法。"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_block(text: str, marker: str) -> Any:
    """取出 ``<!-- marker -->`` 之后第一个 ```json 块并解析。"""

    token = f"<!-- {marker} -->"
    if token not in text:
        raise MentorDataError(f"任务书缺少标记 {marker}")
    start = text.index(token)
    fence = text.find("```json", start)
    if fence < 0:
        raise MentorDataError(f"{marker} 后没有 json 代码块")
    body_start = fence + len("```json")
    end = text.find("```", body_start)
    if end < 0:
        raise MentorDataError(f"{marker} 的 json 代码块没有结束标记")
    body = text[body_start:end]
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:  # pragma: no cover - 防御
        raise MentorDataError(f"{marker} 的 JSON 不合法：{error}") from error


@dataclass
class MentorData:
    """第十八节的结构化内容 + 出处指纹。"""

    doc_path: Path
    doc_sha256: str
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    choice_gold: dict[str, dict[str, Any]] = field(default_factory=dict)
    fill_gold: dict[str, dict[str, Any]] = field(default_factory=dict)
    figures: list[dict[str, Any]] = field(default_factory=list)
    samples: dict[str, dict[str, Any]] = field(default_factory=dict)
    formulas: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def profile(self, paper_id: str) -> dict[str, Any]:
        return self.profiles.get(paper_id, {})

    def expected(self, paper_id: str) -> dict[str, int]:
        return dict(self.profile(paper_id).get("expected") or {})

    def clean_pages(self, paper_id: str) -> set[int]:
        span = self.profile(paper_id).get("clean_page_range") or []
        if len(span) != 2:
            return set()
        return set(range(int(span[0]), int(span[1]) + 1))

    def answer_pages(self, paper_id: str) -> set[int]:
        profile = self.profile(paper_id)
        pages = set(profile.get("choice_fill_answer_pages") or [])
        pages |= set(profile.get("code_answer_pages") or [])
        return {int(page) for page in pages}

    def choice_values(self, paper_id: str) -> tuple[int | None, str]:
        """返回（答案页, 从第 1 题起的选项序列）。"""

        entry = self.choice_gold.get(paper_id) or {}
        return entry.get("page"), entry.get("values", "")

    def fill_values(self, paper_id: str) -> list[str]:
        entry = self.fill_gold.get(paper_id) or {}
        return list(entry.get("values_by_blank_1_to_20") or [])

    def figures_for(self, paper_id: str) -> list[dict[str, Any]]:
        return [figure for figure in self.figures if figure.get("paper_id") == paper_id]

    def formulas_for(self, paper_id: str) -> dict[str, dict[str, Any]]:
        return formulas_for(self, paper_id)

    def totals(self) -> dict[str, int]:
        return totals(self)

    def source_note(self, *, page: int | None = None, kind: str = "mentor_visual") -> dict[str, Any]:
        note = {
            "kind": kind,
            "doc": C.relative(self.doc_path),
            "doc_sha256": self.doc_sha256,
            "section": "18",
        }
        if page is not None:
            note["page"] = int(page)
        return note


def load(path: Path | None = None) -> MentorData:
    doc_path = Path(path) if path else TASK_DOC
    if not doc_path.exists():
        raise MentorDataError(f"找不到任务书：{doc_path}")
    text = doc_path.read_text(encoding="utf-8")
    blocks = {name: extract_block(text, marker) for name, marker in MARKERS.items()}

    profiles = {entry["paper_id"]: entry for entry in blocks["profiles"]}
    data = MentorData(
        doc_path=doc_path,
        doc_sha256=sha256_text(text),
        profiles=profiles,
        choice_gold=blocks["choice_gold"],
        fill_gold=blocks["fill_gold"],
        figures=blocks["figures"],
        samples={entry["paper_id"]: entry for entry in blocks["samples"]},
        formulas=parse_formulas(text),
        raw=blocks,
    )
    return data


def formulas_for(data: MentorData, paper_id: str) -> dict[str, dict[str, Any]]:
    """把 18.6 的公式按 "choice-02" 这样的题目标签建索引。"""

    result: dict[str, dict[str, Any]] = {}
    for row in data.formulas:
        if row["paper_id"] != paper_id:
            continue
        # 题目标识可能带补充说明（"fill-01（空41）"），只取大题-题号前缀
        match = _QUESTION_LABEL.match(row["question"].strip())
        if not match:
            continue
        label = f"{match.group(1)}-{int(match.group(2)):02d}"
        result[label] = {
            "text": row["formula"],
            "page": row["page"],
            "source": "mentor_visual_transcription",
            "doc_sha256": data.doc_sha256,
        }
    return result


def validate(data: MentorData, catalog: Iterable[dict[str, Any]]) -> list[str]:
    """把任务书数据与题库目录、物理页、题量对一遍，返回问题列表。"""

    problems: list[str] = []
    known = {item["id"] for item in catalog}
    python_ids = {item["id"] for item in catalog if item["id"].startswith("py-")}

    for paper_id in data.profiles:
        if paper_id not in python_ids:
            problems.append(f"profiles 里的试卷不在目录中：{paper_id}")
    missing = python_ids - set(data.profiles)
    if missing:
        problems.append(f"profiles 缺试卷：{sorted(missing)}")

    for paper_id, profile in data.profiles.items():
        pages = int(profile.get("physical_pages") or 0)
        clean = profile.get("clean_page_range") or []
        if len(clean) == 2 and (clean[0] < 1 or clean[1] > pages):
            problems.append(f"{paper_id} clean_page_range 越界：{clean}（共 {pages} 页）")
        for key in ("choice_fill_answer_pages", "code_answer_pages"):
            for page in profile.get(key) or []:
                if not 1 <= int(page) <= pages:
                    problems.append(f"{paper_id} {key} 页号越界：{page}")
        expected = profile.get("expected") or {}
        for section in (C.SECTION_CHOICE, C.SECTION_FILL, C.SECTION_CODE):
            if section not in expected:
                problems.append(f"{paper_id} expected 缺 {section}")

    for paper_id, entry in data.choice_gold.items():
        expected = int(data.expected(paper_id).get(C.SECTION_CHOICE) or 0)
        values = entry.get("values") or ""
        if expected and len(values) != expected:
            problems.append(
                f"{paper_id} 选择序列长度 {len(values)} 与 expected {expected} 不符"
            )
        if any(letter not in "ABCD" for letter in values):
            problems.append(f"{paper_id} 选择序列含非 ABCD 字符")
        if paper_id not in data.profiles:
            problems.append(f"choice_gold 里的试卷没有 profile：{paper_id}")

    for paper_id, entry in data.fill_gold.items():
        values = entry.get("values_by_blank_1_to_20") or []
        if len(values) != 20:
            problems.append(f"{paper_id} 填空校录不是 20 项：{len(values)}")
        page = int(entry.get("answer_page") or 0)
        pages = int(data.profile(paper_id).get("physical_pages") or 0)
        if pages and not 1 <= page <= pages:
            problems.append(f"{paper_id} 填空校录页码越界：{page}")

    for figure in data.figures:
        paper_id = figure.get("paper_id")
        bbox = figure.get("bbox") or []
        pages = int(data.profile(paper_id, ).get("physical_pages") or 0) if paper_id in data.profiles else 0
        if len(bbox) != 4:
            problems.append(f"{paper_id} 图像 bbox 不是 4 个值：{bbox}")
        elif pages and not 1 <= int(figure.get("page", 0)) <= pages:
            problems.append(f"{paper_id} 图像页码越界：{figure.get('page')}")

    for paper_id in data.samples:
        if paper_id not in data.profiles:
            problems.append(f"samples 里的试卷没有 profile：{paper_id}")

    return problems


def totals(data: MentorData) -> dict[str, int]:
    total = {"choice": 0, "fill": 0, "code": 0}
    for profile in data.profiles.values():
        for key in total:
            total[key] += int((profile.get("expected") or {}).get(key) or 0)
    total["questions"] = sum(total.values())
    total["papers"] = len(data.profiles)
    total["pages"] = sum(int(p.get("physical_pages") or 0) for p in data.profiles.values())
    return total


def blank_map(paper_id: str, overrides: dict[str, Any]) -> dict[int, list[int]]:
    """小题号 → 空号列表；没配置时按 1:1 顺序推（由调用方决定是否可用）。"""

    entry = (overrides.get(paper_id) or {}).get("fill_subquestion_map") or {}
    return {int(key): [int(value) for value in values] for key, values in entry.items()}