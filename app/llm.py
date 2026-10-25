"""共享的 DeepSeek 模型构造、错误映射与响应读取。

查询和辅导使用同一个供应商与同一个启动配置；这里集中处理：
- 关闭思考模式、max_tokens 放在 extra_body（DeepSeek 只声明 max_tokens）；
- 供应商异常映射成安全错误码，不把原始异常返回页面；
- 输出被 max_tokens 截断时按响应错误处理。
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

logger = logging.getLogger(__name__)

AUTH_MESSAGE = "模型服务认证失败，请检查后端本地配置。"
RATE_LIMIT_MESSAGE = "模型服务当前限流，请稍后重试。"
UNAVAILABLE_MESSAGE = "模型服务暂时不可用，请稍后重试。"
TIMEOUT_MESSAGE = "模型响应超时，请重试。"
INVALID_MESSAGE = "模型返回的内容不符合约定，请重试。"


def build_chat_model(
    settings: DeepSeekSettings,
    *,
    max_tokens: int,
    timeout: float,
    json_mode: bool = True,
    http_async_client: Any | None = None,
) -> ChatOpenAI:
    extra_body: dict[str, Any] = {
        "thinking": {"type": "disabled"},
        "max_tokens": max_tokens,
    }
    if json_mode:
        extra_body["response_format"] = {"type": "json_object"}

    return ChatOpenAI(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=timeout,
        max_retries=0,
        temperature=0.0,
        extra_body=extra_body,
        # 仅测试注入 MockTransport；生产路径为 None。
        http_async_client=http_async_client,
    )


def map_provider_error(exc: Exception) -> QueryError | None:
    """把供应商异常转成安全业务错误；无法归类时返回 None。"""

    if isinstance(exc, AuthenticationError):
        return QueryError("MODEL_AUTH_FAILED", AUTH_MESSAGE)
    if isinstance(exc, RateLimitError):
        return QueryError("MODEL_RATE_LIMITED", RATE_LIMIT_MESSAGE)
    if isinstance(exc, APITimeoutError):
        return QueryError("MODEL_TIMEOUT", TIMEOUT_MESSAGE)
    if isinstance(exc, APIConnectionError):
        return QueryError("MODEL_UNAVAILABLE", UNAVAILABLE_MESSAGE)
    if isinstance(exc, APIStatusError):
        if exc.status_code == 401:
            return QueryError("MODEL_AUTH_FAILED", AUTH_MESSAGE)
        if exc.status_code == 429:
            return QueryError("MODEL_RATE_LIMITED", RATE_LIMIT_MESSAGE)
        return QueryError("MODEL_UNAVAILABLE", UNAVAILABLE_MESSAGE)
    return None


def log_usage(arg1: Any, arg2: Any = None) -> None:
    """记录模型调用 token 用量；支持 log_usage(response) 与 log_usage("action", response)。"""
    if arg2 is not None:
        action, response = str(arg1), arg2
    else:
        action, response = "default", arg1
    metadata = getattr(response, "response_metadata", None) or {}
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return
    logger.info(
        "deepseek usage action=%s model=%s input_tokens=%s output_tokens=%s total_tokens=%s",
        action,
        metadata.get("model_name") or metadata.get("model") or "unknown",
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("total_tokens"),
    )


def response_text(response: Any) -> str:
    """读取文本输出；截断、非文本或空内容统一按响应错误处理。"""

    metadata = getattr(response, "response_metadata", None) or {}
    if metadata.get("finish_reason") == "length":
        raise QueryError("MODEL_RESPONSE_INVALID", INVALID_MESSAGE)

    content = response.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        if parts:
            return "".join(parts)
    raise QueryError("MODEL_RESPONSE_INVALID", INVALID_MESSAGE)
