"""需求理解提示词，以及模型 JSON 的解析与业务校验。

提示词只让模型整理条件，不把目录内容放进上下文，也不允许模型生成试卷。
JSON 格式由 DeepSeek 的 JSON Output 保证，但业务字段仍由 Pydantic 再校验一次。
"""

import json

from pydantic import ValidationError

from app.querying.errors import QueryError
from app.querying.models import Understanding

SYSTEM_PROMPT = """\
你是“河海大学考试复习助手”的需求理解模块。你只负责把学生的一句话整理成检索条件，
不回答复习问题，不推荐资料，不输出试卷信息，也不生成链接或代码。

当前系统只能按“一门课程 + 期中/期末”查询本地已登记的历年试卷。

请只输出一个 JSON 对象，不要 Markdown、不要解释。字段固定为四个，全部必填：
{"action": "search", "course": "课程名或 null", "exam_type": "midterm 或 final 或 null", "reason": "null 或 missing_course 或 ambiguous_course 或 unsupported_constraint 或 out_of_scope"}

规则：
1. 学生明确要查某门课程的试卷：action 为 search；course 保留学生写的课程名（包括 AⅠ、BⅠ 等版本字样）；exam_type 按“期中”映射为 midterm、“期末”映射为 final，没提到就为 null；reason 为 null。
2. 没有提到课程，或只说“那期末呢”这类不完整条件：action 为 clarify，reason 为 missing_course，course 为 null。
3. 同时提到多门课程：action 为 clarify，reason 为 ambiguous_course，course 为 null。
4. 课程明确，但带有年份、学年、上下册、题型等当前不支持的筛选条件：action 为 unsupported，reason 为 unsupported_constraint。
5. 复习计划、题型分析等不属于试卷查询的需求：action 为 unsupported，reason 为 out_of_scope。
6. 不要猜测课程版本，不要把“高数”改写成“高等数学BⅠ”，也不要因为目录里可能没有就换成别的课程；课程别名由程序处理。
7. 不要丢弃学生提出的限制条件。无法支持时返回 unsupported，不能返回一个范围更大的 search。
8. 与试卷查询完全无关的问题：action 为 unsupported，reason 为 out_of_scope。

示例：
输入：我要复习高数
输出：{"action": "search", "course": "高数", "exam_type": null, "reason": null}

输入：找高数期末试卷
输出：{"action": "search", "course": "高数", "exam_type": "final", "reason": null}

输入：高数的期中和期末都要
输出：{"action": "search", "course": "高数", "exam_type": null, "reason": null}

输入：我要复习
输出：{"action": "clarify", "course": null, "exam_type": null, "reason": "missing_course"}

输入：找高数和大学物理的卷子
输出：{"action": "clarify", "course": null, "exam_type": null, "reason": "ambiguous_course"}

输入：只要 2024 年的高数试卷
输出：{"action": "unsupported", "course": "高数", "exam_type": null, "reason": "unsupported_constraint"}

输入：帮我制定高数复习计划
输出：{"action": "unsupported", "course": "高数", "exam_type": null, "reason": "out_of_scope"}
"""

_INVALID_MESSAGE = "模型返回的理解结果不符合约定。"


def parse_understanding(raw: str | None) -> Understanding:
    """把模型返回的 JSON 文本转成校验过的 Understanding。

    空内容、截断、坏 JSON、非对象、未知字段、非法枚举或组合不合法，
    都统一按“模型响应错误”处理，不能退化成“列出全部试卷”或正常空结果。
    """

    if not raw or not raw.strip():
        raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE) from exc

    if not isinstance(payload, dict):
        raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE)

    try:
        return Understanding.model_validate(payload)
    except ValidationError as exc:
        raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE) from exc
