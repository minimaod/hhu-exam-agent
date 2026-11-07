"""F010 C：模型可见数据的逐字段投影。

同一条工具结果对“模型”和“落盘/历史/前端”是两种形状：

* ``public``：可以进入 transcript、跨轮历史、SSE 与前端卡片；
* ``authorized``：只随本轮 tool 消息交给模型，永不落盘。

投影是**正向白名单**：答案正文、参考代码、独立解答、评价正文、答案 URL
等字段不在白名单里，任何处理器误塞进来都会被丢掉。
"""

from __future__ import annotations

from typing import Any, Iterable

# 只允许出现在 public 结果里的题目字段。
PUBLIC_QUESTION_FIELDS = (
    "ref",
    "question_id",
    "build_id",
    "content_sha256",
    "number_label",
    "question_type",
    "statement",
    "options",
    "code_lines",
    "blank_marks",
    "knowledge_points",
    "source",
    "answer",
    "selected",
    "pages",
    "from_snapshot",
)
PUBLIC_SOURCE_FIELDS = (
    "paper_id",
    "paper_title",
    "academic_year",
    "term",
    "exam_type",
    "may_show_answer",
    "download_url",
    "question_number",
)
PRIVATE_KEYS = frozenset(
    {
        "answer_detail",
        "answer_text",
        "answer_url",
        "reference_code",
        "reference_code_pages",
        "solution_text",
        "reference_solution_text",
        "independent_solutions",
        "code_blocks",
        "code_annotations",
        "code_examples",
        "explanation",
        "steps",
        "final_answer",
        "evaluation",
        "step_by_step_analysis",
        "final_answer_assessment",
        "tutoring",
        "evidence",
        "authorized",
        "grant",
        "reply_input",
        "reply_context",
        "transcript",
        "history",
    }
)
# 这些动作的结果会携带“学生本轮已获授权”的正文；只有它们可以有 authorized。
DISCLOSURE_ACTIONS = frozenset(
    {
        "view_question_answer",
        "explain_problem",
        "followup_problem",
        "reveal_solution",
        "evaluate_exercise",
    }
)


def _pick(source: Any, fields: Iterable[str]) -> dict[str, Any]:
    if source is None:
        return {}
    if isinstance(source, dict):
        return {name: source[name] for name in fields if name in source}
    result: dict[str, Any] = {}
    for name in fields:
        value = getattr(source, name, None)
        if value is not None:
            result[name] = value
    return result


def public_options(options: Any, *, limit: int = 8, chars: int = 120) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item in list(options or [])[:limit]:
        label = str(getattr(item, "label", "") or "")
        text = str(getattr(item, "text", "") or "")
        if not label and not text:
            continue
        items.append({"label": label, "text": text[:chars]})
    return items


def public_knowledge_points(points: Any, *, limit: int = 8) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for point in list(points or [])[:limit]:
        name = str(getattr(point, "name", "") or "")
        if not name:
            continue
        items.append(
            {"name": name[:60], "status": str(getattr(point, "status", "") or "")}
        )
    return items


def public_source(source: Any) -> dict[str, Any]:
    return _pick(source, PUBLIC_SOURCE_FIELDS)


def public_answer(answer: Any) -> dict[str, Any]:
    return {
        "available": bool(getattr(answer, "available", False)),
        "state": str(getattr(answer, "state", "") or ""),
    }


def public_question(
    card: Any,
    *,
    ref: str | None = None,
    statement_limit: int = 400,
    include_options: bool = True,
) -> dict[str, Any]:
    """一道题的公开投影；``answer`` 只有可用性，没有正文。"""

    if card is None:
        return {}
    payload: dict[str, Any] = {
        "question_id": str(getattr(card, "question_id", "") or ""),
        "build_id": str(getattr(card, "build_id", "") or ""),
        "content_sha256": str(getattr(card, "content_sha256", "") or ""),
        "number_label": str(getattr(card, "number_label", "") or ""),
        "question_type": str(getattr(card, "question_type", "") or ""),
        "statement": str(getattr(card, "statement", "") or "")[:statement_limit],
        "knowledge_points": public_knowledge_points(
            getattr(card, "knowledge_points", None)
        ),
        "source": public_source(getattr(card, "source", None)),
        "answer": public_answer(getattr(card, "answer", None)),
    }
    if ref:
        payload["ref"] = str(ref)
    if include_options:
        options = public_options(getattr(card, "options", None))
        if options:
            payload["options"] = options
    code_lines = [line[:200] for line in list(getattr(card, "code_lines", None) or [])[:15]]
    if code_lines:
        payload["code_lines"] = code_lines
    return payload


