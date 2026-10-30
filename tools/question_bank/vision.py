"""OpenAI 兼容视觉模型的最小离线封装。

只做三件事：
- 从项目本地配置读取供应商、模型和密钥（不把密钥写入日志/证据）；
- 把页面 PNG 作为 image_url 发给模型，要求返回 JSON；
- 把每次调用的模型名、输入哈希、原始响应、usage 和错误完整留给复核。

模型输出是资料，不是指令；本模块不执行响应中的任何文本，也不根据响应
选择文件路径或调用其他工具。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)

from app.config import PROJECT_ROOT

PROMPT_VERSION = "f007-visual-v1"
DEFAULT_MAX_TOKENS = 4096


class VisionError(RuntimeError):
    """视觉调用失败；error_code 供逐题结果区分失败类型。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VisionUnsupported(VisionError):
    pass


class VisionTruncated(VisionError):
    pass


@dataclass(frozen=True)
class VisionSettings:
    api_key: str
    base_url: str
    model: str
    max_tokens: int = DEFAULT_MAX_TOKENS

    @property
    def display_model(self) -> str:
        return self.model


@dataclass
class VisionCall:
    call_id: str
    model: str
    prompt_version: str
    image_paths: list[str]
    image_sha256: list[str]
    prompt_sha256: str
    status: str
    parsed: dict[str, Any] | None = None
    raw_text: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "image_paths": self.image_paths,
            "image_sha256": self.image_sha256,
            "prompt_sha256": self.prompt_sha256,
            "status": self.status,
            "parsed": self.parsed,
            "raw_text": self.raw_text,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "attempts": self.attempts,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at,
        }


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_settings() -> VisionSettings:
    """读取视觉配置；优先独立 VISION_*，否则复用现有 DEEPSEEK_*。"""

    file_values = {}
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        try:
            file_values = {
                key: value
                for key, value in dotenv_values(env_file, encoding="utf-8").items()
                if value is not None
            }
        except (OSError, ValueError) as exc:
            raise VisionError("vision_config", "无法读取本地 .env") from exc

    def get(name: str, fallback: str = "") -> str:
        if name in os.environ:
            return os.environ[name].strip()
        return str(file_values.get(name, fallback)).strip()

    api_key = get("VISION_API_KEY") or get("DEEPSEEK_API_KEY")
    base_url = get("VISION_BASE_URL") or get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = get("VISION_MODEL") or get("DEEPSEEK_MODEL", "deepseek-flash")
    if not api_key:
        raise VisionError("vision_config", "缺少视觉模型 API key（VISION_API_KEY/DEEPSEEK_API_KEY）")
    if not model:
        raise VisionError("vision_config", "缺少视觉模型名称")
    return VisionSettings(api_key=api_key, base_url=base_url.rstrip("/"), model=model)


def image_data_url(path: Path) -> str:
    data = path.read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _extract_json(text: str) -> dict[str, Any]:
    """只接受 JSON 对象；兼容模型偶尔包一层 markdown fence。"""

    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        payload = json.loads(stripped)
    except ValueError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    return payload


def _usage_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    if isinstance(usage, dict):
        return usage
    return {"repr": str(usage)}


