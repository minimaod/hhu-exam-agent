"""本地会话、查询与辅导轮次接口。

接口层只做参数校验、依赖组装和响应输出。会话不存在、请求冲突、存储不可用、
图片/来源不合法和模型配置缺失由 app/main.py 的异常处理器统一映射。
"""

import asyncio
import logging
from contextlib import aclosing, suppress
from dataclasses import dataclass
from typing import Annotated, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.materials import _material_service
from app.api.query import get_query_config
from app.auth.dependencies import require_user
from app.auth.models import User
from app.config import MaterialConfigState, QueryConfigState
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
    TurnProgressEvent,
)
from app.conversations.reply import StreamingReply
from app.conversations.repository import ConversationRepository
from app.conversations.service import (
    ConversationService,
    ConversationStoreState,
    TurnInput,
)
from app.conversations.turn_types import ExecuteTurn
from app import images
from app.materials.errors import MaterialCoverageRequiredError, MaterialTooLargeError
from app.materials.models import (
    ArtifactInputMode,
    ArtifactDetailView,
    ConversationDeleteImpactView,
    ConversationMaterialItem,
    ConversationMaterialProgressResponse,
    ConversationMaterialUploadResponse,
    ConversationMaterialsResponse,
    OutputKind,
)
from app.mistakes.service import MistakeRecordService
from app.memory.service import LearningMemoryService
from app.papers.service import PaperService, get_paper_service
from app.querying.llm import DeepSeekUnderstanding
from app.querying.service import SSE_HEADERS, QueryService
from app.questions.models import QuestionCard
from app.questions.runtime import QuestionRuntime
from app.sse import encode_sse_event
from app.streaming import ClosingStreamingResponse, DISCONNECT_POLL_SECONDS
from app.tutoring.llm import DeepSeekTutor
from app.tutoring.models import Source

