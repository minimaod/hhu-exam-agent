"""评测专用：把旧自然语言脚本翻译成结构化工具参数。

生产代码不得导入本模块。它只服务于 A 批冻结重放和测试替身，让旧脚本
继续检验原业务断言，同时把“读原话猜意图”限制在评测边界内。
"""

from __future__ import annotations

import re
from typing import Any

from app.conversations.answer_gate import scope_of
from app.papers.courses import detect_course
from app.papers.locator import extract_location

from evals.f010_d.legacy_intent import (
    candidate_number,
    detect_question_type,
    extract_intent,
    is_question_lookup,
    question_answer_requested,
    wants_next_card,
)

_CONTINUE = re.compile(
    r"继续|接着|下一批|下一页|下一部分|剩下的|其余的|后面那些|还没(看|显示|列)"
)
_SIMILAR = re.compile(r"类似|相似|同类型|同一类|换一道|另一道|别的题|其他题|其它题|再换一道")
_LIST_POSITION = re.compile(
    r"(列表|上面|其中|这些|那几个|这几道|这几个|结果|候选)[^。；，]{0,6}"
    r"第?\s*([0-9一二三四五六七八九十]+)\s*(个|项|条|道题?)"
    r"|第\s*([0-9]+)\s*(个|项|条)"
)
_NEW_PROBLEM = re.compile(r"讲解题\s*[:：]|新题|上传|拍照|下面这道")
_TRY_FIRST = re.compile(
    r"先自己做|自己做|别给(我)?答案|不要答案|先不给答案|先别说答案|"
    r"不公布答案|先不(要)?讲解|先别讲|只看题|先看题|先自己(想|写|做)|自己想|"
    r"只给提示|只提示|给点提示|提示一下|先别公布|别公布|不要公布"
)


def _put(params: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if key not in params or params[key] in (None, "", [], ()):
        params[key] = value


def _position(text: str) -> int | None:
    match = _LIST_POSITION.search(text or "")
    if match is None:
        return None
    raw = match.group(2) or match.group(4)
    if raw is None:
        return None
    if raw.isdigit():
        return int(raw)
    digits = {char: number for number, char in enumerate("零一二三四五六七八九")}
    if raw == "十":
        return 10
    if "十" in raw:
        left, right = raw.split("十", 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(raw)


def enrich(
    action: str,
    parameters: dict[str, Any] | None,
    text: str,
    *,
    confirmed_course: str | None = None,
) -> dict[str, Any]:
    """返回补全后的参数副本；已有值（包括故意传错的值）一律不覆盖。"""

    params = dict(parameters or {})
    text = text or ""
    course = params.get("course") or confirmed_course or detect_course(text)
    location = extract_location(text)
    intent = extract_intent(
        text,
        confirmed_course=course,
        planned=params,
        current_paper_id=params.get("paper_id"),
    )

    if action == "query_papers":
        _put(params, "course", course)
        _put(params, "exam_type", location.exam_type if location.exam_type_stated else None)
        _put(params, "academic_year", location.year)
        _put(params, "term", location.term)
        _put(params, "variant", location.variant)
    elif action == "list_paper_questions":
        # 整卷/按题型列题只接受这些字段；query、exam_type、number_label 属于
        # search_questions，混入会被工具校验直接拒绝。
        _put(params, "course", course)
        _put(params, "question_type", intent.question_type)
        _put(params, "paper_id", intent.paper_id)
        if (
            not params.get("query")
            and not params.get("number_label")
            and (_CONTINUE.search(text) or wants_next_card(text))
        ):
            params.setdefault("continue_from_previous", True)
    elif action == "search_questions":
        _put(params, "course", course)
        _put(params, "question_type", intent.question_type)
        _put(params, "query", intent.query or None)
        _put(params, "number_label", intent.number_label)
        _put(params, "exam_type", intent.exam_type)
        _put(params, "paper_id", intent.paper_id)
        if re.search(r"第\s*[0-9一二三四五六七八九十]+\s*题", text):
            params["query"] = ""
        elif params.get("query") and not params.get("paper_id"):
            params.setdefault("scope", "bank")
        fresh_target = bool(
            params.get("query") or params.get("number_label") or params.get("question_type")
        )
        if not fresh_target and (_CONTINUE.search(text) or wants_next_card(text)):
            params.setdefault("continue_from_previous", True)
        if _SIMILAR.search(text) and not fresh_target:
            params.setdefault("exclude_previous", True)
            params.setdefault("continue_from_previous", True)
        if intent.auto_explain:
            params.setdefault("auto_explain", True)
        if _TRY_FIRST.search(text):
            params.setdefault("defer_answer", True)
    elif action == "select_question":
        # 旧的 index/number_label 已经是明确的定位契约；再补 number_label 或
        # question_type 会触发“只能有一个选择器”，或改变原本的跨题型歧义。
        return params
    elif action == "explain_problem":
        scope = scope_of(text)
        if scope in {"hint", "topic"}:
            params.setdefault("scope", scope)
        if _NEW_PROBLEM.search(text):
            params.setdefault("target", "new")
        _put(params, "course", course)
    elif action in {"answer_followup", "followup_problem"}:
        if re.search(r"母题|原题|原来的题|原来那道", text):
            params.setdefault("target", "problem")
    return params


def explain_hint(text: str) -> bool:
    """测试断言辅助：旧文本是否只是要提示/考点。"""

    return scope_of(text) in {"hint", "topic"} or is_question_lookup(text) or question_answer_requested(text)