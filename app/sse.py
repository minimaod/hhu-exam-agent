"""SSE 帧编码。

旧的自然语言查询接口和新会话轮次接口使用同一条编码规则：一条事件一个
data 帧，JSON 使用 UTF-8 原文，中文不转义。放在独立模块里，会话业务不需要
了解旧查询服务内部的字符串拼接方式。
"""

import json

from pydantic import BaseModel


def encode_sse_event(event: BaseModel) -> str:
    """把一条事件模型编码成 SSE data 帧。"""

    payload = event.model_dump(mode="json")
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
