"""F007 多学科拓展：基于视觉多模态模型的理工科试卷逐页抽取、配对与题图裁切工具。

用途：
- 面向纯扫描、图文混合或含复杂数学/物理公式与示意图的 PDF 试卷
  （高等数学、几何与线性代数、大学物理 2020-2021 学年起共 20 份）；
- 复用 ``tools.question_bank.vision.VisionClient``，每页结果落盘缓存以支持断点续跑；
- 产出的 JSONL 与 manifest.json 严格遵守 ``tools.question_bank.build`` 契约，
  可直接与既有 Python 20 份卷合并构建统一的多学科不可变 build。

用法：
    python -m tools.question_bank.vision_extract --papers math-bi-20250116-final
    python -m tools.question_bank.vision_extract --all --workers 3
    python -m tools.question_bank.vision_extract --compile-only
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf
from PIL import Image

from app.question_bank.config import DEFAULT_BANK_ROOT, DEFAULT_PAPERS_ROOT
from app.question_bank.models import QUESTION_TYPES
from app.question_bank.paths import BankPaths, atomic_write_json
from tools.question_bank.vision import VisionCall, VisionClient, VisionError

TARGET_COURSES = ("高等数学", "几何与线性代数", "大学物理")
MIN_ACADEMIC_YEAR = "2020-2021"
SOURCE_SUBDIR = "source/ext-2020plus"
INGEST_SUBDIR = "ingest/ext-2020plus"
REVIEW_SUBDIR = "review/ext-2020plus"

VALID_EXT_TYPES = frozenset({"choice", "fill", "solve", "proof", "judge"})

PAGE_EXTRACTION_PROMPT = """你是一位严谨的高校理工科试卷数字化专家。请仔细阅读这张大学试卷页面图片（课程：{course}，试卷名：《{title}》，PDF 物理第 {page} 页，共 {total_pages} 页），将其中的所有试题或参考答案完整、忠实地提取为 JSON。

严格要求：
1. 必须只返回一个合法 JSON 对象，不要包含额外解释文字。
2. 所有数学与物理公式必须转写为标准 LaTeX：
   - 行内公式用单美元 `$ ... $`（例如 `$\\lim_{{x \\to 0}} \\frac{{\\sin x}}{{x}} = 1$`、`$A^{{-1}}$`、`$^\\circ$`、`$\\vec{{E}}$`）；
   - 矩阵、行列式、分段函数、多行方程组使用标准 LaTeX 环境（如 `$\\begin{{pmatrix}} 1 & 2 \\\\ 3 & 4 \\end{{pmatrix}}$`、`$\\begin{{vmatrix}} a & b \\\\ c & d \\end{{vmatrix}}$`），在 JSON 字符串里注意双反斜杠 `\\\\`。
   - 保持所有上标、下标、积分上下限、求导符号、向量箭头、物理单位与原卷完全一致，不得省略或改写数值。
3. 准确判断本页角色 `page_role`：
   - `"questions"`：试题页（含题目，不含集中参考答案）
   - `"answers"`：参考答案 / 评分标准页
   - `"mixed"`：同一页内既有试题，又直接在每题下方或题干旁印有参考答案/解答（或上部分试题、下部分答案）
   - `"answer_sheet"`：纯空白答题纸/答题卡/草稿区（没有完整题目也没有参考答案）
   - `"cover"`：封面或纯考场须知
4. 题目提取规则（存入 `questions` 数组）：
   - `section_code`：原卷大题编号（如 `"一"`、`"二"`、`"三"`、`"四"`；若本页承接上一页大题且本页未印大题标题，写 `""`）
   - `section_title`：原卷大题完整标题（如 `"一、单项选择题（每小题 3 分，共 15 分）"`；若本页未出现写 `""`）
   - `question_type`：必须严格取以下 5 个值之一：
     * `"choice"`：选择题（单选/多选）
     * `"fill"`：填空题
     * `"solve"`：计算题、解答题、应用题、综合题、简答题、作图计算题
     * `"proof"`：证明题
     * `"judge"`：判断题、是非题
   - `number_label`：纯阿拉伯数字题号字符串（如 `"1"`、`"2"`、`"12"`；如果该段内容是上一页某题的续写且本页顶部没有新题号，设 `is_continuation: true` 并将 `number_label` 设为空串 `""`）。
   - `statement`：完整题干（包含所有小问 (1)、(2)...）。填空题的横线/括号用 `______` 表示。**绝对不要把原卷印刷或手写标注的答案混入 `statement` 或 `options` 中！**
   - `options`：仅选择题填写，格式为 `[{{"label": "A", "text": "..."}}, {{"label": "B", "text": "..."}}, {{"label": "C", "text": "..."}}, {{"label": "D", "text": "..."}}]`；非选择题为空数组 `[]`。
   - `inline_answer`：若答案直接印在本题括号/下方/旁注里，把原答案完整提取到 `inline_answer`（字符串）；若本页无答案，设为 `null`。**严禁自己解题编造答案！**
   - `has_figure`：布尔值。仅当本题配有必须看图才能理解的示意图（如物理受力图、电路图、光路图、坐标曲线图、几何示意图等）时为 `true`；纯数学公式/矩阵/表格不要算作 `has_figure`。
   - `figures`：当 `has_figure` 为 `true` 时，给出每张图的对象数组：`[{{"description": "简短中文图注", "bbox_norm": [x0, y0, x1, y1]}}]`，坐标为相对整页宽高的 0~1000 整数（左、上、右、下，略微向外留白 10~20 像素确保包住坐标轴和图号）；无图时为 `[]`。
