"""程序侧条件抽取：模型可以建议筛选，但不能绕过原文与审核边界。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.papers.courses import detect_course, normalize_course
from app.papers.locator import (
    PaperLocation,
    paper_exam_type,
    paper_key,
    paper_term,
    paper_variant,
    title_key,
)
from app.question_bank.config import MAX_BROWSE_LIMIT, MAX_LIMIT

_TYPE_WORDS = {
    "choice": r"选择|单选|多选",
    "fill": r"填空",
    "code": r"编程|代码题|程序题|程序设计|写程序|写代码",
}
# 学年、学期、卷别的写法统一由 app.papers.locator 维护：
# 查卷和查题如果各写一套，学生换个写法就会一边命中、一边漏。
_YEAR_LIKE = re.compile(r"(20\d{2})\s*[-–—~至]\s*(20\d{2})")
_TERM_LIKE = {
    "s1": re.compile(r"第一学期|上学期|秋季|秋学期|\bs1\b|上卷", re.IGNORECASE),
    "s2": re.compile(r"第二学期|下学期|春季|春学期|\bs2\b|下卷", re.IGNORECASE),
}
_TARGET_PRONOUN = re.compile(
    r"这(道|个|一)?题|该题|那道题|刚才那题|上面那题|前面那题|它|第\s*([0-9一二三四五六七八九十]+)\s*(道|个)?题"
)
# “去试卷库/题库里找”明确是跨卷检索：不能悄悄沿用当前试卷，否则范围被
# 锁死在一份卷里，还可能把学生刚做过的那道原题再返回一次。
_CROSS_PAPER = re.compile(
    r"试卷库|题库|所有试卷|全部试卷|各个试卷|每份卷|跨卷|其他试卷|其它试卷|别的试卷|别的卷"
)
_NEW_PROBLEM_HINT = re.compile(r"新题|另一道|换一道|上传|拍照|下面这道")


def _mentions_term(value: str) -> bool:
    return any(pattern.search(value) for pattern in _TERM_LIKE.values())


def asks_across_papers(text: str) -> bool:
    """学生是否明确要求在试卷库/题库范围内跨卷检索。"""

    return bool(_CROSS_PAPER.search(_clean(text or "")))


def is_question_lookup(text: str) -> bool:
    value = _clean(text)
    return bool(re.search(r"(?:找|查|搜|给|来|出).{0,40}真题|(?:找|查|搜).{0,12}(?:一道|一题|道题|题练)", value)
                or (_YEAR_LIKE.search(value) and re.search(r"第[0-9一二三四五六七八九十]+(?:小|道)?题", value)))


def question_answer_requested(text: str) -> bool:
    """A lookup is practice-first; casual '讲讲' must not reveal its answer."""
    value = _clean(text)
    if re.search(r"(?:别|不要|先不|暂不|不需要|不想).{0,8}(?:答案|解析|讲解|讲|解释)|先.{0,4}(?:作答|自己做|自己答)", value):
        return False
    if re.search(r"(?:直接|马上|现在就).{0,8}(?:讲解|讲|解释|解析|答案|解答)|(?:看|查看).{0,5}(?:答案|解析|参考代码)|答案是什么", value):
        return True
    return not is_question_lookup(text) and bool(re.search(r"讲解|讲讲|解释|解析|答案|参考代码", value))


@dataclass(frozen=True)
class SearchIntent:
    course: str | None
    query: str
    question_type: str | None = None
    exam_type: str | None = None
    paper_id: str | None = None
    number_label: str | None = None
    knowledge_points: tuple[str, ...] = ()
    limit: int = 3
    offset: int = 0
    auto_explain: bool = False
    # 学生说“换一道/不要这道”：要在同一条件下排除已展示的题。
    next_card: bool = False
    # “继续”但这一条件下已经没有未展示的题。
    page_exhausted: bool = False
    page_error: str | None = None
    # 原文明确给了年份/学期/考试类型，但无法唯一落到一份试卷。
    paper_unresolved: bool = False


_CN_DIGITS = {c: n for n, c in enumerate("零一二三四五六七八九")}
_ALL_WORDS = re.compile(
    r"全部|所有|全都|整套|整卷|全卷|每一道|每道|剩下的|其余的?|都有哪些|有哪些|列出|显示|展示"
)
# “前五题”“头 5 道”：从第一道开始的条数，不是题号定位。
_HEAD_COUNT = re.compile(r"(?:前|头)\s*([0-9]+|[一二三四五六七八九十]+)\s*(?:道|个|题|小题)")
# 裸数量词（“三道”“5 道选择题”）：前面出现“第”就是题号，不算数量。
_BARE_COUNT = re.compile(r"(?<!第)([0-9]+|[一二三四五六七八九十]+)\s*(?:道|个)\s*(?:选择题|填空题|编程题|题|小题)?")


def _count_value(raw: str) -> int | None:
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if "十" in raw:
        left, right = raw.split("十", 1)
        if (not left or left in _CN_DIGITS) and (not right or right in _CN_DIGITS):
            return _CN_DIGITS.get(left, 1) * 10 + _CN_DIGITS.get(right, 0)
        return None
    return _CN_DIGITS.get(raw)


def requested_count(text: str) -> int | None:
    """学生本轮明确要了几道题；没说明时返回 ``None``。

    这里只回答“要多少”，不回答“哪一道”；“第五题”是题号定位，不是数量。
    “全部/所有”由 :func:`wants_all` 单独判断。
    """

    value = _clean(text)
    match = _HEAD_COUNT.search(value)
    if match:
        number = _count_value(match.group(1))
        if number is not None:
            return number
    match = _BARE_COUNT.search(value)
    if match:
        number = _count_value(match.group(1))
        if number is not None:
            return number
    return None


def wants_all(text: str) -> bool:
    """学生要的是整卷/全部，而不是固定几条预览。"""

    return bool(_ALL_WORDS.search(text or ""))


def _clean(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def detect_question_type(text: str) -> str | None:
    """学生本轮明确说的题型；同时提多种题型时返回 ``None``。

    选中/定位题号时必须带上它：同卷的选择题第 5 题和填空题第 5 题是两道题。
    """

    return _question_type(text)


def _question_type(text: str) -> str | None:
    # A course title is metadata, not a request for a programming exercise.
    text = re.sub(r"(?:《\s*)?python\s*(?:语言)?\s*程序设计(?:\s*》)?", "", text, flags=re.IGNORECASE)
    hits = [
        kind
        for kind, pattern in _TYPE_WORDS.items()
        if re.search(pattern, text, re.IGNORECASE)
    ]
    return hits[0] if len(hits) == 1 else None


def _exam_type(text: str) -> str | None:
    return paper_exam_type(text)


def _paper_id(
    text: str,
    planned: Any,
    papers: list[dict],
    current_paper_id: str | None = None,
) -> tuple[str | None, bool]:
    """返回（试卷 ID，是否需要澄清）。

    目录里的 PaperItem 使用 ``id``；兼容旧测试/调用方的 ``paper_id``。
    原文明确限制年份/学期时，只有在能唯一落到一份卷子时才加限制；
    不能唯一确定就澄清，绝不静默扩大范围。考试类型只用于缩小同一年/学期的候选。

    ``current_paper_id`` 是最近一次已经确定的有效试卷。本轮没有提出任何换卷
    条件（年份、学期、考试类型、卷名）时就沿用它——“刚刚那张卷”因此不需要
    学生再说一遍年份；本轮明确换卷时以新条件为准，不强行继承。
    """

    if not papers:
        normalized = _clean(text)
        explicit = bool(_YEAR_LIKE.search(normalized) or _mentions_term(normalized))
        if current_paper_id and not explicit:
            return current_paper_id, False
        return None, explicit
    normalized = _clean(text)
    location = PaperLocation(
        year=(
            f"{match.group(1)}-{match.group(2)}"
            if (match := _YEAR_LIKE.search(normalized))
            else None
        ),
        term=next(
            (key for key, pattern in _TERM_LIKE.items() if pattern.search(normalized)),
            None,
        ),
        variant=paper_variant(text),
    )
    exam = _exam_type(text)
    # 只有年份/学期/卷别才构成“限定到某份卷”的强条件；考试类型只缩小检索范围。
    explicit = location.explicit

    def _matches(paper: dict) -> bool:
        if location.year and str(paper.get("academic_year")) != location.year:
            return False
        if location.term and paper_term(paper) != location.term:
            return False
        if exam not in (None, "other") and paper.get("exam_type") != exam:
            return False
        # 目录项没有卷名时无法用 A/B 卷区分，不能因此把唯一候选排除掉。
        if location.variant:
            own = paper_variant(str(paper.get("title") or ""))
            if own is not None and own != location.variant:
                return False
        return True

    named = _named_paper(normalized, papers)
    if named is not None:
        # 本轮直接点名了某份卷（整段卷名出现在句子里），这是最强的换卷信号。
        if _matches(next(paper for paper in papers if paper_key(paper) == named)):
            return named, False
        return None, True

    candidates = [paper for paper in papers if _matches(paper)]
    planned_key = (
        planned.strip()
        if isinstance(planned, str) and planned.strip() in {paper_key(p) for p in papers}
        else None
    )
    if planned_key is not None:
        if not explicit:
            return planned_key, False
        if any(paper_key(paper) == planned_key for paper in candidates):
            return planned_key, False
        return None, True
    if not explicit:
        # 本轮没有给年份/学期：既不换卷，也不扩大范围。“期末真题”这类只给考试
        # 类型的请求按 exam_type 过滤，不因此要求学生补年份；只有什么都没说时
        # 才继承最近一次已确定的试卷。
        # 但“去试卷库/题库里找类似的题”明确要求跨卷，继承当前卷会把检索锁死
        # 在一份卷里，甚至把学生刚做过的那道原题再返回一次。
        if (
            exam is None
            and location.variant is None
            and not _CROSS_PAPER.search(normalized)
            and current_paper_id in {paper_key(p) for p in papers}
        ):
            return current_paper_id, False
        return None, False
    if len(candidates) == 1:
        return paper_key(candidates[0]), False
    return None, True


def _named_paper(normalized_text: str, papers: list[dict]) -> str | None:
    """整段卷名（去掉空白/书名号后）出现在句子里时直接命中，唯一才算数。"""

    haystack = title_key(normalized_text)
    hits = [
        paper_key(paper)
        for paper in papers
        if (key := paper_key(paper))
        and (title := title_key(str(paper.get("title") or "")))
        and title in haystack
    ]
    return hits[0] if len(hits) == 1 else None


def _knowledge_points(planned: Any, text: str) -> tuple[str, ...]:
    if not isinstance(planned, list):
        return ()
    normalized = _clean(text)
    kept: list[str] = []
    for item in planned:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value:
            continue
        if _clean(value) in normalized or value in text:
            kept.append(value)
    return tuple(dict.fromkeys(kept))


def _paper_number(text: str) -> str | None:
    match = re.search(r"第\s*([0-9]+|[一二三四五六七八九十]+)\s*(?:道|个|小)?题", text)
    if not match:
        return None
    value = match.group(1)
    if value.isdigit():
        return str(int(value))
    digits = {c: n for n, c in enumerate("零一二三四五六七八九")}
    if "十" in value:
        left, right = value.split("十", 1)
        if (not left or left in digits) and (not right or right in digits):
            return str(digits.get(left, 1) * 10 + digits.get(right, 0))
        return None
    return str(digits[value]) if value in digits else None


_SWAP_PHRASES = re.compile(
    r"换一道|换一题|换一个|换个题|换道题|再来一道|再换一道|不要这道|别来这道|下一道|下一个题"
)


def wants_next_card(text: str) -> bool:
    """学生是否要求在同一条件下换一道没看过的题。"""

    return bool(_SWAP_PHRASES.search(text or ""))


_FILLER_CHARS = frozenset("给出刚刚才那张份的这一个些看说道要下我你它列来卷道个")
_FILLER_PHRASES = re.compile(
    r"给出|列出来|列出|都列|全列|看看|看下|说说|考啥|考什么|考查什么|是啥|是什么|"
    r"问什么|考哪个|考哪些|主要查|查哪个|哪个知识点|主要|知识点|这张卷子?|那张卷子?|这份卷子?|"
    r"那份卷子?|本卷|这卷|那卷|整卷|全卷|几道题?|几题|几个|"
    r"换一道|换一题|换一个|换个题|换道题|再来一道|再换一道|不要这道|别来这道|下一道|下一个题"
)


def _locator_keywords(text: str) -> str:
    """Keep actual topic constraints; remove only the paper/number locator words.

    只保留“题目内容”词：卷名、年份学期、题型、题号、数量词和口语动词都不是
    检索内容。去掉后为空，就说明这是“整卷浏览”，而不是相关性查询。
    """
    value = re.sub(r"换成|改看|换为|切换到|只(?:解释|说明|分析|讲解)", " ", text)
    value = _YEAR_LIKE.sub(" ", value)
    value = re.sub(r"《[^》]*》", " ", value)
    # 先去掉“题目/小题”这类词，避免“第一个题目”被题号规则切成“第一个题+目”。
    value = re.sub(r"题目|试题|小题", " ", value)
    value = re.sub(r"python(?:语言程序设计)?", " ", value, flags=re.IGNORECASE)
    for pattern in _TERM_LIKE.values():
        value = pattern.sub(" ", value)
    value = re.sub(r"第\s*(?:[0-9]+|[一二三四五六七八九十]+)\s*(?:道|个|小)?题", " ", value)
    # “第一道选择题”里的“第一道”指列表位置，同样不是题干内容。
    value = re.sub(r"第\s*(?:[0-9]+|[一二三四五六七八九十]+)\s*(?:道|个|小)", " ", value)
    value = _HEAD_COUNT.sub(" ", value)
    value = _BARE_COUNT.sub(" ", value)
    value = _ALL_WORDS.sub(" ", value)
    value = re.sub(r"[（(]?\s*(?<![a-zA-Z0-9])[ABab]\s*卷\s*[)）]?", " ", value)
    value = re.sub(r"选择题|单选题|多选题|填空题|编程题|代码题|程序题|程序设计题|大题|期末|期中|期终|试卷|试题|题目|真题|学年", " ", value)
    value = re.sub(r"请|帮我|麻烦|给我|我要|我想|查找|搜索|查|搜|找|看看|看|讲解|讲讲|讲|解释|分析|一下", " ", value)
    value = _FILLER_PHRASES.sub(" ", value)
    # “换一道”被数量词规则拆开后只剩“换”：它是换题动词，不是检索内容。
    value = re.sub(r"(?<![交转变兑])换", " ", value)
    tokens = [
        token
        for token in re.split(r"[\s，。？！、：,.;:!?()（）]+", value)
        if token
        and not set(token) <= _FILLER_CHARS
        and not re.fullmatch(r"第?[0-9一二三四五六七八九十]+", token)
    ]
    return " ".join(tokens).strip()


def extract_intent(
    text: str,
    *,
    confirmed_course: str | None,
    planned: dict[str, Any] | None = None,
    papers: list[dict] | None = None,
    current_paper_id: str | None = None,
) -> SearchIntent:
    planned = planned or {}
    detected = detect_course(text)
    course = detected or confirmed_course
    if not course:
        course = None
    elif normalize_course(course) is None:
        course = course.strip()

    query = planned.get("query")
    query = query.strip() if isinstance(query, str) else ""
    if not query:
        query = re.sub(
            r"^(请|帮我|麻烦|给我|我要|我想)?(找|来|出|查|搜|看看?一下?)+", "", text
        ).strip()
    # 模型给的 query 也可能是“全部选择题”“给出刚刚那张”这类定位词；统一只留内容词。
    # 去掉后为空就是整卷浏览，不是相关性检索。
    query = _locator_keywords(query)[:120]

    auto = bool(planned.get("auto_explain")) and bool(
        question_answer_requested(text)
    )

    paper_id, paper_unresolved = _paper_id(
        text,
        planned.get("paper_id") or planned.get("paper"),
        papers or [],
        current_paper_id=current_paper_id,
    )
    number_label = _paper_number(text)
    kind = _question_type(text)

    # 条数规则：有试卷就是“整卷该题型全量”，有数量词就按数量，有题号就按题号。
    # 没有确定试卷时退回相关性检索，上限是 MAX_LIMIT 条，不冒充整卷。
    count = requested_count(text)
    if number_label is not None:
        if paper_id is None:
            paper_unresolved = True
        # 同一题号可能三种题型各有一道；指定题型时就是唯一一道。
        limit_value = 1 if kind else 3
    elif paper_id is not None:
        limit_value = count if count is not None else MAX_BROWSE_LIMIT
    else:
        limit_value = count if count is not None else MAX_LIMIT
    planned_limit = planned.get("limit")
    if count is None and isinstance(planned_limit, int) and planned_limit > 0:
        # 模型可以建议条数，但不能超过本轮规则允许的范围。
        limit_value = min(limit_value, planned_limit) if number_label is None else limit_value
    limit_value = max(1, min(limit_value, MAX_BROWSE_LIMIT))
    if auto:
        limit_value = 1

    offset = planned.get("offset")
    try:
        offset_value = max(0, int(offset))
    except (TypeError, ValueError):
        offset_value = 0

    return SearchIntent(
        course=course,
        query=query,
        question_type=kind,
        exam_type=_exam_type(text),
        paper_id=paper_id,
        number_label=number_label,
        knowledge_points=_knowledge_points(planned.get("knowledge_points"), text),
        limit=limit_value,
        offset=offset_value,
        auto_explain=auto,
        paper_unresolved=paper_unresolved,
    )


def mentions_question_target(text: str) -> bool:
    return bool(_TARGET_PRONOUN.search(text or ""))


def looks_like_new_problem(text: str) -> bool:
    return bool(_NEW_PROBLEM_HINT.search(text or ""))


_CN_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def candidate_number(text: str) -> int | None:
    match = re.search(r"第\s*([0-9]+|[一二三四五六七八九十]+)\s*(道|个)?题", text or "")
    if not match:
        return None
    raw = match.group(1)
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if len(raw) == 1:
        return _CN_NUMBERS.get(raw)
    return None
