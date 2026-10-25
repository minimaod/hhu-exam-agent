"""DeepSeek 辅导模型适配：题目理解、讲解、追问。

图片按官方文档使用 base64 data URL 放在 user 消息的 image_url 内容块；
只发送当前题目需要的图片，不把整个会话或磁盘路径交给模型。
"""

import base64
import json
from typing import Any

from app.config import DeepSeekSettings
from app.llm import build_chat_model, log_usage, map_provider_error, response_text
from app.querying.errors import QueryError
from app.tutoring.models import Explanation, FollowupAnswer, ImagePayload, ProblemUnderstanding
from app.tutoring.prompts import (
    EXPLANATION_SYSTEM_PROMPT,
    FOLLOWUP_SYSTEM_PROMPT,
    UNDERSTANDING_SYSTEM_PROMPT,
    parse_explanation,
    parse_followup,
    parse_understanding,
)

UNDERSTANDING_MAX_TOKENS = 900
UNDERSTANDING_TIMEOUT = 60.0
EXPLANATION_MAX_TOKENS = 2600
EXPLANATION_TIMEOUT = 90.0
FOLLOWUP_MAX_TOKENS = 1200
FOLLOWUP_TIMEOUT = 60.0


def _image_block(image: ImagePayload) -> dict[str, Any]:
    encoded = base64.b64encode(image.data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{image.mime};base64,{encoded}",
            "detail": "high",
        },
    }


def _human_content(text: str, images: list[ImagePayload]) -> str | list[dict[str, Any]]:
    if not images:
        return text
    return [{"type": "text", "text": text}, *[_image_block(image) for image in images]]


class DeepSeekTutor:
    """实现辅导模型协议；图片能力通过真实请求和 MockTransport 分别验证。"""

    def __init__(
        self,
        settings: DeepSeekSettings,
        *,
        http_async_client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._http_async_client = http_async_client

    async def _call(
        self,
        *,
        system_prompt: str,
        human: str | list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> str:
        model = build_chat_model(
            self._settings,
            max_tokens=max_tokens,
            timeout=timeout,
            json_mode=True,
            http_async_client=self._http_async_client,
        )
        try:
            response = await model.ainvoke(
                [
                    ("system", system_prompt),
                    ("human", human),
                ]
            )
        except Exception as exc:
            mapped = map_provider_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

        log_usage(response)
        return response_text(response)

    async def understand_problem(
        self,
        text: str,
        images: list[ImagePayload],
    ) -> ProblemUnderstanding:
        if images:
            image_note = (
                "请结合图片整理题目。图片中的符号要优先核对；"
                "看不清时标为 unclear。"
            )
        else:
            image_note = (
                "本题没有图片，只根据文字整理；不要因为缺少图片而判为 unclear。"
            )
        prompt_text = f"学生提交的题目文字：\n{text}\n\n{image_note}"
        raw = await self._call(
            system_prompt=UNDERSTANDING_SYSTEM_PROMPT,
            human=_human_content(prompt_text, images),
            max_tokens=UNDERSTANDING_MAX_TOKENS,
            timeout=UNDERSTANDING_TIMEOUT,
        )
        return parse_understanding(raw)

    async def explain(
        self,
        *,
        statement: str,
        student_answer: str | None,
        solution_text: str | None,
        statement_images: list[ImagePayload],
        solution_images: list[ImagePayload],
    ) -> Explanation:
        student_part = (
            f"学生答案：\n{student_answer.strip()}\n" if student_answer and student_answer.strip() else ""
        )
        solution_part = (
            f"学生解题过程：\n{solution_text.strip()}\n"
            if solution_text and solution_text.strip()
            else ""
        )
        if not student_part and not solution_part and not solution_images:
            work_note = "学生没有提交答案或解法；error_analysis 必须为 null。"
        else:
            work_note = (
                "学生提交了答案/解法（文字或图片）。请具体指出错误发生在哪一步；"
                "如果正确但过程不完整，也说明可补充之处。"
            )

        prompt_text = (
            f"当前题目：\n{statement}\n\n"
            f"{student_part}{solution_part}"
            f"{work_note}\n\n"
            "请按 json 输出知识点、步骤、最终答案和错误分析。"
        )
        raw = await self._call(
            system_prompt=EXPLANATION_SYSTEM_PROMPT,
            human=_human_content(prompt_text, [*statement_images, *solution_images]),
            max_tokens=EXPLANATION_MAX_TOKENS,
            timeout=EXPLANATION_TIMEOUT,
        )
        return parse_explanation(raw)

    async def answer_followup(
        self,
        *,
        statement: str,
        explanation: Explanation,
        recent_followups: list[tuple[str, str]],
        question: str,
    ) -> FollowupAnswer:
        history = "\n".join(
            f"学生：{q}\n老师：{a}" for q, a in recent_followups[-3:]
        ) or "（无）"
        prompt_text = (
            f"当前题目：\n{statement}\n\n"
            f"已有讲解（json）：\n{json.dumps(explanation.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            f"最近追问：\n{history}\n\n"
            f"学生的新追问：\n{question}\n\n"
            "请只回答这道题的追问，按 json 输出 answer 和 related_steps。"
        )
        raw = await self._call(
            system_prompt=FOLLOWUP_SYSTEM_PROMPT,
            human=prompt_text,
            max_tokens=FOLLOWUP_MAX_TOKENS,
            timeout=FOLLOWUP_TIMEOUT,
        )
        return parse_followup(raw)
