"""DeepSeek 需求理解适配。

只做一件事：把一句需求交给模型，取回 JSON，再交给 prompt.parse_understanding。
供应商异常和响应读取集中在 app/llm.py，原始异常不进入对外响应。

参数说明（与 2026-10-24 的 DeepSeek 文档和 langchain-openai 1.7 源码核对）：
- 思考模式默认开启，必须显式传 thinking.type=disabled。
- JSON Output 用 response_format.type=json_object，提示词里已要求输出 JSON。
- max_tokens 与 response_format 放在 extra_body：langchain-openai 会把
  ChatOpenAI(max_tokens=...) 改写成 max_completion_tokens，而 DeepSeek 文档
  只声明支持 max_tokens；extra_body 会原样展开进请求体。
- max_retries=0，避免在“一次理解”之外自动重试。
"""

from typing import Any

from app.config import DeepSeekSettings
from app.llm import build_chat_model, log_usage, map_provider_error, response_text
from app.querying.models import Understanding
from app.querying.prompt import SYSTEM_PROMPT, parse_understanding

MODEL_TIMEOUT_SECONDS = 30.0
MODEL_MAX_TOKENS = 1024


class DeepSeekUnderstanding:
    """实现 Understander 协议；每次查询只调用一次模型。"""

    def __init__(
        self,
        settings: DeepSeekSettings,
        *,
        http_async_client: Any | None = None,
    ) -> None:
        self._model = build_chat_model(
            settings,
            max_tokens=MODEL_MAX_TOKENS,
            timeout=MODEL_TIMEOUT_SECONDS,
            json_mode=True,
            http_async_client=http_async_client,
        )

    async def __call__(self, query: str) -> Understanding:
        try:
            response = await self._model.ainvoke(
                [
                    ("system", SYSTEM_PROMPT),
                    ("human", query),
                ]
            )
        except Exception as exc:
            mapped = map_provider_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

        log_usage(response)
        return parse_understanding(response_text(response))
