"""旧显式流程：原查询、辅导、反馈、记录等兼容流程完整保留。"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from fastapi import Request

from app.conversations.dispatch import IntentDecision
from app.conversations.errors import (
    ActiveProblemConflictError,
    AttachmentInvalidError,
    ConversationQueryUnavailableError,
    ConversationStorageError,
    InvalidImageError,
    ProblemNotFoundError,
    ProblemNotReadyError,
    TurnInvalidError,
)
from app.conversations.models import (
    TurnErrorEvent,
    TurnEvent,
    TurnProgressEvent,
    TurnResultEvent,
)
from app.conversations.repository import (
    NewProblem,
    ProblemRecord,
    UpdatedProblem,
)
from app.conversations.turn_types import (
    ExecuteTurn,
    TurnInput,
    _DISPATCH_MESSAGE,
    _DISPATCH_TIMEOUT_SECONDS,
    _QUESTION_KINDS,
    _RunState,
    _TIMEOUT_MESSAGE,
    _TUTORING_MESSAGE,
    _TUTORING_TIMEOUT_SECONDS,
    _storage_error_event,
)
from app.querying.errors import QueryError
from app.querying.models import (
    ErrorEvent,
    ProgressEvent,
    ResultEvent,
)
from app.questions.errors import (
    QuestionBankUnavailableError,
    QuestionSelectionInvalidError,
    QuestionSourceChangedError,
)
from app.questions.models import (
    QuestionCard,
    QuestionSelection,
)
from app.streaming import (
    SENTINEL,
    stream_events,
)
from app.tutoring.models import (
    FeedbackResult,
    FollowupResult,
    IntentClarificationResult,
    NeedsClarificationResult,
    RecordCreatedResult,
    RecordExistsResult,
    TutoringResult,
)
from app.tutoring.service import (
    build_explanation_result,
    build_needs_clarification,
    build_problem_view,
    load_images,
)
from app.tutoring.workflow import build_tutoring_workflow

logger = logging.getLogger("app.conversations.service")

class LegacyFlowMixin:
    """F004-F006 的显式轮次流程，行为与事件顺序保持不变。"""

    def _prepare_explicit(
        self,
        conversation_id: str,
        data: TurnInput,
        canonical: str,
        active: ProblemRecord | None,
    ) -> ExecuteTurn:
        kind = data.kind
        assert kind is not None

        if kind == "query":
            self._validate_attachments(conversation_id, data, "query")
            self._validate_source(active, data, "query")
            if data.problem_id is not None or data.start_new:
                raise TurnInvalidError("查询轮次不能携带题目引用或新题标记。")
            if not data.text:
                raise TurnInvalidError("查询内容不能为空。")
            if self._make_query is None:
                raise ConversationQueryUnavailableError(
                    self._query_error or "自然语言查询尚未配置。"
                )
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="query",
                input_json=canonical,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="query",
            )

        if kind in _QUESTION_KINDS:
            return self._prepare_question_turn(
                conversation_id, data, canonical, active
            )

        if kind == "problem":
            if active is not None and not data.start_new:
                raise ActiveProblemConflictError(
                    "当前已有正在辅导的题目。若要开始新题，请明确确认。"
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
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="problem",
            )

        if kind in ("supplement", "followup"):
            current = self._require_active(active, data.problem_id)

        if kind == "supplement":
            if current.status != "awaiting_supplement":
                raise ProblemNotReadyError(
                    "当前题目已经讲解过；如要改题干，请使用纠正。"
                )
            self._validate_attachments(conversation_id, data, "supplement")
            self._validate_source(current, data, "supplement")
            self._require_tutor()
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="supplement",
                input_json=canonical,
                attachments=data.attachments,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="supplement",
                problem=current,
            )

        if kind == "followup":
            if current.status != "explained" or current.explanation is None:
                raise ProblemNotReadyError("当前题目尚未讲解，暂时不能追问。")
            self._validate_attachments(conversation_id, data, "followup")
            self._validate_source(current, data, "followup")
            if not data.text:
                raise TurnInvalidError("追问内容不能为空。")
            self._require_tutor()
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="followup",
                input_json=canonical,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="followup",
                problem=current,
            )

        if kind == "correction":
            target = self._resolve_target_problem(
                conversation_id,
                active,
                data.problem_id,
                require_explained=False,
            )
            self._validate_attachments(conversation_id, data, "correction")
            self._validate_source(target, data, "correction")
            self._require_tutor()
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="correction",
                input_json=canonical,
                attachments=data.attachments,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="correction",
                problem=target,
            )

        if kind == "record":
            target = self._resolve_target_problem(
                conversation_id,
                active,
                data.problem_id,
                require_explained=True,
            )
            self._validate_attachments(conversation_id, data, "record")
            self._validate_source(target, data, "record")
            if self._records is None:
                raise ConversationStorageError("错题记录服务不可用。")
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="record",
                input_json=canonical,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="record",
                problem=target,
            )

        if kind == "feedback":
            target = self._resolve_target_problem(
                conversation_id,
                active,
                data.problem_id,
                require_explained=True,
            )
            self._validate_attachments(conversation_id, data, "feedback")
            self._validate_source(target, data, "feedback")
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="feedback",
                input_json=canonical,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="feedback",
                problem=target,
            )

        if kind == "reveal_solution":
            ex = self._resolve_exercise_target(conversation_id, data.exercise_id)
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="reveal_solution",
                input_json=canonical,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="reveal_solution",
                exercise=ex,
            )

        if kind in ("exercise", "exercise_answer"):
            ex = self._resolve_exercise_target(conversation_id, data.exercise_id)
            if data.attempt_kind:
                self._validate_attempt_kind(ex, data.attempt_kind)
            self._validate_attachments(conversation_id, data, "tutoring")
            turn = self._repository.start_turn(
                conversation_id,
                data.request_id,
                data.text,
                kind="exercise",
                input_json=canonical,
                attachments=data.attachments,
            )
            return ExecuteTurn(
                conversation_id=conversation_id,
                request_id=data.request_id,
                turn=turn,
                input=data,
                operation="exercise_answer",
                exercise=ex,
            )

        raise TurnInvalidError("未知的轮次类型。")

    def _prepare_question_turn(
        self,
        conversation_id: str,
        data: TurnInput,
        canonical: str,
        active: ProblemRecord | None,
    ) -> ExecuteTurn:
        kind = data.kind
        assert kind in _QUESTION_KINDS
        if not data.question_id or not data.question_source_turn:
            raise TurnInvalidError("缺少题目或查题轮次标识。")
        if data.attachments or data.solution_text or (
            data.student_answer_text and kind != "explain_question"
        ):
            raise TurnInvalidError("仅真题讲解可以携带所选答案；不支持附件或解题过程。")
        if data.source.type != "none":
            raise TurnInvalidError("真题轮次不能另外指定题目来源。")
        if data.problem_id is not None or data.start_new:
            raise TurnInvalidError("真题轮次不能携带题目引用或新题标记。")
        if kind == "explain_question":
            self._require_tutor()
        if not self.questions.ready:
            raise QuestionBankUnavailableError("题目库暂时不可用，请稍后重试。")
        # 先确认候选题仍属于该查题轮次且内容哈希一致，再写入 running 轮次。
        card = self._resolve_question_card(
            conversation_id, data.question_source_turn, data.question_id
        )
        if data.student_answer_text and (
            card.question_type != "choice"
            or data.student_answer_text not in {option.label for option in card.options}
        ):
            raise TurnInvalidError("请选择这道选择题实际提供的选项。")
        turn = self._repository.start_turn(
            conversation_id,
            data.request_id,
            data.text,
            kind=kind,
            input_json=canonical,
        )
        return ExecuteTurn(
            conversation_id=conversation_id,
            request_id=data.request_id,
            turn=turn,
            input=data,
            operation=kind,
            problem=active,
        )

    def _resolve_question_card(
        self, conversation_id: str, source_request_id: str, question_id: str
    ) -> QuestionCard:
        turn = self._repository.find_turn(conversation_id, source_request_id)
        if turn is None:
            raise QuestionSelectionInvalidError("查题轮次不属于当前会话，请重新查题。")
        card = next(
            (item for item in turn.question_results if item.question_id == question_id),
            None,
        )
        if card is None:
            raise QuestionSelectionInvalidError("该题目不在这次查题结果中，请重新查题。")
        if not card.build_id or not card.content_sha256:
            raise QuestionSourceChangedError("题目来源信息不完整，请重新查题。")
        if self.questions.build_id and card.build_id != self.questions.build_id:
            raise QuestionSourceChangedError("题目库已更新，请重新查题后再选。")
        if not self.questions.is_approved(question_id):
            raise QuestionSelectionInvalidError("这道题尚未通过审核，暂不能使用。")
        if not self.questions.verify_card(card):
            raise QuestionSourceChangedError("题目内容已变化，请重新查题。")
        return card

    def _select_question_card(
        self,
        conversation_id: str,
        request_id: str,
        source_request_id: str,
        card: QuestionCard,
    ) -> QuestionSelection:
        return self._repository.create_question_selection(
            conversation_id=conversation_id,
            request_id=request_id,
            source_request_id=source_request_id,
            card=card,
            build_id=card.build_id,
            source_version=self.questions.source_version or "",
        )

    async def _dispatch_events(
        self,
        prepared: ExecuteTurn,
        request: Request,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        if self._make_scheduler is not None:
            async for event in self._dynamic_schedule_events(prepared, request, state):
                yield event
            return

        yield TurnProgressEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            stage="dispatching",
            message="正在判断要查试卷还是辅导题目",
        )
        assert self._make_router is not None
        router = self._make_router()
        try:
            decision: IntentDecision = await asyncio.wait_for(
                router(
                    prepared.input.text,
                    active_problem=(
                        prepared.problem.statement_text[:240]
                        if prepared.problem is not None
                        else None
                    ),
                    problem_status=(
                        prepared.problem.status if prepared.problem is not None else None
                    ),
                ),
                timeout=_DISPATCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            yield self._failure_event(
                prepared,
                "MODEL_TIMEOUT",
                _DISPATCH_MESSAGE,
                state,
            )
            return
        except QueryError as exc:
            yield self._failure_event(prepared, exc.code, exc.message, state)
            return
        except Exception:
            logger.exception("会话意图判断失败")
            yield self._failure_event(
                prepared, "TUTORING_FAILED", _TUTORING_MESSAGE, state
            )
            return

        active = self._repository.get_active_problem(prepared.conversation_id)
        if decision.intent == "query":
            if self._make_query is None:
                yield self._failure_event(
                    prepared,
                    "TUTORING_FAILED",
                    "自然语言查询尚未配置。",
                    state,
                )
                return
            async for event in self._query_events(prepared, request, state):
                yield event
            return

        if decision.intent == "problem":
            if active is not None:
                async for event in self._clarify_events(
                    prepared,
                    state,
                    options=("supplement", "correction", "new_problem"),
                    message=(
                        "当前已有正在辅导的题目。请明确选择：补充当前题、"
                        "纠正题干，还是开始新题。"
                    ),
                ):
                    yield event
                return
            async for event in self._tutoring_events(
                replace(prepared, operation="problem", problem=None),
                request,
                state,
            ):
                yield event
            return

        if active is None:
            async for event in self._clarify_events(
                prepared,
                state,
                options=("query", "tutoring"),
                message="我还不确定你是要查试卷，还是要辅导一道题。",
            ):
                yield event
            return

        if decision.intent == "correction":
            async for event in self._tutoring_events(
                replace(prepared, operation="correction", problem=active),
                request,
                state,
            ):
                yield event
            return

        if decision.intent == "followup":
            if active.status != "explained":
                async for event in self._clarify_events(
                    prepared,
                    state,
                    options=("supplement", "correction"),
                    message="当前题目还没有讲解，请先补充或确认题目。",
                ):
                    yield event
                return
            async for event in self._followup_events(
                replace(prepared, operation="followup", problem=active),
                state,
            ):
                yield event
            return

        if decision.intent == "record":
            if active.status != "explained":
                async for event in self._clarify_events(
                    prepared,
                    state,
                    options=("supplement", "correction"),
                    message="当前题目尚未讲解，暂时不能记录。",
                ):
                    yield event
                return
            async for event in self._record_events(
                replace(prepared, operation="record", problem=active),
                state,
            ):
                yield event
            return

        if decision.intent == "feedback":
            if active.status != "explained":
                async for event in self._clarify_events(
                    prepared,
                    state,
                    options=("supplement", "correction"),
                    message="当前题目尚未讲解，暂时不能记录“我懂了”。",
                ):
                    yield event
                return
            async for event in self._feedback_events(
                replace(prepared, operation="feedback", problem=active),
                state,
            ):
                yield event
            return

        async for event in self._clarify_events(
            prepared,
            state,
            options=("query", "tutoring"),
            message="这句话可以理解为查试卷，也可以理解为辅导题目；请明确选择。",
        ):
            yield event

    async def _query_events(
        self,
        prepared: ExecuteTurn,
        request: Request,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        assert self._make_query is not None
        try:
            service = self._make_query()
        except Exception:
            logger.exception("查询服务初始化失败")
            yield self._failure_event(
                prepared, "QUERY_FAILED", "查询过程中发生错误，请重试。", state
            )
            return

        async for event in service.events(prepared.input.text, request):
            if isinstance(event, ProgressEvent):
                yield TurnProgressEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    stage=event.stage,
                    message=event.message,
                )
                continue

            if isinstance(event, ResultEvent):
                try:
                    self._repository.complete_turn(
                        prepared.conversation_id,
                        prepared.request_id,
                        event,
                    )
                except ConversationStorageError:
                    yield _storage_error_event(prepared)
                    return
                state.saved = True
                yield TurnResultEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    kind="query",
                    result=event,
                )
                return

            assert isinstance(event, ErrorEvent)
            try:
                self._repository.fail_turn(
                    prepared.conversation_id,
                    prepared.request_id,
                    event.code,
                    event.message,
                )
            except ConversationStorageError:
                yield _storage_error_event(prepared)
                return
            state.saved = True
            yield TurnErrorEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                code=event.code,
                message=event.message,
            )
            return

    async def _tutoring_events(
        self,
        prepared: ExecuteTurn,
        request: Request,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        if self._make_tutor is None:
            yield self._failure_event(
                prepared,
                "TUTORING_FAILED",
                self._tutoring_error or "辅导模型尚未配置。",
                state,
            )
            return

        selected = self._selected_attachments(prepared)
        timeout_event = TurnErrorEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            code="TUTORING_FAILED",
            message=_TIMEOUT_MESSAGE,
        )

        async for event in stream_events(
            lambda queue: self._produce_tutoring(
                prepared, selected, queue, state
            ),
            request,
            timeout=_TUTORING_TIMEOUT_SECONDS,
            timeout_event=timeout_event,
            is_terminal=lambda item: isinstance(
                item, (TurnResultEvent, TurnErrorEvent)
            ),
        ):
            if isinstance(event, TurnErrorEvent) and not state.saved:
                saved_event = self._failure_event(
                    prepared, event.code, event.message, state
                )
                yield saved_event
                return
            yield event

    async def _produce_tutoring(
        self,
        prepared: ExecuteTurn,
        selected: list[tuple[str, str]],
        queue: "asyncio.Queue[object]",
        state: _RunState,
    ) -> None:
        async def emit(stage: str, message: str) -> None:
            queue.put_nowait(
                TurnProgressEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    stage=stage,  # type: ignore[arg-type]
                    message=message,
                )
            )

        async def finish(payload: TutoringResult) -> None:
            queue.put_nowait(
                TurnResultEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    kind=prepared.operation,  # type: ignore[arg-type]
                    tutoring=payload,
                )
            )
            state.saved = True

        try:
            assert self._make_tutor is not None
            tutor = self._make_tutor()
            attachments = self._attachments_by_ids(
                prepared.conversation_id, selected
            )
            statement_images = load_images(
                attachments,
                root=self._attachments_root,
                roles={"statement", "supplement"},
            )
            solution_images = load_images(
                attachments, root=self._attachments_root, roles={"solution"}
            )

            source, student_answer, solution_text = self._effective_problem_fields(
                prepared
            )
            workflow = build_tutoring_workflow(tutor, emit)
            result_state = await workflow.ainvoke(
                {
                    "text": self._tutoring_input_text(prepared),
                    "statement_images": statement_images,
                    "solution_images": solution_images,
                    "student_answer": student_answer,
                    "solution_text": solution_text,
                }
            )
            understanding = result_state["understanding"]
            unclear = (
                understanding.clarity == "unclear"
                or understanding.multiple_problems
            )
            explanation = result_state.get("explanation")

            if prepared.operation == "supplement":
                assert prepared.problem is not None
                updated = UpdatedProblem(
                    statement_text=understanding.statement,
                    status="awaiting_supplement" if unclear else "explained",
                    clarity=understanding.clarity,
                    unclear_points=understanding.unclear_points,
                    uncertainties=understanding.uncertainties,
                    source=source,
                    student_answer_text=student_answer,
                    solution_text=solution_text,
                    explanation=None if unclear else explanation,
                )
                record_id = self._record_id_for(prepared.problem)
                holder: dict[str, TutoringResult] = {}

                def build_supplement(saved_problem: ProblemRecord) -> str:
                    payload = (
                        build_needs_clarification(saved_problem)
                        if unclear
                        else build_explanation_result(
                            saved_problem,
                            explanation,
                            record_id=record_id,
                        )
                    )
                    holder["payload"] = payload
                    return json.dumps(
                        payload.model_dump(mode="json"), ensure_ascii=False
                    )

                self._repository.save_supplement_turn(
                    problem=prepared.problem,
                    request_id=prepared.request_id,
                    updated=updated,
                    attachments=selected,
                    output_builder=build_supplement,
                )
                await finish(holder["payload"])
                return

            if prepared.operation == "correction" and unclear:
                assert prepared.problem is not None
                questions = [
                    f"请补充或确认：{item}"
                    for item in (
                        understanding.unclear_points
                        or ["题目中的关键条件或符号仍不清楚。"]
                    )
                ]
                payload = NeedsClarificationResult(
                    problem=build_problem_view(prepared.problem),
                    questions=questions[:5],
                    unclear_points=understanding.unclear_points,
                )
                output_json = json.dumps(
                    payload.model_dump(mode="json"),
                    ensure_ascii=False,
                )
                self._repository.complete_output_turn(
                    prepared.conversation_id,
                    prepared.request_id,
                    kind="correction",
                    output_json=output_json,
                )
                await finish(payload)
                return

            new_problem = NewProblem(
                statement_text=understanding.statement,
                status="awaiting_supplement" if unclear else "explained",
                clarity=understanding.clarity,
                unclear_points=understanding.unclear_points,
                uncertainties=understanding.uncertainties,
                source=source,
                student_answer_text=student_answer,
                solution_text=solution_text,
                explanation=None if unclear else explanation,
                root_problem_id=(
                    prepared.problem.root_problem_id
                    if prepared.operation == "correction" and prepared.problem
                    else None
                ),
                supersedes_problem_id=(
                    prepared.problem.id
                    if prepared.operation == "correction" and prepared.problem
                    else None
                ),
            )
            record_id = (
                self._record_id_for(prepared.problem)
                if prepared.operation == "correction" and prepared.problem
                else None
            )
            holder = {}

            def build_problem(saved_problem: ProblemRecord) -> str:
                payload = (
                    build_needs_clarification(saved_problem)
                    if unclear
                    else build_explanation_result(
                        saved_problem,
                        explanation,
                        record_id=record_id,
                    )
                )
                holder["payload"] = payload
                return json.dumps(
                    payload.model_dump(mode="json"), ensure_ascii=False
                )

            self._repository.save_problem_turn(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind=prepared.operation,
                problem=new_problem,
                attachments=selected,
                output_builder=build_problem,
            )
            await finish(holder["payload"])
        except QueryError as exc:
            queue.put_nowait(
                self._failure_event(prepared, exc.code, exc.message, state)
            )
        except ConversationStorageError:
            queue.put_nowait(_storage_error_event(prepared))
        except (AttachmentInvalidError, InvalidImageError, ProblemNotReadyError) as exc:
            queue.put_nowait(
                self._failure_event(
                    prepared, "TUTORING_FAILED", str(exc), state
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("辅导轮次执行失败")
            queue.put_nowait(
                self._failure_event(
                    prepared, "TUTORING_FAILED", _TUTORING_MESSAGE, state
                )
            )
        finally:
            queue.put_nowait(SENTINEL)

    async def _followup_events(
        self,
        prepared: ExecuteTurn,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        assert prepared.problem is not None
        if self._make_tutor is None:
            yield self._failure_event(
                prepared,
                "TUTORING_FAILED",
                self._tutoring_error or "辅导模型尚未配置。",
                state,
            )
            return
        try:
            recent = self._repository.get_problem_followups(prepared.problem.id)
            pairs = [
                (item["question"], item["answer"]) for item in recent[-3:]
            ]
            tutor = self._make_tutor()
            answer = await tutor.answer_followup(
                statement=prepared.problem.statement_text,
                explanation=prepared.problem.explanation,
                recent_followups=pairs,
                question=prepared.input.text,
            )
            payload = FollowupResult(
                problem_id=prepared.problem.id,
                answer=answer.answer,
                related_steps=answer.related_steps,
            )
            self._repository.save_followup_turn(
                problem=prepared.problem,
                request_id=prepared.request_id,
                question=prepared.input.text,
                answer=answer.answer,
                output_json=json.dumps(
                    payload.model_dump(mode="json"), ensure_ascii=False
                ),
            )
            state.saved = True
            yield TurnResultEvent(
                conversation_id=prepared.conversation_id,
                request_id=prepared.request_id,
                kind="followup",
                tutoring=payload,
            )
        except QueryError as exc:
            yield self._failure_event(prepared, exc.code, exc.message, state)
        except ConversationStorageError:
            yield _storage_error_event(prepared)
        except Exception:
            logger.exception("追问执行失败")
            yield self._failure_event(
                prepared, "TUTORING_FAILED", _TUTORING_MESSAGE, state
            )

    async def _record_events(
        self,
        prepared: ExecuteTurn,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        assert prepared.problem is not None
        if self._records is None:
            yield self._failure_event(
                prepared, "STORAGE_UNAVAILABLE", "错题记录服务不可用。", state
            )
            return
        existing = self._repository.get_record_by_root(
            prepared.conversation_id, prepared.problem.root_problem_id
        )
        needs_update = bool(
            existing is not None
            and existing.problem_version < prepared.problem.version
        )
        holder: dict[str, TutoringResult] = {}

        def build_output(record_row: Any, created: bool) -> str:
            summary = self._records.summary_for(record_row)
            if created:
                payload: TutoringResult = RecordCreatedResult(record=summary)
            else:
                payload = RecordExistsResult(
                    record=summary,
                    needs_update=needs_update,
                    message=(
                        "这道题已有旧版本记录，可在错题本中更新。"
                        if needs_update
                        else "这道题已经记录过了。"
                    ),
                )
            holder["payload"] = payload
            return json.dumps(payload.model_dump(mode="json"), ensure_ascii=False)

        try:
            self._records.create_for_turn(
                conversation_id=prepared.conversation_id,
                problem_id=prepared.problem.id,
                request_id=prepared.request_id,
                output_builder=build_output,
            )
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return
        except (ProblemNotFoundError, ProblemNotReadyError) as exc:
            yield self._failure_event(
                prepared, "TUTORING_FAILED", str(exc), state
            )
            return
        except Exception:
            logger.exception("创建错题记录失败")
            yield self._failure_event(
                prepared, "TUTORING_FAILED", "记录错题时发生错误，请重试。", state
            )
            return

        state.saved = True
        payload = holder.get("payload")
        if payload is None:
            yield self._failure_event(
                prepared, "TUTORING_FAILED", "记录结果不完整，请重试。", state
            )
            return
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind="record",
            tutoring=payload,
        )

    async def _feedback_events(
        self,
        prepared: ExecuteTurn,
        state: _RunState,
    ) -> AsyncIterator[TurnEvent]:
        assert prepared.problem is not None
        payload = FeedbackResult(
            problem_id=prepared.problem.id,
            message=(
                "已记录你的自述：本次已经听懂。这只代表本次反馈，"
                "不代表系统认定你已永久掌握。"
            ),
        )
        try:
            self._repository.save_feedback_turn(
                problem=prepared.problem,
                request_id=prepared.request_id,
                output_json=json.dumps(
                    payload.model_dump(mode="json"), ensure_ascii=False
                ),
            )
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return
        except Exception:
            logger.exception("保存学生反馈失败")
            yield self._failure_event(
                prepared, "TUTORING_FAILED", "记录反馈时发生错误，请重试。", state
            )
            return
        state.saved = True
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind="feedback",
            tutoring=payload,
        )

    async def _clarify_events(
        self,
        prepared: ExecuteTurn,
        state: _RunState,
        *,
        options: tuple[str, ...],
        message: str,
    ) -> AsyncIterator[TurnEvent]:
        payload = IntentClarificationResult(
            message=message,
            options=list(options),
        )
        try:
            self._repository.complete_output_turn(
                prepared.conversation_id,
                prepared.request_id,
                kind="problem",
                output_json=json.dumps(
                    payload.model_dump(mode="json"), ensure_ascii=False
                ),
            )
        except ConversationStorageError:
            yield _storage_error_event(prepared)
            return
        state.saved = True
        yield TurnResultEvent(
            conversation_id=prepared.conversation_id,
            request_id=prepared.request_id,
            kind="problem",
            tutoring=payload,
        )

    def _resolve_target_problem(
        self,
        conversation_id: str,
        active: ProblemRecord | None,
        problem_id: str | None,
        *,
        require_explained: bool,
    ) -> ProblemRecord:
        """记录/反馈/纠正可以指定历史题目；不再默认落到当前活动题。"""

        if problem_id is None:
            problem = active
            if problem is None:
                raise ProblemNotFoundError("当前没有正在辅导的题目。")
        else:
            problem = self._repository.get_problem(problem_id)
            if problem is None or problem.conversation_id != conversation_id:
                raise ProblemNotFoundError("题目不存在。")
        if require_explained and (
            problem.status != "explained" or problem.explanation is None
        ):
            raise ProblemNotReadyError("题目尚未讲解，暂时不能执行该操作。")
        return problem
