"""F008：原生工具调用闭环（决策 → 执行 → 观察 → 再决策 / 结束）。

用例直接驱动 ``DynamicReviewGraph`` + 真实 ``SchedulerAgent``，只把模型换成
按脚本返回 ``tool_calls`` 的替身，验证真实协议接线：

* 工具结果必须以 ``role="tool"`` + 同一 ``tool_call_id`` 回传；
* 拒绝（未知工具 / 参数非法 / 超量）不执行，但仍要回传并计入预算；
* 工具阶段的模型文字不当作最终回复；
* 决策轮次、单响应调用数和执行预算都有硬上限。

注意：本轮协议只在“出现问题时”重新决策。一次响应里全部成功时，同一响应内
的多个调用按顺序执行完即结束，不再额外调用模型（省一次调用）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.conversations.graph import (
    MAX_EXECUTED_ACTIONS,
    MAX_SCHEDULER_ROUNDS,
    MAX_TOOL_CALLS_PER_RESPONSE,
    ActionOutcome,
    DynamicReviewGraph,
    ExecutionResult,
    SchedulerAgent,
)
from tests.tool_fakes import FakeToolModel, tool_call


def _success(action: str, message: str = "已完成", **details: Any) -> ExecutionResult:
    return ExecutionResult(
        outcome=ActionOutcome(
            action=action, status="success", message=message, details=details
        )
    )


class Recorder:
    """记录执行器收到的动作；可按动作预置结果。"""

    def __init__(self, outcomes: dict[str, ExecutionResult] | None = None):
        self.actions: list[Any] = []
        self._outcomes = outcomes or {}

    async def __call__(self, action, state):
        self.actions.append(action)
        preset = self._outcomes.get(action.action)
        if preset is not None:
            return preset
        return _success(action.action, f"{action.action} 完成")

    @property
    def names(self) -> list[str]:
        return [action.action for action in self.actions]


def _run(scheduler: SchedulerAgent, execute: Recorder, **kwargs: Any):
    graph = DynamicReviewGraph(scheduler, execute, **kwargs)
    return graph.run({"query": "帮我查 Python 期末卷"})


def _tool_messages(messages: list[Any]) -> list[dict[str, Any]]:
    return [item for item in messages if isinstance(item, dict) and item.get("role") == "tool"]


def _transcript(scheduler: SchedulerAgent) -> list[Any]:
    """调度器的真实会话历史（system / user / assistant / tool 依次累积）。"""

    return list(getattr(scheduler, "_messages", None) or [])


def _assistant_tool_ids(messages: list[Any]) -> list[str]:
    for item in messages:
        if isinstance(item, dict) and item.get("role") == "assistant" and item.get("tool_calls"):
            return [call["id"] for call in item["tool_calls"]]
    return []


@pytest.mark.anyio
async def test_successful_batch_returns_to_model_then_finishes() -> None:
    """一批工具成功后必须回到决策：模型可以接着安排下一步，也可以收尾。

    这里模型第二轮不再返回 tool_calls，于是循环结束；规划阶段的文字依旧
    不会变成最终回复。
    """

    model = FakeToolModel(
        [
            {
                "content": "我先查一下试卷。",
                "tool_calls": [tool_call("query_papers", {"course": "Python"}, call_id="call_a")],
            },
            {"content": "查完了。"},
        ]
    )
    execute = Recorder({"query_papers": _success("query_papers", "找到 2 份", count=2)})

    scheduler = SchedulerAgent(model=model)
    state = await _run(scheduler, execute)

    assert execute.names == ["query_papers"]
    # 一次规划 + 一次收尾决策才是完整的工具循环。
    assert len(model.calls) == 2
    assert state["rounds"] == 2
    # 模型被真实绑定了 14 个工具；历史里留下了可对应的 assistant tool_calls
    # 和同 id 的 tool 结果，下一次决策/故障恢复才能对上号。
    assert model.bound_tools is not None and len(model.bound_tools) == 14
    transcript = _transcript(scheduler)
    assert _assistant_tool_ids(transcript) == ["call_a"]
    assert [
        item["tool_call_id"] for item in transcript if item.get("role") == "tool"
    ] == ["call_a"]
    # 工具阶段的说明没有被转发；收尾决策（没有 tool_calls）的话可以留用。
    assert state["assistant_reply"] == "查完了。"
    assert "我先查一下试卷" not in (state["assistant_reply"] or "")
    assert state["observations"][0]["status"] == "success"
    assert state["observations"][0]["details"]["count"] == 2


@pytest.mark.anyio
async def test_multiple_calls_in_one_response_execute_in_order() -> None:
    """一次响应里的多个调用按顺序执行，各自留下可回传的 tool_call_id。"""

    model = FakeToolModel(
        [
            {
                "tool_calls": [
                    tool_call("query_papers", {"course": "Python"}, call_id="c1"),
                    tool_call("search_questions", {"course": "Python"}, call_id="c2"),
                ]
            }
        ]
    )
    execute = Recorder()

    scheduler = SchedulerAgent(model=model)
    state = await _run(scheduler, execute)

    assert execute.names == ["query_papers", "search_questions"]
    assert [obs["action"] for obs in state["observations"]] == [
        "query_papers",
        "search_questions",
    ]
    # 两条结果各自带上自己的 tool_call_id，顺序与执行顺序一致。
    ids = [
        item["tool_call_id"]
        for item in _transcript(scheduler)
        if item.get("role") == "tool"
    ]
    assert ids == ["c1", "c2"]


@pytest.mark.anyio
async def test_unknown_tool_is_rejected_observed_and_not_executed() -> None:
    """未知工具不执行，但仍以同一 tool_call_id 回传失败结果，让模型改正。"""

    model = FakeToolModel(
        [
            {"tool_calls": [tool_call("delete_database", {}, call_id="bad")]},
            {"tool_calls": [tool_call("query_papers", {"course": "Python"}, call_id="ok")]},
            {"content": "已改用正确工具。"},
        ]
    )
    execute = Recorder()

    state = await _run(SchedulerAgent(model=model), execute)

    assert execute.names == ["query_papers"]
    assert state["rejected_count"] == 1
    assert len(model.calls) <= MAX_SCHEDULER_ROUNDS
    feedback = _tool_messages(model.calls[1])
    assert feedback[0]["tool_call_id"] == "bad"
    payload = json.loads(feedback[0]["content"])
    assert payload["status"] == "failed"
    assert payload["error_code"] == "UNKNOWN_TOOL"
    assert any(item.get("executed") is False for item in state["observations"])


@pytest.mark.anyio
async def test_invalid_arguments_are_rejected_and_retry_is_allowed() -> None:
    """参数非法同样回传失败；模型用正确参数重试后被真正执行。"""

    model = FakeToolModel(
        [
            {"tool_calls": [tool_call("confirm_course", {}, call_id="bad")]},
            {"tool_calls": [tool_call("confirm_course", {"course": "Python"}, call_id="good")]},
            {"content": "已确认 Python。"},
        ]
    )
    execute = Recorder()

    state = await _run(SchedulerAgent(model=model), execute)

    assert execute.names == ["introduce"]
    assert state["rejected_count"] == 1
    assert state["executed_count"] == 1
    payload = json.loads(_tool_messages(model.calls[1])[0]["content"])
    assert payload["error_code"] == "INVALID_ARGUMENTS"
    # 修正成功后允许再决策一次收尾，但不会无限制地重试。
    assert len(model.calls) <= MAX_SCHEDULER_ROUNDS


@pytest.mark.anyio
async def test_calls_beyond_per_response_limit_are_rejected_not_executed() -> None:
    """单次响应最多 3 个调用；第 4 个被拒绝且不执行，但必须有 tool 结果。"""

    # 四个不同动作：同动作会被“单次动作”规则跳过，测不到调用数量上限。
    calls = [
        tool_call("query_papers", {"course": "Python"}, call_id="c1"),
        tool_call("search_questions", {"course": "Python"}, call_id="c2"),
        tool_call("explain_problem", {"mode": "new"}, call_id="c3"),
        tool_call("select_question", {"index": 1}, call_id="c4"),
    ]
    model = FakeToolModel([{"tool_calls": calls}])
    execute = Recorder()

    state = await _run(SchedulerAgent(model=model), execute)

    assert len(execute.names) == MAX_TOOL_CALLS_PER_RESPONSE
    feedback = {
        item["tool_call_id"]: json.loads(item["content"])
        for item in _tool_messages(model.calls[1])
    }
    assert set(feedback) == {"c1", "c2", "c3", "c4"}
    assert feedback["c4"]["error_code"] == "TOO_MANY_CALLS"
    assert feedback["c1"]["status"] == "ok"
    assert state["rejected_count"] == 1


@pytest.mark.anyio
async def test_dependency_failure_skips_following_action() -> None:
    """前置查题失败时，讲题被跳过而不是假装完成。"""

    model = FakeToolModel(
        [
            {
                "tool_calls": [
                    tool_call("search_questions", {"course": "Python"}, call_id="s1"),
                    tool_call("explain_problem", {"mode": "new"}, call_id="e1"),
                ]
            },
            {"content": "没有找到真题，先按你贴的题干讲。"},
        ]
    )
    execute = Recorder(
        {
            "search_questions": ExecutionResult(
                outcome=ActionOutcome(
                    action="search_questions", status="failed", message="题库为空"
                )
            )
        }
    )

    state = await _run(SchedulerAgent(model=model), execute)

    assert execute.names == ["search_questions"]
    skipped = [item for item in state["observations"] if item["action"] == "explain_problem"]
    assert skipped and skipped[0]["details"]["skipped"] is True
    feedback = _tool_messages(model.calls[1])
    assert json.loads(feedback[1]["content"])["details"]["skipped"] is True


@pytest.mark.anyio
async def test_action_budget_stops_execution_and_marks_remaining_skipped() -> None:
    """执行预算用尽后，剩余调用标记 skipped，不会被执行。"""

    # 一次响应里的第三个动作超出执行预算（max_actions=2），必须被跳过。
    batch = [
        tool_call("query_papers", {"course": "Python"}, call_id="a"),
        tool_call("search_questions", {"course": "Python"}, call_id="b"),
        tool_call("select_question", {"index": 1}, call_id="c"),
    ]
    model = FakeToolModel([{"tool_calls": batch}, {"content": "先到这里。"}])
    execute = Recorder()

    state = await _run(SchedulerAgent(model=model), execute, max_actions=2)

    assert len(execute.names) == 2
    assert state["executed_count"] == 2
    skipped = [item for item in state["observations"] if item["details"].get("skipped")]
    assert skipped and skipped[0]["action"] == "select_question"


@pytest.mark.anyio
async def test_needs_user_stops_round_and_reports_remaining_as_waiting() -> None:
    """需要学生回答时立即结束，其余调用回传“等待学生”，不再调用模型。"""

    model = FakeToolModel(
        [
            {
                "tool_calls": [
                    tool_call("ask_user", {"question": "你想先看哪份试卷？"}, call_id="q1"),
                    tool_call("query_papers", {"course": "Python"}, call_id="p1"),
                ]
            }
        ]
    )
    execute = Recorder(
        {
            "clarify_or_reply": ExecutionResult(
                outcome=ActionOutcome(
                    action="clarify_or_reply",
                    status="clarification",
                    message="你想先看哪份试卷？",
                    needs_user=True,
                )
            )
        }
    )

    scheduler = SchedulerAgent(model=model)
    state = await _run(scheduler, execute)

    assert execute.names == ["clarify_or_reply"]
    assert len(model.calls) == 1
    waiting = [
        json.loads(item["content"])
        for item in _transcript(scheduler)
        if item.get("role") == "tool"
    ]
    assert len(waiting) == 2
    assert waiting[1]["details"]["waiting_user"] is True
    assert state["assistant_reply"] == "你想先看哪份试卷？"


@pytest.mark.anyio
async def test_scheduler_rounds_are_bounded() -> None:
    """模型一直触发问题并重试时，决策次数不超过上限。"""

    responses = [
        {"tool_calls": [tool_call("nope_tool", {}, call_id=f"c{i}")]}
        for i in range(MAX_SCHEDULER_ROUNDS + 3)
    ]
    model = FakeToolModel(responses)
    execute = Recorder()

    state = await _run(SchedulerAgent(model=model), execute)

    assert len(model.calls) <= MAX_SCHEDULER_ROUNDS
    assert execute.names == []
    assert state["pending"] == []
    assert state["rounds"] >= MAX_SCHEDULER_ROUNDS


@pytest.mark.anyio
async def test_same_action_is_not_executed_twice_in_one_turn() -> None:
    """重新决策再次调用同一动作时，按单次动作规则跳过，避免重复副作用。"""

    model = FakeToolModel(
        [
            {"tool_calls": [tool_call("save_mistake", {}, call_id="m1")]},
            {"tool_calls": [tool_call("save_mistake", {}, call_id="m2")]},
            {"content": "错题本暂时没写成。"},
        ]
    )
    execute = Recorder(
        {
            "record_mistake": ExecutionResult(
                outcome=ActionOutcome(
                    action="record_mistake", status="failed", message="存储暂不可用"
                )
            )
        }
    )

    state = await _run(SchedulerAgent(model=model), execute)

    assert execute.names == ["record_mistake"]
    assert any(
        item["action"] == "record_mistake" and item["details"].get("skipped")
        for item in state["observations"]
    )
    assert int(state.get("executed_count", 0)) == 1