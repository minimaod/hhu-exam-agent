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
from app.question_bank.models import QUESTION_TYPE_LABELS

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
    # 查题分页事实：范围、总数、本页与后续结果。
    question_page: dict[str, Any] | None = None
    # 回答范围：hint=只给提示，topic=只问考点。None 表示按学生原话正常回答。
    answer_scope: str | None = None
    suggestions: tuple[str, ...] = ()
    context_summary: str | None = None
    workbench_context: str | None = None
    # 已在卡片里展示给学生、因此可以在正文里完整讲述的讲解内容。
    # 未授权的答案（练习参考解答、未查看的真题答案）不会进入这里。
    teaching: str | None = None
    # F009：程序统计好的学习记忆事实。只描述记录，不含答案，也不含“掌握”结论。
    learning_memory: str | None = None
    # F017 Fix-005：本轮已产出查卷结果卡片时，控制正文不重复输出试卷表格或逐份清单。
    has_paper_cards: bool = False

    def describe_context(self) -> str:
        """终局提示用的文字上下文：记忆、摘要、整卷浏览与等待状态。

        不含本轮工具结果与讲解正文：前者已在同一循环的消息里，后者只在
        学生本轮获准查看时由工具结果携带；回复适配器仍走 ``describe``。
        """

        lines: list[str] = []
        if self.workbench_context:
            lines.append(self.workbench_context)
        if self.course:
            lines.append(f"【当前课程】{self.course}")
        if self.context_summary:
            lines.append(f"【最近对话】{self.context_summary[:600]}")
        if self.learning_memory:
            lines.append("【学习记忆（程序统计，只描述已有记录）】")
            lines.append(self.learning_memory[:1200])
        if self.has_paper_cards:
            lines.append(
                "【查卷结果卡片已展示】下方结果卡片已完整列出所有命中的试卷及 PDF 下载入口；"
                "单纯查卷时正文严禁生成试卷表格、列表或逐份介绍，只用一句话说明找到了多少份并引导在下方卡片下载；"
                "仅当学生明确要求“推荐一份并说明理由”时，正文才保留推荐理由。"
            )
        if self.question_page:
            lines.append(_describe_page(self.question_page))
        if self.answer_scope == "hint":
            lines.append(
                "【回答范围】只给提示：针对学生问的那道题给出一条能推动他往下想的"
                "具体提示，不要公布答案、不要写完整代码或完整推导。"
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
        if self.has_paper_cards:
            lines.append(
                "【查卷结果卡片已展示】下方结果卡片已完整列出所有命中的试卷及 PDF 下载入口；"
                "单纯查卷时正文严禁生成试卷表格、列表或逐份介绍，只用一句话说明找到了多少份并引导在下方卡片下载；"
                "仅当学生明确要求“推荐一份并说明理由”时，正文才保留推荐理由。"
            )
        return "\n".join(lines)

    def describe(self) -> str:
        lines: list[str] = [f"【学生本轮发言】{self.user_text}"]
        if self.course:
            lines.append(f"【当前课程】{self.course}")
        if self.context_summary:
            lines.append(f"【最近对话】{self.context_summary[:600]}")
        if self.learning_memory:
            lines.append("【学习记忆（程序统计，只描述已有记录）】")
            lines.append(self.learning_memory[:1200])
        if self.has_paper_cards:
            lines.append(
                "【查卷结果卡片已展示】下方结果卡片已完整列出所有命中的试卷及 PDF 下载入口；"
                "单纯查卷时正文严禁生成试卷表格、列表或逐份介绍，只用一句话说明找到了多少份并引导在下方卡片下载；"
                "仅当学生明确要求“推荐一份并说明理由”时，正文才保留推荐理由。"
            )
        if self.results:
            lines.append("【本轮工具执行结果】")
            for item in self.results:
                parts = [f"- {action_label(item.action)}：{status_label(item.status)}"]
                if item.message:
                    parts.append(item.message[:300])
                detail_text = _describe_details(item.details, action=item.action)
                if detail_text:
                    parts.append(detail_text)
                lines.append("；".join(parts))
        else:
            lines.append("【本轮工具执行结果】（没有调用工具）")
        if self.question_page:
            lines.append(_describe_page(self.question_page))
        if self.workbench_context:
            lines.append(self.workbench_context)
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
        if self.has_paper_cards:
            lines.append(
                "【查卷结果卡片已展示】下方结果卡片已完整列出所有命中的试卷及 PDF 下载入口；"
                "单纯查卷时正文严禁生成试卷表格、列表或逐份介绍，只用一句话说明找到了多少份并引导在下方卡片下载；"
                "仅当学生明确要求“推荐一份并说明理由”时，正文才保留推荐理由。"
            )
        return "\n".join(lines)


def _describe_page(page: dict[str, Any]) -> str:
    kinds = QUESTION_TYPE_LABELS
    paper_id = page.get("paper_id")
    paper_title = page.get("paper_title")
    is_paper = bool(paper_id or paper_title)
    label = "整卷浏览" if is_paper else "查题结果"
    title = (paper_title or "所选试卷") if is_paper else "本次跨卷检索"
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
    elif total is not None and returned and offset + returned >= total:
        parts.append("已经全部显示")
    return f"【{label}】" + "，".join(parts)


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
    "review_memory": "查看学习记忆",
    "correct_memory": "纠正学习记忆",
    "delete_memory": "移除学习记忆",
    "view_workspace": "读取工作区",
    "view_question_detail": "查看题目详情",
    "list_shown_questions": "列出已展示题目",
    "view_learning_summary": "查看学习摘要",
    "list_conversation_materials": "查看会话资料",
    "generate_material_artifact": "生成资料复习成果",
    "solve_uploaded_exam": "解答上传试卷",
}

