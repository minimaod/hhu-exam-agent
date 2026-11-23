"""会话服务入口：组装依赖、公开方法、轮次执行入口与调度衔接。"""

import asyncio
import json
import logging
import uuid
from collections.abc import (
    AsyncIterator,
    Callable,
)
from contextlib import aclosing, nullcontext, suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import Request

from app.conversations import final_reply
from app.conversations.context import DynamicContextMixin
from app.conversations.dispatch import DeepSeekIntentRouter
from app.conversations.errors import (
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationQueryUnavailableError,
    ConversationStorageError,
    ProblemNotFoundError,
    SourceInvalidError,
    TurnInvalidError,
    TutoringUnavailableError,
)
from app.conversations.explain import ExplainMixin
from app.conversations.inputs import (
    canonical_json,
    query_input,
)
from app.conversations.legacy import LegacyFlowMixin
from app.conversations.memory_handlers import MemoryHandlerMixin
from app.conversations.models import (
    ConversationContextView,
    ConversationDetail,
    ConversationListResponse,
    ConversationSummary,
    ExerciseView,
    RecordRef,
    SuggestionItem,
    TurnErrorEvent,
    TurnEvent,
    WorkbenchSource,
    reply_state as _reply_state,
)
from app.conversations.narration import NarrationMixin
from app.conversations.practice import PracticeMixin
from app.conversations.question_flow import QuestionFlowMixin
from app.conversations.read_tools import ReadToolMixin
from app.conversations.reply import material_from_payload
from app.conversations.repository import (
    AttachmentRecord,
    ConversationRepository,
    ProblemRecord,
)
from app.conversations.review_loop import ReviewLoopMixin
from app.conversations.search import SearchMixin
from app.conversations.selection import SelectionMixin
from app.conversations.turn_types import (
    ConversationStoreState,
    ExecuteTurn,
    PreparedTurn,
    ReplayTurn,
    TurnInput,
    TurnInputRulesMixin,
    _DynamicContext,
    _QUESTION_KINDS,
    _RunState,
    _replay_event,
    _storage_error_event,
    build_store_state,
)
from app.images import ATTACHMENTS_ROOT
from app.mistakes.models import RecordCorrectionEvent
from app.mistakes.service import CorrectionInput, MistakeRecordService
from app.papers.service import PaperService
from app.querying.service import QueryService
from app.quota.errors import (
    QuotaError,
    QuotaStorageError,
    TurnInProgressError,
)
from app.quota.gate import UsageGate
from app.quota.lease import LeaseRegistry
from app.quota.transport import bind_actor
from app.questions.errors import QuestionBankUnavailableError
from app.questions.runtime import (
    QuestionRuntime,
    bank_root_from_env,
)
from app.tutoring.models import Source
from app.tutoring.service import build_problem_view

logger = logging.getLogger("app.conversations.service")

def _is_dynamic_recovery(recovery: dict[str, Any] | None) -> bool:
    """动态轮次的恢复记录；F008 起版本号为 2，旧记录是 1，都必须能恢复。"""

    if not isinstance(recovery, dict):
        return False
    version = recovery.get("version")
    return isinstance(version, int) and version >= 1

