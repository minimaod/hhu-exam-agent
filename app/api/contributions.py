"""学生自愿贡献试卷资料接口（F020）。"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from app.auth.dependencies import require_user
from app.auth.models import User
from app.config import ContributionConfigState
from app.contributions.errors import ContributionTooLargeError
from app.contributions.models import ContributionSubmitResponse
from app.contributions.storage import CONTRIBUTIONS_ROOT, save_contribution

router = APIRouter(prefix="/contributions", tags=["contributions"])


@router.post("", status_code=201, response_model=ContributionSubmitResponse)
async def submit_contribution(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    course: Annotated[str, Form(...)],
    year: Annotated[str, Form(...)],
    user: Annotated[User, Depends(require_user)],
) -> ContributionSubmitResponse:
    """接收学生自愿提交的真题 PDF、学科与年份，存入私有待审目录。"""

    config: ContributionConfigState = getattr(
        request.app.state, "contribution_config", ContributionConfigState()
    )
    data = await file.read(config.max_bytes + 1)
    if len(data) > config.max_bytes:
        mb = config.max_bytes // (1024 * 1024)
        raise ContributionTooLargeError(f"单个 PDF 文件不能超过 {mb} MB。")

    storage_root = Path(config.storage_dir) if config.storage_dir else CONTRIBUTIONS_ROOT
    return await anyio.to_thread.run_sync(
        partial(
            save_contribution,
            root=storage_root,
            owner_id=user.id,
            username=user.username,
            course=course,
            year=year,
            filename=file.filename,
            data=data,
            max_pages=config.max_pages,
            parse_timeout_seconds=config.parse_timeout_seconds,
        )
    )
