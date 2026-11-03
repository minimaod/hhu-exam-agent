"""练习处理器：生成、作答、补充、订正、评价、查看解答与错题写入。"""

import json
import logging
import re
import uuid
from collections.abc import (
    AsyncIterator,
    Callable,
)
from contextlib import suppress
from typing import Any

from app.conversations.errors import (
    AttachmentInvalidError,
    ConversationConflictError,
    ConversationStorageError,
    InvalidImageError,
    MistakeRecordConflictError,
    ProblemNotFoundError,
    TurnInvalidError,
)
from app.conversations.graph import ActionOutcome
from app.conversations.models import (
    ActionResultItem,
    ExerciseAttemptView,
    ExerciseEvaluation,
    ExerciseView,
    RECOVERABLE_CODES,
    TurnEvent,
    TurnMessageDeltaEvent,
    TurnProgressEvent,
    TurnResultEvent,
    reply_state as _reply_state,
)
from app.conversations.reply import material_to_payload
from app.conversations.repository import (
    AttachmentRecord,
    ExerciseAttemptRecord,
    ExerciseRecord,
)
from app.conversations.turn_types import (
    ExecuteTurn,
    TurnInput,
    _AttemptPlan,
    _DynamicContext,
    _RunState,
    _storage_error_event,
)
from app.querying.errors import QueryError
from app.questions.cards import compose_statement
from app.tutoring.exercise_dedup import (
    MAX_GENERATION_ATTEMPTS,
    MAX_RECENT_EXERCISES,
    is_duplicate_statement,
)
from app.tutoring.exercise_llm import (
    AnswerEvaluation,
    GeneratedExercise,
)
from app.tutoring.models import (
    AttachmentView,
    RecordCreatedResult,
    RecordExistsResult,
)
from app.tutoring.service import load_images

logger = logging.getLogger("app.conversations.service")

_RETRY_TEXT_MARKERS = ("重试", "重新评价", "再评价", "再试")

_RETRY_TEXTS = {
    "重试",
    "重试评价",
    "重新评价",
    "重新评价一下",
    "再评价",
    "再评价一次",
    "再试",
    "再试一次",
    "继续评价",
    "评价一下",
    "请评价",
}

