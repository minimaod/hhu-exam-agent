"""错题记录业务服务：创建、回看、修改、纠正、采用新版和删除。

记录是会话题目的快照，不是“自动掌握”或“自动收藏”。创建/纠正/更新都由
程序执行；模型只生成讲解内容，不能声称已经记录。纠正记录时会重新讲解，
旧快照写入修订，当前讲解始终对应当前题干。
"""

import asyncio
import logging
import json
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
from fastapi import Request
from pydantic import ValidationError

from app.conversations.inputs import canonical_json
from app.conversations.models import ExerciseAttemptView, ExerciseEvaluation, ExerciseView
from app.conversations.errors import (
    AttachmentInvalidError,
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    ConversationStorageError,
    InvalidImageError,
    MistakeRecordConflictError,
    MistakeRecordNotFoundError,
    ProblemNotFoundError,
    ProblemNotReadyError,
    SourceInvalidError,
    TutoringUnavailableError,
)
from app.conversations.repository import (
    ConversationRepository,
    ProblemRecord,
    RecordRow,
    RecordSnapshot,
)
from app.mistakes.models import (
    FeedbackView,
    FollowupView,
    MistakeRecordDetail,
    MistakeRecordListResponse,
    RecordCorrectionErrorEvent,
    RecordCorrectionEvent,
    RecordCorrectionProgressEvent,
    RecordCorrectionResultEvent,
    RecordNeedsClarificationResult,
    RecordUpdatedResult,
    RevisionView,
)
from app.papers.errors import CatalogUnavailableError
from app.papers.service import PaperService
from app.querying.errors import QueryError
from app.quota.errors import quota_of
from app.streaming import SENTINEL, stream_events
from app.text_excerpt import math_safe_excerpt
from app.tutoring.llm import DeepSeekTutor
from app.tutoring.models import (
    AttachmentView,
    Explanation,
    ImagePayload,
    RecordSummary,
    Source,
    TutoringResult,
)
from app.tutoring.service import (
    build_needs_clarification,
    load_images,
    record_title,
)
from app.tutoring.workflow import build_tutoring_workflow

logger = logging.getLogger(__name__)

CORRECTION_TIMEOUT_SECONDS = 150.0
_STORAGE_MESSAGE = "本地学习数据暂时不可用，请稍后再试。"
_TUTORING_MESSAGE = "这次讲解没有完成，请重试。"
_TIMEOUT_MESSAGE = "讲解超时，请重试。"
_SOURCE_MESSAGE = "题目来源未登记或不可用。"


@dataclass(frozen=True)
class CorrectionInput:
    statement_text: str
    attachments: list[tuple[str, str]] | None
    source: Source | None
    student_answer_text: str | None
    solution_text: str | None


_STANDALONE_TITLE_PREFIX = "【自主练习主题】"
_PLACEHOLDER_FINAL_ANSWER = "（请先独立作答，完成后再查看参考解答）"
_PLACEHOLDER_STEP_TITLE = "独立作答提示"


def _is_placeholder_explanation(explanation: Any) -> bool:
    if explanation is None:
        return False
    final_answer = str(getattr(explanation, "final_answer", "") or "").strip()
    if final_answer == _PLACEHOLDER_FINAL_ANSWER:
        return True
    steps = list(getattr(explanation, "steps", None) or [])
    if len(steps) == 1 and str(getattr(steps[0], "title", "") or "").strip() == _PLACEHOLDER_STEP_TITLE:
        return True
    return False


def _is_standalone_anchor(statement_text: str | None, explanation: Any) -> bool:
    text = str(statement_text or "").strip()
    if text.startswith(_STANDALONE_TITLE_PREFIX):
        return True
    return _is_placeholder_explanation(explanation)


def _clean_standalone_title(raw_title: str, knowledge_point: str | None) -> str:
    title = str(raw_title or "").strip()
    if title.startswith(_STANDALONE_TITLE_PREFIX):
        cleaned = title[len(_STANDALONE_TITLE_PREFIX) :].strip()
        if cleaned:
            return cleaned[:80]
        kp = str(knowledge_point or "").strip()
        return kp[:80] if kp else "自主练习"
    return title or (str(knowledge_point or "").strip()[:80] or "自主练习")


