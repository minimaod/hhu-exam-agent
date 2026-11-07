"""F010 C 测试替身：带流式终局能力的真实调度器。

生产路径下最终正文由 ``SchedulerAgent.finalize_stream`` 在**同一份循环消息**
上流式生成；A/B 的替身没有 ``astream``，因此走的是服务层的适配器回退。
这里给 C 增加一个同时支持 ``ainvoke``（工具决策）与 ``astream``（终局正文）
的模型，让“首选路径”也有执行证据。
"""

from __future__ import annotations

import copy
from typing import Any

from tests.tool_fakes import FakeToolMessage, FakeToolModel


class StreamingToolModel(FakeToolModel):
    """先按脚本返回工具决策，再用 ``astream`` 产出终局正文。"""

    def __init__(
        self,
        responses: list[dict[str, Any] | Exception],
        pieces: list[str],
        *,
        stream_error: Exception | None = None,
        finish_reason: str | None = None,
    ) -> None:
        super().__init__(responses)
        self.pieces = list(pieces)
        self.stream_error = stream_error
        self.finish_reason = finish_reason
        self.streamed: list[list[Any]] = []

    async def astream(self, messages: list[Any], **kwargs: Any):
        self.streamed.append(copy.deepcopy(messages))
        last = len(self.pieces) - 1
        for index, piece in enumerate(self.pieces):
            if self.stream_error is not None and index == 0:
                raise self.stream_error
            chunk = FakeToolMessage(content=piece)
            if self.finish_reason and index == last:
                chunk.response_metadata["finish_reason"] = self.finish_reason
            yield chunk