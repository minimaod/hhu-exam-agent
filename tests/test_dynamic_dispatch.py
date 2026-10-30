"""F006 动态复习对话调度、意图识别与上下文指代测试。"""

import json
from pathlib import Path
from uuid import uuid4
import pytest
from starlette.requests import Request

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.mistakes.service import MistakeRecordService
from app.papers.repository import PaperRepository
from app.papers.service import DEFAULT_PAPERS_ROOT, PaperService
from app.tutoring.models import Source
from tests.tutoring_fakes import FakeExerciseAgent, FakeScheduler, FakeTutor, sample_explanation


def make_dummy_request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/conversations/test/turns",
        "headers": [],
    }
    return Request(scope)


def setup_service(tmp_path: Path):
    db_path = tmp_path / "study.sqlite3"
    repo = ConversationRepository(db_path)
    repo.initialize()

    papers_repo = PaperRepository(DEFAULT_PAPERS_ROOT)
    papers = PaperService(papers_repo)
    mistakes_service = MistakeRecordService(
        repository=repo,
        papers=papers,
        make_tutor=lambda: tutor,
        tutoring_error=None,
        attachments_root=tmp_path / "attachments",
    )

    scheduler = FakeScheduler()
    ex_agent = FakeExerciseAgent()
    tutor = FakeTutor(explanations=[sample_explanation()])

    svc = ConversationService(
        repository=repo,
        make_query=None,
        query_error=None,
        make_tutor=lambda: tutor,
        make_router=None,
        make_exercise_agent=lambda: ex_agent,
        make_scheduler=lambda: scheduler,
        papers=papers,
        records=mistakes_service,
    )
    return svc, repo, scheduler, ex_agent, tutor


@pytest.mark.anyio
async def test_broad_demand_introduces_and_sets_course(tmp_path: Path) -> None:
    """“我要复习 Python”：确认科目，介绍能力并持久化 confirmed_course。"""
    svc, repo, scheduler, _, _ = setup_service(tmp_path)
    c = repo.create_conversation()

    scheduler.push(
        ScheduleDecision(
            confirmed_course="Python",
            actions=[
                ActionPlan(action="introduce", reason="用户表达宽泛复习需求")
            ],
            assistant_reply="已确认复习科目为 Python。我可以帮您查找历年试卷、答疑讲解题目或进行考点练习。",
            suggested_actions=["找 Python 期末卷", "拍照上传题目"],
        )
    )

    t_input = TurnInput(
        request_id=str(uuid4()),
        kind=None,
        dispatch=True,
        text="我要复习 Python",
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=False,
    )

    prepared = svc.prepare_turn(c.id, t_input)
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]

    res_event = next(e for e in events if e.type == "result")
    assert res_event.kind == "dynamic"
    # F008：带工具调用的决策轮不再转发模型散文，正文由已执行结果拼出。
    assert "Python" in res_event.assistant_message
    assert any(it.action == "introduce" for it in res_event.composite_results)

    # 验证上下文持久化
    ctx = repo.get_context(c.id)
    assert ctx is not None
    assert ctx.confirmed_course == "Python"


@pytest.mark.anyio
async def test_context_course_inheritance_for_papers(tmp_path: Path) -> None:
    """在已确认科目后，“给我期末试卷”自动绑定科目进行查卷。"""
    svc, repo, scheduler, _, _ = setup_service(tmp_path)
    c = repo.create_conversation()
    repo.upsert_context(c.id, confirmed_course="高等数学")

    scheduler.push(
        ScheduleDecision(
            confirmed_course="高等数学",
            actions=[
                ActionPlan(
                    action="query_papers",
                    parameters={"course": "高等数学", "exam_type": "final"},
                    reason="根据上下文查期末卷",
                )
            ],
            assistant_reply="已为您找到高等数学期末试卷。",
        )
    )

    t_input = TurnInput(
        request_id=str(uuid4()),
        kind=None,
        dispatch=True,
        text="给我期末试卷",
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=False,
    )

    prepared = svc.prepare_turn(c.id, t_input)
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]

    res_event = next(e for e in events if e.type == "result")
    assert res_event.result is not None
    assert res_event.result.filters.course == "高等数学"


