"""F010 C：终局回复的判定、材料快照与 ``/reply`` 重校验。

两条路径共用同一份事实来源：

* 在线路径：工具循环结束后，用**同一份循环消息**（含本轮 authorized 工具结果）
  流式写正文；业务结果先提交，正文后写。
* ``/reply``：只重做正文，不执行工具。恢复记录里存的是**公开材料 + 授权引用**
  （题目 ID、版本哈希、问题/练习 ID），不存答案正文；重做时按引用重新读取并
  逐条校验版本与来源，失败的条目丢弃并如实提示。

本模块不读写数据库细节，只通过服务层已有的 repository / questions 能力取数，
因此可以单独测试。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from app.conversations.reply import (
    ReplyMaterial,
    material_from_payload,
    material_to_payload,
)
from app.conversations.tool_projection import DISCLOSURE_ACTIONS, public_question

CONTEXT_VERSION = 2
ANSWER_CHANGED_NOTE = "答案版本已变化，请重新查题。"

TERMINAL_LABELS = {
    "model_finished": "模型判断信息足够",
    "clarification": "等待学生回答",
    "budget": "达到本轮工具上限",
    "replan_error": "后续决策失败",
    "recovery": "恢复已批准步骤",
}


def terminal_reason(final_state: dict[str, Any]) -> str:
    reason = str(final_state.get("terminal_reason") or "")
    if reason:
        return reason
    observations = list(final_state.get("observations") or [])
    if any(item.get("needs_user") for item in observations):
        return "clarification"
    if final_state.get("replan_error"):
        return "replan_error"
    if final_state.get("pending"):
        return "budget"
    return "model_finished"


def clarification_question(final_state: dict[str, Any]) -> str | None:
    """最后一个“等学生回答”的工具消息；澄清轮直接用它作正文。"""

    for item in reversed(list(final_state.get("observations") or [])):
        if item.get("needs_user") and item.get("message"):
            return str(item["message"])[:3000]
    return None


def _card_ref(card: Any, ctx: Any) -> str | None:
    table = getattr(ctx, "ref_table", None)
    if table is not None:
        entry = table.by_question_id(
            getattr(card, "question_id", ""),
            build_id=getattr(card, "build_id", None),
            content_sha256=getattr(card, "content_sha256", None),
        )
        if entry is not None:
            return entry.ref
    return None


def build_context(
    *,
    prepared: Any,
    ctx: Any,
    final_state: dict[str, Any],
    composite_results: list[Any],
    suggestions: list[Any],
    recent_summary: str | None,
    material: ReplyMaterial | None,
    reason: str,
    clarification: str | None = None,
) -> dict[str, Any]:
    """把本轮已经提交的公开材料与授权引用存进恢复记录（不含答案正文）。"""

    payload = material_to_payload(material) if material is not None else {}
    payload["teaching"] = None
    if payload.get("waiting_for") is None and clarification:
        payload["waiting_for"] = clarification[:3000]
    payload["version"] = CONTEXT_VERSION
    payload["reason"] = reason
    payload["scope"] = ctx.answer_scope
    payload["request_id"] = prepared.request_id
    payload["authorized"] = authorized_refs(ctx)
    own_questions = getattr(ctx, "question_source_turn", None) == prepared.request_id
    cards: list[Any] = []
    if getattr(ctx, "question_card", None) is not None:
        cards.append(ctx.question_card)
    for card in (getattr(ctx, "question_results", None) or []) if own_questions else []:
        if all(item.question_id != card.question_id for item in cards):
            cards.append(card)
    payload["questions"] = [
        public_question(card, ref=_card_ref(card, ctx), statement_limit=300)
        for card in cards[:10]
    ]
    payload["suggestions"] = [getattr(item, "label", str(item)) for item in suggestions]
    payload["context_summary"] = recent_summary
    return payload


def explanation_sha256(explanation: Any) -> str | None:
    """讲解快照指纹：旧轮次只能按当时的讲解重读，改版后不得写回。"""

    if explanation is None:
        return None
    payload = {
        "steps": [
            {
                "index": getattr(step, "index", ""),
                "title": getattr(step, "title", ""),
                "content": getattr(step, "content", ""),
            }
            for step in list(getattr(explanation, "steps", None) or [])
        ],
        "final_answer": getattr(explanation, "final_answer", None),
        "error_analysis": getattr(explanation, "error_analysis", None),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def public_material_from_legacy(material: ReplyMaterial) -> ReplyMaterial:
    """B 旧 ``reply_input`` 无法核对当前授权：只保留公开材料。

    讲解正文与“查看答案/讲解/解答/评价”类工具结果都可能含当时获准的答案，
    退出记录后无法证明本轮仍然有效，因此一律不交给回复模型。
    """

    dropped = frozenset(DISCLOSURE_ACTIONS) | {
        "show_question_answer",
        "answer_followup",
        "followup",
        "tutoring",
        "evaluate_exercise",
    }
    results = tuple(item for item in material.results if item.action not in dropped)
    # 旧摘要按 B 的实现可能直接复述原助手答案，且无法核对当时授权，一并丢弃。
    return replace(
        material, teaching=None, context_summary=None, results=results
    )


def _text_sha256(text: str | None) -> str | None:
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def authorized_refs(ctx: Any) -> list[dict[str, Any]]:
    """本轮哪些答案/讲解/评价已经授权；只记引用，不记正文。"""

    refs: list[dict[str, Any]] = []
    grant = getattr(ctx, "answer_grant", None)
    tutoring = getattr(ctx, "tutoring", None)
    tutoring_kind = getattr(tutoring, "kind", None) if tutoring is not None else None
    tutoring_answer = getattr(tutoring, "answer", None) if tutoring_kind == "followup_answer" else None
    followup_sha = _text_sha256(tutoring_answer) or _text_sha256(getattr(ctx, "authorized_text", None))
    question_selection = getattr(ctx, "question_selection", None)
    question_card = getattr(ctx, "question_card", None)
    active_problem = getattr(ctx, "active_problem", None)
    active_exercise = getattr(ctx, "active_exercise", None)
    ctx_request_id = str(getattr(ctx, "request_id", "") or "")

    if grant is not None:
        kind = str(getattr(grant, "kind", "") or "")
        scope = str(getattr(grant, "scope", "") or "")
        req_id = str(getattr(grant, "request_id", "") or ctx_request_id)
        entry: dict[str, Any] = {
            "kind": kind,
            "scope": scope,
            "request_id": req_id,
        }
        if kind in ("question_answer", "question_explain"):
            entry.update(
                {
                    "question_id": str(getattr(grant, "target_id", "") or ""),
                    "selection_id": getattr(grant, "selection_id", None),
                    "build_id": getattr(grant, "build_id", None),
                    "content_sha256": getattr(grant, "content_sha256", None),
                }
            )
            if question_card is not None:
                entry["question_id"] = question_card.question_id
        elif kind == "exercise_solution":
            entry["exercise_id"] = str(getattr(grant, "target_id", "") or "")
        elif kind == "followup_answer":
            target_id = str(getattr(grant, "target_id", "") or "")
            is_question_followup = bool(
                getattr(grant, "selection_id", None)
                or (
                    question_selection is not None
                    and active_problem is None
                    and question_card is not None
                    and target_id == getattr(question_card, "question_id", "")
                )
            )
            is_exercise_followup = bool(
                active_exercise is not None
                and target_id == str(getattr(active_exercise, "id", "") or "")
                and (active_problem is None or target_id != str(getattr(active_problem, "id", "") or ""))
            )
            if is_question_followup:
                q_id = target_id or str(getattr(question_card, "question_id", "") or "")
                refs.append(
                    {
                        "kind": "question_followup",
                        "scope": scope or str(getattr(ctx, "answer_scope", None) or "explain"),
                        "request_id": req_id,
                        "question_id": q_id,
                        "selection_id": getattr(grant, "selection_id", None)
                        or getattr(question_selection, "id", None),
                        "build_id": getattr(grant, "build_id", None)
                        or getattr(question_selection, "build_id", None),
                        "content_sha256": getattr(grant, "content_sha256", None)
                        or getattr(question_selection, "content_sha256", None),
                        "answer_sha256": followup_sha,
                    }
                )
            elif is_exercise_followup and active_exercise is not None:
                refs.append(
                    {
                        "kind": "exercise_followup",
                        "scope": scope or str(getattr(ctx, "answer_scope", None) or "explain"),
                        "request_id": req_id,
                        "exercise_id": str(active_exercise.id),
                        "answer_sha256": followup_sha,
                    }
                )
        if kind in ("question_answer", "question_explain", "exercise_solution"):
            refs.append(entry)
    kind = tutoring_kind
    problem = getattr(tutoring, "problem", None) if tutoring is not None else None
    problem_id = getattr(ctx, "problem_id", None) or getattr(problem, "id", None)
    if kind in ("explanation", "followup_answer") and problem_id:
        record = active_problem
        if record is not None and str(getattr(record, "id", "")) == str(problem_id):
            explanation = getattr(record, "explanation", None)
        else:
            explanation = getattr(problem, "explanation", None)
            if explanation is None and kind == "explanation":
                # ExplanationResult.problem 是 ProblemView，讲解正文位于结果本身。
                explanation = tutoring
        item_ref: dict[str, Any] = {
            "kind": "explanation" if kind == "explanation" else "followup",
            "problem_id": str(problem_id),
            "request_id": ctx_request_id,
            "explanation_sha256": explanation_sha256(explanation),
        }
        if kind == "followup_answer":
            item_ref["scope"] = str(
                getattr(grant, "scope", None)
                or getattr(ctx, "answer_scope", None)
                or "explain"
            )
            item_ref["answer_sha256"] = followup_sha
        refs.append(item_ref)
    exercise = getattr(ctx, "exercise_view", None)
    evaluation = getattr(ctx, "evaluation_view", None)
    if exercise is not None:
        exercise_id = str(getattr(exercise, "id", "") or getattr(ctx, "exercise_id", "") or "")
        if exercise_id and getattr(exercise, "reference_solution_text", None):
            refs.append({"kind": "exercise_solution", "exercise_id": exercise_id})
        if exercise_id and evaluation is not None:
            attempt_id = None
            current = (
                getattr(active_exercise, "current_attempt", None)
                if active_exercise is not None
                else None
            )
            if current is not None:
                attempt_id = str(getattr(current, "id", "") or "") or None
            refs.append(
                {
                    "kind": "evaluation",
                    "exercise_id": exercise_id,
                    "attempt_id": attempt_id,
                }
            )
    # 去重但保留顺序。
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in refs:
        key = (
            str(item.get("kind")),
            str(
                item.get("question_id")
                or item.get("problem_id")
                or item.get("exercise_id")
                or ""
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


# ----------------------------------------------------------------------
# /reply：重新读取并校验授权材料
# ----------------------------------------------------------------------
def _question_answer_text(card: Any) -> str | None:
    parts: list[str] = []
    detail = getattr(card, "answer_detail", None)
    text = getattr(detail, "text", None) if detail is not None else None
    if text:
        parts.append(f"原卷答案：{str(text)[:1200]}")
    reference_code = getattr(card, "reference_code", None)
    if reference_code:
        parts.append(f"参考代码：{str(reference_code)[:1200]}")
    for solution in list(getattr(card, "independent_solutions", None) or [])[:2]:
        body = getattr(solution, "text", None)
        if body:
            parts.append(f"其他解法：{str(body)[:400]}")
    return "\n".join(parts) or None


def question_answer_text(card: Any) -> str | None:
    """公开入口：把已授权卡片上的答案正文整理成一段材料。"""

    return _question_answer_text(card)


def _explanation_text(explanation: Any) -> str | None:
    if explanation is None:
        return None
    lines: list[str] = []
    for step in list(getattr(explanation, "steps", None) or [])[:8]:
        title = getattr(step, "title", "") or ""
        content = getattr(step, "content", "") or ""
        index = getattr(step, "index", "")
        lines.append(f"第{index}步 {title}：{str(content)[:400]}")
    final_answer = getattr(explanation, "final_answer", None)
    if final_answer:
        lines.append(f"最终答案：{str(final_answer)[:600]}")
    error_analysis = getattr(explanation, "error_analysis", None)
    if error_analysis:
        lines.append(f"易错点：{str(error_analysis)[:400]}")
    return "\n".join(lines) or None


def _evaluation_text(exercise: Any, evaluation_json: str | None) -> str | None:
    payload: dict[str, Any] = {}
    if evaluation_json:
        try:
            value = json.loads(evaluation_json)
            if isinstance(value, dict):
                payload = value
        except (TypeError, ValueError):
            payload = {}
    if not payload:
        return None
    is_correct = payload.get("is_correct")
    needs_supplement = bool(payload.get("needs_supplement"))
    if needs_supplement:
        verdict = "需要补充解题过程（未提供完整过程，暂无法定位具体错误）"
        next_hint = "请补充解题过程，便于定位具体错误。"
    elif is_correct is True:
        verdict = "判定：正确"
        next_hint = "作答已批改并在下方评价卡展示，可继续订正或练习下一题。"
    elif is_correct is False:
        verdict = "判定：错误"
        next_hint = "作答已批改，具体分析与参考解答已在下方评价卡展示；如有具体疑惑可继续追问。"
    else:
        verdict = "判定：依据不足（暂时无法判定对错）"
        next_hint = str(payload.get("guidance") or "请补充关键步骤或清晰图片，便于重新判断。")[:120]

    has_solution = bool(
        getattr(exercise, "reference_solution_text", None) if exercise is not None else None
    )
    parts = [
        f"作答评价结果：{verdict}",
        f"下一步引导（正文最多保留这一句）：{next_hint}",
    ]
    summary = str(payload.get("final_answer_assessment") or "").strip()[:160]
    analysis = str(payload.get("step_by_step_analysis") or "").strip()[:300]
    if summary or analysis:
        parts.append(
            "卡片已展示的内部诊断参考（严禁在正文复述，仅用于回答学生额外追问时保持一致）："
            + "；".join(item for item in (summary, analysis) if item)
        )
    parts.append(
        "展示分工说明：完整结果、你的答案、过程诊断、建议"
        + ("及参考解答" if has_solution else "")
        + "已在下方评价卡片中完整展示。正文不要复述上述评价内容、不要展开分步推导；只有当学生本轮还提出了额外追问时，才简明回答追问。"
    )
    return "\n".join(parts)


evaluation_brief_text = _evaluation_text


def revalidate_teaching(
    service: Any, conversation_id: str, context: dict[str, Any]
) -> tuple[str | None, list[str]]:
    """按引用重读授权材料；返回（教学正文, 问题列表）。任何一条失败只丢该条。"""

    pieces: list[str] = []
    notes: list[str] = []
    for ref in list(context.get("authorized") or []):
        if not isinstance(ref, dict):
            continue
        kind = str(ref.get("kind") or "")
        try:
            if kind == "question_answer":
                text = _revalidated_question_answer(service, conversation_id, ref)
                if text is None:
                    notes.append(ANSWER_CHANGED_NOTE)
                else:
                    pieces.append(text)
            elif kind in ("explanation", "followup"):
                problem = service._repository.get_problem(str(ref.get("problem_id") or ""))
                if problem is None or problem.conversation_id != conversation_id:
                    notes.append(ANSWER_CHANGED_NOTE)
                    continue
                expected = ref.get("explanation_sha256")
                current = explanation_sha256(getattr(problem, "explanation", None))
                if not expected or current != expected:
                    notes.append(ANSWER_CHANGED_NOTE)
                    continue
                if kind == "followup" and ref.get("answer_sha256"):
                    answer = _revalidated_turn_followup(service, conversation_id, ref)
                    if answer is None:
                        notes.append(ANSWER_CHANGED_NOTE)
                        continue
                    scope = str(ref.get("scope") or "explain")
                    scope_note = (
                        f"原回答范围：{scope}（不得扩写未授权的完整答案）。\n"
                        if scope in ("hint", "topic")
                        else ""
                    )
                    pieces.append(f"{scope_note}原追问回答：{answer[:2400]}")
                else:
                    text = _explanation_text(problem.explanation)
                    if text is None:
                        notes.append(ANSWER_CHANGED_NOTE)
                    else:
                        pieces.append(text)
            elif kind == "question_followup":
                text = _revalidated_question_followup(service, conversation_id, ref)
                if text is None:
                    notes.append(ANSWER_CHANGED_NOTE)
                else:
                    pieces.append(text)
            elif kind == "exercise_followup":
                text = _revalidated_exercise_followup(service, conversation_id, ref)
                if text is None:
                    notes.append(ANSWER_CHANGED_NOTE)
                else:
                    pieces.append(text)
            elif kind == "exercise_solution":
                exercise = service._repository.get_exercise(str(ref.get("exercise_id") or ""))
                if (
                    exercise is None
                    or exercise.conversation_id != conversation_id
                    or not exercise.reference_solution_text
                ):
                    notes.append(ANSWER_CHANGED_NOTE)
                    continue
                pieces.append(f"参考解答（已向学生开放）：{exercise.reference_solution_text[:900]}")
            elif kind == "evaluation":
                exercise = service._repository.get_exercise(str(ref.get("exercise_id") or ""))
                if exercise is None or exercise.conversation_id != conversation_id:
                    notes.append(ANSWER_CHANGED_NOTE)
                    continue
                attempt_id = str(ref.get("attempt_id") or "")
                attempt = next(
                    (
                        item
                        for item in list(exercise.attempts or [])
                        if str(getattr(item, "id", "")) == attempt_id
                    ),
                    None,
                )
                evaluation_json = (
                    getattr(attempt, "evaluation_json", None) if attempt is not None else None
                )
                text = _evaluation_text(exercise, evaluation_json)
                if not attempt_id or text is None:
                    notes.append(ANSWER_CHANGED_NOTE)
                else:
                    attachments = getattr(attempt, "attachments", None) or []
                    if attachments:
                        text = (
                            f"评价阶段已读取 {len(attachments)} 张过程图片；正文无需重新识图，不得否认已收到图片。\n"
                            + text
                        )
                    pieces.append(text)
        except Exception:
            notes.append(ANSWER_CHANGED_NOTE)
    return "\n\n".join(pieces) or None, notes


def _revalidated_question_selection(
    service: Any,
    conversation_id: str,
    ref: dict[str, Any],
    *,
    require_answer: bool,
) -> tuple[Any, Any] | None:
    question_id = str(ref.get("question_id") or "")
    selection = None
    selection_id = ref.get("selection_id")
    if selection_id:
        selection = service._repository.get_question_selection_by_id(
            conversation_id, str(selection_id)
        )
    if selection is None and question_id:
        selection = service._repository.get_question_selection(
            conversation_id,
            question_id,
            build_id=str(ref.get("build_id") or "") or None,
            content_sha256=str(ref.get("content_sha256") or "") or None,
        )
    if selection is None or selection.conversation_id != conversation_id:
        return None
    if str(selection.build_id or "") != str(ref.get("build_id") or ""):
        return None
    if str(selection.content_sha256 or "") != str(ref.get("content_sha256") or ""):
        return None
    if require_answer:
        if not selection.answer_revealed_at:
            return None
        try:
            card, evidence = service.questions.answer_view(selection)
        except Exception:
            return None
        if evidence.status in ("source_changed", "source_pruned", "unavailable", "missing_answer"):
            return None
        return selection, card
    try:
        card = service.questions.card_for_selection(selection)
        if not service.questions.verify_card(card):
            return None
        is_approved = getattr(service.questions, "is_approved", None)
        if callable(is_approved) and not is_approved(card.question_id):
            return None
    except Exception:
        return None
    return selection, card


def _revalidated_question_answer(
    service: Any, conversation_id: str, ref: dict[str, Any]
) -> str | None:
    validated = _revalidated_question_selection(
        service, conversation_id, ref, require_answer=True
    )
    if validated is None:
        return None
    _, card = validated
    return _question_answer_text(card)


def _revalidated_turn_followup(
    service: Any, conversation_id: str, ref: dict[str, Any]
) -> str | None:
    request_id = str(ref.get("request_id") or "").strip()
    if not request_id:
        return None
    turn = service._repository.find_turn(conversation_id, request_id)
    if turn is None or turn.status != "completed":
        return None
    tutoring = getattr(turn, "tutoring", None)
    if getattr(tutoring, "kind", None) != "followup_answer":
        return None
    answer = str(getattr(tutoring, "answer", "") or "").strip()
    if not answer:
        return None
    expected_sha = ref.get("answer_sha256")
    if not expected_sha or _text_sha256(answer) != str(expected_sha):
        return None
    return answer


def _revalidated_question_followup(
    service: Any, conversation_id: str, ref: dict[str, Any]
) -> str | None:
    validated = _revalidated_question_selection(
        service, conversation_id, ref, require_answer=False
    )
    if validated is None:
        return None
    selection, card = validated
    answer = _revalidated_turn_followup(service, conversation_id, ref)
    if answer is None:
        return None
    scope = str(ref.get("scope") or "explain")
    statement = str(getattr(card, "statement", "") or "").strip()[:600]
    parts = [f"当前选中真题：{statement}"] if statement else []
    if scope in ("hint", "topic") and not getattr(selection, "answer_revealed_at", None):
        parts.append(f"原回答范围：{scope}（参考答案尚未开放，严禁扩写未公开答案）")
    else:
        parts.append(f"原回答范围：{scope}")
    parts.append(f"原追问回答：{answer[:2400]}")
    return "\n".join(parts)


def _revalidated_exercise_followup(
    service: Any, conversation_id: str, ref: dict[str, Any]
) -> str | None:
    exercise = service._repository.get_exercise(str(ref.get("exercise_id") or ""))
    if exercise is None or exercise.conversation_id != conversation_id:
        return None
    answer = _revalidated_turn_followup(service, conversation_id, ref)
    if answer is None:
        return None
    scope = str(ref.get("scope") or "explain")
    statement = str(getattr(exercise, "statement_text", "") or "").strip()[:600]
    parts = [f"当前练习题：{statement}"] if statement else []
    if scope in ("hint", "topic") and not getattr(exercise, "revealed_solution_at", None):
        parts.append(f"原回答范围：{scope}（参考解答尚未开放，严禁泄露最终答案与完整解法）")
    else:
        parts.append(f"原回答范围：{scope}")
    parts.append(f"原追问回答：{answer[:2400]}")
    return "\n".join(parts)


def material_from_context(
    service: Any, conversation_id: str, context: dict[str, Any]
) -> tuple[ReplyMaterial, list[str]]:
    """从恢复记录重建 /reply 材料；授权条目逐条重校验。"""

    payload = {
        key: value
        for key, value in context.items()
        if key
        not in (
            "version",
            "reason",
            "scope",
            "request_id",
            "authorized",
            "questions",
            "_recovery",
        )
    }
    material = material_from_payload(payload)
    teaching, notes = revalidate_teaching(service, conversation_id, context)
    if teaching:
        material = type(material)(
            user_text=material.user_text,
            course=material.course,
            results=material.results,
            waiting_for=material.waiting_for,
            question_titles=material.question_titles,
            question_materials=material.question_materials,
            question_page=material.question_page,
            answer_scope=material.answer_scope,
            suggestions=material.suggestions,
            context_summary=material.context_summary,
            workbench_context=material.workbench_context,
            teaching=teaching,
            learning_memory=material.learning_memory,
        )
    return material, notes
