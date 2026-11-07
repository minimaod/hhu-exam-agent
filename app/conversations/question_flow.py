"""查卷与查题处理器：试卷定位、查询、浏览、分页、选题及来源访问。"""

import json
import logging
import uuid
from collections.abc import (
    AsyncIterator,
    Callable,
)
from dataclasses import replace
from typing import Any

from app.conversations import answer_gate
from app.conversations.errors import ConversationStorageError
from app.conversations.final_reply import question_answer_text
from app.conversations.graph import (
    ActionOutcome,
    ActionPlan,
)
from app.conversations.models import (
    ActionResultItem,
    SuggestionItem,
    Turn,
    TurnEvent,
    TurnMessageDeltaEvent,
    TurnProgressEvent,
    TurnResultEvent,
    reply_state as _reply_state,
)
from app.conversations.reply import (
    build_material,
    material_to_payload,
)
from app.conversations.turn_types import (
    ExecuteTurn,
    _DynamicContext,
    _QUESTION_KINDS,
    _RunState,
    _TUTORING_MESSAGE,
    _storage_error_event,
)
from app.querying.errors import QueryError
from app.questions.cards import answer_url as answer_url_for
from app.questions.errors import (
    QuestionError,
    QuestionNotFoundError,
    QuestionNotRevealedError,
    QuestionSourceChangedError,
    QuestionSourcePrunedError,
)
from app.questions.models import (
    QuestionCard,
    QuestionSelection,
)

logger = logging.getLogger("app.conversations.service")

def _question_suggestions(card: QuestionCard) -> list[SuggestionItem]:
    return [
        SuggestionItem(label="讲解这道题", action="explain_question", target_id=card.question_id),
        SuggestionItem(label="查看参考答案", action="view_question_answer", target_id=card.question_id),
        SuggestionItem(label="生成一道变式", action="exercise", target_id=card.question_id),
        SuggestionItem(label="保存到错题本", action="record", target_id=card.question_id),
    ]

