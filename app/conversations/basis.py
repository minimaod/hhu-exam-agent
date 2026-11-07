"""F010 D：受保护动作的依据“追溯”校验。

``basis`` 只回答“模型声称的依据能否在本轮材料里找到”，**不构成授权**。
能不能保存、出题、看答案，由执行层的 ``authorization`` 独立判断；依据
找不到时返回可纠正的拒绝，模型可以在同一轮改参数重试。
"""

from __future__ import annotations

from typing import Any

PROTECTED_ACTIONS = frozenset(
    {
        "generate_exercise",
        "record_mistake",
        "view_question_answer",
        "reveal_solution",
        "delete_memory",
        "correct_memory",
    }
)

_BUTTON_ACTIONS = {"select_question", "list_paper_questions", "search_questions"}


def _text_of(basis: dict[str, Any]) -> str:
    return str(basis.get("quote") or "").strip()


def _steps(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in transcript if isinstance(step, dict)]


def _plan_action(step: dict[str, Any]) -> str:
    plan = step.get("plan")
    if isinstance(plan, dict):
        return str(plan.get("action") or "")
    return str(step.get("action") or "")


def _step_call_id(step: dict[str, Any]) -> str:
    return str(step.get("call_id") or step.get("step_id") or "")


def _step_status(step: dict[str, Any]) -> str:
    outcome = step.get("outcome")
    if isinstance(outcome, dict):
        return str(outcome.get("status") or "")
    return ""


def _suggestion_id(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("id", "suggestion_id", "value", "label"):
            value = item.get(key)
            if value:
                return str(value)
        return ""
    return str(item)


def validate_basis(
    basis: Any,
    *,
    current_text: str,
    transcript: list[dict[str, Any]],
    suggestions: list[Any],
) -> tuple[str, str] | None:
    """返回 ``(error_code, message)``；通过或未提供时返回 ``None``。"""

    if basis is None:
        return None
    if not isinstance(basis, dict):
        return ("BASIS_REQUIRED", "依据必须是对象：引用学生原话，或指明已确认的建议。")
    kind = str(basis.get("kind") or "").strip()
    if kind == "student_request":
        quote = _text_of(basis)
        if len(quote) < 2 or quote not in (current_text or ""):
            return ("BASIS_UNVERIFIED", "依据里的原话在本轮学生消息中找不到，请重新引用。")
        return None
    if kind == "confirmed_suggestion":
        suggestion_id = str(basis.get("suggestion_id") or "").strip()
        ids = [value for value in (_suggestion_id(item) for item in suggestions) if value]
        if not suggestion_id:
            if len(ids) >= 2:
                return ("AMBIGUOUS_BASIS", "上一轮有多个建议，请先说明确认的是哪一条。")
            return ("BASIS_UNVERIFIED", "没有可核对的建议编号，请重新确认。")
        # 编号必须精确对上一个真实选项，不能靠子串蒙混（“1”不能命中“10”）。
        if suggestion_id not in ids:
            if len(ids) >= 2:
                return ("AMBIGUOUS_BASIS", "这个建议编号对不上上一轮的选项，请重新确认。")
            return ("BASIS_UNVERIFIED", "这个建议编号对不上上一轮的选项，请重新确认。")
        return None
    if kind == "tool_result":
        call_id = str(basis.get("depends_on_call_id") or "").strip()
        matched = [step for step in _steps(transcript) if _step_call_id(step) == call_id]
        if not call_id or not matched:
            return ("BASIS_UNVERIFIED", "依据指向的工具结果不在本轮已完成的调用里。")
        if not any(_step_status(step) == "success" for step in matched):
            return ("BASIS_UNVERIFIED", "依据指向的调用没有成功完成，不能作为依据。")
        return None
    if kind == "button":
        if not any(
            _plan_action(step) in _BUTTON_ACTIONS and _step_status(step) == "success"
            for step in _steps(transcript)
        ):
            return ("BASIS_UNVERIFIED", "没有本轮真实题卡可以证明这是按钮操作。")
        return None
    return (
        "BASIS_UNVERIFIED",
        "未知的依据类型，请使用 student_request/confirmed_suggestion/button/tool_result。",
    )


__all__ = ["PROTECTED_ACTIONS", "validate_basis"]