5. 参考答案提取规则（存入 `answers` 数组，仅当本页是参考答案页或底部附有集中答案时填写）：
   - 每条包含：`section_code`、`section_title`、`question_type`（同上 5 值之一，不确定时填 `""`）、`number_label`（纯数字字符串；若承接上一页某题的解答过程则设 `is_continuation: true`）、`answer_text`（忠实转写原答案与关键步骤，公式用 LaTeX）。
   - **严禁自己推算补全原卷没给出的答案**；本页没有参考答案时 `answers` 必须为空数组 `[]`。

返回 JSON 结构严格如下：
{{
  "page_role": "questions | answers | mixed | answer_sheet | cover",
  "page_notes": "可选：如扫描模糊、缺页或手写批注说明",
  "questions": [
    {{
      "section_code": "一",
      "section_title": "一、选择题",
      "question_type": "choice",
      "number_label": "1",
      "is_continuation": false,
      "statement": "题干内容...",
      "options": [{{"label": "A", "text": "..."}}, {{"label": "B", "text": "..."}}, {{"label": "C", "text": "..."}}, {{"label": "D", "text": "..."}}],
      "inline_answer": null,
      "has_figure": false,
      "figures": []
    }}
  ],
  "answers": [
    {{
      "section_code": "一",
      "section_title": "一、选择题",
      "question_type": "choice",
      "number_label": "1",
      "is_continuation": false,
      "answer_text": "B"
    }}
  ]
}}
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_target_papers(
    papers_root: Path = DEFAULT_PAPERS_ROOT,
    paper_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    catalog = json.loads((papers_root / "catalog.json").read_text(encoding="utf-8"))
    selected = [
        row
        for row in catalog
        if row.get("course") in TARGET_COURSES
        and str(row.get("academic_year") or "") >= MIN_ACADEMIC_YEAR
    ]
    if paper_ids:
        wanted = set(paper_ids)
        selected = [row for row in selected if row["id"] in wanted]
        found = {row["id"] for row in selected}
        missing = sorted(wanted - found)
        if missing:
            raise ValueError(f"指定的试卷不在目标清单中：{missing}")
    return selected


def render_paper_pages(
    pdf_path: Path,
    out_dir: Path,
    *,
    dpi: int = 180,
    max_dim: int = 2600,
) -> tuple[list[Path], str, int]:
    """逐页渲染 PDF 为 PNG（限制最大边长防止超限）；同尺寸已存在则复用。"""

    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_sha = _sha_file(pdf_path)
    doc = pymupdf.open(pdf_path)
    paths: list[Path] = []
    try:
        total = len(doc)
        base_zoom = dpi / 72.0
        for idx in range(total):
            page_no = idx + 1
            png_path = out_dir / f"page-{page_no:02d}.png"
            need_render = not png_path.is_file()
            if not need_render:
                try:
                    with Image.open(png_path) as im:
                        if max(im.size) > max_dim + 200:
                            need_render = True
                except OSError:
                    need_render = True
            if need_render:
                page = doc[idx]
                longest = max(page.rect.width, page.rect.height, 1.0)
                zoom = min(base_zoom, max_dim / longest)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                pix.save(str(png_path))
            paths.append(png_path)
    finally:
        doc.close()
    return paths, pdf_sha, len(paths)


def _split_image_vertical(src: Path, out_dir: Path) -> tuple[Path, Path]:
    """当单页输出被 max_tokens 截断时，把页面按上下各 55% 重叠拆成两半重抽。"""

    pix = pymupdf.Pixmap(str(src))
    w, h = pix.width, pix.height
    mid_top = int(h * 0.56)
    mid_bot = int(h * 0.44)
    top_path = out_dir / f"{src.stem}-top.png"
    bot_path = out_dir / f"{src.stem}-bot.png"

    doc = pymupdf.open()
    try:
        p1 = doc.new_page(width=w, height=mid_top)
        p1.insert_image(pymupdf.Rect(0, 0, w, h), filename=str(src))
        p1.get_pixmap(clip=pymupdf.Rect(0, 0, w, mid_top), alpha=False).save(str(top_path))

        p2 = doc.new_page(width=w, height=h - mid_bot)
        p2.insert_image(pymupdf.Rect(0, -mid_bot, w, h - mid_bot), filename=str(src))
        p2.get_pixmap(clip=pymupdf.Rect(0, 0, w, h - mid_bot), alpha=False).save(str(bot_path))
    finally:
        doc.close()
    return top_path, bot_path


def extract_page_with_vision(
    client: VisionClient,
    paper: dict[str, Any],
    page_no: int,
    total_pages: int,
    png_path: Path,
    cache_path: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    if not force and cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("status") == "success" and isinstance(cached.get("parsed"), dict):
            return cached

    prompt = PAGE_EXTRACTION_PROMPT.format(
        course=paper["course"],
        title=paper["title"],
        page=page_no,
        total_pages=total_pages,
    )
    call: VisionCall = client.complete_json([png_path], prompt, max_tokens=8192)
    payload = call.to_dict()
    payload["paper_id"] = paper["id"]
    payload["page"] = page_no
    payload["split_fallback"] = False

    if call.status != "success" and call.error_code == "vision_truncated":
        # 截断回退：拆上下半页分别提取再合并
        top_img, bot_img = _split_image_vertical(png_path, png_path.parent)
        c_top = client.complete_json([top_img], prompt + "\n（注：本图是该页上半部分）", max_tokens=8192)
        c_bot = client.complete_json([bot_img], prompt + "\n（注：本图是该页下半部分）", max_tokens=8192)
        if c_top.status == "success" and c_bot.status == "success":
            p_top = c_top.parsed or {}
            p_bot = c_bot.parsed or {}
            merged_questions = list(p_top.get("questions") or [])
            seen_keys = {
                (q.get("question_type"), str(q.get("number_label") or ""), str(q.get("statement") or "")[:30])
                for q in merged_questions
            }
            for q in p_bot.get("questions") or []:
                key = (q.get("question_type"), str(q.get("number_label") or ""), str(q.get("statement") or "")[:30])
                if key not in seen_keys:
                    merged_questions.append(q)
                    seen_keys.add(key)
            merged_answers = list(p_top.get("answers") or [])
            seen_ans = {
                (a.get("question_type"), str(a.get("number_label") or ""), str(a.get("answer_text") or "")[:30])
                for a in merged_answers
            }
            for a in p_bot.get("answers") or []:
                key = (a.get("question_type"), str(a.get("number_label") or ""), str(a.get("answer_text") or "")[:30])
                if key not in seen_ans:
                    merged_answers.append(a)
                    seen_ans.add(key)
            payload["status"] = "success"
            payload["split_fallback"] = True
            payload["split_calls"] = [c_top.to_dict(), c_bot.to_dict()]
            payload["parsed"] = {
                "page_role": p_top.get("page_role") or p_bot.get("page_role") or "questions",
                "page_notes": " | ".join(
                    n for n in (p_top.get("page_notes"), p_bot.get("page_notes")) if n
                ),
                "questions": merged_questions,
                "answers": merged_answers,
            }
            payload["error_code"] = None
            payload["error_message"] = None

    atomic_write_json(cache_path, payload)
    return payload


def _normalize_qtype(raw: Any, section_title: str = "") -> str:
    val = str(raw or "").strip().lower()
    if val in VALID_EXT_TYPES:
        return val
    title = section_title or ""
    if any(k in title for k in ("选择", "单选", "多选")):
        return "choice"
    if "填空" in title:
        return "fill"
    if "判断" in title or "是非" in title:
        return "judge"
    if "证明" in title:
        return "proof"
    return "solve"


_CN_NUMS: dict[str, int] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
    "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
}


def _cn_or_int(raw: Any) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))
    cleaned = re.sub(r"[、.．()（）\s]", "", text)
    if cleaned in _CN_NUMS:
        return _CN_NUMS[cleaned]
    return None


