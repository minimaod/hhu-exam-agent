"""试卷定位规则：把学生原话里的条件落成真实目录里的那一份试卷。

职责边界：
- 只处理“原文 → 定位条件 → 命中的目录记录”这一段，不查题库、不读学习库；
  查题（``app.questions.intent``）和查卷（``app.conversations.service``）都用它，
  避免两处各写一套年份/学期规则而互相漂移。
- 目录没有独立的学期字段，学期优先从标题（“第一学期”）确定，其次从登记 ID
  （``-s1-`` / ``-s2-``）；两者都没有就保持未知，不猜测。
- 只有唯一命中才给出试卷；0 份和 ≥2 份都必须如实返回，不静默扩大也不随机挑。

术语表与原先查题里的写法保持一致（含全角卷别、上/下学期等口语）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

_WHITESPACE = re.compile(r"\s+")

# 学年：一段“2024-2025”，或单独写“2024学年/2024年/2024”。
_YEAR_RANGE = re.compile(r"(20\d{2})\s*[-–—~至]\s*(20\d{2})")
_YEAR_TOKEN = re.compile(
    r"((?:19|20)\d{2}\s*(?:[-–—~至]\s*(?:19|20)\d{2})?)\s*学年?"
)

# “第一学期/上学期/秋季”等都归到 s1。与查题共用同一套词。
_TERM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("s1", re.compile(r"第一学期|上学期|秋季|秋学期|\bs1\b|上卷", re.IGNORECASE)),
    ("s2", re.compile(r"第二学期|下学期|春季|春学期|\bs2\b|下卷", re.IGNORECASE)),
)
_TERM_FROM_ID = re.compile(r"-(s1|s2)(?:-|$)", re.IGNORECASE)
# 卷别：A 卷 / B 卷。前面不能紧跟字母数字，避免把 “BI 卷” 之类误当卷别。
_VARIANT = re.compile(r"(?<![a-z0-9])([ab])\s*卷", re.IGNORECASE)

_EXAM_TYPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("midterm", re.compile(r"期中|期终?中|\bmidterm\b", re.IGNORECASE)),
    ("final", re.compile(r"期末|期终|\bfinal\b", re.IGNORECASE)),
    ("other", re.compile(r"其[他它]类型|其[他它]考试|\bother\b", re.IGNORECASE)),
)
# “不限期中期末/全部类型/不要只查期末”：学生主动取消考试类型限制。
_EXAM_TYPE_UNLIMITED = re.compile(
    r"不限|不区分|全部类型|所有类型|不要只查|别只查|不只查|不只要"
)

EXAM_TYPE_LABELS = {"midterm": "期中", "final": "期末", "other": "其他"}  # 与卷别拼成“期末A卷”
EXAM_TYPE_SCOPE = {"midterm": "期中卷", "final": "期末卷", "other": "其他类型"}
TERM_LABELS = {"s1": "第一学期", "s2": "第二学期"}

RANK_UNIQUE = "unique"
RANK_AMBIGUOUS = "ambiguous"
RANK_NONE = "none"
RANK_NULL = "null"


def clean(text: str | None) -> str:
    """去掉全部空白并转小写；年份、卷别、学期都比较这个形式。"""

    return _WHITESPACE.sub("", text or "").lower()


def normalize_academic_year(text: str | None) -> str:
    """把“2024–2025”统一成“2024-2025”。

    “2023学年/2023年”只有起始年时补成“2023-2024”；无法判断就不动，交给
    后续的精确匹配自然返回空结果。
    """

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        if any(mark in raw for mark in "-–—~至"):
            head = _YEAR_RANGE.search(raw)
            return f"{head.group(1)}-{head.group(2)}" if head else raw
        year = int(re.sub(r"\D", "", raw) or 0)
        return f"{year}-{year + 1}" if year else raw

    return _YEAR_TOKEN.sub(_replace, clean(text))


def _year(text: str | None) -> str | None:
    match = _YEAR_RANGE.search(normalize_academic_year(text))
    return f"{match.group(1)}-{match.group(2)}" if match else None


def _exam_type_stated(text: str | None) -> bool:
    """原文是否真的在限定考试类型（“不限期中期末”不算）。"""

    if not text or _EXAM_TYPE_UNLIMITED.search(text):
        return False
    return any(pattern.search(text) for _, pattern in _EXAM_TYPES)


def cancels_exam_type(text: str | None) -> bool:
    """学生是否明确取消考试类型限制（“不限期中期末/全部类型”）。

    这和“本轮没提类型”是两件事：没提可以继承上一轮，明确取消必须清掉
    旧的类型限制和只属于旧类型的候选（见 §7.10 R2b）。
    """

    return bool(text and _EXAM_TYPE_UNLIMITED.search(text))


def _term(text: str | None) -> str | None:
    value = clean(text)
    return next((key for key, pattern in _TERM_PATTERNS if pattern.search(value)), None)


def paper_variant(value: str | None) -> str | None:
    """从卷名里取卷别 A/B；没有卷别返回 None。"""

    match = _VARIANT.search(clean(value))
    return match.group(1).upper() if match else None


def paper_exam_type(text: str | None) -> str | None:
    """原文提到的考试类型；同时提到期中期末时返回 None（表示不限制）。"""

    if not text:
        return None
    if _EXAM_TYPE_UNLIMITED.search(text):
        return None
    mentioned = {
        kind for kind, pattern in _EXAM_TYPES if pattern.search(text)
    }
    return next(iter(mentioned)) if len(mentioned) == 1 else None


def paper_key(paper: Any) -> str | None:
    """目录记录的稳定 ID；同时兼容 ``id`` 和 ``paper_id`` 两种写法。"""

    if isinstance(paper, Mapping):
        value = paper.get("id") or paper.get("paper_id")
    else:
        value = getattr(paper, "id", None) or getattr(paper, "paper_id", None)
    return str(value) if value else None


def _field(paper: Any, name: str) -> Any:
    if isinstance(paper, Mapping):
        return paper.get(name)
    return getattr(paper, name, None)


def title_key(value: str | None) -> str:
    """卷名的可比较形式：去空白、去书名号、去括号与全角标点。

    学生常写“2024-2025学年第一学期 Python语言程序设计 期末试卷 A卷”，
    和标题的差别只在空格、书名号和括号，因此整段卷名直命中必须在
    这个规范形式上比较。
    """

    return re.sub(r"[《》〈〉()（）\[\]【】「」、，,。;；:：]", "", clean(value))


def paper_title_key(paper: Any) -> str:
    return title_key(str(_field(paper, "title") or ""))


def named_paper(text: str | None, papers: Iterable[Any]) -> str | None:
    """整段卷名（书名号可有可无）出现在句子里时直接命中；唯一才算数。"""

    haystack = title_key(text)
    if not haystack:
        return None
    hits = [
        key
        for paper in papers
        if (key := paper_key(paper)) and (title := paper_title_key(paper)) and title in haystack
    ]
    return hits[0] if len(hits) == 1 else None


def paper_term(paper: Any) -> str | None:
    """目录记录的学期：标题优先（“第一学期”），其次登记 ID 的 ``-s1-``。"""

    from_title = _term(str(_field(paper, "title") or ""))
    if from_title:
        return from_title
    match = _TERM_FROM_ID.search(str(paper_key(paper) or ""))
    return match.group(1).lower() if match else None


def paper_year(paper: Any) -> str:
    return normalize_academic_year(str(_field(paper, "academic_year") or ""))


@dataclass(frozen=True)
class PaperLocation:
    """从学生原话抽出的定位条件；None 表示这一项没有限定。"""

    year: str | None = None
    term: str | None = None
    variant: str | None = None
    exam_type: str | None = None
    # 原文是否真的限定过考试类型；“高数期中期末都要”是“不限”，
    # 不能因为调用方另带了 exam_type 就把范围收窄。
    exam_type_stated: bool = False

    @property
    def explicit(self) -> bool:
        """是否出现了“限定到某一份卷”的强条件。

        考试类型只缩小范围，不构成强条件：只说“期末”时仍允许列出全部期末卷。
        """

        return any((self.year, self.term, self.variant))


def extract_location(text: str | None) -> PaperLocation:
    return PaperLocation(
        year=_year(text),
        term=_term(text),
        variant=paper_variant(text),
        exam_type=paper_exam_type(text),
        exam_type_stated=_exam_type_stated(text),
    )


def describe_location(location: PaperLocation) -> str:
    """给学生看的条件摘要：``2024-2025学年第一学期期末A卷``。"""

    parts: list[str] = []
    if location.year:
        parts.append(f"{location.year}学年")
    if location.term:
        parts.append(TERM_LABELS.get(location.term, location.term))
    if location.exam_type:
        parts.append(EXAM_TYPE_LABELS.get(location.exam_type, location.exam_type))
    if location.variant:
        parts.append(f"{location.variant}卷")
    return "".join(parts)


def paper_matches(
    paper: Any,
    location: PaperLocation,
    *,
    include_exam_type: bool = True,
) -> bool:
    """目录记录是否符合定位条件。未给出的项不参与比较。"""

    if location.year and paper_year(paper) != location.year:
        return False
    if location.term and paper_term(paper) != location.term:
        return False
    if location.variant:
        own = paper_variant(str(_field(paper, "title") or ""))
        # 目录标题里没有卷别时无法区分，不能因此把唯一候选排除掉。
        if own is not None and own != location.variant:
            return False
    if include_exam_type and location.exam_type:
        if str(_field(paper, "exam_type") or "") != location.exam_type:
            return False
    return True


@dataclass(frozen=True)
class PaperMatch:
    """一次定位的结果。

    ``rank``：
    - ``unique``：唯一命中，``paper`` 有值；
    - ``ambiguous``：多条候选，需要学生补充条件；
    - ``none``：条件明确但目录里没有这一份；
    - ``null``：本轮没有学年/学期/卷别/卷名，按普通筛选处理。
    """

    rank: str
    paper: Any | None
    candidates: tuple[Any, ...]
    location: PaperLocation
    named: bool = False

    @property
    def unique(self) -> bool:
        return self.rank == RANK_UNIQUE

    @property
    def titles(self) -> tuple[str, ...]:
        return tuple(str(_field(item, "title") or "") for item in self.candidates)


def locate_papers(
    text: str | None,
    papers: Iterable[Any],
    *,
    exam_type: str | None = None,
    location: PaperLocation | None = None,
) -> PaperMatch:
    """按学生原话在给定目录范围内定位一份试卷。

    ``exam_type`` 是调用方已经核对过的考试类型（例如模型只想查期末），
    但学年/学期/卷别一律以原话为准，不能被模型参数覆盖或丢掉。

    ``location`` 用于“本轮原话不完整、条件来自上一轮待澄清”的情况：
    调用方把合并后的完整条件传进来，实际匹配必须用它，而不是重新解析
    这句只说了“改看期中B卷”的原话，否则学年、学期会被悄悄丢掉。
    """

    items = list(papers)
    if location is None:
        location = extract_location(text)
    if exam_type is not None and not location.exam_type_stated:
        # 原文没有限定考试类型时，才采用调用方（模型）核对过的类型。
        location = PaperLocation(
            year=location.year,
            term=location.term,
            variant=location.variant,
            exam_type=exam_type,
            exam_type_stated=False,
        )

    named = named_paper(text, items)
    if named is not None:
        chosen = next(item for item in items if paper_key(item) == named)
        if paper_matches(chosen, location, include_exam_type=False):
            return PaperMatch(RANK_UNIQUE, chosen, (chosen,), location, named=True)
        return PaperMatch(RANK_NONE, None, (), location, named=True)

    if not location.explicit:
        return PaperMatch(RANK_NULL, None, (), location)

    candidates = tuple(
        item for item in items if paper_matches(item, location, include_exam_type=True)
    )
    if len(candidates) > 1:
        # 多条候选时，只有“整段卷名完全等于其中一份”才算唯一：
        # “2024-2025学年第一学期《Python…》期末试卷”是点卷名，
        # 而“2024-2025学年第一学期Python试卷”只是给了范围，仍需问卷别。
        haystack = title_key(text)
        exact = [item for item in candidates if paper_title_key(item) == haystack]
        if len(exact) == 1:
            return PaperMatch(RANK_UNIQUE, exact[0], (exact[0],), location)
        return PaperMatch(RANK_AMBIGUOUS, None, candidates, location)
    if candidates:
        return PaperMatch(RANK_UNIQUE, candidates[0], candidates, location)
    return PaperMatch(RANK_NONE, None, (), location)
