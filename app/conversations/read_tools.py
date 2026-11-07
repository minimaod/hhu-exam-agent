"""F010 B：四个只读工具的实现。

安全边界：
- 只读工具不写业务表、不切换课程、不修改上下文事实；
- 真题详情只走**引用校验 + 公开题面投影**，不调用 ``evidence()``/
  ``answer_view()``，答案正文、参考代码、临时 URL 一律不出现；
- 失败返回可自行纠正的结构化结果（``failed`` 且 ``needs_user=False``），
  只有真正无法判断指向时才要求澄清。
"""

from __future__ import annotations

from typing import Any

from app.conversations.graph import ActionOutcome, ActionPlan
from app.conversations.turn_types import ExecuteTurn, _DynamicContext
from app.conversations.workspace import (
    RefEntry,
    ambiguity,
    build_ref_table,
    build_workspace,
    workspace_text,
)
from app.questions.errors import (
    QuestionBankUnavailableError,
    QuestionError,
    QuestionSelectionInvalidError,
    QuestionSourceChangedError,
)
from app.questions.models import QuestionCard

READ_ONLY_ACTIONS = frozenset(
    {
        "view_workspace",
        "view_question_detail",
        "list_shown_questions",
        "view_learning_summary",
    }
)

_MEMORY_DETAIL_KEYS = frozenset(
    {
        "status",
        "memory_course",
        "memory_range",
        "memory_counts",
        "memory_groups",
        "memory_suggestions",
        "memory_pending",
        "memory_entries",
    }
)

_KINDS = {"choice": "选择题", "fill": "填空题", "code": "编程题"}