_STATUS_LABELS = {"success": "成功", "failed": "未完成", "clarification": "待学生确认"}
# 工具结果里的记忆附注：条数与总字数双重上限，避免提示被单类细节占满。
_MEMORY_NOTE_LIMIT = 40
_MEMORY_NOTE_CHARS = 1000


def action_label(action: str) -> str:
    return _ACTION_LABELS.get(action, action)


def status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status)


def _describe_details(details: dict[str, Any], *, action: str = "") -> str:
    """把白名单里的细节转成简短中文；未知键已被丢弃。"""

    parts: list[str] = []
    if details.get("count") is not None:
        parts.append(f"数量 {details['count']}")
    if details.get("total") is not None:
        if action in ("search_questions", "list_paper_questions"):
            scope = "指定试卷" if details.get("paper_title") or details.get("paper_id") else "本次跨卷检索"
            parts.append(f"{scope}共 {details['total']} 道")
        else:
            parts.append(f"总数 {details['total']}")
    if details.get("offset") is not None and details.get("returned"):
        start = int(details["offset"]) + 1
        end = int(details["offset"]) + int(details["returned"])
        parts.append(f"本次第 {start}–{end} 道")
    if details.get("has_more"):
        parts.append("还有未显示的题目")
    if details.get("paper_title"):
        parts.append(f"试卷 {details['paper_title']}")
    if details.get("download_url"):
        # 正文只允许使用这里给出的真实链接；没有这个字段时不能自己拼。
        parts.append(f"下载入口 {details['download_url']}")
    if details.get("course"):
        parts.append(f"课程 {details['course']}")
    if details.get("exam_type"):
        parts.append(f"类型 {details['exam_type']}")
    if details.get("question_ids"):
        parts.append(f"题目 {len(details['question_ids'])} 道")
    if details.get("record_id"):
        parts.append("已写入错题本")
    if details.get("feedback_saved"):
        parts.append("备注已实际保存：" + str(details.get("user_feedback") or ""))
    elif details.get("record_id"):
        parts.append("本次没有写入新备注，不得声称已保存备注")
    if details.get("solution_image_count") is not None:
        count = int(details["solution_image_count"])
        parts.append(f"本次评价已读取 {count} 张解法图" if count else "本次评价未附解法图")
    if details.get("followup_target"):
        parts.append("追问对象：" + ("当前练习" if details["followup_target"] == "exercise" else "原题"))
    if details.get("exercise_id"):
        parts.append("已关联练习")
    if details.get("generated_mode") == "standalone":
        parts.append("自主生成练习（模型所生成，非真题）")
    if details.get("knowledge_point"):
        parts.append(f"考查知识点 {details['knowledge_point']}")
    if details.get("has_question_bank") is False:
        bank_note = "当前课程仅提供历年试卷下载，暂不支持按考点拆分检索单道真题"
        if details.get("paper_count") is not None:
            bank_note += f"（可下载历年试卷 {details['paper_count']} 份）"
        parts.append(bank_note)
    if details.get("memory_course"):
        parts.append(f"记忆课程 {details['memory_course']}")
    if details.get("memory_entry_id"):
        parts.append(f"记忆条目 {details['memory_entry_id']}")
    if details.get("memory_removed") is not None:
        parts.append("已从学习记忆移除" if details["memory_removed"] else "之前已移除过")
    if details.get("memory_message"):
        parts.append(str(details["memory_message"])[:200])
    # 学习记忆的附注必须能进入正文提示：材料区有预算，工具结果里的附注不能
    # 因为条目多就被丢掉。按条数与总字数双重上限，超出时如实说明还有多少条。
    entries = details.get("memory_entries")
    if isinstance(entries, list):
        rendered = 0
        used = 0
        skipped = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            note = entry.get("note")
            if not isinstance(note, str) or not note.strip():
                continue
            line = f"记忆条目 {entry.get('entry_id')} 附注 {note.strip()[:60]}"
            if rendered >= _MEMORY_NOTE_LIMIT or used + len(line) > _MEMORY_NOTE_CHARS:
                skipped += 1
                continue
            parts.append(line)
            rendered += 1
            used += len(line)
        if skipped:
            parts.append(f"另有 {skipped} 条记忆附注未列出")
    single = details.get("memory_entry")
    if isinstance(single, dict):
        note = single.get("note")
        if isinstance(note, str) and note.strip():
            parts.append(
                f"记忆条目 {single.get('entry_id')} 附注 {note.strip()[:60]}"
            )
    if details.get("skipped"):
        parts.append("该步骤已跳过")
    if details.get("error_code"):
        parts.append(f"错误码 {details['error_code']}")
    return "，".join(parts)