@pytest.mark.anyio
async def test_understood_does_not_trigger_exercise_or_record(tmp_path: Path) -> None:
    """“我懂了”：只记录反馈，绝不自动出题或保存错题。"""
    svc, repo, scheduler, _, _ = setup_service(tmp_path)
    c = repo.create_conversation()

    # 先插入一道讲解完成的原题
    with repo._write() as conn:
        conn.execute(
            """
            INSERT INTO problems (
                id, conversation_id, root_problem_id, version,
                statement_text, status, clarity, source_type,
                active, created_at, updated_at
            ) VALUES ('p-under', ?, 'p-under', 1, '题干', 'explained', 'clear', 'none', 1, '2026-01-01', '2026-01-01')
            """,
            (c.id,),
        )

    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="record_feedback", reason="学生自述理解")],
            assistant_reply="很高兴您已掌握！随时可以继续追问、练题或保存到错题本。",
            suggested_actions=["练一道类似题", "保存到错题本"],
        )
    )

    t_input = TurnInput(
        request_id=str(uuid4()),
        kind=None,
        dispatch=True,
        text="我懂了",
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=False,
    )

    prepared = svc.prepare_turn(c.id, t_input)
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]

    res_event = next(e for e in events if e.type == "result")
    # 核心约束：不能出题，不能建练习
    assert res_event.exercise is None
    # 验证原题自述理解状态更新
    p = repo.get_problem("p-under")
    assert p is not None and p.self_reported_understood is True


@pytest.mark.anyio
async def test_ambiguous_yes_response_asks_clarification(tmp_path: Path) -> None:
    """多选项后只回答“好”且无法判定指向时，必须澄清，不能擅自出题或保存。"""
    svc, repo, scheduler, _, _ = setup_service(tmp_path)
    c = repo.create_conversation()

    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="clarify_or_reply", reason="用户回答好产生歧义")],
            assistant_reply="请问您是希望先做一道类似题练习，还是先保存到错题本？",
            suggested_actions=["练一道类似题", "保存到错题本"],
        )
    )

    t_input = TurnInput(
        request_id=str(uuid4()),
        kind=None,
        dispatch=True,
        text="好",
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=False,
    )

    prepared = svc.prepare_turn(c.id, t_input)
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]

    res_event = next(e for e in events if e.type == "result")
    assert "请问您是希望" in res_event.assistant_message
    assert res_event.exercise is None


@pytest.mark.anyio
async def test_rejection_friendly_acknowledgement(tmp_path: Path) -> None:
    """用户说“不用了”：温和确认并不再强推。"""
    svc, repo, scheduler, _, _ = setup_service(tmp_path)
    c = repo.create_conversation()

    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="clarify_or_reply", reason="用户拒绝建议")],
            assistant_reply="好的，那我们先不练习也不保存。您有其他题目或想复习的内容可以随时告诉我。",
        )
    )

    t_input = TurnInput(
        request_id=str(uuid4()),
        kind=None,
        dispatch=True,
        text="不用了",
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=False,
    )

    prepared = svc.prepare_turn(c.id, t_input)
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]

    res_event = next(e for e in events if e.type == "result")
    assert "好的，那我们先不练习" in res_event.assistant_message
    assert res_event.exercise is None


