"""题目目标选择：候选、歧义、编号定位与目标采纳。"""

import asyncio
import re
from typing import Any

from app.conversations.graph import (
    ActionOutcome,
    ActionPlan,
)
from app.conversations.turn_types import (
    ExecuteTurn,
    _DynamicContext,
)
from app.question_bank.models import SearchRequest
from app.questions.errors import (
    QuestionError,
    QuestionSelectionInvalidError,
    QuestionSourceChangedError,
)
from app.questions.intent import (
    candidate_number,
    detect_question_type,
)
from app.questions.models import (
    QuestionCard,
    QuestionSelection,
)

class SelectionMixin:
    """把学生的话解析成当前要操作的题目目标。"""

    def _adopt_question_target(
        self, prepared: ExecuteTurn, ctx: _DynamicContext,
        selection: QuestionSelection, card: QuestionCard,
    ) -> None:
        previous = self._question_context(ctx)
        ctx.question_selection = selection
        ctx.question_card = card
        ctx.question_result_status = "ok"
        ctx.question_result_message = ""
        if previous.get("paper_id") != card.source.paper_id:
            # 选中一题不代表已经浏览完该卷。新卷的浏览范围尚未知，
            # “继续列题”应从第 0 条真实读取，而不是沿用旧卷游标。
            ctx.question_context = {
                "paper_id": card.source.paper_id,
                "paper_title": card.source.paper_title,
                "question_type": card.question_type,
                "number_label": card.number_label,
                "total": None, "offset": 0, "returned": 0,
                "next_offset": 0, "limit": 64, "query": "",
                "request_id": prepared.request_id,
            }
        course = self._course_for_paper(card.source.paper_id)
        if course:
            ctx.confirmed_course = course

    def _stale_target_message(self, state: dict[str, Any]) -> str:
        """当前目标失效时要求学生指明对象，而不是拿旧题顶上。"""

        if state.get("status") == "clarify_paper":
            return "刚才那份试卷还没确定是哪一份，请先说明卷别（例如 A 卷还是 B 卷），再说要讲哪一道。"
        year = str(state.get("academic_year") or "")
        term = {"s1": "第一学期", "s2": "第二学期"}.get(str(state.get("term") or ""), "")
        scope = f"{year}学年{term}" if year else ""
        where = f"{scope}的这份卷" if scope else "刚才那份卷"
        return f"当前还没有确定试卷：{where}在目录里没有登记记录。请先说明要用哪份试卷，再说要讲哪一道题。"

    @staticmethod
    def _target_ambiguity(cards: list[QuestionCard]) -> str:
        if len({card.source.paper_id for card in cards}) > 1:
            return "这个题号对应多份试卷，请说明要哪一份试卷。"
        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        labels = list(dict.fromkeys(kinds.get(card.question_type, "题目") for card in cards))
        if len(labels) > 1:
            return "这份试卷的" + "、".join(labels) + "都有这个题号，请说明题型。"
        return "这个题号对应多个来源或版本，请重新查题并指定具体题目。"

    def _select_candidate(
        self,
        conversation_id: str,
        request_id: str,
        source_turn: str,
        card: QuestionCard,
    ) -> QuestionSelection:
        verified = self._resolve_question_card(
            conversation_id, source_turn, card.question_id
        )
        return self._select_question_card(
            conversation_id, request_id, source_turn, verified
        )

    async def _resolve_question_selection(
        self, prepared: ExecuteTurn, ctx: _DynamicContext,
        parameters: dict[str, Any] | None = None, *, use_active: bool = False,
    ) -> tuple[QuestionSelection | None, str | None]:
        """选题与讲解共用：本轮试卷约束 → 题型/题号 → 唯一性 → 来源校验。

        先解析本轮明确目标，再查看旧卡片；旧卡恰好同号不能覆盖换卷要求。
        列表位置只在位置请求中使用，题号匹配始终要求唯一。
        """
        parameters = dict(parameters or {})
        text = prepared.input.text or ""
        cards, source_turn = self._question_candidates(ctx)

        def unresolved(message: str) -> tuple[None, str]:
            # 澄清轮没有新查题时，沿用旧卡片的显示位置即可，不能把旧结果
            # 再保存成一批“本轮新找到的题”，否则页面会重复展示整套卡片。
            if source_turn != prepared.request_id:
                ctx.question_results = []
                ctx.question_card = None
            return None, message

        intent = self._search_intent(
            text, ctx, ActionPlan(action="search_questions", parameters=parameters)
        )
        positional = self._mentions_list_position(text)
        if intent.paper_unresolved and not positional:
            return unresolved("我无法从这些条件唯一确定一份试卷，请补充年份、学期或考试类型；不会改选旧试卷。")

        state = self._paper_state(ctx)
        stale_target = state is not None and state.get("status") in {
            "unlocated",
            "clarify_paper",
        }
        if (
            stale_target
            and intent.paper_id is None
            and self._requested_number(parameters, text) is None
            and not positional
        ):
            # 明确换卷失败或还在等卷别时，数据库里的旧已选题不能当成“这道题”：
            # 否则学生会听到旧卷题目的讲解（§7.9 R1）。点历史题卡走
            # question_action 分支，不受这里影响。
            return unresolved(self._stale_target_message(state))

        paper_id = intent.paper_id
        wanted_type = detect_question_type(text) or parameters.get("question_type")
        number = (
            int(intent.number_label) if intent.number_label is not None
            else self._requested_number(parameters, text)
        )
        position = None
        if positional or number is None:
            raw_position = number if positional and number is not None else parameters.get("index")
            if raw_position is not None:
                try:
                    position = int(raw_position)
                except (TypeError, ValueError):
                    return unresolved("请说明列表中的有效位置。")
            if positional:
                number = None

        def matches(card: QuestionCard) -> bool:
            return (
                (not paper_id or card.source.paper_id == paper_id)
                and (not wanted_type or card.question_type == wanted_type)
                and (
                    not intent.course
                    or self._course_for_paper(card.source.paper_id) in (None, intent.course)
                )
            )

        if position is not None:
            if not 1 <= position <= len(cards):
                return unresolved(f"列表里没有第 {position} 个题目，请确认位置。")
            chosen = cards[position - 1]
            if not matches(chosen):
                return unresolved("该列表位置的题目不属于本轮指定的试卷或题型，请按目标试卷重新查题。")
            candidates = [chosen]
        else:
            candidates = [
                card for card in cards
                if matches(card) and (number is None or self._same_number(card.number_label, number))
            ]

        if number is None and position is None and use_active:
            active = ctx.question_selection or self._repository.get_active_question(ctx.conversation_id)
            if active is not None:
                active_card = self.questions.card_for_selection(active)
                if matches(active_card):
                    self._adopt_question_target(prepared, ctx, active, active_card)
                    return active, None

        if len(candidates) > 1:
            return unresolved(self._target_ambiguity(candidates))

        # 当前页没有目标，按本轮已解析的卷与题号查找。题型未指定且当前
        # 浏览上下文也未限定时，查事实库确认唯一性，不能由局部页推断。
        context = self._question_context(ctx)
        needs_lookup = number is not None and paper_id and (
            not candidates or (wanted_type is None and not context.get("question_type"))
        )
        if needs_lookup:
            located, problem = await self._locate_question_by_number(
                ctx, number, wanted_type, paper_id=paper_id
            )
            if located is None:
                return unresolved(problem)
            try:
                selection = self._select_located_card(prepared, ctx, located)
            except QuestionError as exc:
                return unresolved(exc.message)
            self._adopt_question_target(prepared, ctx, selection, located)
            return selection, None

        if not candidates:
            if number is not None:
                return unresolved(f"没有找到符合本轮试卷、题型和题号条件的第 {number} 题，请确认目标。")
            return unresolved("请先查找并说明要选哪一道题。")
        if source_turn is None:
            return unresolved("找不到这次题目的来源记录，请重新查题。")
        chosen = candidates[0]
        try:
            if source_turn == prepared.request_id:
                # 本轮刚查到的卡尚未写入最终轮次，不能按“历史轮次卡片”读取。
                # 仍核验审核/build/哈希，并保留本轮已展示的完整结果集合。
                current_results = ctx.question_results
                selection = self._select_located_card(prepared, ctx, chosen)
                ctx.question_results = current_results
            else:
                selection = self._select_candidate(
                    prepared.conversation_id, prepared.request_id, source_turn, chosen
                )
        except QuestionError as exc:
            return unresolved(exc.message)
        self._adopt_question_target(prepared, ctx, selection, chosen)
        return selection, None

    async def _dynamic_select_question(
        self, prepared: ExecuteTurn, action: ActionPlan, ctx: _DynamicContext,
    ) -> ActionOutcome:
        selection, problem = await self._resolve_question_selection(prepared, ctx, action.parameters)
        if selection is None:
            return self._action_result(prepared, ctx, ActionOutcome(
                "select_question", "clarification", problem or "请说明要选哪一道题。",
                needs_user=True,
            ))
        card = ctx.question_card
        assert card is not None
        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        return self._action_result(prepared, ctx, ActionOutcome(
            "select_question", "success",
            f"已选中第 {card.number_label} 题（{kinds.get(card.question_type, '题目')}）。",
            {
                "question_id": card.question_id,
                "number_label": card.number_label,
                "question_type": card.question_type,
                "paper_id": card.source.paper_id,
                "paper_title": card.source.paper_title,
            },
            needs_user=False,
        ))

    @staticmethod
    def _requested_number(parameters: dict[str, Any], text: str) -> int | None:
        # “第五道选择题”与“第5题”都是原卷题号；本轮原话优先于模型参数。
        match = re.search(
            r"第\s*([0-9]+|[一二三四五六七八九十]+)\s*(?:道|个)?\s*"
            r"(?:(?:选择|单选|多选|填空|编程|代码|程序设计)题|小题|题)", text or "",
        )
        if match:
            raw = match.group(1)
            if raw.isdigit():
                return int(raw)
            digits = {char: number for number, char in enumerate("零一二三四五六七八九")}
            if "十" in raw:
                left, right = raw.split("十", 1)
                if (not left or left in digits) and (not right or right in digits):
                    return digits.get(left, 1) * 10 + digits.get(right, 0)
            if raw in digits:
                return digits[raw]
        raw = parameters.get("number_label")
        if isinstance(raw, str):
            match = re.search(r"([0-9]+)", raw)
            if match:
                return int(match.group(1))
        return candidate_number(text)

    async def _locate_question_by_number(
        self,
        ctx: _DynamicContext,
        number: int,
        question_type: str | None,
        *, paper_id: str | None = None,
    ) -> tuple[QuestionCard | None, str | None]:
        """按原卷题号在已确定的那份试卷里定位一道题（与当前页无关）。"""

        context = self._question_context(ctx)
        paper_id = paper_id or context.get("paper_id")
        if not paper_id:
            return None, "现在还没有确定的试卷；请先说明是哪份卷，我再按题号定位。"
        course = (
            self._course_for_paper(paper_id)
            or ctx.confirmed_course
            or "Python语言程序设计"
        )
        request = SearchRequest(
            course=course,
            query="",
            paper_id=paper_id,
            number_label=str(number),
            question_type=question_type,  # type: ignore[arg-type]
            limit=3,
        )
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.questions.search,
                    request,
                    conversation_id=ctx.conversation_id,
                    request_id=None,
                ),
                timeout=20.0,
            )
        except asyncio.TimeoutError:
            return None, "按题号定位超时，请稍后再试。"
        except QuestionError as exc:
            return None, exc.message
        if not result.cards:
            return None, f"这份试卷里没有第 {number} 题，请确认题号。"
        if len(result.cards) > 1:
            kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
            types = "、".join(
                dict.fromkeys(kinds.get(card.question_type, "题目") for card in result.cards)
            )
            return None, f"这份试卷的{types}都有第 {number} 题，请说明要哪一种题型。"
        # 只做定位，不改本轮状态：选中与否由调用方决定并落库。
        return result.cards[0], None

    def _select_located_card(
        self, prepared: ExecuteTurn, ctx: _DynamicContext, card: QuestionCard
    ) -> QuestionSelection:
        """选中本轮刚定位到的题：按来源/审核/哈希校验后落库。"""

        if not card.build_id or not card.content_sha256:
            raise QuestionSourceChangedError("题目来源信息不完整，请重新查题。")
        if self.questions.build_id and card.build_id != self.questions.build_id:
            raise QuestionSourceChangedError("题目库已更新，请重新查题后再选。")
        if not self.questions.is_approved(card.question_id):
            raise QuestionSelectionInvalidError("这道题尚未通过审核，暂不能使用。")
        if not self.questions.verify_card(card):
            raise QuestionSourceChangedError("题目内容已变化，请重新查题。")
        ctx.question_results = [card]
        ctx.question_source_turn = prepared.request_id
        return self._select_question_card(
            prepared.conversation_id,
            prepared.request_id,
            prepared.request_id,
            card,
        )

    @staticmethod
    def _mentions_list_position(text: str) -> bool:
        """学生是否在说“列表里的第 N 个”，而不是原卷题号。"""

        return bool(
            re.search(
                r"(列表|上面|其中|这些|那几个|这几道|这几个|结果|候选)[^。；，]{0,6}"
                r"第?\s*[0-9一二三四五六七八九十]+\s*(个|项|条|道题?)",
                text or "",
            )
            or re.search(r"第\s*[0-9]+\s*(个|项|条)", text or "")
        )