REPLY_SYSTEM_PROMPT = """你是河海大学考试复习助手的对话撰写者。你写的这段正文就是学生看到的主要内容，
题目卡片、来源、作答区只是附件，不能代替你把话讲清楚。

规则：
1. 真题、试卷、答案、错题或学习记录等系统事实只以材料为准，没有的事实不要编造（包括年份、学校或真题来源）。当材料表明该科不支持按考点检索单道真题时，用面向学生的自然口吻如实说明（如“目前提供 N 份历年试卷 PDF 下载，暂不支持按考点查单题，但可以让我按考点出练习题或直接答疑”），避免使用“馆藏”“题库建设”“真题题目库”等内部开发术语；模型自主生成的练习题必须明确标注“模型生成，非历年真题”。下载入口只写材料里给出的那一个，材料没有给出时不要自己拼链接，也不要说“下载按钮在卡片上”这类空话。
2. 正文就是你和学生的自然对话本身：直接切入学生本轮真正关心的问题，去掉“好的/这是一个好问题/下面为你讲解”等空泛套话，也不要只写“请看下方卡片”。讲解时按学生需求选好深度与形式：
   - 学生要完整讲解：清晰交代思路切入点、补齐关键中间变形与公式适用条件（别用“显然”跳步），并给出结论和易错点；
   - 学生追问“这一步为什么”：只针对这一步讲清条件和变形衔接，不重讲整题；
   - 学生说“我没学过/不懂这概念”：先用一个具体的数值或生活小例子建立直觉，再紧挨着解释公式条件；
   - 学生要求“换一种方法”：另起一条可行的解题切入路线讲清区别。
3. 没有讲解材料且没有操作材料时，如学生是在询问纯学科概念、公式或方法，请直接清晰作答；如果是业务操作结果（如查卷、保存、切题），用一到三句话说清真实结果和下一步即可，不重复刚讲解完的上一题。
4. 绝对不要泄露材料以外的真题答案、参考解答或关键步骤；学生没有主动查看答案时，连暗示都不行。
5. 如果有未完成或失败的步骤，先如实说明失败原因，再说明已完成的部分，不得谎称成功。
6. 可以使用自然的中文排版与轻量 Markdown（加粗、有序/无序列表、代码块、简明表格）及标准 LaTeX 数学公式（`$...$` 与 `$$...$$`）；但不要输出裸 JSON，不要提“工具”“调用”“API”“模型”等实现细节，不要说“作为 AI”。
7. 篇幅按问题需要自适应：局部追问或操作确认简明扼要，完整推导或零基础直观讲解按讲透的需要展开。
8. 有【整卷浏览】或【查题结果】时，说清检索范围、总数和本次显示的范围。整卷总数属于指定试卷，跨卷总数属于本次检索条件。只有材料明确有下一批时才提示学生说“继续”，材料确认显示到末尾时说明已列完。
9. 有【本轮题目材料】且学生问考点、题意或某个选项时，直接用材料里的题干、选项和已确认知识点回答；可以说“这道题主要考……”。若本轮仅是检索或列出了真题卡片供学生查看/选择，严禁在正文中抄写、转述或改写卡片里已有的题干要求和选项（不要写“题目要求：……”），用一两句话说明检索情况、题号、来源或考点并引导作答/选择即可。知识点里标注为候选/未审核的要说明是题库待审核标签还是你的推断，不能把推断说成已审核结论。不要要求学生先作答才能听解释。
10. 学生明确说“先自己做、不给答案”时，只确认题意范围或作答方式，不透露答案与关键步骤。
11. 有【回答范围】时以它为准：只给提示时必须给出与题干相关的具体提示（考点方向、该看哪个条件、下一步先算什么），不能只用“你先试试”“加油”这类空话；只回答考点时说明考查的知识点及理由，不写完整解法、不给答案。
12. 已完成的图片批改结果优先于旧对话里“没收到图片”的说法。评价阶段已读取图片时，不得因正文阶段没有图片而否认已收到；备注保存与否只按本轮实际写入结果说明。当本轮材料标明“详细评价已在评价卡片中完整展示”时，正文对评价部分最多只保留一句简短的下一步引导（如“请补充解题过程，便于定位具体错误。”），禁止复述点评、分步推导和建议；只有学生本轮另附额外追问时，才简明回答该追问。没有提供解题过程时，不得把猜测写成已经确认的错误原因。
13. 本轮是查试卷时：若已有【查卷结果卡片已展示】，单纯查卷时正文严禁生成试卷表格、列表或逐份介绍，只用一句话简洁说明已找到的课程/范围与份数并引导在下方卡片直接查看与下载（仅当学生明确要求“推荐一份并说明理由”时，才保留推荐理由，仍不抄录全部试卷清单）。若材料仅给出了唯一命中的单一试卷且无列表卡片，说出卷名（含学年、学期、卷别）并给出下载入口；材料里已经有学年、学期、卷别时，不得再要求学生补充或确认这些条件。结果为空时如实说明哪个条件没有匹配，不要改列其他学年或学期的试卷；有多份候选需要澄清时只问学生缺少的那个区分条件（例如 A 卷还是 B 卷）。
14. 有【学习记忆】时：只用里面给出的知识点、次数和日期说事，按“独立作答被判错 > 看过解答后作答 > 独立性未确认 > 过程不足 > 还没作答 > 自述理解 > 独立做对（只提醒再确认）”的顺序给建议，最多三条。判错要说材料给的具体日期，不要说成“最近”如果材料标明“早于本窗口”或“只作历史回看”；旧版本依据不能当成当前结论。不要计算掌握率或用百分比，不要因为一次答对就说“你已经掌握”，要说“还需要再确认”；看过解答后的作答不能算独立掌握。学生问起课程归属或记录不对时，说明可以纠正或从记忆里移除；说明移除时原样说“从学习记忆和复习建议中移除，原会话记录保留。”。学习记忆暂时读不到时如实说读不到，不能说“没有记录”。"""


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
        self.last_chunk: Any = None
        self.last_finish_reason: str | None = None

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
        messages = [
            {"role": "system", "content": REPLY_SYSTEM_PROMPT},
            {"role": "user", "content": material.describe() + "\n\n请写这段回复。"},
        ]
        async for piece in self.stream_messages(messages):
            yield piece

    async def stream_messages(
        self, messages: list[dict[str, Any]], *, note: str | None = None
    ) -> AsyncIterator[str]:
        """按给定消息流式生成；F010 C 的终局回复与测试替身回退共用这条路径。"""

        model = self._build_model()
        if note:
            # 与调度器终局路径一致：学习记忆等文字上下文放在最后一条要求里。
            messages = list(messages) + [
                {"role": "user", "content": f"【补充要求】{note}"}
            ]
        buffer = ""
        last_chunk: Any = None
        finish_reason: str | None = None
        async for chunk in model.astream(messages):
            last_chunk = chunk
            finish_reason = chunk_finish_reason(chunk) or finish_reason
            piece = chunk_text(chunk)
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
            self.last_chunk = last_chunk
            log_usage("reply_stream", last_chunk)
        self.last_finish_reason = finish_reason
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


def chunk_finish_reason(chunk: Any) -> str | None:
    """公开别名：终局回复也要判断是否被输出上限截断。"""

    return _chunk_finish_reason(chunk)


def chunk_text(chunk: Any) -> str:
    """公开别名：取一段消息块的文本内容。"""

    return _chunk_text(chunk)


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
    workbench_context: str | None = None,
    teaching: str | None = None,
    learning_memory: str | None = None,
    has_paper_cards: bool = False,
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
    resolved_has_paper_cards = bool(
        has_paper_cards
        or any(
            item.action == "query_papers"
            and item.status == "success"
            and item.details.get("result_cards_rendered")
            for item in results
        )
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
        workbench_context=workbench_context,
        teaching=teaching,
        learning_memory=learning_memory,
        has_paper_cards=resolved_has_paper_cards,
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
        **({"workbench_context": material.workbench_context} if material.workbench_context else {}),
        "teaching": material.teaching,
        "learning_memory": material.learning_memory,
        "has_paper_cards": material.has_paper_cards,
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
        workbench_context=payload.get("workbench_context"),
        teaching=payload.get("teaching"),
        learning_memory=payload.get("learning_memory"),
        has_paper_cards=bool(payload.get("has_paper_cards")),
    )
