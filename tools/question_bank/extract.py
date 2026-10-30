"""从原始 PDF 抽取"题目 + 答案候选 + 出处页码"。

本脚本只产出**候选**结构化数据，不产出 approved 结论：

- 题目：题干、选项、代码行、空位编号，保留原始页码；
- 答案：四值状态（original / missing / pending_review / conflict），
  每条答案附载体类型、页码与"对应关系是否已核对"；
- 交叉检查：题数、题号连续、载体与题号数量一致、卷头异同；
- 复核素材：题干区域裁片、答案页整页图、逐题答案区域裁片。

读取方式说明：文字层取不到公式/涂黑标记，因此涂黑答案用**页面渲染后的
墨迹密度**判定（实心填充、斜线纹理、图案填充都能覆盖），并把每格墨迹值
写进证据，便于事后核对。
"""

from __future__ import annotations

import argparse
import ast
import re
import statistics
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

warnings.filterwarnings("ignore", category=SyntaxWarning)

import pymupdf  # noqa: E402  (ingest 组依赖)

from tools.question_bank import common as C  # noqa: E402

TOOL_VERSION = "extract.py@0.1"
LETTERS = ("A", "B", "C", "D")
ZOOM = 1.6
DARK = 170  # 灰度阈值：低于该值算墨迹


# ----------------------------------------------------------------------
# 页面读取
# ----------------------------------------------------------------------


@dataclass
class Line:
    page: int
    text: str
    bbox: tuple[float, float, float, float]
    red: bool
    column: int = 0


@dataclass
class PageData:
    index: int
    width: float
    height: float
    lines: list[Line]
    running_lines: list[str]
    letters: list[tuple[str, tuple[float, float, float, float]]]
    numbers: list[tuple[int, tuple[float, float, float, float]]]
    header: dict[str, Any]
    drawing_count: int
    page_decl: tuple[int, int] | None
    pixmap: Any | None = None
    grid_answers: dict[int, dict[str, Any]] = field(default_factory=dict)
    grid_meta: dict[str, Any] | None = None


def _is_red(color: int) -> bool:
    red = (color >> 16) & 255
    green = (color >> 8) & 255
    blue = color & 255
    return red > 120 and green < 90 and blue < 90


def detect_column_split(lines: Sequence[Line]) -> float | None:
    """检测一页是否是"左右两栏"排版，返回分栏 x 坐标。

    编程题常把函数写左栏、主程序写右栏。按 (y, x) 排序会交错两栏，
    所以先按 x 找一个宽的空白带：左侧各行的右边界都在空白带左边，
    右侧各行的左边界都在空白带右边。整行宽的题干行不算冲突。
    """

    if len(lines) < 10:
        return None
    starts = sorted(line.bbox[0] for line in lines)
    for left_edge, right_edge in zip(starts, starts[1:]):
        gap = right_edge - left_edge
        if gap < 80:
            continue
        split = left_edge + gap / 2
        left = [line for line in lines if line.bbox[2] <= split + 1]
        right = [line for line in lines if line.bbox[0] >= split - 1]
        if len(left) < 5 or len(right) < 5:
            continue
        # 左栏里不能有横跨到右栏的行（整行宽的题干行会被判到 left，需排除）
        if any(line.bbox[2] > split + 1 for line in left):
            continue
        return split
    return None


def order_two_column_lines(lines: list[Line], split: float) -> list[Line]:
    """两栏代码页：按题号分带，带内先左栏后右栏。

    编程题常把函数写左栏、主程序写右栏。若按 y 逐行读，两栏会交错；
    若整页先读左栏再读右栏，右栏又会被算到下一道题。按题号把页面切成
    若干"带"，带内先输出左栏代码、再输出右栏代码，既保留块结构也不串题。
    """

    right_has_code = any(
        line.column == 1
        and (line.text.startswith((" ", "\t")) or _CODE_LINE.match(line.text))
        for line in lines
    )
    if not right_has_code:
        return sorted(lines, key=lambda item: (round(item.bbox[1] / 6), item.bbox[0]))

    anchors = [
        line
        for line in lines
        if line.column == 0
        and (C.split_question_number(line.text) is not None or re.match(r"^\d{1,2}\s*[.．、]", line.text))
    ]
    anchors.sort(key=lambda item: item.bbox[1])
    bounds = [line.bbox[1] for line in anchors]

    def band_of(line: Line) -> int:
        index = 0
        for position, top in enumerate(bounds):
            if line.bbox[1] >= top - 3:
                index = position
            else:
                break
        return index

    grouped: dict[int, dict[int, list[Line]]] = defaultdict(lambda: defaultdict(list))
    for line in lines:
        grouped[band_of(line)][line.column].append(line)

    ordered: list[Line] = []
    for band in sorted(grouped):
        for column in (0, 1):
            ordered.extend(
                sorted(grouped[band].get(column, []), key=lambda item: (item.bbox[1], item.bbox[0]))
            )
    return ordered


def assign_columns(lines: list[Line], split: float | None) -> None:
    """给每行标列号：整行宽的行归左栏（题干），其余按分栏线归位。"""

    if split is None:
        for line in lines:
            line.column = 0
        return
    for line in lines:
        line.column = 1 if line.bbox[0] >= split else 0


def load_page(doc: pymupdf.Document, index: int) -> PageData:
    page = doc[index]
    payload = page.get_text("dict")
    lines: list[Line] = []
    letters: list[tuple[str, tuple[float, float, float, float]]] = []
    numbers: list[tuple[int, tuple[float, float, float, float]]] = []
    running: list[str] = []

    for block in payload.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if not text.strip():
                continue
            bbox = tuple(line["bbox"])  # type: ignore[assignment]
            red = any(_is_red(int(span.get("color", 0))) for span in line.get("spans", []))
            value = text.rstrip("\n")
            if (
                C.is_running_line(value)
                or C.is_page_number(value, bbox, page.rect.height)
                or C.is_header_line(value, bbox)
                or "考试对象" in value
            ):
                running.append(C.clean_line(value))
                continue
            lines.append(Line(index + 1, value, bbox, red))
            stripped = C.normalize_text(value).strip()
            if stripped in LETTERS and len(stripped) == 1:
                letters.append((stripped, bbox))
            elif stripped.isdigit() and len(stripped) <= 2 and 1 <= int(stripped) <= 60:
                numbers.append((int(stripped), bbox))

    header = C.header_signature(list(running) + [line.text for line in lines])
    decl = None
    joined = "\n".join(list(running) + [line.text for line in lines])
    parsed = C.parse_page_decl(joined)
    if parsed:
        decl = parsed

    split = detect_column_split(lines)
    assign_columns(lines, split)
    if split is not None:
        lines = order_two_column_lines(lines, split)
    else:
        lines.sort(
            key=lambda item: (item.column, round(item.bbox[1] / 6), round(item.bbox[0], 1))
        )
    return PageData(
        index=index + 1,
        width=page.rect.width,
        height=page.rect.height,
        lines=lines,
        running_lines=running,
        letters=letters,
        numbers=numbers,
        header=header,
        drawing_count=len(page.get_drawings()),
        page_decl=decl,
    )


# ----------------------------------------------------------------------
# 答题纸网格与墨迹
# ----------------------------------------------------------------------


def _grid_lines(page: "pymupdf.Page") -> tuple[list[float], list[float]]:
    vertical: set[float] = set()
    horizontal: set[float] = set()
    for drawing in page.get_drawings():
        rect = drawing["rect"]
        if rect.width < 1.6 and rect.height > 5:
            vertical.add(round(rect.x0, 1))
        elif rect.height < 1.6 and rect.width > 5:
            horizontal.add(round(rect.y0, 1))
    return sorted(vertical), sorted(horizontal)


def is_grid_page(page: PageData) -> bool:
    return len(page.letters) >= 30 and len(page.numbers) >= 10


@dataclass
class GridCell:
    label: str
    rect: tuple[float, float, float, float]


def _cell_rect(bbox: Sequence[float], vertical: list[float], horizontal: list[float]):
    left = [value for value in vertical if value <= bbox[0] + 1.5]
    right = [value for value in vertical if value >= bbox[2] - 1.5]
    top = [value for value in horizontal if value <= bbox[1] + 1.5]
    bottom = [value for value in horizontal if value >= bbox[3] - 1.5]
    if not left or not right or not top or not bottom:
        return None
    if right[0] <= left[-1] or bottom[0] <= top[-1]:
        return None
    return (left[-1], top[-1], right[0], bottom[0])


def build_grid(
    letters: Sequence[tuple[str, Sequence[float]]],
    numbers: Sequence[tuple[int, Sequence[float]]],
    vertical: list[float],
    horizontal: list[float],
) -> dict[str, Any]:
    """把字母格与题号配成"每题 4 个候选格"。

    关键约束：只有落在**网格边框内部**的题号才参与配对。答题纸下方的填空
    答案区、页脚等处也会出现裸数字（例如答案正文就是 "2"、"4"），如果直接
    用整页数字，就会把填空题的答案数字当成选择题题号。

    纯几何计算，不读像素，便于单测。
    """

    meta: dict[str, Any] = {
        "bounds": None,
        "rows": 0,
        "questions": 0,
        "letters": len(letters),
        "numbers_in_grid": 0,
        "numbers_outside_grid": len(numbers),
        "cells": 0,
        "ignored_numbers": [],
    }
    if len(vertical) < 5 or len(horizontal) < 5:
        return {"questions": {}, "meta": meta}

    left_edge, right_edge = vertical[0], vertical[-1]
    top_edge, bottom_edge = horizontal[0], horizontal[-1]
    meta["bounds"] = [left_edge, top_edge, right_edge, bottom_edge]

    # 题号通常画在第一列竖线的**左侧**（"1 [A][B][C][D]"），所以横向边界要
    # 向左放宽；纵向仍严格限制在网格内，用来排除下方填空答案区的裸数字。
    left_limit = left_edge
    for _number, bbox in numbers:
        if top_edge - 1 <= (bbox[1] + bbox[3]) / 2 <= bottom_edge + 1:
            left_limit = min(left_limit, bbox[0] - 2)

    def inside(bbox: Sequence[float]) -> bool:
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        return left_limit - 1 <= cx <= right_edge + 1 and top_edge - 1 <= cy <= bottom_edge + 1

    grid_letters = [(label, bbox, _cell_rect(bbox, vertical, horizontal)) for label, bbox in letters]
    grid_letters = [item for item in grid_letters if item[2] is not None and inside(item[1])]

    grid_numbers: list[tuple[int, Sequence[float]]] = []
    ignored: list[int] = []
    for number, bbox in numbers:
        if inside(bbox):
            grid_numbers.append((number, bbox))
        else:
            ignored.append(int(number))
    meta["ignored_numbers"] = sorted(ignored)
    meta["numbers_in_grid"] = len(grid_numbers)
    meta["numbers_outside_grid"] = len(ignored)

    # 行带：按水平线切；题号与字母落在同一条行带才算同一行。
    band_of = lambda bbox: (
        max((value for value in horizontal if value <= bbox[1] + 1.5), default=None),
        min((value for value in horizontal if value >= bbox[3] - 1.5), default=None),
    )
    rows: dict[tuple[float, float], list[tuple[str, tuple[float, float, float, float]]]] = defaultdict(list)
    for label, bbox, rect in grid_letters:
        band = band_of(bbox)
        if None in band:
            continue
        rows[band].append((label, rect))  # type: ignore[arg-type]
    meta["rows"] = len(rows)

    questions: dict[int, list[GridCell]] = {}
    for number, bbox in sorted(grid_numbers, key=lambda item: (item[1][1], item[1][0])):
        band = band_of(bbox)
        candidates = rows.get(band, [])  # type: ignore[arg-type]
        right_side = [
            (label, rect) for label, rect in candidates if rect[0] >= bbox[0] - 2
        ]
        right_side.sort(key=lambda item: item[1][0])
        if len(right_side) < 4:
            continue
        questions[int(number)] = [GridCell(label=label, rect=rect) for label, rect in right_side[:4]]

    meta["questions"] = len(questions)
    meta["cells"] = sum(len(items) for items in questions.values())
    return {"questions": questions, "meta": meta}


