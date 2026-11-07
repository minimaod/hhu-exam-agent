"""F010 B：读时派生的结构化工作区。

职责边界：
- 只读：从已持久化的轮次、上下文、选中事实派生，不写数据库、不调用模型；
- 稳定：短引用 ``Q1…Qn`` 按“首次出现顺序”编号，只增不改，绑定
  ``(会话, 来源轮次, 题目, build + 内容哈希)``，刷新、重启、回放都得到同一张表；
- 安全：只输出公开字段，答案正文与临时 URL 一律不进入工作区。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.conversations.models import Turn
from app.questions.models import QuestionCard, QuestionSelection

REF_PATTERN = re.compile(r"^Q([1-9][0-9]{0,3})$")
MAX_REFS = 200
_QUESTION_KINDS = {"choice": "选择题", "fill": "填空题", "code": "编程题"}


@dataclass(frozen=True)
class RefEntry:
    """一个稳定的题目短引用。"""

    ref: str
    conversation_id: str
    source_request_id: str
    question_id: str
    build_id: str
    content_sha256: str
    number_label: str
    question_type: str
    paper_id: str
    paper_title: str

    def public(self, *, selected: bool = False) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "number_label": self.number_label,
            "question_type": self.question_type,
            "paper_id": self.paper_id,
            "paper_title": self.paper_title,
            "source_turn": self.source_request_id,
            "selected": selected,
        }


class RefTable:
    """会话内短引用表；同一题目只保留第一次出现的绑定。"""

    def __init__(self, entries: Iterable[RefEntry]) -> None:
        self._entries = list(entries)
        self._by_ref = {entry.ref: entry for entry in self._entries}
        self._by_question: dict[str, RefEntry] = {}
        for entry in self._entries:
            self._by_question.setdefault(entry.question_id, entry)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> list[RefEntry]:
        return list(self._entries)

    def resolve(self, ref: str) -> RefEntry | None:
        return self._by_ref.get(str(ref or "").strip().upper())

    def by_question_id(self, question_id: str) -> RefEntry | None:
        return self._by_question.get(question_id)

    def find(
        self,
        number_label: str,
        question_type: str | None = None,
        paper_id: str | None = None,
    ) -> list[RefEntry]:
        key = _number_key(number_label)
        matches = [
            entry
            for entry in self._entries
            if _number_key(entry.number_label) == key
            and (question_type is None or entry.question_type == question_type)
            and (paper_id is None or entry.paper_id == paper_id)
        ]
        return matches

    def public(self, selection: QuestionSelection | None) -> list[dict[str, Any]]:
        selected_id = selection.question_id if selection is not None else None
        return [
            entry.public(selected=entry.question_id == selected_id)
            for entry in self._entries
        ]


def _number_key(label: Any) -> str:
    """把“第 5 题”“5.”“5”统一成可用于比较的数字串。"""

    match = re.search(r"[0-9]+", str(label or ""))
    return match.group(0) if match else str(label or "").strip()


def build_ref_table(
    conversation_id: str,
    turns: Iterable[Turn],
    extra_cards: Iterable[QuestionCard] = (),
) -> RefTable:
    """按轮次顺序给已展示过的题卡编号；同一题目复用第一次的引用。"""

    seen: set[str] = set()
    entries: list[RefEntry] = []
    for turn in sorted(turns, key=lambda item: item.seq):
        for card in list(turn.question_results) + ([turn.question] if turn.question else []):
            if card is None or card.question_id in seen:
                continue
            if len(entries) >= MAX_REFS:
                break
            seen.add(card.question_id)
            entries.append(
                _entry(conversation_id, turn.request_id, card, f"Q{len(entries) + 1}")
            )
        if len(entries) >= MAX_REFS:
            break
    for card in extra_cards:
        if card is None or card.question_id in seen or len(entries) >= MAX_REFS:
            continue
        seen.add(card.question_id)
        entries.append(
            _entry(
                conversation_id,
                card.source_request_id or "",
                card,
                f"Q{len(entries) + 1}",
            )
        )
    return RefTable(entries)


def _entry(
    conversation_id: str, request_id: str, card: QuestionCard, ref: str
) -> RefEntry:
    return RefEntry(
        ref=ref,
        conversation_id=conversation_id,
        source_request_id=request_id,
        question_id=card.question_id,
        build_id=card.build_id or "",
        content_sha256=card.content_sha256 or "",
        number_label=card.number_label,
        question_type=card.question_type,
        paper_id=card.source.paper_id or "",
        paper_title=card.source.paper_title or "",
    )


def build_workspace(
    *,
    conversation_id: str,
    turns: list[Turn],
    context_row: Any | None,
    selection: QuestionSelection | None,
    selection_card: QuestionCard | None,
    problem: Any | None,
    exercise: Any | None,
    paper_state: dict[str, Any] | None,
    located: dict[str, Any] | None,
) -> dict[str, Any]:
    """派生一份公开工作区；``paper_state`` 优先于更早的成功定位。"""

    table = build_ref_table(
        conversation_id,
        turns,
        extra_cards=[selection_card] if selection_card is not None else [],
    )

    paper: dict[str, Any]
    if paper_state:
        paper = {
            "status": str(paper_state.get("status") or "unlocated"),
            "paper_id": None,
            "paper_title": None,
            "academic_year": paper_state.get("academic_year"),
            "term": paper_state.get("term"),
            "exam_type": paper_state.get("exam_type"),
            "candidates": _candidates(paper_state),
            "page": None,
        }
    elif located:
        paper = {
            "status": "located",
            "paper_id": located.get("paper_id"),
            "paper_title": located.get("paper_title"),
            "academic_year": located.get("academic_year"),
            "term": located.get("term"),
            "exam_type": located.get("exam_type"),
            "candidates": [],
            "page": {
                "question_type": located.get("question_type"),
                "total": located.get("total"),
                "offset": located.get("offset"),
                "returned": located.get("returned"),
                "next_offset": located.get("next_offset"),
                "limit": located.get("limit"),
            },
        }
    else:
        paper = {
            "status": "none",
            "paper_id": None,
            "paper_title": None,
            "academic_year": None,
            "term": None,
            "exam_type": None,
            "candidates": [],
            "page": None,
        }

    shown = table.public(selection)
    selection_info: dict[str, Any] | None = None
    if selection is not None:
        entry = table.by_question_id(selection.question_id)
        selection_info = {
            "ref": entry.ref if entry is not None else None,
            "question_id": selection.question_id,
            "build_id": selection.build_id,
            "content_sha256": selection.content_sha256,
            "number_label": selection.number_label,
            "question_type": selection.question_type,
            "paper_id": selection.paper_id,
            "paper_title": (entry.paper_title if entry is not None else "") or None,
            "answer_state": (
                "revealed"
                if getattr(selection, "answer_revealed_at", None)
                else "available"
            ),
        }

    exercise_info = None
    if exercise is not None:
        exercise_info = {
            "exercise_id": getattr(exercise, "id", None),
            "status": getattr(exercise, "status", None),
            "answered": bool(
                getattr(exercise, "student_answer_text", None)
                or getattr(exercise, "student_solution_text", None)
            ),
        }

    pending = None
    if paper_state and paper_state.get("status") == "clarify_paper":
        pending = {"kind": "clarify_paper", "options": [
            item["paper_title"] or item["paper_id"] for item in paper["candidates"]
        ]}
    elif exercise_info is not None and exercise_info["status"] == "awaiting_supplement":
        pending = {"kind": "needs_supplement", "options": []}

    suggestions = []
    if context_row is not None and getattr(context_row, "last_suggestions_json", None):
        import json

        try:
            payload = json.loads(context_row.last_suggestions_json)
        except (TypeError, ValueError):
            payload = []
        suggestions = [
            str(item.get("label") or "") for item in payload if isinstance(item, dict)
        ][:5]

    return {
        "status": "ok",
        "course": (
            getattr(context_row, "confirmed_course", None)
            if context_row is not None
            else None
        ),
        "paper": paper,
        "selection": selection_info,
        "shown": shown,
        "shown_count": len(shown),
        "exercise": exercise_info,
        "problem": (
            {
                "problem_id": getattr(problem, "id", None),
                "status": getattr(problem, "status", None),
            }
            if problem is not None
            else None
        ),
        "last_suggestions": suggestions,
        "pending": pending,
    }


def _candidates(state: dict[str, Any]) -> list[dict[str, Any]]:
    ids = [str(item) for item in state.get("candidates") or []]
    titles = [str(item) for item in state.get("candidate_titles") or []]
    result = []
    for index, paper_id in enumerate(ids):
        title = titles[index] if index < len(titles) else paper_id
        result.append({"paper_id": paper_id, "paper_title": title})
    return result


def ambiguity(
    entries: list[RefEntry],
    question_type: str | None = None,
) -> bool:
    """同一份卷的同一题号出现在不同题型里，且本轮没有限定题型。"""

    if question_type:
        return False
    kinds = {entry.question_type for entry in entries}
    return len(kinds) > 1


def workspace_text(workspace: dict[str, Any], *, max_chars: int = 1200) -> str:
    """把工作区渲染成决策模型可读的确定性摘要。"""

    paper = workspace.get("paper") or {}
    parts: list[str] = []
    course = workspace.get("course")
    if course:
        parts.append(f"课程：{course}")
    status = paper.get("status")
    if status == "located":
        title = paper.get("paper_title") or paper.get("paper_id")
        parts.append(f"当前试卷：{title}")
        page = paper.get("page") or {}
        if page.get("question_type"):
            parts.append(
                f"上次浏览：{_QUESTION_KINDS.get(page['question_type'], '题目')}"
                f"，总数 {page.get('total')}，本次 {page.get('returned')} 道"
            )
    elif status == "clarify_paper":
        names = "、".join(
            f"《{item['paper_title']}》" for item in paper.get("candidates") or []
        )
        parts.append(f"待确认试卷：{names}（学生只答卷别即可）")
    elif status == "unlocated":
        parts.append("当前试卷未在目录中定位；不要回退到更早的卷")
    else:
        parts.append("当前还没有确定试卷")

    selection = workspace.get("selection")
    if selection:
        parts.append(
            f"当前选中：{selection.get('ref')}（第 {selection.get('number_label')} 题，"
            f"{_QUESTION_KINDS.get(selection.get('question_type'), '题目')}）"
        )
    shown = workspace.get("shown") or []
    if shown:
        recent = "、".join(item["ref"] for item in shown[-5:])
        parts.append(f"已展示 {len(shown)} 道，最近引用：{recent}")
    exercise = workspace.get("exercise")
    if exercise:
        parts.append(f"当前练习状态：{exercise.get('status')}")
    pending = workspace.get("pending")
    if pending and pending.get("kind") == "clarify_paper":
        parts.append("等待学生确认卷别后再操作")
    suggestions = workspace.get("last_suggestions") or []
    if suggestions:
        parts.append("上次建议：" + "、".join(suggestions))
    text = "\n".join(parts)
    return text[:max_chars]