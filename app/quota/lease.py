"""进程内执行占用登记。

数据库里的 ``user_active_turns`` 是跨请求的权威记录；本登记表只用于确认
“这次执行是否还在本进程内运行”，从而避免仅凭时间回收一个仍然活跃的轮次。
当前部署以单后端进程为前提。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable


DEFAULT_RECLAIM_SECONDS = 15 * 60


@dataclass
class _LeaseState:
    created_at: float
    started_at: float | None = None
    task: asyncio.Task | None = None
    release_pending: bool = False


@dataclass
class LeaseRegistry:
    reclaim_seconds: float = DEFAULT_RECLAIM_SECONDS
    clock: Callable[[], float] = time.monotonic
    _leases: dict[str, _LeaseState] = field(default_factory=dict)

    def prepare(self, token: str) -> None:
        self._leases[token] = _LeaseState(created_at=self.clock())

    def mark_started(self, token: str, task: asyncio.Task | None = None) -> None:
        state = self._leases.get(token)
        if state is None:
            state = _LeaseState(created_at=self.clock())
            self._leases[token] = state
        state.started_at = self.clock()
        state.task = task

    def finish(self, token: str) -> None:
        self._leases.pop(token, None)

    def mark_release_pending(self, token: str) -> None:
        """执行已经结束、但数据库释放失败；下一次请求优先重试释放。"""

        state = self._leases.get(token)
        if state is not None:
            state.task = None
            state.release_pending = True

    def is_active(self, token: str) -> bool:
        state = self._leases.get(token)
        if state is None:
            return False
        if state.release_pending:
            return True
        if state.task is not None:
            return not state.task.done()
        return True

    def orphans(self) -> list[str]:
        """只返回能确认已经结束的执行。

        - ``prepared`` 且超过回收门槛、从未开始执行：请求在建立 SSE 前消失；
        - ``started`` 但任务已经结束：生成器没有走完 finally 的兜底；
        - ``release_pending``：执行已结束，但上一次数据库释放失败。
        仍存活的任务无论多久都不回收。
        """

        now = self.clock()
        result: list[str] = []
        for token, state in list(self._leases.items()):
            if state.release_pending:
                result.append(token)
                continue
            if state.task is not None:
                if state.task.done():
                    result.append(token)
                continue
            if now - state.created_at >= self.reclaim_seconds:
                result.append(token)
        return result