def _ink(pixmap: Any, zoom: float, box: Sequence[float], step: int = 2) -> float:
    x0, y0, x1, y1 = box
    left, top = int(x0 * zoom), int(y0 * zoom)
    right, bottom = int(x1 * zoom), int(y1 * zoom)
    width, height = pixmap.width, pixmap.height
    buffer = pixmap.samples
    total = 0
    count = 0
    for y in range(top, min(bottom, height), step):
        base = y * width
        for x in range(left, min(right, width), step):
            if buffer[base + x] < DARK:
                total += 1
            count += 1
    return total / max(count, 1)


def pick_marked(
    cells: Sequence[tuple[GridCell, float]], median_ink: float
) -> tuple[GridCell, float, float] | None:
    """在一题的 4 个候选格里挑出被涂黑的一个；分不清就返回 None。"""

    if len(cells) < 2:
        return None
    best_cell, best_ink = max(cells, key=lambda item: item[1])
    runner_up = max((ink for _cell, ink in cells if ink is not best_ink), default=0.0)
    if best_ink <= max(0.30, runner_up * 1.6):
        return None
    if median_ink and best_ink <= median_ink * 1.6:
        return None
    return best_cell, best_ink, runner_up


def decode_grid(page: PageData, pdf_page: "pymupdf.Page", zoom: float = ZOOM) -> None:
    """按单元格墨迹密度解码涂黑答案（实心填充与斜线纹理都适用）。"""

    if not is_grid_page(page):
        return
    vertical, horizontal = _grid_lines(pdf_page)
    grid = build_grid(page.letters, page.numbers, vertical, horizontal)
    questions = grid["questions"]
    meta = grid["meta"]
    if not questions:
        page.grid_meta = {**meta, "page": page.index, "answers": 0}
        return

    pixmap = pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY)
    measured: dict[int, list[tuple[GridCell, float]]] = {}
    for number, cells in questions.items():
        measured[number] = [(cell, _ink(pixmap, zoom, cell.rect)) for cell in cells]

    all_ink = [ink for cells in measured.values() for _cell, ink in cells]
    median_ink = statistics.median(all_ink) if all_ink else 0.0

    answers: dict[int, dict[str, Any]] = {}
    for number, cells in measured.items():
        picked = pick_marked(cells, median_ink)
        if picked is None:
            continue
        cell, ink, runner_up = picked
        answers[number] = {
            "label": cell.label,
            "ink": round(ink, 3),
            "runner_up": round(runner_up, 3),
            "cell": [round(value, 1) for value in cell.rect],
            "page": page.index,
        }

    page.grid_answers = answers
    page.grid_meta = {
        **meta,
        "page": page.index,
        "answers": len(answers),
        "median_ink": round(median_ink, 3),
        "separation": round(
            (statistics.median([item["ink"] for item in answers.values()]) / median_ink)
            if answers and median_ink
            else 0.0,
            2,
        ),
    }


# ----------------------------------------------------------------------
# 副本（同一 PDF 内的题目卷 / 答案卷 / 解析卷）
# ----------------------------------------------------------------------


@dataclass
class Copy:
    start_page: int
    pages: list[PageData]
    declared_total: int | None
    answer_oriented: bool = False
    headers: list[dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"p{self.start_page}-p{self.pages[-1].index}" if self.pages else f"p{self.start_page}"


def build_copies(pages: Sequence[PageData]) -> tuple[list[Copy], list[dict[str, Any]]]:
    """按"第 1 页 共 N 页"切副本；页号不连续与缺首页都记为异常。"""

    issues: list[dict[str, Any]] = []
    starts = [index for index, page in enumerate(pages) if page.page_decl and page.page_decl[0] == 1]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    starts = sorted(set(starts))

    copies: list[Copy] = []
    for position, start in enumerate(starts):
        stop = starts[position + 1] - 1 if position + 1 < len(starts) else len(pages) - 1
        chunk = list(pages[start : stop + 1])
        if not chunk:
            continue
        declared = chunk[0].page_decl[1] if chunk[0].page_decl else None
        copy = Copy(start_page=chunk[0].index, pages=chunk, declared_total=declared)
        copy.headers = [page.header for page in chunk]
        copies.append(copy)
        if declared is not None and len(chunk) != declared:
            issues.append(
                {
                    "kind": "page_count_mismatch",
                    "copy": copy.label,
                    "declared": declared,
                    "observed": len(chunk),
                    "detail": "卷内声明页数与实际页数不一致，可能有跨卷混排或缺页。",
                }
            )
        numbers = [page.page_decl[0] for page in chunk if page.page_decl]
        expected = list(range(1, len(numbers) + 1))
        if numbers and numbers != expected:
            issues.append(
                {
                    "kind": "page_numbering_gap",
                    "copy": copy.label,
                    "observed": numbers,
                    "detail": "卷内页码不连续。",
                }
            )
    return copies, issues


def header_anomalies(copies: Sequence[Copy]) -> list[dict[str, Any]]:
    """同一 PDF 内出现不同学年/年级时记录异常；不据此认定是两套试题。"""

    issues: list[dict[str, Any]] = []
    years = {header.get("academic_year") for copy in copies for header in copy.headers}
    cohorts = {header.get("cohort") for copy in copies for header in copy.headers}
    years.discard(None)
    cohorts.discard(None)
    if len(years) > 1:
        issues.append(
            {
                "kind": "header_year_variants",
                "values": sorted(years),
                "detail": "同一 PDF 内卷头学年不一致；可能混入不同学期材料，也可能是沿用旧模板。"
                "需要按题干与选项内容逐项比对后才能判断。",
            }
        )
    if len(cohorts) > 1:
        issues.append(
            {
                "kind": "header_cohort_variants",
                "values": sorted(cohorts),
                "detail": "同一 PDF 内年级/专业不一致；只说明卷头信息不同，不直接认定答案冲突。",
            }
        )
    return issues


# ----------------------------------------------------------------------
# 题目切分
# ----------------------------------------------------------------------


@dataclass
class RawQuestion:
    section: str
    number: int
    lines: list[Line]
    page_span: list[int]
    copy_label: str


def split_options_by_line(
    lines: Sequence[Line], fallback_stem: str
) -> tuple[str, list[dict[str, str]]]:
    """按行首 "A/B/C/D" 切选项，允许字母后没有点号（`A 分号`）。

    只在正则切分失败时使用；题干续行会并入上一个选项或题干。
    """

    expected = "A"
    stem_parts: list[str] = []
    options: list[dict[str, str]] = []
    current: list[str] = []

    def close() -> None:
        nonlocal current
        if options:
            options[-1]["text"] = C.clean_line(" ".join(current))
        elif current:
            stem_parts.append(C.clean_line(" ".join(current)))
        current = []

    for line in lines:
        text = C.normalize_text(line.text)
        match = re.match(r"^\s*([A-D])\s*[.．、]?\s*(.*)$", text)
        if match and match.group(1) == expected:
            close()
            options.append({"label": match.group(1), "text": ""})
            expected = chr(ord(expected) + 1)
            current = [match.group(2)]
            continue
        current.append(text.strip())
    close()
    if len(options) < 2:
        return fallback_stem, []
    stem = C.clean_line(" ".join(part for part in stem_parts if part)) or fallback_stem
    return stem, options


def restrict_copy(copy: Copy, pages: set[int] | None) -> Copy:
    """把副本限定到指定物理页；用于"题面只取干净页、答案只取答案页"。"""

    if not pages:
        return copy
    kept = [page for page in copy.pages if page.index in pages]
    if not kept:
        return Copy(start_page=copy.start_page, pages=[], declared_total=copy.declared_total)
    return Copy(
        start_page=kept[0].index,
        pages=kept,
        declared_total=copy.declared_total,
        answer_oriented=copy.answer_oriented,
        headers=list(copy.headers),
    )


def parse_copy_questions(copy: Copy, *, answer_copy: bool = False) -> list[RawQuestion]:
    """把一份副本切成小题。

    answer_copy=True 时（答题纸/答案页），没有空位标记的"填空"条目其实是
    答案文本，不应当成题目；选择题的字母网格也不会带选项，自然不会成题。
    """

    questions: list[RawQuestion] = []
    section: str | None = None
    current: RawQuestion | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            questions.append(current)
            current = None

    for page in copy.pages:
        blank_template = bool(page.grid_meta)
        for line in page.lines:
            head = C.section_of(line.text)
            if head is not None:
                flush()
                section = head
                continue
            if section is None:
                continue
            # 少数题号行带一点缩进（不是代码续行），去掉行首空白后再判题号
            stripped_start = line.text.lstrip()
            parsed = C.split_question_number(stripped_start)
            if parsed and 1 <= parsed[0] <= 60:
                numbering_only = not re.sub(r"[\d\s.．、。:：()（）\u3000]", "", line.text)
                opens_question = True
                if numbering_only and len(re.findall(r"\d{1,2}", line.text)) >= 2:
                    # "1.   2." 这类答题模板的编号行，不是题目
                    opens_question = False
                if blank_template and numbering_only:
                    opens_question = False
                if re.match(r"^\d{1,3}\s*、", parsed[1]):
                    # "2、3、5 的正数…" 是句子里的列举，不是新的第 2 题
                    # （原卷"寻找丑数"一题的说明行就以"2、3、5"开头），
                    # 但它是题干正文，必须留作当前题的续行，不能整行丢掉。
                    opens_question = False
                if opens_question:
                    flush()
                    current = RawQuestion(
                        section=section,
                        number=parsed[0],
                        lines=[Line(line.page, parsed[1], line.bbox, line.red)],
                        page_span=[line.page],
                        copy_label=copy.label,
                    )
                    continue
            if section == C.SECTION_FILL and current is not None:
                # 题号后没有点号的填空小题（"6 请用列表生成式…"），只有顺着
                # 上一个题号 +1 时才认，避免把题干里的数字当成新小题。
                loose = re.match(r"^(\d{1,2})[ \u3000]+(\S.*)$", line.text)
                if loose and int(loose.group(1)) == current.number + 1:
                    opening = [Line(line.page, loose.group(2), line.bbox, line.red)]
                    # 空位标记可能印在题号行的上一行（"（12） 。" 然后是
                    # "12 下面程序输出的结果是…"），把它归到新的小题
                    if current.lines:
                        tail = current.lines[-1].text.strip()
                        if re.fullmatch(r"[（(]\s*%d\s*[)）][\s。.．]*" % int(loose.group(1)), tail):
                            moved = current.lines.pop()
                            opening.insert(
                                0, Line(moved.page, moved.text, moved.bbox, moved.red)
                            )
                    flush()
                    current = RawQuestion(
                        section=section,
                        number=int(loose.group(1)),
                        lines=opening,
                        page_span=[line.page],
                        copy_label=copy.label,
                    )
                    continue
            if current is not None:
                current.lines.append(line)
                if line.page not in current.page_span:
                    current.page_span.append(line.page)
    flush()

    if answer_copy:
        kept: list[RawQuestion] = []
        for raw in questions:
            if raw.section == C.SECTION_FILL:
                text = "\n".join(line.text for line in raw.lines)
                if not C.blank_marks(text) and "___" not in text:
                    continue
            kept.append(raw)
        return kept
    return questions


_CODE_LINE = re.compile(
    r"^\s*(?:import|from|def|class|for|while|if|elif|else|try|except|finally|with"
    r"|print\s*\(|return|break|continue|pass|del|raise|assert|global|nonlocal"
    r"|[A-Za-z_][\w\.\[\]\"]*\s*(?:\+=|-=|\*=|/=|//=|%=|=)\s*\S)"
)


def split_statement_code(lines: Sequence[Line]) -> tuple[str, list[Line]]:
    """按版面把一道编程题切成"题面 + 其后全部代码行"。

    找到第一条代码行之后，**剩下所有行都算代码**，不再用关键词逐行过滤。
    逐行过滤会丢掉 `break`、`continue`、`else:`、续行的 `)` 等，拼出来的
    程序就不完整；改成按位置切分后，代码保持原有的行序与缩进。
    """

    index = None
    for position, line in enumerate(lines):
        if _CODE_LINE.match(line.text) or C.looks_like_code(line.text):
            index = position
            break
    if index is None:
        return "\n".join(line.text for line in lines).strip(), []
    statement = "\n".join(line.text for line in lines[:index]).strip()
    return statement, list(lines[index:])


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" ")) if text.strip() else 0