def _clean_public_question(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in PUBLIC_QUESTION_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if key in ("statement", "number_label", "question_type", "question_id", "build_id"):
            result[key] = str(item)[:400]
        elif key == "options":
            result[key] = [
                {
                    "label": str(entry.get("label", ""))[:20],
                    "text": str(entry.get("text", ""))[:120],
                }
                for entry in list(item or [])[:8]
                if isinstance(entry, dict)
            ]
        elif key == "knowledge_points":
            result[key] = [
                {"name": str(entry.get("name", ""))[:60], "status": str(entry.get("status", ""))}
                for entry in list(item or [])[:8]
                if isinstance(entry, dict)
            ]
        elif key == "source":
            result[key] = _pick(item, PUBLIC_SOURCE_FIELDS)
        elif key == "answer":
            result[key] = {
                "available": bool(item.get("available")) if isinstance(item, dict) else False,
                "state": str(item.get("state", ""))[:40] if isinstance(item, dict) else "",
            }
        elif key == "code_lines":
            result[key] = [str(line)[:200] for line in list(item or [])[:15]]
        else:
            result[key] = item
    return result


def _strip_private(value: Any, *, depth: int = 0) -> Any:
    """递归去掉答案类字段；同时挡住 ``authorized`` 被误写进公开层。"""

    if depth > 6:
        return None
    if isinstance(value, dict):
        return {
            key: _strip_private(item, depth=depth + 1)
            for key, item in value.items()
            if key not in PRIVATE_KEYS
        }
    if isinstance(value, list):
        return [_strip_private(item, depth=depth + 1) for item in value[:40]]
    if isinstance(value, str):
        return value[:4000]
    return value


def project_details(action: str, details: Any) -> dict[str, Any]:
    """工具结果的公开投影；未知动作也走同一套白名单与私有字段剥离。"""

    if not isinstance(details, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in details.items():
        if key in PRIVATE_KEYS:
            continue
        if key in ("questions", "shown"):
            entries = [
                _clean_public_question(item)
                for item in list(value or [])[:10]
                if isinstance(item, dict) and item
            ]
            if entries:
                result[key] = entries
            continue
        if key == "source" and isinstance(value, dict):
            result[key] = _pick(value, PUBLIC_SOURCE_FIELDS)
            continue
        if key == "answer" and isinstance(value, dict):
            result[key] = {
                "available": bool(value.get("available")),
                "state": str(value.get("state", ""))[:40],
            }
            continue
        if key == "options":
            result[key] = public_options(
                [
                    type("_Option", (), {"label": item.get("label"), "text": item.get("text")})
                    for item in list(value or [])[:8]
                    if isinstance(item, dict)
                ]
            )
            continue
        if isinstance(value, (dict, list)):
            result[key] = _strip_private(value)
            continue
        if isinstance(value, str):
            result[key] = value[:4000]
            continue
        result[key] = value
    return result


AUTHORIZED_FIELDS = (
    "kind",
    "scope",
    "target_id",
    "build_id",
    "content_sha256",
    "granted_by",
    "text",
    "label",
    "note",
)


def _clean_authorized(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in AUTHORIZED_FIELDS:
        if key not in value or value[key] is None:
            continue
        item = value[key]
        result[key] = str(item)[:2400] if isinstance(item, str) else item
    return result


def model_payload(
    action: str, details: Any, model_details: Any = None
) -> dict[str, Any]:
    """交给模型的工具结果：公开 details + 本轮 authorized（若有）。

    拒绝信封由调用方直接构造，不经过这里。
    """

    payload: dict[str, Any] = {"details": project_details(action, details)}
    if isinstance(model_details, dict) and model_details:
        if action not in DISCLOSURE_ACTIONS:
            # 非披露动作不允许携带授权正文，防止调用方误传。
            model_details = {
                key: value
                for key, value in model_details.items()
                if key != "authorized"
            }
        clean = _strip_private(model_details)
        if isinstance(model_details.get("authorized"), dict):
            clean["authorized"] = _clean_authorized(model_details["authorized"])
        payload.update(clean)
    return payload


def authorized_facts(
    *,
    kind: str,
    grant: Any,
    text: str | None = None,
    scope: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """本轮授权可见的事实；只放在工具结果里随消息回传。"""

    payload: dict[str, Any] = {
        "kind": kind,
        "scope": scope or getattr(grant, "scope", ""),
        "target_id": getattr(grant, "target_id", ""),
        "build_id": getattr(grant, "build_id", None),
        "content_sha256": getattr(grant, "content_sha256", None),
        "granted_by": getattr(grant, "granted_by", "student_text"),
    }
    if text:
        payload["text"] = str(text)[:2400]
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return {"authorized": payload}