"""DeepSeek 模型适配。

只做一件事：把一句需求交给模型，取回 JSON，再交给 prompt.parse_understanding。
供应商异常在这里集中映射成业务错误码，原始异常不进入对外响应。

参数说明（与 2026-10-24 的 DeepSeek 文档和 langchain-openai 1.7 源码核对）：
- 思考模式默认开启，必须显式传 thinking.type=disabled。
- JSON Output 用 response_format.type=json_object，提示词里已要求输出 JSON。
- max_tokens 与 response_format 放在 extra_body：langchain-openai 会把
  ChatOpenAI(max_tokens=...) 改写成 max_completion_tokens，而 DeepSeek 文档
  只声明支持 max_tokens；extra_body 会原样展开进请求体。
- max_retries=0，避免在“一次理解”之外自动重试。
"""

import logging
from typing import Any

from langchain_openai import ChatOpenAI
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from app.config import DeepSeekSettings
from app.querying.errors import QueryError
from app.querying.models import Understanding
from app.querying.prompt import SYSTEM_PROMPT, parse_understanding

MODEL_TIMEOUT_SECONDS = 30.0
MODEL_MAX_TOKENS = 1024

logger = logging.getLogger(__name__)

_AUTH_MESSAGE = "模型服务认证失败，请检查后端本地配置。"
_RATE_LIMIT_MESSAGE = "模型服务当前限流，请稍后重试。"
_UNAVAILABLE_MESSAGE = "模型服务暂时不可用，请稍后重试。"
_TIMEOUT_MESSAGE = "理解需求超时，请重试。"
_INVALID_MESSAGE = "模型返回的理解结果不符合约定，请重试。"


class DeepSeekUnderstanding:
    """实现 Understander 协议；每次查询只调用一次模型。"""

    def __init__(
        self,
        settings: DeepSeekSettings,
        *,
        http_async_client: Any | None = None,
    ) -> None:
        self._model = ChatOpenAI(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=MODEL_TIMEOUT_SECONDS,
            max_retries=0,
            temperature=0.0,
            extra_body={
                "thinking": {"type": "disabled"},
                "max_tokens": MODEL_MAX_TOKENS,
                "response_format": {"type": "json_object"},
            },
            # 仅测试注入 MockTransport；生产路径为 None。
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
        except AuthenticationError as exc:
            raise QueryError("MODEL_AUTH_FAILED", _AUTH_MESSAGE) from exc
        except RateLimitError as exc:
            raise QueryError("MODEL_RATE_LIMITED", _RATE_LIMIT_MESSAGE) from exc
        except APITimeoutError as exc:
            raise QueryError("MODEL_TIMEOUT", _TIMEOUT_MESSAGE) from exc
        except APIConnectionError as exc:
            raise QueryError("MODEL_UNAVAILABLE", _UNAVAILABLE_MESSAGE) from exc
        except APIStatusError as exc:
            if exc.status_code == 401:
                raise QueryError("MODEL_AUTH_FAILED", _AUTH_MESSAGE) from exc
            if exc.status_code == 429:
                raise QueryError("MODEL_RATE_LIMITED", _RATE_LIMIT_MESSAGE) from exc
            raise QueryError("MODEL_UNAVAILABLE", _UNAVAILABLE_MESSAGE) from exc

        metadata = getattr(response, "response_metadata", None) or {}

        usage = getattr(response, "usage_metadata", None)
        if usage:
            logger.info(
                "deepseek usage model=%s input_tokens=%s output_tokens=%s total_tokens=%s",
                metadata.get("model_name") or metadata.get("model") or "unknown",
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("total_tokens"),
            )

        if metadata.get("finish_reason") == "length":
            # 输出被 max_tokens 截断，JSON 很可能不完整，按响应错误处理。
            raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE)

        content = response.content
        if not isinstance(content, str):
            raise QueryError("MODEL_RESPONSE_INVALID", _INVALID_MESSAGE)

        return parse_understanding(content)