class QuestionFlowMixin:
    """真题查询、浏览、来源与答案的轮次处理。"""

    def question_asset(
        self, conversation_id: str, question_id: str, asset_id: str, build_id: str
    ):
        try:
            selection = self._require_question_selection(
                conversation_id, question_id, build_id
            )
        except (QuestionNotFoundError, QuestionSourceChangedError):
            # 候选卡片在选中前就要显示题面图；只允许本会话查题结果里出现过的
            # 卡片，答案正文仍必须走选中 + 首次查看门禁。
            card = self._find_candidate_card(
                conversation_id, question_id, build_id
            )
            if card is None:
                raise
            return self.questions.asset_for_card(card, asset_id)
        return self.questions.asset(selection, asset_id)

    def question_source(
        self, conversation_id: str, question_id: str, build_id: str, kind: str, page: int
    ):
        """用户主动打开来源：验证原卷后才记查看事实，不改变当前选题。"""
        card = self._find_candidate_card(conversation_id, question_id, build_id)
        if card is None:
            raise QuestionNotFoundError("该来源不属于当前会话的题目。")
        payload = self.questions.source_for_card(card, kind, page)
        with self._repository.action_write():
            selection = self._repository.create_question_selection(
                conversation_id=conversation_id,
                request_id=f"source:{uuid.uuid4()}",
                source_request_id=card.source_request_id or "",
                card=card, build_id=card.build_id,
                source_version=payload.source_version or "",
                activate=False,
            )
            self._repository.record_question_source_view(
                selection.id, kind=kind, page=page,
                may_show_answer=card.source.may_show_answer,
            )
            if card.source.may_show_answer:
                self._repository.reveal_question_answer(
                    conversation_id, question_id, source="view", selection_id=selection.id
                )
        return payload

    def question_answer(
        self, conversation_id: str, question_id: str, build_id: str
    ) -> QuestionCard:
        selection = self._require_question_selection(
            conversation_id, question_id, build_id
        )
        if not selection.answer_revealed_at:
            raise QuestionNotRevealedError("还没有查看这道题的答案。")
        card, evidence = self.questions.answer_view(selection)
        if evidence.status == "source_changed":
            raise QuestionSourceChangedError(
                evidence.message or "题目内容已变化，请重新查题。"
            )
        if evidence.status == "source_pruned":
            raise QuestionSourcePrunedError(
                evidence.message or "原来源版本已不在本机。"
            )
        return card

    def _question_card_for_selection(
        self, selection: QuestionSelection, *, include_answer: bool = False
    ) -> QuestionCard:
        card = self.questions.card_for_selection(selection)
        if include_answer and selection.answer_revealed_at:
            try:
                card, _ = self.questions.answer_view(selection)
            except QuestionError:
                pass
        return card

    def _require_question_selection(
        self, conversation_id: str, question_id: str, build_id: str
    ) -> QuestionSelection:
        selection = self._repository.get_question_selection(
            conversation_id, question_id, build_id=build_id
        )
        if selection is None:
            if self._repository.get_question_selection(conversation_id, question_id) is not None:
                raise QuestionSourceChangedError("题目来源版本不一致，请重新查题。")
            raise QuestionNotFoundError("这道题不属于当前会话，或尚未被选中。")
        if build_id and selection.build_id != build_id:
            raise QuestionSourceChangedError("题目来源版本不一致，请重新查题。")
        return selection

    def _find_candidate_card(
        self, conversation_id: str, question_id: str, build_id: str
    ) -> QuestionCard | None:
        for turn in self._repository.list_turns(conversation_id):
            for card in turn.question_results:
                if (
                    card.question_id == question_id
                    and (not build_id or card.build_id == build_id)
                ):
                    return card.model_copy(update={"source_request_id": turn.request_id})
            if (
                turn.question is not None
                and turn.question.question_id == question_id
                and (not build_id or turn.question.build_id == build_id)
            ):
                return turn.question
        return None

    def _enrich_question_turn(self, conversation_id: str, turn: Turn) -> Turn:
        """详情页只从快照重建卡片；仅为已开放对象补答案正文。"""

        updates: dict[str, Any] = {}
        if turn.question_results:
            results = []
            for card in turn.question_results:
                selection = self._repository.find_question_selection_for_card(
                    conversation_id, card.question_id, card.build_id, card.content_sha256
                )
                results.append(QuestionCard.model_validate({**card.model_dump(),
                    "source_url": answer_url_for(conversation_id, card.question_id, card.build_id).replace("/answer?", "/source?"),
                    "source_views": (selection.snapshot.get("source_views", []) if selection else []),
                    "answer_url": answer_url_for(conversation_id, card.question_id, card.build_id) if card.answer.available else None,
                }))
            updates["question_results"] = results
        if turn.question is not None:
            card = turn.question
            selection = None
            if card.selection_id:
                selection = self._repository.get_question_selection_by_id(
                    conversation_id, card.selection_id
                )
            if selection is None:
                # 旧轮次没有 selection_id 时，按当时的 build + 内容哈希回找；
                # 不能只按题号取最新选择，否则历史题干会被新版本替换。
                selection = self._repository.find_question_selection_for_card(
                    conversation_id,
                    card.question_id,
                    card.build_id,
                    card.content_sha256,
                )
            if selection is not None:
                card = self._question_card_for_selection(
                    selection, include_answer=True
                )
                # 以历史卡片自己的快照为准，避免来源更新时把旧轮次换成新题面。
                if (
                    card.content_sha256 != turn.question.content_sha256
                    or card.build_id != turn.question.build_id
                ):
                    card = turn.question
            card = card.model_copy(
                update={
                    "source_url": answer_url_for(conversation_id, card.question_id, card.build_id).replace("/answer?", "/source?"),
                    "answer_url": (
                        answer_url_for(
                            conversation_id, card.question_id, card.build_id
                        )
                        if card.answer.available
                        else None
                    )
                }
            )
            updates["question"] = card
        return turn.model_copy(update=updates) if updates else turn

    async def _question_action_events(
        self, prepared: ExecuteTurn, state: _RunState
    ) -> AsyncIterator[TurnEvent]:
        """卡片按钮的确定性动作：目标只由 question_id 决定，不读当前题。"""

        data = prepared.input
        action = data.question_action or ""
        try:
            yield TurnProgressEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                stage="searching_questions",
                message="正在核对题目来源与内容",
            )
            card = self._resolve_question_card(
                prepared.conversation_id,
                data.question_source_turn or "",
                data.question_id or "",
            )
            selection = self._select_question_card(
                prepared.conversation_id,
                prepared.request_id,
                data.question_source_turn or "",
                card,
            )
        except QuestionError as exc:
            yield self._failure_event(prepared, exc.code, exc.message, state)
            return

        problem = (
            self._repository.get_problem(selection.problem_id)
            if selection.problem_id
            else None
        )
        if problem is not None and problem.conversation_id != prepared.conversation_id:
            problem = None
        ctx = self._load_dynamic_context(prepared.conversation_id)
        ctx.active_problem = problem
        ctx.problem_id = problem.id if problem is not None else None
        ctx.question_selection = selection
        ctx.question_card = card
        ctx.question_source_turn = data.question_source_turn
        ctx.question_results = [card]
        if action == "exercise":
            try:
                outcome = await self._dynamic_generate_exercise(
                    prepared,
                    ctx,
                    lambda stage, message: None,
                )
            except QueryError as exc:
                yield self._failure_event(prepared, exc.code, exc.message, state)
                return
            except Exception:
                logger.exception("真题变式生成失败")
                yield self._failure_event(
                    prepared, "TUTORING_FAILED", _TUTORING_MESSAGE, state
                )
                return
        else:
            try:
                outcome = self._dynamic_record_mistake(
                    prepared,
                    ctx,
                    lambda stage, message: None,
                )
            except Exception:
                logger.exception("真题错题保存失败")
                yield self._failure_event(
                    prepared, "TUTORING_FAILED", _TUTORING_MESSAGE, state
                )
                return
        composite = [
            ActionResultItem(
                action="generate_exercise" if action == "exercise" else "record_mistake",
                status=outcome.status,  # type: ignore[arg-type]
                message=outcome.message,
                details=dict(outcome.details or {}),
            )
        ]
        try:
            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="dynamic",
                tutoring=ctx.tutoring,
                exercise=ctx.exercise_view,
                composite_results=composite,
                assistant_message=outcome.message,
                suggestions=[],
                question=ctx.question_card,
                problem_id=ctx.problem_id,
                exercise_id=ctx.exercise_id,
            )
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return
        state.saved = True
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind="dynamic",
            tutoring=ctx.tutoring,
            exercise=ctx.exercise_view,
            composite_results=composite,
            assistant_message=outcome.message,
            question=ctx.question_card,
            exercise_id=ctx.exercise_id,
        )

    async def _question_events(
        self, prepared: ExecuteTurn, state: _RunState
    ) -> AsyncIterator[TurnEvent]:
        data = prepared.input
        kind = data.kind
        assert kind in _QUESTION_KINDS
        try:
            yield TurnProgressEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                stage="searching_questions",
                message="正在核对题目来源与内容",
            )
            card = self._resolve_question_card(
                prepared.conversation_id,
                data.question_source_turn or "",
                data.question_id or "",
            )
            selection = self._select_question_card(
                prepared.conversation_id,
                prepared.request_id,
                data.question_source_turn or "",
                card,
            )
        except QuestionError as exc:
            yield self._failure_event(prepared, exc.code, exc.message, state)
            return

        if kind == "select_question":
            question = self.questions.card_for_selection(selection, selected=True)
            message = (
                f"已选中《{question.source.paper_title or question.source.paper_id}》"
                f"第 {question.number_label} 题。"
            )
            suggestions = _question_suggestions(question)
            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind=kind,
                question=question,
                question_result_status="ok",
                assistant_message=message,
                suggestions=suggestions,
            )
            state.saved = True
            yield TurnResultEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind=kind,
                question=question,
                question_result_status="ok",
                assistant_message=message,
                suggestions=suggestions,
            )
            return

        if kind == "view_question_answer":
            try:
                yield TurnProgressEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    stage="viewing_question",
                    message="正在读取原卷答案",
                )
                selection = (
                    self._repository.reveal_question_answer(
                        prepared.conversation_id,
                        selection.question_id,
                        source="view",
                        selection_id=selection.id,
                    )
                    or selection
                )
                question, evidence = self.questions.answer_view(selection)
            except QuestionError as exc:
                yield self._failure_event(prepared, exc.code, exc.message, state)
                return
            status = "ok"
            if evidence.status == "missing_answer":
                status = "missing_answer"
            elif evidence.status == "source_changed":
                status = "source_changed"
            elif evidence.status == "source_pruned":
                status = "source_pruned"
            message = evidence.message or "已显示原卷参考答案。"
            suggestions = _question_suggestions(question)
            self._repository.complete_dynamic_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind=kind,
                question=question,
                question_result_status=status,
                question_result_message=message,
                assistant_message=message,
                suggestions=suggestions,
            )
            state.saved = True
            yield TurnResultEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind=kind,
                question=question,
                question_result_status=status,  # type: ignore[arg-type]
                question_result_message=message,
                assistant_message=message,
                suggestions=suggestions,
            )
            return

        # explain_question
        try:
            payload, problem, question = await self._explain_selection(
                prepared, selection
            )
        except QuestionError as exc:
            yield self._failure_event(prepared, exc.code, exc.message, state)
            return
        except Exception:
            logger.exception("真题讲解失败")
            yield self._failure_event(
                prepared, "TUTORING_FAILED", _TUTORING_MESSAGE, state
            )
            return
        # 真题卡片的“讲解/提交并讲解”与普通动态轮次共用同一个正文阶段：
        # 讲解内容已经在卡片里展示，因此允许正文完整讲一遍。
        fallback_message = "已结合原卷材料完成题目讲解。"
        composite = [
            ActionResultItem(
                action="explain_question",
                status="success",
                message=f"已完成第 {question.number_label} 题的讲解。",
                details={
                    "number_label": question.number_label,
                    "question_type": question.question_type,
                },
            )
        ]
        suggestions = _question_suggestions(question)
        ctx = _DynamicContext(
            conversation_id=prepared.conversation_id,
            tutoring=payload,
            problem_id=problem.id,
            question_card=question,
            question_results=[question],
        )
        message = fallback_message
        material = None
        material_payload: dict[str, Any] | None = None
        reply_meta: dict[str, Any] = {
            "mode": "fallback",
            "reason": "reply_not_configured",
            "deltas": 0,
        }
        if self._make_reply is not None:
            material = build_material(
                user_text=prepared.input.text,
                course=None,
                composite_results=composite,
                waiting_for=None,
                question_titles=self._question_titles([question]),
                suggestions=[item.label for item in suggestions],
                context_summary=self._recent_turns_summary(prepared.conversation_id),
                teaching=self._teaching_material(ctx),
            )
            material_payload = material_to_payload(material)
            # 正文还没开始输出就先记成“待生成”：若客户端此刻断开或进程被
            # 重启，恢复记录里已经有讲解和材料，重试只需重做正文。
            reply_meta = {"mode": "fallback", "reason": "reply_pending", "deltas": 0}

        # 顺序很重要：先把讲解、目标绑定、回复材料全部落盘，再开始流式输出。
        # 这样“讲完题但正文没写完”不会变成需要重新讲题的中断轮次。
        self._repository.complete_dynamic_turn(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind=kind,
            tutoring=payload,
            composite_results=composite,
            question=question,
            question_result_status="ok",
            assistant_message=message,
            suggestions=suggestions,
            problem_id=problem.id,
        )
        if self._make_reply is not None:
            self._repository.update_turn_reply(
                prepared.conversation_id,
                prepared.request_id,
                assistant_message=message,
                reply_meta=reply_meta,
                reply_input=material_payload,
            )
        self._repository.upsert_context(
            prepared.conversation_id,
            active_problem_id=problem.id,
            last_suggestions_json=json.dumps(
                [it.model_dump(mode="json") for it in suggestions],
                ensure_ascii=False,
            ),
            last_action_summary=message,
        )
        state.saved = True

        if material is not None:
            yield TurnProgressEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                stage="composing_reply",
                message="正在组织讲解",
            )
            meta: dict[str, Any] = {}
            async for piece in self._reply_pieces(self._make_reply(), material, meta):
                yield TurnMessageDeltaEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    seq=int(meta["deltas"]),
                    delta=piece[:400],
                )
            text = str(meta.pop("text", "") or "")
            if text:
                message = text
            reply_meta = (
                meta
                if meta.get("mode")
                else {"mode": "fallback", "reason": "empty_reply", "deltas": 0}
            )
            # 正文结束（或失败）后再更新一次：正文与状态一起变成最终值。
            self._repository.update_turn_reply(
                prepared.conversation_id,
                prepared.request_id,
                assistant_message=message,
                reply_meta=reply_meta,
                reply_input=material_payload,
            )

        if self._make_reply is None:
            # 没有配置回复模型：正文是确定性说明，不标正文状态。
            reply_status, reply_retryable = None, False
        else:
            reply_status, reply_retryable = _reply_state(
                {"reply": reply_meta, "reply_input": material_payload}
            )
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind=kind,
            tutoring=payload,
            composite_results=composite,
            question=question,
            question_result_status="ok",
            assistant_message=message,
            reply_status=reply_status,  # type: ignore[arg-type]
            reply_retryable=reply_retryable,
            suggestions=suggestions,
        )

    async def _dynamic_list_paper_questions(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        """整卷题型浏览：给总数、本次范围和下一批位置。"""

        name = "list_paper_questions"
        emit("searching_questions", "正在读取这份试卷的题目")
        intent = self._intent_from_params(ctx, action)
        if not intent.course:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "请先告诉我课程，我再按整卷题型列给你。",
                needs_user=True,
            ))
        if intent.paper_id is None:
            options = self._paper_options(intent.course)
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "要列哪一份试卷的题目？" + (f"目前有：{options}。" if options else ""),
                needs_user=True,
            ))
        if intent.page_error:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "clarification", intent.page_error, needs_user=True,
                error_code="PAGE_SCOPE_CHANGED", retryable=False,
            ))
        if intent.page_exhausted:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "clarification", self._exhausted_text(ctx, intent), needs_user=True
            ))
        intent = replace(intent, query="")
        result = await self._run_question_search(
            prepared, ctx, name, intent, emit,
            exclude_question_id=(
                action.parameters.get("exclude_question_id")
                or (
                    self._source_question_id(ctx)
                    if action.parameters.get("exclude_previous")
                    else None
                )
            ),
        )
        if isinstance(result, ActionOutcome):
            return result
        return self._question_search_outcome(prepared, ctx, name, intent, result)

    async def _dynamic_view_question_answer(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "view_question_answer"
        selection = ctx.question_selection or self._repository.get_active_question(
            prepared.conversation_id
        )
        if selection is None:
            cards, source_turn = self._question_candidates(ctx)
            if len(cards) == 1 and source_turn is not None:
                try:
                    selection = self._select_candidate(
                        prepared.conversation_id,
                        prepared.request_id,
                        source_turn,
                        cards[0],
                    )
                except QuestionError as exc:
                    return self._action_result(prepared, ctx, ActionOutcome(
                        name, "failed", exc.message, error_code=exc.code,
                        needs_user=True,
                    ))
            else:
                return self._action_result(prepared, ctx, ActionOutcome(
                    name,
                    "clarification",
                    "请先选中一道真题，再查看答案。",
                    needs_user=True,
                ))
        emit("viewing_question", "正在读取原卷答案")
        # F010 C：先拿到本轮的公开卡片（同轮选中用内存卡，跨轮用已提交快照），
        # 再经过答案门禁。未获授权时不 reveal、不读 answer_detail，也不调用
        # answer_view；版本与来源在真正读取时还会再校验一次。
        card: QuestionCard | None = None
        if ctx.question_card is not None and ctx.question_card.question_id == selection.question_id:
            card = ctx.question_card
        if card is None:
            try:
                card = self.questions.card_for_selection(selection)
            except QuestionError:
                card = None
        if card is None:
            try:
                card = self._resolve_question_card(
                    prepared.conversation_id,
                    selection.source_request_id,
                    selection.question_id,
                )
            except QuestionSourceChangedError as exc:
                return self._gate_rejection(
                    prepared,
                    ctx,
                    name,
                    answer_gate.GateDecision(
                        code="ANSWER_VERSION_CHANGED",
                        message=exc.message,
                        remedies=answer_gate.REJECTION_REMEDIES["ANSWER_VERSION_CHANGED"],
                        facts={
                            "question_id": selection.question_id,
                            "number_label": selection.number_label,
                        },
                    ),
                )
            except QuestionError as exc:
                return self._gate_rejection(
                    prepared,
                    ctx,
                    name,
                    answer_gate.GateDecision(
                        code="ANSWER_SOURCE_INVALID",
                        message=exc.message,
                        remedies=answer_gate.REJECTION_REMEDIES["ANSWER_SOURCE_INVALID"],
                        facts={"question_id": selection.question_id},
                    ),
                )
        decision = answer_gate.question_answer_gate(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            student_text=prepared.input.text,
            selection=selection,
            card=card,
        )
        if not decision.allowed:
            return self._gate_rejection(
                prepared,
                ctx,
                name,
                decision,
                facts={
                    "question_id": selection.question_id,
                    "number_label": selection.number_label,
                },
            )
        ctx.answer_grant = decision.grant
        try:
            selection = (
                self._repository.reveal_question_answer(
                    prepared.conversation_id,
                    selection.question_id,
                    source="view",
                    selection_id=selection.id,
                )
                or selection
            )
            card, evidence = self.questions.answer_view(selection)
        except QuestionError as exc:
            ctx.answer_grant = None
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "failed", exc.message, error_code=exc.code,
                needs_user=True,
            ))
        ctx.question_selection = selection
        ctx.question_card = card
        if evidence.status in ("source_changed", "source_pruned"):
            ctx.answer_grant = None
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                evidence.message or "来源已变化，暂时不能显示答案。",
                needs_user=True,
            ))
        message = evidence.message or "已显示原卷参考答案。"
        ctx.question_result_status = "ok"
        ctx.question_result_message = message
        ctx.authorized_text = question_answer_text(card)
        return self._action_result(prepared, ctx, ActionOutcome(
            name,
            "success",
            message,
            {"question_id": selection.question_id, "answer_state": card.answer.state},
        ))

    def _paper_options(self, course: str | None) -> str:
        if self._papers is None:
            return ""
        try:
            items = self._papers.list_papers(course=course or "Python语言程序设计").items
        except Exception:
            logger.warning("读取试卷目录失败", exc_info=True)
            return ""
        return "、".join(f"《{item.title}》" for item in items[:5])