class MistakeRecordService:
    def __init__(
        self,
        repository: ConversationRepository,
        papers: PaperService,
        *,
        make_tutor: Any | None,
        tutoring_error: str | None,
        attachments_root: Path,
    ) -> None:
        self._repository = repository
        self._papers = papers
        self._make_tutor = make_tutor
        self._tutoring_error = tutoring_error
        self._attachments_root = attachments_root

    def tutoring_available(self) -> bool:
        return self._make_tutor is not None

    # ------------------------------------------------------------------
    # 创建与读取
    # ------------------------------------------------------------------

    def create_from_problem(
        self,
        *,
        conversation_id: str,
        problem_id: str,
        request_id: str,
        title: str | None = None,
        feedback_text: str | None = None,
    ) -> tuple[MistakeRecordDetail, bool, bool]:
        problem = self._load_ready_problem(conversation_id, problem_id)
        existing = self._repository.get_record_by_root(
            conversation_id, problem.root_problem_id
        )
        if existing is not None:
            detail = self.build_detail(existing.id)
            return detail, False, existing.problem_version < problem.version

        snapshot = self._snapshot_from_problem(
            problem,
            request_id=request_id,
            title=title,
            feedback_text=feedback_text,
        )
        row, created = self._repository.create_record(snapshot)
        return self.build_detail(row.id), created, False

    def create_for_turn(
        self,
        *,
        conversation_id: str,
        problem_id: str,
        request_id: str,
        output_builder: Any,
    ) -> tuple[RecordRow, bool, bool]:
        """供会话轮次使用：记录插入与轮次完成在同一事务内。"""

        problem = self._load_ready_problem(conversation_id, problem_id)
        existing = self._repository.get_record_by_root(
            conversation_id, problem.root_problem_id
        )
        if existing is not None:
            row, created = self._repository.create_record(
                self._snapshot_from_problem(problem, request_id=request_id),
                turn=(conversation_id, request_id),
                output_builder=output_builder,
            )
            return row, created, existing.problem_version < problem.version

        snapshot = self._snapshot_from_problem(problem, request_id=request_id)
        row, created = self._repository.create_record(
            snapshot,
            turn=(conversation_id, request_id),
            output_builder=output_builder,
        )
        return row, created, False

    def list_records(self, *, limit: int, offset: int) -> MistakeRecordListResponse:
        rows, total = self._repository.list_records(limit=limit, offset=offset)
        return MistakeRecordListResponse(
            items=[self._summary(row) for row in rows],
            total=total,
        )

    def get_record(self, record_id: str) -> MistakeRecordDetail:
        return self.build_detail(record_id)

    def summary_for(self, row: RecordRow) -> RecordSummary:
        return self._summary(row)

    def build_detail(self, record_id: str) -> MistakeRecordDetail:
        row = self._repository.get_record(record_id)
        if row is None:
            raise MistakeRecordNotFoundError("错题记录不存在。")

        revisions = [
            self._revision_view(revision)
            for revision in self._repository.list_record_revisions(record_id)
        ]
        latest = self._repository.latest_explained_problem(row.root_problem_id)
        has_newer = latest is not None and latest.version > row.problem_version
        newer = (
            {"id": latest.id, "version": latest.version}
            if has_newer and latest is not None
            else None
        )

        attachments = [
            AttachmentView(
                id=item.id,
                role=item.role or "statement",  # type: ignore[arg-type]
                mime=item.mime,
                width=item.width,
                height=item.height,
                url=(
                    f"/api/conversations/{row.conversation_id}"
                    f"/attachments/{item.id}"
                ),
            )
            for item in row.attachments
        ]
        feedback = FeedbackView(
            text=(
                str(row.feedback.get("text"))
                if row.feedback.get("text") not in (None, "")
                else None
            ),
            self_reported_understood=row.self_reported_understood,
            self_reported_at=(
                str(row.feedback["self_reported_at"])
                if row.feedback.get("self_reported_at")
                else None
            ),
            updated_at=(
                str(row.feedback["updated_at"])
                if row.feedback.get("updated_at")
                else None
            ),
        )
        exercises_rows = self._repository.list_exercises_by_root_problem(row.root_problem_id)
        exercise_views = [
            self._repository.view_from_exercise(ex) for ex in exercises_rows
        ]
        is_standalone = _is_standalone_anchor(row.statement_text, row.explanation)
        primary_exercise: ExerciseView | None = None
        if is_standalone and exercise_views:
            # 自主练习锚点以真实生成的练习题为主体，不把当前题再列进关联类似练习。
            primary_exercise = next(
                (ex for ex in exercise_views if ex.problem_id == row.problem_id),
                exercise_views[0],
            )
            exercise_views = [ex for ex in exercise_views if ex.id != primary_exercise.id]

        title = _clean_standalone_title(
            row.title, primary_exercise.knowledge_point if primary_exercise else None
        ) if is_standalone else row.title
        statement_text = (
            primary_exercise.statement_text
            if (is_standalone and primary_exercise is not None)
            else row.statement_text
        )
        explanation = (
            None
            if (is_standalone and _is_placeholder_explanation(row.explanation))
            else row.explanation
        )
        student_answer_text = row.student_answer_text
        solution_text = row.solution_text
        if primary_exercise is not None:
            if not student_answer_text:
                student_answer_text = primary_exercise.student_answer_text
            if not solution_text:
                solution_text = primary_exercise.student_solution_text

        return MistakeRecordDetail(
            id=row.id,
            title=title,
            conversation_id=row.conversation_id,
            problem_id=row.problem_id,
            root_problem_id=row.root_problem_id,
            problem_version=row.problem_version,
            statement_text=statement_text,
            student_answer_text=student_answer_text,
            solution_text=solution_text,
            source=row.source,
            is_model_generated=is_standalone,
            attachments=attachments,
            explanation=explanation,
            followups=[FollowupView(**item) for item in row.followups],
            feedback=feedback,
            self_reported_understood=row.self_reported_understood,
            revisions=revisions,
            conversation_has_newer_version=has_newer,
            newer_problem=newer,
            primary_exercise=primary_exercise,
            exercises=exercise_views,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    # ------------------------------------------------------------------
    # 修改、采用新版、删除
    # ------------------------------------------------------------------

    def update_metadata(
        self,
        record_id: str,
        *,
        title: str | None,
        source: Source | None,
        feedback_text: str | None,
        self_reported_understood: bool | None,
    ) -> MistakeRecordDetail:
        validated_source = self._validate_source(source) if source is not None else None
        row = self._repository.update_record_metadata(
            record_id,
            title=title,
            source=validated_source,
            feedback_text=feedback_text,
            self_reported_understood=self_reported_understood,
        )
        return self.build_detail(row.id)

    def adopt_problem(
        self,
        record_id: str,
        *,
        problem_id: str,
        request_id: str,
    ) -> tuple[MistakeRecordDetail, bool]:
        row = self._repository.get_record(record_id)
        if row is None:
            raise MistakeRecordNotFoundError("错题记录不存在。")
        problem = self._repository.get_problem(problem_id)
        if problem is None:
            raise ProblemNotFoundError("题目不存在。")
        if (
            problem.conversation_id != row.conversation_id
            or problem.root_problem_id != row.root_problem_id
        ):
            raise MistakeRecordConflictError("该题目与记录不是同一道题。")
        if problem.status != "explained":
            raise ProblemNotReadyError("题目尚未讲解，不能更新记录。")
        if problem.version <= row.problem_version:
            return self.build_detail(row.id), False

        snapshot = self._snapshot_from_record_and_problem(
            row, problem, request_id=request_id
        )
        updated, created = self._repository.replace_record(
            record_id,
            request_id=request_id,
            reason="adopt_problem",
            new_snapshot=snapshot,
            request_input=canonical_json(
                {"kind": "adopt_problem", "problem_id": problem_id}
            ),
        )
        return self.build_detail(updated.id), created

    def delete_record(self, record_id: str) -> None:
        self._repository.delete_record(record_id)

    # ------------------------------------------------------------------
    # 纠正
    # ------------------------------------------------------------------

    def validate_correction(self, record_id: str, payload: CorrectionInput) -> None:
        """纠正入口在建立 SSE 前统一校验图片数量、重复、归属和总大小。"""

        record = self._repository.get_record(record_id)
        if record is None:
            raise MistakeRecordNotFoundError("错题记录不存在。")
        selected = (
            payload.attachments
            if payload.attachments is not None
            else [(item.id, item.role or "statement") for item in record.attachments]
        )
        self._repository.validate_attachment_selection(
            record.conversation_id, selected
        )

    def correction_input(self, payload: CorrectionInput) -> str:
        """纠正请求的规范化内容，用于同一 request_id 的重试比较。"""

        attachments = (
            None
            if payload.attachments is None
            else sorted(payload.attachments)
        )
        data: dict[str, object] = {
            "kind": "record_correction",
            "statement_text": payload.statement_text,
            "attachments": attachments,
            "source": (
                payload.source.model_dump(mode="json")
                if payload.source is not None
                else None
            ),
            "student_answer_text": payload.student_answer_text,
            "solution_text": payload.solution_text,
        }
        return canonical_json(data)

    def find_correction_replay(
        self,
        record_id: str,
        request_id: str,
        request_input: str | None = None,
    ) -> RecordUpdatedResult | None:
        row = self._repository.get_record(record_id)
        if row is None:
            raise MistakeRecordNotFoundError("错题记录不存在。")
        revision = self._repository.find_record_revision(record_id, request_id)
        if revision is None:
            return None
        previous_input = revision.snapshot.get("_request_input")
        if (
            request_input is not None
            and previous_input is not None
            and previous_input != request_input
        ):
            raise MistakeRecordConflictError(
                "同一个 request_id 不能提交不同的纠正内容。"
            )
        return RecordUpdatedResult(
            record=self.build_detail(record_id),
            revision_created=False,
        )

    async def corrections(
        self,
        record_id: str,
        request_id: str,
        payload: CorrectionInput,
        request: Request,
    ) -> AsyncIterator[RecordCorrectionEvent]:
        """纠正记录并重新讲解；成功保存后才发 result。"""

        row = self._repository.get_record(record_id)
        if row is None:
            raise MistakeRecordNotFoundError("错题记录不存在。")

        request_input = self.correction_input(payload)
        events = stream_events(
            lambda queue: self._produce_correction(
                row, request_id, payload, request_input, queue
            ),
            request,
            timeout=CORRECTION_TIMEOUT_SECONDS,
            timeout_event=RecordCorrectionErrorEvent(
                record_id=record_id,
                request_id=request_id,
                code="TUTORING_FAILED",
                message=_TIMEOUT_MESSAGE,
            ),
            is_terminal=lambda item: isinstance(
                item, (RecordCorrectionResultEvent, RecordCorrectionErrorEvent)
            ),
        )
        async with aclosing(events):
            async for event in events:
                yield event

    async def _produce_correction(
        self,
        record: RecordRow,
        request_id: str,
        payload: CorrectionInput,
        request_input: str,
        queue: "asyncio.Queue[object]",
    ) -> None:
        async def emit(stage: str, message: str) -> None:
            queue.put_nowait(
                RecordCorrectionProgressEvent(
                    record_id=record.id,
                    request_id=request_id,
                    stage=stage,  # type: ignore[arg-type]
                    message=message,
                )
            )

        try:
            if self._make_tutor is None:
                raise TutoringUnavailableError(
                    self._tutoring_error or "辅导模型尚未配置。"
                )

            selected = (
                payload.attachments
                if payload.attachments is not None
                else [(item.id, item.role or "statement") for item in record.attachments]
            )
            attachments = self._repository.validate_attachment_selection(
                record.conversation_id, selected
            )

            source = (
                self._validate_source(payload.source)
                if payload.source is not None
                else record.source
            )
            statement_images = load_images(
                attachments,
                root=self._attachments_root,
                roles={"statement", "supplement"},
            )
            solution_images = load_images(
                attachments, root=self._attachments_root, roles={"solution"}
            )

            tutor = self._make_tutor()
            workflow = build_tutoring_workflow(tutor, emit)
            state = await workflow.ainvoke(
                {
                    "text": payload.statement_text,
                    "statement_images": statement_images,
                    "solution_images": solution_images,
                    "student_answer": (
                        payload.student_answer_text
                        if payload.student_answer_text is not None
                        else record.student_answer_text
                    ),
                    "solution_text": (
                        payload.solution_text
                        if payload.solution_text is not None
                        else record.solution_text
                    ),
                }
            )
            understanding = state["understanding"]
            if understanding.clarity == "unclear" or understanding.multiple_problems:
                questions = [
                    f"请补充或确认：{item}"
                    for item in (
                        understanding.unclear_points
                        or ["题目中的关键条件或符号仍不清楚。"]
                    )
                ]
                queue.put_nowait(
                    RecordCorrectionResultEvent(
                        record_id=record.id,
                        request_id=request_id,
                        result=RecordNeedsClarificationResult(
                            statement_text=understanding.statement,
                            questions=questions[:5],
                            unclear_points=understanding.unclear_points,
                        ),
                    )
                )
                return

            explanation = state["explanation"]
            assert isinstance(explanation, Explanation)
            await emit("saving", "正在保存更新后的讲解")

            changed = (
                understanding.statement != record.statement_text
                or sorted(selected)
                != sorted(
                    (item.id, item.role or "statement")
                    for item in record.attachments
                )
                or (
                    payload.student_answer_text is not None
                    and payload.student_answer_text != record.student_answer_text
                )
                or (
                    payload.solution_text is not None
                    and payload.solution_text != record.solution_text
                )
            )
            snapshot = RecordSnapshot(
                conversation_id=record.conversation_id,
                problem_id=record.problem_id,
                root_problem_id=record.root_problem_id,
                problem_version=record.problem_version,
                request_id=request_id,
                title=record.title,
                statement_text=understanding.statement,
                student_answer_text=(
                    payload.student_answer_text
                    if payload.student_answer_text is not None
                    else record.student_answer_text
                ),
                solution_text=(
                    payload.solution_text
                    if payload.solution_text is not None
                    else record.solution_text
                ),
                source=source,
                explanation=explanation,
                followups=record.followups,
                feedback=record.feedback,
                self_reported_understood=record.self_reported_understood,
                attachments=selected,
            )
            updated, created = self._repository.replace_record(
                record.id,
                request_id=request_id,
                reason="correction" if changed else "retutor",
                new_snapshot=snapshot,
                request_input=request_input,
                expected_updated_at=record.updated_at,
            )
            queue.put_nowait(
                RecordCorrectionResultEvent(
                    record_id=record.id,
                    request_id=request_id,
                    result=RecordUpdatedResult(
                        record=self.build_detail(updated.id),
                        revision_created=created,
                    ),
                )
            )
        except QueryError as exc:
            queue.put_nowait(
                RecordCorrectionErrorEvent(
                    record_id=record.id,
                    request_id=request_id,
                    code=exc.code,
                    message=exc.message,
                    quota=quota_of(exc),  # type: ignore[arg-type]
                )
            )
        except TutoringUnavailableError as exc:
            queue.put_nowait(
                RecordCorrectionErrorEvent(
                    record_id=record.id,
                    request_id=request_id,
                    code="TUTORING_FAILED",
                    message=exc.message,
                )
            )
        except (
            AttachmentInvalidError,
            AttachmentNotFoundError,
            AttachmentTooLargeError,
            InvalidImageError,
            SourceInvalidError,
        ) as exc:
            queue.put_nowait(
                RecordCorrectionErrorEvent(
                    record_id=record.id,
                    request_id=request_id,
                    code="TUTORING_FAILED",
                    message=str(exc),
                )
            )
        except (MistakeRecordConflictError, MistakeRecordNotFoundError) as exc:
            queue.put_nowait(
                RecordCorrectionErrorEvent(
                    record_id=record.id,
                    request_id=request_id,
                    code="TUTORING_FAILED",
                    message=str(exc),
                )
            )
        except ConversationStorageError:
            queue.put_nowait(
                RecordCorrectionErrorEvent(
                    record_id=record.id,
                    request_id=request_id,
                    code="STORAGE_UNAVAILABLE",
                    message=_STORAGE_MESSAGE,
                )
            )
        except CatalogUnavailableError:
            queue.put_nowait(
                RecordCorrectionErrorEvent(
                    record_id=record.id,
                    request_id=request_id,
                    code="CATALOG_UNAVAILABLE",
                    message=_SOURCE_MESSAGE,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("纠正错题记录失败")
            queue.put_nowait(
                RecordCorrectionErrorEvent(
                    record_id=record.id,
                    request_id=request_id,
                    code="TUTORING_FAILED",
                    message=_TUTORING_MESSAGE,
                )
            )
        finally:
            queue.put_nowait(SENTINEL)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _load_ready_problem(
        self, conversation_id: str, problem_id: str
    ) -> ProblemRecord:
        problem = self._repository.get_problem(problem_id)
        if problem is None or problem.conversation_id != conversation_id:
            raise ProblemNotFoundError("题目不存在。")
        if problem.status != "explained" or problem.explanation is None:
            raise ProblemNotReadyError("题目尚未讲解，不能记录。")
        return problem

    def _snapshot_from_problem(
        self,
        problem: ProblemRecord,
        *,
        request_id: str,
        title: str | None = None,
        feedback_text: str | None = None,
    ) -> RecordSnapshot:
        now = datetime.now(timezone.utc).isoformat()
        feedback: dict[str, object] = {
            "text": feedback_text,
            "updated_at": now if feedback_text is not None else None,
            "self_reported_at": now if problem.self_reported_understood else None,
        }
        return RecordSnapshot(
            conversation_id=problem.conversation_id,
            problem_id=problem.id,
            root_problem_id=problem.root_problem_id,
            problem_version=problem.version,
            request_id=request_id,
            title=title[:80] if title else record_title(problem.statement_text),
            statement_text=problem.statement_text,
            student_answer_text=problem.student_answer_text,
            solution_text=problem.solution_text,
            source=problem.source,
            explanation=problem.explanation,
            followups=self._repository.get_problem_followups(problem.id),
            feedback=feedback,
            self_reported_understood=problem.self_reported_understood,
            attachments=[
                (item.id, item.role or "statement") for item in problem.attachments
            ],
        )

    def _snapshot_from_record_and_problem(
        self,
        record: RecordRow,
        problem: ProblemRecord,
        *,
        request_id: str,
    ) -> RecordSnapshot:
        return RecordSnapshot(
            conversation_id=record.conversation_id,
            problem_id=problem.id,
            root_problem_id=problem.root_problem_id,
            problem_version=problem.version,
            request_id=request_id,
            title=record.title,
            statement_text=problem.statement_text,
            student_answer_text=problem.student_answer_text,
            solution_text=problem.solution_text,
            source=problem.source,
            explanation=problem.explanation,
            followups=self._repository.get_problem_followups(problem.id),
            feedback=record.feedback,
            self_reported_understood=record.self_reported_understood,
            attachments=[
                (item.id, item.role or "statement") for item in problem.attachments
            ],
        )

    def _validate_source(self, source: Source | None) -> Source:
        if source is None:
            return Source(type="none")
        if source.type != "registered_paper":
            return source
        assert source.paper_id is not None
        paper = self._papers.get_paper(source.paper_id)
        if paper is None:
            raise SourceInvalidError(_SOURCE_MESSAGE)
        return Source(
            type="registered_paper",
            paper_id=paper.id,
            paper_title=paper.title,
        )

    def _summary(self, row: RecordRow) -> RecordSummary:
        is_standalone = _is_standalone_anchor(row.statement_text, row.explanation)
        title = row.title
        statement_text = row.statement_text
        if is_standalone:
            exercises_rows = self._repository.list_exercises_by_root_problem(
                row.root_problem_id
            )
            primary_ex = next(
                (ex for ex in exercises_rows if ex.problem_id == row.problem_id),
                exercises_rows[0] if exercises_rows else None,
            )
            title = _clean_standalone_title(
                row.title,
                primary_ex.knowledge_point if primary_ex is not None else None,
            )
            if primary_ex is not None and primary_ex.statement_text.strip():
                statement_text = primary_ex.statement_text

        # 截断时不切断公式，避免列表摘要出现半截 LaTeX 源码。
        excerpt = math_safe_excerpt(statement_text, 90)
        return RecordSummary(
            id=row.id,
            title=title,
            statement_excerpt=excerpt,
            source=row.source,
            is_model_generated=is_standalone,
            problem_version=row.problem_version,
            self_reported_understood=row.self_reported_understood,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _revision_view(self, revision: Any) -> RevisionView:
        snapshot = revision.snapshot
        source_raw = snapshot.get("source", {})
        explanation_raw = snapshot.get("explanation")
        try:
            source = Source.model_validate(source_raw)
            explanation = (
                Explanation.model_validate(explanation_raw)
                if explanation_raw is not None
                else None
            )
        except ValidationError as exc:
            raise ConversationStorageError("错题记录的修订内容不完整。") from exc
        return RevisionView(
            id=revision.id,
            request_id=revision.request_id,
            reason=revision.reason,
            statement_text=str(snapshot.get("statement_text", "")),
            source=source,
            explanation=explanation,
            created_at=revision.created_at,
        )