def _normalize_number(raw: Any) -> str:
    val = _cn_or_int(raw)
    return str(val) if val is not None else ""


def _clean_section_code(raw: Any, fallback: str) -> str:
    text = str(raw or "").strip()
    if text:
        return text
    return fallback


def _refine_figure_bbox(page_png: Path, bbox_norm: list[float]) -> list[float]:
    """用页面墨迹投影对 VLM 粗框做局部对齐，防止图被裁半或误吞正文行。"""

    if not page_png.is_file():
        return bbox_norm
    try:
        arr = np.array(Image.open(page_png).convert("L"))
    except OSError:
        return bbox_norm
    h, w = arr.shape
    ink = arr < 215
    x0n, y0n, x1n, y1n = bbox_norm

    # 若原框上下边界没有被墨迹穿切，只允许小幅外扩；若被穿切（框偏移裁半），扩大搜索窗口
    vy0 = max(0, min(h - 1, int(y0n / 1000.0 * h)))
    vy1 = max(vy0 + 1, min(h, int(y1n / 1000.0 * h)))
    vx0 = max(0, min(w - 1, int(x0n / 1000.0 * w)))
    vx1 = max(vx0 + 1, min(w, int(x1n / 1000.0 * w)))

    def _band_has_ink(y_center: int) -> bool:
        lo = max(0, y_center - 4)
        hi = min(h, y_center + 5)
        return int(ink[lo:hi, vx0:vx1].sum()) > 12

    top_cut = _band_has_ink(vy0)
    bot_cut = _band_has_ink(vy1)

    pad_top = int(0.085 * h) if top_cut else int(0.020 * h)
    pad_bot = int(0.085 * h) if bot_cut else int(0.020 * h)
    sx0 = max(0, int((x0n - 140.0) / 1000.0 * w))
    sy0 = max(0, vy0 - pad_top)
    sx1 = min(w, int((x1n + 140.0) / 1000.0 * w))
    sy1 = min(h, vy1 + pad_bot)

    # 识别左起正文短行并屏蔽，避免跨进上一题/下一题的文字
    row_ink_full = ink.sum(axis=1)
    in_b = False
    st = 0
    text_mask = np.zeros_like(ink)
    for y in range(h):
        if row_ink_full[y] > 3 and not in_b:
            in_b = True
            st = y
        elif row_ink_full[y] <= 3 and in_b:
            in_b = False
            bh = y - st
            col_ink = ink[st:y].sum(axis=0)
            xs = np.where(col_ink > 0)[0]
            if len(xs):
                bx0, bx1 = int(xs[0]), int(xs[-1])
                if bh < int(0.032 * h) and bx0 < int(0.22 * w):
                    in_gap = False
                    gap_st = 0
                    split_x = None
                    for x in range(bx0, bx1 + 1):
                        if col_ink[x] == 0:
                            if not in_gap:
                                in_gap = True
                                gap_st = x
                        else:
                            if in_gap and (x - gap_st) >= int(0.03 * w) and gap_st > int(0.35 * w):
                                split_x = gap_st
                                break
                            in_gap = False
                    if split_x is not None and x0n > 450:
                        text_mask[st:y, :split_x] = True
                    else:
                        text_mask[st:y, :] = True

    clean = ink & (~text_mask)
    sub = clean[sy0:sy1, sx0:sx1]
    r_cnt = sub.sum(axis=1)
    clusters: list[tuple[int, int]] = []
    in_c = False
    cst = 0
    gap = 0
    for r in range(sub.shape[0]):
        if r_cnt[r] > 2:
            if not in_c:
                in_c = True
                cst = r
            gap = 0
        elif in_c:
            gap += 1
            if gap > 8:
                clusters.append((cst, r - gap + 1))
                in_c = False
    if in_c:
        clusters.append((cst, sub.shape[0]))
    if not clusters:
        return bbox_norm

    vy0_rel = vy0 - sy0
    vy1_rel = vy1 - sy0
    overlapping = [c for c in clusters if min(c[1], vy1_rel) - max(c[0], vy0_rel) > 0]
    pool = overlapping or clusters
    best = max(pool, key=lambda c: c[1] - c[0])
    ry0, ry1 = sy0 + best[0], sy0 + best[1]
    for c in clusters:
        if c == best:
            continue
        if (c[1] - c[0]) <= int(0.025 * h):
            if 0 <= best[0] - c[1] <= 18:
                ry0 = min(ry0, sy0 + c[0])
            elif 0 <= c[0] - best[1] <= 18:
                ry1 = max(ry1, sy0 + c[1])

    cols = np.where(clean[ry0:ry1, sx0:sx1].sum(axis=0) > 0)[0]
    if len(cols) == 0:
        return bbox_norm
    rx0, rx1 = sx0 + int(cols[0]), sx0 + int(cols[-1])
    pad = 8
    rx0 = max(0, rx0 - pad)
    ry0 = max(0, ry0 - pad)
    rx1 = min(w, rx1 + pad)
    ry1 = min(h, ry1 + pad)
    return [
        round(rx0 * 1000.0 / w, 1),
        round(ry0 * 1000.0 / h, 1),
        round(rx1 * 1000.0 / w, 1),
        round(ry1 * 1000.0 / h, 1),
    ]


