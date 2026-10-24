"""模型适配的请求体与异常映射。

用 httpx.MockTransport 捕获最终发往 DeepSeek 的 JSON，确认思考模式关闭、
JSON Output 生效、max_tokens 不被 LangChain 改写成 max_completion_tokens。
这些检查不需要真实密钥，但真实调用仍需另行验证。
"""

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

from app.config import DeepSeekSettings
from app.querying.errors import QueryError
from app.querying.llm import DeepSeekUnderstanding
from app.querying.models import Understanding

SETTINGS = DeepSeekSettings(
    api_key="test-key",
    base_url="https://example.test",
    model="deepseek-flash",
)

VALID_CONTENT = (
    '{"action":"search","course":"高数","exam_type":null,"reason":null}'
)


def completion(content: str, finish_reason: str = "stop") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def run_with_handler(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Understanding | QueryError:
    async def go() -> Understanding:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            understander = DeepSeekUnderstanding(
                SETTINGS,
                http_async_client=client,
            )
            return await understander("我要复习高数")

    try:
        return asyncio.run(go())
    except QueryError as exc:
        return exc


def test_request_body_matches_deepseek_contract() -> None:
    captured_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(200, json=completion(VALID_CONTENT))

    transport = httpx.MockTransport(handler)

    async def go() -> Understanding:
        async with httpx.AsyncClient(transport=transport) as client:
            understander = DeepSeekUnderstanding(
                SETTINGS,
                http_async_client=client,
            )
            return await understander("我要复习高数")

    result = asyncio.run(go())

    assert result.action == "search"
    assert captured_body["model"] == "deepseek-flash"
    assert captured_body["stream"] is False
    assert captured_body["temperature"] == 0.0
    assert captured_body["thinking"] == {"type": "disabled"}
    assert captured_body["response_format"] == {"type": "json_object"}
    assert captured_body["max_tokens"] == 1024
    assert "max_completion_tokens" not in captured_body
    assert captured_body["messages"][0]["role"] == "system"
    assert "JSON" in captured_body["messages"][0]["content"]


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (401, "MODEL_AUTH_FAILED"),
        (429, "MODEL_RATE_LIMITED"),
        (500, "MODEL_UNAVAILABLE"),
        (400, "MODEL_UNAVAILABLE"),
    ],
)
def test_http_errors_are_mapped(status_code: int, expected_code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"error": {"message": "upstream failed", "type": "invalid_request_error"}},
        )

    result = run_with_handler(handler)

    assert isinstance(result, QueryError)
    assert result.code == expected_code
    assert "upstream" not in result.message


def test_empty_content_is_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion(""))

    result = run_with_handler(handler)

    assert isinstance(result, QueryError)
    assert result.code == "MODEL_RESPONSE_INVALID"


def test_truncated_output_is_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion(VALID_CONTENT, finish_reason="length"))

    result = run_with_handler(handler)

    assert isinstance(result, QueryError)
    assert result.code == "MODEL_RESPONSE_INVALID"


def _raise_from_model(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> DeepSeekUnderstanding:
    """用一个只负责抛异常的对象替换 ChatOpenAI 实例。"""

    understander = DeepSeekUnderstanding(SETTINGS)
    model = SimpleNamespace()

    async def raise_error(*args, **kwargs):
        raise error

    model.ainvoke = raise_error
    monkeypatch.setattr(understander, "_model", model)
    return understander


def test_timeout_is_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    understander = _raise_from_model(
        monkeypatch,
        APITimeoutError(request=httpx.Request("POST", "https://example.test")),
    )

    with pytest.raises(QueryError) as excinfo:
        asyncio.run(understander("我要复习高数"))

    assert excinfo.value.code == "MODEL_TIMEOUT"


def test_connection_error_is_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    understander = _raise_from_model(
        monkeypatch,
        APIConnectionError(request=httpx.Request("POST", "https://example.test")),
    )

    with pytest.raises(QueryError) as excinfo:
        asyncio.run(understander("我要复习高数"))

    assert excinfo.value.code == "MODEL_UNAVAILABLE"
