"""自然语言查询测试用替身与小工具。

FakeUnderstander 按脚本返回 Understanding、原始 JSON 或 QueryError，
用来替换真实模型；测试不需要网络和密钥。
"""

import json
from collections.abc import Iterable

from app.querying.errors import QueryError
from app.querying.models import Understanding
from app.querying.prompt import parse_understanding


class FakeUnderstander:
    """按顺序吐出预设结果；用完再调用会直接失败，暴露多余请求。"""

    def __init__(self, results: Iterable[Understanding | QueryError | str]) -> None:
        self._results = list(results)
        self.queries: list[str] = []

    async def __call__(self, query: str) -> Understanding:
        self.queries.append(query)
        if not self._results:
            raise AssertionError("FakeUnderstander 没有更多预设结果。")

        item = self._results.pop(0)
        if isinstance(item, QueryError):
            raise item
        if isinstance(item, str):
            return parse_understanding(item)
        return item


def understanding(
    *,
    action: str = "search",
    course: str | None = "高数",
    exam_type: str | None = None,
    reason: str | None = None,
) -> Understanding:
    """构造一个通过组合校验的理解结果。"""

    return Understanding.model_validate(
        {
            "action": action,
            "course": course,
            "exam_type": exam_type,
            "reason": reason,
        }
    )


def parse_sse(body: str) -> list[dict]:
    """把测试里收到的 SSE 正文拆成事件 JSON 列表。"""

    events: list[dict] = []
    for frame in body.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        assert frame.startswith("data: "), f"不是 data 帧：{frame!r}"
        events.append(json.loads(frame[len("data: ") :]))
    return events
