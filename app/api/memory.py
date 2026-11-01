"""学习记忆接口：查看、课程列表、纠正与删除。

契约（与 F009 任务书一致）：
- ``GET /api/learning-memory``：按课程 + 时间窗返回摘要；课程未给出时只返回
  待确认归属的条目，不把多门课混成一份；
- ``GET /api/learning-memory/courses``：有记录的课程与待确认条数；
- ``PATCH /api/learning-memory/{kind}/{source_id}``：纠正课程/知识点/说明；
- ``DELETE /api/learning-memory/{kind}/{source_id}``：持续移除。

删除只写“排除标记”，原会话、练习与错题记录都不删。数据库不可用时统一 503，
错误体只含 ``detail``，不出现磁盘路径或 sqlite 原文。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.conversations.errors import ConversationStorageError
from app.memory.models import (
    LearningMemoryView,
    MemoryCorrectionRequest,
    MemoryCoursesView,
    MemoryEntryView,
)
from app.memory.service import MAX_DAYS, MAX_LIMIT, LearningMemoryService
from app.papers.service import PaperService, get_paper_service

router = APIRouter(prefix="/learning-memory", tags=["learning-memory"])

MemoryKind = Literal["exercise", "attempt", "selfreport"]

_STORAGE_MESSAGE = "本地学习数据暂时不可用，请稍后再试。"
_DELETE_MESSAGE = "从学习记忆和复习建议中移除，原会话记录保留。"
_DELETE_REPEAT = "这条记忆之前已经移除，原会话记录仍然保留。"


@dataclass(frozen=True)
class MemoryServiceState:
    service: LearningMemoryService | None
    error: str | None


def get_memory_service_state(
    request: Request,
    papers: Annotated[PaperService, Depends(get_paper_service)],
) -> MemoryServiceState:
    """数据库不可用时先返回状态，参数校验通过后再决定是否 503。"""

    state = request.app.state.conversation_store
    if state.repository is None:
        return MemoryServiceState(
            service=None,
            error=state.error or _STORAGE_MESSAGE,
        )
    return MemoryServiceState(
        service=LearningMemoryService(state.repository.db_path, papers=papers),
        error=None,
    )


def require_memory_service(state: MemoryServiceState) -> LearningMemoryService:
    if state.service is None:
        raise ConversationStorageError(state.error or _STORAGE_MESSAGE)
    return state.service


@router.get("", response_model=LearningMemoryView)
def get_learning_memory(
    state: Annotated[MemoryServiceState, Depends(get_memory_service_state)],
    course: Annotated[str | None, Query(max_length=40)] = None,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 30,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
) -> LearningMemoryView:
    service = require_memory_service(state)
    return service.summary(course, days=days, limit=limit)


@router.get("/courses", response_model=MemoryCoursesView)
def get_learning_memory_courses(
    state: Annotated[MemoryServiceState, Depends(get_memory_service_state)],
) -> MemoryCoursesView:
    return require_memory_service(state).courses()


@router.patch("/{kind}/{source_id}", response_model=MemoryEntryView)
def correct_learning_memory(
    kind: MemoryKind,
    source_id: UUID,
    payload: MemoryCorrectionRequest,
    state: Annotated[MemoryServiceState, Depends(get_memory_service_state)],
) -> MemoryEntryView:
    service = require_memory_service(state)
    return service.correct(kind, str(source_id), payload)


@router.delete("/{kind}/{source_id}")
def delete_learning_memory(
    kind: MemoryKind,
    source_id: UUID,
    state: Annotated[MemoryServiceState, Depends(get_memory_service_state)],
    reason: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, object]:
    service = require_memory_service(state)
    removed = service.delete(kind, str(source_id), reason=reason)
    return {
        "entry_id": f"{kind}:{source_id}",
        "removed": removed,
        "message": _DELETE_MESSAGE if removed else _DELETE_REPEAT,
    }


__all__ = ["router"]