class VisionClient:
    def __init__(
        self,
        settings: VisionSettings | None = None,
        *,
        timeout: float = 180.0,
        max_attempts: int = 3,
        sleep: Any = time.sleep,
    ) -> None:
        self.settings = settings or load_settings()
        self._client = OpenAI(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            timeout=timeout,
            max_retries=0,
        )
        self.max_attempts = max_attempts
        self._sleep = sleep

    def complete_json(
        self,
        image_paths: list[Path],
        prompt: str,
        *,
        max_tokens: int | None = None,
    ) -> VisionCall:
        """发送图片和 prompt，返回带证据的调用结果；失败也返回而不是抛出。"""

        if not image_paths:
            raise VisionError("vision_input", "没有输入图片")

        image_bytes = []
        for path in image_paths:
            try:
                image_bytes.append((path, path.read_bytes()))
            except OSError as exc:
                raise VisionError("vision_input", f"输入图片不可读：{path.name}") from exc

        prompt_sha = _sha_bytes(prompt.encode("utf-8"))
        call = VisionCall(
            call_id="vc-" + uuid.uuid4().hex[:16],
            model=self.settings.model,
            prompt_version=PROMPT_VERSION,
            image_paths=[str(path) for path, _ in image_bytes],
            image_sha256=[_sha_bytes(data) for _, data in image_bytes],
            prompt_sha256=prompt_sha,
            status="running",
            created_at=_now(),
        )
        content = [{"type": "text", "text": prompt}]
        for _, data in image_bytes:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,"
                        + base64.b64encode(data).decode("ascii"),
                        "detail": "high",
                    },
                }
            )

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            started = time.time()
            try:
                response = self._client.chat.completions.create(
                    model=self.settings.model,
                    messages=[{"role": "user", "content": content}],
                    max_tokens=max_tokens or self.settings.max_tokens,
                    temperature=0,
                    response_format={"type": "json_object"},
                    extra_body={"thinking": {"type": "disabled"}},
                )
                choice = response.choices[0]
                raw = choice.message.content or ""
                call.finish_reason = choice.finish_reason
                call.usage = _usage_dict(response.usage)
                attempt_row = {
                    "attempt": attempt,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "finish_reason": choice.finish_reason,
                    "content_chars": len(raw),
                }
                call.attempts.append(attempt_row)
                if choice.finish_reason == "length":
                    raise VisionTruncated("vision_truncated", "模型输出被截断")
                if not raw.strip():
                    raise VisionError("vision_empty", "模型返回空内容")
                try:
                    parsed = _extract_json(raw)
                except ValueError as exc:
                    raise VisionError("vision_json", "模型返回的不是合法 JSON") from exc
                call.raw_text = raw
                call.parsed = parsed
                call.status = "success"
                return call
            except VisionTruncated as exc:
                last_error = exc
                call.error_code = exc.code
                call.error_message = str(exc)
                # 截断不应盲目重试同一页；交给调用方拆页。
                break
            except VisionUnsupported:
                raise
            except AuthenticationError as exc:
                raise VisionError("vision_auth", "视觉模型认证失败") from exc
            except BadRequestError as exc:
                message = str(exc)
                call.attempts.append(
                    {
                        "attempt": attempt,
                        "elapsed_ms": int((time.time() - started) * 1000),
                        "error": message[:300],
                    }
                )
                # 400 常见于模型不接受图片/JSON；明确报告，不降级成文本。
                raise VisionUnsupported("vision_unsupported", f"模型拒绝了图片输入：{message[:300]}") from exc
            except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
                last_error = exc
                call.attempts.append(
                    {
                        "attempt": attempt,
                        "elapsed_ms": int((time.time() - started) * 1000),
                        "error": type(exc).__name__,
                    }
                )
                if attempt < self.max_attempts:
                    self._sleep(min(2 ** attempt, 8))
                    continue
                call.error_code = "vision_unavailable"
                call.error_message = type(exc).__name__
                break
            except APIStatusError as exc:
                last_error = exc
                call.attempts.append(
                    {
                        "attempt": attempt,
                        "elapsed_ms": int((time.time() - started) * 1000),
                        "status": exc.status_code,
                        "error": str(exc)[:200],
                    }
                )
                if exc.status_code in (429, 500, 502, 503, 504) and attempt < self.max_attempts:
                    self._sleep(min(2 ** attempt, 8))
                    continue
                call.error_code = "vision_http"
                call.error_message = f"HTTP {exc.status_code}"
                break
            except VisionError as exc:
                last_error = exc
                call.attempts.append(
                    {
                        "attempt": attempt,
                        "elapsed_ms": int((time.time() - started) * 1000),
                        "error": exc.code,
                    }
                )
                if exc.code == "vision_json" and attempt < self.max_attempts:
                    continue
                call.error_code = exc.code
                call.error_message = str(exc)
                break

        call.status = "failed"
        if call.error_code is None:
            call.error_code = "vision_failed"
            call.error_message = str(last_error or "未知错误")
        return call