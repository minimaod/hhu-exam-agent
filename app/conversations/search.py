"""查题意图、检索、翻页与旧查询定位流程。"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from app.conversations import tool_projection
from app.conversations.graph import (
    ActionOutcome,
    ActionPlan,
)
from app.conversations.turn_types import (
    ExecuteTurn,
    _DynamicContext,
)
from app.papers.courses import normalize_course
from app.papers.locator import (
    PaperLocation,
    RANK_AMBIGUOUS,
    RANK_UNIQUE,
    describe_location,
    locate_papers_by_location,
    normalize_academic_year,
    paper_matches,
)
from app.querying.models import ResultEvent
from app.question_bank.config import MAX_BROWSE_LIMIT
from app.question_bank.models import SearchRequest
from app.questions.errors import QuestionError
from app.questions.intent import SearchIntent
from app.questions.models import QuestionSearchResult

logger = logging.getLogger("app.conversations.service")

class SearchMixin:
    """搜索意图、范围、翻页和查询结果的组织。"""

    async def _dynamic_search_questions(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "search_questions"
        emit("searching_questions", "正在检索已审核的 Python 真题")
        intent = self._intent_from_params(ctx, action)
        if not intent.course:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "请先告诉我课程，例如“找一道 Python 期末真题讲讲”。",
                needs_user=True,
            ))
        if intent.paper_unresolved:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "clarification",
                "我无法从这些条件唯一确定一份试卷，请补充年份、学期或考试类型；"
                "不会扩大到其他试卷。",
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
        exclude = action.parameters.get("exclude_question_id")
        if not exclude and action.parameters.get("exclude_previous"):
            exclude = self._source_question_id(ctx)
        result = await self._run_question_search(
            prepared, ctx, name, intent, emit,
            exclude_question_id=exclude,
        )
        if isinstance(result, ActionOutcome):
            return result
        return self._question_search_outcome(prepared, ctx, name, intent, result)

    async def _run_question_search(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        name: str,
        intent: Any,
        emit: Callable[[str, str], None],
        *,
        exclude_question_id: str | None = None,
    ) -> Any:
        """执行一次题库检索；失败时返回可以直接回传的 ``ActionOutcome``。"""

        request = SearchRequest(
            course=intent.course,
            query=intent.query,
            question_type=intent.question_type,  # type: ignore[arg-type]
            exam_type=intent.exam_type,  # type: ignore[arg-type]
            paper_id=intent.paper_id,
            number_label=intent.number_label,
            knowledge_points=list(intent.knowledge_points),
            offset=intent.offset,
            limit=intent.limit,
            preview_candidates=False,
        )
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.questions.search,
                    request,
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                ),
                timeout=20.0,
            )
        except asyncio.TimeoutError:
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "failed",
                "查题超时，请稍后重试；仍可查看或下载原卷。",
                error_code="QUESTION_SEARCH_FAILED",
                retryable=True,
            ))
        except QuestionError as exc:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "failed", exc.message, error_code=exc.code,
                retryable=exc.status_code == 503,
            ))
        if exclude_question_id and result.cards:
            # 学生要的是“另一道类似的题”，把刚做过的原题过滤掉；请求时已经
            # 多要了一条，所以过滤后仍有内容可展示。
            kept = [c for c in result.cards if c.question_id != exclude_question_id]
            if len(kept) != len(result.cards):
                result = result.model_copy(
                    update={"cards": kept, "returned": len(kept)}
                )
        return result

    def _question_search_outcome(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        name: str,
        intent: Any,
        result: QuestionSearchResult,
    ) -> ActionOutcome:
        """把一次真实检索结果变成动作结果：公开总数、本次范围与下一步。"""

        shown = self._shown_ids(ctx)
        if intent.next_card and not intent.paper_id and result.cards:
            # 相关度检索没有“位置”可翻：按已经展示过的题排除，给出下一道没看过的。
            seen = set(shown)
            fresh = [card for card in result.cards if card.question_id not in seen]
            if not fresh:
                # 不能把旧题（可能已经公开答案）回显成这次的结果。
                ctx.question_results = []
                ctx.question_card = None
                ctx.question_selection = None
                ctx.tutoring = None
                return self._action_result(prepared, ctx, ActionOutcome(
                    name,
                    "clarification",
                    "这条条件下的题我都已经给你看过了；换个条件或去掉关键词再找，我就能给你新题。",
                    needs_user=True,
                ))
            result = result.model_copy(
                update={"cards": fresh[:1], "returned": 1, "next_offset": None}
            )
        ctx.question_results = list(result.cards)
        # Do not echo an already-revealed previous selection into a fresh search.
        ctx.question_card = None
        ctx.question_selection = None
        ctx.question_source_turn = prepared.request_id
        ctx.question_result_status = result.status
        ctx.question_result_message = result.message
        if intent.number_label is not None and intent.question_type is None and len(result.cards) > 1:
            kinds_all = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
            types = "、".join(
                dict.fromkeys(kinds_all.get(card.question_type, "题目") for card in result.cards)
            )
            ctx.question_result_message = (
                f"这份试卷的{types}均有第 {intent.number_label} 题，请按题型选择下面的题目。"
            )
        if result.status == "bank_unavailable":
            return self._action_result(prepared, ctx, ActionOutcome(
                name,
                "failed",
                result.message,
                error_code="QUESTION_BANK_UNAVAILABLE",
                retryable=True,
            ))
        if result.status != "ok":
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "clarification", result.message, needs_user=True
            ))
        paper_id, paper_title = self._result_paper(result) if intent.paper_id else (None, None)
        if intent.paper_id:
            # 只有本轮真的锁定了某份试卷才记成“当前试卷”。跨卷相关度检索可能
            # 命中不同试卷的题，不能随手把其中一张记成学生选中的卷。
            ctx.question_context = {
                "paper_id": intent.paper_id,
                "paper_title": paper_title or intent.paper_id,
                "question_type": intent.question_type,
                "number_label": intent.number_label,
                "total": result.total,
                "offset": result.offset,
                "returned": result.returned,
                "next_offset": result.next_offset,
                # 记下这一页的大小：“继续”沿用同一页宽，而不是改成整卷。
                "limit": intent.limit,
                "request_id": prepared.request_id,
                "query": intent.query,
            }
        else:
            # 没有锁定试卷的相关度检索也要留下检索条件，供“换一道”继承。
            ctx.question_context = {
                "paper_id": None,
                "paper_title": None,
                "query": intent.query,
                "question_type": intent.question_type,
                "number_label": intent.number_label,
                "total": result.total,
                "offset": result.offset,
                "returned": result.returned,
                "next_offset": result.next_offset,
                "limit": intent.limit,
                "request_id": prepared.request_id,
            }
        ctx.question_context["shown_ids"] = list(
            dict.fromkeys(shown + [card.question_id for card in result.cards])
        )[-200:]

        selected = False
        if intent.auto_explain and len(result.cards) == 1:
            card = result.cards[0]
            selection = self._select_question_card(
                prepared.conversation_id,
                prepared.request_id,
                prepared.request_id,
                card,
            )
            ctx.question_selection = selection
            ctx.question_card = card
            selected = True
        range_text = self._range_text(result, intent)
        if selected:
            message = f"找到 1 道已审核真题，并已选中第 {result.cards[0].number_label} 题。"
        elif intent.number_label is not None and intent.question_type is None and len(result.cards) > 1:
            message = ctx.question_result_message or result.message
        else:
            message = range_text
        details: dict[str, Any] = {
            "status": result.status,
            "count": len(result.cards),
            "question_ids": [card.question_id for card in result.cards],
            "selected": selected,
            "total": result.total,
            "offset": result.offset,
            "returned": result.returned,
            "next_offset": result.next_offset,
            "has_more": result.next_offset is not None,
            "paper_id": paper_id,
            "paper_title": paper_title,
            "question_type": intent.question_type,
            "number_label": intent.number_label,
            # F010 C：公开题面随检索结果交给模型；不含答案正文。
            "questions": [
                tool_projection.public_question(card, statement_limit=400)
                for card in list(result.cards)[:10]
            ],
        }
        return self._action_result(prepared, ctx, ActionOutcome(
            name,
            "success",
            message,
            details,
            # 查题成功是中间事实：是否结束本轮由模型根据学生真实要求决定，
            # 不再因为“这是查题动作”就一律要求先作答。
            needs_user=False,
        ))

    def _exhausted_text(self, ctx: _DynamicContext, intent: Any) -> str:
        """这一条件下已经全部展示过：说明范围，而不是从第 1 道重放。"""

        context = self._question_context(ctx)
        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        kind = kinds.get(context.get("question_type") or "", "")
        total = context.get("total")
        title = context.get("paper_title") or "这份试卷"
        scope = f"{title}的{kind}" if kind else f"{title}的题目"
        if total:
            return f"{scope}已经全部列完了（共 {total} 道），没有更多没显示的题。"
        return f"{scope}已经全部列完了，没有更多没显示的题。"

    def _last_search_filter(self, ctx: _DynamicContext) -> dict[str, Any]:
        """上一次成功检索的条件（关键词、题型），供“换一道”继承。"""

        repository = getattr(self, "_repository", None)
        if repository is None:  # 隔离替身没有仓库
            return {}
        try:
            stored = repository.find_question_context(
                ctx.conversation_id, require_paper=False
            )
        except Exception:  # pragma: no cover - 读取失败不影响本轮
            logger.warning("读取上次检索条件失败", exc_info=True)
            return {}
        return dict(stored or {})

    def _shown_ids(self, ctx: _DynamicContext) -> list[str]:
        """本会话已经展示过的题：最近几轮的题卡 + 本轮已返回的题。

        “换一道”要靠它排除旧题；旧会话也能从题卡本身恢复，不依赖新增字段。
        """

        ids = [str(item) for item in (self._question_context(ctx).get("shown_ids") or [])]
        ids.extend(card.question_id for card in ctx.question_results)
        repository = getattr(self, "_repository", None)
        if repository is not None:
            try:
                for turn in repository.list_turns(ctx.conversation_id)[-4:]:
                    ids.extend(card.question_id for card in turn.question_results)
            except Exception:  # pragma: no cover - 读取失败不影响本轮
                logger.warning("读取已展示题目失败", exc_info=True)
        return list(dict.fromkeys(ids))[-200:]

    @staticmethod
    def _result_paper(result: QuestionSearchResult) -> tuple[str | None, str | None]:
        if result.paper_id:
            return result.paper_id, result.paper_title
        return None, None

    def _range_text(self, result: QuestionSearchResult, intent: Any) -> str:
        """“共 N 道，本次第 a–b 道，还有多少没显示”。"""

        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        paper_title = result.paper_title if intent.paper_id else None
        scope = f"{paper_title}的" if paper_title else ""
        kind = kinds.get(intent.question_type or "", "")
        label = f"{scope}{kind}" if kind else (f"{scope}题目" if paper_title else "已审核真题")
        total = result.total if result.total is not None else len(result.cards)
        if intent.query:
            scope = f"《{paper_title or intent.paper_id}》" if intent.paper_id else f"{intent.course or '当前课程'}题库"
            return (
                f"在{scope}中按“{intent.query}”查找{kind or '真题'}，本次展示 {len(result.cards)} 道。"
                "这是按相关性选出的题目，不表示已经列出所有相关题。"
            )
        if intent.number_label is not None:
            if intent.question_type is None and len(result.cards) > 1:
                kinds_all = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
                types = "、".join(
                    dict.fromkeys(kinds_all.get(card.question_type, "题目") for card in result.cards)
                )
                return f"这份试卷的{types}均有第 {intent.number_label} 题，请按题型选择下面的题目。"
            numbers = "、".join(f"第 {card.number_label} 题" for card in result.cards)
            return f"已定位到{label}{numbers}，共 {len(result.cards)} 道。"
        start = result.offset + 1
        end = result.offset + result.returned
        if total <= 0:
            return f"{label}没有符合条件的已审核题目。"
        text = f"{label}共 {total} 道，本次显示第 {start}–{end} 道。"
        if result.next_offset is not None:
            text += (
                f"还有 {total - end} 道没有显示，想接着看说“继续”即可。"
            )
        else:
            text += "已经全部列出。"
        return text

    def _intent_from_params(
        self, ctx: _DynamicContext, action: ActionPlan
    ) -> SearchIntent:
        """只从工具参数与工作区事实构造检索目标，不读取学生原话。"""

        params = dict(action.parameters)
        context = self._question_context(ctx, trace=True)
        current_paper = context.get("paper_id")
        scope = str(params.get("scope") or "current")
        paper_id = params.get("paper_id")
        if scope == "bank":
            paper_id = None
            unresolved = False
        elif paper_id is None:
            paper_id = current_paper if scope == "current" else None
            unresolved = scope == "paper" and paper_id is None
        else:
            unresolved = False

        year = (
            normalize_academic_year(str(params.get("academic_year")))
            if params.get("academic_year")
            else None
        )
        term = str(params.get("term")) if params.get("term") else None
        variant = str(params.get("variant")) if params.get("variant") else None
        if year or term or variant:
            location = PaperLocation(
                year=year,
                term=term,
                variant=variant,
                exam_type=params.get("exam_type"),
                exam_type_stated=bool(params.get("exam_type")),
            )
            papers = None
            if self._papers is not None:
                try:
                    papers = self._papers.list_papers(
                        course=(
                            normalize_course(str(params.get("course")))
                            if params.get("course")
                            else self._course_for_paper(str(paper_id) if paper_id else None)
                        ),
                        exam_type=params.get("exam_type"),
                    )
                except Exception:
                    papers = None
            if papers is not None:
                match = locate_papers_by_location(
                    papers.items, exam_type=location.exam_type, location=location
                )
                if match.rank == RANK_UNIQUE:
                    paper_id = str(getattr(match.paper, "id", "")) or paper_id
                else:
                    paper_id = None
                    unresolved = True

        course = normalize_course(str(params.get("course") or "")) if params.get("course") else None
        if course is None:
            course = normalize_course(ctx.confirmed_course) if ctx.confirmed_course else None
        if course is None:
            course = self._course_for_paper(paper_id or current_paper)
        if course and paper_id and self._course_for_paper(paper_id) not in (None, course):
            paper_id = None
            unresolved = unresolved or scope == "paper"

        question_type = params.get("question_type")
        query = str(params.get("query") or "").strip()
        number_label = params.get("number_label")
        number_label = str(number_label) if number_label is not None else None
        offset = 0
        page_exhausted = False
        page_error: str | None = None
        continue_page = bool(params.get("continue_from_previous"))
        explicit_offset = params.get("offset")
        if continue_page and explicit_offset is not None:
            page_error = "offset 与 continue_from_previous 只能选一个。"
        elif continue_page:
            if not context or not context.get("request_id"):
                page_error = "当前没有可续的查询，请先列出一页题目。"
            else:
                if paper_id is None:
                    paper_id = context.get("paper_id")
                if question_type is None:
                    question_type = context.get("question_type")
                if not query and context.get("query"):
                    query = str(context.get("query") or "")
                changed = (
                    (paper_id or None) != (context.get("paper_id") or None)
                    or (question_type or None) != (context.get("question_type") or None)
                    or query != str(context.get("query") or "")
                )
                if changed:
                    page_error = "查询条件已经变化，不能沿用上一页的位置；请确认是否从第一页重新看。"
            if page_error is None:
                next_offset = context.get("next_offset")
                if next_offset is None:
                    page_exhausted = True
                else:
                    offset = int(next_offset)
        elif explicit_offset is not None:
            offset = int(explicit_offset)

        limit = params.get("limit")
        if limit is None and continue_page and context.get("limit"):
            limit = int(context["limit"])
        if limit is None:
            # 整卷浏览的默认仍是一次列全，保持旧卷面体验；跨卷检索才用较小页宽。
            limit = 40 if action.action == "list_paper_questions" else (
                MAX_BROWSE_LIMIT if paper_id else 3
            )

        knowledge_points = params.get("knowledge_points") or ()
        auto_explain = bool(params.get("auto_explain")) and not bool(params.get("defer_answer"))
        return SearchIntent(
            course=course,
            query=query,
            question_type=question_type,
            exam_type=params.get("exam_type"),
            paper_id=str(paper_id) if paper_id else None,
            number_label=number_label,
            knowledge_points=tuple(str(item) for item in knowledge_points),
            limit=max(1, min(int(limit), 64)),
            offset=max(0, offset),
            auto_explain=auto_explain,
            next_card=bool(params.get("exclude_previous")),
            page_exhausted=page_exhausted,
            page_error=page_error,
            paper_unresolved=unresolved,
        )

    def _dynamic_query(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "query_papers"
        params = dict(action.parameters)
        course = normalize_course(str(params.get("course") or "")) if params.get("course") else None
        if course is None and ctx.confirmed_course:
            course = normalize_course(ctx.confirmed_course)
        if not course:
            return ActionOutcome(
                name,
                "clarification",
                "请先告诉我复习哪门课程，我才能查找对应试卷。",
                needs_user=True,
            )
        ctx.confirmed_course = course
        if self._papers is None:
            return ActionOutcome(name, "failed", "试卷目录暂时不可用，请稍后重试。", error_code="CATALOG_UNAVAILABLE", retryable=True)

        all_exam_types = bool(params.get("all_exam_types"))
        exam_type = None if all_exam_types else params.get("exam_type")
        emit("retrieving", "正在检索试卷资料")
        try:
            papers = self._papers.list_papers(course=course, exam_type=exam_type)
        except Exception as exc:
            logger.warning("查卷执行失败：%s", exc)
            return ActionOutcome(name, "failed", "试卷检索失败，请稍后重试。", error_code="QUERY_FAILED", retryable=True)

        year = normalize_academic_year(str(params.get("academic_year"))) if params.get("academic_year") else None
        term = str(params.get("term")) if params.get("term") else None
        variant = str(params.get("variant")) if params.get("variant") else None
        location = PaperLocation(
            year=year,
            term=term,
            variant=variant,
            exam_type=exam_type,
            exam_type_stated=bool(exam_type),
        )
        candidates: list[str] | None = None
        state = self._paper_state(ctx)
        if (
            state is not None
            and state.get("status") == "clarify_paper"
            and not (year or term)
        ):
            location, candidates = self._merge_clarification(
                state, location, course, clear_exam_type=all_exam_types
            )

        param_paper = params.get("paper_id")
        if param_paper:
            chosen = next(
                (item for item in papers.items if str(getattr(item, "id", "")) == str(param_paper)),
                None,
            )
            if chosen is None:
                return ActionOutcome(
                    name,
                    "clarification",
                    "目录里没有这份试卷，请确认年份、学期和卷别。",
                    needs_user=True,
                    error_code="NO_SUCH_PAPER",
                )
            if location.explicit and not paper_matches(chosen, location, include_exam_type=False):
                # 学生对学年/学期/卷别给了明确新条件：旧 paper_id 视为过期。
                ctx.last_paper_switch_override = {
                    "old_paper_id": str(param_paper),
                    "academic_year": year,
                    "term": term,
                    "variant": variant,
                }
            else:
                return self._adopt_located_paper(
                    prepared, ctx, name, course, chosen, location
                )

        if location.explicit:
            return self._located_query(
                prepared, ctx, name, course, papers, location, candidates
            )

        scope = {None: "全部类型", "midterm": "期中", "final": "期末", "other": "其他类型"}[exam_type]
        if papers.items:
            ctx.result = ResultEvent(
                type="result",
                kind="found",
                message=f"找到 {papers.total} 份【{course}】试卷（{scope}）。",
                filters={"course": course, "exam_type": exam_type},
                items=papers.items,
                total=papers.total,
            )
        else:
            ctx.result = ResultEvent(
                type="result",
                kind="empty",
                message=(
                    f"本次按{scope}筛选，未找到【{course}】匹配的本地试卷，但您可以继续拍照或输入题目"
                    "进行辅导与考点练习。"
                ),
                filters={"course": course, "exam_type": exam_type},
                items=[],
                total=0,
            )
        details = {
            "total": papers.total,
            "course": course,
            "exam_type": exam_type,
            "papers": [{"paper_id": item.id, "title": item.title,
                        "academic_year": item.academic_year, "exam_type": item.exam_type}
                       for item in papers.items],
        }
        if getattr(ctx, "last_paper_switch_override", None):
            details["paper_switch_override"] = ctx.last_paper_switch_override
        return ActionOutcome(name, "success", ctx.result.message, details)

    def _merge_clarification(
        self,
        state: dict[str, Any],
        location: PaperLocation,
        course: str | None = None,
        *,
        clear_exam_type: bool = False,
    ) -> tuple[PaperLocation, list[str] | None]:
        """把上一轮的待澄清条件合并进本轮；换了课程或条件就作废旧候选。"""

        state_course = state.get("course")
        if (
            state_course
            and course
            and normalize_course(state_course) != normalize_course(course)
        ):
            return location, None
        if location.year or location.term:
            return location, None
        if clear_exam_type:
            return (
                PaperLocation(
                    year=location.year or state.get("academic_year"),
                    term=location.term or state.get("term"),
                    variant=location.variant,
                    exam_type=None,
                    exam_type_stated=False,
                ),
                None,
            )
        stated_exam = location.exam_type if location.exam_type_stated else None
        if stated_exam and stated_exam != state.get("exam_type"):
            return (
                PaperLocation(
                    year=location.year or state.get("academic_year"),
                    term=location.term or state.get("term"),
                    variant=location.variant,
                    exam_type=stated_exam,
                    exam_type_stated=True,
                ),
                None,
            )
        merged = PaperLocation(
            year=location.year or state.get("academic_year"),
            term=location.term or state.get("term"),
            variant=location.variant,
            exam_type=(
                location.exam_type
                if location.exam_type_stated
                else (state.get("exam_type") or location.exam_type)
            ),
            exam_type_stated=location.exam_type_stated or bool(state.get("exam_type")),
        )
        candidates = [str(item) for item in (state.get("candidates") or []) if item]
        return merged, candidates or None

    def _located_query(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        name: str,
        course: str,
        papers: Any,
        location: PaperLocation,
        candidates: list[str] | None = None,
    ) -> ActionOutcome:
        """按学年/学期/卷别精确落到一份试卷；0 份与多份都如实返回。"""

        scope = describe_location(location)
        pool = [
            item
            for item in papers.items
            if not candidates or str(getattr(item, "id", "")) in candidates
        ]
        match = locate_papers_by_location(
            pool, exam_type=location.exam_type, location=location
        )
        if match.rank == RANK_UNIQUE:
            return self._adopt_located_paper(prepared, ctx, name, course, match.paper, location)
        if match.rank == RANK_AMBIGUOUS:
            options = "、".join(f"《{title}》" for title in match.titles[:5])
            ctx.result = None
            # 保存待澄清条件与真实候选：下一轮只答“B卷”也能落回这个范围。
            ctx.question_context = {
                "status": "clarify_paper",
                "course": course,
                "academic_year": location.year,
                "term": location.term,
                "exam_type": location.exam_type,
                "candidates": [
                    str(getattr(item, "id", "")) for item in match.candidates
                ],
                # F010 B：工作区要显示卷名，不能只给 ID（旧记录没有该字段时回退）。
                "candidate_titles": [
                    str(getattr(item, "title", "") or "") for item in match.candidates
                ],
                "request_id": prepared.request_id,
            }
            return ActionOutcome(
                name,
                "clarification",
                f"{scope}对应 {len(match.candidates)} 份试卷，请说明要哪一份"
                + (f"：{options}" if options else "")
                + "。",
                {"status": "ambiguous"},
                needs_user=True,
            )
        # RANK_NONE：条件明确但目录里没有这一份，不能扩大范围。
        # 这里必须留下持久状态：只清空本轮上下文的话，下一轮指代会从历史里
        # 读回上一份成功定位的旧卷（§7.7 R1）。
        ctx.question_context = {
            "status": "unlocated",
            "course": course,
            "academic_year": location.year,
            "term": location.term,
            "exam_type": location.exam_type,
            "request_id": prepared.request_id,
        }
        ctx.result = ResultEvent(
            type="result",
            kind="empty",
            message=(
                f"本地试卷目录里没有【{course}】{scope}的登记记录。"
                "不会自动扩大到其他学年或学期；如果考试类型不同，可以说清是期中还是期末。"
            ),
            filters={"course": course, "exam_type": location.exam_type},
            items=[],
            total=0,
        )
        return ActionOutcome(
            name,
            "success",
            ctx.result.message,
            {
                "status": "empty",
                "total": 0,
                "course": course,
                "exam_type": location.exam_type,
                "academic_year": location.year,
                "term": location.term,
                "papers": [],
            },
        )

    def _adopt_located_paper(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        name: str,
        course: str,
        paper: Any,
        location: PaperLocation,
    ) -> ActionOutcome:
        """唯一命中：只展示这一份，并把试卷身份存进上下文供后续指代。"""

        title = str(getattr(paper, "title", "") or "")
        paper_id = str(getattr(paper, "id", "") or "")
        download_url = getattr(paper, "download_url", None) or (
            f"/api/papers/{paper_id}/download"
        )
        year = str(getattr(paper, "academic_year", "") or "")
        term = location.term
        term_label = {"s1": "第一学期", "s2": "第二学期"}.get(term or "", "")
        if year and year in title and (not term_label or term_label in title):
            # 目录卷名本身已带学年学期（如“2024-2025学年第一学期《…》(A卷)”），
            # 再套一层会出现“《…《Python语言程序设计》…》”这种嵌套书名号。
            message = f"已找到{title}，下载入口已备好。"
        else:
            condition = f"【{course}】" + (f"{year}学年" if year else "") + term_label
            message = f"已找到{condition}的《{title}》，下载入口已备好。"
        ctx.result = ResultEvent(
            type="result",
            kind="found",
            message=message,
            filters={"course": course, "exam_type": getattr(paper, "exam_type", None)},
            items=[paper],
            total=1,
        )
        # 唯一命中就是“当前试卷”：“这张卷的选择题”要继承它，而不是再问一遍年份。
        ctx.question_context = {
            "paper_id": paper_id,
            "paper_title": title,
            "question_type": None,
            "number_label": None,
            "total": None,
            "offset": 0,
            "returned": 0,
            "next_offset": 0,
            "limit": 64,
            "request_id": prepared.request_id,
            "query": "",
        }
        return ActionOutcome(
            name,
            "success",
            message,
            {
                "status": "found",
                "total": 1,
                "course": course,
                "paper_id": paper_id,
                "paper_title": title,
                "download_url": download_url,
                "academic_year": year,
                "term": term,
                "exam_type": getattr(paper, "exam_type", None),
            },
        )
