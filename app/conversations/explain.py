"""讲解处理器：真题讲解、图文辅导、追问和目标解析。"""

import asyncio
from contextlib import suppress
import inspect
import logging
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from app import images
from app.conversations import answer_gate
from app.conversations.workspace import card_identity
from app.conversations.errors import (
    TurnInvalidError,
    TutoringUnavailableError,
)
from app.conversations.graph import (
    ActionOutcome,
    ActionPlan,
)
from app.conversations.repository import (
    NewProblem,
    ProblemRecord,
    UpdatedProblem,
)
from app.conversations.turn_types import (
    ExecuteTurn,
    _DynamicContext,
)
from app.questions.cards import (
    attach_answer,
    compose_statement,
)
from app.questions.errors import QuestionError
from app.questions.models import (
    QuestionCard,
    QuestionSelection,
)
from app.tutoring.models import (
    Explanation,
    FollowupResult,
    KnowledgePoint,
    Source,
    Step,
    TutoringResult,
    fit_knowledge_point_name,
)
from app.tutoring.service import (
    build_explanation_result,
    build_needs_clarification,
    load_images,
)

logger = logging.getLogger("app.conversations.service")

class ExplainMixin:
    """真题讲解、追问和讲解目标解析。"""

    async def _dynamic_explain(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "explain_problem"
        if self._make_tutor is None:
            return self._action_result(prepared, ctx, ActionOutcome(name, "failed", "辅导模型尚未配置，暂时不能讲解。"))

        real_selection, ambiguity = await self._resolve_real_question_target(
            prepared, ctx, action
        )
        if ambiguity:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "clarification", ambiguity, needs_user=True
            ))
        if real_selection is not None:
            raw_answer = action.parameters.get("student_answer")
            return await self._dynamic_explain_selection(
                prepared,
                ctx,
                real_selection,
                emit,
                focus=action.parameters.get("focus"),
                student_answer=str(raw_answer) if raw_answer is not None else None,
            )

        active = ctx.active_problem
        if (
            active is not None
            and active.clarity == "clear"
            and not prepared.input.start_new
            and prepared.input.material_artifact_id
            and prepared.input.material_question_id
        ):
            # 会话内章节测试追问/讲解已绑定完整原题干；不得重新理解用户提示词把原题覆盖或换题。
            if active.explanation is None and not (
                prepared.input.student_answer_text or action.parameters.get("student_answer")
            ):
                ctx.answer_scope = ctx.answer_scope or "hint"
                ctx.authorized_text = f"原题干（未解锁答案，只引导思路不公布答案）：\n{active.statement_text}"
                return self._action_result(
                    prepared,
                    ctx,
                    ActionOutcome(
                        name,
                        "success",
                        "已定位当前章节测试原题（尚未查看参考答案），将围绕原题思路与考点引导分析。",
                        {"problem_id": active.id, "version": active.version},
                    ),
                )
            if active.explanation is not None and not (
                prepared.input.student_answer_text
                or prepared.input.solution_text
                or action.parameters.get("student_answer")
                or action.parameters.get("focus")
            ):
                ctx.tutoring = build_explanation_result(
                    active, active.explanation, record_id=self._record_id_for(active)
                )
                ctx.answer_grant = answer_gate.AnswerGrant(
                    kind="explanation",
                    scope=ctx.answer_scope or "explain",
                    request_id=prepared.request_id,
                    target_id=active.id,
                )
                return self._action_result(
                    prepared,
                    ctx,
                    ActionOutcome(
                        name,
                        "success",
                        "已载入当前章节测试原题与参考解析。",
                        {"problem_id": active.id, "version": active.version},
                    ),
                )
            raw_ans = action.parameters.get("student_answer")
            stu_ans = (
                str(raw_ans)
                if raw_ans is not None
                else (prepared.input.student_answer_text or active.student_answer_text)
            )
            emit("explaining", "正在结合当前原题生成讲解")
            tutor = self._make_tutor()
            explain_kwargs: dict[str, Any] = {
                "statement": active.statement_text,
                "statement_images": [],
                "solution_images": [],
                "student_answer": stu_ans,
                "solution_text": prepared.input.solution_text or active.solution_text,
            }
            focus = action.parameters.get("focus")
            if focus and "focus" in inspect.signature(tutor.explain).parameters:
                explain_kwargs["focus"] = str(focus)
            explanation = await tutor.explain(**explain_kwargs)
            with self._repository.action_write():
                saved = self._repository.update_problem_content(
                    active,
                    updated=UpdatedProblem(
                        statement_text=active.statement_text,
                        status="explained",
                        clarity="clear",
                        unclear_points=[],
                        uncertainties=active.uncertainties,
                        source=active.source,
                        student_answer_text=stu_ans,
                        solution_text=prepared.input.solution_text or active.solution_text,
                        explanation=explanation,
                    ),
                    attachments=[],
                )
                ctx.active_problem = saved
                ctx.problem_id = saved.id
                ctx.tutoring = build_explanation_result(
                    saved, explanation, record_id=self._record_id_for(saved)
                )
                ctx.answer_grant = answer_gate.AnswerGrant(
                    kind="explanation",
                    scope=ctx.answer_scope or "explain",
                    request_id=prepared.request_id,
                    target_id=saved.id,
                )
                ctx.recovery["business_saved"] = True
                return self._action_result(
                    prepared,
                    ctx,
                    ActionOutcome(
                        name,
                        "success",
                        "题目已完成解析与分步讲解",
                        {"problem_id": saved.id, "version": saved.version},
                    ),
                )

        mode = self._dynamic_explain_mode(prepared, action, active)
        if mode == "invalid":
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "我没能确定这是新题、补充还是纠正；请说明要修改哪道题。",
                needs_user=True,
            ))

        selected = list(prepared.input.attachments)
        if active is not None and mode in ("supplement", "correction"):
            existing = [
                (item.id, item.role or "statement") for item in active.attachments
            ]
            if not selected:
                selected = existing
            elif mode == "supplement":
                # 补充图片是在原题基础上追加，旧附件不能丢。
                seen = {item[0] for item in selected}
                selected.extend(item for item in existing if item[0] not in seen)
        records = (
            self._attachments_by_ids(prepared.conversation_id, selected)
            if selected
            else []
        )
        statement_images = load_images(
            records, root=self._attachments_root, roles={"statement", "supplement"}
        )
        solution_images = load_images(
            records, root=self._attachments_root, roles={"solution"}
        )

        if mode == "new":
            problem_text = prepared.input.text
        else:
            assert active is not None
            action_label = "补充" if mode == "supplement" else "纠正"
            if prepared.input.text:
                problem_text = (
                    f"原题干：\n{active.statement_text}\n\n"
                    f"学生{action_label}：\n{prepared.input.text}"
                )
            else:
                problem_text = (
                    f"原题干：\n{active.statement_text}\n\n"
                    f"学生只{action_label}了图片，请结合原题干和新图片整理题目。"
                )

        source = prepared.input.source
        student_answer = prepared.input.student_answer_text
        solution_text = prepared.input.solution_text
        if active is not None and mode in ("supplement", "correction"):
            if source.type == "none":
                source = active.source
            if student_answer is None:
                student_answer = active.student_answer_text
            if solution_text is None:
                solution_text = active.solution_text

        emit("explaining", "正在理解题目并生成分步讲解")
        tutor = self._make_tutor()
        understanding = await tutor.understand_problem(problem_text, statement_images)
        unclear = (
            understanding.clarity == "unclear" or understanding.multiple_problems
        )

        if unclear:
            with self._repository.action_write():
                new_problem = NewProblem(
                    statement_text=understanding.statement,
                    status="awaiting_supplement",
                    clarity="unclear",
                    unclear_points=understanding.unclear_points,
                    uncertainties=understanding.uncertainties,
                    source=source,
                    student_answer_text=student_answer,
                    solution_text=solution_text,
                    explanation=None,
                    root_problem_id=(
                        active.root_problem_id
                        if mode == "correction" and active is not None
                        else None
                    ),
                    supersedes_problem_id=(
                        active.id if mode == "correction" and active is not None else None
                    ),
                )
                if mode == "supplement" and active is not None:
                    saved = self._repository.update_problem_content(
                        active,
                        updated=UpdatedProblem(
                            statement_text=new_problem.statement_text,
                            status=new_problem.status,
                            clarity=new_problem.clarity,
                            unclear_points=new_problem.unclear_points,
                            uncertainties=new_problem.uncertainties,
                            source=new_problem.source,
                            student_answer_text=new_problem.student_answer_text,
                            solution_text=new_problem.solution_text,
                            explanation=None,
                        ),
                        attachments=selected,
                    )
                else:
                    saved = self._repository.create_problem_version(
                        conversation_id=prepared.conversation_id,
                        problem=new_problem,
                        attachments=selected,
                    )
                payload = build_needs_clarification(saved)
                if mode != "supplement":
                    # 新题/纠正已切换题目版本，旧练习不应再作为当前练习。
                    self._repository.clear_active_exercise(prepared.conversation_id)
                    ctx.active_exercise = None
                    ctx.exercise_id = None
                ctx.active_problem = saved
                ctx.problem_id = saved.id
                ctx.tutoring = payload
                questions = "；".join(payload.questions)
                ctx.recovery["business_saved"] = True
                return self._action_result(prepared, ctx, ActionOutcome(
                    name,
                    "clarification",
                    f"题目关键信息还不完整，请补充：{questions}",
                    {"problem_id": saved.id, "version": saved.version},
                    needs_user=True,
                ))

        explain_kwargs: dict[str, Any] = {
            "statement": understanding.statement,
            "statement_images": statement_images,
            "solution_images": solution_images,
            "student_answer": student_answer,
            "solution_text": solution_text,
        }
        focus = action.parameters.get("focus")
        if focus and "focus" in inspect.signature(tutor.explain).parameters:
            explain_kwargs["focus"] = str(focus)
        explanation = await tutor.explain(**explain_kwargs)
        with self._repository.action_write():
            if mode == "supplement" and active is not None:
                saved = self._repository.update_problem_content(
                    active,
                    updated=UpdatedProblem(
                        statement_text=understanding.statement,
                        status="explained",
                        clarity="clear",
                        unclear_points=[],
                        uncertainties=understanding.uncertainties,
                        source=source,
                        student_answer_text=student_answer,
                        solution_text=solution_text,
                        explanation=explanation,
                    ),
                    attachments=selected,
                )
            else:
                saved = self._repository.create_problem_version(
                    conversation_id=prepared.conversation_id,
                    problem=NewProblem(
                        statement_text=understanding.statement,
                        status="explained",
                        clarity="clear",
                        unclear_points=[],
                        uncertainties=understanding.uncertainties,
                        source=source,
                        student_answer_text=student_answer,
                        solution_text=solution_text,
                        explanation=explanation,
                        root_problem_id=(
                            active.root_problem_id
                            if mode == "correction" and active is not None
                            else None
                        ),
                        supersedes_problem_id=(
                            active.id if mode == "correction" and active is not None else None
                        ),
                    ),
                    attachments=selected,
                )
            ctx.active_problem = saved
            ctx.problem_id = saved.id
            if mode != "supplement":
                # 新版本题目不再沿用旧题的练习指针；旧练习记录本身保留。
                self._repository.clear_active_exercise(prepared.conversation_id)
                ctx.active_exercise = None
                ctx.exercise_id = None
            ctx.tutoring = build_explanation_result(
                saved, explanation, record_id=self._record_id_for(saved)
            )
            ctx.answer_grant = answer_gate.AnswerGrant(
                kind="explanation",
                scope=ctx.answer_scope or "explain",
                request_id=prepared.request_id,
                target_id=saved.id,
            )
            ctx.recovery["business_saved"] = True
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "success",
                "题目已完成解析与分步讲解",
                {"problem_id": saved.id, "version": saved.version},
            ))

    def _dynamic_explain_mode(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        active: ProblemRecord | None,
    ) -> str:
        requested = action.parameters.get("mode")
        if requested not in (None, "new", "supplement", "correction"):
            return "invalid"
        if prepared.input.start_new or active is None:
            return "new"
        if (
            requested == "new"
            and prepared.input.material_artifact_id
            and prepared.input.material_question_id
        ):
            return "supplement" if active.status == "awaiting_supplement" else "correction"
        if requested == "new":
            return "new"
        if active.status == "awaiting_supplement":
            return "supplement" if requested != "correction" else "correction"
        if requested == "supplement":
            # 已讲解题不接受原地补充，只能建立新版本，避免覆盖旧内容。
            return "correction"
        return "correction"

    async def _dynamic_explain_selection(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        selection: QuestionSelection,
        emit: Callable[[str, str], None],
        *,
        focus: str | None = None,
        student_answer: str | None = None,
    ) -> ActionOutcome:
        emit("explaining", "正在结合原卷材料生成分步讲解")
        try:
            payload, problem, question = await self._explain_selection(
                prepared, selection, focus=focus, student_answer=student_answer
            )
        except QuestionError as exc:
            return self._action_result(prepared, ctx, ActionOutcome(
                "explain_problem", "failed", exc.message, error_code=exc.code,
                retryable=exc.status_code == 503,
            ))
        ctx.active_problem = problem
        ctx.problem_id = problem.id
        ctx.active_exercise = None
        ctx.exercise_id = None
        ctx.tutoring = payload
        ctx.question_selection = (
            self._repository.get_question_selection_by_id(
                prepared.conversation_id, selection.id
            )
            or selection
        )
        ctx.question_card = question
        ctx.question_result_status = "ok"
        ctx.question_result_message = ""
        ctx.answer_grant = answer_gate.AnswerGrant(
            kind="question_explain",
            scope=ctx.answer_scope or "explain",
            request_id=prepared.request_id,
            target_id=question.question_id,
            build_id=question.build_id,
            content_sha256=question.content_sha256,
            selection_id=selection.id,
        )
        return self._action_result(prepared, ctx, ActionOutcome(
            "explain_problem",
            "success",
            "已结合原卷材料完成题目讲解",
            {"problem_id": problem.id, "question_id": question.question_id},
        ))

    async def _resolve_real_question_target(
        self, prepared: ExecuteTurn, ctx: _DynamicContext, action: ActionPlan
    ) -> tuple[QuestionSelection | None, str | None]:
        """讲解入口采用与选题相同的目标解析，不自行匹配第一张同号卡片。"""
        if action.parameters.get("target") == "new":
            return None, None
        if prepared.input.attachments:
            return None, None
        explicit_target = action.parameters.get("paper_id") or action.parameters.get("ref")
        if (
            not explicit_target
            and prepared.input.kind in ("problem", "supplement", "correction")
        ):
            return None, None
        cards, _ = self._question_candidates(ctx)
        selection = ctx.question_selection or self._repository.get_active_question(ctx.conversation_id)
        if not cards and selection is None:
            return None, None
        return await self._resolve_question_selection(
            prepared, ctx, action.parameters, use_active=True
        )

    async def _explain_selection(
        self,
        prepared: ExecuteTurn,
        selection: QuestionSelection,
        *,
        focus: str | None = None,
        student_answer: str | None = None,
    ) -> tuple[TutoringResult, ProblemRecord, QuestionCard]:
        """用真实题目材料讲解，并把题目绑定为当前原题。"""

        if self._make_tutor is None:
            raise TutoringUnavailableError(
                self._tutoring_error or "辅导模型尚未配置。"
            )
        material = await asyncio.to_thread(self.questions.material, selection)
        card = material.card or self.questions.card_for_selection(selection)
        effective_answer = (student_answer or prepared.input.student_answer_text or "").strip() or None
        if (
            effective_answer
            and card.question_type == "choice"
            and card.options
            and effective_answer not in {option.label for option in card.options}
        ):
            raise TurnInvalidError("请选择这道选择题实际提供的选项。")
        tutor = self._make_tutor()
        explain_kwargs: dict[str, Any] = {
            "statement": material.text,
            "statement_images": material.images,
            "solution_images": [],
            "student_answer": effective_answer,
            "solution_text": None,
        }
        if focus and "focus" in inspect.signature(tutor.explain).parameters:
            explain_kwargs["focus"] = str(focus)
        explanation = await tutor.explain(**explain_kwargs)
        links = self._import_question_images(
            prepared.conversation_id, material.images
        )
        source = Source(
            type="registered_paper",
            paper_id=card.source.paper_id,
            paper_title=card.source.paper_title,
        )
        with self._repository.action_write():
            saved = self._repository.create_problem_version(
                conversation_id=prepared.conversation_id,
                problem=NewProblem(
                    statement_text=compose_statement(card),
                    status="explained",
                    clarity="clear",
                    unclear_points=[],
                    uncertainties=[],
                    source=source,
                    student_answer_text=effective_answer,
                    solution_text=None,
                    explanation=explanation,
                ),
                attachments=links,
            )
            self._repository.bind_question_problem(selection.id, saved.id)
            self._repository.activate_question_selection(prepared.conversation_id, selection.id)
            self._repository.reveal_question_answer(
                prepared.conversation_id,
                selection.question_id,
                source="explain",
                selection_id=selection.id,
            )
            self._repository.clear_active_exercise(prepared.conversation_id)
        payload = build_explanation_result(
            saved, explanation, record_id=self._record_id_for(saved)
        )
        question = self.questions.card_for_selection(selection, selected=True)
        question = question.model_copy(update={"answer_revealed": True})
        if material.evidence is not None and material.evidence.status == "ok":
            question = attach_answer(question, material.evidence)
        return payload, saved, question

    def _import_question_images(
        self, conversation_id: str, payloads: list
    ) -> list[tuple[str, str]]:
        """把题面图复制为本次讲解的附件；超限或无效图跳过，不阻止讲解。"""

        links: list[tuple[str, str]] = []
        for payload in payloads[: images.MAX_IMAGES_PER_TURN]:
            try:
                validated = images.validate_image(payload.data)
            except Exception:
                logger.warning("题面图校验失败，已跳过", exc_info=True)
                continue
            attachment_id = str(uuid.uuid4())
            try:
                stored_name = images.save_attachment(
                    self._attachments_root,
                    conversation_id,
                    attachment_id,
                    validated,
                )
            except Exception:
                logger.warning("题面图保存失败，已跳过", exc_info=True)
                continue
            try:
                self._repository.create_attachment(
                    conversation_id=conversation_id,
                    attachment_id=attachment_id,
                    stored_name=stored_name,
                    mime=validated.mime,
                    width=validated.width,
                    height=validated.height,
                    size_bytes=len(validated.data),
                    sha256=validated.sha256,
                )
            except Exception:
                images.delete_attachment(self._attachments_root, stored_name)
                logger.warning("题面图登记失败，已跳过", exc_info=True)
                continue
            links.append((attachment_id, "statement"))
        return links

    def _load_followup_process_images(
        self,
        prepared: ExecuteTurn,
        *,
        allow_statement_role: bool,
        fallback_attachments: Sequence[Any] = (),
    ) -> list[Any]:
        """优先读取本轮提交的过程图片，否则安全读取当前题目历史过程图；读取失败返回空。"""

        if prepared.input.attachments:
            normalized = [
                (att_id, "solution" if role == "student_solution" else role)
                for att_id, role in prepared.input.attachments
            ]
            roles = (
                {"solution", "student_solution", "statement"}
                if allow_statement_role
                else {"solution", "student_solution"}
            )
            with suppress(Exception):
                records = self._attachments_by_ids(prepared.conversation_id, normalized)
                return load_images(records, root=self._attachments_root, roles=roles)
            return []
        if fallback_attachments:
            with suppress(Exception):
                return load_images(
                    list(fallback_attachments),
                    root=self._attachments_root,
                    roles={"student_solution", "solution"},
                )
        return []

    async def _dynamic_followup(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
        target: str = "auto",
        intent: str = "auto",
        ref: str | None = None,
    ) -> ActionOutcome:
        name = "followup_problem"
        active = self._question_operation_problem(ctx)
        card = ctx.question_card
        exercise = ctx.active_exercise

        if isinstance(ref, str) and ref.strip():
            ref_code = ref.strip()
            table = self._ensure_ref_table(ctx)
            entry = table.resolve(ref_code)
            if entry is None:
                return ActionOutcome(
                    name,
                    "clarification",
                    f"工作区里没有找到引用 {ref_code} 对应的题目；请说明要追问哪一道题。",
                    needs_user=True,
                )
            ref_card, _, ref_msg = self._ref_target(ctx, ref_code)
            if ref_card is None:
                return ActionOutcome(
                    name,
                    "clarification",
                    ref_msg or "请说明要追问哪一道题。",
                    needs_user=True,
                )
            card = ref_card
            ctx.question_card = card
            selection = self._repository.find_question_selection_for_card(
                prepared.conversation_id,
                card.question_id,
                card.build_id,
                card.content_sha256,
            )
            if selection is None:
                try:
                    selection = self._select_located_card(prepared, ctx, card)
                except QuestionError as exc:
                    return ActionOutcome(
                        name,
                        "failed",
                        exc.message,
                        error_code=exc.code,
                        needs_user=False,
                    )
            ctx.question_selection = selection
            active = (
                self._repository.get_problem(selection.problem_id)
                if selection.problem_id
                else None
            )
            if active is not None and active.conversation_id != prepared.conversation_id:
                active = None
            ctx.active_problem = active
            ctx.problem_id = active.id if active is not None else None

        exercise_source = (
            self._repository.get_exercise_question_source(exercise.id)
            if exercise is not None
            else None
        )
        exercise_matches_focus = exercise is not None and (
            (card is None and active is None)
            or (active is not None and exercise.problem_id == active.id)
            or (
                card is not None
                and exercise_source is not None
                and card_identity(exercise_source) == card_identity(card)
            )
        )

        # 若只选中了一道新真题但尚未生成讲解，局部追问直接依托公开题面回答，
        # 普通泛泛追问（如 intent=auto）则要求先讲解或说明疑问，绝不退回无关旧练习。
        can_use_unexplained_card = (
            card is not None
            and active is None
            and intent in {"why_step", "beginner_example", "check_process"}
        )
        can_use_standalone_exercise = exercise_matches_focus
        has_explained_problem = (
            active is not None
            and active.status == "explained"
            and active.explanation is not None
        )
        if (
            not has_explained_problem
            and not can_use_standalone_exercise
            and not can_use_unexplained_card
        ):
            if card is not None and active is None:
                return ActionOutcome(
                    name,
                    "clarification",
                    f"第 {card.number_label} 题还没有完成讲解；需要我先讲解这道题，还是告诉我你想问哪一步？",
                    needs_user=True,
                )
            return ActionOutcome(
                name,
                "clarification",
                "当前还没有完成讲解的题目；请先上传或指定题目完成讲解，再针对题目追问。",
                needs_user=True,
            )
        if self._make_tutor is None:
            return ActionOutcome(name, "failed", "辅导模型尚未配置，暂时不能回答追问。")

        emit("answering", "正在解答您对当前题目的疑问")
        explicit_problem = target == "problem"
        if explicit_problem:
            target = "problem"
        use_exercise = exercise_matches_focus and (
            target == "exercise" or (target == "auto" and not explicit_problem)
        )
        if target == "exercise" and not use_exercise:
            return ActionOutcome(
                name,
                "clarification",
                "当前没有与这道题关联的活跃练习，请说明要追问哪道练习。",
                needs_user=True,
            )

        history = (
            self._repository.get_problem_followups(active.id)
            if active is not None and not use_exercise
            else []
        )
        recent_followups = [
            (item["question"], item["answer"]) for item in history[-3:]
        ]
        tutor = self._make_tutor()
        allow_stmt_img = intent == "check_process"
        process_text_parts: list[str] = []
        process_images: list[Any] = []

        if use_exercise and exercise is not None:
            statement = "【当前追问对象：练习题，不是母题】\n" + exercise.statement_text
            visible = self._build_exercise_view(exercise)
            solution = visible.reference_solution_text
            chunks = (
                [solution[i : i + 2000] for i in range(0, len(solution), 2000)]
                if solution
                else ["学生尚未查看解答；仅回答题意、考点、前置小例子或局部提示，不给出最终答案和完整解法。"]
            )
            # 未查看/未开放的解答绝不注入追问上下文。这里不改变查看事实。
            explanation = Explanation(
                knowledge_points=[
                    KnowledgePoint(
                        name=fit_knowledge_point_name(exercise.knowledge_point),
                        explanation="当前练习考点",
                    )
                ],
                steps=[
                    Step(
                        index=index,
                        title="练习参考解答" if solution else "作答提示范围",
                        content=chunk,
                    )
                    for index, chunk in enumerate(chunks, 1)
                ],
                final_answer="完整练习解答见以上材料。" if solution else "参考解答尚未开放，不得公布或推导最终答案。",
            )
            recent_followups = []  # 母题的旧追问不能混入练习的回答依据。
            attempts = visible.attempts or []
            latest_attempt = attempts[-1] if attempts else None
            ex_latest = (exercise.attempts[-1] if exercise.attempts else None)
            sol_text = (
                (prepared.input.solution_text or "").strip()
                or (
                    (latest_attempt.student_solution_text or "").strip()
                    if latest_attempt is not None
                    else ""
                )
                or (visible.student_solution_text or "").strip()
            )
            ans_text = (
                (prepared.input.student_answer_text or "").strip()
                or (
                    (latest_attempt.student_answer_text or "").strip()
                    if latest_attempt is not None
                    else ""
                )
                or (visible.student_answer_text or "").strip()
            )
            if ans_text and sol_text:
                process_text_parts.append(f"学生最终答案：{ans_text}")
            if sol_text:
                process_text_parts.append(sol_text)
            fallback_atts = (
                ex_latest.attachments
                if ex_latest is not None and ex_latest.attachments
                else exercise.attachments
            )
            process_images = self._load_followup_process_images(
                prepared,
                allow_statement_role=allow_stmt_img,
                fallback_attachments=fallback_atts,
            )
            target_id = exercise.id
            followup_target = "exercise"
        elif active is not None:
            statement = active.statement_text
            if (
                ctx.question_selection is not None
                and card is not None
                and ctx.question_selection.problem_id == active.id
            ):
                statement = compose_statement(card)
            if active.explanation is not None:
                explanation = active.explanation
            else:
                explanation = Explanation(
                    knowledge_points=[
                        KnowledgePoint(name="当前题面材料", explanation="此题尚无完整讲解，仅基于公开题面回答局部疑问或概念例子。")
                    ],
                    steps=[Step(index=1, title="题面信息", content=statement[:1200])],
                    final_answer="完整解答尚未生成或开放，不要直接给出完整答案。",
                )
            sol_text = (
                (prepared.input.solution_text or "").strip()
                or (active.solution_text or "").strip()
            )
            ans_text = (
                (prepared.input.student_answer_text or "").strip()
                or (active.student_answer_text or "").strip()
            )
            if ans_text and sol_text:
                process_text_parts.append(f"学生最终答案：{ans_text}")
            if sol_text:
                process_text_parts.append(sol_text)
            process_images = self._load_followup_process_images(
                prepared,
                allow_statement_role=allow_stmt_img,
                fallback_attachments=active.attachments or (),
            )
            target_id = active.id
            followup_target = "problem"
        else:
            assert card is not None
            statement = compose_statement(card)
            explanation = Explanation(
                knowledge_points=[
                    KnowledgePoint(
                        name=fit_knowledge_point_name(
                            "、".join(
                                str(getattr(kp, "name", kp))
                                for kp in (card.knowledge_points or [])
                            ),
                            fallback="选中真题",
                        ),
                        explanation="参考答案尚未公开，仅围绕题意、前置概念例子或第一步思路提示回答，不得泄露完整解法或答案。",
                    )
                ],
                steps=[Step(index=1, title="公开题面", content=statement[:1200])],
                final_answer="参考解答尚未开放，不得公布或推导最终答案。",
            )
            sol_text = (prepared.input.solution_text or "").strip()
            if sol_text:
                process_text_parts.append(sol_text)
            process_images = self._load_followup_process_images(
                prepared,
                allow_statement_role=allow_stmt_img,
            )
            target_id = card.question_id
            followup_target = "question"

        student_solution_text = "\n".join(process_text_parts).strip() or None
        has_student_process = bool(student_solution_text) or bool(process_images)

        followup_kwargs: dict[str, Any] = {
            "statement": statement,
            "explanation": explanation,
            "recent_followups": recent_followups,
            "question": prepared.input.text,
        }
        sig_params = inspect.signature(tutor.answer_followup).parameters
        accepts_varkw = any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in sig_params.values()
        )
        if accepts_varkw or "intent" in sig_params:
            followup_kwargs["intent"] = intent or "auto"
        if accepts_varkw or "has_student_process" in sig_params:
            followup_kwargs["has_student_process"] = has_student_process
        if accepts_varkw or "student_solution_text" in sig_params:
            followup_kwargs["student_solution_text"] = student_solution_text
        if accepts_varkw or "student_solution_images" in sig_params:
            followup_kwargs["student_solution_images"] = process_images

        answer = await tutor.answer_followup(**followup_kwargs)
        ctx.authorized_text = answer.answer
        followup_id = (
            exercise.id
            if (use_exercise and exercise is not None)
            else (active.id if active is not None else card.question_id)
        )
        if active is not None and not use_exercise:
            ctx.problem_id = active.id
        ctx.tutoring = FollowupResult(
            problem_id=followup_id,
            answer=answer.answer,
            related_steps=answer.related_steps,
        )
        if followup_target == "question" and card is not None:
            selection = ctx.question_selection
            ctx.answer_grant = answer_gate.AnswerGrant(
                kind="followup_answer",
                scope=ctx.answer_scope or "explain",
                request_id=prepared.request_id,
                target_id=card.question_id,
                build_id=card.build_id,
                content_sha256=card.content_sha256,
                selection_id=selection.id if selection is not None else None,
            )
        else:
            ctx.answer_grant = answer_gate.AnswerGrant(
                kind="followup_answer",
                scope=ctx.answer_scope or "explain",
                request_id=prepared.request_id,
                target_id=target_id,
            )
        return ActionOutcome(
            name,
            "success",
            "已解答您的追问疑问",
            {
                "problem_id": active.id if active is not None and not use_exercise else None,
                "target_id": target_id,
                "history_count": len(recent_followups),
                "followup_target": followup_target,
                "intent": intent or "auto",
                "has_student_process": has_student_process,
                "process_image_count": len(process_images),
            },
        )
