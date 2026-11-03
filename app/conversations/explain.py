"""讲解处理器：真题讲解、图文辅导、追问和目标解析。"""

import asyncio
import logging
import re
import uuid
from collections.abc import Callable

from app import images
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
from app.questions.intent import (
    looks_like_new_problem,
    mentions_question_target,
)
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

        real_selection, ambiguity = await self._resolve_real_question_target(prepared, ctx)
        if ambiguity:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "clarification", ambiguity, needs_user=True
            ))
        if real_selection is not None:
            return await self._dynamic_explain_selection(
                prepared, ctx, real_selection, emit
            )

        active = ctx.active_problem
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
                return self._action_result(prepared, ctx, ActionOutcome(
                    name,
                    "clarification",
                    f"题目关键信息还不完整，请补充：{questions}",
                    {"problem_id": saved.id, "version": saved.version},
                    needs_user=True,
                ))

        explanation = await tutor.explain(
            statement=understanding.statement,
            statement_images=statement_images,
            solution_images=solution_images,
            student_answer=student_answer,
            solution_text=solution_text,
        )
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
    ) -> ActionOutcome:
        emit("explaining", "正在结合原卷材料生成分步讲解")
        try:
            payload, problem, question = await self._explain_selection(
                prepared, selection
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
        return self._action_result(prepared, ctx, ActionOutcome(
            "explain_problem",
            "success",
            "已结合原卷材料完成题目讲解",
            {"problem_id": problem.id, "question_id": question.question_id},
        ))

    async def _resolve_real_question_target(
        self, prepared: ExecuteTurn, ctx: _DynamicContext
    ) -> tuple[QuestionSelection | None, str | None]:
        """讲解入口采用与选题相同的目标解析，不自行匹配第一张同号卡片。"""
        if prepared.input.attachments or prepared.input.kind in ("problem", "supplement", "correction"):
            return None, None
        text = prepared.input.text or ""
        cards, _ = self._question_candidates(ctx)
        selection = ctx.question_selection or self._repository.get_active_question(ctx.conversation_id)
        if not cards and selection is None:
            return None, None
        targetish = (
            mentions_question_target(text) or not text.strip()
            or bool(re.search(r"第\s*[0-9一二三四五六七八九十]+", text))
            or (ctx.question_result_status == "ok" and not looks_like_new_problem(text))
        )
        if not targetish:
            return None, None
        return await self._resolve_question_selection(prepared, ctx, use_active=True)

    async def _explain_selection(
        self, prepared: ExecuteTurn, selection: QuestionSelection
    ) -> tuple[TutoringResult, ProblemRecord, QuestionCard]:
        """用真实题目材料讲解，并把题目绑定为当前原题。"""

        if self._make_tutor is None:
            raise TutoringUnavailableError(
                self._tutoring_error or "辅导模型尚未配置。"
            )
        material = await asyncio.to_thread(self.questions.material, selection)
        card = material.card or self.questions.card_for_selection(selection)
        student_answer = prepared.input.student_answer_text
        if student_answer and (
            card.question_type != "choice"
            or student_answer not in {option.label for option in card.options}
        ):
            raise TurnInvalidError("请选择这道选择题实际提供的选项。")
        tutor = self._make_tutor()
        explanation = await tutor.explain(
            statement=material.text,
            statement_images=material.images,
            solution_images=[],
            student_answer=student_answer,
            solution_text=None,
        )
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
                    student_answer_text=student_answer,
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

    async def _dynamic_followup(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
        target: str = "auto",
    ) -> ActionOutcome:
        name = "followup_problem"
        active = self._question_operation_problem(ctx)
        if active is None or active.status != "explained" or active.explanation is None:
            return ActionOutcome(
                name,
                "clarification",
                "当前还没有完成讲解的题目；请先上传题目并完成讲解，再针对题目追问。",
                needs_user=True,
            )
        if self._make_tutor is None:
            return ActionOutcome(name, "failed", "辅导模型尚未配置，暂时不能回答追问。")

        emit("answering", "正在解答您对当前题目的疑问")
        history = self._repository.get_problem_followups(active.id)
        recent_followups = [
            (item["question"], item["answer"]) for item in history[-3:]
        ]
        tutor = self._make_tutor()
        statement = active.statement_text
        explanation = active.explanation
        exercise = ctx.active_exercise
        explicit_problem = bool(re.search(r"母题|原题|原来的题|原来那道", prepared.input.text))
        if explicit_problem:
            target = "problem"
        use_exercise = exercise is not None and exercise.problem_id == active.id and (
            target == "exercise" or (target == "auto" and not explicit_problem)
        )
        if target == "exercise" and not use_exercise:
            return ActionOutcome(name, "clarification", "当前没有与这道题关联的活跃练习，请说明要追问哪道练习。", needs_user=True)
        if (
            ctx.question_selection is not None
            and ctx.question_card is not None
            and ctx.question_selection.problem_id == active.id
        ):
            statement = compose_statement(ctx.question_card)
        if use_exercise:
            statement = "【当前追问对象：变式练习，不是母题】\n" + exercise.statement_text
            visible = self._build_exercise_view(exercise)
            solution = visible.reference_solution_text
            chunks = [solution[i:i + 2000] for i in range(0, len(solution), 2000)] if solution else [
                "学生尚未查看解答；仅回答题意、考点或一条提示，不给出答案和完整解法。"
            ]
            # 未查看/未开放的解答绝不注入追问上下文。这里不改变查看事实。
            explanation = Explanation(
                knowledge_points=[KnowledgePoint(name=exercise.knowledge_point, explanation="当前练习考点")],
                steps=[Step(index=index, title="练习参考解答" if solution else "作答提示", content=chunk)
                       for index, chunk in enumerate(chunks, 1)],
                final_answer="完整练习解答见以上材料。" if solution else "参考解答尚未开放，不得公布或推导最终答案。",
            )
            recent_followups = []  # 母题的旧追问不能混入练习的回答依据。
        answer = await tutor.answer_followup(
            statement=statement,
            explanation=explanation,
            recent_followups=recent_followups,
            question=prepared.input.text,
        )
        ctx.problem_id = active.id
        ctx.tutoring = FollowupResult(
            problem_id=active.id,
            answer=answer.answer,
            related_steps=answer.related_steps,
        )
        return ActionOutcome(
            name,
            "success",
            "已解答您的追问疑问",
            {
                "problem_id": active.id,
                "history_count": len(recent_followups),
                "followup_target": "exercise" if use_exercise else "problem",
            },
        )

    @classmethod
    def _answer_scope(cls, text: str) -> str | None:
        """本轮回答范围：``hint``（只要提示）、``topic``（只问考点）。

        两者都**不是**“完整讲解”授权，因此不能执行讲解或看答案。学生同时
        要求讲解/答案时返回 ``None``，按完整讲解处理。
        """

        value = text or ""
        if cls._wants_hint_only(value):
            return "hint"
        # 判断“解释/讲”的对象与范围：说的是“考什么/考点/知识点”，就只是考点，
        # 不能因为出现“解释”两个字就当成完整讲解。按小句判断，避免“讲讲…的
        # 考点和解题步骤”被整句吞掉。
        topic_object = re.compile(
            r"考点|知识点|考查(什么|点|哪些|哪个)"
            r"|考[了的]?(什么|啥|哪些|哪个|哪几个)"
            r"|考核?[^。；，,！？]{0,3}(什么|啥|哪些|哪个|哪几个)"
            r"|查[^。；，,！？]{0,3}(什么|啥|哪个|哪些)"
        )
        if not topic_object.search(value):
            return None
        # 问“这段代码/解题过程考什么”时，代码和过程是提问对象，不是
        # 请求的输出。完整解答须由独立请求或明确的解答动作来授权。
        full_marker = re.compile(
            r"步骤|答案|解答|过程|代码|完整|怎么做|为什么|解析|推导|思路|例子|举例"
        )
        solution_request = re.compile(
            r"(?:给出|展示|写出|补全|实现|解释|讲解|分析).{0,8}"
            r"(?:答案|解答|解法|步骤|解题过程|参考代码|完整代码)"
            r"|(?:代码|程序)(?:怎么写|如何写)|为什么(?:选|答案|结果)"
        )
        clause_marker = re.compile(r"讲|解释|说明|分析|说说|聊聊")
        has_topic = False
        wants_more = False
        for clause in re.split(r"[，,。；;！!？?\s]+|和|以及|与|并|然后|再|顺便|另外", value):
            # “不用给答案/不必讲步骤”是限制，不是要求。
            clause = re.sub(r"(不用|不需要|不必|不要|别|无需)[^。；，,]{0,8}", " ", clause)
            if not clause.strip():
                continue
            if topic_object.search(clause):
                has_topic = True
                if solution_request.search(clause):
                    wants_more = True
            elif clause_marker.search(clause) or full_marker.search(clause):
                wants_more = True
        return "topic" if has_topic and not wants_more else None
