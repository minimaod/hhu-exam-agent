"""F009：学习记忆用例服务。

这里把三件事串起来：
1. 用 ``evidence`` 从既有记录投影证据（只读）；
2. 用 ``repository`` 叠加用户的纠正与删除排除；
3. 用 ``summary`` 生成页面视图、决策上下文和正文材料。

失败策略：读取失败抛 ``ConversationStorageError``，由接口层映射成 503，
并让上层（Agent）能用“暂时无法读取”而不是“没有记录”来回复；写入失败同样
回滚，不产生半条记忆。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.conversations.errors import (
    ConversationStorageError,
    MemoryCorrectionUnsupportedError,
    MemoryEntryNotFoundError,
)
from app.memory.evidence import (
    EntryEvidence,
    KnowledgeNormalizer,
    collect_all,
    collect_evidence,
    select_window,
)
from app.memory.models import (
    MemoryCorrectionRequest,
    MemoryCourseCount,
    MemoryCoursesView,
    MemoryEntryView,
    LearningMemoryView,
    build_entry_id,
)
from app.memory.repository import UNSET, MemoryRepository
from app.memory.summary import (
    build_range,
    build_view,
    context_line,
    empty_view,
    material_block,
)
from app.papers.courses import normalize_course

logger = logging.getLogger(__name__)

MAX_DAYS = 365
MAX_LIMIT = 50
DEFAULT_DAYS = 30
DEFAULT_LIMIT = 20


class LearningMemoryService:
    def __init__(
        self,
        db_path: Path,
        *,
        papers: Any | None = None,
        taxonomy_path: Path | None = None,
        current_file: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = MemoryRepository(Path(db_path))
        self._papers = papers
        self._taxonomy_path = taxonomy_path
        self._current_file = current_file
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._normalizer: KnowledgeNormalizer | None = None
        self._paper_cache: dict[str, tuple[str | None, str | None]] = {}
        self._build_id: str | None | object = _UNSET_BUILD

    @property
    def db_path(self) -> Path:
        return self._repository.db_path

    # ------------------------------------------------------------------
    # 依赖与降级
    # ------------------------------------------------------------------

    def _knowledge(self) -> KnowledgeNormalizer:
        if self._normalizer is None:
            taxonomy = None
            path = self._taxonomy_path or _default_taxonomy_path()
            if path is not None and Path(path).is_file():
                try:
                    from app.question_bank.taxonomy import load_taxonomy

                    taxonomy = load_taxonomy(Path(path))
                except Exception:
                    logger.info("知识点词表不可用，学习记忆保留原标签", exc_info=True)
                    taxonomy = None
            self._normalizer = KnowledgeNormalizer(taxonomy)
        return self._normalizer

    def _current_build(self) -> str | None:
        if self._build_id is _UNSET_BUILD:
            self._build_id = self._read_build_id()
        return self._build_id  # type: ignore[return-value]

    def _read_build_id(self) -> str | None:
        path = self._current_file or _default_current_file()
        if path is None:
            return None
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return None
        build = payload.get("build_id") if isinstance(payload, dict) else None
        return str(build) if build else None

    def _lookup_paper(self, paper_id: str | None) -> tuple[str | None, str | None]:
        if not paper_id:
            return None, None
        if paper_id in self._paper_cache:
            return self._paper_cache[paper_id]
        course: str | None = None
        title: str | None = None
        if self._papers is not None:
            try:
                paper = self._papers.get_paper(paper_id)
            except Exception:
                paper = None
            if paper is not None:
                course = getattr(paper, "course", None)
                title = getattr(paper, "title", None)
        result = (course, title)
        self._paper_cache[paper_id] = result
        return result

    def _entries(self) -> tuple[list[EntryEvidence], int]:
        """全部证据（含待确认），已应用纠正与删除排除。"""

        with self._repository.read() as connection:
            bundle = collect_all(
                connection,
                paper_lookup=self._lookup_paper,
                normalizer=self._knowledge(),
                corrections=self._repository.corrections(),
                exclusions=self._repository.exclusions(),
                current_build_id=self._current_build(),
            )
        return bundle.entries, bundle.excluded_entries

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def summary(
        self,
        course: str | None,
        *,
        days: int = DEFAULT_DAYS,
        limit: int = DEFAULT_LIMIT,
    ) -> LearningMemoryView:
        days = max(1, min(MAX_DAYS, int(days)))
        limit = max(1, min(MAX_LIMIT, int(limit)))
        now = self._clock()
        with self._repository.read() as connection:
            bundle = collect_evidence(
                connection,
                course=course,
                paper_lookup=self._lookup_paper,
                normalizer=self._knowledge(),
                corrections=self._repository.corrections(),
                exclusions=self._repository.exclusions(),
                current_build_id=self._current_build(),
                now=now,
                days=days,
                limit=limit,
            )
        if not bundle.entries:
            view = empty_view(course, days=days, limit=limit, now=now)
            view.counts.excluded = bundle.excluded_entries
            view.counts.pending_course = bundle.pending_total
            view.pending_course_entries = [item.to_view() for item in bundle.pending]
            if course is None:
                view.message = "学习记忆按课程分开：先选一门课，再查看对应的记录。"
                view.range_text = "尚未选择课程"
            return view
        window = build_range(
            days=days,
            limit=limit,
            now=now,
            entries=bundle.entries,
            fallback=bundle.fallback_used,
        )
        return build_view(
            status="ok",
            course=course,
            window=window,
            entries=bundle.entries,
            pending=bundle.pending,
            excluded_entries=bundle.excluded_entries,
            cutoff=now - timedelta(days=days),
            pending_total=bundle.pending_total,
        )

    def courses(self) -> MemoryCoursesView:
        entries, _ = self._entries()
        counts: dict[str, int] = {}
        pending = 0
        for entry in entries:
            if entry.course is None:
                pending += 1
                continue
            counts[entry.course] = counts.get(entry.course, 0) + 1
        items = [
            MemoryCourseCount(course=course, label=course, evidence=count)
            for course, count in sorted(counts.items(), key=lambda pair: -pair[1])
        ]
        return MemoryCoursesView(items=items, pending=pending)

    def _entry_view(self, entry_id: str) -> MemoryEntryView | None:
        entries, _ = self._entries()
        for entry in entries:
            if entry.entry_id == entry_id:
                return entry.to_view()
        return None

    def entry_course(self, kind: str, source_id: str) -> str | None:
        view = self._entry_view(build_entry_id(kind, source_id))
        return view.course if view else None

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def correct(
        self,
        kind: str,
        source_id: str,
        request: MemoryCorrectionRequest,
    ) -> MemoryEntryView:
        if kind == "attempt":
            raise MemoryCorrectionUnsupportedError(
                "单次作答只能整体移除，不能单独纠正归属。"
            )
        if kind not in ("exercise", "selfreport"):
            raise MemoryEntryNotFoundError("没有这条学习记忆。")
        if not request.has_change():
            raise MemoryCorrectionUnsupportedError(
                "请说明要纠正的课程、知识点或补充说明。"
            )
        entry_key = build_entry_id(kind, source_id)
        if not self._repository.entry_exists(kind, source_id):
            raise MemoryEntryNotFoundError("没有这条学习记忆。")
        self._repository.upsert_correction(
            entry_key,
            course=(
                UNSET
                if request.course is None
                else (request.course if request.course else "")
            ),
            knowledge_point=(
                UNSET if request.knowledge_point is None else request.knowledge_point
            ),
            note=UNSET if request.note is None else request.note,
        )
        view = self._entry_view(entry_key)
        if view is None:
            # 纠正后可能被改到其他课程：仍要返回最新状态，不能报“成功但查不到”。
            raise ConversationStorageError("纠正已保存，但重新读取学习记忆失败。")
        return view

    def delete(
        self, kind: str, source_id: str, *, reason: str | None = None
    ) -> bool:
        """写持续排除；返回 True 表示本次新移除，False 表示之前已移除。"""

        if kind not in ("exercise", "attempt", "selfreport"):
            raise MemoryEntryNotFoundError("没有这条学习记忆。")
        entry_key = build_entry_id(kind, source_id)
        if not self._repository.entry_exists(kind, source_id):
            raise MemoryEntryNotFoundError("没有这条学习记忆。")
        return self._repository.add_exclusion(entry_key, reason)

    # ------------------------------------------------------------------
    # 给 Agent 的两个入口
    # ------------------------------------------------------------------

    def scheduler_context(
        self, course: str | None, *, days: int = DEFAULT_DAYS
    ) -> str:
        view = self.summary(course, days=days, limit=MAX_LIMIT)
        return context_line(view)

    def reply_material(
        self, course: str | None, *, days: int = DEFAULT_DAYS
    ) -> str | None:
        view = self.summary(course, days=days, limit=MAX_LIMIT)
        if view.status != "ok":
            return view.message or "暂时无法读取学习记忆"
        return material_block(view)


_UNSET_BUILD: object = object()


def _default_taxonomy_path() -> Path | None:
    try:
        from app.question_bank.config import DEFAULT_BANK_ROOT

        return Path(DEFAULT_BANK_ROOT) / "taxonomy" / "knowledge-points.json"
    except Exception:
        return None


def _default_current_file() -> Path | None:
    try:
        from app.question_bank.config import DEFAULT_BANK_ROOT

        return Path(DEFAULT_BANK_ROOT) / "db" / "current.json"
    except Exception:
        return None


__all__ = [
    "DEFAULT_DAYS",
    "DEFAULT_LIMIT",
    "LearningMemoryService",
    "MAX_DAYS",
    "MAX_LIMIT",
    "select_window",
]