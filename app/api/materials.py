"""个人资料 PDF 上传、原件预览、可读性检查、复习清单与删除接口。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.query import get_query_config
from app.auth.dependencies import require_user
from app.auth.models import User
from app.config import MaterialConfigState
from app.conversations.service import ConversationStoreState
from app.materials.errors import MaterialCoverageRequiredError, MaterialInvalidError, MaterialStorageError, MaterialTooLargeError
from app.materials.exam_solver import ExamSolver
from app.materials.exampass_llm import DeepSeekExamPassGenerator
from app.materials.llm import DeepSeekGuideGenerator
from app.materials.ocr import MaterialOcr
from app.materials.models import (
    ArtifactDetailView,
    CreateArtifactRequest,
    CreateGuideRequest,
    DeleteImpactView,
    DeleteMaterialResponse,
    GraphNoteUpdateRequest,
    GraphNoteView,
    ImportSampleArtifactRequest,
    MaterialDetailResponse,
    MaterialLibraryResponse,
    MaterialListResponse,
    MaterialOcrRequest,
    MaterialOcrResponse,
    MaterialPageView,
    MaterialSummary,
    QuizAttemptRequest,
    ReadabilityReport,
    ReadabilityRequest,
    ReviewGuideView,
    StudyFromArtifactKcRequest,
    StudyFromArtifactKcResponse,
    StudyFromArtifactQuestionRequest,
    StudyFromArtifactQuestionResponse,
    StudyFromItemRequest,
    StudyFromItemResponse,
)
from app.tutoring.exercise_llm import DeepSeekExerciseAgent
from app.materials.repository import MaterialRepository
from app.materials.service import MaterialService

router = APIRouter(prefix="/materials", tags=["materials"])


def _material_service(request: Request, user: User) -> MaterialService:
    store: ConversationStoreState = request.app.state.conversation_store
    if store.db_path is None or store.error is not None:
        raise MaterialStorageError("本地学习数据暂时不可用，请稍后再试。")
    config: MaterialConfigState = getattr(
        request.app.state, "material_config", MaterialConfigState()
    )
    usage_gate = getattr(request.app.state, "usage_gate", None)
    usage_config = getattr(request.app.state, "usage_config", None)
    usage_error = getattr(usage_config, "error", None)
    query_config = get_query_config(request)
    http_client = getattr(request.app.state, "model_http_client", None)

    def _generator_factory() -> DeepSeekGuideGenerator:
        if query_config.settings is None or query_config.error is not None:
            raise MaterialStorageError(query_config.error or "模型配置不可用。")
        return DeepSeekGuideGenerator(
            query_config.settings,
            http_async_client=http_client,
        )

    def _exampass_factory() -> DeepSeekExamPassGenerator:
        if query_config.settings is None or query_config.error is not None:
            raise MaterialStorageError(query_config.error or "模型配置不可用。")
        return DeepSeekExamPassGenerator(
            query_config.settings,
            http_async_client=http_client,
        )

    def _evaluator_factory() -> DeepSeekExerciseAgent:
        if query_config.settings is None or query_config.error is not None:
            raise MaterialStorageError(query_config.error or "模型配置不可用。")
        return DeepSeekExerciseAgent(
            query_config.settings,
            http_async_client=http_client,
        )

    def _ocr_factory() -> MaterialOcr:
        if query_config.settings is None or query_config.error is not None or http_client is None:
            raise MaterialStorageError(query_config.error or "扫描页识别服务暂时不可用。")
        return MaterialOcr(query_config.settings, http_client=http_client)

    def _exam_solver_factory() -> ExamSolver:
        if query_config.settings is None or query_config.error is not None or http_client is None:
            raise MaterialStorageError(query_config.error or "试卷解答服务暂时不可用。")
        return ExamSolver(query_config.settings, http_client=http_client)

    repo = MaterialRepository(store.db_path, owner_id=user.id)
    return MaterialService(
        repo,
        config=config,
        usage_gate=usage_gate,
        usage_error=usage_error,
        guide_generator_factory=_generator_factory,
        exampass_generator_factory=_exampass_factory,
        evaluator_factory=_evaluator_factory,
        ocr_factory=_ocr_factory,
        exam_solver_factory=_exam_solver_factory,
    )


@router.post("/pdfs", status_code=201, response_model=MaterialSummary)
async def upload_material_pdf(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    user: Annotated[User, Depends(require_user)],
) -> MaterialSummary:
    service = _material_service(request, user)
    config: MaterialConfigState = getattr(
        request.app.state, "material_config", MaterialConfigState()
    )
    data = await file.read(config.max_bytes + 1)
    if len(data) > config.max_bytes:
        mb = config.max_bytes // (1024 * 1024)
        raise MaterialTooLargeError(f"单个 PDF 文件不能超过 {mb} MB。")
    return await service.upload_pdf(filename=file.filename, data=data)


@router.get("/pdfs", response_model=MaterialListResponse)
def list_materials(
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> MaterialListResponse:
    return _material_service(request, user).list_materials()


@router.get("/library", response_model=MaterialLibraryResponse)
def list_material_library(
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> MaterialLibraryResponse:
    return MaterialLibraryResponse(items=_material_service(request, user).list_library())


@router.get("/pdfs/{material_id}", response_model=MaterialDetailResponse)
def get_material_detail(
    material_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> MaterialDetailResponse:
    return _material_service(request, user).get_material_detail(str(material_id))


@router.get("/pdfs/{material_id}/file")
def preview_material_pdf(
    material_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> FileResponse:
    path, display_name = _material_service(request, user).get_pdf_file(str(material_id))
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=display_name,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


@router.post("/pdfs/{material_id}/readability", response_model=ReadabilityReport)
async def check_material_readability(
    material_id: UUID,
    body: ReadabilityRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> ReadabilityReport:
    return await _material_service(request, user).check_readability(
        str(material_id),
        start_page=body.start_page,
        end_page=body.end_page,
    )


@router.post("/pdfs/{material_id}/ocr", response_model=MaterialOcrResponse)
async def recognize_material_pages(
    material_id: UUID,
    body: MaterialOcrRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    conversation_id: UUID | None = None,
) -> MaterialOcrResponse:
    conv_id = str(conversation_id) if conversation_id is not None else request.headers.get("X-Conversation-Id")
    return await _material_service(request, user).recognize_pages(
        str(material_id),
        body.pages,
        conversation_id=conv_id,
    )


@router.post("/pdfs/{material_id}/guides", response_model=ReviewGuideView)
async def create_or_get_guide(
    material_id: UUID,
    body: CreateGuideRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> ReviewGuideView:
    return await _material_service(request, user).generate_or_get_guide(
        str(material_id),
        start_page=body.start_page,
        end_page=body.end_page,
        course_hint=body.course_hint,
    )


@router.get("/guides/{guide_id}", response_model=ReviewGuideView)
def get_guide(
    guide_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> ReviewGuideView:
    return _material_service(request, user).get_guide(str(guide_id))


@router.get("/pdfs/{material_id}/pages/{page_no}", response_model=MaterialPageView)
async def get_material_page(
    material_id: UUID,
    page_no: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> MaterialPageView:
    return await _material_service(request, user).get_page(str(material_id), page_no)


@router.post(
    "/guides/{guide_id}/items/{item_id}/study",
    response_model=StudyFromItemResponse,
)
def study_from_guide_item(
    guide_id: UUID,
    item_id: UUID,
    body: StudyFromItemRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> StudyFromItemResponse:
    return _material_service(request, user).prepare_study_from_item(
        str(guide_id), str(item_id), mode=body.mode
    )


@router.get("/pdfs/{material_id}/delete-impact", response_model=DeleteImpactView)
def get_delete_impact(
    material_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> DeleteImpactView:
    return _material_service(request, user).get_delete_impact(str(material_id))


@router.delete("/pdfs/{material_id}", response_model=DeleteMaterialResponse)
def delete_material(
    material_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> DeleteMaterialResponse:
    _material_service(request, user).delete_material(str(material_id))
    return DeleteMaterialResponse(deleted=True, material_id=str(material_id))


@router.get("/pdfs/{material_id}/pages/{page_no}/image")
async def get_material_page_image(
    material_id: UUID,
    page_no: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> FileResponse:
    img_path = await _material_service(request, user).get_page_image(
        str(material_id), page_no
    )
    return FileResponse(
        img_path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _resolve_sample_dir(sample_name: str) -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "tmp" / sample_name / ".epa_work",
        Path(__file__).resolve().parents[3] / "河海大学考试复习" / "tmp" / sample_name / ".epa_work",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    raise MaterialInvalidError("未找到本地 ExamPass 样例目录。")


@router.post(
    "/pdfs/{material_id}/artifacts/import-sample",
    response_model=ArtifactDetailView,
)
def import_sample_artifact(
    material_id: UUID,
    body: ImportSampleArtifactRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> ArtifactDetailView:
    return _material_service(request, user).import_sample_artifact(
        str(material_id), resolve_sample=lambda: _resolve_sample_dir(body.sample_name)
    )


@router.post(
    "/pdfs/{material_id}/artifacts",
    response_model=ArtifactDetailView,
)
async def generate_or_get_artifact(
    material_id: UUID,
    body: CreateArtifactRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> ArtifactDetailView:
    try:
        return await _material_service(request, user).generate_or_get_artifact(
            str(material_id),
            start_page=body.start_page,
            end_page=body.end_page,
            course_hint=body.course_hint,
            outputs=body.outputs,
            conversation_id=body.conversation_id,
            coverage_mode=body.coverage_mode,
        )
    except MaterialCoverageRequiredError as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc


@router.get("/artifacts/{artifact_id}", response_model=ArtifactDetailView)
def get_artifact(
    artifact_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> ArtifactDetailView:
    return _material_service(request, user).get_artifact(str(artifact_id))


@router.post(
    "/artifacts/{artifact_id}/questions/{question_id}/attempt",
    response_model=ArtifactDetailView,
)
def submit_quiz_attempt(
    artifact_id: UUID,
    question_id: UUID,
    body: QuizAttemptRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> ArtifactDetailView:
    return _material_service(request, user).submit_quiz_attempt(
        artifact_id=str(artifact_id),
        question_id=str(question_id),
        user_answer=body.user_answer,
    )


@router.post(
    "/artifacts/{artifact_id}/questions/{question_id}/reveal",
    response_model=ArtifactDetailView,
)
def reveal_quiz_solution(
    artifact_id: UUID,
    question_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> ArtifactDetailView:
    return _material_service(request, user).reveal_quiz_solution(
        artifact_id=str(artifact_id),
        question_id=str(question_id),
    )


@router.put(
    "/artifacts/{artifact_id}/graph-notes/{node_id}",
    response_model=GraphNoteView,
)
def update_graph_note(
    artifact_id: UUID,
    node_id: str,
    body: GraphNoteUpdateRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> GraphNoteView:
    return _material_service(request, user).update_graph_note(
        artifact_id=str(artifact_id),
        node_id=node_id,
        note_text=body.note_text,
    )


@router.put("/artifacts/{artifact_id}/kc-notes/{kc_id}", response_model=GraphNoteView)
def update_knowledge_point_note(
    artifact_id: UUID,
    kc_id: str,
    body: GraphNoteUpdateRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> GraphNoteView:
    return _material_service(request, user).update_graph_note(
        artifact_id=str(artifact_id), node_id=kc_id, note_text=body.note_text, is_knowledge_point=True,
    )


@router.post(
    "/artifacts/{artifact_id}/questions/{question_id}/study",
    response_model=StudyFromArtifactQuestionResponse,
)
def study_from_artifact_question(
    artifact_id: UUID,
    question_id: UUID,
    body: StudyFromArtifactQuestionRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> StudyFromArtifactQuestionResponse:
    return _material_service(request, user).prepare_study_from_artifact_question(
        artifact_id=str(artifact_id),
        question_id=str(question_id),
        mode=body.mode,
        conversation_id=body.conversation_id,
    )


@router.post(
    "/artifacts/{artifact_id}/questions/{question_id}/evaluate",
    response_model=ArtifactDetailView,
)
async def evaluate_quiz_question(
    artifact_id: UUID,
    question_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> ArtifactDetailView:
    return await _material_service(request, user).evaluate_quiz_question(
        artifact_id=str(artifact_id),
        question_id=str(question_id),
    )


@router.post(
    "/artifacts/{artifact_id}/kcs/{kc_id}/study",
    response_model=StudyFromArtifactKcResponse,
)
def study_from_artifact_kc(
    artifact_id: UUID,
    kc_id: str,
    body: StudyFromArtifactKcRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> StudyFromArtifactKcResponse:
    return _material_service(request, user).prepare_study_from_artifact_kc(
        artifact_id=str(artifact_id),
        kc_id=kc_id,
        mode=body.mode,
        conversation_id=body.conversation_id,
    )