class ReadToolMixin:
    """工作区、题目详情、已展示列表与学习摘要。"""

    # ------------------------------------------------------------------
    # 工作区
    # ------------------------------------------------------------------
    def _visible_selection(self, ctx: _DynamicContext) -> Any:
        """换卷失败/待澄清后清除“默认目标”，但历史题卡仍可显式引用。"""

        selection = ctx.question_selection
        if selection is None:
            return None
        state = self._repository.find_paper_state(ctx.conversation_id)
        if state is None:
            return selection
        blocked = (
            self._repository.find_turn(
                ctx.conversation_id, str(state.get("request_id") or "")
            )
            if state.get("request_id")
            else None
        )
        if blocked is None:
            return None
        if selection.source_request_id:
            source = self._repository.find_turn(
                ctx.conversation_id, selection.source_request_id
            )
            if source is not None and source.seq <= blocked.seq:
                return None
        return selection

    def _workspace(self, ctx: _DynamicContext) -> dict[str, Any]:
        if ctx.workspace:
            return ctx.workspace
        turns = self._repository.list_turns(ctx.conversation_id)
        selection = self._visible_selection(ctx)
        paper_state = self._repository.find_paper_state(ctx.conversation_id)
        ctx.workspace = build_workspace(
            conversation_id=ctx.conversation_id,
            turns=turns,
            context_row=self._repository.get_context(ctx.conversation_id),
            selection=selection,
            selection_card=ctx.question_card if selection is not None else None,
            problem=ctx.active_problem,
            exercise=ctx.active_exercise,
            paper_state=paper_state,
            located=self._repository.find_question_context(ctx.conversation_id)
            if paper_state is None
            else None,
        )
        ctx.ref_table = build_ref_table(ctx.conversation_id, turns)
        return ctx.workspace

    def _ensure_ref_table(self, ctx: _DynamicContext) -> Any:
        if ctx.ref_table is None:
            self._workspace(ctx)
        return ctx.ref_table

    def _view_workspace_outcome(
        self, prepared: ExecuteTurn, action: ActionPlan, ctx: _DynamicContext
    ) -> ActionOutcome:
        ctx.workspace = {}
        workspace = self._workspace(ctx)
        paper = workspace.get("paper") or {}
        title = paper.get("paper_title") or "尚未确定试卷"
        selected = (workspace.get("selection") or {}).get("ref")
        message = (
            f"已读取工作区：{title}；已展示 {workspace.get('shown_count', 0)} 道"
            + (f"；当前选中 {selected}" if selected else "")
            + "。"
        )
        return self._action_result(
            prepared,
            ctx,
            ActionOutcome(
                "view_workspace",
                "success",
                message,
                {"workspace": workspace, "count": workspace.get("shown_count", 0)},
            ),
        )

    # ------------------------------------------------------------------
    # 题目详情（公开题面 + 版本元数据）
    # ------------------------------------------------------------------
    def _resolve_ref_entry(
        self, ctx: _DynamicContext, ref: str | None
    ) -> RefEntry | None:
        table = self._ensure_ref_table(ctx)
        if ref:
            return table.resolve(ref)
        if ctx.question_selection is not None:
            return table.by_question_id(ctx.question_selection.question_id)
        return None

    def _view_question_detail_outcome(
        self, prepared: ExecuteTurn, action: ActionPlan, ctx: _DynamicContext
    ) -> ActionOutcome:
        ref = str(action.parameters.get("ref") or "").strip() or None
        entry = self._resolve_ref_entry(ctx, ref)
        if entry is None and ref is None and ctx.question_selection is not None:
            return self._read_failure(
                prepared,
                ctx,
                "view_question_detail",
                "UNAVAILABLE",
                "当前选中的题不在最近一次定位的试卷范围内，请先重新查题。",
                details={},
            )
        if entry is None:
            return self._read_failure(
                prepared,
                ctx,
                "view_question_detail",
                "INVALID_REF",
                (
                    f"没有找到引用 {ref}；先用 view_workspace 或 list_shown_questions "
                    "查看可用的 Q 编号。" if ref else "当前还没有可查看的题目引用。"
                ),
                details={"ref": ref},
            )
        try:
            card = self._resolve_question_card(
                ctx.conversation_id, entry.source_request_id, entry.question_id
            )
        except QuestionSourceChangedError:
            return self._read_failure(
                prepared,
                ctx,
                "view_question_detail",
                "VERSION_CHANGED",
                "这道题的题库版本或内容已变化，请重新查题后再看。",
                details={"ref": entry.ref},
            )
        except QuestionSelectionInvalidError:
            return self._read_failure(
                prepared,
                ctx,
                "view_question_detail",
                "UNAVAILABLE",
                "这道题当前不可用（未通过审核或不在原查题结果里）。",
                details={"ref": entry.ref},
            )
        except QuestionError as exc:
            return self._read_failure(
                prepared,
                ctx,
                "view_question_detail",
                exc.code,
                exc.message,
                details={"ref": entry.ref},
                retryable=exc.status_code == 503,
            )
        selected = (
            ctx.question_selection is not None
            and ctx.question_selection.question_id == card.question_id
        )
        details = self._public_card(card, entry.ref, selected=selected)
        return self._action_result(
            prepared,
            ctx,
            ActionOutcome(
                "view_question_detail",
                "success",
                f"已读取第 {card.number_label} 题的公开题面与版本信息。",
                details,
            ),
        )

    @staticmethod
    def _public_card(
        card: QuestionCard, ref: str, *, selected: bool
    ) -> dict[str, Any]:
        """逐字段白名单：只给公开题面与版本元数据。"""

        return {
            "ref": ref,
            "question_id": card.question_id,
            "build_id": card.build_id,
            "content_sha256": card.content_sha256,
            "question_type": card.question_type,
            "number_label": card.number_label,
            "statement": card.statement[:1200],
            "options": [
                {"label": option.label, "text": option.text[:200]}
                for option in card.options[:8]
            ],
            "code_lines": [line[:200] for line in card.code_lines[:15]],
            "blank_marks": list(card.blank_marks)[:30],
            "formulas": [
                {"text": item.text[:200], "page": item.page}
                for item in card.formulas[:10]
            ],
            "pages": {
                "statement": list(card.pages.statement)[:10],
                "answer": list(card.pages.answer)[:10],
            },
            "knowledge_points": [
                {"slug": item.slug, "name": item.name}
                for item in card.knowledge_points[:10]
            ],
            "source": {
                "paper_id": card.source.paper_id,
                "paper_title": card.source.paper_title,
                "exam_type": card.source.exam_type,
                "academic_year": card.source.academic_year,
                "question_number": card.source.question_number,
                "may_show_answer": card.source.may_show_answer,
            },
            "answer": {
                "available": bool(card.answer.available),
                "state": card.answer.state,
            },
            "selected": selected,
            "from_snapshot": True,
        }

    # ------------------------------------------------------------------
    # 已展示题目
    # ------------------------------------------------------------------
    def _list_shown_questions_outcome(
        self, prepared: ExecuteTurn, action: ActionPlan, ctx: _DynamicContext
    ) -> ActionOutcome:
        workspace = self._workspace(ctx)
        shown = list(workspace.get("shown") or [])
        question_type = action.parameters.get("question_type")
        if question_type:
            shown = [item for item in shown if item.get("question_type") == question_type]
        offset = int(action.parameters.get("offset") or 0)
        limit = int(action.parameters.get("limit") or 20)
        page = shown[offset : offset + limit]
        has_more = offset + len(page) < len(shown)
        return self._action_result(
            prepared,
            ctx,
            ActionOutcome(
                "list_shown_questions",
                "success",
                f"共展示过 {len(shown)} 道题，本次返回 {len(page)} 道。",
                {
                    "total": len(shown),
                    "offset": offset,
                    "returned": len(page),
                    "has_more": has_more,
                    "shown": page,
                },
            ),
        )

    # ------------------------------------------------------------------
    # 学习摘要（只读，不切换课程）
    # ------------------------------------------------------------------
    def _view_learning_summary_outcome(
        self, prepared: ExecuteTurn, action: ActionPlan, ctx: _DynamicContext
    ) -> ActionOutcome:
        outcome = self._memory_review_outcome(
            prepared, action, ctx, prepared.input.text, confirmed_course=None
        )
        details = {
            key: value
            for key, value in outcome.details.items()
            if key in _MEMORY_DETAIL_KEYS
        }
        return self._action_result(
            prepared,
            ctx,
            ActionOutcome(
                "view_learning_summary",
                outcome.status,
                outcome.message,
                details,
                needs_user=outcome.needs_user,
                error_code=outcome.error_code,
                retryable=outcome.retryable,
            ),
        )

    # ------------------------------------------------------------------
    # 公共失败格式
    # ------------------------------------------------------------------
    def _read_failure(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        action: str,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> ActionOutcome:
        return self._action_result(
            prepared,
            ctx,
            ActionOutcome(
                action,
                "failed",
                message,
                {**(details or {}), "error_code": code},
                needs_user=False,
                error_code=code,
                retryable=retryable,
            ),
        )

    # ------------------------------------------------------------------
    # 供 select_question(ref) 复用
    # ------------------------------------------------------------------
    def _ref_target(
        self, ctx: _DynamicContext, ref: str, question_type: str | None = None
    ) -> tuple[QuestionCard | None, str | None, str | None]:
        """把 ref 解析成题卡；返回 ``(card, error_code, message)``。"""

        entry = self._resolve_ref_entry(ctx, ref)
        if entry is None:
            return None, "INVALID_REF", f"没有找到引用 {ref}，请先查看工作区里的 Q 编号。"
        page = [
            item
            for item in ctx.question_results
            if item.source.paper_id == entry.paper_id
            and str(item.number_label) == str(entry.number_label)
        ]
        if ambiguity(
            [
                RefEntry(
                    ref=entry.ref,
                    conversation_id=entry.conversation_id,
                    source_request_id=entry.source_request_id,
                    question_id=entry.question_id,
                    build_id=entry.build_id,
                    content_sha256=entry.content_sha256,
                    number_label=entry.number_label,
                    question_type=item.question_type,
                    paper_id=entry.paper_id,
                    paper_title=entry.paper_title,
                )
                for item in page
            ],
            question_type,
        ):
            kinds = "、".join(
                dict.fromkeys(_KINDS.get(item.question_type, "题目") for item in page)
            )
            return (
                None,
                "AMBIGUOUS_TARGET",
                f"这份试卷的{kinds}都有第 {entry.number_label} 题；先确认要哪一种题型。",
            )
        try:
            card = self._resolve_question_card(
                ctx.conversation_id, entry.source_request_id, entry.question_id
            )
        except QuestionSourceChangedError:
            return None, "VERSION_CHANGED", "这道题的题库版本或内容已变化，请重新查题后再选。"
        except QuestionSelectionInvalidError:
            return None, "UNAVAILABLE", "这道题当前不可用，请重新查题。"
        except QuestionBankUnavailableError as exc:
            return None, "BANK_UNAVAILABLE", exc.message
        except QuestionError as exc:
            return None, exc.code, exc.message
        return card, None, None