"""试卷查询与下载接口。

接口层只做两件事：把外部参数规范成业务参数、把服务结果交给 FastAPI 输出。
目录故障（503）、试卷不可用（404）由 app/main.py 的异常处理器统一映射，
非法 exam_type 属于参数错误，在这里直接返回 422。
"""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.papers.models import ExamType, PaperListResponse
from app.papers.service import PaperService, get_paper_service

router = APIRouter(tags=["papers"])

VALID_EXAM_TYPES: frozenset[str] = frozenset({"midterm", "final", "other"})


def normalize_exam_type(raw: str | None) -> ExamType | None:
    """省略或空白表示“全部类型”；其余只接受 midterm / final / other，否则 422。"""

    if raw is None:
        return None

    text = raw.strip()
    if not text:
        return None

    if text not in VALID_EXAM_TYPES:
        raise HTTPException(
            status_code=422,
            detail="exam_type 只接受 midterm、final 或 other；省略该参数表示全部类型。",
        )

    return cast(ExamType, text)


@router.get("/papers", response_model=PaperListResponse, summary="查询已登记试卷")
def read_papers(
    service: Annotated[PaperService, Depends(get_paper_service)],
    course: Annotated[
        str | None,
        Query(description="课程名称或别名，例如 高数、高等数学、高等数学BⅠ"),
    ] = None,
    exam_type: Annotated[
        str | None,
        Query(description="midterm、final 或 other；省略或为空表示全部类型"),
    ] = None,
) -> PaperListResponse:
    """返回已登记试卷；没有命中时返回空列表，目录故障时返回 503。"""

    return service.list_papers(
        course=course,
        exam_type=normalize_exam_type(exam_type),
    )


@router.get(
    "/papers/{paper_id}/download",
    response_class=FileResponse,
    summary="下载原始试卷 PDF",
)
def download_paper(
    paper_id: str,
    service: Annotated[PaperService, Depends(get_paper_service)],
) -> FileResponse:
    """按已登记的 ID 返回原始 PDF，作为附件并使用原文件名。"""

    target = service.get_download(paper_id)
    return FileResponse(
        path=target.path,
        media_type="application/pdf",
        filename=target.filename,
    )
