"""动态上下文：加载、快照、恢复、最近轮次与题目状态。"""

import logging
from contextlib import suppress
from typing import Any

from app.conversations.errors import ProblemNotFoundError
from app.conversations.history import build_history, project_turn
from app.conversations.models import (
    ExerciseEvaluation,
    ExerciseView,
    Turn,
)
from app.conversations.repository import ProblemRecord
from app.conversations.turn_types import _DynamicContext
from app.papers.courses import normalize_course
from app.querying.models import ResultEvent
from app.questions.models import (
    QuestionCard,
    QuestionSelection,
)
from app.tutoring.models import TutoringResult

logger = logging.getLogger("app.conversations.service")


def _summary_clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


class DynamicContextMixin:
    """一轮执行所需的上下文读取与恢复。"""

    def _load_dynamic_context(self, conversation_id: str) -> _DynamicContext:
        ctx_record = self._repository.get_context(conversation_id)
        active_problem = self._repository.get_active_problem(conversation_id)
        active_exercise = self._repository.get_active_exercise(conversation_id)
        selection = self._repository.get_active_question(conversation_id)
        latest_question = self._latest_question_turn(conversation_id)
        return _DynamicContext(
            conversation_id=conversation_id,
            confirmed_course=ctx_record.confirmed_course if ctx_record else None,
            active_problem=active_problem,
            active_exercise=active_exercise,
            problem_id=active_problem.id if active_problem is not None else None,
            exercise_id=active_exercise.id if active_exercise is not None else None,
            question_selection=selection,
            question_card=(
                self.questions.card_for_selection(selection)
                if selection is not None
                else None
            ),
            question_results=(
                list(latest_question.question_results) if latest_question else []
            ),
            question_source_turn=(
                latest_question.request_id if latest_question else None
            ),
        )

    @staticmethod
    def _context_snapshot(ctx: _DynamicContext) -> dict[str, Any]:
        return {"confirmed_course": ctx.confirmed_course, "problem_id": ctx.problem_id,
                "question_context": ctx.question_context,
                "exercise_id": ctx.exercise_id,
                "question": {
                    "selection_id": (
                        ctx.question_selection.id
                        if ctx.question_selection is not None
                        else None
                    ),
                    "source_turn": ctx.question_source_turn,
                    "status": ctx.question_result_status,
                    "message": ctx.question_result_message,
                    "cards": [card.public_snapshot() for card in ctx.question_results],
                },
                **{name: getattr(ctx, name).model_dump(mode="json") if getattr(ctx, name) is not None else None
                   for name in ("result", "tutoring", "exercise_view", "evaluation_view")}}

    def _restore_dynamic_context(self, conversation_id: str, snapshot: dict) -> _DynamicContext:
        from pydantic import TypeAdapter
        problem_id, exercise_id = snapshot.get("problem_id"), snapshot.get("exercise_id")
        problem = self._repository.get_problem(problem_id) if problem_id else None
        exercise = self._repository.get_exercise(exercise_id) if exercise_id else None
        if ((problem is not None and problem.conversation_id != conversation_id)
            or (exercise is not None and exercise.conversation_id != conversation_id)):
            raise ProblemNotFoundError("恢复目标题目不属于当前会话。")
        question = snapshot.get("question") or {}
        cards: list[QuestionCard] = []
        for item in question.get("cards") or []:
            with suppress(Exception):
                cards.append(QuestionCard.model_validate(item))
        selection: QuestionSelection | None = None
        selection_id = question.get("selection_id")
        if selection_id:
            with suppress(Exception):
                selection = self._repository.get_question_selection_by_id(
                    conversation_id, str(selection_id)
                )
        if selection is None:
            selection = self._repository.get_active_question(conversation_id)
        return _DynamicContext(
            conversation_id=conversation_id, confirmed_course=snapshot.get("confirmed_course"),
            active_problem=problem, active_exercise=exercise, problem_id=problem_id, exercise_id=exercise_id,
            question_selection=selection,
            question_card=(
                self.questions.card_for_selection(selection)
                if selection is not None
                else (cards[0] if cards else None)
            ),
            question_results=cards,
            question_source_turn=question.get("source_turn"),
            question_result_status=question.get("status"),
            question_result_message=question.get("message"),
            question_context=dict(snapshot.get("question_context") or {}),
            result=ResultEvent.model_validate(snapshot["result"]) if snapshot.get("result") else None,
            tutoring=TypeAdapter(TutoringResult).validate_python(snapshot["tutoring"]) if snapshot.get("tutoring") else None,
            exercise_view=ExerciseView.model_validate(snapshot["exercise_view"]) if snapshot.get("exercise_view") else None,
            evaluation_view=ExerciseEvaluation.model_validate(snapshot["evaluation_view"]) if snapshot.get("evaluation_view") else None,
        )

    def _recent_turns_summary(self, conversation_id: str) -> str:
        """最近四轮摘要；必须与历史投影同源，不能回传原始助手正文。

        摘要同时进调度提示和正文材料。上一轮合法展示的答案或练习解答，
        在原始 ``assistant_message`` 里是可读正文；这里统一走
        ``project_turn``，只保留授权事实或结构化占位。
        """

        turns = [
            turn
            for turn in self._repository.list_turns(conversation_id)
            if turn.status == "completed"
        ]
        if not turns:
            return ""
        transcripts = self._repository.list_turn_transcripts(conversation_id)
        summary_lines: list[str] = []
        for turn in turns[-4:]:
            block = project_turn(turn, transcripts.get(turn.request_id))
            if block is None:
                continue
            summary_lines.append(f"用户：{_summary_clip(block.get('query'), 200)}")
            if block.get("assistant"):
                summary_lines.append(
                    f"助手：{_summary_clip(block['assistant'], 300)}"
                )
        return "\n".join(summary_lines)

    def _question_context(self, ctx: _DynamicContext, *, trace: bool = False) -> dict[str, Any]:
        """本轮已确定的试卷/位置；本轮没有就用库里最近一次成功的记录。"""

        if ctx.question_context:
            return dict(ctx.question_context)
        state = self._paper_state(ctx)
        if state is not None:
            # 明确换卷失败或正在等学生补充卷别：不能用更早的成功记录补位，
            # 否则“这张卷”会静默指回旧卷（§7.7 R1）。
            return dict(state) if state.get("status") == "unlocated" else {}
        try:
            stored = self._repository.find_question_context(ctx.conversation_id, require_paper=False)
        except Exception:  # pragma: no cover - 读取失败不影响本轮
            logger.warning("读取试卷上下文失败", exc_info=True)
            stored = None
        if not stored:
            # 旧会话没有 question_context：从历史题卡恢复“这是哪份卷”。
            stored = self._context_from_cards(ctx.conversation_id)
            if stored and trace:
                logger.debug("由历史题卡恢复试卷上下文：%s", stored.get("paper_id"))
        elif trace:
            logger.debug("沿用上一次试卷上下文：%s", stored.get("paper_id"))
        return dict(stored or {})

    def _paper_state(self, ctx: _DynamicContext) -> dict[str, Any] | None:
        """本轮或最近的“未定位 / 待澄清”状态；没有则返回 None。"""

        raw = ctx.question_context or {}
        if raw.get("status") in {"unlocated", "clarify_paper"}:
            return dict(raw)
        repository = getattr(self, "_repository", None)
        if repository is None:
            return None
        try:
            return repository.find_paper_state(ctx.conversation_id)
        except Exception:  # pragma: no cover - 读取失败不影响本轮
            logger.warning("读取试卷状态失败", exc_info=True)
            return None

    def _context_from_cards(self, conversation_id: str) -> dict[str, Any] | None:
        """返修前的记录只有题卡：能唯一确定试卷时补出试卷上下文。"""

        if getattr(self, "_repository", None) is None:  # 隔离替身没有仓库
            return None
        turn = self._latest_question_turn(conversation_id)
        if turn is None or not turn.question_results:
            return None
        papers = {
            str(card.source.paper_id)
            for card in turn.question_results
            if card.source.paper_id
        }
        if len(papers) != 1:
            # 多份试卷混在一起时不猜，宁可让学生说明。
            return None
        paper_id = papers.pop()
        types = {card.question_type for card in turn.question_results}
        title = next(
            (card.source.paper_title for card in turn.question_results if card.source.paper_title),
            None,
        )
        shown = [card.question_id for card in turn.question_results]
        return {
            "paper_id": paper_id,
            "paper_title": title,
            "question_type": types.pop() if len(types) == 1 else None,
            "number_label": None,
            # 历史卡片只证明“已经展示过这么多”，不能证明总量或是否翻完；
            # total 留空，继续时从已展示条数往后查，由真实检索给出总数。
            "total": None,
            "offset": 0,
            "returned": len(shown),
            "next_offset": len(shown),
            "limit": len(shown),
            "request_id": turn.request_id,
            "shown_ids": shown,
            # 由历史题卡推断出来的上下文：只有学生的话指向“这份卷”时才沿用，
            # 否则一次普通关键词检索会被旧试卷绑住。
            "derived": True,
        }

    def _question_candidates(
        self, ctx: _DynamicContext
    ) -> tuple[list[QuestionCard], str | None]:
        if ctx.question_results and ctx.question_source_turn:
            return list(ctx.question_results), ctx.question_source_turn
        turn = self._latest_question_turn(ctx.conversation_id)
        if turn is None:
            return [], None
        ctx.question_results = list(turn.question_results)
        ctx.question_source_turn = turn.request_id
        return list(turn.question_results), turn.request_id

    def _latest_question_turn(self, conversation_id: str) -> Turn | None:
        for turn in reversed(self._repository.list_turns(conversation_id)):
            if turn.question_results:
                return turn
        return None

    def _history_for(
        self, conversation_id: str, current_request_id: str
    ) -> list[dict[str, Any]]:
        """跨轮消息历史：只读投影，同一请求重放时结果完全一致。"""

        turns = self._repository.list_turns(conversation_id)
        transcripts = self._repository.list_turn_transcripts(conversation_id)
        return build_history(
            turns, transcripts, current_request_id=current_request_id
        )

    def _source_question_id(self, ctx: _DynamicContext) -> str | None:
        """学生此刻在看的题：优先选中的题，否则用当前页码/卷内的那张卡。"""

        if ctx.question_card is not None:
            return ctx.question_card.question_id
        pool = list(ctx.question_results)
        if not pool:
            shown = self._question_context(ctx).get("shown_ids") or []
            return str(shown[0]) if shown else None
        context = self._question_context(ctx)
        number = context.get("number_label")
        if number:
            for card in pool:
                if card.number_label == str(number):
                    return card.question_id
        return pool[0].question_id

    def _course_for_paper(self, paper_id: str | None) -> str | None:
        """按试卷反查课程；目录不可用时返回 None。"""

        if not paper_id or self._papers is None:
            return None
        try:
            paper = self._papers.get_paper(paper_id)
        except Exception:
            logger.warning("按试卷反查课程失败", exc_info=True)
            return None
        return normalize_course(paper.course) if paper is not None else None

    def _question_operation_problem(self, ctx: _DynamicContext) -> ProblemRecord | None:
        """自然语言和卡片动作都跟随持久选题；未讲解时绝不退回旧题。"""
        selection = self._repository.get_active_question(ctx.conversation_id)
        ctx.question_selection = selection
        if selection is None:
            ctx.question_card = None
            return ctx.active_problem
        ctx.question_card = self.questions.card_for_selection(selection)
        problem = (
            self._repository.get_problem(selection.problem_id) if selection.problem_id else None
        )
        if problem is not None and problem.conversation_id != ctx.conversation_id:
            problem = None
        ctx.active_problem = problem
        ctx.problem_id = problem.id if problem is not None else None
        return problem