class PracticeMixin:
    """练习尝试、评价和答案展开。"""

    def _build_exercise_view(
        self, ex: ExerciseRecord, *, allow_solution: bool = False
    ) -> ExerciseView:
        evaluation = self._parse_evaluation(ex.evaluation_json)

        ref_sol = None
        if allow_solution or ex.status in ("submitted", "revealed") or ex.revealed_solution_at is not None:
            ref_sol = ex.reference_solution_text

        attempts = [
            ExerciseAttemptView(
                id=attempt.id,
                attempt_no=attempt.attempt_no,
                kind=attempt.kind,  # type: ignore[arg-type]
                student_answer_text=attempt.student_answer_text,
                student_solution_text=attempt.student_solution_text,
                answered_after_revealed=attempt.answered_after_revealed,
                evaluation=self._parse_evaluation(attempt.evaluation_json),
                request_id=attempt.request_id,
                attachments=self._attachment_views(
                    ex.conversation_id, attempt.attachments
                ),
                created_at=attempt.created_at,
                updated_at=attempt.updated_at,
            )
            for attempt in ex.attempts
        ]

        return ExerciseView(
            id=ex.id,
            conversation_id=ex.conversation_id,
            problem_id=ex.problem_id,
            root_problem_id=ex.root_problem_id,
            statement_text=ex.statement_text,
            knowledge_point=ex.knowledge_point,
            status=ex.status,  # type: ignore[arg-type]
            student_answer_text=ex.student_answer_text,
            student_solution_text=ex.student_solution_text,
            revealed_solution_at=ex.revealed_solution_at,
            answered_after_revealed=ex.answered_after_revealed,
            evaluation=evaluation,
            reference_solution_text=ref_sol,
            source_question=self._repository.get_exercise_question_source(ex.id),
            attachments=self._attachment_views(ex.conversation_id, ex.attachments),
            attempts=attempts,
            created_at=ex.created_at,
            updated_at=ex.updated_at,
        )

    @staticmethod
    def _parse_evaluation(raw: str | None) -> ExerciseEvaluation | None:
        if not raw:
            return None
        with suppress(Exception):
            return ExerciseEvaluation.model_validate(json.loads(raw))
        return None

    @staticmethod
    def _attachment_views(
        conversation_id: str, attachments: list[AttachmentRecord]
    ) -> list[AttachmentView]:
        views: list[AttachmentView] = []
        for att in attachments:
            role = (
                att.role
                if att.role in ("student_solution", "student_answer")
                else "student_solution"
            )
            views.append(
                AttachmentView(
                    id=att.id,
                    role=role,  # type: ignore[arg-type]
                    mime=att.mime,
                    width=att.width,
                    height=att.height,
                    url=f"/api/conversations/{conversation_id}/attachments/{att.id}",
                )
            )
        return views

    @staticmethod
    def _enforce_independence(
        evaluation: AnswerEvaluation, attempt: ExerciseAttemptRecord
    ) -> AnswerEvaluation:
        """程序兜底：查看解答后的作答不得被表述为独立掌握。"""

        if not attempt.answered_after_revealed:
            if evaluation.independence_assessment is None:
                return evaluation.model_copy(
                    update={
                        "independence_assessment": (
                            "independent"
                            if evaluation.is_correct is True
                            else "unclear"
                        )
                    }
                )
            return evaluation
        note = (evaluation.independence_note or "").strip()
        if "参考解答" not in note and "对照" not in note:
            note = (
                "本次作答前参考解答已经展示，属于对照作答；答案正确不代表独立掌握，"
                "建议遮住解答后完整重做一遍。"
            )
        return evaluation.model_copy(
            update={
                "independence_assessment": "after_reveal",
                "independence_note": note[:800],
            }
        )

    def _plan_exercise_attempt(
        self, ex: ExerciseRecord, request: TurnInput
    ) -> _AttemptPlan:
        """把一次提交归类为复用、首次、补充或订正，不修改任何旧尝试。"""

        latest = ex.current_attempt
        explicit_answer = (request.student_answer_text or "").strip() or None
        explicit_solution = (request.solution_text or "").strip() or None
        text = request.text.strip()
        has_material = bool(explicit_answer or explicit_solution or request.attachments)
        retry_requested = bool(request.retry_of) or (
            not has_material and bool(text) and self._is_retry_text(text)
        )
        revealed = (
            ex.revealed_solution_at is not None
            or ex.status in ("submitted", "revealed")
        )
        if (
            request.kind is None
            and not explicit_answer
            and not explicit_solution
            and not request.attachments
            and text
            and not self._looks_like_exercise_text(text)
            and not self._is_retry_text(text)
        ):
            if latest is not None and latest.evaluation_json is not None:
                return _AttemptPlan(
                    kind="correction",
                    answer=None,
                    solution=None,
                    attachments=(),
                    answered_after_revealed=False,
                    error="这道练习题已经完成评价；如需订正，请写出新的答案或过程，或生成新的类似题。",
                )
            return _AttemptPlan(
                kind="initial",
                answer=None,
                solution=None,
                attachments=(),
                answered_after_revealed=False,
                error="请先写出最终答案或上传解题过程图片，我再进行评价。",
            )

        if request.attempt_kind and request.retry_of:
            return _AttemptPlan(
                kind="initial",
                answer=None,
                solution=None,
                attachments=(),
                answered_after_revealed=False,
                error="重试评价不需要再指定补充或订正。",
            )

        if retry_requested:
            target = None
            if request.retry_of:
                target = self._repository.get_attempt_by_request(
                    ex.id, request.retry_of
                )
                if target is None:
                    # 兼容 B 批之前创建的、没有 request_id 的故障轮次。
                    if latest is not None and latest.evaluation_json is None:
                        target = latest
                    else:
                        return _AttemptPlan(
                            kind="initial",
                            answer=None,
                            solution=None,
                            attachments=(),
                            answered_after_revealed=False,
                            error="找不到这次的作答记录，请刷新后重新提交。",
                        )
                elif target.evaluation_json is not None:
                    return _AttemptPlan(
                        kind="initial",
                        answer=None,
                        solution=None,
                        attachments=(),
                        answered_after_revealed=False,
                        error="这次作答已经完成评价，请刷新查看结果，不要重复提交。",
                    )
            else:
                if latest is None:
                    return _AttemptPlan(
                        kind="initial",
                        answer=None,
                        solution=None,
                        attachments=(),
                        answered_after_revealed=False,
                        error="请先写出最终答案或上传解题过程，我再进行评价。",
                    )
                if latest.evaluation_json is not None:
                    return _AttemptPlan(
                        kind="initial",
                        answer=None,
                        solution=None,
                        attachments=(),
                        answered_after_revealed=False,
                        error=(
                            "这道练习题已经完成评价；如需订正，请写出新的答案或过程。"
                        ),
                    )
                target = latest
            return _AttemptPlan(
                kind=target.kind,
                answer=target.student_answer_text,
                solution=target.student_solution_text,
                attachments=(),
                answered_after_revealed=target.answered_after_revealed,
                reuse=True,
                attempt=target,
            )

        kind_hint = request.attempt_kind
        if kind_hint == "supplement":
            if latest is None or not self._attempt_needs_supplement(latest):
                return _AttemptPlan(
                    kind="supplement",
                    answer=None,
                    solution=None,
                    attachments=(),
                    answered_after_revealed=False,
                    error="当前作答不需要补充；可直接提交订正答案，或生成新的类似题。",
                )
            # 独立作答表单把答案放在 query 里；对话里的“补充：……”只并入过程。
            answer = explicit_answer or (
                text if request.kind in ("exercise", "exercise_answer") else None
            )
            return _AttemptPlan(
                kind="supplement",
                answer=answer or latest.student_answer_text,
                solution=self._merge_supplement(
                    latest.student_solution_text,
                    explicit_solution or text,
                ),
                attachments=tuple(request.attachments),
                answered_after_revealed=revealed,
            )

        if kind_hint == "correction":
            if latest is None:
                return _AttemptPlan(
                    kind="correction",
                    answer=None,
                    solution=None,
                    attachments=(),
                    answered_after_revealed=False,
                    error="请先提交首次作答，再进行订正。",
                )
            if self._attempt_needs_supplement(latest):
                return _AttemptPlan(
                    kind="correction",
                    answer=None,
                    solution=None,
                    attachments=(),
                    answered_after_revealed=False,
                    error="这次作答还需要补充过程，请先补充并重新评价。",
                )
            return _AttemptPlan(
                kind="correction",
                answer=explicit_answer or text or None,
                solution=explicit_solution or None,
                attachments=tuple(request.attachments),
                answered_after_revealed=revealed,
            )

        if latest is None:
            return _AttemptPlan(
                kind="initial",
                answer=explicit_answer or text or None,
                solution=explicit_solution or None,
                attachments=tuple(request.attachments),
                answered_after_revealed=revealed,
            )

        if latest.evaluation_json is None:
            same_content = (
                (latest.student_answer_text or "") == (explicit_answer or "")
                and (latest.student_solution_text or "") == (explicit_solution or "")
                and not request.attachments
            )
            if same_content:
                return _AttemptPlan(
                    kind=latest.kind,
                    answer=latest.student_answer_text,
                    solution=latest.student_solution_text,
                    attachments=(),
                    answered_after_revealed=latest.answered_after_revealed,
                    reuse=True,
                    attempt=latest,
                )
            return _AttemptPlan(
                kind="correction",
                answer=explicit_answer or text or None,
                solution=explicit_solution or None,
                attachments=tuple(request.attachments),
                answered_after_revealed=revealed,
            )

        if self._attempt_needs_supplement(latest):
            answer = explicit_answer or (
                text if request.kind in ("exercise", "exercise_answer") else None
            )
            return _AttemptPlan(
                kind="supplement",
                answer=answer or latest.student_answer_text,
                solution=self._merge_supplement(
                    latest.student_solution_text,
                    explicit_solution or text,
                ),
                attachments=tuple(request.attachments),
                answered_after_revealed=revealed,
            )

        new_answer = explicit_answer or text or None
        new_solution = explicit_solution or None
        if (
            latest.evaluation_json is not None
            and (latest.student_answer_text or "") == (new_answer or "")
            and (latest.student_solution_text or "") == (new_solution or "")
            and not request.attachments
        ):
            return _AttemptPlan(
                kind="correction",
                answer=None,
                solution=None,
                attachments=(),
                answered_after_revealed=False,
                error="这次作答已经完成评价且内容没有变化；如需订正，请修改答案或过程。",
            )
        return _AttemptPlan(
            kind="correction",
            answer=new_answer,
            solution=new_solution,
            attachments=tuple(request.attachments),
            answered_after_revealed=revealed,
        )

    @staticmethod
    def _is_retry_text(text: str) -> bool:
        normalized = "".join(text.split()).strip("。.!！?？")
        if not normalized:
            return False
        if normalized in _RETRY_TEXTS:
            return True
        return any(marker in normalized for marker in _RETRY_TEXT_MARKERS)

    @staticmethod
    def _merge_supplement(prior: str | None, new: str) -> str | None:
        """把补充说明接到原步骤后；用户提交完整过程时不重复拼接。"""

        base = (prior or "").strip()
        addition = (new or "").strip()
        if not addition:
            return base or None
        if not base or addition in base:
            return base or addition
        if base in addition:
            return addition
        return f"{base}\n\n补充：\n{addition}"

    @staticmethod
    def _looks_like_exercise_text(text: str) -> bool:
        """区分“这是新答案/过程”和“再讲一次/再来一道”这类请求。"""

        normalized = "".join(text.split())
        if not normalized:
            return False
        if len(normalized) >= 30:
            return True
        if any(ch.isdigit() for ch in normalized):
            return True
        markers = (
            "答案",
            "选",
            "算",
            "求",
            "得",
            "等于",
            "因为",
            "所以",
            "步骤",
            "证明",
            "代入",
            "公式",
            "定理",
            "导数",
            "积分",
            "极限",
            "函数",
            "=",
        )
        return any(marker in normalized for marker in markers)

    @staticmethod
    def _attempt_needs_supplement(attempt: Any) -> bool:
        if attempt is None or not attempt.evaluation_json:
            return False
        with suppress(Exception):
            evaluation = ExerciseEvaluation.model_validate(
                json.loads(attempt.evaluation_json)
            )
            return bool(evaluation.needs_supplement)
        return False

    async def _dynamic_generate_exercise(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "generate_exercise"
        active = self._question_operation_problem(ctx)
        if active is None or active.status != "explained" or active.explanation is None:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "需要先有一道完成讲解的题目，才能生成类似练习。请先上传或粘贴题目。",
                needs_user=True,
            ))
        if self._make_exercise_agent is None:
            return self._action_result(prepared, ctx, ActionOutcome(name, "failed", "练习生成模型尚未配置。"))

        emit("generating_exercise", "正在根据核心考点生成类似练习题")
        from_question = (
            ctx.question_selection is not None
            and ctx.question_card is not None
            and ctx.question_selection.problem_id == active.id
        )
        original_statement = (
            compose_statement(ctx.question_card)
            if from_question and ctx.question_card is not None
            else active.statement_text
        )
        knowledge_points = ", ".join(
            point.name for point in active.explanation.knowledge_points
        )
        steps_summary = "; ".join(
            f"步骤{step.index}:{step.title}" for step in active.explanation.steps
        )
        agent = self._make_exercise_agent()
        recent = self._repository.list_recent_exercise_statements(
            prepared.conversation_id, limit=MAX_RECENT_EXERCISES
        )
        avoid: list[str] = []
        generated: GeneratedExercise | None = None
        attempts_used = 0
        for _ in range(MAX_GENERATION_ATTEMPTS):
            attempts_used += 1
            candidate = await agent.generate_exercise(
                original_statement=original_statement,
                knowledge_points=knowledge_points,
                steps_summary=steps_summary,
                course=ctx.confirmed_course,
                recent_statements=recent,
                avoid_statements=avoid or None,
            )
            if not is_duplicate_statement(
                candidate.statement,
                recent,
                original=original_statement,
            ):
                generated = candidate
                break
            avoid.append(candidate.statement)
            logger.info("类似题生成与近期题目重复，触发一次有界重生成。")
        if generated is None:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "failed",
                "这次没能生成与近期题目明显不同的新题，未保存重复练习；请稍后重试或换一个知识点。",
                {"reason": "duplicate_exercise", "generation_attempts": attempts_used},
                needs_user=True,
                error_code="MODEL_RESPONSE_INVALID",
                retryable=True,
            ))
        with self._repository.action_write():
            exercise = self._repository.create_exercise(
                id=str(uuid.uuid4()),
                conversation_id=prepared.conversation_id,
                problem_id=active.id,
                root_problem_id=active.root_problem_id,
                statement_text=generated.statement,
                reference_solution_text=generated.reference_solution,
                knowledge_point=generated.knowledge_point,
            )
            ctx.active_exercise = exercise
            ctx.exercise_id = exercise.id
            if from_question and ctx.question_card is not None:
                self._repository.save_exercise_question_source(
                    exercise_id=exercise.id,
                    card=ctx.question_card,
                    build_id=ctx.question_card.build_id,
                    source_version=self.questions.source_version or "",
                )
            ctx.exercise_view = self._build_exercise_view(exercise)
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "success",
                "已根据真题生成针对性练习题（待作答）"
                if from_question
                else "已生成针对性练习题（待作答）",
                {
                    "exercise_id": exercise.id,
                    "knowledge_point": generated.knowledge_point,
                    "from_question": from_question,
                },
                needs_user=True,
            ))

    async def _dynamic_evaluate_exercise(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "evaluate_exercise"
        exercise = ctx.active_exercise
        if exercise is None:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "当前没有可以作答的练习题，请先让我生成一道类似题。",
                needs_user=True,
            ))
        plan = self._plan_exercise_attempt(exercise, prepared.input)
        if plan.error:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "failed",
                plan.error,
                needs_user=True,
                error_code="BUSINESS_REJECTED",
            ))
        if not plan.reuse and not (
            (plan.answer or "").strip()
            or (plan.solution or "").strip()
            or plan.attachments
        ):
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "请先写出最终答案或上传解题过程图片，我再进行评价。",
                needs_user=True,
            ))

        emit("evaluating_exercise", "正在评价您的作答与推导过程")
        saved, attempt = self._save_exercise_attempt(prepared, exercise, plan)
        images = load_images(
            attempt.attachments,
            root=self._attachments_root,
            roles={
                "statement",
                "solution",
                "supplement",
                "student_answer",
                "student_solution",
            },
        )
        ctx.active_exercise = saved
        ctx.exercise_id = saved.id
        ctx.exercise_view = self._build_exercise_view(saved)

        if self._make_exercise_agent is None:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "failed",
                "作答已保存，但练习评价模型尚未配置，请先配置评价模型。",
                {"exercise_id": saved.id, "answer_saved": True},
                needs_user=True,
            ))

        agent = self._make_exercise_agent()
        try:
            evaluation = await agent.evaluate_answer(
                statement=exercise.statement_text,
                reference_solution=exercise.reference_solution_text,
                student_answer=attempt.student_answer_text,
                student_solution_text=attempt.student_solution_text,
                images=images,
                answered_after_revealed=attempt.answered_after_revealed,
                attempt_kind=attempt.kind,
            )
        except Exception as exc:
            logger.warning("作答评价失败：%s", exc)
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "failed",
                "作答已保存，但评价没有完成，请重试。",
                {"exercise_id": saved.id, "answer_saved": True},
                needs_user=True, error_code=exc.code if isinstance(exc, QueryError) else "TUTORING_FAILED",
                retryable=not isinstance(exc, QueryError) or exc.code in RECOVERABLE_CODES,
            ))

        effective = self._enforce_independence(evaluation, attempt)
        evaluation_json = json.dumps(
            effective.model_dump(mode="json"), ensure_ascii=False
        )
        with self._repository.action_write():
            try:
                evaluated = self._repository.save_attempt_evaluation(
                    attempt.id, evaluation_json=evaluation_json
                )
            except ConversationConflictError:
                current = self._repository.get_exercise(saved.id)
                same_attempt = (
                    current.current_attempt
                    if current is not None
                    else None
                )
                if (
                    same_attempt is None
                    or same_attempt.id != attempt.id
                    or same_attempt.evaluation_json is None
                ):
                    return self._action_result(prepared, ctx, ActionOutcome(
                        name,
                        "failed",
                        "评价结果没有保存成功，请刷新后重试。",
                        {"exercise_id": saved.id, "answer_saved": True},
                        needs_user=True,
                    ))
                evaluated = current

            evaluation_view = self._build_evaluation_view(evaluated)
            ctx.active_exercise = evaluated
            ctx.exercise_id = evaluated.id
            ctx.exercise_view = self._build_exercise_view(evaluated)
            ctx.evaluation_view = evaluation_view
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "success",
                "作答已完成评价",
                {
                    "exercise_id": evaluated.id,
                    "is_correct": (
                        evaluation_view.is_correct if evaluation_view is not None else None
                    ),
                    "answered_after_revealed": attempt.answered_after_revealed,
                    "solution_image_count": len(images),
                },
            ))

    def _dynamic_reveal(self, ctx: _DynamicContext) -> ActionOutcome:
        name = "reveal_solution"
        exercise = ctx.active_exercise
        if exercise is None:
            return ActionOutcome(
                name,
                "clarification",
                "当前没有可以查看解答的练习题。",
                needs_user=True,
            )
        try:
            revealed = self._repository.reveal_exercise_solution(
                exercise.id, conversation_id=ctx.conversation_id
            )
        except ProblemNotFoundError as exc:
            return ActionOutcome(name, "failed", str(exc), needs_user=True)
        ctx.active_exercise = revealed
        ctx.exercise_id = revealed.id
        ctx.exercise_view = self._build_exercise_view(
            revealed, allow_solution=True
        )
        return ActionOutcome(
            name,
            "success",
            "已为您展示参考解答",
            {"exercise_id": revealed.id},
            needs_user=True,
        )

    def _dynamic_record_mistake(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
        user_feedback: str | None = None,
    ) -> ActionOutcome:
        name = "record_mistake"
        active = self._question_operation_problem(ctx)
        if active is None or active.status != "explained" or active.explanation is None:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "当前还没有完成讲解的题目，暂时不能保存到错题本。",
                needs_user=True,
            ))
        if self._records is None:
            return self._action_result(prepared, ctx, ActionOutcome(name, "failed", "错题记录服务不可用。"))

        emit("recording", "正在将本题收录至错题本")
        explicit_note = re.search(r"备注\s*[:：]\s*(.+)", prepared.input.text, re.DOTALL)
        feedback = (explicit_note.group(1) if explicit_note else user_feedback or "").strip() or None
        if feedback and len(feedback) > 2000:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "clarification", "备注超过 2000 字，请缩短后再保存。", needs_user=True))
        try:
            detail, created, needs_update = self._records.create_from_problem(
                request_id=prepared.request_id,
                conversation_id=prepared.conversation_id,
                problem_id=active.id,
                feedback_text=feedback,
            )
        except MistakeRecordConflictError:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "success", "该题目已在错题本中。", {"problem_id": active.id}
            ))
        if not created and feedback is not None:
            detail = self._records.update_metadata(
                detail.id, title=None, source=None, feedback_text=feedback,
                self_reported_understood=None,
            )
        record_row = self._repository.get_record(detail.id)
        if record_row is None:
            return self._action_result(prepared, ctx, ActionOutcome(name, "failed", "保存结果读取失败，请稍后重试。"))
        summary = self._records.summary_for(record_row)
        if created:
            if (
                ctx.question_selection is not None
                and ctx.question_card is not None
                and ctx.question_selection.problem_id == active.id
            ):
                self._repository.save_record_question_source(
                    record_id=detail.id,
                    card=ctx.question_card,
                    build_id=ctx.question_card.build_id,
                    source_version=self.questions.source_version or "",
                )
            ctx.tutoring = RecordCreatedResult(record=summary)
            message = f"已成功加入错题本：{detail.title}"
        else:
            ctx.tutoring = RecordExistsResult(
                record=summary, message="这道题已经记录过了。", needs_update=needs_update,
            )
            message = f"错题本中已有本题：{detail.title}"
        ctx.problem_id = active.id
        feedback_saved = feedback is not None and detail.feedback.text == feedback
        if feedback_saved:
            message += "；备注已保存。"
        return self._action_result(prepared, ctx, ActionOutcome(
            name, "success", message, {"record_id": detail.id,
                "feedback_saved": feedback_saved,
                **({"user_feedback": feedback} if feedback_saved else {})}
        ))

    def _build_evaluation_view(
        self, exercise: ExerciseRecord
    ) -> ExerciseEvaluation | None:
        if not exercise.evaluation_json:
            return None
        with suppress(Exception):
            return ExerciseEvaluation.model_validate(
                json.loads(exercise.evaluation_json)
            )
        return None

    async def _reveal_solution_events(
        self,
        prepared: ExecuteTurn,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        ex = prepared.exercise
        if ex is None:
            yield self._failure_event(prepared, "TUTORING_FAILED", "未找到当前练习题。", state)
            return
        revealed = self._repository.reveal_exercise_solution(
            ex.id, conversation_id=prepared.conversation_id
        )
        ex_view = self._build_exercise_view(revealed, allow_solution=True)
        try:
            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="reveal_solution",
                exercise=ex_view,
                assistant_message="已为您揭晓练习题参考解答。",
                problem_id=ex.problem_id,
                exercise_id=ex.id,
            )
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return
        state.saved = True
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind="reveal_solution",
            exercise=ex_view,
            assistant_message="已为您揭晓练习题参考解答。",
            exercise_id=ex.id,
        )

    async def _exercise_answer_events(
        self,
        prepared: ExecuteTurn,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        ex = prepared.exercise
        if ex is None:
            yield self._failure_event(
                prepared, "TUTORING_FAILED", "未找到当前练习题，无法评价。", state
            )
            return

        plan = self._plan_exercise_attempt(ex, prepared.input)
        if plan.error:
            yield self._failure_event(
                prepared, "BUSINESS_REJECTED", plan.error, state
            )
            return

        # 先保存本次尝试（短事务），再在事务外调用评价模型；模型失败时作答不丢。
        try:
            saved, attempt = self._save_exercise_attempt(prepared, ex, plan)
        except ConversationConflictError as exc:
            yield self._failure_event(
                prepared, "BUSINESS_REJECTED", str(exc), state
            )
            return
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return

        saved_view = self._build_exercise_view(saved)
        yield TurnProgressEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            stage="evaluating_exercise",
            message="作答已保存，正在评价您的答案与推导过程",
        )

        if self._make_exercise_agent is None:
            failure_results = [ActionResultItem(
                action="evaluate_exercise", status="failed", message="作答已保存，但评价模型尚未配置，请先配置评价模型。",
                details={"error_code": "MODEL_NOT_CONFIGURED", "retryable": False},
            )]
            try:
                self._repository.complete_dynamic_turn(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    kind="exercise",
                    exercise=saved_view,
                    composite_results=failure_results,
                    assistant_message="作答已保存，但评价模型尚未配置，请先配置评价模型。",
                    problem_id=ex.problem_id,
                    exercise_id=ex.id,
                )
            except ConversationStorageError:
                yield _storage_error_event(prepared)
                return
            state.saved = True
            yield TurnResultEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="exercise",
                exercise=saved_view,
                composite_results=failure_results,
                assistant_message="作答已保存，但评价模型尚未配置，请先配置评价模型。",
                exercise_id=ex.id,
            )
            return

        try:
            selected_images = load_images(
                attempt.attachments,
                root=self._attachments_root,
                roles={
                    "statement",
                    "solution",
                    "supplement",
                    "student_answer",
                    "student_solution",
                },
            )
        except (AttachmentInvalidError, InvalidImageError) as exc:
            yield self._failure_event(
                prepared, "INVALID_INPUT", str(exc), state
            )
            return

        ex_agent = self._make_exercise_agent()
        try:
            eval_res = await ex_agent.evaluate_answer(
                statement=ex.statement_text,
                reference_solution=ex.reference_solution_text,
                student_answer=attempt.student_answer_text,
                student_solution_text=attempt.student_solution_text,
                images=selected_images,
                answered_after_revealed=attempt.answered_after_revealed,
                attempt_kind=attempt.kind,
            )
        except Exception as exc:
            logger.warning("评价作答失败，作答已保存：%s", exc)
            saved_view = self._build_exercise_view(saved)
            code = exc.code if isinstance(exc, QueryError) else "TUTORING_FAILED"
            can_retry = code in RECOVERABLE_CODES
            message = "作答已保存，但评价没有完成，请重试。" if can_retry else "作答已保存，评价服务拒绝了请求，请先检查模型配置。"
            failure_results = [ActionResultItem(
                action="evaluate_exercise", status="failed", message=message,
                details={"error_code": code, "retryable": can_retry},
            )]
            try:
                self._repository.complete_dynamic_turn(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    kind="exercise",
                    exercise=saved_view,
                    composite_results=failure_results,
                    assistant_message=message,
                    problem_id=ex.problem_id,
                    exercise_id=ex.id,
                )
            except ConversationStorageError:
                yield _storage_error_event(prepared)
                return
            state.saved = True
            yield TurnResultEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="exercise",
                exercise=saved_view,
                composite_results=failure_results,
                assistant_message=message,
                exercise_id=ex.id,
            )
            return

        effective = self._enforce_independence(eval_res, attempt)
        try:
            ex_view, eval_view = self._complete_exercise_evaluation(
                prepared, saved, ex, attempt, effective
            )
        except ConversationConflictError:
            yield self._failure_event(prepared, "BUSINESS_REJECTED", "这份作答已被更新，请刷新查看最新评价。", state)
            return
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return

        ctx = _DynamicContext(
            conversation_id=prepared.conversation_id,
            exercise_view=ex_view,
            evaluation_view=eval_view,
        )
        composite = [ActionResultItem(
            action="evaluate_exercise", status="success", message="作答已完成评价",
            details={"exercise_id": ex.id, "solution_image_count": len(selected_images)},
        )]
        material = self._build_reply_material(prepared, ctx, composite, [], None)
        message = self._teaching_material(ctx) or "已完成作答评价。"
        payload = material_to_payload(material) if material is not None else None
        meta = {
            "mode": "fallback", "deltas": 0,
            "reason": "reply_pending" if self._make_reply else "reply_not_configured",
        }
        # 与聊天入口相同：评价已经落盘后，只重做正文材料；停止不会重评作答。
        self._repository.update_turn_reply(
            prepared.conversation_id, prepared.request_id,
            assistant_message=message, reply_meta=meta, reply_input=payload,
        )
        state.saved = True
        if material is not None and self._make_reply is not None:
            yield TurnProgressEvent(
                conversation_id=prepared.conversation_id, request_id=prepared.request_id,
                stage="composing_reply", message="正在组织作答评价",
            )
            meta = {}
            async for piece in self._reply_pieces(self._make_reply(), material, meta):
                yield TurnMessageDeltaEvent(
                    conversation_id=prepared.conversation_id, request_id=prepared.request_id,
                    seq=int(meta["deltas"]), delta=piece[:400],
                )
            text = str(meta.pop("text", "") or "")
            message = text or message
            self._repository.update_turn_reply(
                prepared.conversation_id, prepared.request_id,
                assistant_message=message, reply_meta=meta, reply_input=payload,
            )
        reply_status, reply_retryable = (
            _reply_state({"reply": meta, "reply_input": payload})
            if self._make_reply else (None, False)
        )
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind="exercise",
            exercise=ex_view,
            evaluation=eval_view,
            assistant_message=message,
            exercise_id=ex.id,
            reply_status=reply_status,
            reply_retryable=reply_retryable,
        )

    def _save_exercise_attempt(
        self, prepared: ExecuteTurn, ex: ExerciseRecord, plan: _AttemptPlan
    ) -> tuple[ExerciseRecord, ExerciseAttemptRecord]:
        """保存尝试或复用已有尝试；复用不写库、不覆盖原答案。"""

        if plan.reuse and plan.attempt is not None:
            return ex, plan.attempt

        mapped = [
            (
                attachment_id,
                "student_answer" if role == "statement" else "student_solution",
            )
            for attachment_id, role in plan.attachments
        ]
        with self._repository.action_write():
            saved = self._repository.create_exercise_attempt(
                ex.id,
                kind=plan.kind,
                student_answer_text=plan.answer,
                student_solution_text=plan.solution,
                attachments=mapped,
                answered_after_revealed=plan.answered_after_revealed,
                request_id=prepared.request_id,
            )
            attempt = saved.current_attempt
            if attempt is None:
                raise ConversationStorageError("作答尝试没有保存成功，请重试。")
            original = self._repository.find_turn(
                prepared.conversation_id, prepared.request_id
            )
            original_input = (
                original.recovery.get("input") if original else None
            ) or json.loads(self._canonical(prepared.input))
            self._repository.save_recovery(
                prepared.conversation_id,
                prepared.request_id,
                {"input": original_input, "exercise_id": ex.id},
            )
        return saved, attempt

    def _complete_exercise_evaluation(
        self,
        prepared: ExecuteTurn,
        saved: ExerciseRecord,
        ex: ExerciseRecord,
        attempt: ExerciseAttemptRecord,
        evaluation: AnswerEvaluation,
    ) -> tuple[ExerciseView, ExerciseEvaluation | None]:
        # 评价与轮次完成一起提交；失败则评价回滚，原作答仍可恢复。
        with self._repository.action_write():
            updated = self._repository.save_attempt_evaluation(
                attempt.id,
                evaluation_json=json.dumps(
                    evaluation.model_dump(mode="json"), ensure_ascii=False
                ),
            )
            view = self._build_exercise_view(updated)
            evaluation_view = self._build_evaluation_view(updated)
            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id, request_id=prepared.request_id, kind="exercise",
                exercise=view, evaluation=evaluation_view, assistant_message="已完成作答评价。",
                problem_id=ex.problem_id, exercise_id=ex.id,
            )
        return view, evaluation_view

    def _validate_attempt_kind(
        self, exercise: ExerciseRecord, attempt_kind: str
    ) -> None:
        latest = exercise.current_attempt
        if attempt_kind == "supplement":
            if latest is None or not self._attempt_needs_supplement(latest):
                raise ConversationConflictError(
                    "当前作答不需要补充；可直接提交订正答案，或生成新的类似题。"
                )
            return
        if attempt_kind == "correction":
            if latest is None:
                raise ConversationConflictError("请先提交首次作答，再进行订正。")
            if self._attempt_needs_supplement(latest):
                raise ConversationConflictError(
                    "这次作答还需要补充过程，请先补充并重新评价。"
                )
            return
        raise TurnInvalidError("未知的作答类型。")

    def _resolve_exercise_target(
        self, conversation_id: str, exercise_id: str | None
    ) -> ExerciseRecord:
        """显式指定的练习必须存在且属于本会话；只有未指定时才用活动练习。"""

        if exercise_id is not None:
            exercise = self._repository.get_exercise(exercise_id)
            if exercise is None:
                raise ProblemNotFoundError("指定的练习题不存在。")
            if exercise.conversation_id != conversation_id:
                raise ProblemNotFoundError("练习题不属于当前会话。")
            return exercise
        exercise = self._repository.get_active_exercise(conversation_id)
        if exercise is None:
            raise ProblemNotFoundError("当前没有可以操作的练习题。")
        return exercise
