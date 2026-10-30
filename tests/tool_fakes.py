"""F008：原生工具调用与流式正文的模型替身。

这些替身只模拟供应商协议（``tool_calls``、``role="tool"``、``astream`` 分块），
不模拟业务；业务仍由真实调度器、真实服务和真实仓库执行，因此用例验证的是
“协议接线是否正确”，不是把断言写在替身内部。
"""

from __future__ import annotations

import copy
from typing import Any


class FakeToolMessage:
    """模拟带 tool_calls 的 AIMessage。"""

    def __init__(self, content: str = "", tool_calls: list[dict] | None = None):
        self.content = content
        self.tool_calls = list(tool_calls or [])
        self.response_metadata = {
            "model_name": "deepseek-chat",
            "finish_reason": "tool_calls" if self.tool_calls else "stop",
        }
        self.usage_metadata = {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }


def tool_call(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    call_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": call_id or f"call_{name}",
        "name": name,
        "args": args or {},
        "type": "tool_call",
    }


class FakeToolModel:
    """按脚本依次返回 tool_calls 的模型替身。"""

    def __init__(self, responses: list[dict[str, Any] | Exception]):
        self._responses = list(responses)
        self.calls: list[list[Any]] = []
        self.bound_tools: list[dict[str, Any]] | None = None
        self.bind_count = 0

    def bind_tools(self, tools: list[dict[str, Any]], **kwargs: Any) -> "FakeToolModel":
        self.bound_tools = tools
        self.bind_count += 1
        return self

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> FakeToolMessage:
        self.calls.append(copy.deepcopy(messages))
        if not self._responses:
            raise AssertionError("模型调用次数超出脚本设定")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeToolMessage(
            content=str(item.get("content") or ""),
            tool_calls=list(item.get("tool_calls") or []),
        )


class FakeReplyModel:
    """按脚本产出正文片段的流式模型替身。

    ``finish_reason="length"`` 用来模拟“达到输出上限被截断”的真实情况：
    文字是真的，但不是完整回复。
    """

    def __init__(
        self,
        pieces: list[str],
        *,
        fail_after: int | None = None,
        finish_reason: str = "stop",
    ):
        self._pieces = list(pieces)
        self._fail_after = fail_after
        self._finish_reason = finish_reason
        self.calls: list[list[Any]] = []

    async def astream(self, messages: list[Any], **kwargs: Any):
        self.calls.append(copy.deepcopy(messages))
        for index, piece in enumerate(self._pieces):
            if self._fail_after is not None and index >= self._fail_after:
                raise RuntimeError("供应商流中断")
            yield FakeToolMessage(content=piece)
        if self._finish_reason != "stop":
            tail = FakeToolMessage(content="")
            tail.response_metadata = {"finish_reason": self._finish_reason}
            yield tail


class ReplyFactory:
    """可重复创建新回复器的工厂，模拟“每轮新建一个模型客户端”。

    返回的是真实 ``StreamingReply``（只换掉底层模型），因此服务层的调用方式
    与生产一致：``await reply.stream(material)``。
    """

    def __init__(
        self,
        pieces: list[str],
        *,
        fail_after: int | None = None,
        finish_reason: str = "stop",
    ):
        from app.conversations.reply import StreamingReply

        self._pieces = pieces
        self._fail_after = fail_after
        self._finish_reason = finish_reason
        self._streaming_reply = StreamingReply
        self.models: list[FakeReplyModel] = []

    def __call__(self):
        model = FakeReplyModel(
            self._pieces,
            fail_after=self._fail_after,
            finish_reason=self._finish_reason,
        )
        self.models.append(model)
        return self._streaming_reply(model=model)