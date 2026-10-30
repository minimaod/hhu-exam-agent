"""F008：业务执行结束后的自然语言正文生成。

设计要点：
- 工具决策阶段不产生正文；本模块只在“工具调用已经结束、结果已经确定”之后
  运行一次，并且**不再绑定工具**。
- 正文逐块产出（``astream``），由服务层转成 SSE ``message_delta`` 事件；
  不是完整生成后用定时器模拟逐字输出。
- 输入是白名单化的公开事实（``app.conversations.tools.public_details``），
  因此未开放的答案、参考解答和完整供应商消息都不会进入正文提示词。
- 生成失败时由服务层退回 ``_dynamic_reply`` 的确定性说明，并在恢复记录里
  标记为兜底，不能算作自然回复验证通过。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.config import DeepSeekSettings
from app.conversations.tools import public_details
from app.llm import build_chat_model, log_usage

logger = logging.getLogger(__name__)

MAX_REPLY_TOKENS = 1400
REPLY_TIMEOUT_SECONDS = 40.0
# 单个正文片段的最大字符数：太小会让中文一个字一个事件，太大会失去流式观感。
MAX_DELTA_CHARS = 120
MIN_DELTA_CHARS = 24

_HARD_BREAKS = "。！？；\n"
_SOFT_BREAKS = "，、,.:：）)】」』”’ "


@dataclass(frozen=True)
class ReplyResult:
    """一条已执行动作的公开事实。"""

    action: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class ReplyTruncated(RuntimeError):
    """模型因长度上限等原因被截断，正文不完整。"""

    def __init__(self, reason: str = "length") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ReplyMaterial:
    """正文生成允许看到的一切；不包含任何私有材料。"""

    user_text: str
    course: str | None = None
    results: tuple[ReplyResult, ...] = ()
    waiting_for: str | None = None
    question_titles: tuple[str, ...] = ()
    # 公开题面材料：题干、选项、代码、已确认知识点与来源。答案与参考解答
    # 不在这里，只有学生明确查看后才走 teaching。
    question_materials: tuple[str, ...] = ()
    # 整卷浏览事实：共多少道、本次第几到第几、还有多少没显示。
    question_page: dict[str, Any] | None = None
    # 回答范围：hint=只给提示，topic=只问考点。None 表示按学生原话正常回答。
    answer_scope: str | None = None
    suggestions: tuple[str, ...] = ()
    context_summary: str | None = None
    # 已在卡片里展示给学生、因此可以在正文里完整讲述的讲解内容。
    # 未授权的答案（练习参考解答、未查看的真题答案）不会进入这里。
    teaching: str | None = None

    def describe(self) -> str:
        lines: list[str] = [f"【学生本轮发言】{self.user_text}"]
        if self.course:
            lines.append(f"【当前课程】{self.course}")
        if self.context_summary:
            lines.append(f"【最近对话】{self.context_summary[:600]}")
        if self.results:
            lines.append("【本轮工具执行结果】")
            for item in self.results:
                parts = [f"- {action_label(item.action)}：{status_label(item.status)}"]
                if item.message:
                    parts.append(item.message[:300])
                detail_text = _describe_details(item.details)
                if detail_text:
                    parts.append(detail_text)
                lines.append("；".join(parts))
        else:
            lines.append("【本轮工具执行结果】（没有调用工具）")
        if self.question_page:
            lines.append(f"【整卷浏览】{_describe_page(self.question_page)}")
        if self.question_materials:
            lines.append("【本轮题目材料（公开部分，可直接用来回答考点或题意）】")
            lines.extend(f"- {item}" for item in self.question_materials)
        if self.teaching:
            lines.append("【本轮讲解（卡片已同步展示，正文可以完整讲述）】")
            lines.append(self.teaching[:4000])
        if self.question_titles:
            lines.append("【候选题目】" + "、".join(self.question_titles[:5]))
        if self.answer_scope == "hint":
            lines.append(
                "【回答范围】只给提示：针对学生问的那道题给出一条能推动他往下想的"
                "具体提示（该关注哪个条件、该用哪个知识点、该先确定什么），"
                "不要公布答案、不要写完整代码或完整推导，也不要只说“你先试试”。"
            )
        elif self.answer_scope == "topic":
            lines.append(
                "【回答范围】只回答考点：直接说明这道题考查什么知识点、为什么考它；"
                "不要展开完整解法，不要给出答案。"
            )
        if self.waiting_for:
            lines.append(f"【等待学生】{self.waiting_for}")
        if self.suggestions:
            lines.append("【可用建议】" + "、".join(self.suggestions[:5]))
        return "\n".join(lines)


def _describe_page(page: dict[str, Any]) -> str:
    kinds = {"choice": "选择题", "fill": "填空题", "code": "编程题"}
    title = page.get("paper_title") or "当前试卷"
    kind = kinds.get(page.get("question_type") or "", "")
    total = page.get("total")
    offset = page.get("offset") or 0
    returned = page.get("returned") or 0
    parts = [title + (f"的{kind}" if kind else "")]
    if total is not None:
        parts.append(f"共 {total} 道")
    if returned:
        parts.append(f"本次显示第 {offset + 1}–{offset + returned} 道")
    if page.get("next_offset") is not None and total is not None:
        parts.append(f"还有 {max(0, int(total) - offset - returned)} 道没有显示（学生说“继续”可看下一批）")
    elif total is not None and returned:
        parts.append("已经全部显示")
    return "，".join(parts)


_ACTION_LABELS = {
    "introduce": "确认课程",
    "query_papers": "查找试卷",
    "explain_problem": "题目讲解",
    "explain_question": "真题讲解",
    "view_question_answer": "查看真题答案",
    "followup_problem": "回答追问",
    "generate_exercise": "生成练习",
    "evaluate_exercise": "评价作答",
    "reveal_solution": "查看参考解答",
    "record_mistake": "保存错题",
    "record_feedback": "记录理解",
    "search_questions": "查找真题",
    "list_paper_questions": "浏览整卷题目",
    "select_question": "选择真题",
    "view_question_answer": "查看真题答案",
    "clarify_or_reply": "澄清",
}

_STATUS_LABELS = {"success": "成功", "failed": "未完成", "clarification": "待学生确认"}


def action_label(action: str) -> str:
    return _ACTION_LABELS.get(action, action)


def status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status)


def _describe_details(details: dict[str, Any]) -> str:
    """把白名单里的细节转成简短中文；未知键已被丢弃。"""

    parts: list[str] = []
    if details.get("count") is not None:
        parts.append(f"数量 {details['count']}")
    if details.get("total") is not None:
        parts.append(f"总数 {details['total']}")
    if details.get("offset") is not None and details.get("returned"):
        start = int(details["offset"]) + 1
        end = int(details["offset"]) + int(details["returned"])
        parts.append(f"本次第 {start}–{end} 道")
    if details.get("has_more"):
        parts.append("还有未显示的题目")
    if details.get("paper_title"):
        parts.append(f"试卷 {details['paper_title']}")
    if details.get("course"):
        parts.append(f"课程 {details['course']}")
    if details.get("exam_type"):
        parts.append(f"类型 {details['exam_type']}")
    if details.get("question_ids"):
        parts.append(f"题目 {len(details['question_ids'])} 道")
    if details.get("record_id"):
        parts.append("已写入错题本")
    if details.get("exercise_id"):
        parts.append("已生成练习")
    if details.get("skipped"):
        parts.append("该步骤已跳过")
    if details.get("error_code"):
        parts.append(f"错误码 {details['error_code']}")
    return "，".join(parts)


REPLY_SYSTEM_PROMPT = """你是河海大学考试复习助手的对话撰写者。你写的这段正文就是学生看到的主要内容，
题目卡片、来源、作答区只是附件，不能代替你把话讲清楚。