@pytest.mark.anyio
async def test_composite_actions_save_and_exercise(tmp_path: Path) -> None:
    """复合指令：“保存这题，再来一道类似题”，分别反馈执行结果，单轮模型调用严格受控。"""
    svc, repo, scheduler, ex_agent, _ = setup_service(tmp_path)
    c = repo.create_conversation()

    # 插入一道已完成讲解的原题
    expl_json = sample_explanation().model_dump_json()
    with repo._write() as conn:
        conn.execute(
            """
            INSERT INTO problems (
                id, conversation_id, root_problem_id, version,
                statement_text, status, clarity, source_type,
                explanation_json, active, created_at, updated_at
            ) VALUES (
                'p-comp', ?, 'p-comp', 1, '求解极限 lim x->0 sin(x)/x', 'explained', 'clear', 'none',
                ?, 1, '2026-01-01', '2026-01-01'
            )
            """,
            (c.id, expl_json),
        )

    scheduler.push(
        ScheduleDecision(
            actions=[
                ActionPlan(action="record_mistake", reason="保存错题"),
                ActionPlan(action="generate_exercise", reason="生成类似题"),
            ],
            assistant_reply="已为您保存该题至错题本，并生成了一道考点类似的练习题，请作答：",
            suggested_actions=["查看解答"],
        )
    )

    t_input = TurnInput(
        request_id=str(uuid4()),
        kind=None,
        dispatch=True,
        text="保存这题，再来一道类似题",
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=False,
    )

    prepared = svc.prepare_turn(c.id, t_input)
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]

    res_event = next(e for e in events if e.type == "result")
    assert res_event.kind == "dynamic"
    # F008：正文来自两条真实执行结果（保存错题 + 生成练习），不是规划时的散文。
    assert "错题本" in res_event.assistant_message
    assert "练习" in res_event.assistant_message

    # 验证复合操作各自反馈
    actions_done = [item.action for item in res_event.composite_results]
    assert "record_mistake" in actions_done
    assert "generate_exercise" in actions_done

    # 验证错题已保存
    record_status = next(it for it in res_event.composite_results if it.action == "record_mistake")
    assert record_status.status == "success"

    # 验证练习题生成，且待作答状态下参考解答防泄露
    assert res_event.exercise is not None
    assert res_event.exercise.reference_solution_text is None
    assert res_event.exercise.status == "pending_answer"


@pytest.mark.anyio
@pytest.mark.parametrize("text,course,planned,expected,total", [
    ("查看高等数学试卷", "高等数学", "final", None, 14),
    ("查看 Python 语言程序设计试卷", "Python", "final", None, 20),
    ("查看高等数学试卷", "高等数学", "midterm", None, 14),
    ("给我期中卷", "Python", "midterm", "midterm", 10),
    ("给我期中卷", "Python", None, "midterm", 10),
    ("给我期末卷", "Python", "midterm", "final", 10),
    ("高数期中期末都要", "高等数学", "final", None, 14),
    ("找全部期末试卷", "Python", "final", "final", 10),
    ("不要只查期末，给我全部类型", "Python", "final", None, 20),
    ("查看转专业试卷", "转专业", "other", None, 10),
    ("查看转专业其他类型试卷", "转专业", "other", "other", 10),
])
async def test_paper_scope_follows_current_request_not_model_default(
    tmp_path: Path, text: str, course: str, planned: str | None, expected: str | None, total: int,
) -> None:
    """模拟模型擅加期末或传错类型，检查真实查库、回复、详情与回放。"""
    svc, repo, scheduler, _, _ = setup_service(tmp_path)
    conversation = repo.create_conversation()
    repo.upsert_context(conversation.id, confirmed_course=course)
    scheduler.push(ScheduleDecision(
        actions=[ActionPlan(action="query_papers", parameters={"exam_type": planned})],
        assistant_reply="下面只给您期末试卷。",
    ))
    data = TurnInput(
        request_id=str(uuid4()), kind=None, dispatch=True, text=text, attachments=(),
        student_answer_text=None, solution_text=None, source=Source(), problem_id=None, start_new=False,
    )
    events = [e async for e in svc.stream_prepared(svc.prepare_turn(conversation.id, data), make_dummy_request())]
    result = next(e for e in events if e.type == "result")
    assert result.result.filters.exam_type == expected
    assert result.result.total == total
    assert result.assistant_message == result.result.message
    assert result.composite_results[0].details["exam_type"] == expected
    if expected is not None:
        assert {item.exam_type for item in result.result.items} == {expected}
    elif course in ("高等数学", "Python"):
        assert {item.exam_type for item in result.result.items} == {"midterm", "final"}
    saved = svc.get_detail(conversation.id).turns[-1]
    assert saved.result.filters.exam_type == expected
    assert saved.assistant_message == result.assistant_message
    replay = [e async for e in svc.stream_prepared(svc.prepare_turn(conversation.id, data), make_dummy_request())][-1]
    assert replay.result.total == total
    # 一次规划 + 一次“模型收尾”的决策（原生工具循环会回到模型）。
    assert len(scheduler.calls) == 2

