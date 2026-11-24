"""个人资料复习业务服务。"""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import anyio

from app.config import MaterialConfigState
from app.images import ATTACHMENTS_ROOT
from app.materials.citation import derive_verified_excerpt, verify_citations
from app.materials.coverage import bind_inputs
from app.materials.errors import (
    GuideInsufficientError,
    GuideNotFoundError,
    MaterialConflictError,
    MaterialCoverageRequiredError,
    MaterialInvalidError,
    MaterialNotFoundError,
    MaterialPageLimitError,
    MaterialStorageError,
    MaterialTooLargeError,
)
from app.materials.exam_solver import (
    EXAM_PAGES_PER_BATCH,
    EXAM_PAGES_PER_TURN,
    ExamResult,
    ExamSolver,
    _SCOPE_QUESTION,
)
from app.materials.exampass_adapter import (
    EXAMPASS_PHYSICS_SAMPLE_SHA256,
    grade_objective_question,
    load_sample_exampass_bundle,
    skeleton_to_graph_tree,
)
from app.materials.exampass_llm import (
    EXAMPASS_RULE_VERSION,
    DeepSeekExamPassGenerator,
)
from app.materials.llm import GUIDE_RULE_VERSION, DeepSeekGuideGenerator
from app.materials.models import (
    ArtifactDetailView,
    ArtifactInputMode,
    ConversationDeleteImpactView,
    ConversationMaterialItem,
    ConversationMaterialUploadResponse,
    DeleteImpactView,
    GraphNoteView,
    MaterialDetailResponse,
    MaterialLibraryItem,
    MaterialListResponse,
    MaterialPageView,
    MaterialProgressItem,
    MaterialSummary,
    MaterialOcrFailure,
    MaterialOcrResponse,
    MAX_OCR_PAGES_PER_REQUEST,
    OutputKind,
    ReadabilityOverview,
    ReadabilityReport,
    ReviewGuideView,
    StudyFromArtifactKcResponse,
    StudyFromArtifactQuestionResponse,
    StudyFromItemResponse,
)
from app.tutoring.exercise_llm import DeepSeekExerciseAgent
from app.materials.slide_renderer import (
    render_ocr_page_image,
    render_pdf_page_image,
    slides_dir_for_material,
)
from app.materials.pdf_parser import (
    ParsedPage,
    build_readability_report,
    run_isolated_pdf_parse,
    validate_pdf_header,
)
from app.materials.ocr import (
    ExcludedOcrPage, FailedOcrPage, MaterialOcr, OcrPage, describe_page, rule_version_for_pages,
)
from app.materials.repository import MaterialRepository, _normalize_outputs
from app.materials.storage import (
    MATERIALS_ROOT,
    delete_material_pdf,
    resolve_material_pdf,
    sanitize_display_name,
    save_material_pdf,
    try_delete_file,
)
from app.querying.errors import QueryError
from app.quota.errors import QuotaError
from app.quota.gate import UsageGate
from app.quota.transport import bind_actor