router = APIRouter(tags=["conversations"])
logger = logging.getLogger(__name__)

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
    # 真题轮次：题目来自某次查题轮次的 question_results。
    question_id: str | None = Field(default=None, max_length=200)
    question_source_turn: UUID | None = None
    # 卡片按钮的确定性动作；只允许针对 question_id 指定的题，避免串到当前题。
    question_action: Literal["exercise", "record"] | None = None
    # F019 第二阶段：个人资料清单项跳转学习；只传 ID，材料由后端校验并加载。
    material_guide_id: UUID | None = None
    material_item_id: UUID | None = None
    material_study_mode: Literal["explain", "exercise", "evaluate"] | None = None
    material_artifact_id: UUID | None = None
    material_question_id: UUID | None = None
    material_kc_id: str | None = Field(default=None, max_length=60)
    material_workbench_view: Literal["notes", "graph", "quiz"] | None = None
    material_source_pages: list[int] = Field(default_factory=list, max_length=40)

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
                or self.attempt_kind is not None or self.question_id
                or self.question_source_turn is not None
                or self.question_action is not None):
                raise ValueError("重试只需提供 request_id 和 retry_of，原输入由服务端恢复。")
            return self
        if self.question_action is not None:
            if self.kind not in (None, "auto"):
                raise ValueError("真题动作只能用于自动轮次。")
            if not self.question_id or self.question_source_turn is None:
                raise ValueError("真题动作必须提供 question_id 和 question_source_turn。")
            if (self.attachments or self.student_answer or self.solution_text
                    or self.problem_id or self.start_new
                    or self.source.type != "none"):
                raise ValueError("真题动作不能携带作答、图片或另一个题目引用。")
            return self
        if self.kind in ("select_question", "explain_question", "view_question_answer"):
            if not self.question_id or self.question_source_turn is None:
                raise ValueError("真题轮次必须提供 question_id 和 question_source_turn。")
            if self.solution_text or self.attachments or (
                self.student_answer and self.kind != "explain_question"
            ):
                raise ValueError("仅真题讲解可以携带所选答案；不支持附带图片或解题过程。")
            return self
        if self.question_id or self.question_source_turn is not None:
            raise ValueError("只有真题轮次可以携带题目引用。")
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
    user: Annotated[User, Depends(require_user)],
) -> ConversationServiceState:
    """按启动时的数据库状态和查询配置组装服务状态。

    读取历史不需要模型密钥；只有真正执行模型轮次时才创建适配器。
    """

    state: ConversationStoreState = request.app.state.conversation_store
    if state.db_path is None or state.error is not None:
        return ConversationServiceState(
            service=None,
            error=state.error or "本地学习数据暂时不可用，请稍后再试。",
        )
    repository = ConversationRepository(state.db_path).scoped(user.id)

    settings = config.settings
    client = getattr(request.app.state, "model_http_client", None)
    quota = getattr(request.app.state, "usage_gate", None)
    usage_config = getattr(request.app.state, "usage_config", None)
    usage_error = getattr(usage_config, "error", None)
    if usage_error:
        # 额度配置无效时不创建任何真实模型适配器，避免绕过记账。
        settings = None
    lease_registry = getattr(request.app.state, "lease_registry", None)
    make_query: Callable[[], QueryService] | None = None
    make_tutor: Callable[[], DeepSeekTutor] | None = None
    make_router: Callable[[], DeepSeekIntentRouter] | None = None
    make_exercise_agent: Callable[[], DeepSeekExerciseAgent] | None = None
    make_scheduler: Callable[[], SchedulerAgent] | None = None
    make_reply: Callable[[], StreamingReply] | None = None
    make_auth_verifier: Callable[[], Any] | None = None
    if settings is not None:

        def _make_query() -> QueryService:
            return QueryService(
                understand=DeepSeekUnderstanding(
                    settings, http_async_client=client
                ),
                papers=papers,
            )

        def _make_tutor() -> DeepSeekTutor:
            return DeepSeekTutor(settings, http_async_client=client)

        def _make_router() -> DeepSeekIntentRouter:
            return DeepSeekIntentRouter(settings, http_async_client=client)

        def _make_exercise_agent() -> DeepSeekExerciseAgent:
            return DeepSeekExerciseAgent(settings, http_async_client=client)

        def _make_scheduler() -> SchedulerAgent:
            return SchedulerAgent(settings, http_async_client=client)

        def _make_reply() -> StreamingReply:
            return StreamingReply(settings, http_async_client=client)

        def _make_auth_verifier():
            from app.conversations.authorization import DeepSeekAuthVerifier

            return DeepSeekAuthVerifier(settings, http_async_client=client)

        make_query = _make_query
        make_tutor = _make_tutor
        make_router = _make_router
        make_exercise_agent = _make_exercise_agent
        make_scheduler = _make_scheduler
        make_reply = _make_reply
        make_auth_verifier = _make_auth_verifier

    records = MistakeRecordService(
        repository,
        papers,
        make_tutor=make_tutor,
        tutoring_error=config.error,
        attachments_root=images.ATTACHMENTS_ROOT,
    )
    runtime: QuestionRuntime | None = getattr(
        request.app.state, "question_runtime", None
    )
    memory = LearningMemoryService(
        state.db_path, papers=papers, owner_id=user.id
    )
    return ConversationServiceState(
        service=ConversationService(
            repository=repository,
            make_query=make_query,
            query_error=config.error,
            make_tutor=make_tutor,
            tutoring_error=config.error,
            make_router=make_router,
            make_exercise_agent=make_exercise_agent,
            make_scheduler=make_scheduler,
            make_reply=make_reply,
            make_auth_verifier=make_auth_verifier,
            papers=papers,
            records=records,
            memory=memory,
            make_material_service=lambda: _material_service(request, user),
            attachments_root=images.ATTACHMENTS_ROOT,
            question_runtime=runtime,
            quota=quota,
            usage_error=usage_error,
            lease_registry=lease_registry,
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
    user: Annotated[User, Depends(require_user)],
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
        dispatch=payload.kind == "auto" or payload.question_action is not None,
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
        question_id=payload.question_id,
        question_source_turn=(
            str(payload.question_source_turn)
            if payload.question_source_turn is not None
            else None
        ),
        question_action=payload.question_action,
        material_guide_id=(
            str(payload.material_guide_id)
            if payload.material_guide_id is not None
            else None
        ),
        material_item_id=(
            str(payload.material_item_id)
            if payload.material_item_id is not None
            else None
        ),
        material_study_mode=payload.material_study_mode,
        material_artifact_id=(
            str(payload.material_artifact_id)
            if payload.material_artifact_id is not None
            else None
        ),
        material_question_id=(
            str(payload.material_question_id)
            if payload.material_question_id is not None
            else None
        ),
        material_kc_id=payload.material_kc_id,
        material_workbench_view=payload.material_workbench_view,
        material_source_pages=tuple(payload.material_source_pages),
    )
    prepared = service.prepare_turn(str(conversation_id), data)
    if isinstance(prepared, ExecuteTurn) and prepared.operation == "dispatch":
        return _stream_dispatch_turn(request, user.id, service, prepared)

    async def encode_stream():
        async for event in service.stream_prepared(prepared, request):
            yield encode_sse_event(event)

    return StreamingResponse(
        encode_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


class _WorkerRequest(Request):
    async def is_disconnected(self) -> bool:
        # The response owns subscription lifetime. The worker owns the turn.
        return False


def _material_turn_tasks(
    request: Request,
) -> dict[tuple[str, str, str], asyncio.Task[None]]:
    tasks = getattr(request.app.state, "material_turn_tasks", None)
    if tasks is None:
        tasks = request.app.state.material_turn_tasks = {}
    return tasks


def _stream_dispatch_turn(
    request: Request, owner_id: str, service: ConversationService, prepared: ExecuteTurn
) -> StreamingResponse:
    tasks = _material_turn_tasks(request)
    key = (owner_id, prepared.conversation_id, prepared.request_id)
    events: asyncio.Queue[str | None] = asyncio.Queue()
    subscribed = True
    started = False

    async def run_turn() -> None:
        nonlocal started
        started = True
        try:
            worker_request = _WorkerRequest(request.scope)
            async with aclosing(service.stream_prepared(prepared, worker_request)) as stream:
                async for event in stream:
                    if subscribed and isinstance(event, TurnProgressEvent) and event.stage == "generating_material":
                        tasks[key] = worker
                    if subscribed:
                        events.put_nowait(encode_sse_event(event))
        except Exception:
            logger.exception("会话轮次执行失败")
        finally:
            tasks.pop(key, None)
            if subscribed:
                events.put_nowait(None)

    worker = asyncio.create_task(run_turn())

    def unsubscribe() -> None:
        nonlocal subscribed
        if not subscribed:
            return
        subscribed = False
        if key not in tasks and not worker.done() and not worker.cancelling():
            if started:
                worker.cancel()
            else:
                # Let stream_prepared enter its lease finalizer before cancellation.
                asyncio.get_running_loop().call_soon(worker.cancel)

    async def encode_stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(events.get(), timeout=DISCONNECT_POLL_SECONDS)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    continue
                if event is None:
                    return
                yield event
        finally:
            unsubscribe()
            if key not in tasks:
                with suppress(asyncio.CancelledError):
                    await worker

    return ClosingStreamingResponse(
        encode_stream(), on_close=unsubscribe, media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.post(
    "/conversations/{conversation_id}/turns/{request_id}/stop-material",
    status_code=204,
    summary="停止当前会话中的资料生成轮次",
)
async def stop_material_turn(
    conversation_id: UUID,
    request_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    state: Annotated[ConversationServiceState, Depends(get_conversation_service_state)],
) -> Response:
    await run_in_threadpool(require_service(state)._require_owned_conversation, str(conversation_id))
    task = _material_turn_tasks(request).get((user.id, str(conversation_id), str(request_id)))
    if task is not None and not task.cancelling():
        task.cancel()
    return Response(status_code=204)


@router.post(
    "/conversations/{conversation_id}/turns/{request_id}/reply",
    summary="只重新生成某一轮的正文，不重跑已完成的工具操作",
)
async def regenerate_turn_reply(
    conversation_id: UUID,
    request_id: UUID,
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> dict[str, object]:
    service = require_service(state)
    return await service.regenerate_reply(str(conversation_id), str(request_id))


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
    return service.reveal_exercise_solution(
        str(conversation_id), str(exercise_id)
    )


_QUESTION_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/conversations/{conversation_id}/questions/{question_id}/source")
def read_question_source(
    conversation_id: UUID,
    question_id: str,
    build: Annotated[str, Query(min_length=1, max_length=100)],
    kind: Literal["statement", "answer"],
    page: Annotated[int, Query(ge=1)],
    state: Annotated[ConversationServiceState, Depends(get_conversation_service_state)],
    download: bool = False,
) -> FileResponse:
    payload = require_service(state).question_source(
        str(conversation_id), question_id, build, kind, page
    )
    return FileResponse(
        payload.path, media_type="application/pdf", filename="source.pdf",
        content_disposition_type="attachment" if download else "inline", headers=_QUESTION_HEADERS,
    )


@router.get(
    "/conversations/{conversation_id}/questions/{question_id}/assets/{asset_id}",
    summary="读取已选中真题的题面图片",
)
def read_question_asset(
    conversation_id: UUID,
    question_id: str,
    asset_id: str,
    build: Annotated[str, Query(min_length=1, max_length=100)],
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> Response:
    """只允许本会话已选中的题目、且 build 与选中时一致。"""

    payload = require_service(state).question_asset(
        str(conversation_id), question_id, asset_id, build
    )
    return Response(
        content=payload.path.read_bytes(),
        media_type=payload.mime,
        headers=_QUESTION_HEADERS,
    )


@router.get(
    "/conversations/{conversation_id}/questions/{question_id}/answer",
    summary="读取已开放的真题答案",
)
def read_question_answer(
    conversation_id: UUID,
    question_id: str,
    build: Annotated[str, Query(min_length=1, max_length=100)],
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> JSONResponse:
    """未开放答案时 409；来源变化时 409/410，不返回答案正文。"""

    card: QuestionCard = require_service(state).question_answer(
        str(conversation_id), question_id, build
    )
    return JSONResponse(
        content=card.model_dump(mode="json"), headers=_QUESTION_HEADERS
    )


# ---------------------------------------------------------------------------
# F019 第三阶段：会话内个人资料上传、关联、按需生成与删除影响接口
# ---------------------------------------------------------------------------


class ConversationMaterialGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage_mode: ArtifactInputMode = "full"

    start_page: int | None = Field(default=None, ge=1)
    end_page: int | None = Field(default=None, ge=1)
    course_hint: str | None = Field(default=None, max_length=80)
    outputs: list[OutputKind] = Field(
        default_factory=lambda: ["notes", "quiz", "graph"],
        min_length=1,
        max_length=3,
    )


class ConversationMaterialGenerateResponse(BaseModel):
    artifact: ArtifactDetailView
    turn_request_id: str | None = None


class ConversationMaterialDetachResponse(BaseModel):
    detached: bool = True
    conversation_id: str
    material_id: str


class DeleteConversationResponse(BaseModel):
    deleted: bool = True
    conversation_id: str


@router.get(
    "/conversations/{conversation_id}/materials",
    response_model=ConversationMaterialsResponse,
    summary="列出会话关联的个人 PDF 资料与生成产物",
)
def list_conversation_materials(
    conversation_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> ConversationMaterialsResponse:
    require_service(state)._require_owned_conversation(str(conversation_id))
    items = _material_service(request, user).list_conversation_materials(
        str(conversation_id)
    )
    return ConversationMaterialsResponse(
        conversation_id=str(conversation_id), items=items
    )


@router.post(
    "/conversations/{conversation_id}/materials/upload",
    status_code=201,
    response_model=ConversationMaterialUploadResponse,
    summary="在会话内上传个人 PDF 资料并做基础可读性检查",
)
async def upload_conversation_material(
    conversation_id: UUID,
    file: Annotated[UploadFile, File(...)],
    request: Request,
    user: Annotated[User, Depends(require_user)],
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> ConversationMaterialUploadResponse:
    conv_svc = require_service(state)
    conv_svc._require_owned_conversation(str(conversation_id))
    mat_svc = _material_service(request, user)
    config: MaterialConfigState = getattr(
        request.app.state, "material_config", MaterialConfigState()
    )
    data = await file.read(config.max_bytes + 1)
    if len(data) > config.max_bytes:
        mb = config.max_bytes // (1024 * 1024)
        raise MaterialTooLargeError(f"单个 PDF 文件不能超过 {mb} MB。")
    res = await mat_svc.upload_pdf_for_conversation(
        conversation_id=str(conversation_id),
        filename=file.filename,
        data=data,
    )
    req_id = conv_svc.record_material_upload_turn(
        str(conversation_id),
        item=res.item,
        assistant_notice=res.assistant_notice,
        is_attach=False,
    )
    return ConversationMaterialUploadResponse(
        conversation_id=res.conversation_id,
        item=res.item,
        assistant_notice=res.assistant_notice,
        turn_request_id=req_id,
    )


@router.post(
    "/conversations/{conversation_id}/materials/{material_id}/attach",
    response_model=ConversationMaterialItem,
    summary="将已有个人资料关联到当前学习会话",
)
def attach_conversation_material(
    conversation_id: UUID,
    material_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> ConversationMaterialItem:
    conv_svc = require_service(state)
    conv_svc._require_owned_conversation(str(conversation_id))
    item = _material_service(request, user).attach_to_conversation(
        conversation_id=str(conversation_id),
        material_id=str(material_id),
    )
    ov = item.readability
    title = item.material.display_name.rsplit(".", 1)[0]
    if ov is not None and not ov.sufficient:
        notice = (
            f"已将《{title}》关联到当前会话（共 {item.material.page_count} 页）。"
            "该资料可用文字不足，目前仅支持原件预览。"
        )
    else:
        notice = (
            f"已将《{title}》关联到当前会话，共 {item.material.page_count} 页。"
            "你想先看章节笔记、做一套章节测试，还是看知识图谱？也可以指定页码。"
        )
    conv_svc.record_material_upload_turn(
        str(conversation_id),
        item=item,
        assistant_notice=notice,
        is_attach=True,
    )
    return item


@router.delete(
    "/conversations/{conversation_id}/materials/{material_id}",
    response_model=ConversationMaterialDetachResponse,
    summary="从当前会话移除个人资料关联（保留原 PDF 与既有成果）",
)
def detach_conversation_material(
    conversation_id: UUID,
    material_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> ConversationMaterialDetachResponse:
    require_service(state)._require_owned_conversation(str(conversation_id))
    _material_service(request, user).detach_from_conversation(
        conversation_id=str(conversation_id),
        material_id=str(material_id),
    )
    return ConversationMaterialDetachResponse(
        detached=True,
        conversation_id=str(conversation_id),
        material_id=str(material_id),
    )


@router.post(
    "/conversations/{conversation_id}/materials/{material_id}/generate",
    response_model=ConversationMaterialGenerateResponse,
    summary="在会话内按需生成章节笔记、章节测试或知识图谱",
)
async def generate_conversation_material_artifact(
    conversation_id: UUID,
    material_id: UUID,
    body: ConversationMaterialGenerateRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> ConversationMaterialGenerateResponse:
    conv_svc = require_service(state)
    conv_svc._require_owned_conversation(str(conversation_id))
    mat_svc = _material_service(request, user)
    mat_svc.attach_to_conversation(
        conversation_id=str(conversation_id), material_id=str(material_id)
    )
    detail = mat_svc.get_material_detail(str(material_id))
    max_range = getattr(
        getattr(mat_svc, "_config", None), "max_range_pages", 40
    )
    start_page = body.start_page or 1
    end_page = (
        body.end_page
        if body.end_page is not None
        else (
            start_page
            if body.start_page is not None
            else min(detail.material.page_count, max_range)
        )
    )
    try:
        artifact = await mat_svc.generate_or_get_artifact(
            str(material_id),
            start_page=start_page,
            end_page=end_page,
            course_hint=body.course_hint,
            outputs=body.outputs,
            conversation_id=str(conversation_id),
            coverage_mode=body.coverage_mode,
        )
    except MaterialCoverageRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc
    req_id = conv_svc.record_material_artifact_turn(
        str(conversation_id),
        artifact=artifact,
        outputs=list(body.outputs),
    )
    return ConversationMaterialGenerateResponse(
        artifact=artifact,
        turn_request_id=req_id,
    )


class ConversationMaterialTurnProgressResponse(ConversationMaterialProgressResponse):
    chat_turn_request_id: str | None = None


@router.get(
    "/conversations/{conversation_id}/materials/progress",
    response_model=ConversationMaterialTurnProgressResponse,
    summary="查询当前会话正在进行的个人资料处理进度",
)
async def get_conversation_material_progress(
    conversation_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> ConversationMaterialTurnProgressResponse:
    conv_svc = require_service(state)
    await run_in_threadpool(conv_svc._require_owned_conversation, str(conversation_id))
    material_service = _material_service(request, user)
    items = material_service.list_conversation_progress(str(conversation_id))
    chat_turn_request_id = next(
        (
            request_id
            for owner_id, conv_id, request_id in _material_turn_tasks(request)
            if owner_id == user.id and conv_id == str(conversation_id)
        ),
        None,
    )
    return ConversationMaterialTurnProgressResponse(
        conversation_id=str(conversation_id),
        items=items,
        chat_turn_request_id=chat_turn_request_id,
    )


@router.get(
    "/conversations/{conversation_id}/delete-impact",
    response_model=ConversationDeleteImpactView,
    summary="查询删除会话前的影响范围（交代关联资料与成果去向）",
)
def get_conversation_delete_impact(
    conversation_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> ConversationDeleteImpactView:
    require_service(state)._require_owned_conversation(str(conversation_id))
    return _material_service(request, user).get_conversation_delete_impact(
        str(conversation_id)
    )


@router.delete(
    "/conversations/{conversation_id}",
    response_model=DeleteConversationResponse,
    summary="删除学习会话（保留关联资料与已生成成果）",
)
def delete_conversation(
    conversation_id: UUID,
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> DeleteConversationResponse:
    require_service(state).delete_conversation(str(conversation_id))
    return DeleteConversationResponse(
        deleted=True, conversation_id=str(conversation_id)
    )
