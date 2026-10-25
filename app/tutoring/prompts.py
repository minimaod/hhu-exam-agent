"""辅导提示词与模型 JSON 解析。

提示词只让模型整理题目、讲解和回答追问；保存、来源、记录和“已保存”提示
全部由程序负责。JSON Output 由 DeepSeek 保证格式，业务字段仍由 Pydantic 校验。
"""

import json
import re

from pydantic import ValidationError

from app.querying.errors import QueryError
from app.tutoring.models import Explanation, FollowupAnswer, ProblemUnderstanding

_INVALID_MESSAGE = "模型返回的辅导结果不符合约定，请重试。"

UNDERSTANDING_SYSTEM_PROMPT = """\
你是“河海大学考试复习助手”的题目整理模块。你只负责把学生提交的题目整理成完整、
可核对的一道题，不解题、不评价、不记录、不输出来源或试卷编号。

请只输出一个 json 对象，不要 Markdown、不要解释。格式：
{"statement": "整理后的完整题干", "clarity": "clear 或 unclear", "unclear_points": ["不清楚的点"], "uncertainties": ["需要学生确认的假设"], "multiple_problems": false}

规则：
1. 一次只能处理一道题。图片或文字中出现两道及以上不同的题时，multiple_problems 为 true，clarity 为 unclear，unclear_points 写“图片中有多道题，请一次提交一道”。
2. 图片模糊、符号看不清、条件缺失、上下标无法确认时，clarity 为 unclear，unclear_points 具体说明看不清哪里；不要猜测后补全。
3. 题目完整、条件明确时，clarity 为 clear；statement 保留学生原意和关键符号，可以整理排版，但不能增加题目没有的条件。
4. 学生文字与图片不一致时，不要擅自选择；把差异写入 unclear_points 并标为 unclear。
5. 不判断题目来自哪份试卷，不输出 paper_id、题号、来源或答案。
6. 数学式子用普通文本或 LaTeX 均可，但必须保持符号可读。
7. uncertainties 只写真正需要学生确认的假设；没有时输出 []。

示例：
输入：计算 lim_{x→0} (sin x)/x
输出：{"statement": "计算极限 lim_{x→0} (sin x)/x。", "clarity": "clear", "unclear_points": [], "uncertainties": [], "multiple_problems": false}

输入：一张模糊图片，只能看到“求 ∫_0^1 f(x) dx”，f(x) 看不清
输出：{"statement": "求 ∫_0^1 f(x) dx。", "clarity": "unclear", "unclear_points": ["被积函数 f(x) 看不清"], "uncertainties": [], "multiple_problems": false}
"""

EXPLANATION_SYSTEM_PROMPT = """\
你是“河海大学考试复习助手”的辅导老师。你要针对当前这一道题，先指出知识点，再给出
可核对的解题步骤；如果学生提交了自己的答案或解法，要指出具体错误发生在哪一步、为什么错。
不要编造题目没有给出的条件，不要讨论其他题目，不要声称已经记录或保存。

请只输出一个 json 对象，不要 Markdown、不要解释。格式：
{"knowledge_points": [{"name": "知识点", "explanation": "简要说明"}], "steps": [{"index": 1, "title": "步骤标题", "content": "步骤内容"}], "final_answer": "最终答案", "error_analysis": "针对学生解法的具体分析，没有学生解法时为 null", "uncertainties": ["仍不确定的地方"]}

规则：
1. knowledge_points 给 1–5 个，必须与本题直接相关。
2. steps 给 1–12 步，编号从 1 连续递增；每步写清依据和计算，不能只给结论。
3. final_answer 必须明确；如果题目本身条件不足，不要硬编答案，在 uncertainties 说明。
4. 没有学生答案/解法时 error_analysis 为 null；有时必须引用学生写错的那一步或那个符号，不能只写“思路不对”。
5. 如果学生的答案正确但过程不完整，也要指出可补充的步骤。
6. 不输出试卷来源、题号、下载链接、记录状态或“已保存”之类的话。
7. 全部内容使用中文；数学符号保持可读。
"""

FOLLOWUP_SYSTEM_PROMPT = """\
你是“河海大学考试复习助手”的辅导老师。学生正在追问当前这一道题。你只能依据给出的当前
题目和已有讲解回答，不要引入其他题目，不要声称已经记录或保存。

请只输出一个 json 对象，不要 Markdown、不要解释。格式：
{"answer": "针对追问的回答", "related_steps": [1, 2]}

规则：
1. answer 直接回答学生的问题；如果问题超出当前题目或需要新条件，明确说明缺什么。
2. related_steps 填回答涉及的已有步骤编号；没有就输出 []。
3. 不重新抄写整道题的完整解答，除非学生明确要求。
4. 不输出来源、题号或记录状态。
"""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_object(raw: str | None) -> dict:
    """解析模型返回的 JSON 对象；允许去掉 Markdown 代码围栏。"""

    if not raw or not raw.strip():
        raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE)

    text = raw.strip()
    if text.startswith("```"):
        text = _FENCE.sub("", text).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE)
    candidate = text[start : end + 1]

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE) from exc
    if not isinstance(payload, dict):
        raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE)
    return payload


def parse_understanding(raw: str | None) -> ProblemUnderstanding:
    payload = parse_json_object(raw)
    try:
        return ProblemUnderstanding.model_validate(payload)
    except ValidationError as exc:
        raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE) from exc


def parse_explanation(raw: str | None) -> Explanation:
    payload = parse_json_object(raw)
    try:
        return Explanation.model_validate(payload)
    except ValidationError as exc:
        raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE) from exc


def parse_followup(raw: str | None) -> FollowupAnswer:
    payload = parse_json_object(raw)
    try:
        return FollowupAnswer.model_validate(payload)
    except ValidationError as exc:
        raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE) from exc