_METHOD_MARK = re.compile(r"^[（(]?\s*方法\s*([0-9一二三四五六])\s*[)）]?\s*[：:]")


def code_blocks(lines: Sequence[Line]) -> list[dict[str, Any]]:
    """把参考代码按版面切成"参考方法 / 函数 / 主程序"块，保持原顺序与缩进。

    卷面常写"方法2："再接另一种解法。不同方法各成一组，组内再按 `def`
    与顶层语句分出函数和主程序；不跨块拼接，避免两种解法交错粘成一段。
    """

    # 先按"方法N："切成若干解法段
    sections: list[tuple[int, str | None, list[Line]]] = []
    method = 1
    label: str | None = None
    current: list[Line] = []
    for line in lines:
        match = _METHOD_MARK.match(line.text.strip())
        if match:
            if current:
                sections.append((method, label, current))
                current = []
            method = (
                int(match.group(1))
                if match.group(1).isdigit()
                else "一二三四五六".index(match.group(1)) + 1
            )
            label = line.text.strip()
            continue
        current.append(line)
    if current:
        sections.append((method, label, current))

    blocks: list[dict[str, Any]] = []
    for method_index, method_label, section_lines in sections:
        groups: list[list[Line]] = []
        chunk: list[Line] = []
        for line in section_lines:
            column_changed = bool(chunk) and line.column != chunk[0].column
            restarted = (
                bool(chunk)
                and len(chunk) >= 6
                and line.text == chunk[0].text
                and line.column == chunk[0].column
            )
            if (re.match(r"^\s*(?:def|class)\s+", line.text) or column_changed or restarted) and chunk:
                groups.append(chunk)
                chunk = [line]
            else:
                chunk.append(line)
        if chunk:
            groups.append(chunk)

        def_blocks: list[list[Line]] = []
        for group in groups:
            if not re.match(r"^\s*(?:def|class)\s+", group[0].text):
                def_blocks.append(group)
                continue
            end = len(group)
            seen_body = False
            for index, line in enumerate(group):
                if index == 0:
                    continue
                if _indent(line.text) > 0:
                    seen_body = True
                elif seen_body and _indent(line.text) == 0:
                    end = index
                    break
            def_blocks.append(group[:end])
            if end < len(group):
                def_blocks.append(group[end:])

        for group in def_blocks:
            text = "\n".join(line.text for line in group).rstrip()
            if not text.strip():
                continue
            match = re.match(r"^(def|class)\s+(\w+)", group[0].text.strip())
            blocks.append(
                {
                    "method": method_index,
                    "method_label": method_label,
                    "column": group[0].column,
                    "kind": "function" if match else "main",
                    "name": match.group(2) if match else None,
                    "text": text,
                    "page": group[0].page,
                    "lines": len(group),
                }
            )
    return merge_continuation_blocks(blocks)


_CONT_END = re.compile(r"[+\-*/%,=({\[\\:]$")
_CONT_START = re.compile(r"^[)\]}.,+\-*/%]")