规则：
1. 只使用材料里的事实（数量、课程、题号、题型、保存结果、失败原因）；没有的事实不要编造，也不要输出下载链接。
2. 如果【本轮讲解】有内容，正文就是讲解本身：先用一两句说清思路，再按步骤讲关键推导，最后给出结论和易错点。不要只写“已生成讲解，请看卡片”。
3. 没有讲解材料时，用一到三句话说明结果和下一步该做什么；不要空泛客套。
4. 绝对不要泄露材料以外的答案、参考解答或关键步骤；学生没有主动查看答案时，连暗示都不行。
5. 如果有未完成或失败的步骤，先如实说明失败原因，再说明已完成的部分，不得谎称成功。
6. 纯中文正文，不要 Markdown 标题、代码块、列表符号或编号列表，不要输出 JSON，不要提“工具”“调用”“API”“模型”等实现细节，不要说“作为 AI”。
7. 篇幅按内容需要：讲解类可以写成 200–600 字；普通进展一到三句即可。
8. 有【整卷浏览】时必须说清总数和本次范围（例如“这份卷的选择题一共 40 道，这次列出第 1 到第 10 道，还有 30 道没显示”），并告诉学生说“继续”就能看下一批；已经全部列出时明确说“已经列完”。不能把本次条数说成题目总数。
9. 有【本轮题目材料】且学生问考点、题意或某个选项时，直接用材料里的题干、选项和已确认知识点回答；可以说“这道题主要考……”。知识点里标注为候选/未审核的要说明是题库待审核标签还是你的推断，不能把推断说成已审核结论。不要要求学生先作答才能听解释。
10. 学生明确说“先自己做、不给答案”时，只确认题意范围或作答方式，不透露答案与关键步骤。
11. 有【回答范围】时以它为准：只给提示时必须给出与题干相关的具体提示（考点方向、该看哪个条件、下一步先算什么），不能只用“你先试试”“加油”这类空话；只回答考点时说明考查的知识点及理由，不写完整解法、不给答案。"""


class StreamingReply:
    """流式生成最终正文。

    ``model`` 可注入模型替身；生产路径按启动配置创建一个不绑定工具的模型。
    """

    def __init__(
        self,
        settings: DeepSeekSettings | None = None,
        *,
        model: Any | None = None,
        http_async_client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._model = model
        self._http_async_client = http_async_client

    def _build_model(self) -> Any:
        return self._model or build_chat_model(
            self._settings,  # type: ignore[arg-type]
            max_tokens=MAX_REPLY_TOKENS,
            timeout=REPLY_TIMEOUT_SECONDS,
            json_mode=False,
            http_async_client=self._http_async_client,
            stream_usage=True,
        )

    async def stream(self, material: ReplyMaterial) -> AsyncIterator[str]:
        model = self._build_model()
        messages = [
            {"role": "system", "content": REPLY_SYSTEM_PROMPT},
            {"role": "user", "content": material.describe() + "\n\n请写这段回复。"},
        ]
        buffer = ""
        last_chunk: Any = None
        finish_reason: str | None = None
        async for chunk in model.astream(messages):
            last_chunk = chunk
            finish_reason = _chunk_finish_reason(chunk) or finish_reason
            piece = _chunk_text(chunk)
            if not piece:
                continue
            buffer += piece
            while True:
                segment, buffer = _take_segment(buffer)
                if segment is None:
                    break
                yield segment
        if buffer:
            yield buffer
        if last_chunk is not None:
            log_usage("reply_stream", last_chunk)
        if finish_reason == "length":
            # 已达输出上限：已经产出的文字是真的，但不是完整回复。
            raise ReplyTruncated("length")


def _chunk_finish_reason(chunk: Any) -> str | None:
    """读取 LangChain 消息块的结束原因；不同供应商放在两个位置。"""

    for holder in ("response_metadata", "generation_info"):
        metadata = getattr(chunk, holder, None)
        if isinstance(metadata, dict):
            reason = metadata.get("finish_reason") or metadata.get("stop_reason")
            if isinstance(reason, str) and reason:
                return reason
    return None


def _chunk_text(chunk: Any) -> str:
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _take_segment(buffer: str) -> tuple[str | None, str]:
    """从缓冲里切出一个不超过 ``MAX_DELTA_CHARS`` 的片段。"""

    if len(buffer) < MIN_DELTA_CHARS:
        return None, buffer
    window = buffer[:MAX_DELTA_CHARS]
    for index in range(len(window) - 1, MIN_DELTA_CHARS - 1, -1):
        if window[index] in _HARD_BREAKS:
            return window[: index + 1], buffer[index + 1 :]
    for index in range(len(window) - 1, MIN_DELTA_CHARS - 1, -1):
        if window[index] in _SOFT_BREAKS:
            return window[: index + 1], buffer[index + 1 :]
    if len(buffer) >= MAX_DELTA_CHARS:
        return buffer[:MAX_DELTA_CHARS], buffer[MAX_DELTA_CHARS:]
    return None, buffer


def build_material(
    *,
    user_text: str,
    course: str | None,
    composite_results: list[Any],
    waiting_for: str | None,
    question_titles: list[str] | None = None,
    question_materials: list[str] | None = None,
    question_page: dict[str, Any] | None = None,
    answer_scope: str | None = None,
    suggestions: list[str] | None = None,
    context_summary: str | None = None,
    teaching: str | None = None,
) -> ReplyMaterial:
    """把执行结果过滤成正文材料。

    ``composite_results`` 是 ``ActionResultItem`` 列表；这里只保留白名单细节，
    因此未开放的答案正文不会出现在提示词里。
    """

    results = tuple(
        ReplyResult(
            action=str(getattr(item, "action", "") or ""),
            status=str(getattr(item, "status", "") or ""),
            message=str(getattr(item, "message", "") or ""),
            details=public_details(getattr(item, "details", {}) or {}),
        )
        for item in composite_results
    )
    return ReplyMaterial(
        user_text=user_text,
        course=course,
        results=results,
        waiting_for=waiting_for,
        question_titles=tuple(question_titles or ()),
        question_materials=tuple(question_materials or ()),
        question_page=question_page,
        answer_scope=answer_scope,
        suggestions=tuple(suggestions or ()),
        context_summary=context_summary,
        teaching=teaching,
    )


def material_to_payload(material: ReplyMaterial) -> dict[str, Any]:
    """把材料存进恢复记录，供“只重试回复”时复用。"""

    return {
        "user_text": material.user_text,
        "course": material.course,
        "results": [
            {
                "action": item.action,
                "status": item.status,
                "message": item.message,
                "details": item.details,
            }
            for item in material.results
        ],
        "waiting_for": material.waiting_for,
        "question_titles": list(material.question_titles),
        "question_materials": list(material.question_materials),
        "question_page": material.question_page,
        "answer_scope": material.answer_scope,
        "suggestions": list(material.suggestions),
        "context_summary": material.context_summary,
        "teaching": material.teaching,
    }


def material_from_payload(payload: dict[str, Any]) -> ReplyMaterial:
    """恢复材料；字段缺失时按空值处理，不让旧记录直接报错。"""

    return ReplyMaterial(
        user_text=str(payload.get("user_text") or ""),
        course=payload.get("course"),
        results=tuple(
            ReplyResult(
                action=str(item.get("action") or ""),
                status=str(item.get("status") or ""),
                message=str(item.get("message") or ""),
                details=dict(item.get("details") or {}),
            )
            for item in payload.get("results") or []
        ),
        waiting_for=payload.get("waiting_for"),
        question_titles=tuple(payload.get("question_titles") or ()),
        question_materials=tuple(payload.get("question_materials") or ()),
        question_page=payload.get("question_page"),
        answer_scope=payload.get("answer_scope"),
        suggestions=tuple(payload.get("suggestions") or ()),
        context_summary=payload.get("context_summary"),
        teaching=payload.get("teaching"),
    )