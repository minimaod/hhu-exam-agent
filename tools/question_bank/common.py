"""离线抽取与复核工具共用的纯函数与路径约定。

设计原则：
- 不导入 PyMuPDF：本模块只放可在测试中直接调用的纯逻辑（切分、清洗、
  状态判定），PDF 读取集中在 `extract.py`。
- 所有写入路径都通过 `paths()` 计算并打印，便于在报告里交代写入位置。
- 任何"识别结果"一律先标为候选；本模块不产生 approved。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data"
CATALOG_PATH = DATA_ROOT / "papers" / "catalog.json"
PAPER_ROOT = DATA_ROOT / "papers"

QB_ROOT = DATA_ROOT / "local" / "question-bank"
SOURCE_ROOT = QB_ROOT / "source"
WORK_ROOT = QB_ROOT / "ingest"
REVIEW_ROOT = QB_ROOT / "review"
MODEL_CACHE_ROOT = QB_ROOT / "models"
EVIDENCE_ROOT = DATA_ROOT / "local" / "qb-evidence"

COURSE = "Python语言程序设计"

SECTION_CHOICE = "choice"
SECTION_FILL = "fill"
SECTION_CODE = "code"

# 答案状态四值：
#   original       原卷给出了答案，且载体与题号的对应关系已核对
#   missing        原始资料确实没有答案载体
#   pending_review 找到了候选载体，但对应关系（题号/卷别）尚未确认
#   conflict       两份以上原卷证据互相矛盾
ANSWER_ORIGINAL = "original"
ANSWER_MISSING = "missing"
ANSWER_PENDING = "pending_review"
ANSWER_CONFLICT = "conflict"

CHEM_NUMBER = str.maketrans("０１２３４５６７８９（）", "0123456789()")

_PAGE_DECL = re.compile(r"第\s*\d+\s*页")
_PAGE_TOTAL = re.compile(r"共\s*\d+\s*页")
_YEAR_RANGE = re.compile(r"(\d{4})\s*[~～\-－—]\s*(\d{4})\s*学年")
_COHORT = re.compile(r"[（(]\s*(\d{4})\s*级[^）)]*[）)]")
_COHORT_LOOSE = re.compile(r"(\d{4})\s*级")
_COURSE_LINE = re.compile(r"《[^》]*》")
_MULTI_SPACE = re.compile(r"[ \t\u3000]{2,}")

# 页眉行：学年行、课程标题行、年级专业行、考试名称行。只在前 140pt 内生效，
# 避免把正文里偶尔出现的《》或“期中试卷”误判成页眉。
_HEAD_YEAR = re.compile(r"^\d{4}\s*[~～\-－—]\s*\d{4}\s*学年")
_HEAD_COURSE = re.compile(r"^《[^》]+》")
_HEAD_COHORT = re.compile(r"^[（(]\s*\d{4}\s*级[^）)]*[）)]\s*$")
_HEAD_EXAM = re.compile(r"(期中|期末)(考试)?试卷\s*$")
_HEADER_MAX_Y = 140.0

# 题型大题标题（允许空格与全角）
_SECTION_HEAD_CHOICE = re.compile(r"^[一二三四五六七八]、\s*选\s*择\s*题")
_SECTION_HEAD_FILL = re.compile(r"^[一二三四五六七八]、\s*填\s*空\s*题")
_SECTION_HEAD_CODE = re.compile(r"^[一二三四五六七八]、\s*编\s*程\s*题")

_CODE_HINT = re.compile(
    r"(^|\s)(def |class |for |while |if |elif |else|import |from |print\s*\(|return |try:|except|lambda )"
)


def normalize_text(value: str) -> str:
    """统一全角数字/括号，压缩多余空格；不改动代码缩进的换行结构。"""

    return value.translate(CHEM_NUMBER)


def clean_line(value: str) -> str:
    """单行清洗：去掉行尾空白，多个空格压成一个（用于比对与展示）。"""

    return _MULTI_SPACE.sub(" ", value.replace("\u00a0", " ")).strip()


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_catalog(path: Path | None = None) -> list[dict[str, Any]]:
    payload = json.loads((path or CATALOG_PATH).read_text(encoding="utf-8"))
    items = payload["items"] if isinstance(payload, dict) else payload
    return list(items)


def select_papers(
    catalog: Sequence[dict[str, Any]],
    paper_ids: Sequence[str] | None,
    course: str = COURSE,
) -> list[dict[str, Any]]:
    """按课程与给定 ID 选卷；未给 ID 时返回该课程全部卷。"""

    pool = [item for item in catalog if item.get("course") == course]
    if not paper_ids:
        return pool
    wanted = list(paper_ids)
    by_id = {item["id"]: item for item in pool}
    missing = [pid for pid in wanted if pid not in by_id]
    if missing:
        raise KeyError(f"目录里没有这些 Python 试卷：{', '.join(missing)}")
    return [by_id[pid] for pid in wanted]


def paper_pdf_path(item: dict[str, Any], root: Path | None = None) -> Path:
    return (root or PAPER_ROOT) / item["file_path"]


# ----------------------------------------------------------------------
# 页眉/页脚识别
# ----------------------------------------------------------------------


def is_running_line(text: str) -> bool:
    """判断是否为页眉页脚类噪声（页码、总页数、卷头、年级专业行）。

    单独的数字行不在这里判掉：答题纸网格里的题号也是裸数字，需结合
    y 坐标判断，见 `is_page_number`。
    """

    value = clean_line(normalize_text(text))
    if not value:
        return False
    if _PAGE_DECL.search(value) or _PAGE_TOTAL.search(value):
        return True
    return False


def is_page_number(text: str, bbox: Sequence[float], page_height: float) -> bool:
    """裸数字且位于页眉/页脚区域时，才当作页码丢弃。"""

    value = clean_line(normalize_text(text))
    if not re.fullmatch(r"[—\-–\s]*\d{1,3}[—\-–\s]*", value):
        return False
    return bbox[3] > page_height - 55 or bbox[1] < 40


def is_header_line(text: str, bbox: Sequence[float]) -> bool:
    """页眉三件套：学年行、课程标题行、年级专业行、考试名称行。"""

    if bbox[1] > _HEADER_MAX_Y:
        return False
    value = clean_line(normalize_text(text))
    if not value:
        return False
    return bool(
        _HEAD_YEAR.match(value)
        or _HEAD_COURSE.match(value)
        or _HEAD_COHORT.match(value)
        or _HEAD_EXAM.search(value)
    )


_PAGE_PAIR = re.compile(r"第\s*(\d{1,3})\s*页\s*共\s*(\d{1,3})\s*页")


def parse_page_decl(text: str) -> tuple[int, int] | None:
    """解析"第 X 页 共 Y 页"，用于把 PDF 切成各自独立编号的副本。"""

    match = _PAGE_PAIR.search(clean_line(normalize_text(text)))
    if not match:
        return None
    current, total = int(match.group(1)), int(match.group(2))
    if current < 1 or total < current:
        return None
    return current, total


def header_signature(lines: Iterable[str]) -> dict[str, Any]:
    """从一页的文字行里提取卷头要素，作为"同卷/不同卷"的候选证据。

    只记录事实，不据此判定"混入另一套试卷"——那需要题目内容比对。
    """

    joined = "\n".join(clean_line(normalize_text(line)) for line in lines)
    years = _YEAR_RANGE.search(joined)
    cohort = _COHORT.search(joined) or _COHORT_LOOSE.search(joined)
    course_line = _COURSE_LINE.search(joined)
    page_decl = _PAGE_DECL.search(joined)
    return {
        "academic_year": f"{years.group(1)}-{years.group(2)}" if years else None,
        "cohort": cohort.group(1) if cohort else None,
        "course_line": course_line.group(0) if course_line else None,
        "page_decl": page_decl.group(0) if page_decl else None,
    }


# ----------------------------------------------------------------------
# 大题与小题切分
# ----------------------------------------------------------------------

SECTION_HEADS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (SECTION_CHOICE, _SECTION_HEAD_CHOICE),
    (SECTION_FILL, _SECTION_HEAD_FILL),
    (SECTION_CODE, _SECTION_HEAD_CODE),
)

SECTION_TITLES = {
    SECTION_CHOICE: "一、选择题",
    SECTION_FILL: "二、填空题",
    SECTION_CODE: "三、编程题",
}

_NUMBER_LINE = re.compile(r"^(\d{1,2})\s*[.．、]\s*(.*)$")
_BLANK_MARK = re.compile(r"(?<![0-9A-Za-z.])[(（]\s*(\d{1,3})\s*[)）](?![0-9A-Za-z])")
_OPTION_MARK = re.compile(r"(?:(?<=^)|(?<=[\s(（、]))([A-D])\s*[.．、]")


def section_of(text: str) -> str | None:
    value = clean_line(normalize_text(text))
    for code, pattern in SECTION_HEADS:
        if pattern.match(value):
            return code
    return None


def split_question_number(text: str) -> tuple[int, str] | None:
    """把 "12. 题干" 拆成 (12, "题干")；不是编号行返回 None。"""

    value = clean_line(normalize_text(text))
    match = _NUMBER_LINE.match(value)
    if not match:
        return None
    return int(match.group(1)), match.group(2).strip()


def blank_marks(text: str) -> list[int]:
    return [int(m.group(1)) for m in _BLANK_MARK.finditer(normalize_text(text))]


def _find_option_chain(text: str) -> list[tuple[str, int, int]]:
    """在文本中找一条 A→B→C→D 的顺序选项链。

    只接受可以连续递增的字母序列，避免把代码里的 "A." / "x.A" 当成选项。
    """

    marks = [(m.group(1), m.start(), m.end()) for m in _OPTION_MARK.finditer(text)]
    if len(marks) < 2:
        return []
    best: list[tuple[str, int, int]] = []
    current: list[tuple[str, int, int]] = []
    expected = "A"
    for mark in marks:
        label = mark[0]
        if label == expected:
            current.append(mark)
            expected = chr(ord(expected) + 1)
        elif label == "A":
            if len(current) > len(best):
                best = current
            current = [mark]
            expected = "B"
        else:
            continue
    if len(current) > len(best):
        best = current
    return best if len(best) >= 2 else []


def _split_options_by_unique_labels(
    value: str,
) -> tuple[str, list[dict[str, str]]] | None:
    """处理 A/C 同行、B/D 同行的两栏选项排版。

    这类卷面按视觉列读出后是 `A. … C. …\\nB. … D. …`，字母顺序不是
    A→B→C→D，`_find_option_chain` 只能取到前两个。只有 A–D 各出现
    恰好一次时才按标号归位，避免把代码里的 `A.` 当选项。
    """

    marks = list(_OPTION_MARK.finditer(value))
    if len(marks) != 4:
        return None
    labels = [mark.group(1) for mark in marks]
    if sorted(labels) != ["A", "B", "C", "D"]:
        return None
    stem = clean_line(value[: marks[0].start()])
    bodies: dict[str, str] = {}
    for index, mark in enumerate(marks):
        stop = marks[index + 1].start() if index + 1 < len(marks) else len(value)
        bodies[mark.group(1)] = clean_line(value[mark.end() : stop])
    if any(not bodies[label] for label in "ABCD"):
        return None
    options = [{"label": label, "text": bodies[label]} for label in "ABCD"]
    return stem, options


def split_options(text: str) -> tuple[str, list[dict[str, str]]]:
    """把一道选择题的正文拆成（选项前的题干, 选项列表）。

    选项可能同行也可能分行；找不到成链选项时返回原文与空列表。
    两栏配对排版（A/C、B/D）在成链切分不足四项时按标号归位。
    """

    value = normalize_text(text)
    chain = _find_option_chain(value)
    if len(chain) < 4:
        paired = _split_options_by_unique_labels(value)
        if paired is not None:
            return paired
    if not chain:
        return clean_line(value), []
    stem = clean_line(value[: chain[0][1]])
    options: list[dict[str, str]] = []
    for index, (label, _start, end) in enumerate(chain):
        stop = chain[index + 1][1] if index + 1 < len(chain) else len(value)
        body = clean_line(value[end:stop])
        options.append({"label": label, "text": body})
    return stem, options


def looks_like_code(text: str) -> bool:
    value = normalize_text(text)
    if not value.strip():
        return False
    if value[:1] in (" ", "\t"):
        # PyMuPDF 会保留缩进；但页边也可能有空格，所以还要看内容特征。
        if _CODE_HINT.search(value) or re.search(r"[=:]", value):
            return True
    if _CODE_HINT.search(value):
        return True
    if re.match(r"^[A-Za-z_][\w\.\[\]\(\)]*\s*(=|\+=|-=|\*=|/=)", value.strip()):
        return True
    if re.search(r"print\s*\(|input\s*\(|range\s*\(|\.append\s*\(|\.format\s*\(", value):
        return True
    return False


# ----------------------------------------------------------------------
# 答案来源与状态
# ----------------------------------------------------------------------


@dataclass
class AnswerSource:
    """一条答案证据。

    kind: grid_mark / red_annotation / typed_list / layout_text / code_block
    pairing_verified: 载体与题号的对应关系是否已通过结构检查（题数一致、
      题号连续、同卷证据一致）。False 表示只能作为候选。
    """

    kind: str
    text: str
    pages: list[int] = field(default_factory=list)
    detail: str = ""
    pairing_verified: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "pages": self.pages,
            "detail": self.detail,
            "pairing_verified": self.pairing_verified,
        }


def resolve_answer_state(
    sources: Sequence[AnswerSource],
) -> tuple[str, str | None, str | None]:
    """按四值口径判定答案状态。

    返回 (state, 采用文本, 说明)。规则：
    - 没有任何来源 → missing；
    - 来源文本互相矛盾 → conflict（不擅自选一个当作原卷答案）；
    - 只有未核对的候选对应 → pending_review（文本仍保留在说明里）；
    - 否则 → original。
    """

    if not sources:
        return ANSWER_MISSING, None, "原始资料未提供参考答案。"

    distinct = {source.text.strip() for source in sources if source.text.strip()}
    if len(distinct) > 1:
        detail = "；".join(
            f"{source.kind}@{source.pages}={source.text.strip() or '(空)'}"
            for source in sources
        )
        return ANSWER_CONFLICT, None, f"原卷证据互相矛盾：{detail}"

    text = next(iter(distinct)) if distinct else None
    if not any(source.pairing_verified for source in sources):
        detail = "；".join(f"{source.kind}@{source.pages}" for source in sources)
        return ANSWER_PENDING, text, f"已找到答案载体（{detail}），但题号对应关系尚未确认。"
    return ANSWER_ORIGINAL, text, None


def choose_primary(sources: Sequence[AnswerSource]) -> AnswerSource | None:
    """挑选用于展示的主证据：优先已核对，再按载体可信度排序。"""

    if not sources:
        return None
    order = {"grid_mark": 0, "typed_list": 1, "red_annotation": 2, "layout_text": 3, "code_block": 4}
    return sorted(
        sources,
        key=lambda item: (not item.pairing_verified, order.get(item.kind, 9), item.pages[:1]),
    )[0]


# ----------------------------------------------------------------------
# 读写入
# ----------------------------------------------------------------------


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def get_logger(name: str, log_path: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    logger.propagate = False
    return logger


def relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()