class ConversationService(
    TurnInputRulesMixin,
    DynamicContextMixin,
    NarrationMixin,
    LegacyFlowMixin,
    QuestionFlowMixin,
    SearchMixin,
    SelectionMixin,
    ReadToolMixin,
    ExplainMixin,
    PracticeMixin,
    MemoryHandlerMixin,
    ReviewLoopMixin,
):
    """会话服务入口；按职责混入上下文、正文、旧流程和各工具处理器。"""

    def __init__(
        self,
        repository: ConversationRepository,
        make_query: Callable[[], QueryService] | None,
        query_error: str | None,
        *,
        make_tutor: Callable[[], Any] | None = None,
        tutoring_error: str | None = None,
        make_router: Callable[[], DeepSeekIntentRouter] | None = None,
        make_exercise_agent: Callable[[], Any] | None = None,
        make_scheduler: Callable[[], Any] | None = None,
        make_reply: Callable[[], Any] | None = None,
        make_auth_verifier: Callable[[], Any] | None = None,
        papers: PaperService | None = None,
        records: MistakeRecordService | None = None,
        memory: Any | None = None,
        make_material_service: Callable[[], Any] | None = None,
        attachments_root: Path = ATTACHMENTS_ROOT,
        question_runtime: QuestionRuntime | None = None,
        quota: UsageGate | None = None,
        usage_error: str | None = None,
        lease_registry: LeaseRegistry | None = None,
        allow_unscoped: bool = False,
    ) -> None:
        if repository.owner_id is None and not allow_unscoped:
            raise ConversationStorageError(
                "缺少用户身份，不能创建业务会话服务。"
            )
        self._repository = repository
        self._make_query = make_query
        self._query_error = query_error
        self._make_tutor = make_tutor
        self._tutoring_error = tutoring_error
        self._make_router = make_router
        self._make_exercise_agent = make_exercise_agent
        self._make_scheduler = make_scheduler
        self._make_reply = make_reply
        self._make_auth_verifier = make_auth_verifier
        self._papers = papers
        self._records = records
        self._memory = memory
        self._make_material_service = make_material_service
        self._attachments_root = attachments_root
        self._quota = quota
        self._usage_error = usage_error
        self._lease_registry = lease_registry
        self._lease_by_request: dict[str, str] = {}
        self._current_lease_token: str | None = None
        self._owns_question_runtime = question_runtime is None
        self.questions = question_runtime or QuestionRuntime(
            None, error="题目库未启用", bank_root=bank_root_from_env()
        )

    def close(self) -> None:
        """只关闭本服务显式持有的题库运行时；共享运行时由应用生命周期关闭。"""

        if getattr(self, "_owns_question_runtime", False):
            self.questions.close()

    @property
    def records(self) -> MistakeRecordService | None:
        return self._records

    def get_active_problem(self, conversation_id: str) -> ProblemRecord | None:
        return self._repository.get_active_problem(conversation_id)

    def _require_owned_conversation(self, conversation_id: str) -> None:
        if self._repository.get_conversation(conversation_id) is None:
            raise ConversationNotFoundError("会话不存在。")

    def create_conversation(self) -> ConversationSummary:
        return self._repository.create_conversation()

    def list_conversations(self) -> ConversationListResponse:
        items = self._repository.list_conversations()
        return ConversationListResponse(items=items, total=len(items))

    def get_detail(self, conversation_id: str) -> ConversationDetail:
        conversation = self._repository.get_conversation(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError("会话不存在。")
        active = self._repository.get_active_problem(conversation_id)
        records_map = self._repository.list_records_by_conversation(conversation_id)
        records_by_root = {
            root_id: RecordRef(id=rec.id, problem_version=rec.problem_version)
            for root_id, rec in records_map.items()
        }
        record_ref: RecordRef | None = None
        if active is not None:
            record_ref = records_by_root.get(active.root_problem_id)

        active_ex = self._repository.get_active_exercise(conversation_id)
        ex_view = None
        if active_ex is not None:
            ex_view = self._build_exercise_view(active_ex)

        ctx_rec = self._repository.get_context(conversation_id)
        ctx_view = None
        if ctx_rec is not None:
            sugs = []
            if ctx_rec.last_suggestions_json:
                with suppress(Exception):
                    sugs = [
                        SuggestionItem.model_validate(it)
                        for it in json.loads(ctx_rec.last_suggestions_json)
                    ]
            ctx_view = ConversationContextView(
                confirmed_course=ctx_rec.confirmed_course,
                active_problem_id=ctx_rec.active_problem_id,
                active_exercise_id=ctx_rec.active_exercise_id,
                last_suggestions=sugs,
            )

        active_question = None
        active_selection = self._repository.get_active_question(conversation_id)
        if active_selection is not None:
            active_question = self._question_card_for_selection(
                active_selection, include_answer=True
            )

        turns = [
            self._enrich_question_turn(conversation_id, turn)
            for turn in self._repository.list_turns(conversation_id)
        ]
        materials = self._list_conversation_materials_safe(conversation_id)

        return ConversationDetail(
            conversation=conversation,
            turns=turns,
            active_problem=build_problem_view(active) if active is not None else None,
            active_exercise=ex_view,
            active_question=active_question,
            context=ctx_view,
            record=record_ref,
            records_by_root=records_by_root,
            materials=materials,
        )

    def _material_service_instance(self) -> Any | None:
        if self._make_material_service is not None:
            return self._make_material_service()
        if self._repository.owner_id is None:
            return None
        from app.config import MaterialConfigState
        from app.materials.repository import MaterialRepository
        from app.materials.service import MaterialService

        return MaterialService(
            MaterialRepository(self._repository.db_path, owner_id=self._repository.owner_id),
            config=MaterialConfigState(),
            usage_gate=self._quota,
            usage_error=self._usage_error,
        )

    def _list_conversation_materials_safe(self, conversation_id: str) -> list[dict[str, Any]]:
        if self._repository.owner_id is None:
            return []
        try:
            from app.materials.repository import MaterialRepository

            mat_repo = MaterialRepository(
                self._repository.db_path, owner_id=self._repository.owner_id
            )
            return [
                item.model_dump(mode="json")
                for item in mat_repo.list_conversation_materials(conversation_id)
            ]
        except Exception:
            return []

    def record_material_upload_turn(
        self,
        conversation_id: str,
        *,
        item: Any,
        assistant_notice: str,
        is_attach: bool = False,
    ) -> str:
        from app.conversations.models import ActionResultItem

        mat = item.material
        overview = item.readability
        details: dict[str, Any] = {
            "material_id": mat.id,
            "material_title": mat.display_name,
            "material_is_exam": mat.is_exam,
            "page_count": mat.page_count,
            "sufficient": bool(overview.sufficient) if overview is not None else True,
            "scanned_pages": list(overview.scanned_pages) if overview is not None else [],
            "formula_warning_pages": list(overview.formula_warning_pages) if overview is not None else [],
        }
        action_name = "attach_material"
        query_label = (
            f"关联个人资料：《{mat.display_name}》"
            if is_attach
            else f"上传个人资料：《{mat.display_name}》"
        )
        return self._repository.append_completed_material_turn(
            conversation_id,
            request_id=str(uuid.uuid4()),
            query=query_label,
            assistant_message=assistant_notice,
            composite_results=[
                ActionResultItem(
                    action=action_name,
                    status="success",
                    message=assistant_notice,
                    details=details,
                )
            ],
            title_hint=mat.display_name.rsplit(".", 1)[0],
        )

    def record_material_artifact_turn(
        self,
        conversation_id: str,
        *,
        artifact: Any,
        outputs: list[str],
    ) -> str:
        from app.conversations.models import ActionResultItem
        from app.materials.models import describe_artifact_input

        output_labels = {"notes": "章节笔记", "quiz": "章节测试", "graph": "知识图谱"}
        selected_str = "、".join(output_labels.get(o, o) for o in outputs)
        page_label = (
            f"第 {artifact.start_page}–{artifact.end_page} 页"
            if artifact.start_page != artifact.end_page
            else f"第 {artifact.start_page} 页"
        )
        ready_parts: list[str] = []
        failed_parts: list[str] = []
        if "notes" in outputs:
            (ready_parts if artifact.notes_status == "ready" else failed_parts).append("章节笔记")
        if "quiz" in outputs:
            (ready_parts if artifact.quiz_status == "ready" else failed_parts).append("章节测试")
        if "graph" in outputs:
            (ready_parts if artifact.graph_status == "ready" else failed_parts).append("知识图谱")

        if failed_parts and not ready_parts:
            status = "failed"
            msg = f"《{artifact.title}》（{page_label}）生成{'、'.join(failed_parts)}未完成：{artifact.error_message or '请重试未完成部分。'}"
        elif failed_parts:
            status = "failed"
            msg = (
                f"《{artifact.title}》（{page_label}）已完成{'、'.join(ready_parts)}；"
                f"{'、'.join(failed_parts)}暂未成功，可点击成果卡片重试未完成部分。"
            )
        else:
            status = "success"
            reuse_hint = "（已直接复用既有成果）" if artifact.reused else ""
            msg = f"已为你准备好《{artifact.title}》（{page_label}）的{'、'.join(ready_parts)}{reuse_hint}。"

        input_coverage = getattr(artifact, "coverage", None)
        output_coverage = getattr(artifact, "output_coverage", {})
        partial = bool(input_coverage and input_coverage.partial) or any(
            output_coverage[key].partial for key in outputs if key in output_coverage
        )
        if ready_parts:
            msg += describe_artifact_input(
                input_coverage,
                quiz_question_count=len(artifact.questions) if "章节测试" in ready_parts else None,
            )
            msg += "可在复习工作台查看。"

        return self._repository.append_completed_material_turn(
            conversation_id,
            request_id=str(uuid.uuid4()),
            query=f"生成《{artifact.title}》（{page_label}）：{selected_str}",
            assistant_message=msg,
            composite_results=[
                ActionResultItem(
                    action="generate_material_artifact",
                    status=status,  # type: ignore[arg-type]
                    message=msg,
                    details={
                        "artifact_id": artifact.id,
                        "material_id": artifact.material_id,
                        "material_title": artifact.title,
                        "start_page": artifact.start_page,
                        "end_page": artifact.end_page,
                        "outputs": list(outputs),
                        "notes_status": artifact.notes_status,
                        "quiz_status": artifact.quiz_status,
                        "graph_status": artifact.graph_status,
                        "question_count": len(artifact.questions),
                        "reused": bool(artifact.reused),
                        "coverage_warnings": list(artifact.coverage_warnings),
                        "partial_coverage": partial,
                        "retryable": bool(failed_parts),
                    },
                )
            ],
        )

    def delete_conversation(self, conversation_id: str) -> None:
        from app.images import delete_attachment

        stored_names = self._repository.delete_conversation_with_attachments(
            conversation_id
        )
        for stored_name in stored_names:
            with suppress(Exception):
                delete_attachment(self._attachments_root, stored_name)

    def reveal_exercise_solution(
        self, conversation_id: str, exercise_id: str
    ) -> ExerciseView:
        self._require_owned_conversation(conversation_id)
        ex = self._repository.reveal_exercise_solution(
            exercise_id, conversation_id=conversation_id
        )
        return self._build_exercise_view(ex, allow_solution=True)

    async def regenerate_reply(
        self, conversation_id: str, request_id: str
    ) -> dict[str, Any]:
        """重新生成某一轮的正文，不重跑任何工具操作。

        业务结果（保存的错题、生成的练习、写入的作答）已经在上一轮完成，
        这里只复用当时的正文材料重新写一遍，因此不会产生重复副作用。
        """

        self._require_owned_conversation(conversation_id)
        self._sweep_orphans()
        turn = self._repository.find_turn(conversation_id, request_id)
        if turn is None:
            raise ConversationNotFoundError("该轮次不存在。")
        # 只有已完成的轮次才有“已提交材料”可复用；进行中/失败/中断的轮次
        # 不能借 /reply 绕过正常执行或恢复流程。
        if turn.status != "completed":
            raise ConversationConflictError("这一轮还没有完成，请等它结束后再重做正文。")
        recovery = turn.recovery or {}
        context = recovery.get("reply_context")
        payload = recovery.get("reply_input")
        paper_lookup = self._paper_lookup_reply(turn.result, turn.query, turn.composite_results)
        if paper_lookup is None and not context and not payload:
            raise ConversationConflictError("这一轮没有可复用的回复材料，请重新提出需求。")
        owner = self._repository.owner_id
        notes: list[str] = []
        legacy_public_only = False
        material = None
        lesson_reply = None
        if paper_lookup is None:
            if isinstance(context, dict) and int(context.get("version") or 0) >= 2:
                # 讲解及答案仍需通过当时的授权与当前题目版本核对。
                material, notes = final_reply.material_from_context(
                    self, conversation_id, context
                )
            else:
                material = final_reply.public_material_from_legacy(
                    material_from_payload(payload)
                )
                legacy_public_only = True
            if material.teaching and not notes and not legacy_public_only:
                lesson_reply = self._standalone_explanation_reply(
                    turn.tutoring, turn.composite_results, material.answer_scope
                )
            if lesson_reply is None:
                if self._make_reply is None:
                    raise ConversationConflictError("回复模型尚未配置。")
                if self._usage_error:
                    raise ConversationQueryUnavailableError(self._usage_error)
                if self._quota is not None and owner is not None:
                    self._quota.ensure_available(owner)
        lease_token = str(uuid.uuid4())
        if owner is not None:
            self._repository.acquire_reply_lease(
                conversation_id, request_id, lease_token
            )
        if self._lease_registry is not None:
            self._lease_registry.prepare(lease_token)
            self._lease_registry.mark_started(lease_token, asyncio.current_task())
        actor_context = (
            bind_actor(
                owner,
                request_id=f"reply:{request_id}",
                conversation_id=conversation_id,
            )
            if owner is not None
            else nullcontext()
        )
        try:
            if paper_lookup is not None:
                text = paper_lookup
                meta = {"mode": "clarification", "reason": "paper_lookup", "deltas": 0}
            elif lesson_reply is not None:
                text = lesson_reply
                meta = {"mode": "clarification", "reason": "structured_explanation", "deltas": 0}
            else:
                assert material is not None and self._make_reply is not None
                with actor_context:
                    text, meta = await self._stream_reply(self._make_reply(), material)
        finally:
            self._release_lease(lease_token)
        if not text:
            raise ConversationConflictError("这次仍然没能生成回复，请稍后再试。")
        if legacy_public_only:
            meta = {**meta, "material_scope": "public_only"}
        if notes:
            unique = list(dict.fromkeys(notes))
            text = text.rstrip() + "\n\n" + "；".join(unique)
            meta = {**meta, "reason": "answer_revalidated", "revalidated": unique}
        notice = self._memory_notice_from_material(material) if material is not None else None
        if notice and notice not in text:
            text = text + "\n\n" + notice

        recovery["reply"] = meta
        self._repository.update_turn_reply(
            conversation_id,
            request_id,
            assistant_message=text,
            reply_meta=meta,
        )
        reply_status, reply_retryable = _reply_state(recovery)
        return {
            "request_id": request_id,
            "assistant_message": text,
            "reply_status": reply_status,
            "reply_retryable": reply_retryable,
            "reply": meta,
        }

    def prepare_record_correction(self, record_id: str, request_id: str) -> str:
        """SSE 前获取账号占用；准备失败也保留可重试释放的登记。"""
        self._sweep_orphans()
        token = str(uuid.uuid4())
        if self._lease_registry is not None:
            self._lease_registry.prepare(token)
        try:
            self._repository.acquire_correction_lease(record_id, request_id, token)
        except BaseException:
            self._release_lease(token)
            raise
        return token

    def release_record_correction(self, lease_token: str) -> None:
        """幂等释放；响应尚未开始迭代时也可由响应清理调用。"""
        self._release_lease(lease_token)

    async def stream_record_correction(
        self,
        record_id: str,
        request_id: str,
        correction: CorrectionInput,
        request: Request,
        lease_token: str,
    ) -> AsyncIterator[RecordCorrectionEvent]:
        """先关闭纠正流及其后台任务，再释放账号占用。"""
        if self._lease_registry is not None:
            self._lease_registry.mark_started(lease_token, asyncio.current_task())
        try:
            if self._records is None or self._repository.owner_id is None:
                raise ConversationStorageError("错题记录服务不可用。")
            with bind_actor(self._repository.owner_id, request_id=request_id):
                async with aclosing(self._records.corrections(
                    record_id, request_id, correction, request
                )) as events:
                    async for event in events:
                        yield event
        finally:
            self.release_record_correction(lease_token)

    def _precheck_quota(self, data: TurnInput) -> None:
        """SSE 前的只读额度预检；实际封顶仍在发送前的 SQLite 事务。"""

        if not self._may_use_model(data):
            return
        if self._usage_error:
            raise ConversationQueryUnavailableError(self._usage_error)
        if self._quota is not None and self._repository.owner_id is not None:
            self._quota.ensure_available(self._repository.owner_id)

    @staticmethod
    def _may_use_model(data: TurnInput) -> bool:
        if data.retry_of is not None:
            return True
        if data.question_action == "record":
            return False
        if data.question_action == "exercise":
            return True
        return data.kind not in (
            "record",
            "feedback",
            "reveal_solution",
            "select_question",
            "view_question_answer",
        )

    def _sweep_orphans(self) -> None:
        if self._lease_registry is None:
            return
        tokens = self._lease_registry.orphans()
        if tokens:
            # 只能移除本仓库确实删掉的数据库占用；另一个账号的清理不能
            # 把 A 的内存登记删掉，否则 A 自己再也回收不了数据库占用。
            released = self._repository.release_turn_slots(tokens)
            for token in released:
                self._lease_registry.finish(token)

    def _release_lease(self, token: str) -> bool:
        """先让数据库确认释放，再移除内存登记。

        数据库短暂失败时保留登记，下一次请求的孤儿回收会重试；不能先删内存
        登记，否则数据库里的占用再也没有人负责回收。
        """

        try:
            self._repository.release_turn_slot(token)
        except Exception:
            logger.warning(
                "释放执行占用失败，保留登记等待下次回收", exc_info=True
            )
            if self._lease_registry is not None:
                self._lease_registry.mark_release_pending(token)
            return False
        if self._lease_registry is not None:
            self._lease_registry.finish(token)
        return True

    def _discard_lease(self, token: str) -> None:
        self._release_lease(token)

    def prepare_turn(
        self,
        conversation_id: str,
        data: TurnInput,
    ) -> PreparedTurn:
        """建立 SSE 流之前完成的全部检查；错误按约定返回 HTTP 状态。"""

        if self._repository.get_conversation(conversation_id) is None:
            raise ConversationNotFoundError("会话不存在。")

        self._sweep_orphans()
        token = str(uuid.uuid4())
        self._current_lease_token = token
        if self._lease_registry is not None:
            # 先登记，再尝试写数据库占用；这样“占用已写入、随后准备失败且
            # 首次释放也失败”时，下一次请求能凭登记重试回收。
            self._lease_registry.prepare(token)
        try:
            if data.retry_of is not None:
                prepared = self._prepare_retry(conversation_id, data)
            else:
                prepared = self._prepare_new_turn(conversation_id, data)
        except Exception:
            # 只有本次请求真正持有的租约才允许中断/释放；重复 request_id
            # 被拒绝时不能把原轮次打断。
            try:
                held = self._repository.release_turn_slot(token)
            except Exception:
                # 数据库短暂失败：保留内存登记，下一次孤儿回收重试。
                logger.warning(
                    "轮次准备失败后释放占用失败，保留登记", exc_info=True
                )
                if self._lease_registry is not None:
                    self._lease_registry.mark_release_pending(token)
                held = False
            else:
                if held:
                    with suppress(Exception):
                        self._repository.interrupt_turn(
                            conversation_id, data.request_id
                        )
                if self._lease_registry is not None:
                    self._lease_registry.finish(token)
            raise

        if isinstance(prepared, ExecuteTurn):
            self._lease_by_request[prepared.request_id] = token
            if self._lease_registry is not None:
                self._lease_registry.prepare(token)
        else:
            # 回放已完成轮次：不占新 lease，也不扣额度。
            self._discard_lease(token)
        return prepared

    def _resolve_workbench_source(self, conversation_id: str, data: TurnInput) -> WorkbenchSource:
        from app.materials.errors import GuideNotFoundError
        from app.materials.repository import MaterialRepository

        if (
            not self._repository.owner_id or not data.material_artifact_id
            or not data.material_workbench_view or not data.dispatch or data.kind is not None
            or self._make_scheduler is None or data.attachments or data.question_action
            or data.material_guide_id or data.material_item_id or not data.text
        ):
            raise TurnInvalidError("复习工作台提问需要绑定当前会话内的复习成果和文字问题。")
        repo = MaterialRepository(self._repository.db_path, owner_id=self._repository.owner_id)
        try:
            art = repo.get_artifact_detail(data.material_artifact_id)
        except GuideNotFoundError as exc:
            raise TurnInvalidError("复习成果未关联到当前会话或已删除。") from exc
        if not repo.is_artifact_accessible_in_conversation(conversation_id=conversation_id, artifact_id=art.id):
            raise TurnInvalidError("复习成果未关联到当前会话或已删除。")
        if getattr(art, f"{data.material_workbench_view}_status") != "ready":
            raise TurnInvalidError("当前复习成果尚未生成完成。")
        question = None
        kc_id = data.material_kc_id
        if data.material_question_id:
            question = next((q for q in art.questions if q.id == data.material_question_id), None)
            if not question or data.material_workbench_view != "quiz" or (kc_id and question.kc_id != kc_id):
                raise TurnInvalidError("该题不属于当前知识点的章节测试。")
            kc_id = question.kc_id if question.kc_id in art.kc_index else None
        point = art.kc_index.get(kc_id or "")
        if kc_id and point is None:
            raise TurnInvalidError("指定的知识点不存在。")

        expected = question.source_pages if question and question.source_pages else point.pages if point else []
        allowed = {page for page in expected if art.start_page <= page <= art.end_page}
        if not allowed and not (question or point):
            allowed = set(range(art.start_page, art.end_page + 1))
        requested = list(dict.fromkeys(page for page in data.material_source_pages if page in allowed))
        return WorkbenchSource(
            artifact_id=art.id, kc_id=kc_id, question_id=question.id if question else None,
            label=point.label if point else ("章节测试题" if question else art.title),
            pages=(requested or sorted(allowed))[:40], view=data.material_workbench_view,
        )

    def _prepare_new_turn(
        self,
        conversation_id: str,
        data: TurnInput,
    ) -> PreparedTurn:
        normalized = self._normalize(data)
        normalized = replace(normalized, source=self._resolve_source(normalized))
        canonical = self._canonical(normalized)

        existing = self._repository.find_turn(
            conversation_id, normalized.request_id
        )
        if existing is not None:
            if not self._same_input(existing, canonical):
                raise ConversationConflictError(
                    "同一个 request_id 不能提交不同的输入或附件。"
                )
            if existing.status == "running":
                raise ConversationConflictError(
                    "该请求仍在执行，请稍后查看会话记录。"
                )
            if existing.status == "interrupted":
                raise ConversationConflictError(
                    "该请求已中断，请使用新的 request_id 重新提交。"
                )
            return ReplayTurn(conversation_id=conversation_id, turn=existing)

        if normalized.material_workbench_view is not None:
            normalized = replace(normalized, workbench_source=self._resolve_workbench_source(conversation_id, normalized))

        if self._repository.has_active_lease():
            raise TurnInProgressError(
                "你已有一轮正在处理，请等待完成或先停止。"
            )

        self._precheck_quota(normalized)

        if self._repository.has_running_turn(conversation_id):
            raise ConversationConflictError(
                "该会话已有一轮正在执行，请等待完成或先停止。"
            )

        active = self._repository.get_active_problem(conversation_id)

        # F004 旧客户端只发送 request_id + query：按查询处理，不额外调用意图模型。
        if (
            normalized.kind is None
            and not normalized.dispatch
            and not normalized.attachments
            and not normalized.question_action
        ):
            legacy = replace(normalized, kind="query")
            return self._prepare_explicit(
                conversation_id, legacy, query_input(legacy.text), active
            )

        if normalized.kind is None:
            return self._prepare_dispatched(conversation_id, normalized, canonical, active)

        return self._prepare_explicit(
            conversation_id, normalized, canonical, active
        )

    def _prepare_retry(self, conversation_id: str, request: TurnInput) -> PreparedTurn:
        canonical = canonical_json({"retry_of": request.retry_of})
        existing = self._repository.find_turn(conversation_id, request.request_id)
        if existing:
            if not self._same_input(existing, canonical) or existing.status in ("running", "interrupted"):
                raise ConversationConflictError("重试请求仍在执行、已中断或内容发生冲突，请刷新记录。")
            return ReplayTurn(conversation_id, existing)
        if self._repository.has_active_lease():
            raise TurnInProgressError(
                "你已有一轮正在处理，请等待完成或先停止。"
            )
        origin = self._repository.find_turn(conversation_id, request.retry_of or "")
        if origin is None:
            raise ProblemNotFoundError("原轮次不属于当前会话。")
        if not origin.retryable or origin.status == "running":
            raise ConversationConflictError(origin.retry_hint or "这一轮不能直接重试，请按提示继续。")
        for turn in self._repository.list_turns(conversation_id):
            if turn.input_json and json.loads(turn.input_json).get("retry_of") == origin.request_id:
                raise ConversationConflictError("这一步已经重试过，请查看最新重试轮次的结果。")
        if self._repository.has_running_turn(conversation_id):
            raise ConversationConflictError("当前会话已有请求正在处理，请稍候。")
        payload = origin.recovery.get("input") or json.loads(origin.input_json or "{}")
        if not payload or "retry_of" in payload:
            raise ConversationConflictError("原轮次缺少完整输入，请确认已有结果后重新提出需求。")
        recovered = TurnInput(
            request_id=request.request_id, kind=payload.get("kind"), text=payload.get("text", origin.query),
            attachments=tuple((it["id"], it["role"]) for it in payload.get("attachments", [])),
            student_answer_text=payload.get("student_answer"), solution_text=payload.get("solution_text"),
            source=Source.model_validate(payload.get("source") or {}), problem_id=payload.get("problem_id"),
            exercise_id=payload.get("exercise_id") or origin.exercise_id,
            start_new=payload.get("start_new", False), dispatch=payload.get("dispatch", False),
            retry_of=request.retry_of, attempt_kind=payload.get("attempt_kind"),
            question_id=payload.get("question_id"),
            question_source_turn=payload.get("question_source_turn"),
            question_action=payload.get("question_action"),
            material_guide_id=payload.get("material_guide_id"),
            material_item_id=payload.get("material_item_id"),
            material_study_mode=payload.get("material_study_mode"),
            material_artifact_id=payload.get("material_artifact_id"),
            material_question_id=payload.get("material_question_id"),
            material_kc_id=payload.get("material_kc_id"),
            material_workbench_view=payload.get("material_workbench_view"),
            material_source_pages=tuple(payload.get("material_source_pages") or []),
        )
        if recovered.material_workbench_view:
            recovered = replace(recovered, workbench_source=self._resolve_workbench_source(conversation_id, recovered))
        self._precheck_quota(recovered)
        if _is_dynamic_recovery(origin.recovery):
            turn = self._repository.start_turn(conversation_id, request.request_id, origin.query,
                                             kind="dynamic", input_json=canonical,
                                             attachments=recovered.attachments,
                                             lease_token=self._current_lease_token,
                                             workbench_source=recovered.workbench_source)
            recovery = json.loads(json.dumps(origin.recovery))
            self._repository.save_recovery(conversation_id, request.request_id, recovery)
            return ExecuteTurn(conversation_id, request.request_id, turn, recovered, "dispatch", recovery=recovery)
        active = self._repository.get_active_problem(conversation_id)
        prepared = (self._prepare_dispatched(conversation_id, recovered, canonical, active)
                    if recovered.kind is None else self._prepare_explicit(conversation_id, recovered, canonical, active))
        if isinstance(prepared, ExecuteTurn):
            self._repository.save_recovery(conversation_id, request.request_id, {"input": payload})
        return prepared

    def _prepare_dispatched(
        self,
        conversation_id: str,
        data: TurnInput,
        canonical: str,
        active: ProblemRecord | None,
    ) -> ExecuteTurn:
        if data.question_action is not None:
            if not self.questions.ready:
                raise QuestionBankUnavailableError("题目库暂时不可用，请稍后重试。")
            if not data.question_id or not data.question_source_turn:
                raise TurnInvalidError("真题动作必须指定题目和查题轮次。")
            card = self._resolve_question_card(
                conversation_id, data.question_source_turn, data.question_id
            )
            selection = self._select_question_card(
                conversation_id, data.request_id, data.question_source_turn, card
            )
            problem = (
                self._repository.get_problem(selection.problem_id)
                if selection.problem_id
                else None
            )
            if problem is not None and problem.conversation_id != conversation_id:
                problem = None
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text or ("生成变式" if data.question_action == "exercise" else "保存错题"),
                kind="dynamic",
                input_json=canonical,
                lease_token=self._current_lease_token,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation=(
                    "question_exercise"
                    if data.question_action == "exercise"
                    else "question_record"
                ),
                problem=problem,
            )
        if data.attachments:
            if active is not None and self._make_scheduler is not None:
                # 动态调度自行判断新题/补充/纠正；不再因为已有题目而
                # 强制拦截附件，否则澄清后的图片补充无法接续同一目标。
                self._validate_attachments(conversation_id, data, "supplement")
                turn = self._repository.start_turn(
                    conversation_id,
                    data.request_id,
                    data.text,
                    kind="query",
                    input_json=canonical,
                    attachments=data.attachments,
                    lease_token=self._current_lease_token,
                )
                return ExecuteTurn(
                    conversation_id=conversation_id,
                    request_id=data.request_id,
                    turn=turn,
                    input=data,
                    operation="dispatch",
                    problem=active,
                )
            if active is not None:
                self._validate_attachments(conversation_id, data, "supplement")
                turn = self._repository.start_turn(
                    conversation_id,
                    data.request_id,
                    data.text,
                    kind="problem",
                    input_json=canonical,
                    attachments=data.attachments,
                    lease_token=self._current_lease_token,
                )
                return ExecuteTurn(
                    conversation_id=conversation_id,
                    request_id=data.request_id,
                    turn=turn,
                    input=data,
                    operation="clarify",
                    problem=active,
                    clarify_options=("supplement", "correction", "new_problem"),
                    clarify_message=(
                        "当前已有正在辅导的题目。请明确选择：补充当前题、"
                        "纠正题干，还是开始新题；不要直接混用附件。"
                    ),
                )
            self._validate_attachments(conversation_id, data, "problem")
            self._validate_source(active, data, "problem")
            self._require_tutor()
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="problem",
                input_json=canonical,
                attachments=data.attachments,
                lease_token=self._current_lease_token,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="problem",
                problem=None,
            )

        if self._make_router is None and self._make_scheduler is None:
            raise ConversationQueryUnavailableError(
                self._query_error
                or self._tutoring_error
                or "会话所需的模型尚未配置。"
            )

        turn = self._repository.start_turn(
            conversation_id,
            data.request_id,
            data.text,
            kind="query",
            input_json=canonical,
            lease_token=self._current_lease_token,
            workbench_source=data.workbench_source,
        )
        return ExecuteTurn(
            conversation_id=conversation_id,
            request_id=data.request_id,
            turn=turn,
            input=data,
            operation="dispatch",
            problem=active,
        )

    async def stream_prepared(
        self,
        prepared: PreparedTurn,
        request: Request,
    ) -> AsyncIterator[TurnEvent]:
        """把已准备好的轮次转成 SSE 事件；最终事件发出前必须已落库。"""

        if isinstance(prepared, ReplayTurn):
            yield _replay_event(prepared)
            return

        state = _RunState()
        token = self._lease_by_request.pop(prepared.request_id, None)
        owner = self._repository.owner_id
        if token is not None and self._lease_registry is not None:
            self._lease_registry.mark_started(token, asyncio.current_task())
        actor_context = (
            bind_actor(
                owner,
                request_id=prepared.request_id,
                conversation_id=prepared.conversation_id,
            )
            if owner is not None
            else nullcontext()
        )
        try:
            with actor_context:
                if prepared.operation == "clarify":
                    async for event in self._clarify_events(
                        prepared,
                        state,
                        options=prepared.clarify_options or ("query", "tutoring"),
                        message=(
                            prepared.clarify_message
                            or "请明确要查试卷，还是要辅导一道题。"
                        ),
                    ):
                        yield event
                    return

                if prepared.operation == "dispatch":
                    async for event in self._dispatch_events(prepared, request, state):
                        yield event
                    return

                if prepared.operation == "query":
                    async for event in self._query_events(prepared, request, state):
                        yield event
                    return

                if prepared.operation in ("problem", "supplement", "correction"):
                    async for event in self._tutoring_events(
                        prepared, request, state
                    ):
                        yield event
                    return

                if prepared.operation in _QUESTION_KINDS:
                    async for event in self._question_events(prepared, state):
                        yield event
                    return

                if prepared.operation in ("question_exercise", "question_record"):
                    async for event in self._question_action_events(prepared, state):
                        yield event
                    return

                if prepared.operation == "followup":
                    async for event in self._followup_events(prepared, state):
                        yield event
                    return

                if prepared.operation == "record":
                    async for event in self._record_events(prepared, state):
                        yield event
                    return

                if prepared.operation == "reveal_solution":
                    async for event in self._reveal_solution_events(prepared, state):
                        yield event
                    return

                if prepared.operation == "exercise_answer":
                    async for event in self._exercise_answer_events(prepared, state):
                        yield event
                    return

                if prepared.operation == "feedback":
                    async for event in self._feedback_events(prepared, state):
                        yield event
                    return
        finally:
            if token is not None:
                self._release_lease(token)
            if not state.saved:
                self._interrupt_quietly(prepared)

    def _require_tutor(self) -> None:
        if self._make_tutor is None:
            raise TutoringUnavailableError(
                self._tutoring_error or "辅导模型尚未配置。"
            )

    def _require_active(
        self, active: ProblemRecord | None, problem_id: str | None
    ) -> ProblemRecord:
        if active is None:
            raise ProblemNotFoundError("当前没有正在辅导的题目。")
        if problem_id is not None and problem_id != active.id:
            raise ProblemNotFoundError("指定的题目不是当前活动题目。")
        return active

    def _validate_attachments(
        self,
        conversation_id: str,
        data: TurnInput,
        mode: str,
    ) -> None:
        attachments = list(data.attachments)
        if not attachments:
            if mode in ("problem", "correction") and not data.text.strip():
                raise TurnInvalidError("请提供题干文字或至少一张题干图片。")
            if mode == "supplement" and not data.text.strip():
                raise TurnInvalidError("请提供补充文字或图片。")
            return

        # 数量、重复、角色、归属、单张大小和总量统一由仓库校验。
        self._repository.validate_attachment_selection(
            conversation_id, attachments
        )
        roles = [role for _, role in attachments]

        if mode in ("problem", "correction") and not data.text.strip():
            if "statement" not in roles:
                raise TurnInvalidError("请提供题干文字或至少一张题干图片。")
        if mode == "supplement" and not data.text.strip() and not attachments:
            raise TurnInvalidError("请提供补充文字或图片。")

    def _resolve_source(self, data: TurnInput) -> Source:
        """把已登记 paper_id 解析成带标题的来源快照；未登记返回 422。"""

        source = data.source
        if source.type != "registered_paper":
            return source
        if self._papers is None:
            raise SourceInvalidError("试卷目录不可用，暂时不能校验来源。")
        paper = self._papers.get_paper(source.paper_id or "")
        if paper is None:
            raise SourceInvalidError("题目来源未登记，请检查试卷选择。")
        return Source(
            type="registered_paper",
            paper_id=paper.id,
            paper_title=paper.title,
        )

    def _validate_source(
        self,
        active: ProblemRecord | None,
        data: TurnInput,
        mode: str,
    ) -> None:
        if mode not in ("problem", "supplement", "correction"):
            if data.source.type != "none":
                raise TurnInvalidError("该轮次不能携带题目来源。")
            return
        source = data.source
        if source.type != "registered_paper":
            return
        if self._papers is None:
            raise SourceInvalidError("试卷目录不可用，暂时不能校验来源。")
        paper = self._papers.get_paper(source.paper_id or "")
        if paper is None:
            raise SourceInvalidError("题目来源未登记，请检查试卷选择。")

    def _selected_attachments(
        self, prepared: ExecuteTurn
    ) -> list[tuple[str, str]]:
        provided = list(prepared.input.attachments)
        if provided:
            return provided
        if (
            prepared.operation in ("supplement", "correction")
            and prepared.problem is not None
        ):
            return [
                (item.id, item.role or "statement")
                for item in prepared.problem.attachments
            ]
        return []

    def _attachments_by_ids(
        self,
        conversation_id: str,
        entries: list[tuple[str, str]],
    ) -> list[AttachmentRecord]:
        return self._repository.validate_attachment_selection(
            conversation_id, entries
        )

    def _tutoring_input_text(self, prepared: ExecuteTurn) -> str:
        """补充/纠正时把原题干一起交给理解模型，避免只看半句话。"""

        text = prepared.input.text.strip()
        if (
            prepared.problem is None
            or prepared.operation not in ("supplement", "correction")
        ):
            return text

        original = prepared.problem.statement_text.strip()
        action = "补充" if prepared.operation == "supplement" else "纠正"
        if text:
            return f"原题干：\n{original}\n\n学生{action}：\n{text}"
        return (
            f"原题干：\n{original}\n\n"
            f"学生只{action}了图片，请结合原题干和新图片整理题目。"
        )

    def _effective_problem_fields(
        self, prepared: ExecuteTurn
    ) -> tuple[Source, str | None, str | None]:
        source = prepared.input.source
        student_answer = prepared.input.student_answer_text
        solution_text = prepared.input.solution_text
        if prepared.problem is not None and prepared.operation in (
            "supplement",
            "correction",
        ):
            if source.type == "none":
                source = prepared.problem.source
            if student_answer is None:
                student_answer = prepared.problem.student_answer_text
            if solution_text is None:
                solution_text = prepared.problem.solution_text
        return source, student_answer, solution_text

    def _record_id_for(self, problem: ProblemRecord) -> str | None:
        record = self._repository.get_record_by_root(
            problem.conversation_id, problem.root_problem_id
        )
        return record.id if record is not None else None

    def _failure_event(
        self,
        prepared: ExecuteTurn,
        code: str,
        message: str,
        state: _RunState,
        *,
        quota: dict[str, object] | None = None,
        saved: bool = False,
    ) -> TurnErrorEvent:
        try:
            self._repository.fail_turn(
                prepared.conversation_id,
                prepared.request_id,
                code,
                message,
            )
        except ConversationStorageError:
            return _storage_error_event(prepared)
        state.saved = True
        return TurnErrorEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            code=code,  # type: ignore[arg-type]
            message=message,
            quota=quota,  # type: ignore[arg-type]
            saved=saved,
        )

    def _interrupt_quietly(self, prepared: ExecuteTurn) -> None:
        """断流或保存失败时尽力标记 interrupted；失败留给启动恢复。"""

        try:
            self._repository.interrupt_turn(
                prepared.conversation_id,
                prepared.request_id,
            )
        except ConversationStorageError:
            logger.warning(
                "无法把中断的轮次写入数据库，将在下次启动时恢复。",
                exc_info=True,
            )
