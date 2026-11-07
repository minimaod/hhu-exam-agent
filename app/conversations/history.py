"""F010 B：跨轮消息历史的只读投影。

安全边界（本模块的核心）：
- **只从已持久化字段派生**，不读模型、不写库；刷新、重启、回放都得到同一份历史。
- **不跨轮复用可能含解答的正文**。真题讲解/答案、练习提交后的参考答案，
  以及无法判断授权范围的旧练习轮次，一律替换成可核对的结构化摘要；
  其余轮次保留正文（截断）。
- **工具参数与结果逐字段白名单化**：答案正文、学生私有作答、临时 URL
  不会进入历史。
- **配对不变量**：每条 ``tool`` 消息前必须有携带同一 ``tool_call_id`` 的
  ``assistant(tool_calls)``；不满足就整对丢弃。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from app.conversations.models import Turn

HISTORY_MAX_CHARS = 6000
HISTORY_MAX_MESSAGES = 24
HISTORY_TARGET_TURNS = 4
HISTORY_MAX_TURNS = 8
HISTORY_MAX_SUMMARIES = 8
ASSISTANT_CHAR_LIMIT = 600
TOOL_RESULT_CHAR_LIMIT = 1200
ARG_CHAR_LIMIT = 120
SUMMARY_CHAR_LIMIT = 120
MAX_STEPS_PER_TURN = 6

# 可跨轮复用的参数：只登记与“找什么”有关的事实，不登记学生作答、备注原文。
HISTORY_ARG_FIELDS: dict[str, set[str]] = {
    "introduce": {"course"},
    "query_papers": {
        "course", "exam_type", "academic_year", "term", "variant", "paper_id",
        "all_exam_types",
    },
    "search_questions": {
        "course",
        "exam_type",
        "question_type",
        "number_label",
        "paper_id",
        "offset",
        "continue_from_previous",
        "query",
        "scope",
        "exclude_question_id",
        "exclude_previous",
        "defer_answer",
    },
    "list_paper_questions": {
        "course", "paper_id", "question_type", "offset",
        "continue_from_previous", "limit",
    },
    "select_question": {
        "ref", "index", "number_label", "question_type", "paper_id",
    },
    "explain_problem": {"mode", "course", "scope", "ref", "target"},
    "followup_problem": {"target", "ref"},
    "generate_exercise": {"from_question", "ref"},
    "evaluate_exercise": set(),
    "reveal_solution": {"ref"},
    "view_question_answer": {"ref"},
    "record_mistake": set(),
    "record_feedback": {"understood"},
    "clarify_or_reply": {"question", "options"},
    "review_memory": {"course", "days", "limit"},
    "correct_memory": {"entry_id", "course", "knowledge_point"},
    "delete_memory": {"entry_id"},
    "view_workspace": set(),
    "view_question_detail": {"ref"},
    "list_shown_questions": {"question_type", "offset", "limit"},
    "view_learning_summary": {"course", "days", "limit"},
}

# 工具结果的白名单：四个只读工具由处理器深度投影后整体放行；
# 其余动作只保留与“做了什么”有关的公开事实。
HISTORY_DETAIL_FIELDS: dict[str, set[str]] = {
    "query_papers": {"total", "course", "exam_type", "papers"},
    "search_questions": {
        "count",
        "total",
        "offset",
        "returned",
        "next_offset",
        "has_more",
        "paper_id",
        "paper_title",
        "question_type",
        "number_label",
        "selected",
        # F010 C：跨轮只保留公开题面的压缩投影。
        "questions",
    },
    "list_paper_questions": {
        "count",
        "total",
        "offset",
        "returned",
        "next_offset",
        "has_more",
        "paper_id",
        "paper_title",
        "question_type",
        "questions",
    },
    "select_question": {
        "question_id",
        "number_label",
        "question_type",
        "paper_id",
        "paper_title",
        "ref",
        "question",
    },
    "explain_problem": {"question_id", "number_label", "mode", "problem_id"},
    "followup_problem": {"followup_target", "problem_id"},
    "generate_exercise": {"exercise_id", "question_type", "knowledge_point"},
    "evaluate_exercise": {"needs_supplement", "independence", "verdict"},
    "reveal_solution": {"exercise_id"},
    "view_question_answer": {"question_id", "number_label"},
    "record_mistake": {"record_id"},
    "record_feedback": {"problem_id"},
    "clarify_or_reply": set(),
    "review_memory": {
        "status",
        "memory_course",
        "memory_counts",
        "memory_suggestions",
        "memory_pending",
    },
    "correct_memory": {"memory_entry_id"},
    "delete_memory": {"memory_entry_id", "memory_removed"},
    "view_workspace": {"workspace", "count"},
    "view_question_detail": {
        "ref",
        "question_id",
        "build_id",
        "content_sha256",
        "question_type",
        "number_label",
        "statement",
        "options",
        "code_lines",
        "blank_marks",
        "formulas",
        "pages",
        "knowledge_points",
        "source",
        "answer",
        "selected",
        "from_snapshot",
        "error_code",
    },
    "list_shown_questions": {"total", "offset", "returned", "has_more", "shown"},
    "view_learning_summary": {
        "status",
        "memory_course",
        "memory_range",
        "memory_counts",
        "memory_groups",
        "memory_suggestions",
        "memory_pending",
        "memory_entries",
    },
}

_DISCLOSURE_ACTIONS = {
    "view_question_answer",
    "reveal_solution",
    "explain_problem",
    "followup_problem",
    "evaluate_exercise",
}
_EXERCISE_KINDS = {"exercise", "exercise_answer", "reveal_solution"}
_TUTORING_ANSWER_KINDS = {"explanation", "followup_answer"}


def disclosure_of(turn: Turn) -> str | None:
    """判断这一轮是否展示过可能含解答的内容。

    返回 ``"answer"``（真题讲解/答案）、``"exercise"``（练习解答已开放）、
    ``"unknown"``（旧练习轮次，授权范围无法核对）或 ``None``（可复用正文）。
    """

    exercise = turn.exercise
    if exercise is not None:
        status = str(getattr(exercise, "status", "pending_answer"))
        if (
            status in ("submitted", "revealed")
            or getattr(exercise, "revealed_solution_at", None)
            or getattr(exercise, "reference_solution_text", None)
            or getattr(exercise, "answered_after_revealed", False)
        ):
            return "exercise"
    if turn.evaluation is not None:
        return "exercise"
    actions = {
        item.action for item in turn.composite_results if item.status == "success"
    }
    if actions & {"evaluate_exercise", "reveal_solution"}:
        return "exercise"
    if actions & _DISCLOSURE_ACTIONS:
        return "answer"
    tutoring = turn.tutoring
    if tutoring is not None and getattr(tutoring, "kind", None) in _TUTORING_ANSWER_KINDS:
        return "answer"
    if (
        turn.kind in _EXERCISE_KINDS
        and exercise is None
        and turn.evaluation is None
    ):
        # 旧记录只有 kind，没有解答状态：无法核对授权范围，按最保守的方式处理。
        return "unknown"
    return None


def project_turn(turn: Turn, transcript: dict[str, Any] | None) -> dict[str, Any] | None:
    """把一轮投影成历史块；没有可复用内容的轮次返回 ``None``。"""

    kind = disclosure_of(turn)
    steps = _project_steps(transcript)
    assistant = _assistant_text(turn, kind, steps)
    if not assistant and not steps and not turn.query:
        return None
    return {
        "request_id": turn.request_id,
        "seq": turn.seq,
        "query": (turn.query or "").strip(),
        "assistant": assistant,
        "disclosure": kind,
        "steps": steps,
    }


def build_history(
    turns: Iterable[Turn],
    transcripts: dict[str, dict[str, Any]],
    *,
    current_request_id: str,
    max_chars: int = HISTORY_MAX_CHARS,
    max_messages: int = HISTORY_MAX_MESSAGES,
) -> list[dict[str, Any]]:
    """组装决策模型看到的消息序列；只做读时投影，不产生任何写入。"""

    projected: list[dict[str, Any]] = []
    for turn in sorted(turns, key=lambda item: item.seq):
        if turn.status != "completed" or turn.request_id == current_request_id:
            continue
        block = project_turn(turn, transcripts.get(turn.request_id))
        if block is not None:
            projected.append(block)

    if not projected:
        return []

    recent = projected[-HISTORY_MAX_TURNS:]
    blocks: list[list[dict[str, Any]]] = []
    total = 0
    for block in reversed(recent):
        messages = _block_messages(block)
        if not messages:
            continue
        if not blocks:
            messages = _fit_newest(messages)
        elif total + len(messages) > max_messages:
            break
        blocks.append(messages)
        total += len(messages)
    blocks.reverse()

    kept = len(blocks)
    older = projected[: max(0, len(projected) - kept)]
    summaries = _summaries(older)
    messages: list[dict[str, Any]] = list(summaries)
    for block in blocks:
        messages.extend(block)
    return _trim(messages, max_chars=max_chars, max_messages=max_messages)


def history_stats(messages: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "messages": len(messages),
        "chars": sum(len(str(item.get("content") or "")) for item in messages),
        "tool_calls": sum(len(item.get("tool_calls") or []) for item in messages),
    }


# ----------------------------------------------------------------------
# 内部：步骤投影
# ----------------------------------------------------------------------
def _project_steps(transcript: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(transcript, dict):
        return []
    raw_steps = transcript.get("steps")
    if not isinstance(raw_steps, list):
        return []
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(raw_steps[-MAX_STEPS_PER_TURN:]):
        if not isinstance(step, dict):
            continue
        plan = step.get("plan") if isinstance(step.get("plan"), dict) else {}
        action = str(plan.get("action") or step.get("action") or "")
        if not action:
            continue
        outcome = step.get("outcome") if isinstance(step.get("outcome"), dict) else {}
        raw_details = outcome.get("details") if isinstance(outcome.get("details"), dict) else {}
        details = _project_details(action, raw_details)
        payload = {
            "status": _status_text(outcome.get("status")),
            "message": _clip(outcome.get("message"), 200),
            "details": details,
            "error_code": outcome.get("error_code") or raw_details.get("error_code"),
            "retryable": bool(outcome.get("retryable") or raw_details.get("retryable")),
            "needs_user": bool(outcome.get("needs_user")),
        }
        steps.append(
            {
                "id": str(step.get("id") or f"step-{index + 1}"),
                "tool": str(step.get("tool") or action),
                "action": action,
                "call_id": str(step.get("call_id") or step.get("id") or f"call-{index + 1}"),
                "arguments": _project_arguments(action, plan.get("parameters")),
                "payload": payload,
                "carried": bool(step.get("carried")),
            }
        )
    return steps


def _project_arguments(action: str, parameters: Any) -> dict[str, Any]:
    if not isinstance(parameters, dict):
        return {}
    allowed = HISTORY_ARG_FIELDS.get(action, set())
    result: dict[str, Any] = {}
    for key in allowed:
        if key not in parameters:
            continue
        value = parameters[key]
        if isinstance(value, str):
            value = value[:ARG_CHAR_LIMIT]
        elif isinstance(value, list):
            value = [str(item)[:ARG_CHAR_LIMIT] for item in value[:5]]
        result[key] = value
    return result


def _project_details(action: str, details: Any) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    allowed = HISTORY_DETAIL_FIELDS.get(action, set())
    result: dict[str, Any] = {}
    for key in allowed:
        if key not in details:
            continue
        value = details[key]
        if key in {"workspace", "shown", "options", "knowledge_points", "pages", "source", "answer"}:
            # 只读工具已在处理器里逐字段投影；这里整体保留（仍只含公开字段）。
            result[key] = value
            continue
        if key == "questions":
            # 跨轮历史只留定位与一小段题干；不保留答案可用性之外的任何正文。
            entries: list[dict[str, Any]] = []
            for item in list(value or [])[:6]:
                if not isinstance(item, dict):
                    continue
                entries.append(
                    {
                        "question_id": str(item.get("question_id") or "")[:80],
                        "number_label": str(item.get("number_label") or "")[:20],
                        "question_type": str(item.get("question_type") or "")[:20],
                        "statement": str(item.get("statement") or "")[:120],
                        "ref": str(item.get("ref") or "")[:20],
                        "source": {
                            "paper_id": str(
                                (item.get("source") or {}).get("paper_id") or ""
                            )[:80],
                            "paper_title": str(
                                (item.get("source") or {}).get("paper_title") or ""
                            )[:120],
                        },
                    }
                )
            if entries:
                result[key] = entries
            continue
        if key == "question" and isinstance(value, dict):
            result[key] = {
                "question_id": str(value.get("question_id") or "")[:80],
                "number_label": str(value.get("number_label") or "")[:20],
                "question_type": str(value.get("question_type") or "")[:20],
                "statement": str(value.get("statement") or "")[:120],
                "ref": str(value.get("ref") or "")[:20],
            }
            continue
        result[key] = value if not isinstance(value, str) else value[:ARG_CHAR_LIMIT]
    return result


def project_step(
    *,
    step_id: str,
    tool: str,
    action: str,
    call_id: str,
    parameters: Any,
    outcome: Any,
    carried: bool = False,
    from_request_id: str | None = None,
) -> dict[str, Any]:
    """把单个工具步骤投影成可落盘的公开形状。

    ``_transcript`` 在写入 SQLite 前就走这里；读取时的 ``_project_steps``
    仍会再投影一次，作为纵深防御，但数据库里不再保存原始参数/结果。
    """

    raw = outcome if isinstance(outcome, dict) else {}
    raw_details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    stored_outcome = {
        "status": str(raw.get("status") or "failed"),
        "message": _clip(raw.get("message"), 200),
        "details": _project_details(action, raw_details),
        "error_code": raw.get("error_code") or raw_details.get("error_code"),
        "retryable": bool(raw.get("retryable") or raw_details.get("retryable")),
        "needs_user": bool(raw.get("needs_user")),
        "executed": bool(raw.get("executed", True)),
    }
    step: dict[str, Any] = {
        "id": str(step_id),
        "call_id": str(call_id),
        "tool": str(tool),
        "plan": {
            "action": str(action),
            "parameters": _project_arguments(action, parameters),
        },
        "outcome": stored_outcome,
    }
    if carried:
        step["carried"] = True
        step["from_request_id"] = from_request_id
    return step


def _status_text(status: Any) -> str:
    return {"success": "ok", "clarification": "needs_user", "failed": "failed"}.get(
        str(status), "failed"
    )


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


# ----------------------------------------------------------------------
# 内部：历史正文
# ----------------------------------------------------------------------
def _assistant_text(
    turn: Turn, kind: str | None, steps: list[dict[str, Any]]
) -> str:
    if kind == "answer":
        return _answer_line(turn, steps)
    if kind == "exercise":
        return _exercise_line(turn)
    if kind == "unknown":
        return "（旧练习轮次：正文与参考解答不跨轮复用）"
    text = (turn.assistant_message or "").strip()
    if not text and turn.result is not None:
        text = str(turn.result.message or "").strip()
    if not text and turn.tutoring is not None:
        text = str(getattr(turn.tutoring, "message", "") or "").strip()
    return _clip(text, ASSISTANT_CHAR_LIMIT)


def _answer_line(turn: Turn, steps: list[dict[str, Any]]) -> str:
    number = ""
    if turn.question is not None:
        number = str(turn.question.number_label or "")
    if not number and turn.question_results:
        number = str(turn.question_results[0].number_label or "")
    if not number:
        for step in steps:
            details = step.get("payload", {}).get("details") or {}
            if details.get("number_label"):
                number = str(details["number_label"])
                break
    where = f"第 {number} 题" if number else "该题"
    return f"（已向学生展示{where}的讲解/答案；正文不跨轮复用，需要时用当前工具重新获取）"


def _exercise_line(turn: Turn) -> str:
    exercise = turn.exercise
    status = str(getattr(exercise, "status", "") or "")
    opened = bool(
        status in ("submitted", "revealed")
        or getattr(exercise, "revealed_solution_at", None)
        or getattr(exercise, "reference_solution_text", None)
    )
    verdict = ""
    evaluation = turn.evaluation or getattr(exercise, "evaluation", None)
    if evaluation is not None:
        if evaluation.is_correct is True:
            verdict = "；判定=正确"
        elif evaluation.is_correct is False:
            verdict = "；判定=错误"
        else:
            verdict = "；判定=依据不足"
        if evaluation.needs_supplement:
            verdict += "（待补充）"
    return (
        f"（练习状态={status or '未知'}；参考答案已开放={'是' if opened else '否'}"
        f"{verdict}；正文不跨轮复用）"
    )


# ----------------------------------------------------------------------
# 内部：消息与预算
# ----------------------------------------------------------------------
def _block_messages(block: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    query = block.get("query") or "（学生本轮没有文字）"
    messages.append({"role": "user", "content": _clip(query, 300)})
    for step in block.get("steps") or []:
        call_id = step["call_id"]
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": step["tool"],
                            "arguments": json.dumps(
                                step.get("arguments") or {}, ensure_ascii=False
                            ),
                        },
                    }
                ],
            }
        )
        body = json.dumps(step.get("payload") or {}, ensure_ascii=False)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _clip(body, TOOL_RESULT_CHAR_LIMIT),
            }
        )
    assistant = block.get("assistant") or ""
    if assistant:
        messages.append({"role": "assistant", "content": assistant})
    return messages


def _fit_newest(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """最新一轮也必须保留；超出预算时裁掉较早的工具对，绝不留下孤立 tool。"""

    if len(messages) <= 8 and sum(len(m["content"]) for m in messages) <= 2500:
        return messages
    head = messages[:1]
    tail = messages[-1:] if messages[-1].get("role") == "assistant" else []
    middle = messages[1 : len(messages) - len(tail)]
    pairs: list[list[dict[str, Any]]] = []
    for index in range(0, len(middle) - 1, 2):
        first, second = middle[index], middle[index + 1]
        if first.get("role") == "assistant" and second.get("role") == "tool":
            pairs.append([first, second])
    pairs = pairs[-3:]
    compact: list[dict[str, Any]] = list(head)
    for pair in pairs:
        compact.extend(pair)
    compact.extend(tail)
    return compact


def _summaries(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for block in blocks[-HISTORY_MAX_SUMMARIES:]:
        tools = "、".join(
            dict.fromkeys(step["tool"] for step in block.get("steps") or [])
        )
        suffix = f" → 工具 {tools}" if tools else ""
        text = (
            f"[r{block['seq']}·{block['request_id'][:8]}] "
            f"学生「{_clip(block.get('query'), 40)}」{suffix}"
        )
        lines.append(
            {
                "role": "user",
                "content": _clip(text, SUMMARY_CHAR_LIMIT),
            }
        )
    return lines


def _trim(
    messages: list[dict[str, Any]], *, max_chars: int, max_messages: int
) -> list[dict[str, Any]]:
    messages = list(messages)
    while len(messages) > max_messages:
        messages.pop(0)
    while _chars(messages) > max_chars and len(messages) > 1:
        messages.pop(0)
    return messages


def _chars(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(item.get("content") or "")) for item in messages)