class MaterialService:
    # 进程内正在执行模型生成的 guide_id 集合，用于删除冲突保护
    _active_generations: set[str] = set()
    _active_ocr: set[tuple[str, str]] = set()
    _ocr_render_limit = anyio.CapacityLimiter(2)
    _progress_lock = threading.Lock()
    _progress_tasks: dict[str, tuple[str, MaterialProgressItem]] = {}

    @classmethod
    def _set_progress(cls, owner_id: str, item: MaterialProgressItem) -> None:
        with cls._progress_lock:
            cls._progress_tasks[item.task_id] = (owner_id, item)

    @classmethod
    def _update_progress(
        cls,
        task_id: str,
        *,
        stage: str | None = None,
        stage_label: str | None = None,
        ocr_done: int | None = None,
        ocr_total: int | None = None,
        ocr_failed_pages: list[int] | None = None,
    ) -> None:
        with cls._progress_lock:
            entry = cls._progress_tasks.get(task_id)
            if entry is None:
                return
            owner_id, item = entry
            cls._progress_tasks[task_id] = (
                owner_id,
                item.model_copy(
                    update={
                        **({"stage": stage} if stage is not None else {}),
                        **({"stage_label": stage_label} if stage_label is not None else {}),
                        **({"ocr_done": ocr_done} if ocr_done is not None else {}),
                        **({"ocr_total": ocr_total} if ocr_total is not None else {}),
                        **({"ocr_failed_pages": list(ocr_failed_pages)} if ocr_failed_pages is not None else {}),
                        "updated_at": datetime.now(timezone.utc),
                    }
                ),
            )

    @classmethod
    def _clear_progress(cls, task_id: str) -> None:
        with cls._progress_lock:
            cls._progress_tasks.pop(task_id, None)

    def list_conversation_progress(self, conversation_id: str) -> list[MaterialProgressItem]:
        with self._progress_lock:
            return [
                item
                for owner_id, item in self._progress_tasks.values()
                if owner_id == self._repo.owner_id and item.conversation_id == conversation_id
            ]

    def __init__(
        self,
        repository: MaterialRepository,
        *,
        config: MaterialConfigState,
        materials_root: Path | None = None,
        attachments_root: Path | None = None,
        usage_gate: UsageGate | None = None,
        usage_error: str | None = None,
        guide_generator_factory: Callable[[], DeepSeekGuideGenerator] | None = None,
        exampass_generator_factory: Callable[[], DeepSeekExamPassGenerator] | None = None,
        evaluator_factory: Callable[[], DeepSeekExerciseAgent] | None = None,
        ocr_factory: Callable[[], MaterialOcr] | None = None,
        exam_solver_factory: Callable[[], ExamSolver] | None = None,
    ) -> None:
        self._repo = repository
        self._config = config
        self._materials_root = materials_root if materials_root is not None else MATERIALS_ROOT
        self._attachments_root = attachments_root if attachments_root is not None else ATTACHMENTS_ROOT
        self._usage_gate = usage_gate
        self._usage_error = usage_error
        self._guide_generator_factory = guide_generator_factory
        self._exampass_generator_factory = exampass_generator_factory
        self._evaluator_factory = evaluator_factory
        self._ocr_factory = ocr_factory
        self._exam_solver_factory = exam_solver_factory

    def list_materials(self) -> MaterialListResponse:
        return MaterialListResponse(items=self._repo.list_materials())

    def get_material_detail(self, material_id: str) -> MaterialDetailResponse:
        mat = self._repo.get_material(material_id)
        guides = self._repo.list_guides_for_material(material_id)
        artifacts = self._repo.list_artifacts_for_material(material_id)
        return MaterialDetailResponse(
            material=mat.to_summary(),
            guides=guides,
            artifacts=artifacts,
        )

    async def upload_pdf(
        self, *, filename: str | None, data: bytes
    ) -> MaterialSummary:
        if not data:
            raise MaterialInvalidError("上传的 PDF 文件为空。")
        if len(data) > self._config.max_bytes:
            mb = self._config.max_bytes // (1024 * 1024)
            raise MaterialTooLargeError(f"单个 PDF 文件不能超过 {mb} MB。")

        validate_pdf_header(data)
        material_id = str(uuid.uuid4())
        display_name = sanitize_display_name(filename)
        stored_name, sha256 = await anyio.to_thread.run_sync(
            lambda: save_material_pdf(
                self._materials_root,
                self._repo.owner_id,
                material_id,
                data,
            )
        )
        pdf_path = self._materials_root / stored_name
        try:
            # 上传阶段仅校验文件加密与总页数，不提取整卷文本，避免大卷/告警 PDF 阻塞上传。
            parsed = await anyio.to_thread.run_sync(
                lambda: run_isolated_pdf_parse(
                    pdf_path,
                    max_pages=self._config.max_pages,
                    timeout_seconds=self._config.parse_timeout_seconds,
                )
            )
            record = self._repo.create_material(
                material_id=material_id,
                display_name=display_name,
                stored_name=stored_name,
                file_sha256=sha256,
                size_bytes=len(data),
                page_count=parsed.page_count,
                max_per_user=self._config.max_per_user,
            )
            if parsed.pages:
                self._repo.save_parsed_pages(material_id, parsed.pages)
        except Exception:
            delete_material_pdf(self._materials_root, stored_name)
            raise
        return record.to_summary()

    def _validate_page_range(
        self, *, page_count: int, start_page: int, end_page: int
    ) -> None:
        if start_page < 1 or end_page < start_page or end_page > page_count:
            raise MaterialPageLimitError(
                f"页码范围无效：请输入 1 至 {page_count} 之间的有效起止页。"
            )
        if end_page - start_page + 1 > self._config.max_range_pages:
            raise MaterialPageLimitError(
                f"单次最多选择 {self._config.max_range_pages} 页，请缩小页码范围。"
            )

    async def _ensure_pages_parsed(
        self, material_id: str, stored_name: str, start_page: int, end_page: int
    ) -> list[ParsedPage]:
        cached = self._repo.get_cached_pages(material_id, start_page, end_page, fresh_pdf_text=True)
        expected = list(range(start_page, end_page + 1))
        if len(cached) != len(expected):
            pdf_path = resolve_material_pdf(self._materials_root, stored_name)
            parsed = await anyio.to_thread.run_sync(
                lambda: run_isolated_pdf_parse(
                    pdf_path,
                    max_pages=self._config.max_pages,
                    start_page=start_page,
                    end_page=end_page,
                    timeout_seconds=self._config.parse_timeout_seconds,
                )
            )
            self._repo.save_parsed_pages(material_id, [p for p in parsed.pages if p.page_no not in cached])
            cached = self._repo.get_cached_pages(material_id, start_page, end_page)
        if any(describe_page(page).status in ("pending_ocr", "ocr_failed") for page in cached.values()):
            if self._repo.reuse_visual_pages(material_id, start_page, end_page):
                cached = self._repo.get_cached_pages(material_id, start_page, end_page)
        return [cached[page_no] for page_no in expected]

    async def recognize_pages(
        self,
        material_id: str,
        page_numbers: list[int],
        *,
        conversation_id: str | None = None,
    ) -> MaterialOcrResponse:
        mat = self._repo.get_material(material_id)
        if not page_numbers or len(page_numbers) > MAX_OCR_PAGES_PER_REQUEST:
            raise MaterialPageLimitError(f"每次可识别 1 至 {MAX_OCR_PAGES_PER_REQUEST} 页。")
        selected = sorted(set(page_numbers))
        if selected[0] < 1 or selected[-1] > mat.page_count:
            raise MaterialPageLimitError(f"页码超出范围（1–{mat.page_count}）。")

        key = (self._repo.owner_id, mat.id)
        if key in self._active_ocr:
            raise MaterialConflictError("该资料正在识别，请稍后查看或重试。")
        self._active_ocr.add(key)
        task_id = f"ocr:{uuid.uuid4()}"
        if conversation_id:
            self._set_progress(
                self._repo.owner_id,
                MaterialProgressItem(
                    task_id=task_id,
                    conversation_id=conversation_id,
                    material_id=mat.id,
                    material_name=mat.display_name,
                    kind="ocr",
                    start_page=selected[0],
                    end_page=selected[-1],
                    outputs=[],
                    stage="checking_text",
                    stage_label="检查文字",
                    ocr_done=0,
                    ocr_total=len(selected),
                    ocr_failed_pages=[],
                    updated_at=datetime.now(timezone.utc),
                ),
            )
        try:
            pages = await self._ensure_pages_parsed(mat.id, mat.stored_name, selected[0], selected[-1])
            requested = [page for page in pages if page.page_no in selected]
            reused = [
                page.page_no for page in requested if isinstance(page, (OcrPage, ExcludedOcrPage))
            ]
            missing = [
                page.page_no for page in requested
                if describe_page(page).status in ("pending_ocr", "ocr_failed")
            ]
            skipped = [page_no for page_no in selected if page_no not in reused and page_no not in missing]
            recognized: list[int] = []
            excluded: list[int] = []
            original = {page.page_no: page for page in requested}
            failures: list[MaterialOcrFailure] = []
            if conversation_id:
                self._update_progress(
                    task_id,
                    stage="ocr",
                    stage_label="识别扫描页",
                    ocr_done=0,
                    ocr_total=len(missing) if missing else len(selected),
                )
            if missing:
                if self._usage_error or self._usage_gate is None or self._ocr_factory is None:
                    raise MaterialStorageError(self._usage_error or "扫描页识别服务暂时不可用。")
                ocr = self._ocr_factory()
                pdf_path = resolve_material_pdf(self._materials_root, mat.stored_name)
                with bind_actor(self._repo.owner_id, request_id=f"ocr:{uuid.uuid4()}"):
                    for idx, page_no in enumerate(missing, start=1):
                        try:
                            self._usage_gate.ensure_available(self._repo.owner_id)
                            image = await anyio.to_thread.run_sync(
                                lambda: render_ocr_page_image(
                                    pdf_path, page_no, timeout_seconds=self._config.parse_timeout_seconds
                                ),
                                limiter=self._ocr_render_limit,
                            )
                            page = await ocr.transcribe(image, page_no)
                        except QuotaError:
                            raise
                        except (QueryError, MaterialInvalidError) as exc:
                            self._repo.save_parsed_pages(mat.id, [FailedOcrPage(**asdict(original[page_no]))])
                            failures.append(MaterialOcrFailure(page_no=page_no, message=str(exc)))
                            if conversation_id:
                                self._update_progress(
                                    task_id,
                                    ocr_done=idx,
                                    ocr_failed_pages=[f.page_no for f in failures],
                                )
                            continue
                        self._repo.save_parsed_pages(mat.id, [page])
                        (excluded if isinstance(page, ExcludedOcrPage) else recognized).append(page_no)
                        if conversation_id:
                            self._update_progress(
                                task_id,
                                ocr_done=idx,
                                ocr_failed_pages=[f.page_no for f in failures],
                            )
            return MaterialOcrResponse(
                recognized_pages=recognized,
                excluded_pages=excluded,
                reused_pages=reused,
                skipped_pages=skipped,
                failures=failures,
                readability=self._repo.get_readability_overview(mat.id),
            )
        finally:
            self._clear_progress(task_id)
            self._active_ocr.discard(key)

    async def solve_uploaded_exam(
        self,
        *,
        conversation_id: str,
        material_id: str,
        query: str,
        focus: str | None,
        answer_only: bool,
        question_refs: list[str],
        start_page: int | None = None,
        end_page: int | None = None,
    ) -> ExamResult:
        attached = self._repo.list_conversation_materials(conversation_id)
        if not any(item.material.id == material_id for item in attached):
            raise MaterialNotFoundError("指定试卷不在当前会话中。")
        mat = self._repo.get_material(material_id)
        start = start_page if start_page is not None else 1
        requested_end = end_page if end_page is not None else mat.page_count
        if not 1 <= start <= requested_end <= mat.page_count:
            raise MaterialPageLimitError(f"请指定这份试卷的物理第 1 至 {mat.page_count} 页。")
        end = min(requested_end, start + EXAM_PAGES_PER_TURN - 1)
        self._validate_page_range(page_count=mat.page_count, start_page=start, end_page=end)
        if self._usage_error or self._usage_gate is None or self._exam_solver_factory is None:
            raise MaterialStorageError(self._usage_error or "试卷解答服务暂时不可用。")
        solver = self._exam_solver_factory()
        cached_text = self._repo.get_cached_pages(mat.id, start, end)
        pdf_path = resolve_material_pdf(self._materials_root, mat.stored_name)
        result = ExamResult()
        page_numbers = list(range(start, end + 1))
        batch_size = EXAM_PAGES_PER_TURN if question_refs else EXAM_PAGES_PER_BATCH
        for offset in range(0, len(page_numbers), batch_size):
            batch_pages = page_numbers[offset:offset + batch_size]
            try:
                self._usage_gate.ensure_available(self._repo.owner_id)
                inputs: list[tuple[int, str, bytes | None]] = []
                for page_no in batch_pages:
                    # 已缓存的文字仅作辅助，解题不等待新的文字提取。
                    image = await anyio.to_thread.run_sync(
                        lambda page_no=page_no: render_ocr_page_image(
                            pdf_path, page_no, timeout_seconds=self._config.parse_timeout_seconds,
                        ),
                        limiter=self._ocr_render_limit,
                    )
                    cached = cached_text.get(page_no)
                    inputs.append((page_no, cached.extracted_text if cached else "", image))
                with bind_actor(self._repo.owner_id, conversation_id=conversation_id, request_id=f"exam:{uuid.uuid4()}"):
                    batch = await solver.solve(
                        query=query, focus=focus, answer_only=answer_only, question_refs=question_refs, pages=inputs,
                    )
            except (QueryError, MaterialInvalidError) as exc:
                if not result.checked_pages:
                    raise
                result.unresolved.append(f"第 {batch_pages[0]} 页起尚未查看：{exc}")
                result.next_page = batch_pages[0]
                return result
            result.checked_pages.extend(batch_pages)
            result.answers.extend(batch.answers)
            result.unresolved.extend(batch.unresolved)
            if batch.no_match:
                result.no_match = result.no_match or batch.no_match
        if result.answers or result.unresolved:
            result.no_match = None
        if end < requested_end and not (question_refs and len(result.answers) == len(question_refs)):
            result.next_page = end + 1
        return result

    async def check_readability(
        self, material_id: str, *, start_page: int, end_page: int
    ) -> ReadabilityReport:
        mat = self._repo.get_material(material_id)
        self._validate_page_range(
            page_count=mat.page_count, start_page=start_page, end_page=end_page
        )
        pages = await self._ensure_pages_parsed(
            mat.id, mat.stored_name, start_page, end_page
        )
        states = [describe_page(page) for page in pages]
        excluded = {state.page_no for state in states if state.status == "excluded"}
        report = build_readability_report(
            mat.id, start_page, end_page, [page for page in pages if page.page_no not in excluded]
        )
        report.pages = states
        counts = [
            (sum(state.status == status for state in states), label)
            for status, label in (
                ("usable", "可用"), ("pending_ocr", "待识别"), ("ocr_failed", "识别失败"),
                ("excluded", "无需复习"),
            )
        ]
        report.reason = "所选范围已检查。" + "，".join(f"{count} 页{label}" for count, label in counts if count) + "。"
        if any(state.needs_review for state in states):
            report.reason += "公式及图示可对照原页。"
        return report

    async def get_page(self, material_id: str, page_no: int) -> MaterialPageView:
        mat = self._repo.get_material(material_id)
        if page_no < 1 or page_no > mat.page_count:
            raise MaterialPageLimitError(f"页码超出范围（1–{mat.page_count}）。")
        view = self._repo.get_page_view(material_id, page_no)
        if view is not None:
            return view
        await self._ensure_pages_parsed(mat.id, mat.stored_name, page_no, page_no)
        view = self._repo.get_page_view(material_id, page_no)
        if view is None:
            raise MaterialNotFoundError("物理页内容不存在。")
        return view

    def get_pdf_file(self, material_id: str) -> tuple[Path, str]:
        mat = self._repo.get_material(material_id)
        path = resolve_material_pdf(self._materials_root, mat.stored_name)
        return path, mat.display_name

    def get_guide(self, guide_id: str) -> ReviewGuideView:
        return self._repo.get_guide(guide_id)

    async def generate_or_get_guide(
        self,
        material_id: str,
        *,
        start_page: int,
        end_page: int,
        course_hint: str | None = None,
    ) -> ReviewGuideView:
        mat = self._repo.get_material(material_id)
        self._validate_page_range(
            page_count=mat.page_count, start_page=start_page, end_page=end_page
        )
        pages = await self._ensure_pages_parsed(
            mat.id, mat.stored_name, start_page, end_page
        )
        readability = build_readability_report(mat.id, start_page, end_page, pages)
        if not readability.sufficient:
            raise GuideInsufficientError(
                readability.recommendation or readability.reason
            )

        acquire = self._repo.acquire_guide_generation(
            material_id=mat.id,
            file_sha256=mat.file_sha256,
            start_page=start_page,
            end_page=end_page,
            rule_version=rule_version_for_pages(GUIDE_RULE_VERSION, pages),
            course_hint=course_hint,
            readability=readability,
        )
        if acquire.reused or acquire.generation_token is None:
            return self._repo.get_guide(acquire.guide_id, reused=True)

        token = acquire.generation_token
        self._active_generations.add(acquire.guide_id)
        try:
            if self._usage_error:
                raise MaterialStorageError(self._usage_error)
            if self._usage_gate is not None:
                self._usage_gate.ensure_available(self._repo.owner_id)
            if self._guide_generator_factory is None:
                raise MaterialStorageError("模型服务尚未配置，暂时无法生成复习清单。")

            generator = self._guide_generator_factory()
            with bind_actor(self._repo.owner_id, request_id=f"guide:{acquire.guide_id}"):
                payload = await generator.generate(
                    display_name=mat.display_name,
                    start_page=start_page,
                    end_page=end_page,
                    pages=pages,
                    course_hint=course_hint,
                )

            page_texts = {p.page_no: p.extracted_text for p in pages}
            items_to_save: list[dict[str, object]] = []
            for item in payload.items:
                verified = verify_citations(
                    [c.model_dump() for c in item.citations],
                    page_texts=page_texts,
                    start_page=start_page,
                    end_page=end_page,
                )
                verified_excerpt = derive_verified_excerpt(verified, page_texts)
                items_to_save.append(
                    {
                        "category": item.category,
                        "title": item.title,
                        "source_excerpt": verified_excerpt,
                        "ai_explanation": item.ai_explanation,
                        "practice_prompt": item.practice_prompt,
                        "verified_citations": [c.model_dump() for c in verified],
                    }
                )

            saved = self._repo.complete_guide_generation(
                guide_id=acquire.guide_id,
                material_id=mat.id,
                generation_token=token,
                conversation_id=acquire.conversation_id,
                items=items_to_save,
            )
            if not saved:
                raise MaterialNotFoundError("个人资料已删除，本次生成结果已丢弃。")

            return self._repo.get_guide(acquire.guide_id, reused=False)
        except QueryError as exc:
            self._repo.fail_guide_generation(
                guide_id=acquire.guide_id,
                generation_token=token,
                error_code=exc.code,
                error_message=exc.message,
            )
            return self._repo.get_guide(acquire.guide_id, reused=False)
        except asyncio.CancelledError:
            self._repo.fail_guide_generation(
                guide_id=acquire.guide_id,
                generation_token=token,
                error_code="CANCELLED",
                error_message="清单生成已取消，请重试。",
            )
            raise
        except Exception as exc:
            self._repo.fail_guide_generation(
                guide_id=acquire.guide_id,
                generation_token=token,
                error_code="GUIDE_GENERATION_FAILED",
                error_message=str(exc) or "复习清单生成失败，请重试。",
            )
            raise
        finally:
            self._active_generations.discard(acquire.guide_id)

    def prepare_study_from_item(
        self, guide_id: str, item_id: str, *, mode: str
    ) -> StudyFromItemResponse:
        guide, item, _display_name = self._repo.get_guide_item_context(guide_id, item_id)
        if guide.status != "ready" or not guide.conversation_id:
            raise GuideNotFoundError("复习清单尚未就绪，请稍后再试。")

        # 绝不将 PDF 原文、模型生成标题或解释拼入用户消息（避免 PDF 内容被当作用户授权）
        if mode == "explain":
            prompt = "请讲解我选中的资料知识点"
        else:
            prompt = "请围绕我选中的资料知识点出一道自测练习题"

        return StudyFromItemResponse(
            conversation_id=guide.conversation_id,
            mode=mode,  # type: ignore[arg-type]
            guide_id=guide.id,
            item_id=item.id,
            prompt=prompt,
        )

    def get_delete_impact(self, material_id: str) -> DeleteImpactView:
        return self._repo.get_delete_impact(material_id)

    def delete_material(self, material_id: str) -> None:
        if (self._repo.owner_id, material_id) in self._active_ocr:
            raise MaterialConflictError("该资料正在识别，请识别结束后删除。")
        self._repo.mark_deleting_and_delete(
            material_id,
            active_guide_ids=set(self._active_generations),
        )
        try:
            shutil.rmtree(
                slides_dir_for_material(self._materials_root, material_id),
                ignore_errors=True,
            )
        except Exception:
            pass
        self.retry_pending_cleanups()

    async def get_page_image(self, material_id: str, page_no: int) -> Path:
        mat = self._repo.get_material(material_id)
        if page_no < 1 or page_no > mat.page_count:
            raise MaterialInvalidError(f"页码超出范围（共 {mat.page_count} 页）。")
        pdf_path = resolve_material_pdf(self._materials_root, mat.stored_name)
        if not pdf_path.is_file():
            raise MaterialNotFoundError("个人资料原文件不存在。")
        slide_dir = slides_dir_for_material(self._materials_root, material_id)
        return await anyio.to_thread.run_sync(
            lambda: render_pdf_page_image(pdf_path, slide_dir, page_no=page_no)
        )

    def import_sample_artifact(
        self, material_id: str, *, resolve_sample: Callable[[], Path]
    ) -> ArtifactDetailView:
        mat = self._repo.get_material(material_id)
        sample_dir = resolve_sample()
        if mat.file_sha256 != EXAMPASS_PHYSICS_SAMPLE_SHA256:
            raise MaterialInvalidError(
                "该个人资料与静电场样例原件不匹配，请上传对应的《大学物理II期中讲座(1).pdf》后再导入样例。"
            )
        bundle = load_sample_exampass_bundle(sample_dir, max_page=mat.page_count)
        artifact_id, reused = self._repo.upsert_ready_artifact(
            material_id=mat.id,
            file_sha256=mat.file_sha256,
            start_page=1,
            end_page=mat.page_count,
            rule_version="exampass-sample-v1",
            course_hint="大学物理（导入样例）",
            title=str(bundle["title"]),
            is_sample=True,
            skeleton=bundle["skeleton"],
            notes_html=str(bundle["notes_html"]),
            graph_tree=bundle["graph_tree"],
            coverage_warnings=[
                "当前为本地 ExamPass 样例导入展示，用于验证笔记、测试、图谱与原页对照功能。",
                "内容核对提示：同轴电缆能量公式若 Q 为总电荷，结果中长度 l 应位于分母，请结合右侧第 33–35 页原件核对。",
            ],
            questions=bundle["questions"],
        )
        return self._repo.get_artifact_detail(artifact_id, reused=reused)

    def get_artifact(self, artifact_id: str) -> ArtifactDetailView:
        return self._repo.get_artifact_detail(artifact_id)

    def submit_quiz_attempt(
        self, *, artifact_id: str, question_id: str, user_answer: object
    ) -> ArtifactDetailView:
        row = self._repo.get_quiz_question_row(artifact_id, question_id)
        q_type = str(row["q_type"])
        max_points = float(row["points"])
        if q_type in ("choice", "multi", "tf", "fill"):
            options = json.loads(str(row["options_json"])) if row["options_json"] else None
            correct_answer = json.loads(str(row["answer_json"]))
            try:
                grading_status, is_correct, score, feedback = grade_objective_question(
                    q_type=q_type,
                    stem=str(row["stem"]),
                    options=options,
                    correct_answer=correct_answer,
                    user_answer=user_answer,
                    max_points=max_points,
                )
            except ValueError as exc:
                raise MaterialInvalidError(f"作答格式无效：{exc}") from exc
            self._repo.save_quiz_attempt(
                artifact_id=artifact_id,
                question_id=question_id,
                user_answer=user_answer,
                is_correct=is_correct,
                score=score,
                max_points=max_points,
                grading_status=grading_status,
                feedback=feedback,
            )
        else:
            self._repo.save_quiz_attempt(
                artifact_id=artifact_id,
                question_id=question_id,
                user_answer=user_answer,
                is_correct=None,
                score=None,
                max_points=max_points,
                grading_status="pending_review",
                feedback="主观题已保存作答，可前往对话评价或主动查看参考解答。",
            )
        return self._repo.get_artifact_detail(artifact_id)

    def reveal_quiz_solution(
        self, *, artifact_id: str, question_id: str
    ) -> ArtifactDetailView:
        row = self._repo.get_quiz_question_row(artifact_id, question_id)
        self._repo.reveal_quiz_solution(
            artifact_id=artifact_id,
            question_id=question_id,
            max_points=float(row["points"]),
        )
        return self._repo.get_artifact_detail(artifact_id)

    def update_graph_note(
        self, *, artifact_id: str, node_id: str, note_text: str, is_knowledge_point: bool = False
    ) -> GraphNoteView:
        return self._repo.upsert_graph_note(
            artifact_id=artifact_id,
            node_id=node_id,
            note_text=note_text,
            is_knowledge_point=is_knowledge_point,
        )

    def prepare_study_from_artifact_question(
        self,
        *,
        artifact_id: str,
        question_id: str,
        mode: str,
        conversation_id: str | None = None,
    ) -> StudyFromArtifactQuestionResponse:
        art = self._repo.get_artifact_detail(artifact_id)
        if art.quiz_status != "ready":
            raise MaterialConflictError("章节测试尚未生成完成，请稍候完成后再操作。")
        target_conv_id = conversation_id or art.conversation_id
        if not target_conv_id:
            raise GuideNotFoundError("该复习产物尚未关联学习会话。")
        if conversation_id and not self._repo.is_artifact_accessible_in_conversation(
            conversation_id=conversation_id, artifact_id=artifact_id
        ):
            raise GuideNotFoundError("该复习产物未关联到当前学习会话。")
        if not any(q.id == question_id for q in art.questions):
            raise GuideNotFoundError("测试题目不存在。")
        if mode == "evaluate":
            prompt = "请结合我的作答过程，评价我选中的这道章节测试题"
        else:
            prompt = "请围绕我选中的章节测试题目进行讲解或解答我的疑问"
        return StudyFromArtifactQuestionResponse(
            conversation_id=target_conv_id,
            artifact_id=art.id,
            question_id=question_id,
            mode=mode,  # type: ignore[arg-type]
            prompt=prompt,
        )

    def prepare_study_from_artifact_kc(
        self,
        *,
        artifact_id: str,
        kc_id: str,
        mode: str,
        conversation_id: str | None = None,
    ) -> StudyFromArtifactKcResponse:
        art = self._repo.get_artifact_detail(artifact_id)
        if art.skeleton is None and art.status != "ready":
            raise MaterialConflictError("复习资料尚未生成完成，请稍候完成后再操作。")
        target_conv_id = conversation_id or art.conversation_id
        if not target_conv_id:
            raise GuideNotFoundError("该复习产物尚未关联学习会话。")
        if conversation_id and not self._repo.is_artifact_accessible_in_conversation(
            conversation_id=conversation_id, artifact_id=artifact_id
        ):
            raise GuideNotFoundError("该复习产物未关联到当前学习会话。")
        found = False
        for ch in (art.skeleton or {}).get("chapters", []) or []:
            for kc in ch.get("kcs", []) or []:
                if str(kc.get("id", "")) == kc_id or str(kc.get("label", "")) == kc_id:
                    found = True
                    kc_id = str(kc.get("id") or kc_id)
                    break
            if found:
                break
        if not found:
            raise GuideNotFoundError("指定的知识点不存在。")
        prompt = (
            "请讲解我选中的资料知识点"
            if mode == "explain"
            else "请围绕我选中的资料知识点出一道自测练习题"
        )
        return StudyFromArtifactKcResponse(
            conversation_id=target_conv_id,
            artifact_id=art.id,
            kc_id=kc_id,
            mode=mode,  # type: ignore[arg-type]
            prompt=prompt,
        )

    async def evaluate_quiz_question(
        self, *, artifact_id: str, question_id: str
    ) -> ArtifactDetailView:
        row = self._repo.get_quiz_question_row(artifact_id, question_id)
        art = self._repo.get_artifact_detail(artifact_id)
        q_view = next((q for q in art.questions if q.id == question_id), None)
        if q_view is None or q_view.attempt is None or q_view.attempt.user_answer is None:
            raise MaterialInvalidError("请先提交本题作答后再进行 AI 评价。")
        expected_updated_at = self._repo.get_quiz_attempt_updated_at(question_id)
        if expected_updated_at is None:
            raise MaterialInvalidError("请先提交本题作答后再进行 AI 评价。")
        if self._usage_error:
            raise MaterialStorageError(self._usage_error)
        if self._usage_gate is not None:
            self._usage_gate.ensure_available(self._repo.owner_id)
        if self._evaluator_factory is None:
            raise MaterialStorageError("模型服务尚未配置，暂时无法评价作答。")

        correct_ans = json.loads(str(row["answer_json"]))
        ref_solution = f"标准答案：{correct_ans}\n解析：{row['explanation']}"
        user_ans_text = (
            json.dumps(q_view.attempt.user_answer, ensure_ascii=False)
            if not isinstance(q_view.attempt.user_answer, str)
            else q_view.attempt.user_answer
        )
        evaluator = self._evaluator_factory()
        with bind_actor(self._repo.owner_id, request_id=f"quiz-eval:{question_id}"):
            evaluation = await evaluator.evaluate_answer(
                statement=str(row["stem"]),
                reference_solution=ref_solution,
                student_answer=user_ans_text,
                student_solution_text=user_ans_text,
                answered_after_revealed=bool(q_view.attempt.answered_after_revealed),
                attempt_kind="correction" if q_view.attempt.answered_after_revealed else "initial",
            )
        max_points = float(row["points"])
        is_correct = evaluation.is_correct
        score = max_points if is_correct is True else (0.0 if is_correct is False else None)
        feedback_parts = [evaluation.final_answer_assessment]
        if evaluation.step_by_step_analysis:
            feedback_parts.append(evaluation.step_by_step_analysis)
        if evaluation.guidance:
            feedback_parts.append(f"建议：{evaluation.guidance}")
        if evaluation.independence_note:
            feedback_parts.append(evaluation.independence_note)

        self._repo.save_quiz_attempt(
            artifact_id=artifact_id,
            question_id=question_id,
            user_answer=q_view.attempt.user_answer,
            is_correct=is_correct,
            score=score,
            max_points=max_points,
            grading_status="evaluated",
            feedback="\n".join(p for p in feedback_parts if p),
            expected_updated_at=expected_updated_at,
        )
        return self._repo.get_artifact_detail(artifact_id)

    async def generate_or_get_artifact(
        self,
        material_id: str,
        *,
        start_page: int,
        end_page: int,
        course_hint: str | None = None,
        outputs: list[OutputKind] | tuple[OutputKind, ...] | None = None,
        conversation_id: str | None = None,
        progress_callback: Callable[[str, str], None] | None = None,
        coverage_mode: ArtifactInputMode = "full",
    ) -> ArtifactDetailView:
        target_outputs = _normalize_outputs(outputs)
        mat = self._repo.get_material(material_id)
        self._validate_page_range(
            page_count=mat.page_count, start_page=start_page, end_page=end_page
        )
        task_id = f"gen:{uuid.uuid4()}"

        def _report(stage: str, stage_label: str) -> None:
            if conversation_id:
                self._update_progress(task_id, stage=stage, stage_label=stage_label)
            if progress_callback is not None:
                progress_callback(stage, stage_label)

        if conversation_id:
            self._set_progress(
                self._repo.owner_id,
                MaterialProgressItem(
                    task_id=task_id,
                    conversation_id=conversation_id,
                    material_id=mat.id,
                    material_name=mat.display_name,
                    kind="generate",
                    start_page=start_page,
                    end_page=end_page,
                    outputs=list(target_outputs),
                    stage="checking_text",
                    stage_label="检查文字",
                    updated_at=datetime.now(timezone.utc),
                ),
            )
        if progress_callback is not None:
            progress_callback("checking_text", "检查文字")
        try:
            pages = await self._ensure_pages_parsed(
                mat.id, mat.stored_name, start_page, end_page
            )
            rule_version, coverage, pages = bind_inputs(EXAMPASS_RULE_VERSION, pages)
            if coverage.missing_pages and coverage_mode == "recognize":
                missing = coverage.missing_pages
                failures: list[int] = []
                _report("ocr", "补齐所选页")
                if conversation_id:
                    self._update_progress(task_id, ocr_done=0, ocr_total=len(missing))
                for offset in range(0, len(missing), MAX_OCR_PAGES_PER_REQUEST):
                    batch = missing[offset : offset + MAX_OCR_PAGES_PER_REQUEST]
                    result = await self.recognize_pages(mat.id, batch)
                    failures.extend(failure.page_no for failure in result.failures)
                    if conversation_id:
                        self._update_progress(
                            task_id, ocr_done=offset + len(batch), ocr_failed_pages=failures
                        )
                refreshed = await self._ensure_pages_parsed(
                    mat.id, mat.stored_name, start_page, end_page
                )
                rule_version, coverage, pages = bind_inputs(EXAMPASS_RULE_VERSION, refreshed)
            readability = build_readability_report(mat.id, start_page, end_page, pages)
            if coverage.missing_pages and coverage_mode != "partial":
                raise MaterialCoverageRequiredError(
                    coverage.missing_pages, can_generate_partial=readability.sufficient
                )
            if not readability.sufficient:
                raise GuideInsufficientError(
                    readability.recommendation or readability.reason
                )

            acquire = self._repo.acquire_artifact_generation(
                material_id=mat.id,
                file_sha256=mat.file_sha256,
                start_page=start_page,
                end_page=end_page,
                rule_version=rule_version,
                coverage=coverage,
                course_hint=course_hint,
                default_title=mat.display_name.rsplit(".", 1)[0],
                outputs=target_outputs,
                conversation_id=conversation_id,
            )
            if acquire.reused or acquire.generation_token is None:
                return self._repo.get_artifact_detail(acquire.artifact_id, reused=True)

            token = acquire.generation_token
            self._active_generations.add(acquire.artifact_id)
            current_step = acquire.current_step
            need_notes = "notes" in target_outputs and acquire.notes_status != "ready"
            need_quiz = "quiz" in target_outputs and acquire.quiz_status != "ready"
            need_graph = "graph" in target_outputs and acquire.graph_status != "ready"
            try:
                if self._usage_error:
                    raise MaterialStorageError(self._usage_error)

                skeleton = acquire.existing_skeleton
                graph_tree = acquire.existing_graph_tree
                notes_html = acquire.existing_notes_html
                questions = acquire.existing_questions

                # 仅当真正需要模型生成新骨架、笔记或测试时才校验/实例化模型
                need_model = (skeleton is None) or (need_notes and not notes_html) or need_quiz
                generator: DeepSeekExamPassGenerator | None = None
                if need_model:
                    if self._exampass_generator_factory is None:
                        raise MaterialStorageError("模型服务尚未配置，暂时无法生成完整复习资料。")
                    generator = self._exampass_generator_factory()

                review_warnings: list[str] = []
                patched_questions: list[dict[str, object]] | None = None

                with bind_actor(self._repo.owner_id, request_id=f"artifact:{acquire.artifact_id}"):
                    if skeleton is None:
                        assert generator is not None
                        current_step = "skeleton"
                        _report("skeleton", "生成知识骨架")
                        if self._usage_gate is not None:
                            self._usage_gate.ensure_available(self._repo.owner_id)
                        skeleton = await generator.generate_skeleton(
                            display_name=mat.display_name,
                            start_page=start_page,
                            end_page=end_page,
                            pages=pages,
                            course_hint=course_hint,
                        )
                        graph_tree = skeleton_to_graph_tree(skeleton)
                        title = str(skeleton.get("title") or mat.display_name)
                        next_step = "notes" if need_notes else ("quiz" if need_quiz else "completed")
                        if not self._repo.save_artifact_step_skeleton(
                            artifact_id=acquire.artifact_id,
                            generation_token=token,
                            title=title,
                            skeleton=skeleton,
                            graph_tree=graph_tree,
                            mark_graph_ready=need_graph,
                            next_step=next_step,
                        ):
                            raise MaterialConflictError("资料状态已变更，请刷新后重试。")
                    elif need_graph and graph_tree is None:
                        _report("graph", "生成知识图谱")
                        graph_tree = skeleton_to_graph_tree(skeleton)
                        title = str(skeleton.get("title") or mat.display_name)
                        next_step = "notes" if need_notes else ("quiz" if need_quiz else "completed")
                        if not self._repo.save_artifact_step_skeleton(
                            artifact_id=acquire.artifact_id,
                            generation_token=token,
                            title=title,
                            skeleton=skeleton,
                            graph_tree=graph_tree,
                            mark_graph_ready=True,
                            next_step=next_step,
                        ):
                            raise MaterialConflictError("资料状态已变更，请刷新后重试。")

                    if need_notes and not notes_html:
                        assert generator is not None
                        current_step = "notes"
                        _report("notes", "生成章节笔记")
                        if self._usage_gate is not None:
                            self._usage_gate.ensure_available(self._repo.owner_id)
                        notes_sig = inspect.signature(generator.generate_notes)
                        accepts_stage = (
                            "on_stage" in notes_sig.parameters
                            or any(
                                p.kind == inspect.Parameter.VAR_KEYWORD
                                for p in notes_sig.parameters.values()
                            )
                        )
                        notes_kwargs: dict[str, object] = {}
                        if accepts_stage:
                            notes_kwargs["on_stage"] = _report
                        notes_html = await generator.generate_notes(
                            display_name=mat.display_name,
                            start_page=start_page,
                            end_page=end_page,
                            skeleton=skeleton,
                            pages=pages,
                            course_hint=course_hint,
                            **notes_kwargs,  # type: ignore[arg-type]
                        )
                        if not self._repo.save_artifact_step_notes(
                            artifact_id=acquire.artifact_id,
                            generation_token=token,
                            notes_html=notes_html,
                            next_step="quiz" if need_quiz else "completed",
                        ):
                            raise MaterialConflictError("资料状态已变更，请刷新后重试。")

                    if need_quiz:
                        assert generator is not None
                        if not questions:
                            current_step = "quiz"
                            _report("quiz", "生成章节测试")
                            if self._usage_gate is not None:
                                self._usage_gate.ensure_available(self._repo.owner_id)
                            questions = await generator.generate_questions(
                                display_name=mat.display_name,
                                start_page=start_page,
                                end_page=end_page,
                                skeleton=skeleton,
                                pages=pages,
                                course_hint=course_hint,
                            )
                            if not self._repo.save_artifact_step_quiz(
                                artifact_id=acquire.artifact_id,
                                generation_token=token,
                                questions=questions,
                            ):
                                raise MaterialConflictError("资料状态已变更，请刷新后重试。")

                        current_step = "review"
                        _report("review", "审校")
                        if self._usage_gate is not None:
                            self._usage_gate.ensure_available(self._repo.owner_id)
                        review_warnings, patched_questions = await generator.review_and_refine(
                            display_name=mat.display_name,
                            start_page=start_page,
                            end_page=end_page,
                            skeleton=skeleton,
                            questions=questions,
                            pages=pages,
                        )

                all_warnings: list[str] = list(acquire.existing_coverage_warnings)
                if readability.affected_pages:
                    affected_str = "、".join(str(p.page_no) for p in readability.affected_pages)
                    scan_warn = (
                        f"第 {affected_str} 页存在扫描或公式提取告警，阅读公式与图示时请点击右侧原页核对。"
                    )
                    if scan_warn not in all_warnings:
                        all_warnings.append(scan_warn)
                for w in review_warnings:
                    if w not in all_warnings:
                        all_warnings.append(w)

                if not self._repo.complete_artifact_generation(
                    artifact_id=acquire.artifact_id,
                    generation_token=token,
                    coverage_warnings=all_warnings,
                    questions_override=patched_questions,
                    mark_outputs_ready=target_outputs,
                ):
                    raise MaterialConflictError("资料状态已变更，请刷新后重试。")
                return self._repo.get_artifact_detail(acquire.artifact_id, reused=False)
            except QueryError as exc:
                self._repo.fail_artifact_generation(
                    artifact_id=acquire.artifact_id,
                    generation_token=token,
                    failed_step=current_step,
                    error_code=exc.code,
                    error_message=f"[{current_step}] {exc.message}",
                )
                return self._repo.get_artifact_detail(acquire.artifact_id, reused=False)
            except asyncio.CancelledError:
                self._repo.fail_artifact_generation(
                    artifact_id=acquire.artifact_id,
                    generation_token=token,
                    failed_step=current_step,
                    error_code="CANCELLED",
                    error_message=f"[{current_step}] 生成任务已取消，可点击重试从当前步骤继续。",
                )
                raise
            except Exception as exc:
                self._repo.fail_artifact_generation(
                    artifact_id=acquire.artifact_id,
                    generation_token=token,
                    failed_step=current_step,
                    error_code="GENERATE_FAILED",
                    error_message=f"[{current_step}] {exc}",
                )
                raise
            finally:
                self._active_generations.discard(acquire.artifact_id)
        finally:
            self._clear_progress(task_id)

    async def upload_pdf_for_conversation(
        self,
        *,
        conversation_id: str,
        filename: str | None,
        data: bytes,
    ) -> ConversationMaterialUploadResponse:
        summary = await self.upload_pdf(filename=filename, data=data)
        try:
            item = self._repo.attach_material_to_conversation(
                conversation_id=conversation_id, material_id=summary.id
            )
        except Exception:
            # 会话不存在等异常时，清理刚上传的孤立资料
            try:
                self.delete_material(summary.id)
            except Exception:
                pass
            raise
        overview = item.readability
        title = summary.display_name.rsplit(".", 1)[0]
        if summary.is_exam:
            notice = f"收到《{title}》了。我可以按原题给出解答和步骤。{_SCOPE_QUESTION}"
            if overview is not None and (overview.scanned_pages or overview.ocr_pages or overview.formula_warning_pages):
                notice += "\n\n这是扫描件或有公式需要核对。你选好题目后，我会将必要的原页图片交给 DeepSeek 核对。"
        elif overview is not None and not overview.sufficient:
            notice = (
                f"已收到《{title}》，共 {summary.page_count} 页。"
                "这份资料的可用文字不足。你可以预览原件，或在资料卡选择「识别扫描页」，"
                "识别后再按需生成复习成果。"
            )
        elif overview is not None and overview.scanned_pages:
            scanned_str = "、".join(str(p) for p in overview.scanned_pages[:6])
            more = " 等" if len(overview.scanned_pages) > 6 else ""
            notice = (
                f"已收到《{title}》，共 {summary.page_count} 页（其中第 {scanned_str}{more} 页疑似扫描页，阅读时可核对原页）。"
                "你想先看章节笔记、做一套章节测试，还是看知识图谱？也可以指定页码。"
            )
        else:
            notice = (
                f"已收到《{title}》，共 {summary.page_count} 页。"
                "你想先看章节笔记、做一套章节测试，还是看知识图谱？也可以指定页码。"
            )
        return ConversationMaterialUploadResponse(
            conversation_id=conversation_id,
            item=item,
            assistant_notice=notice,
        )

    def attach_to_conversation(
        self, *, conversation_id: str, material_id: str
    ) -> ConversationMaterialItem:
        return self._repo.attach_material_to_conversation(
            conversation_id=conversation_id, material_id=material_id
        )

    def detach_from_conversation(
        self, *, conversation_id: str, material_id: str
    ) -> bool:
        return self._repo.detach_material_from_conversation(
            conversation_id=conversation_id, material_id=material_id
        )

    def list_conversation_materials(
        self, conversation_id: str
    ) -> list[ConversationMaterialItem]:
        return self._repo.list_conversation_materials(conversation_id)

    def list_library(self) -> list[MaterialLibraryItem]:
        return self._repo.list_material_library()

    def get_conversation_delete_impact(
        self, conversation_id: str
    ) -> ConversationDeleteImpactView:
        return self._repo.get_conversation_delete_impact(conversation_id)

    def retry_pending_cleanups(self) -> int:
        """重试清理 pending_file_cleanups 中残留的磁盘文件，返回本次成功回收数。"""

        cleaned = 0
        for cleanup_id, root_kind, stored_name in self._repo.list_pending_cleanups():
            root = (
                self._materials_root
                if root_kind == "material"
                else self._attachments_root
            )
            err = try_delete_file(root, stored_name)
            if err is None:
                self._repo.mark_cleanup_succeeded(cleanup_id)
                cleaned += 1
            else:
                self._repo.record_cleanup_failure(cleanup_id, err)
        return cleaned

    @classmethod
    def cleanup_pending_files_on_startup(
        cls,
        db_path: Path,
        *,
        materials_root: Path | None = None,
        attachments_root: Path | None = None,
    ) -> int:
        """服务启动时独立重试清理待回收磁盘文件。"""

        repo = MaterialRepository(db_path, owner_id="__startup__")
        svc = cls(
            repo,
            config=MaterialConfigState(),
            materials_root=materials_root or MATERIALS_ROOT,
            attachments_root=attachments_root or ATTACHMENTS_ROOT,
        )
        return svc.retry_pending_cleanups()
