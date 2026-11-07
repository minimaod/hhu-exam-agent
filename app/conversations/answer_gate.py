"""F010 C：答案与讲解的授权门禁。

门禁只回答一个问题：**学生本轮的原话、会话绑定、题目版本和来源状态，是否
允许模型取得这道题的答案/参考解答。** 它不读取答案内容，也不替模型选择工具。

设计边界（任务书 §五 C-1）：

* 授权证据只来自学生本轮发言；模型参数（``auto_explain``、``mode``、``ref``
  或未来新增字段）、历史里的 ``answer_revealed_at``、transcript、以及“模型
  调用了查看答案工具”这一事实本身，都不构成授权。
* 授权只在本轮有效；下一轮要重新满足条件。
* 拒绝以结构化 ``GateDecision`` 返回，由调用方转成 role=tool 的拒绝信封；
  拒绝体永远不含答案正文、答案 URL 或参考代码。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from app.conversations.models import ExerciseEvaluation

Scope = Literal["hint", "topic", "explain", "answer", "solution"]

# ----------------------------------------------------------------------
# 学生原话的信号（与 explain/search 既有实现保持同一套词表，集中在这里维护）
# ----------------------------------------------------------------------
_HINT_ONLY = re.compile(
    r"只给提示|只提示|给点提示|给个提示|提示一下|稍作提示|稍微提示"
)
_TRY_FIRST = re.compile(
    r"先自己做|自己做|别给(我)?答案|不要答案|先不给答案|先别说答案|"
    r"不公布答案|先不(要)?讲解|先别讲|只看题|先看题|先自己(想|写|做)|自己想|"
    r"只给提示|只提示|给点提示|提示一下|先别公布|别公布|不要公布"
)
_EXPLAIN_MARK = re.compile(
    r"讲|解释|说明|分析|啥意思|什么意思|考(点|啥|什么|哪个)|怎么做|为什么|"
    r"思路|步骤|看不懂|看不明白|不会|错在|对不对|选哪个|哪个选项"
)
_ANSWER_ASK = re.compile(
    r"(?:看|查看|给|要|展示|显示|公布|对).{0,6}(?:答案|解析|参考代码|参考答案)"
    r"|答案是什么|直接给答案|对答案"
)
_LOOKUP = re.compile(
    r"(?:找|查|搜|给|来|出).{0,40}真题|(?:找|查|搜).{0,12}(?:一道|一题|道题|题练)"
)
_NEGATED_ANSWER = re.compile(
    r"(?:别|不要|先不|暂不|不需要|不想).{0,8}(?:答案|解析|讲解|讲|解释)"
    r"|先.{0,4}(?:作答|自己做|自己答)"
)
_EXPLICIT_ANSWER_AFTER_LOOKUP = re.compile(
    r"(?:直接|马上|现在就).{0,8}(?:讲解|讲|解释|解析|答案|解答)"
    r"|(?:看|查看).{0,5}(?:答案|解析|参考代码)|答案是什么"
)

_TOPIC_OBJECT = re.compile(
    r"考点|知识点|考查(什么|点|哪些|哪个)"
    r"|考[了的]?(什么|啥|哪些|哪个|哪几个)"
    r"|考核?[^。；，,！？]{0,3}(什么|啥|哪些|哪个|哪几个)"
    r"|查[^。；，,！？]{0,3}(什么|啥|哪个|哪些)"
)
_FULL_MARKER = re.compile(
    r"步骤|答案|解答|过程|代码|完整|怎么做|为什么|解析|推导|思路|例子|举例"
)
_SOLUTION_REQUEST = re.compile(
    r"(?:给出|展示|写出|补全|实现|解释|讲解|分析).{0,8}"
    r"(?:答案|解答|解法|步骤|解题过程|参考代码|完整代码)"
    r"|(?:代码|程序)(?:怎么写|如何写)|为什么(?:选|答案|结果)"
)
_CLAUSE_MARKER = re.compile(r"讲|解释|说明|分析|说说|聊聊")
_CLAUSE_SPLIT = re.compile(r"[，,。；;！!？?\s]+|和|以及|与|并|然后|再|顺便|另外")
_NEGATED_CLAUSE = re.compile(r"(不用|不需要|不必|不要|别|无需)[^。；，,]{0,8}")

# 拒绝码与固定话术；调用方可以覆盖 message，但不能自己发明 code。
REJECTION_MESSAGES: dict[str, str] = {
    "ANSWER_NOT_REQUESTED": "学生本轮没有要答案或讲解，不能直接公布答案。",
    "ANSWER_SCOPE_DENIED": "学生这轮只要提示或考点，不能公布答案或完整解法。",
    "ANSWER_NOT_SELECTED": "当前没有选中的题目，需要先查题并选中具体一道。",
    "ANSWER_TARGET_NOT_OWNED": "这道题不属于当前会话，不能按之前的题目处理。",
    "ANSWER_VERSION_CHANGED": "题目版本已变化，需要重新查题后再看答案。",
    "ANSWER_SOURCE_INVALID": "这道题的来源当前不可用，无法展示答案。",
    "EXERCISE_SOLUTION_NOT_AUTHORIZED": "学生还没作答，也没有主动要求看参考解答。",
    "EXERCISE_NOT_FOUND": "当前没有进行中的练习。",
}
REJECTION_REMEDIES: dict[str, tuple[str, ...]] = {
    "ANSWER_NOT_REQUESTED": ("explain_problem", "view_question_detail", "ask_user"),
    "ANSWER_SCOPE_DENIED": ("explain_problem", "view_question_detail"),
    "ANSWER_NOT_SELECTED": ("search_questions", "select_question", "view_workspace"),
    "ANSWER_TARGET_NOT_OWNED": ("search_questions", "select_question"),
    "ANSWER_VERSION_CHANGED": ("search_questions",),
    "ANSWER_SOURCE_INVALID": ("search_questions", "ask_user"),
    "EXERCISE_SOLUTION_NOT_AUTHORIZED": ("evaluate_exercise", "ask_user"),
    "EXERCISE_NOT_FOUND": ("generate_exercise",),
}


def hint_only(text: str) -> bool:
    return bool(_HINT_ONLY.search(text or ""))


def wants_to_try_first(text: str) -> bool:
    """学生明确要求先自己做、暂不给答案；这是唯一的“先作答”合法理由。"""

    return bool(_TRY_FIRST.search(text or ""))


def wants_explanation(text: str) -> bool:
    return bool(_EXPLAIN_MARK.search(text or ""))


def answer_requested(text: str) -> bool:
    """学生本轮是否明确要答案/解析/参考代码。

    只是“找一道真题”属于练习优先，不能当成看答案；但同一句里出现“直接给
    答案”“查看答案”这类明确请求时放行。
    """

    value = text or ""
    if _NEGATED_ANSWER.search(value):
        return False
    if _EXPLICIT_ANSWER_AFTER_LOOKUP.search(value):
        return True
    if _LOOKUP.search(value) or re.search(r"真题", value):
        return bool(_ANSWER_ASK.search(value))
    return bool(_ANSWER_ASK.search(value)) or bool(
        re.search(r"讲解|讲讲|解释|解析|答案|参考代码", value)
    )


def scope_of(text: str) -> str | None:
    """本轮回答范围：``hint``（只要提示）、``topic``（只问考点）。

    两者都**不是**“完整讲解”授权，因此不能执行讲解或看答案。学生同时要求
    讲解/答案时返回 ``None``，按完整讲解处理。
    """

    value = text or ""
    if hint_only(value):
        return "hint"
    if not _TOPIC_OBJECT.search(value):
        return None
    has_topic = False
    wants_more = False
    for clause in _CLAUSE_SPLIT.split(value):
        # “不用给答案/不必讲步骤”是限制，不是要求。
        clause = _NEGATED_CLAUSE.sub(" ", clause)
        if not clause.strip():
            continue
        if _TOPIC_OBJECT.search(clause):
            has_topic = True
            if _SOLUTION_REQUEST.search(clause):
                wants_more = True
        elif _CLAUSE_MARKER.search(clause) or _FULL_MARKER.search(clause):
            wants_more = True
    return "topic" if has_topic and not wants_more else None


# ----------------------------------------------------------------------
# 授权决定
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class AnswerGrant:
    """一次本轮有效的授权凭据；可写进 transcript 的只有这些元数据。"""

    kind: str
    scope: str
    request_id: str
    target_id: str
    build_id: str | None = None
    content_sha256: str | None = None
    selection_id: str | None = None
    granted_by: str = "student_text"

    def public(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scope": self.scope,
            "target_id": self.target_id,
            "build_id": self.build_id,
            "granted_by": self.granted_by,
        }


@dataclass(frozen=True)
class GateDecision:
    grant: AnswerGrant | None = None
    code: str | None = None
    message: str = ""
    needs_user: bool = False
    retryable: bool = False
    remedies: tuple[str, ...] = ()
    facts: dict[str, Any] | None = None

    @property
    def allowed(self) -> bool:
        return self.grant is not None

    @property
    def rejected(self) -> bool:
        return self.grant is None and self.code is not None


def _deny(code: str, *, facts: dict[str, Any] | None = None, needs_user: bool = False) -> GateDecision:
    return GateDecision(
        code=code,
        message=REJECTION_MESSAGES.get(code, "这个操作当前不可用。"),
        needs_user=needs_user,
        retryable=False,
        remedies=REJECTION_REMEDIES.get(code, ()),
        facts=facts,
    )


def _allow(
    *,
    kind: str,
    scope: str,
    request_id: str,
    target_id: str,
    build_id: str | None,
    content_sha256: str | None,
    selection_id: str | None,
) -> GateDecision:
    return GateDecision(
        grant=AnswerGrant(
            kind=kind,
            scope=scope,
            request_id=request_id,
            target_id=target_id,
            build_id=build_id,
            content_sha256=content_sha256,
            selection_id=selection_id,
        )
    )


def question_answer_gate(
    *,
    conversation_id: str,
    request_id: str,
    student_text: str,
    selection: Any,
    card: Any,
) -> GateDecision:
    """真题答案门禁。``card`` 必须是本轮按当前题库重新解析出的公开卡片。"""

    if selection is None:
        return _deny("ANSWER_NOT_SELECTED")
    if str(getattr(selection, "conversation_id", "")) != conversation_id:
        return _deny("ANSWER_TARGET_NOT_OWNED")
    if card is None:
        return _deny("ANSWER_SOURCE_INVALID")
    if (
        str(getattr(card, "build_id", "") or "") != str(getattr(selection, "build_id", "") or "")
        or str(getattr(card, "content_sha256", "") or "")
        != str(getattr(selection, "content_sha256", "") or "")
    ):
        return _deny("ANSWER_VERSION_CHANGED")
    source = getattr(card, "source", None)
    if source is None or not bool(getattr(source, "may_show_answer", False)):
        return _deny("ANSWER_SOURCE_INVALID")
    if not bool(getattr(getattr(card, "answer", None), "available", False)):
        return _deny("ANSWER_SOURCE_INVALID")
    scope = scope_of(student_text)
    if scope in ("hint", "topic") or wants_to_try_first(student_text):
        return _deny("ANSWER_SCOPE_DENIED")
    if not answer_requested(student_text):
        return _deny("ANSWER_NOT_REQUESTED")
    return _allow(
        kind="question_answer",
        scope="answer",
        request_id=request_id,
        target_id=str(getattr(selection, "question_id", "") or ""),
        build_id=str(getattr(selection, "build_id", "") or ""),
        content_sha256=str(getattr(selection, "content_sha256", "") or ""),
        selection_id=str(getattr(selection, "id", "") or ""),
    )


def question_explain_gate(
    *,
    conversation_id: str,
    request_id: str,
    student_text: str,
    selection: Any,
    card: Any,
) -> GateDecision:
    """真题讲解门禁：只允许公开题面 + 讲解，不读存档答案。

    返回的 grant 用于最终回复标记“本轮讲解过哪道题”；``scope`` 为
    ``hint``/``topic`` 时调用方只能给公开提示，不能调用讲解模型。
    """

    if selection is None:
        return _deny("ANSWER_NOT_SELECTED")
    if str(getattr(selection, "conversation_id", "")) != conversation_id:
        return _deny("ANSWER_TARGET_NOT_OWNED")
    if card is None:
        return _deny("ANSWER_SOURCE_INVALID")
    if (
        str(getattr(card, "build_id", "") or "") != str(getattr(selection, "build_id", "") or "")
        or str(getattr(card, "content_sha256", "") or "")
        != str(getattr(selection, "content_sha256", "") or "")
    ):
        return _deny("ANSWER_VERSION_CHANGED")
    scope = scope_of(student_text)
    if not wants_explanation(student_text) and scope is None:
        return _deny("ANSWER_NOT_REQUESTED")
    return _allow(
        kind="question_explain",
        scope=scope or "explain",
        request_id=request_id,
        target_id=str(getattr(selection, "question_id", "") or ""),
        build_id=str(getattr(selection, "build_id", "") or ""),
        content_sha256=str(getattr(selection, "content_sha256", "") or ""),
        selection_id=str(getattr(selection, "id", "") or ""),
    )


_SOLUTION_NOUN = re.compile(r"参考解答|参考答案|解答|答案|解析|解法|参考代码")
_SOLUTION_VERB = re.compile(r"看|查看|给|要|展示|显示|公布|对|提供|发|讲|说说|写出")
_SOLUTION_NEGATED = re.compile(
    r"^(?:暂不|暂时不|先不|先别|不用|不需要|不必|不要|别|不想|无需|不急着)"
)


def solution_requested(text: str) -> bool:
    """学生本轮是否主动要看参考解答（逐分句判断，否定分句不算请求）。"""

    for clause in _CLAUSE_SPLIT.split(text or ""):
        clause = clause.strip()
        if not clause or _SOLUTION_NEGATED.match(clause):
            continue
        if _SOLUTION_NOUN.search(clause) and _SOLUTION_VERB.search(clause):
            return True
        if re.search(r"(参考解答|解答|答案)(呢|是什么|是什么样|怎么)", clause):
            return True
    return False


def solution_negated(text: str) -> bool:
    """「暂不展示参考解答」「不用给解答」是对展示的限制，不是请求。"""

    for clause in _CLAUSE_SPLIT.split(text or ""):
        clause = clause.strip()
        if clause and _SOLUTION_NEGATED.match(clause) and _SOLUTION_NOUN.search(clause):
            return True
    return False


def _json_object(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _finished_evaluation(raw: Any) -> ExerciseEvaluation | None:
    """只有完整、可解析且明确“不需要补充”的评价才算完成。

    评价缺失、畸形或字段不全时默认封闭（F006：评价失败不等于练习已完成）。
    """

    value = _json_object(raw)
    if not value or "needs_supplement" not in value:
        return None
    try:
        evaluation = ExerciseEvaluation.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        return None
    if not (
        evaluation.final_answer_assessment.strip()
        and evaluation.step_by_step_analysis.strip()
        and evaluation.guidance.strip()
    ):
        return None
    return evaluation


def _completed_attempt(attempt: Any) -> bool:
    """一次真正完成的作答：有内容，且评价完整并明确没有要求补充。"""

    def field(name: str) -> Any:
        if isinstance(attempt, dict):
            return attempt.get(name)
        return getattr(attempt, name, None)

    has_content = bool(
        str(field("student_answer_text") or "").strip()
        or str(field("student_solution_text") or "").strip()
        or field("attachments")
    )
    if not has_content:
        return False
    evaluation = _finished_evaluation(field("evaluation_json"))
    return evaluation is not None and not evaluation.needs_supplement


def _has_completed_attempt(exercise: Any) -> bool:
    attempts = list(getattr(exercise, "attempts", None) or [])
    if any(_completed_attempt(attempt) for attempt in attempts):
        return True
    if attempts:
        return False
    # 兼容只镜像顶层字段的旧记录。
    return _completed_attempt(
        {
            "student_answer_text": getattr(exercise, "student_answer_text", None),
            "student_solution_text": getattr(exercise, "student_solution_text", None),
            "attachments": getattr(exercise, "attachments", None),
            "evaluation_json": getattr(exercise, "evaluation_json", None),
        }
    )


def exercise_solution_gate(
    *,
    request_id: str,
    student_text: str,
    exercise: Any,
) -> GateDecision:
    """F006 练习参考解答门禁：已完成作答或学生主动要求；否定请求不放行。"""

    if exercise is None:
        return _deny("EXERCISE_NOT_FOUND")
    if wants_to_try_first(student_text) or solution_negated(student_text):
        return _deny("EXERCISE_SOLUTION_NOT_AUTHORIZED")
    status = str(getattr(exercise, "status", "") or "")
    if status == "revealed" or _has_completed_attempt(exercise) or solution_requested(
        student_text
    ):
        return _allow(
            kind="exercise_solution",
            scope="solution",
            request_id=request_id,
            target_id=str(getattr(exercise, "id", "") or ""),
            build_id=None,
            content_sha256=None,
            selection_id=None,
        )
    return _deny("EXERCISE_SOLUTION_NOT_AUTHORIZED")


def rejection_payload(decision: GateDecision) -> dict[str, Any]:
    """转成 role=tool 的拒绝信封；只含公开定位事实，不含答案。"""

    return {
        "status": "failed",
        "error_code": decision.code,
        "message": decision.message,
        "needs_user": decision.needs_user,
        "retryable": decision.retryable,
        "rejected": True,
        "remedies": list(decision.remedies),
        "facts": dict(decision.facts or {}),
    }