def merge_continuation_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把"左栏末尾续到右栏开头"的代码接回去。

    两栏排版时程序可能从左上写到右上：左栏最后一行停在半个表达式上。
    这种跨栏不是"另一种解法"，按续行合并，避免把一道题劈成两段。
    """

    merged: list[dict[str, Any]] = []
    for block in blocks:
        if merged and block["column"] != merged[-1]["column"]:
            previous_lines = merged[-1]["text"].rstrip().splitlines()
            current_lines = block["text"].lstrip().splitlines()
            if previous_lines and current_lines:
                if _CONT_END.search(previous_lines[-1].rstrip()) or _CONT_START.match(
                    current_lines[0].lstrip()
                ):
                    merged[-1]["text"] = f"{merged[-1]['text']}\n{block['text']}"
                    merged[-1]["lines"] += block["lines"]
                    continue
        merged.append(block)
    return merged


def question_text(raw: RawQuestion) -> dict[str, Any]:
    raw_text = "\n".join(line.text for line in raw.lines)
    if raw.section == C.SECTION_CHOICE:
        stem, options = C.split_options(raw_text)
        if len(options) < 2:
            # 有些卷的选项不带点号（"A 分号"），按行首字母兜底切分。
            stem, options = split_options_by_line(raw.lines[1:], stem)
        if options:
            statement = stem
        else:
            statement = C.clean_line(raw_text)
        return {"statement": statement, "options": options, "code_lines": []}
    if raw.section == C.SECTION_CODE:
        statement, code = split_statement_code(raw.lines)
        return {
            "statement": statement,
            "options": [],
            "code_lines": [line.text for line in code],
        }
    text = "\n".join(line.text for line in raw.lines)
    return {"statement": text.strip(), "options": [], "code_lines": []}


# 导师 2026-10-27 目视原卷后给出的选项补录。这里只修“文字层确实漏掉、
# 原卷可唯一读出”的 4 题；两条原卷印了两个 B 的题保留四项原文和异常标签，
# 不替原卷改标签。
# 结构：paper_id -> 题号 -> 完整四项；statement_suffix 是旧题干里残留的
# “A. …”选项行，补录后从题干尾部移除。
CHOICE_OPTION_FIXES: dict[tuple[str, int], dict[str, Any]] = {
    ("py-2021-2022-s2-midterm", 22): {
        "options": [
            {"label": "A", "text": "语法错误"},
            {"label": "B", "text": "当a=7 时，表达式结果为False"},
            {"label": "C", "text": "当a=2 时，表达式结果为False"},
            {"label": "D", "text": "当a=10时，表达式结果为True"},
        ],
        "basis": "导师目视 p4–5：原卷 D 项完整可读",
    },
    ("py-2021-2022-s2-midterm", 30): {
        "options": [
            {"label": "A", "text": "15"},
            {"label": "B", "text": "7"},
            {"label": "C", "text": "5"},
            {"label": "D", "text": "死循环"},
        ],
        "statement_suffix": "\nA. 15",
        "basis": "导师目视 p5–6：四项完整",
    },
    ("py-2024-2025-s1-midterm", 40): {
        "options": [
            {"label": "A", "text": "45"},
            {"label": "B", "text": "55"},
            {"label": "C", "text": "18"},
            {"label": "D", "text": "7"},
        ],
        "statement_suffix": "\nA. 45",
        "basis": "导师目视 p5：四项完整",
    },
    ("py-2024-2025-s2-final", 11): {
        "options": [
            {"label": "A", "text": "x=int('16/4')"},
            {"label": "B", "text": "x= eval ('16/4')"},
            {"label": "C", "text": "x= float('16/4')"},
            {"label": "D", "text": "x=eval(16/4)"},
        ],
        "basis": "导师目视 p1–2：原卷 D 项完整可读",
    },
    # 下面两题原卷印成两个 B；保留 A、B、B、D 四项原文，不做静默改标签。
    ("py-2022-2023-s1-final", 11): {
        "options": [
            {"label": "A", "text": "1 3 5 7 9 11 13"},
            {"label": "B", "text": "1 4 7 10 13"},
            {"label": "B", "text": "1 3 5 7 9 13 15"},
            {"label": "D", "text": "1 3 5 7 9 11"},
        ],
        "basis": "导师目视 p4：原卷两个 B，保留原标签",
    },
    ("py-2023-2024-s1-final", 19): {
        "options": [
            {"label": "A", "text": "[2,8,9,11,7,15]"},
            {"label": "B", "text": "[15,11,9,8,7,2]"},
            {"label": "B", "text": "[2,7,8,9,11,15]"},
            {"label": "D", "text": "[15,8,9,11,2,7]"},
        ],
        "basis": "导师目视 p5：原卷两个 B，保留原标签",
    },
}


def apply_choice_option_fix(
    paper_id: str, number: int, content: dict[str, Any]
) -> dict[str, Any]:
    fix = CHOICE_OPTION_FIXES.get((paper_id, number))
    if fix is None:
        return content
    content["options"] = [dict(item) for item in fix["options"]]
    suffix = str(fix.get("statement_suffix") or "")
    if suffix and content["statement"].endswith(suffix):
        content["statement"] = content["statement"][: -len(suffix)].rstrip()
    return content


# ----------------------------------------------------------------------
# 答案解析
# ----------------------------------------------------------------------


_RED_ANSWER = re.compile(r"[（(]\s*([A-D])\s*[)）]")
_TYPED_ITEM = re.compile(r"^(\d{1,2})\s*[.．、]\s*(.*)$")


def red_answers(copy: Copy) -> dict[int, dict[str, Any]]:
    """把红字批注按题目区间归位：区间内出现"（B）"或"答案：B"即该题答案候选。"""

    red_by_page: dict[int, list[Line]] = defaultdict(list)
    for page in copy.pages:
        for line in page.lines:
            if line.red:
                red_by_page[page.index].append(line)
    if not red_by_page:
        return {}

    answers: dict[int, dict[str, Any]] = {}
    for page in copy.pages:
        page_red = red_by_page.get(page.index, [])
        if not page_red:
            continue
        anchors: list[tuple[int, tuple[float, float, float, float]]] = list(page.numbers)
        for line in page.lines:
            parsed = C.split_question_number(line.text)
            if parsed:
                anchors.append((parsed[0], line.bbox))
        anchors.sort(key=lambda item: item[1][1])
        for line in page_red:
            normalized = C.normalize_text(line.text).strip()
            match = _RED_ANSWER.search(normalized.replace(" ", ""))
            label = None
            if match:
                label = match.group(1)
            elif normalized in LETTERS:
                # 解析卷常见写法：单独一行红字字母标注正确选项
                label = normalized
            if label is None:
                continue
            above = [number for number, bbox in anchors if bbox[1] <= line.bbox[1] + 2]
            if not above:
                continue
            answers[above[-1]] = {
                "label": label,
                "page": page.index,
                "excerpt": C.clean_line(line.text)[:120],
                "style": "paren" if match else "letter",
            }
    return answers


def typed_fill_answers(
    copy: Copy,
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """解析答案页上打字的填空答案。

    支持：`1. wel` 同行、`1.` 换行再接答案、一行并排多个
    （`19. sum(grades)          20. grades.sort(...)`）、以及原卷把两个空号
    挤在一起写成 `13.14.` 的情况（两个空共用下一行的答案）。

    返回 (答案, 问题清单)。题号重复或跳号时只记录问题，不擅自改写题号。
    """

    answers: dict[int, dict[str, Any]] = {}
    problems: list[dict[str, Any]] = []
    in_fill = False
    pending: list[int] | None = None
    item_start = re.compile(r"(?:^|[\s\u3000]{2,})(\d{1,2})\s*[.．、]\s*")
    pair_label = re.compile(r"^(\d{1,2})\s*[.．、]\s*(\d{1,2})\s*[.．、]\s*$")
    last_number = 0

    def _extends_answer(text: str) -> bool:
        """判断一行是不是上一空答案的续行，而不是新的题干或解析。"""

        if not text or len(text) > 40:
            return False
        if "解析" in text or "答案" in text:
            return False
        return not re.match(r"^[（(]?\d{1,2}[)）.．、]", text)

    def store(number: int, text: str, page: int) -> None:
        nonlocal last_number
        if number in answers:
            if C.clean_line(answers[number]["text"]) != C.clean_line(text):
                problems.append(
                    {
                        "kind": "fill_answer_duplicate_number",
                        "number": number,
                        "page": page,
                        "text": text,
                        "detail": f"原卷答案页重复出现空号 {number}，两处文本不同；保留先出现的一条。",
                    }
                )
            return
        if last_number and number > last_number + 1:
            problems.append(
                {
                    "kind": "fill_answer_number_gap",
                    "from": last_number,
                    "to": number,
                    "page": page,
                    "detail": f"答案页空号从 {last_number} 跳到 {number}，中间空号没有答案文本。",
                }
            )
        if last_number and number <= last_number:
            problems.append(
                {
                    "kind": "fill_answer_number_order",
                    "previous": last_number,
                    "number": number,
                    "page": page,
                    "detail": f"答案页空号顺序异常（{last_number} 之后出现 {number}），可能是原卷编号笔误。",
                }
            )
        answers[number] = {"text": text, "page": page}
        last_number = max(last_number, number)

    for page in copy.pages:
        page_is_answer = bool(page.grid_answers) or any(
            "答案" in line.text for line in page.lines
        )
        if not page_is_answer:
            continue
        for line in page.lines:
            head = C.section_of(line.text)
            if head == C.SECTION_FILL:
                in_fill = True
                pending = None
                continue
            if head is not None:
                in_fill = False
                pending = None
                continue
            if not in_fill:
                continue
            text = C.normalize_text(line.text).strip()
            if not text:
                continue
            # 带空位标记的行是题目，不是答案
            if C.blank_marks(text) or "___" in text:
                continue
            pair = pair_label.match(text)
            if pair:
                pending = [int(pair.group(1)), int(pair.group(2))]
                continue
            marks = list(item_start.finditer(text))
            if len(marks) >= 2:
                for index, match in enumerate(marks):
                    number = int(match.group(1))
                    stop = marks[index + 1].start() if index + 1 < len(marks) else len(text)
                    body = text[match.end() : stop].strip()
                    if body:
                        store(number, body, page.index)
                        pending = None
                    else:
                        pending = [number]
                continue
            m_item = re.match(r"^(\d{1,2})\s*[.．、]\s*(.*)$", text)
            is_decimal = bool(re.match(r"^\d{1,2}\s*[.．]\d", text))
            if pending and (m_item is None or is_decimal):
                # 期待答案时，"2.0"、"<" 这类整行都是答案文本
                for number in pending:
                    store(number, text, page.index)
                pending = None
                continue
            if m_item:
                number = int(m_item.group(1))
                body = m_item.group(2).strip()
                if body:
                    store(number, body, page.index)
                    pending = None
                else:
                    pending = [number]
                continue
            if pending:
                for number in pending:
                    store(number, text, page.index)
                pending = None
    return answers, problems


_RANGE_HEAD = re.compile(
    r"(\d{1,2})\s*[-–—~]\s*(\d{1,2})\s*[:：]\s*([A-Da-d](?:[\s,，、]*[A-Da-d])*)"
)


def range_choice_answers(pages: Sequence[PageData]) -> dict[int, dict[str, Any]]:
    """解析"1-5:B B B A B  6-10:…"这种按区间的答案清单（2024–25 s2 两卷）。

    只按区间读，不猜单题；区间长度必须和字母个数一致，否则整段跳过，
    避免把漏印或折行读成错位的选项。
    """

    answers: dict[int, dict[str, Any]] = {}
    for page in pages:
        for line in page.lines:
            for match in _RANGE_HEAD.finditer(line.text):
                start, end = int(match.group(1)), int(match.group(2))
                letters = [letter.upper() for letter in re.findall(r"[A-Da-d]", match.group(3))]
                if end - start + 1 != len(letters) or not 1 <= start <= end <= 60:
                    continue
                for offset, letter in enumerate(letters):
                    answers.setdefault(
                        start + offset,
                        {
                            "label": letter,
                            "page": page.index,
                            "detail": f"区间清单 {start}-{end}",
                        },
                    )
    return answers


_FILL_ROW = re.compile(r"^(\d{1,2})\s+(\S.*)$")


def numbered_table_fill_answers(pages: Sequence[PageData]) -> dict[int, dict[str, Any]]:
    """解析"41 答案…"这种按原空号排的表格答案（2024–25 s2 两卷）。

    空号按原卷 41–60 保留，不减 40；只有落在"二、填空题"标题之后、
    以两位编号开头的行才算，答案文本原样保留（含引号、反斜杠、空格）。
    """

    answers: dict[int, dict[str, Any]] = {}
    for page in pages:
        in_fill = False
        for line in page.lines:
            head = C.section_of(line.text)
            if head is not None:
                in_fill = head == C.SECTION_FILL
                continue
            if not in_fill:
                continue
            match = _FILL_ROW.match(line.text.strip())
            if not match:
                continue
            number = int(match.group(1))
            if not 1 <= number <= 99:
                continue
            answers[number] = {
                "text": match.group(2).strip(),
                "page": page.index,
                "detail": "编号表格单元格",
            }
    return answers


def typed_choice_answers(copy: Copy) -> dict[int, dict[str, Any]]:
    """解析形如 `1.A 2.B 3.C` 的答案清单（部分新版卷用打字答案页）。

    只在答案页上启用，并要求一行至少 5 组、题号递增、行首是题号，
    避免把题干里的 "A. 20 B. 56" 误读成答案。
    """

    answers: dict[int, dict[str, Any]] = {}
    in_choice = False
    pattern = re.compile(r"(\d{1,2})\s*[.．、:：]?\s*([A-D])(?![A-Za-z])")
    for page in copy.pages:
        for line in page.lines:
            head = C.section_of(line.text)
            if head == C.SECTION_CHOICE:
                in_choice = True
                continue
            if head is not None:
                in_choice = False
                continue
            if not in_choice:
                continue
            text = C.normalize_text(line.text).strip()
            if not re.match(r"^\d{1,2}", text):
                continue
            hits = pattern.findall(text)
            if len(hits) < 5:
                continue
            numbers = [int(number) for number, _label in hits]
            if any(left >= right for left, right in zip(numbers, numbers[1:])):
                continue
            for number, label in hits:
                answers[int(number)] = {"label": label, "page": page.index}
    return answers


def code_answers(copy: Copy, questions: Sequence[RawQuestion]) -> dict[int, dict[str, Any]]:
    """把编程题里的参考代码按题号取出，并保留该副本的题面用于配对校验。"""

    answers: dict[int, dict[str, Any]] = {}
    for raw in questions:
        if raw.section != C.SECTION_CODE:
            continue
        statement, code_lines = split_statement_code(raw.lines)
        if len(code_lines) >= 2:
            blocks = code_blocks(code_lines)
            # 跨页代码（2024–25 s2 期中从第 9 页续到第 10 页）要把页号带全，
            # 否则答案只写起始页，裁片也只剩一张。
            offset = 0
            for block in blocks:
                span = code_lines[offset : offset + int(block.get("lines") or 0)]
                block["pages"] = sorted({line.page for line in span})
                block["page"] = span[0].page if span else code_lines[0].page
                offset += int(block.get("lines") or 0)
            answers[raw.number] = {
                "code": "\n".join(line.text for line in code_lines),
                "blocks": blocks,
                "page": code_lines[0].page,
                "pages": sorted({line.page for line in code_lines}),
                "statement": statement,
                "copy": copy.label,
                "boxes": [
                    {"text": line.text, "bbox": [round(value, 1) for value in line.bbox], "page": line.page}
                    for line in code_lines
                ],
            }
    return answers


def apply_code_regions(
    pages: Sequence[PageData],
    answers: dict[int, dict[str, Any]],
    config: dict[str, Any],
    pdf_sha256: str,
) -> None:
    """按已目视核对的逐卷区域读取代码；不把旁注或其他方法拼成程序。

    位置规则绑定原文件哈希，并校验首行、行数及语法。PDF 改版或区域失效时
    明确报错，不能静默退回此前已知错误的自动分栏结果。
    """

    if not config:
        return
    if config.get("pdf_sha256") != pdf_sha256:
        raise ValueError("代码区域规则与原 PDF 哈希不符，请重新核对版面")
    by_page = {page.index: page for page in pages}

    def region_lines(page_number: int, spec: dict[str, Any]) -> list[Line]:
        x0, y0, x1, y1 = spec["bbox"]
        selected = sorted(
            (
                line for line in by_page[page_number].lines
                if x0 <= line.bbox[0] and y0 <= line.bbox[1]
                and line.bbox[2] <= x1 and line.bbox[3] <= y1
                and not (spec.get("black_only") and line.red)
            ),
            key=lambda line: (line.bbox[1], line.bbox[0]),
        )
        if len(selected) != spec["expected_lines"] or (
            not selected or selected[0].text.strip() != spec["expected_first"]
        ):
            raise ValueError(f"第 {page_number} 页代码区域 {spec['bbox']} 与核对记录不符")
        return selected

    for question in config["questions"]:
        number = question["number"]
        info = answers[number]
        question_pages = list(question.get("pages") or [question["page"]])
        if info["page"] not in question_pages:
            raise ValueError(f"编程题 {number} 的答案页与区域规则不符")
        blocks = []
        for spec in question["blocks"]:
            page_number = int(spec.get("page") or question_pages[0])
            lines = region_lines(page_number, spec)
            text = "\n".join(line.text.rstrip() for line in lines)
            ast.parse(text)  # 仅检查语法，不执行原卷程序，也不判定算法正确。
            blocks.append({
                "block_id": spec["id"], "method": spec["method"],
                "method_label": spec["label"], "kind": spec.get("kind", "main"),
                "name": None, "text": text, "page": page_number, "lines": len(lines),
                "bbox": spec["bbox"], "depends_on": spec.get("depends_on", []),
                "pages": sorted({line.page for line in lines}),
                "extraction": "reviewed_region",
            })
        ids = {block["block_id"] for block in blocks}
        if len(ids) != len(blocks) or any(
            dependency not in ids for block in blocks for dependency in block["depends_on"]
        ):
            raise ValueError("代码区域存在重复块名或未知前置块")
        primary = next(block for block in blocks if block["block_id"] == question["primary"])
        annotations = []
        for spec in question.get("annotations", []):
            page_number = int(spec.get("page") or question_pages[0])
            lines = region_lines(page_number, spec)
            annotations.append({
                "text": "\n".join(line.text.strip() for line in lines),
                "page": page_number, "bbox": spec["bbox"], "origin": "paper",
                "role": spec.get("role", "note"),
            })
        examples = []
        for spec in question.get("examples", []):
            page_number = int(spec.get("page") or question_pages[0])
            lines = region_lines(page_number, spec)
            examples.append({
                "text": "\n".join(line.text.rstrip() for line in lines),
                "page": page_number, "bbox": spec["bbox"], "origin": "paper",
                "label": spec.get("label", "示例输入输出"),
            })
        used_pages = sorted(
            {block["page"] for block in blocks}
            | {annotation["page"] for annotation in annotations}
            | {example["page"] for example in examples}
        )
        info.update(
            code=primary["text"], blocks=blocks, annotations=annotations,
            examples=examples, page=blocks[0]["page"], pages=used_pages,
            layout={"pdf_sha256": pdf_sha256, "basis": config["basis"],
                    "primary_block": primary["block_id"], "copy": info["copy"]},
        )


# ----------------------------------------------------------------------
# 合并与状态判定
# ----------------------------------------------------------------------


_BLANK_MARK = re.compile(r"[(（]\s*(\d{1,3})\s*[)）]")


def apply_fill_label_fixes(
    fill_all: dict[int, dict[str, Any]],
    problems: Sequence[dict[str, Any]],
    fixes: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把原卷印错的空号映射到规范空号（例如把第二个"17"归为第 23 空）。

    只改"编号归属"，不改原文、不改 PDF；每条映射都保留印出来的标号、所在
    页码和纠正依据，便于复核。答案文本仍然来自原卷答案纸。
    """

    applied: list[dict[str, Any]] = []
    for fix in fixes:
        printed = int(fix["printed"])
        canonical = int(fix["canonical"])
        occurrence = int(fix.get("occurrence", 2))
        duplicates = [
            problem
            for problem in problems
            if problem.get("kind") == "fill_answer_duplicate_number"
            and int(problem.get("number", -1)) == printed
        ]
        if len(duplicates) < occurrence - 1:
            continue
        source = duplicates[occurrence - 2]
        text = source.get("text")
        if not text:
            continue
        fill_all[canonical] = {
            "text": text,
            "page": source["page"],
            "printed_label": printed,
            "label_source": "mentor",
            "label_basis": fix.get("basis", ""),
            "evidence_pages": fix.get("evidence_pages", []),
        }
        applied.append(
            {
                "printed": printed,
                "canonical": canonical,
                "page": source["page"],
                "text": text,
                "basis": fix.get("basis", ""),
                "evidence_pages": fix.get("evidence_pages", []),
            }
        )
    return applied


