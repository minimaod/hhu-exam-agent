"""会话图片上传与读取接口。

上传只接收 multipart 文件；文件名由服务端生成。读取时校验附件属于路径中的
会话，返回图片本身，不暴露 stored_name 或磁盘绝对路径。
"""

import uuid
from pathlib import Path
from typing import Annotated
from uuid import UUID

import anyio
from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse

from app.conversations.errors import (
    AttachmentNotFoundError,
    AttachmentStorageError,
    ConversationNotFoundError,
)
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationStoreState
from app import images
from app.images import (
    MAX_IMAGE_BYTES,
    delete_attachment,
    resolve_attachment,
    save_attachment,
    validate_image,
)
from app.tutoring.models import AttachmentMeta

router = APIRouter(tags=["attachments"])


def _repository(request: Request) -> ConversationRepository:
    state: ConversationStoreState = request.app.state.conversation_store
    if state.repository is None:
        raise AttachmentStorageError("本地学习数据暂时不可用，请稍后再试。")
    return state.repository


def _root() -> Path:
    return images.ATTACHMENTS_ROOT


@router.post(
    "/conversations/{conversation_id}/attachments",
    status_code=201,
    response_model=AttachmentMeta,
    summary="上传一道题的图片",
)
async def upload_attachment(
    conversation_id: UUID,
    request: Request,
    file: Annotated[UploadFile, File(...)],
) -> AttachmentMeta:
    repository = _repository(request)
    if repository.get_conversation(str(conversation_id)) is None:
        raise ConversationNotFoundError("会话不存在。")

    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        from app.conversations.errors import AttachmentTooLargeError

        raise AttachmentTooLargeError("单张图片不能超过 6 MiB。")

    image = await anyio.to_thread.run_sync(validate_image, data)
    attachment_id = str(uuid.uuid4())
    stored_name = await anyio.to_thread.run_sync(
        lambda: save_attachment(
            _root(), str(conversation_id), attachment_id, image
        )
    )
    try:
        record = repository.create_attachment(
            conversation_id=str(conversation_id),
            attachment_id=attachment_id,
            stored_name=stored_name,
            mime=image.mime,
            width=image.width,
            height=image.height,
            size_bytes=len(image.data),
            sha256=image.sha256,
        )
    except Exception:
        delete_attachment(_root(), stored_name)
        raise

    return AttachmentMeta(
        id=record.id,
        mime=record.mime,
        width=record.width,
        height=record.height,
        size_bytes=record.size_bytes,
        created_at=record.created_at,
    )


@router.get(
    "/conversations/{conversation_id}/attachments/{attachment_id}",
    summary="读取会话中的题目图片",
)
def read_attachment(
    conversation_id: UUID,
    attachment_id: UUID,
    request: Request,
) -> FileResponse:
    repository = _repository(request)
    attachment = repository.get_attachment(
        str(conversation_id), str(attachment_id)
    )
    if attachment is None:
        raise AttachmentNotFoundError("图片不存在。")

    path = resolve_attachment(_root(), attachment.stored_name)
    return FileResponse(
        path,
        media_type=attachment.mime,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
        content_disposition_type="inline",
    )