def _crop_figure(
    pdf_path: Path,
    page_png: Path,
    page_no: int,
    bbox_norm: list[Any],
    target_path: Path,
    *,
    dpi: int = 200,
) -> list[float] | None:
    """按 0..1000 归一化坐标从原 PDF 裁切高分辨率题图，返回 PDF 点坐标 bbox。"""

    if not isinstance(bbox_norm, list) or len(bbox_norm) != 4:
        return None
    try:
        vals = [float(v) for v in bbox_norm]
    except (TypeError, ValueError):
        return None
    if max(vals) <= 1.0:
        vals = [v * 1000.0 for v in vals]
    vals = _refine_figure_bbox(page_png, vals)
    x0n, y0n, x1n, y1n = vals
    x0n = max(0.0, min(1000.0, x0n - 6.0))
    y0n = max(0.0, min(1000.0, y0n - 6.0))
    x1n = max(0.0, min(1000.0, x1n + 6.0))
    y1n = max(0.0, min(1000.0, y1n + 6.0))
    if x1n - x0n < 20.0 or y1n - y0n < 20.0:
        return None

    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_no - 1]
        rect = page.rect
        clip = pymupdf.Rect(
            rect.x0 + rect.width * (x0n / 1000.0),
            rect.y0 + rect.height * (y0n / 1000.0),
            rect.x0 + rect.width * (x1n / 1000.0),
            rect.y0 + rect.height * (y1n / 1000.0),
        )
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(target_path))
        return [round(clip.x0, 2), round(clip.y0, 2), round(clip.x1, 2), round(clip.y1, 2)]
    finally:
        doc.close()


def _check_latex_balance(text: str) -> list[str]:
    issues: list[str] = []
    if not text:
        return issues
    # 去掉 $$ 与 \$ 后统计单 $ 数量
    cleaned = text.replace(r"\$", "")
    if cleaned.count("$$") % 2 != 0:
        issues.append("display_dollar_unbalanced")
    cleaned_no_display = cleaned.replace("$$", "")
    if cleaned_no_display.count("$") % 2 != 0:
        issues.append("inline_dollar_unbalanced")
    begins = re.findall(r"\\begin\{([a-zA-Z*]+)\}", text)
    ends = re.findall(r"\\end\{([a-zA-Z*]+)\}", text)
    if len(begins) != len(ends):
        issues.append("latex_env_unbalanced")
    return issues


