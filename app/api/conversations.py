"""本地会话、查询与辅导轮次接口。

接口层只做参数校验、依赖组装和响应输出。会话不存在、请求冲突、存储不可用、
图片/来源不合法和模型配置缺失由 app/main.py 的异常处理器统一映射。
"""

from dataclasses import dataclass
from typing import Annotated, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.query import get_query_config
from app.config import QueryConfigState
from app.conversations.dispatch import DeepSeekIntentRouter
from app.conversations.graph import SchedulerAgent
from app.tutoring.exercise_llm import DeepSeekExerciseAgent
from app.conversations.errors import ConversationStorageError
from app.conversations.models import (
    ConversationDetail,
    ConversationListResponse,
    ConversationSummary,
    ExerciseView,
    TurnKind,
)
from app.conversations.service import (
    ConversationService,
    ConversationStoreState,
    TurnInput,
)
from app import images
from app.mistakes.service import MistakeRecordService
from app.papers.service import PaperService, get_paper_service
from app.querying.llm import DeepSeekUnderstanding
from app.querying.service import SSE_HEADERS, QueryService
from app.sse import encode_sse_event
from app.tutoring.llm import DeepSeekTutor
from app.tutoring.models import Source

router = APIRouter(tags=["conversations"])

MAX_QUERY_LENGTH = 1000
MAX_TUTORING_TEXT = 2000
MAX_STUDENT_ANSWER = 2000
MAX_SOLUTION_TEXT = 4000


class CreateConversationRequest(BaseModel):
    """POST /api/conversations 接收空对象。"""

    model_config = ConfigDict(extra="forbid")


class AttachmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    role: Literal["statement", "solution", "supplement"]