def inline_fill_answers(pages: Sequence[PageData]) -> dict[int, dict[str, Any]]:
    """读取"答案直接写在空位上方/右侧"的排版（答案版试卷常见）。

    只处理**空位标记所在页**：以标记为锚点，取标记上方 17pt 内、x 在标记
    右侧的短文本行，按纵坐标顺序拼成答案。答案版把多行答案写在横线上方，
    例如第 16 空是 `acdef` 换行后 `ghi`，这里应当拼成两行而不是只取一行。
    因为位置是估的，结果一律标 `pairing_verified=False`，交给人工看裁片。
    """

    found: dict[int, dict[str, Any]] = {}
    for page in pages:
        marks: list[tuple[int, float, float, int]] = []
        rows_with_marks: dict[float, list[float]] = defaultdict(list)
        for line in page.lines:
            text = C.normalize_text(line.text)
            width = max(line.bbox[2] - line.bbox[0], 1.0)
            for match in _BLANK_MARK.finditer(text):
                ratio = match.start() / max(len(text), 1)
                x = line.bbox[0] + ratio * width
                marks.append((int(match.group(1)), x, line.bbox[1], line.page))
                rows_with_marks[round(line.bbox[1], 1)].append(x)
        if not marks:
            continue
        for number, mark_x, mark_y, page_number in marks:
            same_row = sorted(
                value for value in rows_with_marks.get(round(mark_y, 1), []) if value > mark_x + 2
            )
            right_limit = same_row[0] - 2 if same_row else mark_x + 200
            candidates = [
                line
                for line in page.lines
                if mark_x + 6 <= line.bbox[0] <= right_limit
                and mark_y - 20 <= line.bbox[1] <= mark_y - 2
                and 0 < len(C.clean_line(line.text)) <= 40
                and not C.blank_marks(line.text)
                and not re.search(r"[（(）)如果填写下列正确分行]", line.text)
                and "解析" not in line.text
            ]
            candidates.sort(key=lambda line: line.bbox[1])
            if not candidates:
                continue
            text = "\n".join(C.clean_line(line.text) for line in candidates[:3])
            if not text.strip():
                continue
            found.setdefault(
                number,
                {
                    "text": text,
                    "page": page_number,
                    "detail": "答案写在空位上方/右侧的排版文本，按位置拼接（未逐字核对）",
                },
            )
    return found


def embedded_fill_answers(
    statements: dict[int, list[str]]
) -> dict[int, dict[str, Any]]:
    """识别"答案直接写在题干里"的填空（解析卷常见形态）。

    做法：同一题在两个副本里都有文字时，取较短的一份为题干、较长的一份为
    "题干+答案+解析"，忽略空白后的公共前缀之后、`解析` 之前的部分作为答案
    候选。这种切分无法保证准确，因此一律标记为未核对对应关系。
    """

    found: dict[int, dict[str, Any]] = {}
    for number, texts in statements.items():
        unique = sorted({re.sub(r"\s+", "", text) for text in texts}, key=len)
        if len(unique) < 2 or "解析" not in unique[-1]:
            continue
        base, extended = unique[0], unique[-1]
        index = 0
        limit = min(len(base), len(extended))
        while index < limit and base[index] == extended[index]:
            index += 1
        if index < 10:
            continue
        rest = extended[index:].split("解析")[0]
        rest = rest.strip(" :：。；;、")
        if not rest or len(rest) > 80:
            continue
        found[number] = {
            "text": rest,
            "detail": "答案与题干写在同一段文字里，按公共前缀切分（已去空白），需人工确认",
        }
    return found


def _cohort(page_headers: dict[int, dict[str, Any]], pages: Sequence[int]) -> set[str]:
    values = {
        page_headers.get(page, {}).get("cohort")
        for page in pages
    }
    return {value for value in values if value}


def cohort_mismatch(
    page_headers: dict[int, dict[str, Any]],
    question_pages: Sequence[int],
    answer_pages: Sequence[int],
) -> str | None:
    """题干页与答案页的年级/专业不同时给出说明（不直接判定为两套题）。"""

    left = _cohort(page_headers, question_pages)
    right = _cohort(page_headers, answer_pages)
    if left and right and left != right:
        return f"答案页年级 {sorted(right)} 与题干页 {sorted(left)} 不一致"
    return None


def _similar(left: str, right: str) -> float:
    left, right = C.clean_line(left), C.clean_line(right)
    if not left or not right:
        return 0.0
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if shorter[:30] in longer:
        return 1.0
    common = len(set(shorter) & set(longer))
    return common / max(len(set(shorter)), 1)


def choose_primary_raw(
    raws: Sequence[RawQuestion],
    annotated_copies: set[str],
    answer_copies: set[str],
) -> RawQuestion:
    """题面优先取"没有红字批注、也不是答案载体"的副本。

    解析卷的题面里混着答案和讲解，文字更长，但它不是干净的题面。只有在
    没有干净副本时，才退回到解析卷。这不是"取最长文本"。
    """

    def rank(raw: RawQuestion) -> tuple[bool, bool, bool, int]:
        return (
            # 带红字批注的条目里混着答案与讲解，题面不能用它
            any(line.red for line in raw.lines),
            raw.copy_label in annotated_copies,
            raw.copy_label in answer_copies,
            -len(question_text(raw)["statement"]),
        )

    return min(raws, key=rank)


