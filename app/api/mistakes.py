"""错题记录接口：创建、列表、详情、修改、纠正、采用新版与删除。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.conversations import (
    AttachmentInput,
    ConversationServiceState,
    get_conversation_service_state,
    require_service,
)
from app.conversations.errors import (
    ConversationStorageError,
    MistakeRecordConflictError,
    ProblemNotFoundError,
    TutoringUnavailableError,
)
from app.conversations.service import ConversationService
from app.mistakes.models import (
    MistakeRecordDetail,
    MistakeRecordListResponse,
    RecordCorrectionResultEvent,
)
from app.mistakes.service import CorrectionInput, MistakeRecordService
from app.querying.service import SSE_HEADERS
from app.sse import encode_sse_event
from app.tutoring.models import Source

router = APIRouter(tags=["mistakes"])


class CreateRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    conversation_id: UUID
    problem_id: UUID | None = None
    title: str | None = None
    feedback_text: str | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("标题不能为空。")
        if len(text) > 80:
            raise ValueError("标题最长为 80 个字符。")
        return text

    @field_validator("feedback_text")
    @classmethod
    def _feedback(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 4000:
            raise ValueError("反馈最长为 4000 个字符。")
        return value.strip() or None


class UpdateRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    source: Source | None = None
    feedback_text: str | None = None
    self_reported_understood: bool | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("标题不能为空。")
        if len(text) > 80:
            raise ValueError("标题最长为 80 个字符。")
        return text

    @field_validator("feedback_text")
    @classmethod
    def _feedback(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 4000:
            raise ValueError("反馈最长为 4000 个字符。")
        return value


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    statement_text: str
    attachments: list[AttachmentInput] | None = None
    source: Source | None = None
    student_answer_text: str | None = None
    solution_text: str | None = None

    @field_validator("statement_text")
    @classmethod
    def _statement(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("题干不能为空。")
        if len(text) > 4000:
            raise ValueError("题干最长为 4000 个字符。")
        return text

    @field_validator("student_answer_text")
    @classmethod
    def _answer(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 2000:
            raise ValueError("学生答案最长为 2000 个字符。")
        return value.strip() if value is not None else None

    @field_validator("solution_text")
    @classmethod
    def _solution(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 4000:
            raise ValueError("解题过程最长为 4000 个字符。")
        return value.strip() if value is not None else None


class AdoptProblemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    problem_id: UUID


def _service(
    state: Annotated[
        ConversationServiceState,
        Depends(get_conversation_service_state),
    ],
) -> ConversationService:
    return require_service(state)


def get_records_service(
    service: Annotated[ConversationService, Depends(_service)],
) -> MistakeRecordService:
    if service.records is None:
        raise ConversationStorageError("错题记录服务不可用。")
    return service.records


RecordsDep = Annotated[MistakeRecordService, Depends(get_records_service)]
ServiceDep = Annotated[ConversationService, Depends(_service)]


@router.post(
    "/mistake-records",
    response_model=MistakeRecordDetail,
    summary="把当前已讲解题目记录到错题本",
)
def create_record(
    payload: CreateRecordRequest,
    response: Response,
    records: RecordsDep,
    service: ServiceDep,
) -> MistakeRecordDetail:
    problem_id = str(payload.problem_id) if payload.problem_id is not None else None
    if problem_id is None:
        active = service.get_active_problem(str(payload.conversation_id))
        if active is None:
            raise ProblemNotFoundError("当前没有正在辅导的题目。")
        problem_id = active.id

    detail, created, needs_update = records.create_from_problem(
        conversation_id=str(payload.conversation_id),
        problem_id=problem_id,
        request_id=str(payload.request_id),
        title=payload.title,
        feedback_text=payload.feedback_text,
    )
    if needs_update:
        raise MistakeRecordConflictError(
            "这道题已有旧版本记录，请在错题本中更新。",
            record_id=detail.id,
        )
    response.status_code = 201 if created else 200
    return detail


@router.get(
    "/mistake-records",
    response_model=MistakeRecordListResponse,
    summary="列出错题记录",
)
def list_records(
    records: RecordsDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MistakeRecordListResponse:
    return records.list_records(limit=limit, offset=offset)


@router.get(
    "/mistake-records/{record_id}",
    response_model=MistakeRecordDetail,
    summary="读取错题记录详情",
)
def read_record(record_id: UUID, records: RecordsDep) -> MistakeRecordDetail:
    return records.get_record(str(record_id))


@router.patch(
    "/mistake-records/{record_id}",
    response_model=MistakeRecordDetail,
    summary="修改记录标题、来源、反馈或自述",
)
def update_record(
    record_id: UUID,
    payload: UpdateRecordRequest,
    records: RecordsDep,
) -> MistakeRecordDetail:
    return records.update_metadata(
        str(record_id),
        title=payload.title,
        source=payload.source,
        feedback_text=payload.feedback_text,
        self_reported_understood=payload.self_reported_understood,
    )


@router.delete(
    "/mistake-records/{record_id}",
    status_code=204,
    summary="删除错题记录（保留会话和图片）",
)
def delete_record(record_id: UUID, records: RecordsDep) -> Response:
    records.delete_record(str(record_id))
    return Response(status_code=204)


@router.post(
    "/mistake-records/{record_id}/corrections",
    summary="纠正记录题干并重新讲解",
)
async def correct_record(
    record_id: UUID,
    payload: CorrectionRequest,
    request: Request,
    records: RecordsDep,
) -> StreamingResponse:
    if not records.tutoring_available():
        raise TutoringUnavailableError("辅导模型尚未配置。")

    correction = CorrectionInput(
        statement_text=payload.statement_text,
        attachments=(
            None
            if payload.attachments is None
            else [(str(item.id), item.role) for item in payload.attachments]
        ),
        source=payload.source,
        student_answer_text=payload.student_answer_text,
        solution_text=payload.solution_text,
    )
    records.validate_correction(str(record_id), correction)
    request_input = records.correction_input(correction)

    replay = records.find_correction_replay(
        str(record_id), str(payload.request_id), request_input
    )
    if replay is not None:
        request_id = str(payload.request_id)

        async def replay_stream():
            yield encode_sse_event(
                RecordCorrectionResultEvent(
                    record_id=str(record_id),
                    request_id=request_id,
                    result=replay,
                )
            )

        return StreamingResponse(
            replay_stream(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    acquired = await records.begin_correction(str(record_id))
    if not acquired:
        raise MistakeRecordConflictError("该记录正在纠正，请稍后再试。")

    async def encode_corrections():
        async for event in records.corrections(
            str(record_id),
            str(payload.request_id),
            correction,
            request,
        ):
            yield encode_sse_event(event)

    return StreamingResponse(
        encode_corrections(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post(
    "/mistake-records/{record_id}/adopt-problem",
    response_model=MistakeRecordDetail,
    summary="采用同一会话更新版本的已讲解内容",
)
def adopt_problem(
    record_id: UUID,
    payload: AdoptProblemRequest,
    records: RecordsDep,
) -> MistakeRecordDetail:
    detail, _created = records.adopt_problem(
        str(record_id),
        problem_id=str(payload.problem_id),
        request_id=str(payload.request_id),
    )
    return detail