def _norm_options(raw_opts: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in raw_opts or []:
        if not isinstance(item, dict):
            continue
        lbl = str(item.get("label") or "").strip().upper().rstrip(".．、)）")
        txt = str(item.get("text") or "").strip()
        if lbl and txt:
            out.append({"label": lbl, "text": txt})
    return out


def _extract_inline_options(text: str) -> tuple[str, list[dict[str, str]]]:
    """从题干/续页文本中拆出形如 `(C) ...` / `（C）...` / `C. ...` 的选项。"""

    found: list[dict[str, str]] = []
    kept_lines: list[str] = []
    opt_pat = re.compile(r"^\s*(?:[（(]\s*([A-E])\s*[）)]|([A-E])[.．、)])\s*(.+?)\s*$")
    for line in text.splitlines():
        m = opt_pat.match(line)
        if m:
            lbl = (m.group(1) or m.group(2)).upper()
            txt = m.group(3).strip()
            if txt:
                found.append({"label": lbl, "text": txt})
                continue
        kept_lines.append(line)
    return "\n".join(kept_lines).strip(), found


def _unpack_compact_choice_answers(
    raw_answers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """展开整串选择题答案（如 'CBCCC CCBCB BD' -> 第 1..12 题逐题答案）。"""

    expanded: list[dict[str, Any]] = []
    for item in raw_answers:
        text = str(item.get("answer_text") or item.get("text") or "").strip()
        if (
            item.get("question_type") == "choice"
            and re.fullmatch(r"[A-Ea-e\s,，、.;；]+", text)
        ):
            letters = re.findall(r"[A-Ea-e]", text)
            if len(letters) >= 2:
                start_num = _cn_or_int(item.get("number_label")) or 1
                for offset, letter in enumerate(letters):
                    expanded.append(
                        {
                            "section_code": item.get("section_code") or "",
                            "section_title": item.get("section_title") or "",
                            "question_type": "choice",
                            "number_label": str(start_num + offset),
                            "text": letter.upper(),
                            "pages": list(item.get("pages") or []),
                        }
                    )
                continue
        expanded.append(item)
    return expanded


def _detect_paper_variant(title: str) -> str | None:
    m = re.search(r"[（(]\s*([AB])\s*卷\s*[）)]|\b([AB])\s*卷", title)
    if m:
        return (m.group(1) or m.group(2)).upper()
    return None


def compile_paper_records(
    paper: dict[str, Any],
    pdf_path: Path,
    pages_dir: Path,
    pdf_sha: str,
    total_pages: int,
    page_results: list[dict[str, Any]],
    crops_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把逐页视觉结果拼装为标准题库记录，完成跨页合并、题图裁切与答案配对。"""

    paper_id = paper["id"]
    course = paper["course"]
    paper_variant = _detect_paper_variant(str(paper.get("title") or ""))
    raw_questions: list[dict[str, Any]] = []
    raw_answers: list[dict[str, Any]] = []
    failed_pages: list[int] = []
    page_roles: dict[int, str] = {}

    last_section_code = ""
    last_section_title = ""
    last_qtype = ""

    for page_data in sorted(page_results, key=lambda item: int(item.get("page") or 0)):
        page_no = int(page_data.get("page") or 0)
        if page_data.get("status") != "success" or not isinstance(page_data.get("parsed"), dict):
            failed_pages.append(page_no)
            continue
        parsed = page_data["parsed"]
        role = str(parsed.get("page_role") or "questions")
        page_roles[page_no] = role

        for q in parsed.get("questions") or []:
            if not isinstance(q, dict):
                continue
            stmt = str(q.get("statement") or "").strip()
            norm_opts = _norm_options(q.get("options"))
            is_cont = bool(q.get("is_continuation"))
            num = _normalize_number(q.get("number_label"))
            sec_title = str(q.get("section_title") or "").strip()
            sec_code = str(q.get("section_code") or "").strip()
            if not is_cont and not num and sec_code:
                num = _normalize_number(sec_code)
            if sec_title:
                last_section_title = sec_title
            if sec_code:
                last_section_code = sec_code
            qtype = _normalize_qtype(q.get("question_type"), sec_title or last_section_title)
            if qtype:
                last_qtype = qtype

            if (is_cont or (not num and not sec_code)) and raw_questions:
                prev = raw_questions[-1]
                if norm_opts:
                    existing_labels = {opt["label"] for opt in prev["options"]}
                    for opt in norm_opts:
                        if opt["label"] not in existing_labels:
                            prev["options"].append(opt)
                            existing_labels.add(opt["label"])
                if prev["question_type"] == "choice" and stmt:
                    rest_stmt, parsed_opts = _extract_inline_options(stmt)
                    if parsed_opts:
                        existing_labels = {opt["label"] for opt in prev["options"]}
                        for opt in parsed_opts:
                            if opt["label"] not in existing_labels:
                                prev["options"].append(opt)
                                existing_labels.add(opt["label"])
                        stmt = rest_stmt
                # 若续页仅包含已在 options 里提取的选项，不再重复塞入 statement
                if stmt and not (norm_opts and re.match(r"^[（(]?[A-E][）).．、]", stmt)):
                    prev["statement"] = (prev["statement"] + "\n" + stmt).strip()
                if page_no not in prev["pages_statement"]:
                    prev["pages_statement"].append(page_no)
                if q.get("inline_answer"):
                    prev_ans = str(prev.get("inline_answer") or "").strip()
                    add_ans = str(q.get("inline_answer") or "").strip()
                    prev["inline_answer"] = (prev_ans + "\n" + add_ans).strip() if prev_ans else add_ans
                    if page_no not in prev["pages_answer"]:
                        prev["pages_answer"].append(page_no)
                if q.get("has_figure") and q.get("figures"):
                    for fig in q.get("figures") or []:
                        if isinstance(fig, dict):
                            prev["raw_figures"].append({**fig, "page": page_no})
                continue

            if not stmt and not norm_opts:
                continue
            if not num:
                num = str(len(raw_questions) + 1)

            inline_ans = str(q.get("inline_answer") or "").strip() or None
            raw_figs = []
            if q.get("has_figure") and isinstance(q.get("figures"), list):
                for fig in q["figures"]:
                    if isinstance(fig, dict):
                        raw_figs.append({**fig, "page": page_no})

            raw_questions.append(
                {
                    "section_code": _clean_section_code(sec_code, last_section_code or "一"),
                    "section_title": sec_title or last_section_title or f"{qtype}",
                    "question_type": qtype or last_qtype or "solve",
                    "number_label": num,
                    "statement": stmt,
                    "options": norm_opts,
                    "inline_answer": inline_ans,
                    "pages_statement": [page_no],
                    "pages_answer": [page_no] if inline_ans else [],
                    "raw_figures": raw_figs,
                }
            )

        last_ans_sec = ""
        last_ans_type = ""
        for a in parsed.get("answers") or []:
            if not isinstance(a, dict):
                continue
            ans_text = str(a.get("answer_text") or "").strip()
            if not ans_text:
                continue
            sec_title = str(a.get("section_title") or "").strip()
            ans_variant = _detect_paper_variant(sec_title)
            if paper_variant and ans_variant and ans_variant != paper_variant:
                # 忽略附在卷末的另一卷别（如 A 卷末尾附带的 B 卷简要答案）
                continue
            is_cont = bool(a.get("is_continuation"))
            num = _normalize_number(a.get("number_label"))
            sec_code = str(a.get("section_code") or "").strip()
            if not is_cont and not num and sec_code:
                num = _normalize_number(sec_code)
            if sec_code:
                last_ans_sec = sec_code
            qtype_raw = str(a.get("question_type") or "").strip().lower()
            qtype = qtype_raw if qtype_raw in VALID_EXT_TYPES else (
                _normalize_qtype("", sec_title) if sec_title else last_ans_type
            )
            if qtype:
                last_ans_type = qtype

            if (is_cont or (not num and not sec_code)) and raw_answers:
                prev_a = raw_answers[-1]
                prev_a["text"] = (prev_a["text"] + "\n" + ans_text).strip()
                if page_no not in prev_a["pages"]:
                    prev_a["pages"].append(page_no)
                continue

            if not num:
                continue
            raw_answers.append(
                {
                    "section_code": sec_code or last_ans_sec,
                    "section_title": sec_title,
                    "question_type": qtype,
                    "number_label": num,
                    "text": ans_text,
                    "pages": [page_no],
                }
            )

    # 展开类似 'CBCCC CCBCB BD' 的集中选择题答案串
    raw_answers = _unpack_compact_choice_answers(raw_answers)

    # 构造稳定 question_id 并去重防冲突
    used_ids: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    ans_by_sec_num: dict[tuple[str, str], list[dict[str, Any]]] = {}
    ans_by_sec: dict[str, list[dict[str, Any]]] = {}
    ans_by_type_num: dict[tuple[str, str], list[dict[str, Any]]] = {}
    ans_by_num: dict[str, list[dict[str, Any]]] = {}
    for a in raw_answers:
        if a["section_code"]:
            ans_by_sec.setdefault(a["section_code"], []).append(a)
            ans_by_sec_num.setdefault((a["section_code"], a["number_label"]), []).append(a)
        if a["question_type"]:
            ans_by_type_num.setdefault((a["question_type"], a["number_label"]), []).append(a)
        ans_by_num.setdefault(a["number_label"], []).append(a)

    q_num_freq: dict[str, int] = {}
    q_sec_freq: dict[str, int] = {}
    for q in raw_questions:
        q_num_freq[q["number_label"]] = q_num_freq.get(q["number_label"], 0) + 1
        if q["section_code"]:
            q_sec_freq[q["section_code"]] = q_sec_freq.get(q["section_code"], 0) + 1

    issues: list[dict[str, Any]] = []
    figures_cropped = 0
    figures_failed = 0

    for idx, q in enumerate(raw_questions, start=1):
        qtype = q["question_type"]
        num_int = int(q["number_label"])
        base_id = f"{paper_id}-{qtype}-{num_int:02d}"
        if base_id in used_ids:
            sec_int = _cn_or_int(q["section_code"])
            candidate = f"{paper_id}-s{sec_int:02d}-{qtype}-{num_int:02d}" if sec_int else ""
            if candidate and candidate not in used_ids:
                qid = candidate
                used_ids[qid] = 1
            else:
                used_ids[base_id] += 1
                qid = f"{base_id}-dup{used_ids[base_id]}"
                issues.append({"question_id": qid, "issue": f"duplicate_id_from_{base_id}"})
        else:
            used_ids[base_id] = 1
            qid = base_id

        # 答案配对优先级：
        # 1) inline_answer
        # 2) 同 section_code 下唯一一题（如“六、”整道大题含小问 (1)(2)），合并该 section 下全部答案块
        # 3) (section_code, number_label) 精确匹配未消耗答案
        # 4) (question_type, number_label) 匹配未消耗答案
        # 5) 全局唯一题号兜底
        ans_origin: str | None = None
        ans_pages = list(q["pages_answer"])
        ans_text = q["inline_answer"]
        if ans_text:
            ans_origin = "inline_answer"
        else:
            sec = q["section_code"]
            if sec and q_sec_freq.get(sec, 0) == 1:
                sec_cands = [c for c in (ans_by_sec.get(sec) or []) if not c.get("_used")]
                if sec_cands:
                    for c in sec_cands:
                        c["_used"] = True
                        ans_pages.extend(c["pages"])
                    ans_text = "\n\n".join(c["text"] for c in sec_cands)
                    ans_pages = sorted(set(ans_pages))
                    ans_origin = "paper_answer_page"

            if not ans_text:
                cands = [
                    c for c in (ans_by_sec_num.get((sec, q["number_label"])) or [])
                    if not c.get("_used")
                ] if sec else []
                if not cands:
                    cands = [
                        c for c in (ans_by_type_num.get((qtype, q["number_label"])) or [])
                        if not c.get("_used")
                    ]
                if not cands and q_num_freq.get(q["number_label"], 0) == 1:
                    cands = [
                        c for c in (ans_by_num.get(q["number_label"]) or [])
                        if not c.get("_used")
                    ]
                if cands:
                    matched_ans = cands[0]
                    matched_ans["_used"] = True
                    ans_text = matched_ans["text"]
                    ans_pages = sorted(set(ans_pages + matched_ans["pages"]))
                    ans_origin = "paper_answer_page"

        if ans_text:
            answer_obj: dict[str, Any] = {
                "state": "original",
                "text": ans_text,
                "origin": ans_origin,
                "sources": [
                    {
                        "kind": ans_origin or "paper_answer_page",
                        "text": ans_text,
                        "pairing_verified": True,
                        "pages": ans_pages,
                    }
                ],
            }
        else:
            answer_obj = {
                "state": "missing",
                "text": None,
                "origin": None,
                "sources": [],
            }

        # 裁切题图
        assets: list[dict[str, Any]] = []
        for f_idx, fig in enumerate(q["raw_figures"], start=1):
            fig_page = int(fig.get("page") or q["pages_statement"][0])
            page_png = pages_dir / f"page-{fig_page:02d}.png"
            crop_rel = f"{paper_id}/{qid}-fig{f_idx}.png"
            target_png = crops_root / crop_rel
            bbox_pts = _crop_figure(pdf_path, page_png, fig_page, fig.get("bbox_norm") or [], target_png)
            if bbox_pts is not None:
                figures_cropped += 1
                assets.append(
                    {
                        "kind": "figure",
                        "page": fig_page,
                        "bbox": bbox_pts,
                        "description": str(fig.get("description") or f"第 {q['number_label']} 题示意图").strip(),
                        "transcription": "",
                        "crop": crop_rel,
                    }
                )
            else:
                figures_failed += 1
                issues.append({"question_id": qid, "issue": f"figure_crop_failed_page_{fig_page}"})

        # 质量检查
        q_issues = _check_latex_balance(q["statement"])
        if qtype == "choice":
            labels = [opt["label"] for opt in q["options"]]
            if labels != ["A", "B", "C", "D"]:
                q_issues.append(f"choice_options_{','.join(labels) or 'empty'}")
        if not q["statement"]:
            q_issues.append("empty_statement")
        for item in q_issues:
            issues.append({"question_id": qid, "issue": item})

        record = {
            "question_id": qid,
            "paper_id": paper_id,
            "course": course,
            "section": {
                "code": q["section_code"],
                "title": q["section_title"],
            },
            "number_label": q["number_label"],
            "question_type": qtype,
            "statement": q["statement"],
            "options": q["options"],
            "code_lines": [],
            "blank_marks": [],
            "formulas": [],
            "assets": assets,
            "pages": {
                "statement": sorted(set(q["pages_statement"])),
                "answer": sorted(set(ans_pages)),
            },
            "answer": answer_obj,
            "checks": {
                "automated": "pass" if not q_issues else "warn",
                "issues": q_issues,
            },
            "review": {
                "visual": "vision_extracted",
                "approved": False,
            },
        }
        records.append(record)

    summary = {
        "paper_id": paper_id,
        "course": course,
        "title": paper["title"],
        "academic_year": paper.get("academic_year"),
        "exam_type": paper.get("exam_type"),
        "exam_date": paper.get("exam_date"),
        "pdf_sha256": pdf_sha,
        "pages": total_pages,
        "failed_pages": failed_pages,
        "page_roles": page_roles,
        "question_count": len(records),
        "by_type": {
            t: sum(1 for r in records if r["question_type"] == t)
            for t in QUESTION_TYPES
            if any(r["question_type"] == t for r in records)
        },
        "answered_count": sum(1 for r in records if r["answer"]["state"] == "original"),
        "missing_answer_count": sum(1 for r in records if r["answer"]["state"] == "missing"),
        "figures_cropped": figures_cropped,
        "figures_failed": figures_failed,
        "issue_count": len(issues),
        "issues": issues,
    }
    return records, summary


def run_extraction(
    *,
    bank_root: Path = DEFAULT_BANK_ROOT,
    papers_root: Path = DEFAULT_PAPERS_ROOT,
    paper_ids: list[str] | None = None,
    workers: int = 3,
    dpi: int = 180,
    force_vision: bool = False,
    compile_only: bool = False,
) -> dict[str, Any]:
    paths = BankPaths(bank_root)
    ingest_root = paths.root / INGEST_SUBDIR
    source_root = paths.root / SOURCE_SUBDIR
    review_root = paths.root / REVIEW_SUBDIR
    crops_root = review_root / "crops"
    source_root.mkdir(parents=True, exist_ok=True)
    review_root.mkdir(parents=True, exist_ok=True)

    targets = load_target_papers(papers_root, paper_ids)
    client = None if compile_only else VisionClient(timeout=180.0, max_attempts=3)

    manifest_papers: list[dict[str, Any]] = []
    all_summaries: list[dict[str, Any]] = []
    api_calls_new = 0
    api_calls_cached = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0

    for paper in targets:
        paper_id = paper["id"]
        pdf_path = papers_root / paper["file_path"]
        pages_dir = ingest_root / "pages" / paper_id
        vision_dir = ingest_root / "vision" / paper_id
        vision_dir.mkdir(parents=True, exist_ok=True)

        png_paths, pdf_sha, total_pages = render_paper_pages(pdf_path, pages_dir, dpi=dpi)
        print(f"[{paper['course']}] {paper_id} ({total_pages} 页) 开始处理...", flush=True)

        page_results: list[dict[str, Any]] = []
        if compile_only:
            for page_no in range(1, total_pages + 1):
                cache_path = vision_dir / f"page-{page_no:02d}.json"
                if cache_path.is_file():
                    page_results.append(json.loads(cache_path.read_text(encoding="utf-8")))
                else:
                    page_results.append({"page": page_no, "status": "missing"})
        else:
            assert client is not None

            def _job(page_no_and_path: tuple[int, Path]) -> tuple[dict[str, Any], bool]:
                pno, ppath = page_no_and_path
                cpath = vision_dir / f"page-{pno:02d}.json"
                was_cached = (not force_vision) and cpath.is_file() and (
                    json.loads(cpath.read_text(encoding="utf-8")).get("status") == "success"
                )
                res = extract_page_with_vision(
                    client, paper, pno, total_pages, ppath, cpath, force=force_vision
                )
                return res, was_cached

            jobs = [(idx + 1, p) for idx, p in enumerate(png_paths)]
            if workers <= 1:
                for job in jobs:
                    res, was_cached = _job(job)
                    page_results.append(res)
                    if was_cached:
                        api_calls_cached += 1
                    else:
                        api_calls_new += 1
                    usage = res.get("usage") or {}
                    total_prompt_tokens += int(usage.get("prompt_tokens") or 0)
                    total_completion_tokens += int(usage.get("completion_tokens") or 0)
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    for res, was_cached in pool.map(_job, jobs):
                        page_results.append(res)
                        if was_cached:
                            api_calls_cached += 1
                        else:
                            api_calls_new += 1
                        usage = res.get("usage") or {}
                        total_prompt_tokens += int(usage.get("prompt_tokens") or 0)
                        total_completion_tokens += int(usage.get("completion_tokens") or 0)

        records, summary = compile_paper_records(
            paper, pdf_path, pages_dir, pdf_sha, total_pages, page_results, crops_root
        )
        out_jsonl = source_root / f"{paper_id}.jsonl"
        out_jsonl.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + ("\n" if records else ""),
            encoding="utf-8",
        )
        manifest_papers.append(
            {
                "paper_id": paper_id,
                "course": paper["course"],
                "title": paper["title"],
                "exam_type": paper.get("exam_type"),
                "academic_year": paper.get("academic_year"),
                "exam_date": paper.get("exam_date"),
                "pdf_sha256": pdf_sha,
                "pages": total_pages,
                "question_count": summary["question_count"],
                "answered_count": summary["answered_count"],
            }
        )
        all_summaries.append(summary)
        print(
            f"  -> {paper_id}: {summary['question_count']} 题 "
            f"(答案 {summary['answered_count']}, 题图 {summary['figures_cropped']}, "
            f"失败页 {len(summary['failed_pages'])}, 告警 {summary['issue_count']})",
            flush=True,
        )

    # 合并更新 manifest.json（按 paper_id 保留已有其他卷，支持分批跑）
    manifest_path = source_root / "manifest.json"
    existing_papers: dict[str, dict[str, Any]] = {}
    if manifest_path.is_file():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in old.get("papers") or []:
            existing_papers[item["paper_id"]] = item
    for item in manifest_papers:
        existing_papers[item["paper_id"]] = item
    merged_papers = sorted(existing_papers.values(), key=lambda x: (x["course"], x["paper_id"]))
    manifest_payload = {
        "schema_version": 1,
        "scope": "ext-2020plus",
        "generated_at": _now(),
        "courses": sorted({p["course"] for p in merged_papers}),
        "papers": merged_papers,
    }
    atomic_write_json(manifest_path, manifest_payload)

    report = {
        "generated_at": _now(),
        "papers_processed": len(all_summaries),
        "total_questions": sum(s["question_count"] for s in all_summaries),
        "total_answered": sum(s["answered_count"] for s in all_summaries),
        "total_figures_cropped": sum(s["figures_cropped"] for s in all_summaries),
        "api_calls_new": api_calls_new,
        "api_calls_cached": api_calls_cached,
        "usage": {
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
        },
        "summaries": all_summaries,
    }
    report_name = "extract-report-all.json" if not paper_ids else "extract-report-batch.json"
    atomic_write_json(review_root / report_name, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="F007 多学科试卷视觉提取与编目工具")
    parser.add_argument("--bank-root", default=str(DEFAULT_BANK_ROOT))
    parser.add_argument("--papers-root", default=str(DEFAULT_PAPERS_ROOT))
    parser.add_argument("--papers", nargs="*", default=None, help="指定要处理的 paper_id 列表")
    parser.add_argument("--all", action="store_true", help="处理全部 20 份 2020-2021+ 三科试卷")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--dpi", type=int, default=180)
    parser.add_argument("--force-vision", action="store_true")
    parser.add_argument("--compile-only", action="store_true")
    args = parser.parse_args(argv)

    if not args.all and not args.papers:
        parser.error("请指定 --all 或 --papers <paper_id ...>")

    try:
        report = run_extraction(
            bank_root=Path(args.bank_root),
            papers_root=Path(args.papers_root),
            paper_ids=None if args.all else args.papers,
            workers=args.workers,
            dpi=args.dpi,
            force_vision=args.force_vision,
            compile_only=args.compile_only,
        )
        print(
            f"完成：{report['papers_processed']} 份卷，共 {report['total_questions']} 题，"
            f"有原答案 {report['total_answered']} 题，裁切题图 {report['total_figures_cropped']} 张，"
            f"新调用 {report['api_calls_new']} 次 / 命中缓存 {report['api_calls_cached']} 次。"
        )
        return 0
    except (VisionError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
