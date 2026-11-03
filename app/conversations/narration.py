"""回复材料与正文：公开材料投影、答案范围、正文生成、流式输出与只重做正文。"""

import logging
from collections.abc import (
    AsyncIterator,
    Callable,
)
from dataclasses import replace
from typing import Any

from app.conversations.models import (
    ActionResultItem,
    SuggestionItem,
    TurnMessageDeltaEvent,
)
from app.conversations.reply import (
    ReplyMaterial,
    ReplyTruncated,
    build_material,
    material_to_payload,
)
from app.conversations.turn_types import (
    ExecuteTurn,
    TurnInputRulesMixin,
    _DynamicContext,
)
from app.questions.intent import candidate_number
from app.questions.models import QuestionCard

logger = logging.getLogger("app.conversations.service")

class NarrationMixin:
    """把执行结果投影成回复材料，并生成或流式输出正文。"""

    def _build_reply_material(
        self,
        prepared: ExecuteTurn,
        ctx: "_DynamicContext",
        composite_results: list[ActionResultItem],
        suggestions: list[SuggestionItem],
        recent_summary: str | None,
    ) -> ReplyMaterial | None:
        """构建正文材料；纯澄清轮不需要回复模型，返回 ``None``。

        单独拆出来的原因：材料要在正文开始输出之前就存进恢复记录，否则
        客户端中途断开时，就只剩“重新执行一遍工具”这一条路。
        """

        clarified = [it for it in composite_results if it.status == "clarification"]
        teaching = self._teaching_material(ctx)
        if clarified and not teaching and ctx.answer_scope is None:
            # 纯澄清：问题是要学生逐字回答的，不能让回复模型改写或替学生作答，
            # 因此直接用工具给出的问题原文，也不再消耗一次模型调用。
            # 但如果这一轮已经完成讲解，就必须先把讲解写进正文（见下）。
            # “只要提示/只问考点”不是纯澄清：正文必须真的给出提示或考点。
            return None
        # 只有本轮真的查过题，才把题目标题和整卷浏览进度交给正文；否则模型会
        # 拿着上一轮的题目编“这次先列出第 1 到第 5 题，回复继续看下一批”。
        own_questions = ctx.question_source_turn == prepared.request_id
        return self._restrict_reply_material(build_material(
            user_text=prepared.input.text,
            course=ctx.confirmed_course,
            composite_results=composite_results,
            waiting_for=(
                None
                if ctx.answer_scope is not None
                else (clarified[-1].message if clarified else self._waiting_for(composite_results))
            ),
            question_titles=(
                self._question_titles(ctx.question_results) if own_questions else []
            ),
            question_materials=self._question_materials(
                ctx,
                prepared.input.text,
                ctx.question_results if own_questions else [],
            ),
            question_page=(
                ctx.question_context
                if own_questions
                and ctx.question_context.get("paper_id")
                and not ctx.question_context.get("query")
                else None
            ),
            answer_scope=ctx.answer_scope,
            suggestions=[item.label for item in suggestions],
            context_summary=recent_summary,
            teaching=teaching,
            learning_memory=self._memory_material(
                ctx, text=prepared.input.text
            ),
        ))

    @classmethod
    def _restrict_reply_material(cls, material: ReplyMaterial) -> ReplyMaterial:
        """公开题干仍可被模型自行解出；纯选题/浏览不需要把它交给正文。

        同一投影也在生成前应用，防止旧恢复材料绕过本轮回答范围。
        前端题卡不受影响；考点、提示、题意或明确解题请求仍获得公开材料。
        """
        if (
            material.teaching or material.answer_scope is not None
            or (
                not cls._wants_to_try_first(material.user_text)
                and cls._wants_explanation(material.user_text)
            )
        ):
            return material
        return replace(material, question_materials=(), context_summary=None)

    @classmethod
    def _selection_reply(cls, material: ReplyMaterial) -> str | None:
        """纯选题已完成时只确认真实目标，不让正文擅自解题或补猜题量。

        这只决定已完成操作的展示方式；Agent 的继续决策和用户的后续请求不受限。
        """
        if (
            material.teaching or material.answer_scope or cls._wants_explanation(material.user_text)
            or not material.results
            or any(item.action != "select_question" or item.status != "success" for item in material.results)
        ):
            return None
        details = material.results[-1].details
        title, number = details.get("paper_title"), details.get("number_label")
        kind = {"choice": "选择题", "fill": "填空题", "code": "编程题"}.get(details.get("question_type"))
        if not title or not number or not kind:
            return None
        return f"已选中《{title}》的{kind}第 {number} 题。"

    @staticmethod
    def _question_materials(
        ctx: _DynamicContext, text: str = "", results: list[Any] | None = None
    ) -> list[str]:
        """本轮公开题面材料：题干、选项、代码、知识点与来源（不含答案）。

        只放本轮真实返回、并且已经在卡片上展示给学生的题；“第 5 题”这类
        明确题号会被排到最前面，保证提示和考点回答针对学生问的那一道。
        超出长度时截断并注明，不静默丢题。
        """

        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        cards: list[QuestionCard] = []
        if ctx.question_card is not None:
            cards.append(ctx.question_card)
        for card in (ctx.question_results if results is None else results):
            if all(item.question_id != card.question_id for item in cards):
                cards.append(card)
        wanted = candidate_number(text)
        if wanted is not None:
            preferred = [card for card in cards if TurnInputRulesMixin._same_number(card.number_label, wanted)]
            if preferred:
                cards = preferred + [card for card in cards if card not in preferred]
        lines: list[str] = []
        used = 0
        budget = 4000
        for index, card in enumerate(cards):
            pieces = [
                f"第 {card.number_label} 题（{kinds.get(card.question_type, '题目')}"
                + (f"，{card.source.paper_title}" if card.source.paper_title else "")
                + "）",
                f"题干：{card.statement[:300]}",
            ]
            if card.options:
                options = "；".join(
                    f"{item.label}. {item.text[:60]}" for item in card.options[:8]
                )
                pieces.append("选项：" + options)
            if card.code_lines:
                pieces.append("代码：\n" + "\n".join(card.code_lines[:15])[:600])
            approved = [
                item.name
                for item in card.knowledge_points
                if getattr(item, "status", "") == "verified"
            ]
            candidates = [
                item.name
                for item in card.knowledge_points
                if getattr(item, "status", "") != "verified"
            ]
            if approved:
                pieces.append("已审核知识点：" + "、".join(approved[:6]))
            if candidates:
                pieces.append("题库候选知识点（未审核，仅供参考）：" + "、".join(candidates[:6]))
            text = "｜".join(pieces)
            if used + len(text) > budget and lines:
                lines.append(f"（另有 {len(cards) - index} 道题面没有列出，正文可以让学生按题号继续问）")
                break
            lines.append(text)
            used += len(text)
        return lines

    async def _compose_reply(
        self,
        prepared: ExecuteTurn,
        ctx: _DynamicContext,
        final_state: dict[str, Any],
        composite_results: list[ActionResultItem],
        suggestions: list[SuggestionItem],
        *,
        material: ReplyMaterial | None = None,
        emit: Callable[[str, str], None],
        push: Callable[[object], None],
        recent_summary: str | None,
    ) -> tuple[str, dict[str, Any]]:
        """业务结果确定后单独流式生成正文；失败时退回确定性说明。

        返回值第二项写入恢复记录的 ``reply`` 字段，用于区分“模型真实生成”
        与“程序兜底”，报告不能把兜底算作自然回复验证通过。
        """

        fallback = self._dynamic_reply(final_state, composite_results, ctx)
        clarified = [it for it in composite_results if it.status == "clarification"]
        if material is None:
            material = self._build_reply_material(
                prepared, ctx, composite_results, suggestions, recent_summary
            )
        if material is None:
            return fallback, {"mode": "clarification", "deltas": 0}

        if ctx.recovery is not None:
            # 留着材料，回复失败/截断时可以只重做正文，不重跑已经成功的操作。
            ctx.recovery["reply_input"] = material_to_payload(material)
        if self._make_reply is None:
            return fallback, {
                "mode": "fallback",
                "reason": "reply_not_configured",
                "deltas": 0,
            }

        emit("composing_reply", "正在组织回复")

        def on_piece(piece: str, seq: int) -> None:
            push(
                TurnMessageDeltaEvent(
                    conversation_id=prepared.conversation_id,
                    request_id=prepared.request_id,
                    seq=seq,
                    delta=piece[:400],
                )
            )

        text, meta = await self._stream_reply(self._make_reply(), material, on_piece)
        if text and clarified and material.answer_scope is None:
            # 讲解之后再问下一步：讲解已经由模型写出，问题按工具原文补在末尾，
            # 既不会丢讲解，也不会让模型改写要学生回答的问题。
            question = clarified[-1].message.strip()
            if question and question not in text:
                seq = int(meta.get("deltas") or 0) + 1
                extra = "\n\n" + question
                on_piece(extra, seq)
                text = text + extra
                meta["deltas"] = seq
        if text:
            # 移除学习的记忆是程序事实：模型换了说法也不能把“原会话记录保留”说丢。
            notice = self._memory_notice(composite_results)
            if notice and notice not in text:
                seq = int(meta.get("deltas") or 0) + 1
                extra = "\n\n" + notice
                on_piece(extra, seq)
                text = text + extra
                meta["deltas"] = seq
            return text, meta
        return fallback, dict(meta, mode="fallback")

    async def _stream_reply(
        self,
        reply: Any,
        material: ReplyMaterial,
        on_piece: Callable[[str, int], None] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """流式生成正文，并如实报告“完整 / 截断 / 失败”。

        已经产出的文字不会丢弃：截断或中途出错时保留残文，由公开状态
        告诉前端“正文未完整生成”，而不是把它伪装成完整回复。
        """

        meta: dict[str, Any] = {}
        async for piece in self._reply_pieces(reply, material, meta):
            if on_piece is not None:
                on_piece(piece, meta["deltas"])
        text = str(meta.pop("text", "") or "")
        return text, meta

    async def _reply_pieces(
        self,
        reply: Any,
        material: ReplyMaterial,
        meta: dict[str, Any],
    ) -> AsyncIterator[str]:
        """逐块产出正文，并把“完整/截断/失败”写进 ``meta``。

        做成异步生成器的原因：普通路径要把片段推给 SSE 队列，真题讲解路径
        要直接 ``yield`` 事件。两条路径共用同一套截断与兜底判定，避免出现
        “一条路径有正文、另一条没有”的分叉。
        """

        pieces: list[str] = []
        material = self._restrict_reply_material(material)
        selection_reply = self._selection_reply(material)
        if selection_reply:
            # 没尝试回复模型，保持公开 reply_status=None；reason 标明确定性回执，
            # 不伪称真实模型生成，也不把已经完整的确认语标成失败。
            meta.update(mode=None, reason="selection_ack", deltas=1, text=selection_reply)
            yield selection_reply
            return
        seq = 0
        meta.update({"mode": "model", "deltas": 0, "text": ""})
        try:
            async for piece in reply.stream(material):
                if not piece:
                    continue
                seq += 1
                pieces.append(piece)
                meta["deltas"] = seq
                yield piece
        except ReplyTruncated as exc:
            logger.warning("最终回复被截断：%s", exc.reason)
            meta.update(
                mode="truncated",
                reason=exc.reason,
                deltas=seq,
                text="".join(pieces).strip(),
            )
            return
        except Exception:
            logger.warning("最终回复生成失败", exc_info=True)
            text = "".join(pieces).strip()
            if text:
                meta.update(mode="incomplete", reason="stream_error", deltas=seq, text=text)
            else:
                meta.update(mode="fallback", reason="reply_failed", deltas=seq, text="")
            return

        text = "".join(pieces).strip()
        if not text:
            meta.update(mode="fallback", reason="empty_reply", deltas=seq, text="")
            return
        meta.update(mode="model", deltas=seq, text=text)

    @staticmethod
    def _waiting_for(composite_results: list[ActionResultItem]) -> str | None:
        """推断“本轮结束后在等学生做什么”，供正文使用。"""

        hints = {
            "search_questions": "选择题号，或直接作答这道真题",
            "select_question": "作答这道题，或要求讲解",
            "generate_exercise": "作答当前练习",
        }
        for item in reversed(composite_results):
            if item.status != "success":
                continue
            if item.details.get("needs_supplement"):
                return "补充解题过程后重新提交"
            if item.action == "search_questions" and item.details.get("selected"):
                # 学生已经明确要求直接讲解/看答案，本次已自动选中，不再等待选择。
                continue
            hint = hints.get(item.action)
            if hint:
                return hint
        return None

    @staticmethod
    def _teaching_material(ctx: "_DynamicContext") -> str | None:
        """把学生已经能看到的讲解整理成正文材料。

        只取卡片上已经展示过的内容：讲解、追问回答、学生主动查看后的参考
        解答与评价。未查看的答案不会出现在这里，因此正文不会提前泄题。
        """

        tutoring = ctx.tutoring
        kind = getattr(tutoring, "kind", None) if tutoring is not None else None
        # 讲解结果有显式 kind；测试替身/旧记录可能直接给讲解内容本身，
        # 因此再按结构判断一次（有步骤和最终答案即是讲解）。
        if kind == "explanation" or (
            kind is None
            and tutoring is not None
            and getattr(tutoring, "steps", None)
            and getattr(tutoring, "final_answer", None)
        ):
            problem = getattr(tutoring, "problem", None)
            statement = getattr(problem, "statement_text", None)
            lines = [f"题目：{statement[:400]}"] if statement else []
            points = getattr(tutoring, "knowledge_points", None) or []
            if points:
                lines.append(
                    "知识点："
                    + "；".join(
                        f"{point.name}（{point.explanation[:80]}）" for point in points
                    )
                )
            for step in getattr(tutoring, "steps", None) or []:
                lines.append(f"第{step.index}步 {step.title}：{step.content[:400]}")
            final_answer = getattr(tutoring, "final_answer", None)
            if final_answer:
                lines.append(f"最终答案：{final_answer[:600]}")
            error_analysis = getattr(tutoring, "error_analysis", None)
            if error_analysis:
                lines.append(f"易错点：{error_analysis[:400]}")
            return "\n".join(lines)
        if kind == "followup_answer":
            answer = getattr(tutoring, "answer", None)
            if answer:
                return f"追问回答：{answer[:900]}"

        exercise = ctx.exercise_view
        if exercise is not None:
            solution = getattr(exercise, "reference_solution_text", None)
            evaluation = getattr(exercise, "evaluation", None)
            if solution and getattr(ctx, "evaluation_view", None) is None:
                # 本轮主动查看解答时，不让旧评价覆盖刚打开的解答。
                return f"参考解答（已向学生开放）：{solution[:900]}"
            if evaluation is not None:
                # 判定与“是否需要补步骤”是两件事：判错不等于要补充，
                # 依据不足也不等于答错。分别表达，正文才不会改写原评价含义。
                if evaluation.is_correct is True:
                    verdict = "正确"
                elif evaluation.is_correct is False:
                    verdict = "错误"
                else:
                    verdict = "依据不足，暂时无法判定对错"
                if getattr(evaluation, "needs_supplement", False):
                    verdict += "；需要补充解题过程后才能判断"
                parts = [
                    f"判定：{verdict}",
                    f"点评：{evaluation.final_answer_assessment[:300]}",
                    f"步骤分析：{evaluation.step_by_step_analysis[:400]}",
                    f"建议：{evaluation.guidance[:300]}",
                ]
                attempts = getattr(exercise, "attempts", None) or []
                attachments = attempts[-1].attachments if attempts else getattr(exercise, "attachments", [])
                if attachments:
                    parts.insert(0, f"评价阶段已读取 {len(attachments)} 张过程图片；以下是已完成的批改结果。正文无需重新识图，不得否认评价阶段收到图片。")
                if solution:
                    parts.append(f"参考解答（已向学生开放）：{solution[:900]}")
                return "\n".join(parts)
            if solution:
                return f"参考解答（已向学生开放）：{solution[:900]}"
        return None

    @staticmethod
    def _question_titles(cards: list[QuestionCard]) -> list[str]:
        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        return [
            f"第 {card.number_label} 题（{kinds.get(card.question_type, '题目')}）"
            for card in cards[:5]
        ]

    @staticmethod
    def _dynamic_suggestions(
        final_state: dict[str, Any],
        composite_results: list[ActionResultItem],
    ) -> list[SuggestionItem]:
        if any(it.status == "failed" for it in composite_results):
            return []
        return [
            SuggestionItem(label=label, action=label)
            for label in (final_state.get("suggested_actions") or [])
        ]

    def _question_context_line(
        self, ctx: _DynamicContext, review_state: dict[str, Any]
    ) -> str:
        """给决策模型的结构化试卷/浏览事实。

        这一段决定模型知不知道“刚刚那张卷是哪张、现在看到第几道”。只写真实
        数字；本轮还没有新查题时，从库里最近一次成功记录恢复。
        """

        context = ctx.question_context or self._question_context(ctx)
        if not context and not ctx.question_results:
            return review_state.get("question_context") or ""
        parts: list[str] = []
        if context.get("paper_id"):
            parts.append(f"当前试卷ID：{context['paper_id']}")
        title = context.get("paper_title")
        if title:
            parts.append(f"当前试卷：《{title}》")
        kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
        kind = kinds.get(context.get("question_type") or "")
        if kind:
            parts.append(f"当前题型：{kind}")
        total = context.get("total")
        offset = context.get("offset") or 0
        returned = context.get("returned") or 0
        if total is not None:
            parts.append(f"共 {total} 道已审核题目")
        if returned and not context.get("query"):
            parts.append(
                f"已展示第 {offset + 1}–{offset + returned} 道"
                + (f"，下一批从第 {offset + returned + 1} 道开始" if context.get("next_offset") else "，已全部展示")
            )
        if ctx.question_selection is not None:
            parts.append(
                f"已选中第 {ctx.question_selection.number_label} 题，可直接讲解或看答案"
            )
        elif ctx.question_results:
            parts.append("学生还没指定哪一道；他来问考点或要讲解时按题号定位")
        if context.get("paper_id"):
            parts.append("学生说“刚刚那张卷”“这张卷”时沿用当前试卷；说“继续/下一批”时从上一句的位置往后")
        return "；".join(parts)[:400]
