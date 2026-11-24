"""F017：Agent 智能度与灵活度修复回归测试。

验证：
1. 未确认课程时不默认用 Python 冒充题库能力；confirm_course 如实透传无题库与试卷数。
2. search_questions 返回 bank_not_built 时不阻断决策循环，允许同轮继续自主出题或查卷。
3. 无已讲解原题时允许按 course + topic 自主生成非真题练习，并保持既有原题/选题守卫。
4. 纯知识点答疑不调工具时 _finalize_messages 不留空占位消息。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision, SchedulerAgent
from app.conversations.tools import GenerateExerciseArgs, TOOLS_BY_NAME
from app.memory.evidence import KnowledgeNormalizer, _standalone_anchor_course, collect_all
from app.questions.models import QuestionCapabilityView, QuestionSearchResult
from app.tutoring.exercise_llm import GeneratedExercise
from tests.test_f010b_support import dummy_request, make_harness, turn_input
from tests.tutoring_fakes import FakeExerciseAgent, FakeScheduler


def _result_of(events: list) -> object:
    return next(event for event in events if event.type == "result")


@pytest.mark.anyio
async def test_confirm_course_exposes_catalog_only_status_and_paper_count(
    tmp_path: Path,
) -> None:
    scheduler = FakeScheduler(
        decisions=[
            ScheduleDecision(
                actions=[ActionPlan(action="introduce")],
                confirmed_course="高等数学",
            ),
            ScheduleDecision(actions=[], assistant_reply="已确认高等数学。"),
        ]
    )
    harness = make_harness(tmp_path, scheduler=scheduler)
    seen_cap_courses: list[str] = []

    def _cap(course: str) -> QuestionCapabilityView:
        seen_cap_courses.append(course)
        if "高等数学" in course:
            return QuestionCapabilityView(
                course=course,
                status="catalog_only",
                message="该课程目前只有试卷目录，没有真题题目库；可查看或下载原卷。",
            )
        return QuestionCapabilityView(
            course=course,
            status="ready",
            searchable=10,
            approved=10,
            message="支持按考点检索历年真题。",
        )

    harness.questions.capability = _cap
    conversation = harness.service.create_conversation()

    prepared = harness.service.prepare_turn(conversation.id, turn_input("我要复习高数"))
    events = [
        event async for event in harness.service.stream_prepared(prepared, dummy_request())
    ]
    result = _result_of(events)

    assert result.business_status == "succeeded"
    # 未确认课程的首轮调用不能用 Python 冒充已确认课程能力。
    first_context = scheduler.calls[0]["question_context"]
    assert "尚未确认" in first_context
    assert "真题" in first_context
    assert "Python" not in first_context
    assert "Python语言程序设计" not in seen_cap_courses
    assert "高等数学" in seen_cap_courses

    intro_item = next(item for item in result.composite_results if item.action == "introduce")
    assert intro_item.status == "success"
    assert intro_item.details["course"] == "高等数学"
    assert intro_item.details["has_question_bank"] is False
    assert intro_item.details["bank_status"] == "catalog_only"
    assert isinstance(intro_item.details["paper_count"], int)
    assert intro_item.details["paper_count"] >= 0
    assert "没有真题" in intro_item.message or "目录" in intro_item.message


@pytest.mark.anyio
async def test_search_bank_not_built_allows_same_turn_standalone_exercise(
    tmp_path: Path,
) -> None:
    owner_id = "f017-course-memory-owner"
    scheduler = FakeScheduler(
        decisions=[
            ScheduleDecision(
                actions=[
                    ActionPlan(
                        action="search_questions",
                        parameters={"course": "高等数学", "query": "求极限"},
                    )
                ],
                confirmed_course="高等数学",
            ),
            ScheduleDecision(
                actions=[
                    ActionPlan(
                        action="generate_exercise",
                        parameters={
                            "course": "高等数学",
                            "topic": "洛必达法则求极限",
                            "from_question": False,
                            "basis": {"kind": "student_request", "quote": "来一道极限题"},
                        },
                    )
                ],
                confirmed_course="高等数学",
            ),
        ]
    )
    harness = make_harness(tmp_path, scheduler=scheduler, owner_id=owner_id)
    # 显式模拟 FakeQuestionRuntime 对非 Python 课程返回 bank_not_built。
    harness.questions.capability = lambda course: QuestionCapabilityView(
        course=course,
        status="catalog_only",
        message="该课程目前只有试卷目录，没有真题题目库；可查看或下载原卷。",
    )
    harness.questions.search = lambda req, **_kwargs: QuestionSearchResult(
        status="bank_not_built",
        message="高等数学暂未建设单题真题库。",
        cards=[],
        total=0,
    )

    conversation = harness.service.create_conversation()
    prepared = harness.service.prepare_turn(conversation.id, turn_input("来一道极限题"))
    events = [
        event async for event in harness.service.stream_prepared(prepared, dummy_request())
    ]
    result = _result_of(events)

    # bank_not_built 不再短路阻断循环，调度器得以第二次进入并在同轮继续生成练习题。
    assert len(scheduler.calls) >= 2
    search_item = next(
        item for item in result.composite_results if item.action == "search_questions"
    )
    assert search_item.status == "success"
    assert search_item.details.get("has_question_bank") is False

    gen_item = next(
        item for item in result.composite_results if item.action == "generate_exercise"
    )
    assert gen_item.status == "success"
    assert gen_item.details.get("generated_mode") == "standalone"
    assert result.exercise is not None
    assert result.exercise.source_question is None
    assert result.exercise.knowledge_point

    scheduler.push(ScheduleDecision(
        actions=[ActionPlan(action="introduce")],
        confirmed_course="线性代数",
    ))
    switched = harness.service.prepare_turn(conversation.id, turn_input("接下来复习线性代数"))
    switch_events = [event async for event in harness.service.stream_prepared(switched, dummy_request())]
    assert _result_of(switch_events).business_status == "succeeded"
    context = harness.repo.get_context(conversation.id)
    assert context is not None and context.confirmed_course == "线性代数"
    with closing(sqlite3.connect(harness.db)) as connection:
        connection.row_factory = sqlite3.Row
        entries = collect_all(
            connection,
            owner_id=owner_id,
            paper_lookup=lambda _: (None, None),
            normalizer=KnowledgeNormalizer(),
            corrections={},
            exclusions=set(),
            current_build_id=None,
        ).entries
    entry = next(entry for entry in entries if entry.source["exercise_id"] == result.exercise.id)
    assert (entry.course, entry.course_source) == ("高等数学", "problem")


def test_standalone_anchor_without_course_does_not_use_separator_in_topic() -> None:
    topic = "极限 · 连续性"
    assert _standalone_anchor_course(f"【自主练习主题】{topic}", topic) is None
    assert _standalone_anchor_course(f"【自主练习主题】高等数学 · {topic}", topic) == "高等数学"


@pytest.mark.anyio
async def test_standalone_exercise_anchor_hides_reference_solution_and_keeps_from_question_guard(
    tmp_path: Path,
) -> None:
    sentinel_solution = "SECRET-STANDALONE-SOLUTION-DO-NOT-LEAK"
    fake_agent = FakeExerciseAgent(
        exercises=[
            GeneratedExercise(
                statement="计算极限 lim_{x->0} (sin x - x) / x^3。",
                knowledge_point="泰勒公式求极限",
                reference_solution=sentinel_solution,
            )
        ]
    )
    scheduler = FakeScheduler(
        decisions=[
            ScheduleDecision(
                actions=[
                    ActionPlan(
                        action="generate_exercise",
                        parameters={
                            "course": "高等数学",
                            "topic": "泰勒公式求极限",
                            "from_question": False,
                            "basis": {"kind": "student_request", "quote": "自己出一道给我"},
                        },
                    )
                ],
                confirmed_course="高等数学",
            ),
            ScheduleDecision(actions=[], assistant_reply="已按考点为你生成练习题（非真题）。"),
        ]
    )
    harness = make_harness(tmp_path, scheduler=scheduler)
    harness.service._make_exercise_agent = lambda: fake_agent  # noqa: SLF001

    conversation = harness.service.create_conversation()
    prepared = harness.service.prepare_turn(conversation.id, turn_input("自己出一道给我"))
    events = [
        event async for event in harness.service.stream_prepared(prepared, dummy_request())
    ]
    result = _result_of(events)

    assert result.exercise is not None
    assert "泰勒" in result.exercise.knowledge_point
    assert sentinel_solution not in result.exercise.statement_text
    # 传入 generate_exercise 的参数确实走无原题独立出题分支
    assert fake_agent.generate_calls[-1]["original_statement"] is None
    assert fake_agent.generate_calls[-1]["topic"] == "泰勒公式求极限"

    # 数据库中锚点题目外键存在，且锚点 explanation 绝不泄露完整参考解答
    anchor = harness.repo.get_problem(result.exercise.problem_id)
    assert anchor is not None and anchor.explanation is not None
    assert sentinel_solution not in anchor.explanation.final_answer
    assert all(sentinel_solution not in step.content for step in anchor.explanation.steps)

    # 若显式要求基于真题出题（from_question=True）而无已讲解原题，仍需返回待澄清。
    scheduler_guard = FakeScheduler(
        decisions=[
            ScheduleDecision(
                actions=[
                    ActionPlan(
                        action="generate_exercise",
                        parameters={
                            "from_question": True,
                            "topic": "泰勒公式",
                            "basis": {"kind": "student_request", "quote": "根据真题再出一道"},
                        },
                    )
                ],
                confirmed_course="高等数学",
            )
        ]
    )
    harness2 = make_harness(tmp_path / "guard", scheduler=scheduler_guard)
    conv2 = harness2.service.create_conversation()
    prep2 = harness2.service.prepare_turn(conv2.id, turn_input("根据真题再出一道"))
    ev2 = [event async for event in harness2.service.stream_prepared(prep2, dummy_request())]
    res2 = _result_of(ev2)
    assert res2.business_status == "needs_input"
    assert res2.exercise is None


def test_finalize_messages_drops_empty_assistant_when_no_tools_called() -> None:
    scheduler = SchedulerAgent(SimpleNamespace(deepseek=None))
    scheduler._messages = [  # noqa: SLF001 - 单测检查消息清理结果
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "什么是夹逼准则？"},
        {"role": "assistant", "content": "准备直接回答。"},
    ]
    scheduler._turn_start = 1  # noqa: SLF001

    cleaned = scheduler._finalize_messages()  # noqa: SLF001
    assert [m["role"] for m in cleaned] == ["system", "user"]

    spec = TOOLS_BY_NAME["generate_exercise"]
    args = GenerateExerciseArgs(course="高等数学", topic="洛必达法则", from_question=False)
    assert args.topic == "洛必达法则"
    assert {"course", "topic"} <= set(spec.schema["properties"])