class TurnRequest(BaseModel):
    """POST /api/conversations/{id}/turns 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    # auto 表示让服务端判断查询/辅导；省略 kind 是 F004 旧客户端的查询语义。
    kind: TurnKind | Literal["auto"] | None = None
    query: str = ""
    attachments: list[AttachmentInput] = Field(default_factory=list)
    student_answer: str | None = None
    solution_text: str | None = None
    source: Source = Field(default_factory=Source)
    problem_id: UUID | None = None
    exercise_id: UUID | None = None
    start_new: bool = False
    retry_of: UUID | None = None
    # 订正/补充是练习作答的增量行为；不指定时由服务端按现有尝试推断。
    attempt_kind: Literal["supplement", "correction"] | None = None

    @field_validator("query")
    @classmethod
    def _normalize_query(cls, value: str) -> str:
        text = value.strip()
        if len(text) > MAX_TUTORING_TEXT:
            raise ValueError(f"本轮文字最长为 {MAX_TUTORING_TEXT} 个字符。")
        return text

    @field_validator("student_answer")
    @classmethod
    def _normalize_answer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if len(text) > MAX_STUDENT_ANSWER:
            raise ValueError(f"学生答案最长为 {MAX_STUDENT_ANSWER} 个字符。")
        return text or None

    @field_validator("solution_text")
    @classmethod
    def _normalize_solution(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if len(text) > MAX_SOLUTION_TEXT:
            raise ValueError(f"解题过程最长为 {MAX_SOLUTION_TEXT} 个字符。")
        return text or None

    @model_validator(mode="after")
    def _require_input(self) -> "TurnRequest":
        if self.attempt_kind is not None and self.kind not in (
            "exercise",
            "exercise_answer",
        ):
            raise ValueError("只有练习作答轮次可以指定补充或订正。")
        if self.retry_of is not None:
            if (self.kind is not None or self.query or self.attachments or self.student_answer
                or self.solution_text or self.problem_id or self.exercise_id
                or self.start_new or self.source.type != "none"
                or self.attempt_kind is not None):
                raise ValueError("重试只需提供 request_id 和 retry_of，原输入由服务端恢复。")
            return self
        if self.kind in ("exercise", "exercise_answer") and (self.student_answer or self.solution_text):
            return self
        if self.query or self.attachments:
            return self
        if self.kind in ("record", "feedback"):
            return self
        raise ValueError("请提供文字或图片。")


@dataclass(frozen=True)
class ConversationServiceState:
    """依赖只组装状态，不在参数校验前抛存储错误。"""

    service: ConversationService | None
    error: str | None


def get_conversation_service_state(
    request: Request,
    config: Annotated[QueryConfigState, Depends(get_query_config)],
    papers: Annotated[PaperService, Depends(get_paper_service)],
) -> ConversationServiceState:
    """按启动时的数据库状态和查询配置组装服务状态。

    读取历史不需要模型密钥；只有真正执行模型轮次时才创建适配器。
    """

    state: ConversationStoreState = request.app.state.conversation_store
    if state.repository is None:
        return ConversationServiceState(
            service=None,
            error=state.error or "本地学习数据暂时不可用，请稍后再试。",
        )

    settings = config.settings
    make_query: Callable[[], QueryService] | None = None
    make_tutor: Callable[[], DeepSeekTutor] | None = None
    make_router: Callable[[], DeepSeekIntentRouter] | None = None
    make_exercise_agent: Callable[[], DeepSeekExerciseAgent] | None = None
    make_scheduler: Callable[[], SchedulerAgent] | None = None
    if settings is not None:

        def _make_query() -> QueryService:
            return QueryService(
                understand=DeepSeekUnderstanding(settings),
                papers=papers,
            )

        def _make_tutor() -> DeepSeekTutor:
            return DeepSeekTutor(settings)

        def _make_router() -> DeepSeekIntentRouter:
            return DeepSeekIntentRouter(settings)

        def _make_exercise_agent() -> DeepSeekExerciseAgent:
            return DeepSeekExerciseAgent(settings)

        def _make_scheduler() -> SchedulerAgent:
            return SchedulerAgent(settings)

        make_query = _make_query
        make_tutor = _make_tutor
        make_router = _make_router
        make_exercise_agent = _make_exercise_agent
        make_scheduler = _make_scheduler

    records = MistakeRecordService(
        state.repository,
        papers,
        make_tutor=make_tutor,
        tutoring_error=config.error,
        attachments_root=images.ATTACHMENTS_ROOT,
    )
    return ConversationServiceState(
        service=ConversationService(
            repository=state.repository,
            make_query=make_query,
            query_error=config.error,
            make_tutor=make_tutor,
            tutoring_error=config.error,
            make_router=make_router,
            make_exercise_agent=make_exercise_agent,
            make_scheduler=make_scheduler,
            papers=papers,
            records=records,
            attachments_root=images.ATTACHMENTS_ROOT,
        ),
        error=None,
    )


def require_service(state: ConversationServiceState) -> ConversationService:
    """在参数校验通过后确认数据库可用；不可用才返回 503。"""

    if state.service is None:
        raise ConversationStorageError(
            state.error or "本地学习数据暂时不可用，请稍后再试。"
        )
    return state.service


@router.post(
    "/conversations",
    status_code=201,
    response_model=ConversationSummary,
    summary="新建本地会话",
)
def create_conversation(
    payload: CreateConversationRequest,
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> ConversationSummary:
    return require_service(state).create_conversation()


@router.get(
    "/conversations",
    response_model=ConversationListResponse,
    summary="列出本地会话",
)
def list_conversations(
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> ConversationListResponse:
    return require_service(state).list_conversations()


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    summary="读取会话及全部轮次",
)
def read_conversation(
    conversation_id: UUID,
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> ConversationDetail:
    return require_service(state).get_detail(str(conversation_id))


@router.post(
    "/conversations/{conversation_id}/turns",
    summary="在会话中提交一轮查询、辅导或记录操作",
)
async def create_turn(
    conversation_id: UUID,
    payload: TurnRequest,
    request: Request,
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> StreamingResponse:
    """返回 text/event-stream：progress 若干，最后一条 result 或 error。"""

    service = require_service(state)
    data = TurnInput(
        request_id=str(payload.request_id),
        kind=None if payload.kind == "auto" else payload.kind,
        dispatch=payload.kind == "auto",
        text=payload.query,
        attachments=tuple(
            (str(item.id), item.role) for item in payload.attachments
        ),
        student_answer_text=payload.student_answer,
        solution_text=payload.solution_text,
        source=payload.source,
        problem_id=(
            str(payload.problem_id) if payload.problem_id is not None else None
        ),
        start_new=payload.start_new,
        exercise_id=(
            str(payload.exercise_id) if payload.exercise_id is not None else None
        ),
        retry_of=str(payload.retry_of) if payload.retry_of is not None else None,
        attempt_kind=payload.attempt_kind,
    )
    prepared = service.prepare_turn(str(conversation_id), data)

    async def encode_stream():
        async for event in service.stream_prepared(prepared, request):
            yield encode_sse_event(event)

    return StreamingResponse(
        encode_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post(
    "/conversations/{conversation_id}/exercises/{exercise_id}/reveal",
    response_model=ExerciseView,
    summary="查看练习题参考解答",
)
def reveal_exercise_solution(
    conversation_id: UUID,
    exercise_id: UUID,
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> ExerciseView:
    service = require_service(state)
    ex = service._repository.reveal_exercise_solution(
        str(exercise_id), conversation_id=str(conversation_id)
    )
    return service._build_exercise_view(ex, allow_solution=True)