def extract_paper(
    item: dict[str, Any],
    *,
    pdf_path: Path,
    crops_dir: Path | None = None,
    profile: dict[str, Any] | None = None,
    mentor: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    import pymupdf as fitz  # 局部导入，便于测试纯逻辑时不加载

    issues: list[dict[str, Any]] = []
    # 第十八节的校录答案、页码与区域规则都绑定在原 PDF 上：先把实际哈希对一遍，
    # 不一致就立刻失败，绝不带着错位的校录继续抽取。
    actual_pdf_sha256 = C.sha256_file(pdf_path)
    if mentor is not None and profile and profile.get("pdf_sha256"):
        if profile["pdf_sha256"] != actual_pdf_sha256:
            raise ValueError(
                f"{item['id']} 的原 PDF 哈希与第十八节 profile 不符："
                f"记录 {profile['pdf_sha256'][:12]}…，实际 {actual_pdf_sha256[:12]}…"
            )
    document = pymupdf.open(pdf_path)
    try:
        pages = [load_page(document, index) for index in range(document.page_count)]
        for page, pdf_page in zip(pages, document):
            if is_grid_page(page):
                decode_grid(page, pdf_page)
        copies, copy_issues = build_copies(pages)
        issues.extend(copy_issues)
        issues.extend(header_anomalies(copies))

        # 第十八节裁定的题面页/答案页：题面只取干净页，答案页不参与主题面，
        # 这样答案纸上的小题号（15/16/19…）不会被当成额外小题。
        clean_pages: set[int] | None = None
        if profile and len(profile.get("clean_page_range") or []) == 2:
            low, high = profile["clean_page_range"]
            clean_pages = set(range(int(low), int(high) + 1))
        statement_copies = [restrict_copy(copy, clean_pages) for copy in copies]

        # 每份副本分别解析题目与答案载体
        all_questions: list[RawQuestion] = []
        per_copy: list[dict[str, Any]] = []
        all_fill_problems: list[dict[str, Any]] = []
        for copy, statement_copy in zip(copies, statement_copies):
            has_grid = any(page.grid_answers for page in copy.pages)
            mentions_answer = any("答案" in line.text for page in copy.pages for line in page.lines)
            # 题面只从干净页取；答案页（答题纸/答案纸）不贡献小题号
            parsed = parse_copy_questions(
                statement_copy, answer_copy=has_grid or mentions_answer
            )
            grid = {}
            grid_meta = None
            for page in copy.pages:
                grid.update(page.grid_answers)
                if page.grid_meta:
                    grid_meta = page.grid_meta
            fill, fill_problems = typed_fill_answers(copy)
            all_fill_problems.extend(fill_problems)
            for problem in fill_problems:
                issues.append({"copy": copy.label, **problem})
            choice_typed = typed_choice_answers(copy)
            red = red_answers(copy)
            code = code_answers(copy, parsed)
            copy.answer_oriented = bool(grid or fill or choice_typed)
            per_copy.append(
                {
                    "label": copy.label,
                    "pages": [page.index for page in copy.pages],
                    "declared_total": copy.declared_total,
                    "questions": len(parsed),
                    "choice": sum(1 for raw in parsed if raw.section == C.SECTION_CHOICE),
                    "fill": sum(1 for raw in parsed if raw.section == C.SECTION_FILL),
                    "code": sum(1 for raw in parsed if raw.section == C.SECTION_CODE),
                    "grid_answers": len(grid),
                    "grid_meta": grid_meta,
                    "fill_answers": len(fill),
                    "typed_choice_answers": len(choice_typed),
                    "red_answers": len(red),
                    "code_answers": len(code),
                }
            )
            all_questions.extend(parsed)

        # 按 (大题, 题号) 合并，题干取信息量最大的一份
        grouped: dict[tuple[str, int], list[RawQuestion]] = defaultdict(list)
        for raw in all_questions:
            grouped[(raw.section, raw.number)].append(raw)

        # 空白答题纸会把填空题号逐个列出来（"1." "2." ...），这些条目没有题干，
        # 不能当成填空题；有真实题干或有空位标记的才保留。
        for key in list(grouped):
            section, _number = key
            if section != C.SECTION_FILL:
                continue
            text = max(
                (question_text(raw)["statement"] for raw in grouped[key]), key=len
            )
            residue = re.sub(r"[\d\s.．。、()（）]", "", text)
            if not C.blank_marks(text) and len(residue) < 6:
                del grouped[key]

        # 18.3 第 5 条：同一道编程题在原卷与答案页编号不同时按裁定合并
        for fix in (item.get("_overrides") or {}).get("code_label_fixes") or []:
            printed, canonical = int(fix["printed"]), int(fix["canonical"])
            if (C.SECTION_CODE, printed) in grouped:
                grouped[(C.SECTION_CODE, canonical)].extend(grouped.pop((C.SECTION_CODE, printed)))

        # 全局答案载体（跨副本）
        grid_all: dict[int, dict[str, Any]] = {}
        grid_info: dict[str, Any] | None = None
        fill_all: dict[int, dict[str, Any]] = {}
        typed_choice_all: dict[int, dict[str, Any]] = {}
        red_all: dict[int, dict[str, Any]] = {}
        code_all: dict[int, dict[str, Any]] = {}
        fill_answer_pages: list[PageData] = []
        for copy in copies:
            for page in copy.pages:
                if page.grid_answers:
                    grid_all.update(page.grid_answers)
                    grid_info = page.grid_meta
            fill, _fill_problems = typed_fill_answers(copy)
            has_grid = any(page.grid_answers for page in copy.pages)
            mentions_answer = any("答案" in line.text for page in copy.pages for line in page.lines)
            fill_all.update(fill)
            if fill:
                fill_answer_pages.extend(copy.pages)
            typed_choice_all.update(typed_choice_answers(copy))
            red_all.update(red_answers(copy))
            copy_is_answer = bool(fill) or has_grid or mentions_answer
            for number, info in code_answers(
                copy, parse_copy_questions(copy, answer_copy=copy_is_answer)
            ).items():
                # 答案副本里的参考代码优先；同卷重复出现时保留先出现的一份。
                if copy_is_answer or number not in code_all:
                    code_all[number] = info

        # 第十八节裁定的答案页：范围式选择清单、41–60 编号表格只在这几页上解析
        answer_page_set = {
            int(page) for page in (profile or {}).get("choice_fill_answer_pages") or []
        }
        blank_map: dict[int, list[int]] = {}
        if mentor is not None and hasattr(mentor, "blank_map"):
            blank_map = mentor.blank_map(item["id"], item.get("_overrides") or {})
        answer_pages = [page for page in pages if page.index in answer_page_set]
        if answer_pages:
            typed_choice_all.update(range_choice_answers(answer_pages))
            table_fill = numbered_table_fill_answers(answer_pages)
            if table_fill:
                # 答案页就是"41 答案"这样的编号表格时以表格为准：打字列表的解析
                # 在这类页面上会把题干误读成答案
                fill_all = dict(table_fill)
            if not fill_answer_pages:
                fill_answer_pages = answer_pages

        # 导师 18.4/18.5 的视觉校录：作为独立来源写进记录，解码结果只作证据
        gold_choice: dict[int, str] = {}
        gold_choice_page: int | None = None
        gold_fill: list[str] = []
        mentor_doc: dict[str, Any] | None = None
        if mentor is not None:
            gold_choice_page, gold_values = mentor.choice_values(item["id"])
            if gold_values:
                gold_choice = {
                    index + 1: letter.upper() for index, letter in enumerate(gold_values)
                }
                if gold_choice_page is None:
                    gold_choice_page = (
                        int(profile["choice_fill_answer_pages"][0])
                        if profile and profile.get("choice_fill_answer_pages")
                        else None
                    )
                mentor_doc = mentor.source_note(kind="mentor_visual")
            gold_fill = mentor.fill_values(item["id"])
            if gold_fill:
                fill_page = int(
                    (mentor.fill_gold.get(item["id"]) or {}).get("answer_page")
                    or (answer_pages[0].index if answer_pages else 0)
                )
                for blank, value in enumerate(gold_fill, start=1):
                    previous = fill_all.get(blank)
                    fill_all[blank] = {
                        "text": value,
                        "page": fill_page,
                        "source_kind": "mentor_visual_transcription",
                        "parser_text": (previous or {}).get("text"),
                        "evidence_pages": mentor.fill_gold[item["id"]].get(
                            "extra_evidence_pages", {}
                        ),
                    }
                if gold_choice_page is None and answer_pages:
                    gold_choice_page = answer_pages[0].index

        choice_numbers = sorted(
            number for section, number in grouped if section == C.SECTION_CHOICE
        )
        fill_numbers = sorted(number for section, number in grouped if section == C.SECTION_FILL)
        fill_blanks = sorted(
            {
                blank
                for (section, _number) in grouped
                if section == C.SECTION_FILL
                for blank in C.blank_marks(question_text(grouped[(section, _number)][0])["statement"])
            }
        )

        grid_verified = bool(grid_all) and sorted(grid_all) == choice_numbers
        code_numbers = sorted(number for section, number in grouped if section == C.SECTION_CODE)
        code_verified = bool(code_all) and bool(code_numbers) and sorted(code_all) == code_numbers
        if grid_all and not grid_verified:
            issues.append(
                {
                    "kind": "grid_question_mismatch",
                    "grid_questions": sorted(grid_all)[:5] + ["…"],
                    "grid_count": len(grid_all),
                    "choice_count": len(choice_numbers),
                    "detail": "涂黑格的题号与选择题题号集合不一致，答案对应关系按待复核处理。",
                }
            )
        fill_verified = bool(fill_all) and set(fill_blanks) <= set(fill_all)

        page_headers = {page.index: page.header for page in pages}
        has_annotation_copy = any(
            line.red for page in pages for line in page.lines
        )
        fill_embedded = embedded_fill_answers(
            {
                number: [question_text(raw)["statement"] for raw in raws]
                for (section, number), raws in grouped.items()
                if section == C.SECTION_FILL
            }
        )
        # 打字答案页漏掉的空位，再看同一份答案卷的**题目页**有没有
        # "写在空位上方"的排版文本（答案页本身是列表，不适用这条规则）。
        # 这条规则依赖版面位置，只在 override 明确开启的卷上使用，
        # 避免在普通题目页上把上一行题干误当成答案。
        typed_pages = {info["page"] for info in fill_all.values()}
        overrides = item.get("_overrides") or {}
        apply_code_regions(
            pages, code_all, overrides.get("code_regions") or {}, C.sha256_file(pdf_path)
        )
        label_fixes = apply_fill_label_fixes(
            fill_all, all_fill_problems, overrides.get("fill_label_fixes") or []
        )
        fill_inline = (
            {
                number: info
                for number, info in inline_fill_answers(
                    [page for page in fill_answer_pages if page.index not in typed_pages]
                ).items()
                if number not in fill_all
            }
            if overrides.get("inline_fill")
            else {}
        )
        if fill_inline:
            for issue in issues:
                if issue.get("kind") != "fill_answer_number_gap":
                    continue
                middle = range(int(issue["from"]) + 1, int(issue["to"]))
                filled = [number for number in middle if number in fill_inline]
                if filled:
                    issue["detail"] = (
                        f"{issue['detail']}其中第 {filled} 空已由答案版卷面补齐（见 fill-answers）。"
                    )
        for fix in label_fixes:
            for issue in issues:
                if issue.get("kind") != "fill_answer_number_gap":
                    continue
                if int(issue["from"]) < fix["canonical"] < int(issue["to"]):
                    issue["detail"] = (
                        f"{issue['detail']}第 {fix['canonical']} 空的原卷标号为 {fix['printed']}，"
                        f"已按导师核对结果归位（见 label_corrections）。"
                    )

        records: list[dict[str, Any]] = []
        annotated_copies = {
            copy.label
            for copy in copies
            if any(line.red for page in copy.pages for line in page.lines)
        }
        answer_copies = {copy.label for copy in copies if copy.answer_oriented}

        for (section, number), raws in sorted(grouped.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            primary = choose_primary_raw(raws, annotated_copies, answer_copies)
            content = question_text(primary)
            if section == C.SECTION_CHOICE:
                content = apply_choice_option_fix(item["id"], number, content)
            duplicated = [raw.copy_label for raw in raws if raw is not primary]
            pages_of_statement = sorted({page for raw in raws for page in raw.page_span})

            sources: list[C.AnswerSource] = []
            if section == C.SECTION_CHOICE:
                # 导师目视核对的选项优先；几何解码只作为证据保留
                if number in gold_choice:
                    sources.append(
                        C.AnswerSource(
                            kind="mentor_visual",
                            text=gold_choice[number],
                            pages=[gold_choice_page] if gold_choice_page else [],
                            detail="第十八节 18.4 视觉核对值（原卷答题纸逐题读取）",
                            pairing_verified=True,
                        )
                    )
                elif number in grid_all:
                    info = grid_all[number]
                    sources.append(
                        C.AnswerSource(
                            kind="grid_mark",
                            text=info["label"],
                            pages=[info["page"]],
                            detail=f"涂黑格墨迹 {info['ink']}（阈值法，次高 {info['runner_up']}）",
                            pairing_verified=grid_verified,
                        )
                    )
                elif number in typed_choice_all:
                    info = typed_choice_all[number]
                    sources.append(
                        C.AnswerSource(
                            kind="typed_list",
                            text=info["label"],
                            pages=[info["page"]],
                            detail="打字答案清单",
                            pairing_verified=True,
                        )
                    )
                if number in red_all:
                    info = red_all[number]
                    # 单独一个红字字母也可能是在标注"某个选项"而不是答案，
                    # 只有在没有其他载体时才以"待复核"的形式保留。
                    if not sources:
                        sources.append(
                            C.AnswerSource(
                                kind="red_annotation",
                                text=info["label"],
                                pages=[info["page"]],
                                detail=f"红字批注（可能标注的是选项而非答案）：{info['excerpt']}",
                                pairing_verified=info.get("style") == "paren",
                            )
                        )
            elif section == C.SECTION_FILL:
                marks = C.blank_marks(content["statement"])
                if not marks:
                    # 空位标记可能印在小题标题上一行；18.3 已给出小题→空号映射的卷用它
                    marks = list(blank_map.get(int(number), []))
                covered = [mark for mark in marks if mark in fill_all]
                inline_marks = [
                    mark for mark in marks if mark not in fill_all and mark in fill_inline
                ]
                if marks and covered:
                    parts = [f"({mark}) {fill_all[mark]['text']}" for mark in covered]
                    parts += [f"({mark}) {fill_inline[mark]['text']}" for mark in inline_marks]
                    text = " ｜ ".join(parts)
                    answer_pages = sorted(
                        {fill_all[mark]["page"] for mark in covered}
                        | {fill_inline[mark]["page"] for mark in inline_marks}
                    )
                    complete = len(covered) + len(inline_marks) == len(marks)
                    detail = f"答案页空号 {covered}/{marks}"
                    if inline_marks:
                        detail += f"；{inline_marks} 取自空位上方的排版文本"
                    fixed_marks = [mark for mark in covered if fill_all[mark].get("printed_label")]
                    for mark in fixed_marks:
                        detail += (
                            f"；空号 {fill_all[mark]['printed_label']}→{mark} 由导师核对卷面后校正"
                        )
                    mentor_marks = [
                        mark
                        for mark in covered
                        if fill_all[mark].get("source_kind") == "mentor_visual_transcription"
                    ]
                    if mentor_marks:
                        detail += f"；第 {mentor_marks} 空为导师视觉校录（18.5）"
                    typing = (
                        "mentor_visual_transcription"
                        if mentor_marks and len(mentor_marks) == len(covered)
                        else "typed_list"
                    )
                    sources.append(
                        C.AnswerSource(
                            kind=typing,
                            text=text,
                            pages=answer_pages,
                            detail=detail,
                            pairing_verified=complete,
                        )
                    )
                if number in fill_embedded and not sources:
                    info = fill_embedded[number]
                    sources.append(
                        C.AnswerSource(
                            kind="layout_text",
                            text=info["text"],
                            pages=pages_of_statement,
                            detail=info["detail"],
                            pairing_verified=False,
                        )
                    )
                if not sources and has_annotation_copy:
                    sources.append(
                        C.AnswerSource(
                            kind="layout_text",
                            text="",
                            pages=pages_of_statement,
                            detail="本卷含红字解析副本，但未能定位到本题答案；按待复核处理，不记为缺失。",
                            pairing_verified=False,
                        )
                    )
            elif section == C.SECTION_CODE:
                if number in code_all:
                    info = code_all[number]
                    similarity = _similar(content["statement"], info["statement"])
                    # 代码答案页往往只有代码、没有重复题干，相似度会偏低；
                    # 若答案页题号与题面题号完全对上，按结构证据认为配对成立。
                    code_paired = similarity >= 0.6 or code_verified
                    code_pages = list(info.get("pages") or [info["page"]])
                    sources.append(
                        C.AnswerSource(
                            kind="code_block",
                            text="[参考代码]",
                            pages=code_pages,
                            detail=(
                                f"答案副本中的参考代码；题面相似度 {similarity:.2f}"
                                + ("，题号集合一致" if code_verified else "")
                            ),
                            pairing_verified=code_paired,
                        )
                    )

            # 卷头年级/专业不同只记录，不据此降级：配对是否成立由"题号集合一致、
            # 空号全覆盖、代码题面相似度"这些结构证据判断，不由卷头文字判断。
            header_notes = sorted(
                {
                    note
                    for source in sources
                    if (note := cohort_mismatch(page_headers, pages_of_statement, source.pages))
                }
            )

            state, answer_text, note = C.resolve_answer_state(sources)

            record_marks = (
                marks if section == C.SECTION_FILL and marks else C.blank_marks(content["statement"])
            )
            record: dict[str, Any] = {
                "question_id": f"{item['id']}-{section}-{number:02d}",
                "paper_id": item["id"],
                "course": item["course"],
                "section": {"code": section, "title": C.SECTION_TITLES[section]},
                "number_label": str(number),
                "question_type": section,
                "statement": content["statement"],
                "options": content["options"],
                "code_lines": content["code_lines"],
                "blank_marks": record_marks,
                                "pages": {
                    # 18.3 第 8 条：题面页只写实际选中的干净页；其它副本的页
                    # 作为证据另存，避免把答案/解析页当成题面页
                    "statement": sorted(primary.page_span),
                    "statement_all": pages_of_statement,
                    "answer": sorted({p for s in sources for p in s.pages}),
                },
                "answer": {
                    "state": state,
                    "text": answer_text,
                    "note": note,
                    "origin": "paper",
                    "label": "原卷参考答案",
                    "sources": [source.as_dict() for source in sources],
                },
                # 导师独立解答（或后续补充的讲解）单独存放，永远不写进上面的
                # "原卷参考答案"字段，避免把补充答案当成原卷答案。
                "independent_solutions": [],
                "label_corrections": [
                    {
                        "printed": fix["printed"],
                        "canonical": fix["canonical"],
                        "page": fix["page"],
                        "text": fix["text"],
                        "basis": fix["basis"],
                        "evidence_pages": fix["evidence_pages"],
                        "author": "mentor",
                    }
                    for fix in label_fixes
                    if fix["canonical"] in record_marks
                ],
                "header_notes": header_notes,
                "copies": {
                    "primary": primary.copy_label,
                    "all": sorted({raw.copy_label for raw in raws}),
                },
                "duplicates": duplicated,
                "checks": {
                    "text_layer": bool(content["statement"].strip()),
                    "options_complete": (len(content["options"]) == 4) if section == C.SECTION_CHOICE else None,
                    "visual": "not_reviewed",
                },
                "review": {"automated": "pass", "visual": "not_reviewed", "approved": False},
            }
            if section == C.SECTION_CODE and number in code_all:
                record["reference_code"] = code_all[number]["code"]
                record["code_blocks"] = code_all[number].get("blocks", [])
                record["reference_code_origin"] = "paper"
                if "layout" in code_all[number]:
                    record["code_layout"] = code_all[number]["layout"]
                    record["code_annotations"] = code_all[number]["annotations"]
                if code_all[number].get("examples"):
                    record["code_examples"] = code_all[number]["examples"]
                record["reference_code_pages"] = list(
                    code_all[number].get("pages") or [code_all[number]["page"]]
                )

            problems: list[str] = []
            if not record["statement"].strip():
                problems.append("题干为空")
            if section == C.SECTION_CHOICE:
                labels = [str(option.get("label")) for option in content["options"]]
                if len(content["options"]) != 4:
                    problems.append(f"选项数为 {len(content['options'])}")
                elif labels != ["A", "B", "C", "D"]:
                    problems.append(f"选项标签异常：{','.join(labels)}")
            if section == C.SECTION_FILL and not record["blank_marks"]:
                problems.append("未识别到空位编号")
            if section == C.SECTION_CODE and not record["code_lines"]:
                problems.append("题干中未识别到代码行")
            if state == C.ANSWER_PENDING:
                problems.append("答案载体存在但对应关系待确认")
            if state == C.ANSWER_CONFLICT:
                problems.append("原卷答案证据互相矛盾")
            if problems:
                record["checks"]["automated"] = "warn"
                record["review"]["automated"] = "warn"
                record["checks"]["problems"] = problems
            records.append(record)

        # 18.6/18.7：公式转写与图像区域按题目挂接，保留原页与出处
        if mentor is not None and hasattr(mentor, "formulas_for"):
            formula_map = mentor.formulas_for(item["id"])
            for record in records:
                label = f"{record['section']['code']}-{int(record['number_label']):02d}"
                if label in formula_map:
                    record["formulas"] = [formula_map[label]]
            figure_dir = crops_dir / "assets" if crops_dir is not None else None
            for figure in mentor.figures_for(item["id"]):
                box = figure.get("bbox") or []
                page_no = int(figure.get("page") or 0)
                label = str(figure.get("question") or "")
                asset = {
                    "kind": "figure",
                    "page": page_no,
                    "bbox": box,
                    "description": figure.get("description"),
                    "transcription": figure.get("transcription"),
                    "source": "mentor_visual_region",
                    "doc_sha256": mentor.doc_sha256,
                }
                if figure_dir is not None and len(box) == 4 and 1 <= page_no <= document.page_count:
                    target = figure_dir / f"p{page_no:02d}-{label}.png"
                    clip = pymupdf.Rect(*box) & document[page_no - 1].rect
                    if not clip.is_empty:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        document[page_no - 1].get_pixmap(
                            matrix=pymupdf.Matrix(2.0, 2.0), clip=clip
                        ).save(target)
                        asset["crop"] = f"{item['id']}/assets/{target.name}"
                for record in records:
                    if record["question_id"].endswith(label):
                        record.setdefault("assets", []).append(asset)
                        break

        # 填空答案单独按"空号"落库，避免与小题号混用
        fill_records: dict[int, dict[str, Any]] = {}
        for number in sorted(fill_all):
            info = fill_all[number]
            fill_records[number] = {
                "blank": number,
                "text": info["text"],
                "page": info["page"],
                "pairing_verified": fill_verified,
                "origin": "paper",
                "printed_label": info.get("printed_label"),
                "label_source": info.get("label_source"),
                "label_basis": info.get("label_basis"),
            }

        # 复核裁片：按"副本 + 物理页"分别保存，跨页题会有多张图
        if crops_dir is not None:
            crops_dir.mkdir(parents=True, exist_ok=True)
            crop_sources: dict[str, tuple[str, list[Line]]] = {}
            code_sources: dict[str, tuple[str, list[dict[str, Any]]]] = {}
            for (section, number), raws in grouped.items():
                question_id = f"{item['id']}-{section}-{number:02d}"
                primary_raw = choose_primary_raw(raws, annotated_copies, answer_copies)
                crop_sources[question_id] = (primary_raw.copy_label, list(primary_raw.lines))
                if section == C.SECTION_CODE and number in code_all:
                    info = code_all[number]
                    code_sources[question_id] = (info.get("copy", ""), info.get("boxes", []))
            render_crops(document, pages, copies, records, crops_dir, crop_sources, code_sources)

        summary = {
            "paper_id": item["id"],
            "title": item["title"],
            "pdf": str(pdf_path),
            "pdf_sha256": C.sha256_file(pdf_path),
            "pages": document.page_count,
            "copies": per_copy,
            "questions": len(records),
            "by_section": dict(
                Counter(record["section"]["code"] for record in records)
            ),
            "answer_states": dict(Counter(record["answer"]["state"] for record in records)),
            "grid": grid_info,
            "grid_verified": grid_verified,
            "fill_answers": len(fill_records),
            "fill_verified": fill_verified,
            "fill_inline_answers": sorted(fill_inline),
            "overrides": sorted(
                key
                for key in (item.get("_overrides") or {})
                if not key.startswith("_") and key != "reason"
            ),
            "tool": TOOL_VERSION,
        }
        return records, {"summary": summary, "fill_answers": fill_records}, issues
    finally:
        document.close()


def render_crops(
    document: Any,
    pages: Sequence[PageData],
    copies: Sequence[Copy],
    records: Sequence[dict[str, Any]],
    out_dir: Path,
    crop_sources: dict[str, tuple[str, list[Line]]],
    code_sources: dict[str, tuple[str, list[dict[str, Any]]]] | None = None,
) -> None:
    """按"副本 + 物理页"保存题干区域、答案页整页与代码区裁片。

    跨页题在每一页各出一张图（文件名带页号），不把两页拼成一张；代码题额外
    按块裁图，便于核对函数体与主程序各自的位置。
    """

    page_index = {page.index: page for page in pages}

    def save(page_number: int, box: Sequence[float] | None, target: Path, zoom: float = 2.2) -> bool:
        pdf_page = document[page_number - 1]
        clip = pdf_page.rect if box is None else pymupdf.Rect(*box) & pdf_page.rect
        if clip.is_empty:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip).save(target)
        return True

    for record in records:
        question_id = record["question_id"]
        label, lines = crop_sources.get(question_id, ("", []))
        if not lines:
            continue
        by_page: dict[int, list[Line]] = defaultdict(list)
        for line in lines:
            by_page[line.page].append(line)
        for page_number, page_lines in sorted(by_page.items()):
            box = (
                min(line.bbox[0] for line in page_lines) - 4,
                min(line.bbox[1] for line in page_lines) - 4,
                max(line.bbox[2] for line in page_lines) + 4,
                max(line.bbox[3] for line in page_lines) + 4,
            )
            save(
                page_number,
                box,
                out_dir / label / f"p{page_number:02d}-{question_id}.png",
            )

        # 编程题：代码块单独裁一次，文件名标明块类型与序号。
        # 代码在答案副本里，因此按 code_sources 的位置裁，而不是题干副本。
        label_for_code, boxes = (code_sources or {}).get(question_id, ("", []))
        for position, block in enumerate(record.get("code_blocks") or [], start=1):
            if block.get("extraction") == "reviewed_region":
                # 相同的 k=0、print() 会在多种方法里重复，按文字匹配会跨栏误裁。
                save(
                    block["page"], block["bbox"],
                    out_dir / (label_for_code or label)
                    / f"p{block['page']:02d}-{question_id}-region-{block['block_id']}.png",
                )
                continue
            wanted = [line.strip() for line in block["text"].splitlines() if line.strip()]
            # 跨页块按页各裁一张，文件名带页号，不把两页拼成一张
            for page_number in sorted(block.get("pages") or [block["page"]]):
                block_boxes = [
                    box
                    for box in boxes
                    if box["page"] == page_number and box["text"].strip() in wanted
                ]
                if not block_boxes:
                    continue
                box = (
                    min(item["bbox"][0] for item in block_boxes) - 4,
                    min(item["bbox"][1] for item in block_boxes) - 4,
                    max(item["bbox"][2] for item in block_boxes) + 4,
                    max(item["bbox"][3] for item in block_boxes) + 4,
                )
                method = f"-m{block['method']}" if block.get("method") else ""
                save(
                    page_number,
                    box,
                    out_dir / (label_for_code or label)
                    / f"p{page_number:02d}-{question_id}-code{position}{method}"
                    f"-{block['kind']}.png",
                )

        # 18.7：题目自带的示例输入输出单独裁片，不与参考代码混在一起
        for position, example in enumerate(record.get("code_examples") or [], start=1):
            save(
                example["page"],
                example.get("bbox"),
                out_dir / (label_for_code or label)
                / f"p{example['page']:02d}-{question_id}-example{position}.png",
            )

    # 每个答案页整页图（含涂黑格），供人工确认"确实涂了哪一格"
    seen: set[int] = set()
    for record in records:
        for page_number in record["pages"]["answer"]:
            if page_number in seen or page_number not in page_index:
                continue
            seen.add(page_number)
            save(page_number, None, out_dir / "answer-pages" / f"answer-p{page_number:02d}.png", zoom=1.4)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从原始 PDF 抽取题目与答案候选")
    parser.add_argument("--papers", default="", help="逗号分隔的试卷 ID；留空表示该课程全部")
    parser.add_argument("--source", default=str(C.SOURCE_ROOT / "sample"))
    parser.add_argument("--work", default=str(C.WORK_ROOT / "sample"))
    parser.add_argument("--crops", default=str(C.REVIEW_ROOT / "sample" / "crops"))
    parser.add_argument("--paper-root", default=str(C.PAPER_ROOT))
    parser.add_argument("--catalog", default=str(C.CATALOG_PATH))
    parser.add_argument(
        "--overrides",
        default=str(C.QB_ROOT / "overrides.json"),
        help="逐卷人工覆盖项（不修改原始资料目录）；文件不存在时按默认规则处理",
    )
    args = parser.parse_args(argv)

    source_dir = Path(args.source)
    work_dir = Path(args.work)
    crops_dir = Path(args.crops)
    logger = C.get_logger("qb.extract", work_dir / "extract.log")

    ids = [part.strip() for part in args.papers.split(",") if part.strip()]
    catalog = C.load_catalog(Path(args.catalog))
    papers = C.select_papers(catalog, ids)
    overrides_path = Path(args.overrides)
    overrides = C.read_json(overrides_path) if overrides_path.exists() else {}
    if overrides:
        logger.info("读取逐卷覆盖项 %s：%s", C.relative(overrides_path), sorted(overrides))
    mentor_reader = None
    try:
        from tools.question_bank import mentor as mentor_module

        mentor_data = mentor_module.load()
        problems = mentor_module.validate(mentor_data, catalog)
        if problems:
            raise SystemExit(f"第十八节校录数据校验未通过：{problems[:3]}")
        mentor_reader = mentor_data
        logger.info(
            "读取第十八节校录：%d 卷、公式 %d 条、图像 %d 处",
            len(mentor_data.profiles),
            len(mentor_data.formulas),
            len(mentor_data.figures),
        )
    except ModuleNotFoundError:
        logger.warning("未找到 mentor 模块，按纯规则抽取")
    for item in papers:
        item["_overrides"] = overrides.get(item["id"], {})

    logger.info("开始抽取 %d 卷：%s", len(papers), ", ".join(item["id"] for item in papers))
    logger.info("结构化候选写入 %s", C.relative(source_dir))
    logger.info("中间产物写入 %s", C.relative(work_dir))
    logger.info("复核裁片写入 %s", C.relative(crops_dir))

    manifest_papers: list[dict[str, Any]] = []
    all_issues: list[dict[str, Any]] = []
    for item in papers:
        pdf_path = C.paper_pdf_path(item, Path(args.paper_root))
        if not pdf_path.exists():
            logger.error("缺文件：%s", pdf_path)
            all_issues.append({"paper_id": item["id"], "kind": "pdf_missing", "detail": str(pdf_path)})
            continue
        profile = mentor_reader.profile(item["id"]) if mentor_reader else None
        records, extra, issues = extract_paper(
            item,
            pdf_path=pdf_path,
            crops_dir=crops_dir / item["id"],
            profile=profile,
            mentor=mentor_reader,
        )
        target = source_dir / f"{item['id']}.jsonl"
        C.write_jsonl(target, records)
        fill_path = source_dir / f"{item['id']}.fill-answers.json"
        C.write_json(fill_path, extra["fill_answers"])
        summary = extra["summary"]
        manifest_papers.append(summary)
        for issue in issues:
            all_issues.append({"paper_id": item["id"], **issue})
        logger.info(
            "%s：%d 题，%s，答案状态 %s",
            item["id"],
            summary["questions"],
            summary["by_section"],
            summary["answer_states"],
        )

    manifest = {
        "tool": TOOL_VERSION,
        "generated_at": C.now_iso(),
        "course": C.COURSE,
        "papers": manifest_papers,
        "issues": all_issues,
        "totals": {
            "papers": len(manifest_papers),
            "questions": sum(paper["questions"] for paper in manifest_papers),
            "answer_states": dict(
                Counter(
                    state
                    for paper in manifest_papers
                    for state, count in paper["answer_states"].items()
                    for _ in range(count)
                )
            ),
        },
    }
    if mentor_reader is not None:
        expected = mentor_reader.totals()
        rows = []
        for paper in manifest_papers:
            want = mentor_reader.expected(paper["paper_id"]) or {}
            got = paper["by_section"]
            rows.append(
                {
                    "paper_id": paper["paper_id"],
                    "expected": want,
                    "actual": got,
                    "questions": paper["questions"],
                    "answer_states": paper["answer_states"],
                    "match": (got.get("choice"), got.get("fill"), got.get("code"))
                    == (want.get("choice"), want.get("fill"), want.get("code")),
                }
            )
        manifest["expected_totals"] = expected
        manifest["reconciliation"] = {
            "papers": len(rows),
            "questions": sum(row["questions"] for row in rows),
            "expected_questions": expected["questions"],
            "by_section": dict(
                Counter(
                    (section, key)
                    for paper in manifest_papers
                    for key, count in paper["by_section"].items()
                    for section in [key]
                    for _ in range(0)
                )
            ),
            "mismatched_papers": [row["paper_id"] for row in rows if not row["match"]],
            "rows": rows,
            "mentor_doc": {"path": C.relative(mentor_reader.doc_path), "sha256": mentor_reader.doc_sha256},
        }
        manifest["reconciliation"]["by_section"] = {
            key: sum(paper["by_section"].get(key, 0) for paper in manifest_papers)
            for key in ("choice", "fill", "code")
        }
    C.write_json(source_dir / "manifest.json", manifest)
    C.write_json(work_dir / "issues.json", all_issues)
    logger.info("完成：%d 卷 / %d 题", manifest["totals"]["papers"], manifest["totals"]["questions"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
