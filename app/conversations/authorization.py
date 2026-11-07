"""F010 D：受保护动作的独立授权。

授权只看**本轮学生原话**和本轮已经提交的事实，不看模型填的参数，也不看
``basis``。``basis`` 只负责追溯；即使引用了一句真实存在的话，只要那句话
是否定/假设/转述，这里仍然拒绝。

拒绝分两类：
- ``needs_user=False``：参数或引用可改，模型可以在同一轮换参数重试；
- ``needs_user=True``：目标本身有歧义，需要学生回答后才能继续。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.conversations.basis import PROTECTED_ACTIONS

_NEGATION = re.compile(
    r"(别|不要|不用|不需要|不必|先不|暂不|取消|停止|不想|算了|别再|先别|不用给|不要给|不给)"
)

_SAVE = re.compile(r"(保存|存起来|存一下|存到|加入错题|记到错题|记错题|收藏)")
# “练习”单独出现不等于要出新题：例如“查看上次练习的结果”。只有明确的
# 出题/练习动作才算请求；裸词“练习”不再授权。
_EXERCISE = re.compile(
    r"(再来一道|来一道|来道|出道题|出道|出一道|出题|出练习|练一道|练一下|练练|"
    r"练习一下|再练习|再练|想练|要练|类似题|类似的?题|同类型|变式|做题|刷题|再做一道)"
)
_ANSWER = re.compile(
    r"(答案|参考答案|参考代码|参考解答|看答案|告诉我答案|完整解法|看解答)"
)
_DELETE = re.compile(r"(删除|删掉|删了|移除|去掉|不要这条|别算|不算|不参与|别记)")
_CORRECT = re.compile(r"(改一下|改成|纠正|更正|记错|修改|更新)")

# 这些是“问保存/删除本身会怎样”的元问题或回忆，不是操作请求。
_SAVE_META = re.compile(
    r"(会发生|会怎样|会怎么样|有什么用|是什么意思|是什么|什么时候|为什么|"
    r"是否|如果|假如|假设|想知道|问一下|可以吗|行吗|好吗)"
)
_RECALL_META = re.compile(
    r"(什么时候|何时|为什么|是不是|有没有|删除的|删掉的|删除过|修改的|改过的)"
)


@dataclass(frozen=True)
class AuthDecision:
    code: str
    message: str
    needs_user: bool = False
    remedies: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)


def _negated(text: str, match: re.Match[str]) -> bool:
    prefix = text[max(0, match.start() - 12):match.start()]
    return bool(_NEGATION.search(prefix))


def _requested(pattern: re.Pattern[str], text: str) -> bool:
    return any(not _negated(text, match) for match in pattern.finditer(text or ""))


def requested_question_types(text: str) -> set[str]:
    """学生明确要求的题型集合，仅作目标依据核验。"""
    labels = {"choice": r"选择题|单选题|多选题", "fill": r"填空题?", "code": r"编程题|代码题|程序设计题"}
    return {kind for kind, pattern in labels.items() if _requested(re.compile(pattern), text)}


def requested_question_type(text: str) -> str | None:
    """核对用户的选型依据，模型填入的题型本身不能消除同号歧义。"""
    requested = requested_question_types(text)
    return next(iter(requested)) if len(requested) == 1 else None


def requested_question_ref(text: str, ref: str) -> bool:
    """仅学生明确点名的短引用可当作选题依据。"""

    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(ref) + r"(?![0-9])", re.IGNORECASE)
    return _requested(pattern, text)


def _step_action(step: dict[str, Any]) -> str:
    plan = step.get("plan")
    if isinstance(plan, dict):
        return str(plan.get("action") or "")
    return str(step.get("action") or "")


def _step_status(step: dict[str, Any]) -> str:
    outcome = step.get("outcome")
    if isinstance(outcome, dict):
        return str(outcome.get("status") or "")
    return ""


def _succeeded(transcript: list[dict[str, Any]], action: str) -> bool:
    return any(
        isinstance(step, dict)
        and _step_action(step) == action
        and _step_status(step) == "success"
        for step in transcript
    )


def _deny(
    action: str,
    code: str,
    message: str,
    remedies: list[str],
    *,
    needs_user: bool = False,
) -> AuthDecision:
    return AuthDecision(
        code=code,
        message=message,
        needs_user=needs_user,
        remedies=remedies,
        facts={"action": action},
    )


_REFUSAL = re.compile(r"(不用|不需要|算了|不要|别|取消|先不)")
_META = re.compile(
    r"(会发生|会怎样|有什么用|是什么意思|是什么|什么时候|为什么|是否|"
    r"如果|假如|假设|想知道|问一下|可以吗|行吗|好吗|查看|结果|记录|历史|上次)"
)


def needs_user_for_denial(action: str, text: str, default: bool) -> bool:
    """拒绝写操作时是否先追问，而不是直接报业务失败。

    只服务于授权结果，不决定下一步调用哪个工具；没有明确请求时先问一句，
    学生已拒绝或在问元问题时直接失败。
    """

    if default or not text:
        return default
    if action not in {"record_mistake", "generate_exercise"}:
        return default
    return not _REFUSAL.search(text) and not _META.search(text)


def authorize_action(
    action: str,
    text: str,
    *,
    transcript: list[dict[str, Any]],
    entry_id: str | None = None,
) -> AuthDecision | None:
    """返回拒绝决定；``None`` 表示本轮原话足以授权。"""

    if action not in PROTECTED_ACTIONS:
        return None
    current = (text or "").strip()

    if action == "record_mistake":
        if not _requested(_SAVE, current) or _SAVE_META.search(current):
            return _deny(
                action,
                "AUTHORIZATION_REQUIRED",
                "这轮你没有明确说要保存错题，我不会写入错题本。要保存的话，直接告诉我。",
                ["ask_user"],
            )
        return None

    if action == "generate_exercise":
        if not _requested(_EXERCISE, current):
            return _deny(
                action,
                "AUTHORIZATION_REQUIRED",
                "这轮你没有要练习，我不会出题。要练一道的话，直接告诉我。",
                ["ask_user", "explain_problem"],
            )
        return None

    if action in {"view_question_answer", "reveal_solution"}:
        if action == "reveal_solution" and _succeeded(transcript, "evaluate_exercise"):
            return None
        if not _requested(_ANSWER, current):
            return _deny(
                action,
                "ANSWER_NOT_REQUESTED",
                "这轮你没有要看答案或完整解法，我不会公开答案。",
                ["explain_problem", "ask_user"],
            )
        return None

    if action == "delete_memory":
        if not _requested(_DELETE, current) or _RECALL_META.search(current):
            return _deny(
                action,
                "AUTHORIZATION_REQUIRED",
                "要把这条从学习记忆和复习建议里移除吗？原会话记录会保留。",
                ["review_learning_memory", "ask_user"],
                needs_user=True,
            )
        return None

    if action == "correct_memory":
        if not _requested(_CORRECT, current) or _RECALL_META.search(current):
            return _deny(
                action,
                "AUTHORIZATION_REQUIRED",
                "这轮你没有明确要纠正这条学习记忆，我不会改写。",
                ["review_learning_memory", "ask_user"],
            )
        return None

    return None


__all__ = ["AuthDecision", "authorize_action"]
