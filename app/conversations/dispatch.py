"""会话文本的轻量意图路由。

只在“没有显式 kind、没有图片”时调用。它不解答题目，只判断学生想查试卷、
提出新题、追问、纠正、记录还是反馈；无法判断时返回 ambiguous，由页面给出
固定选项，不猜。

调用成本很小（约 200 输出 token），但会让纯文字轮次多一次模型调用；这是
换取歧义处理能力的明确取舍，已记录在任务书和实现报告中。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import DeepSeekSettings
from app.llm import build_chat_model, log_usage, map_provider_error, response_text
from app.querying.errors import QueryError
from app.tutoring.prompts import parse_json_object

Intent = Literal[
    "query",
    "problem",
    "followup",
    "correction",
    "record",
    "feedback",
    "ambiguous",
]

DECISION_SYSTEM_PROMPT = """\
你是“河海大学考试复习助手”的会话入口。你只判断学生这句话要做什么，不回答题目，
不推荐资料，不生成试卷，不输出代码或链接。

请只输出一个 json 对象，不要 Markdown、不要解释。格式：
{"intent": "query 或 problem 或 followup 或 correction 或 record 或 feedback 或 ambiguous", "reason": "一句话原因"}

规则：
1. query：想按课程/期中/期末查找、下载试卷或复习资料，例如“我要复习高数”“找线代期末试卷”。
2. problem：提出一道新题目，或要求讲解一道题，且当前没有正在辅导的题目。
3. followup：围绕当前已讲解题目追问，例如“第三步为什么用洛必达”“为什么不能用直接代入”。
4. correction：指出当前题干的文字、符号、条件或图片有误，要求改正。
5. record：明确要求把当前题目记录到错题本，例如“记录这题”“加入错题本”。
6. feedback：表示已经听懂、明白了、谢谢，例如“我懂了”“明白了”。
7. ambiguous：无法判断，或一句话同时像查试卷又像辅导题目。
8. 没有当前题目时，不要输出 followup、correction、record、feedback；无法判断就输出 ambiguous。
9. 只判断意图，不因为题目内容而输出答案。
"""


class IntentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    reason: str = Field(min_length=1, max_length=200)


def parse_intent(raw: str | None) -> IntentDecision:
    payload = parse_json_object(raw)
    try:
        return IntentDecision.model_validate(payload)
    except ValidationError as exc:
        raise QueryError(
            "MODEL_RESPONSE_INVALID",
            "模型返回的会话意图不符合约定，请重试。",
        ) from exc


class DeepSeekIntentRouter:
    def __init__(
        self,
        settings: DeepSeekSettings,
        *,
        http_async_client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._http_async_client = http_async_client

    async def __call__(
        self,
        text: str,
        *,
        active_problem: str | None,
        problem_status: str | None,
    ) -> IntentDecision:
        model = build_chat_model(
            self._settings,
            max_tokens=220,
            timeout=20.0,
            json_mode=True,
            http_async_client=self._http_async_client,
        )
        context = (
            f"当前题目：{active_problem}\n当前状态：{problem_status}\n"
            if active_problem
            else "当前没有正在辅导的题目。\n"
        )
        human = (
            f"{context}\n学生这句话：\n{text}\n\n"
            "请只输出意图 json。"
        )
        try:
            response = await model.ainvoke(
                [("system", DECISION_SYSTEM_PROMPT), ("human", human)]
            )
        except Exception as exc:
            mapped = map_provider_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

        log_usage(response)
        return parse_intent(response_text(response))
