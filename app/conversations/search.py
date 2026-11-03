"""查题意图、检索、翻页与旧查询定位流程。"""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

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
    cancels_exam_type,
    describe_location,
    extract_location,
    locate_papers,
)
from app.querying.models import ResultEvent
from app.question_bank.models import SearchRequest
from app.questions.errors import QuestionError
from app.questions.intent import (
    SearchIntent,
    asks_across_papers,
    extract_intent,
    question_answer_requested,
    wants_next_card,
)
from app.questions.models import QuestionSearchResult

logger = logging.getLogger("app.conversations.service")

_PAGE_WORDS = re.compile(
    r"继续|接着|下一批|下一页|下一部分|剩下的|其余的|后面那些|还没(看|显示|列)|"
    r"换一道|换一题|换一个|换个题|换道题|再来一道|再换一道|不要这道|别来这道|下一道|下一个题|"
    r"选择题|填空题|编程题|判断题|计算题|简答题|问答题|大题|小题|运算题|"
    r"给我|再看|再列|再显示|看看|一下|这些|那些|这|那|份|张|道|题|卷|个|的|吧|呢|啊|呀|了|我|你|它|"
    r"再|还|都|全|列|显示|看|来|出|给|要|有|几"
)

def _page_topic_words(text: str) -> str:
    """去掉翻页/客套词后剩下的话题约束；空表示这一轮只是翻页。"""

    return _PAGE_WORDS.sub("", text or "").strip()

