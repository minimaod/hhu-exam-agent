"""记忆处理器：摘要、查看、纠正、删除排除及故障降级。"""

import logging
import re
from typing import Any

from app.conversations.errors import (
    MemoryCorrectionUnsupportedError,
    MemoryEntryNotFoundError,
)
from app.conversations.graph import (
    ActionOutcome,
    ActionPlan,
)
from app.conversations.models import ActionResultItem
from app.conversations.reply import ReplyMaterial
from app.conversations.turn_types import (
    ExecuteTurn,
    _DynamicContext,
)
from app.memory.models import (
    MemoryCorrectionRequest,
    MemoryEntryView,
    parse_entry_id,
)
from app.memory.summary import (
    context_line,
    material_block,
)
from app.papers.courses import normalize_course

logger = logging.getLogger("app.conversations.service")

class MemoryHandlerMixin:
    """学习记忆的读取、写入、纠正与删除。"""

    _MEMORY_DELETE_MESSAGE = "从学习记忆和复习建议中移除，原会话记录保留。"

    _MEMORY_DELETE_REPEAT = "这条记忆之前已经移除，原会话记录保留。"

    _MEMORY_UNAVAILABLE_TEXT = (
        "【学习记忆】学习记忆暂时无法读取；回复时如实说明读不到，也不要编造复习建议。"
    )

    def _memory_course(
        self,
        ctx: _DynamicContext,
        action: ActionPlan | None,
        text: str,
        confirmed: str | None = None,
    ) -> str | None:
        """课程归属：工具参数 > 本轮明说 > 本轮调度确认 > 已存上下文 > 记忆默认课程。

        本轮明确换课时必须压过上一轮的记忆课程，否则学生说“换高数”仍会读到
        Python 的记录；历史课程的“反推”也在这里被排除。
        """

        if action is not None:
            course = action.parameters.get("course")
            if isinstance(course, str) and course.strip():
                return normalize_course(course.strip())
        if confirmed:
            return normalize_course(confirmed)
        if ctx.confirmed_course:
            return normalize_course(ctx.confirmed_course)
        if ctx.memory_course:
            return ctx.memory_course
        return None

    def _prepare_memory_context(
        self, prepared: ExecuteTurn, ctx: _DynamicContext, *, force: bool = False
    ) -> str | None:
        """本轮是否要注入学习记忆；读取失败必须与“没有记录”区分开。

        - 未配置记忆服务：不注入，也不影响其他能力；
        - 学生本轮没提复习/记忆且还没有上下文：不额外查库，避免每轮都读；
        - 读取失败：注入“暂时无法读取”，让回复如实说明。
        """

        if self._memory is None:
            return None
        if not force:
            return None
        course = self._memory_course(ctx, None, prepared.input.text)
        if course is None:
            ctx.memory_status = "needs_course"
            ctx.memory_text = (
                "【学习记忆】学生还没有确定课程：要给复习建议时先问清楚是哪一门课。"
            )
            return ctx.memory_text
        try:
            view = self._memory.summary(course, days=30, limit=50)
        except Exception:
            logger.warning("学习记忆读取失败", exc_info=True)
            ctx.memory_view = None
            ctx.memory_status = "unavailable"
            ctx.memory_text = self._MEMORY_UNAVAILABLE_TEXT
            return ctx.memory_text
        ctx.memory_view = view
        ctx.memory_course = course
        ctx.memory_status = view.status
        ctx.memory_text = context_line(view)
        return ctx.memory_text

    @staticmethod
    def _memory_notice(results: list[ActionResultItem]) -> str | None:
        """成功移除时必须原样告诉学生“原会话记录保留”。"""

        for item in results:
            if item.action != "delete_memory" or item.status != "success":
                continue
            message = (item.details or {}).get("memory_message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return None

    def _memory_notice_from_material(self, material: ReplyMaterial) -> str | None:
        """重做正文时也要保留“原会话记录保留”的原话。"""

        for item in material.results:
            if item.action != "delete_memory":
                continue
            if item.message in (
                self._MEMORY_DELETE_MESSAGE,
                self._MEMORY_DELETE_REPEAT,
            ):
                return item.message
        return None

    def _memory_material(self, ctx: _DynamicContext) -> str | None:
        if self._memory is None:
            return None
        if ctx.memory_status == "unavailable":
            return self._MEMORY_UNAVAILABLE_TEXT
        if ctx.memory_view is None:
            return None
        focus = set(ctx.memory_focus_entry_ids)
        try:
            return material_block(ctx.memory_view, focus_entry_ids=focus)
        except Exception:
            logger.warning("学习记忆材料生成失败", exc_info=True)
            return self._MEMORY_UNAVAILABLE_TEXT

    def _memory_review_outcome(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        ctx: _DynamicContext,
        text: str,
        *,
        confirmed_course: str | None = None,
    ) -> ActionOutcome:
        name = action.action
        if self._memory is None:
            return ActionOutcome(
                name,
                "failed",
                "学习记忆暂时不可用，请稍后再试。",
                error_code="MEMORY_UNAVAILABLE",
                retryable=True,
            )
        course = self._memory_course(ctx, action, text, confirmed_course)
        if course is None:
            return ActionOutcome(
                name,
                "clarification",
                "要给学习建议，得先知道是哪门课：你现在复习的是哪一门？",
                needs_user=True,
            )
        days = int(action.parameters.get("days") or 30)
        limit = int(action.parameters.get("limit") or 20)
        try:
            view = self._memory.summary(course, days=days, limit=limit)
        except Exception:
            logger.warning("学习记忆读取失败", exc_info=True)
            ctx.memory_status = "unavailable"
            ctx.memory_text = self._MEMORY_UNAVAILABLE_TEXT
            return ActionOutcome(
                name,
                "failed",
                "学习记忆暂时无法读取，请稍后再试。",
                error_code="MEMORY_UNAVAILABLE",
                retryable=True,
            )

        ctx.memory_view = view
        ctx.memory_course = course
        ctx.memory_status = view.status
        ctx.memory_text = context_line(view)
        entries = list(view.entries) + list(view.pending_course_entries)
        ctx.memory_entry_ids = {entry.entry_id for entry in entries}
        for entry in entries:
            ctx.memory_entry_ids.update(
                f"attempt:{attempt.attempt_id}" for attempt in entry.attempts
            )
        requested_focus = action.parameters.get("entry_ids") or []
        ctx.memory_focus_entry_ids = {
            str(entry_id)
            for entry_id in requested_focus
            if str(entry_id) in ctx.memory_entry_ids
        }

        if view.status == "empty":
            message = view.message
        else:
            message = view.range_text + "。"
            if view.suggestions:
                message += "建议先补：" + "；".join(view.suggestions)
            else:
                message += "暂时没有需要优先补的知识点。"
        details = {
            "status": view.status,
            "memory_course": course,
            "memory_range": view.range.model_dump(mode="json"),
            "memory_counts": view.counts.model_dump(mode="json"),
            "memory_groups": [
                group.model_dump(mode="json") for group in view.groups
            ],
            "memory_suggestions": list(view.suggestions),
            "memory_pending": view.counts.pending_course,
            # 待确认条目也要带编号：否则模型知道“有 11 条待确认”却没法纠正归属。
            "memory_entries": [self._memory_entry_detail(entry) for entry in entries],
        }
        model_details = {
            "memory_entries": [
                self._memory_entry_detail(entry, include_note=True) for entry in entries
            ],
        }
        return ActionOutcome(
            name, "success", message, details, model_details=model_details
        )

    @staticmethod
    def _memory_entry_detail(
        entry: MemoryEntryView, *, include_note: bool = False
    ) -> dict[str, Any]:
        """工具结果里的一条记忆：编号、首末变化和每次作答的编号。

        附注只进入模型上下文，不进入公开 details；公开结果会经 SSE 出站。
        """

        payload = {
            "entry_id": entry.entry_id,
            "kind": entry.kind,
            "course": entry.course,
            "course_source": entry.course_source,
            "pending_course": entry.course is None,
            "verdict": entry.verdict,
            "first_verdict": entry.first_verdict,
            "knowledge_points": entry.knowledge_points,
            "validity": entry.validity,
            "validity_note": entry.validity_note,
            "recorded_at": entry.recorded_at.isoformat(),
            "first_at": entry.first_at.isoformat() if entry.first_at else None,
            "wrong_at": entry.wrong_at.isoformat() if entry.wrong_at else None,
            "source": entry.source,
            "attempts": [
                {
                    "attempt_id": attempt.attempt_id,
                    "attempt_no": attempt.attempt_no,
                    "kind": attempt.kind,
                    "verdict": attempt.verdict,
                    "is_correct": attempt.is_correct,
                    "independence": attempt.independence,
                    "recorded_at": attempt.recorded_at.isoformat(),
                    "excluded": attempt.excluded,
                }
                for attempt in entry.attempts
            ],
        }
        if include_note:
            payload["note"] = entry.note
        return payload

    def _memory_write_target(
        self, action: ActionPlan, ctx: _DynamicContext
    ) -> tuple[tuple[str, str] | None, ActionOutcome | None]:
        """只允许改本轮真实返回过的条目，防止模型凭记忆拼 entry_id。"""

        raw = action.parameters.get("entry_id")
        parsed = parse_entry_id(raw if isinstance(raw, str) else "")
        if parsed is None:
            return None, ActionOutcome(
                action.action,
                "failed",
                "条目编号不合法：先查看学习记忆，再按返回的编号操作。",
                error_code="INVALID_INPUT",
                needs_user=True,
            )
        entry_id = f"{parsed[0]}:{parsed[1]}"
        if entry_id not in ctx.memory_entry_ids:
            return None, ActionOutcome(
                action.action,
                "failed",
                "只能修改本轮学习记忆结果里出现过的条目；我先帮你把记录列出来。",
                error_code="INVALID_INPUT",
                needs_user=True,
            )
        return parsed, None

    def _memory_correct_outcome(
        self, prepared: ExecuteTurn, action: ActionPlan, ctx: _DynamicContext
    ) -> ActionOutcome:
        name = action.action
        if self._memory is None:
            return ActionOutcome(
                name, "failed", "学习记忆暂时不可用，请稍后再试。",
                error_code="MEMORY_UNAVAILABLE", retryable=True,
            )
        parsed, error = self._memory_write_target(action, ctx)
        if error is not None:
            return error
        assert parsed is not None
        if parsed[0] == "attempt":
            return ActionOutcome(
                name,
                "failed",
                "单次作答只能整体移除，不能单独纠正归属。",
                error_code="INVALID_INPUT",
                needs_user=True,
            )
        request = MemoryCorrectionRequest(
            course=action.parameters.get("course"),
            knowledge_point=action.parameters.get("knowledge_point"),
            note=action.parameters.get("note"),
        )
        try:
            view = self._memory.correct(parsed[0], parsed[1], request)
        except MemoryEntryNotFoundError:
            return ActionOutcome(
                name,
                "failed",
                "没有这条学习记忆（原记录可能已经删除）。",
                error_code="NOT_FOUND",
                needs_user=True,
            )
        except MemoryCorrectionUnsupportedError as exc:
            return ActionOutcome(
                name, "failed", str(exc), error_code="INVALID_INPUT", needs_user=True
            )
        except Exception:
            logger.warning("学习记忆纠正失败", exc_info=True)
            return ActionOutcome(
                name,
                "failed",
                "学习记忆暂时无法更新，请稍后再试。",
                error_code="MEMORY_UNAVAILABLE",
                retryable=True,
            )

        # 纠正后立刻重新汇总：正文和下一轮建议都要读最新状态。
        ctx.memory_focus_entry_ids.add(f"{parsed[0]}:{parsed[1]}")
        self._prepare_memory_context(prepared, ctx, force=True)
        parts = [f"已把这条学习记忆更正为：课程 {view.course or '待确认'}"]
        if view.knowledge_points:
            parts.append("知识点 " + "、".join(view.knowledge_points))
        if view.note:
            parts.append("补充说明 " + view.note)
        return ActionOutcome(
            name,
            "success",
            "；".join(parts),
            {
                "memory_entry_id": view.entry_id,
                "memory_course": view.course,
                "memory_entry": view.model_dump(mode="json"),
            },
        )

    def _memory_delete_outcome(
        self, prepared: ExecuteTurn, action: ActionPlan, ctx: _DynamicContext
    ) -> ActionOutcome:
        name = action.action
        if self._memory is None:
            return ActionOutcome(
                name, "failed", "学习记忆暂时不可用，请稍后再试。",
                error_code="MEMORY_UNAVAILABLE", retryable=True,
            )
        parsed, error = self._memory_write_target(action, ctx)
        if error is not None:
            return error
        assert parsed is not None
        try:
            removed = self._memory.delete(
                parsed[0], parsed[1], reason=action.parameters.get("reason")
            )
        except MemoryEntryNotFoundError:
            return ActionOutcome(
                name,
                "failed",
                "没有这条学习记忆（原记录可能已经删除）。",
                error_code="NOT_FOUND",
                needs_user=True,
            )
        except Exception:
            logger.warning("学习记忆移除失败", exc_info=True)
            return ActionOutcome(
                name,
                "failed",
                "学习记忆暂时无法更新，请稍后再试。",
                error_code="MEMORY_UNAVAILABLE",
                retryable=True,
            )
        self._prepare_memory_context(prepared, ctx, force=True)
        ctx.memory_focus_entry_ids.add(f"{parsed[0]}:{parsed[1]}")
        message = (
            self._MEMORY_DELETE_MESSAGE if removed else self._MEMORY_DELETE_REPEAT
        )
        return ActionOutcome(
            name,
            "success",
            message,
            {
                "memory_entry_id": f"{parsed[0]}:{parsed[1]}",
                "memory_removed": removed,
                "memory_message": message,
            },
        )
