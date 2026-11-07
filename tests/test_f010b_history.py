"""F010 B：跨轮历史投影（答案边界、配对、预算、幂等）。"""

from __future__ import annotations

import json

from app.conversations.history import (
    HISTORY_MAX_CHARS,
    HISTORY_MAX_MESSAGES,
    build_history,
    disclosure_of,
    history_stats,
    project_turn,
)
from app.conversations.models import (
    ActionResultItem,
    ExerciseEvaluation,
    Turn,
)
from tests.question_fakes import sample_card


def make_turn(
    seq: int,
    *,
    request_id: str | None = None,
    query: str = "问题",
    assistant: str = "回答",
    status: str = "completed",
    kind: str = "dynamic",
    **kwargs,
) -> Turn:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return Turn(
        request_id=request_id or f"req-{seq}",
        seq=seq,
        query=query,
        status=status,
        kind=kind,
        assistant_message=assistant,
        started_at=now,
        finished_at=None if status == "running" else now,
        **kwargs,
    )


def transcript(*actions: tuple[str, str]) -> dict:
    steps = []
    for index, (action, tool) in enumerate(actions, start=1):
        steps.append(
            {
                "id": f"step-{index}",
                "call_id": f"call-{index}",
                "tool": tool,
                "plan": {"action": action, "parameters": {"number_label": "5"}},
                "outcome": {
                    "status": "success",
                    "message": "ok",
                    "details": {
                        "answer_detail": "F010B-ANSWER-A",
                        "student_answer_text": "学生私有作答",
                        "number_label": "5",
                    },
                },
            }
        )
    return {"version": 1, "steps": steps}


def test_answer_disclosure_is_replaced_by_structured_fact() -> None:
    turn = make_turn(
        1,
        assistant="答案是 B（合成标记 F010B-ANSWER-A）",
        composite_results=[
            ActionResultItem(action="view_question_answer", status="success", message="ok")
        ],
        question=sample_card(number="5"),
    )
    assert disclosure_of(turn) == "answer"
    block = project_turn(turn, transcript(("view_question_answer", "show_question_answer")))
    assert block is not None
    assert "F010B-ANSWER-A" not in json.dumps(block, ensure_ascii=False)
    assert "第 5 题" in block["assistant"]
    # 工具结果里的答案字段也不进入历史。
    assert "answer_detail" not in json.dumps(block, ensure_ascii=False)


def test_exercise_solution_and_unknown_legacy_are_redacted() -> None:
    evaluation = ExerciseEvaluation(
        final_answer_assessment="过程基本正确",
        step_by_step_analysis="第一步…",
        guidance="注意边界",
    )
    submitted = make_turn(1, assistant="参考解答：F010B-SOLUTION-A", evaluation=evaluation)
    assert disclosure_of(submitted) == "exercise"
    assert "F010B-SOLUTION-A" not in project_turn(submitted, None)["assistant"]

    legacy = make_turn(2, kind="exercise", assistant="F010B-SOLUTION-A")
    assert disclosure_of(legacy) == "unknown"
    assert "F010B-SOLUTION-A" not in project_turn(legacy, None)["assistant"]


def test_plain_turns_keep_text_and_pair_tool_messages() -> None:
    turn = make_turn(1)
    messages = build_history(
        [turn],
        {"req-1": transcript(("select_question", "select_question"))},
        current_request_id="req-next",
    )
    roles = [item["role"] for item in messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert messages[0]["content"] == "问题"
    assert messages[1]["tool_calls"][0]["function"]["name"] == "select_question"
    assert messages[2]["tool_call_id"] == messages[1]["tool_calls"][0]["id"]


def test_hard_limits_and_every_tool_has_a_call_pair() -> None:
    turns = [make_turn(index, query=f"第{index}轮", assistant="说明" * 200) for index in range(1, 41)]
    transcripts = {
        turn.request_id: transcript(("search_questions", "search_questions")) for turn in turns
    }
    messages = build_history(turns, transcripts, current_request_id="req-current")
    stats = history_stats(messages)
    assert stats["messages"] <= HISTORY_MAX_MESSAGES
    assert stats["chars"] <= HISTORY_MAX_CHARS
    # 配对：每条 tool 消息都能在前面找到同 id 的 assistant tool_calls。
    pending: set[str] = set()
    for item in messages:
        if item["role"] == "assistant":
            pending = {call["id"] for call in item.get("tool_calls") or []}
        elif item["role"] == "tool":
            assert item["tool_call_id"] in pending
            pending.discard(item["tool_call_id"])


def test_history_is_deterministic_and_skips_current_and_running() -> None:
    turns = [
        make_turn(1, assistant="第一轮回答"),
        make_turn(2, status="running", assistant=None),
        make_turn(3, assistant="第三轮回答"),
    ]
    transcripts = {turn.request_id: transcript(("view_workspace", "view_workspace")) for turn in turns}
    first = build_history(turns, transcripts, current_request_id="req-3")
    second = build_history(turns, transcripts, current_request_id="req-3")
    assert first == second
    joined = json.dumps(first, ensure_ascii=False)
    assert "第三轮回答" not in joined
    assert "第一轮回答" in joined


def test_old_turn_without_transcript_uses_text_only() -> None:
    turn = make_turn(1, assistant="旧轮次的回答")
    messages = build_history([turn], {}, current_request_id="req-x")
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "旧轮次的回答"