_SIMILAR_QUESTION_RE = re.compile(r"类似|相似|同类型|同一类|换一道|另一道|别的题|其他题|其它题")

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
        intent = self._search_intent(prepared.input.text, ctx, action)
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
        if intent.page_exhausted:
            return self._action_result(prepared, ctx, ActionOutcome(
                name, "clarification", self._exhausted_text(ctx, intent), needs_user=True
            ))
        result = await self._run_question_search(
            prepared, ctx, name, intent, emit,
            exclude_question_id=self._similar_source_question(prepared.input.text, ctx),
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
                "request_id": prepared.request_id,
            }
        ctx.question_context["shown_ids"] = list(
            dict.fromkeys(shown + [card.question_id for card in result.cards])
        )[-200:]

        selected = False
        wants_explain = self._wants_explanation(prepared.input.text) or question_answer_requested(
            prepared.input.text
        )
        if intent.auto_explain and wants_explain and len(result.cards) == 1:
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

    def _search_intent(
        self, text: str, ctx: _DynamicContext, action: ActionPlan
    ) -> SearchIntent:
        papers: list[dict[str, Any]] = []
        if self._papers is not None:
            try:
                papers = [
                    item.model_dump()
                    for item in self._papers.list_papers(course="Python语言程序设计").items
                ]
            except Exception:
                logger.warning("读取试卷目录失败，查题不带试卷筛选", exc_info=True)
        confirmed = normalize_course(ctx.confirmed_course) if ctx.confirmed_course else None
        context = self._question_context(ctx, trace=True)
        current_paper = context.get("paper_id")
        if context.get("derived") and not self._refers_to_current_paper(text):
            # 旧会话里只有题卡时，不能把任意一次查题都算成“这张卷”。
            current_paper = None
        parameters = dict(action.parameters)
        # 明确跨卷与“沿用当前卷”互斥：先删模型带来的旧 ID，“这道题”之类的
        # 指代也不能把旧卷重新写回去（见 §7.7 R3）。
        cross_paper = asks_across_papers(text)
        if cross_paper:
            parameters.pop("paper_id", None)
        # 最近成功浏览是指代依据；旧选题和模型带来的旧卷 ID 都不能覆盖它。
        # 新年份/学期等显式条件仍由 extract_intent 优先核对。
        if current_paper and not cross_paper and self._refers_to_current_paper(text):
            parameters["paper_id"] = current_paper
        explicit = extract_intent(text, confirmed_course=confirmed, papers=papers)
        if explicit.paper_id:
            # 明确年份/学期/卷名能唯一定位时，在共用入口先消除旧 ID；
            # 底层解析器仍保留“条件冲突必须澄清”的校验契约。
            parameters["paper_id"] = explicit.paper_id
        if parameters.get("paper_id"):
            # 模型给的试卷 ID 必须真实存在；不存在就当没给，由上下文或原文决定。
            known = {str(item.get("id")) for item in papers}
            if str(parameters["paper_id"]) not in known:
                parameters.pop("paper_id")
        if parameters.get("offset") is None:
            parameters.pop("offset", None)
        intent = extract_intent(
            text,
            confirmed_course=confirmed,
            planned=parameters,
            papers=papers,
            current_paper_id=current_paper,
        )
        course = normalize_course(intent.course) if intent.course else None
        if course is None:
            # 学生说“这张卷/继续”时不会重复课程名：沿用已确认课程，或按当前
            # 试卷所属课程补齐，避免工具因为没课程而要求重复信息。
            course = confirmed or self._course_for_paper(current_paper)
        # 明确换了课程（例如从 Python 改问高数）时，不能带着旧试卷 ID。
        if course and intent.paper_id and self._course_for_paper(intent.paper_id) not in (None, course):
            intent = replace(intent, paper_id=None)
        if course and not intent.paper_id and current_paper:
            if self._course_for_paper(current_paper) not in (None, course):
                current_paper = None
                ctx.question_context = {}
        query = re.sub(
            r"(来|找|出|给|查|搜)(一道|一题|道题|个题)|一道|一题|真题|题目|讲讲|讲解|分析|练练|练习|一下",
            " ",
            intent.query,
        )
        query = re.sub(r"\s+", " ", query).strip()[:120]
        # “直接讲解”只由学生的话决定：模型可以建议，但不能替学生开启讲解。
        auto = bool(parameters.get("auto_explain") or action.parameters.get("auto_explain"))
        auto = auto and self._wants_explanation(text)
        intent = replace(intent, course=course, query=query, auto_explain=auto)
        intent = self._apply_page_position(text, intent, context, ctx)
        if self._source_question_id(ctx) is not None and (
            asks_across_papers(text) or _SIMILAR_QUESTION_RE.search(text or "")
        ):
            # 会过滤掉原题，所以多要一条，避免过滤后这一页变空。
            intent = replace(intent, limit=min(intent.limit + 1, 64))
        return intent

    def _apply_page_position(
        self,
        text: str,
        intent: SearchIntent,
        context: dict[str, Any],
        ctx: _DynamicContext,
    ) -> SearchIntent:
        """把“继续/下一批/换一道”落到正确位置，并绑定筛选条件。

        续页位置只对同一 (试卷, 题型, 无关键词) 的浏览成立：换了题型
        （例如看完选择题又问填空题）就从第 1 道重新开始，不沿用旧位置；
        已经列完时标记 ``page_exhausted``，而不是回到第 1 道重放。
        """

        wants_next = self._wants_continuation(text) or wants_next_card(text)
        if intent.next_card is False and wants_next_card(text):
            intent = replace(intent, next_card=True)
        if not wants_next or intent.number_label is not None:
            return intent
        if not _page_topic_words(text):
            # 这一轮没有新的内容约束（没说“考循环的选择题”），只是翻页：
            # 不能把“继续/下一批/换一道”当成检索关键词，否则会变成相关度检索。
            intent = replace(intent, query="")
        if intent.next_card:
            # “换一道”是换题不是换条件：沿用上一次的关键词与题型，
            # 再排除已经展示过的题。
            if not _page_topic_words(text):
                last = self._last_search_filter(ctx)
                if last.get("query") and not intent.query:
                    intent = replace(intent, query=str(last["query"]))
                if last.get("question_type") and intent.question_type is None:
                    intent = replace(intent, question_type=last["question_type"])
            if intent.paper_id is None:
                # 没有锁定试卷时要先有一小池候选，才能排除看过的、给出新题。
                intent = replace(intent, limit=max(intent.limit, 10))
        if (
            intent.question_type is None
            and context.get("paper_id")
            and context["paper_id"] == intent.paper_id
            and context.get("question_type")
        ):
            # 只说“继续”时沿用上次的题型，否则会被当成整卷而要重列一遍。
            intent = replace(intent, question_type=context["question_type"])
        if not self._same_page_filter(context, intent):
            return intent
        if intent.offset:
            return intent
        next_offset = context.get("next_offset")
        if next_offset is None:
            return replace(intent, page_exhausted=True)
        limited = intent.limit
        if context.get("limit"):
            # 沿用上一页页宽，避免“前五题”之后突然变成整卷。
            limited = min(limited, int(context["limit"])) if limited else int(context["limit"])
        return replace(intent, offset=int(next_offset), limit=limited)

    @staticmethod
    def _same_page_filter(context: dict[str, Any], intent: SearchIntent) -> bool:
        """续页只能在同一个筛选条件下成立。"""

        if not context.get("paper_id") or context["paper_id"] != intent.paper_id:
            return False
        if intent.query:
            return False
        stored_kind = context.get("question_type") or None
        return stored_kind == (intent.question_type or None)

    @staticmethod
    def _refers_to_current_paper(text: str) -> bool:
        """学生的话是否指向刚刚那份卷（而不是另起一个检索条件）。"""

        return bool(
            re.search(
                r"这张|这份|这卷|该卷|本卷|刚才|刚刚|上面|之前那|继续|下一批|下一页|剩下的|"
                r"第\s*[0-9一二三四五六七八九十]+\s*(?:道|个)?题|这道|这个题",
                text or "",
            )
        )

    @staticmethod
    def _wants_continuation(text: str) -> bool:
        return bool(
            re.search(r"继续|接着|下一批|下一页|下一部分|剩下的|其余的|后面那些|还没(看|显示|列)", text or "")
        )

    def _similar_source_question(self, text: str, ctx: _DynamicContext) -> str | None:
        """要求学生找“类似的题”时，返回应当排除的原题 ID。

        不排除的话，按相关度检索往往把学生刚做完的那道原题排在第一位，
        等于把原题又发了一遍。
        """

        if not (_SIMILAR_QUESTION_RE.search(text or "") or asks_across_papers(text)):
            return None
        return self._source_question_id(ctx)

    def _dynamic_query(
        self,
        prepared: ExecuteTurn,
        action: ActionPlan,
        ctx: _DynamicContext,
        emit: Callable[[str, str], None],
    ) -> ActionOutcome:
        name = "query_papers"
        text = prepared.input.text
        course = action.parameters.get("course") or ctx.confirmed_course
        exam_type = self._requested_exam_type(
            text, action.parameters.get("exam_type")
        )
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

        emit("retrieving", "正在检索试卷资料")
        try:
            papers = self._papers.list_papers(course=course, exam_type=exam_type)
        except Exception as exc:
            logger.warning("查卷执行失败：%s", exc)
            return ActionOutcome(name, "failed", "试卷检索失败，请稍后重试。", error_code="QUERY_FAILED", retryable=True)

        # 模型只给“课程＋考试类型”是常态（截图里的真实调用就只有这两个）：
        # 原文里的学年、学期、卷别必须由服务端自己核对，不能因为工具参数没带
        # 就静默丢掉，也不能退回全部试卷。
        location = extract_location(text)
        candidates: list[str] | None = None
        state = self._paper_state(ctx)
        if state is not None and state.get("status") == "clarify_paper":
            # 上一轮已经问过“A 卷还是 B 卷”：本轮学生只回“B卷”时，
            # 学年、学期和候选范围要接着上一轮，不能重新在全目录里找。
            location, candidates = self._merge_clarification(state, location, course, text)
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
        return ActionOutcome(
            name,
            "success",
            ctx.result.message,
            {"total": papers.total, "course": course, "exam_type": exam_type,
             "papers": [{"paper_id": item.id, "title": item.title,
                         "academic_year": item.academic_year, "exam_type": item.exam_type}
                        for item in papers.items]},
        )

    def _merge_clarification(
        self,
        state: dict[str, Any],
        location: PaperLocation,
        course: str | None = None,
        text: str | None = None,
    ) -> tuple[PaperLocation, list[str] | None]:
        """把上一轮的待澄清条件合并进本轮；换了课程或条件就作废旧候选。

        区分四种情况：
        - 换了课程：旧课程的条件和候选 ID 全部作废（§7.9 R2 场景 A）；
        - 明确取消考试类型：保留学年/学期，清掉类型限制与旧候选（§7.10 R2b）；
        - 同一课程里显式改考试类型：学年/学期可以留下，候选必须按新类型重查；
        - 只补卷别或什么都没说：沿用上一轮的学年、学期与候选范围。
        """

        state_course = state.get("course")
        if (
            state_course
            and course
            and normalize_course(state_course) != normalize_course(course)
        ):
            return location, None
        if location.year or location.term:
            # 明确改了年份或学期：旧候选不再适用，按新范围重新定位。
            return location, None
        if cancels_exam_type(text):
            # “不限期中期末”：题型限制和旧候选都释放，学年学期仍然有效。
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
            # 同一课程、显式换考试类型（例如期末澄清期间改看期中）：
            # 旧的期末候选 ID 不能把合法的期中卷排除掉。
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
        match = locate_papers(
            prepared.input.text, pool, exam_type=location.exam_type, location=location
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

    @staticmethod
    def _requested_exam_type(text: str, planned: Any) -> str | None:
        """模型选择能力，程序检查筛选是否确实出现在本轮请求中。"""
        if re.search(r"不限|不区分|全部类型|所有类型|不要只查|别只查|不只查|不只要", text):
            return None
        markers = {
            "midterm": r"期中|\bmidterm\b",
            "final": r"期末|期终|\bfinal\b",
            "other": r"其[他它]类型|其[他它]考试|\bother\b",
        }
        mentioned = {kind for kind, pattern in markers.items() if re.search(pattern, text, re.IGNORECASE)}
        if not mentioned:
            return None
        # 未指定某一类型时查询全部；“全部期末卷”仍是期末范围。
        if len(mentioned) > 1 and not re.search(r"不要|不查|不找|不看|不是|只要|只查|仅要|仅查", text):
            return None
        if isinstance(planned, str) and planned in mentioned:
            return planned
        if len(mentioned) == 1:
            return next(iter(mentioned))
        return None

    @staticmethod
    def _wants_explanation(text: str) -> bool:
        """学生是否在本轮要求讲解、问考点或问题意。"""

        return bool(
            re.search(
                r"讲|解释|说明|分析|啥意思|什么意思|考(点|啥|什么|哪个)|怎么做|为什么|"
                r"思路|步骤|看不懂|看不明白|不会|错在|对不对|选哪个|哪个选项",
                text or "",
            )
        )

    @staticmethod
    def _wants_to_try_first(text: str) -> bool:
        """学生是否明确要求先自己做、暂不给答案。

        这是唯一保留“先作答”的合法理由；不能拿“这是真题请求”当理由。
        """

        return bool(
            re.search(
                r"先自己做|自己做|别给(我)?答案|不要答案|先不给答案|先别说答案|"
                r"不公布答案|先不(要)?讲解|先别讲|只看题|先看题|先自己(想|写|做)|自己想|"
                r"只给提示|只提示|给点提示|提示一下|先别公布|别公布|不要公布",
                text or "",
            )
        )

    @staticmethod
    def _wants_hint_only(text: str) -> bool:
        """学生只要提示、不要完整答案。"""

        return bool(
            re.search(r"只给提示|只提示|给点提示|给个提示|提示一下|稍作提示|稍微提示", text or "")
        )
