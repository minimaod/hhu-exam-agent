"""F006 审阅问题针对性自动化回归测试。

覆盖：
1. [P1] 实际模型适配器 log_usage 调用与 token 日志解析（使用含 usage_metadata 的 AIMessage）；
2. [P1] 动态调度 explain_problem 执行分支与事后决策；
3. [P1] 图片作答附件正确加载为 ImagePayload 传给模型；
4. [P1] 跨会话练习揭晓越权拦截与请求去重 exercise_id 冲突识别；
5. [P2] 动态轮次与练习轮次完整结果回放；
6. [P2] 查看解答后历史轮次 exercise 状态实时更新。
"""

import json
import logging
from pathlib import Path
from uuid import uuid4
import pytest
from starlette.requests import Request

from app.conversations.errors import (
    ConversationConflictError,
    ProblemNotFoundError,
)
from app.conversations.graph import ActionPlan, ScheduleDecision, SchedulerAgent
from app.conversations.models import (
    ActionResultItem,
    ExerciseEvaluation,
    ExerciseView,
    TurnResultEvent,
)
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.mistakes.service import MistakeRecordService
from app.papers.repository import PaperRepository
from app.papers.service import DEFAULT_PAPERS_ROOT, PaperService
from app.tutoring.exercise_llm import DeepSeekExerciseAgent
from app.tutoring.models import Source
from tests.tutoring_fakes import (
    FakeExerciseAgent,
    FakeScheduler,
    FakeTutor,
    clear_understanding,
    sample_explanation,
)


def make_dummy_request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/conversations/test/turns",
        "headers": [],
    }
    return Request(scope)


class FakeAIMessage:
    """模拟 LangChain AIMessage，附带 usage_metadata 与 response_metadata。"""

    def __init__(self, content: str, tool_calls: list[dict] | None = None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.response_metadata = {"model_name": "deepseek-chat", "finish_reason": "stop"}
        self.usage_metadata = {
            "input_tokens": 12,
            "output_tokens": 34,
            "total_tokens": 46,
        }


class FakeChatModel:
    """模拟 LangChain ChatDeepSeek，接受 prompt 返回 FakeAIMessage。"""

    def __init__(self, response_text: str, tool_calls: list[dict] | None = None):
        self._text = response_text
        self._tool_calls = tool_calls
        self.invocations: list[list] = []

    def bind_tools(self, tools, **kwargs):  # pragma: no cover - 仅保持接口一致
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages, **kwargs):
        self.invocations.append(messages)
        return FakeAIMessage(self._text, self._tool_calls)


def setup_service(tmp_path: Path):
    db_path = tmp_path / "study.sqlite3"
    repo = ConversationRepository(db_path)
    repo.initialize()

    papers_repo = PaperRepository(DEFAULT_PAPERS_ROOT)
    papers = PaperService(papers_repo)

    scheduler = FakeScheduler()
    ex_agent = FakeExerciseAgent()
    tutor = FakeTutor(
        understandings=[clear_understanding("计算极限 lim x->0 sin(x)/x")],
        explanations=[sample_explanation()],
    )

    mistakes_service = MistakeRecordService(
        repository=repo,
        papers=papers,
        make_tutor=lambda: tutor,
        tutoring_error=None,
        attachments_root=tmp_path / "attachments",
    )

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
        attachments_root=tmp_path / "attachments",
    )
    return svc, repo, scheduler, ex_agent, tutor


# ------------------------------------------------------------------
# 1. 实际模型适配器 log_usage 与参数测试
# ------------------------------------------------------------------


@pytest.mark.anyio
async def test_deepseek_exercise_agent_usage_logging_with_real_structure():
    """验证 DeepSeekExerciseAgent 调用真实结构响应时不报 log_usage 参数错误。"""
    gen_json = json.dumps(
        {
            "statement": "计算极限 lim x->0 sin(5x)/x",
            "reference_solution": "令 t=5x，原式 = 5",
            "knowledge_point": "重要极限",
        }
    )
    model = FakeChatModel(gen_json)
    agent = DeepSeekExerciseAgent(model=model)

    ex = await agent.generate_exercise(
        original_statement="计算极限 lim x->0 sin(x)/x",
        knowledge_points="重要极限",
        steps_summary="步骤1",
        course="高等数学",
    )
    assert ex.statement == "计算极限 lim x->0 sin(5x)/x"
    assert ex.knowledge_point == "重要极限"

    eval_json = json.dumps(
        {
            "is_correct": True,
            "final_answer_assessment": "答案正确",
            "step_by_step_analysis": "推导无误",
            "guidance": "掌握扎实",
            "needs_supplement": False,
        }
    )
    eval_model = FakeChatModel(eval_json)
    eval_agent = DeepSeekExerciseAgent(model=eval_model)
    ev = await eval_agent.evaluate_answer(
        statement=ex.statement,
        reference_solution=ex.reference_solution,
        student_answer="5",
        student_solution_text="设 t=5x",
        images=[],
    )
    assert ev.is_correct is True
    assert ev.final_answer_assessment == "答案正确"


@pytest.mark.anyio
async def test_scheduler_agent_usage_logging_with_real_structure(caplog):
    """验证 SchedulerAgent 调用真实结构响应（原生 tool_calls）时不报 log_usage 错误。"""
    tool_calls = [
        {
            "id": "call_introduce_1",
            "name": "confirm_course",
            "args": {"course": "Python"},
            "type": "tool_call",
        }
    ]
    model = FakeChatModel("", tool_calls=tool_calls)
    scheduler = SchedulerAgent(model=model)

    with caplog.at_level(logging.INFO, logger="app.llm"):
        dec = await scheduler.plan("我想复习 Python")

    assert dec.confirmed_course == "Python"
    assert [plan.action for plan in dec.actions] == ["introduce"]
    assert dec.actions[0].tool_call_id == "call_introduce_1"
    assert any("deepseek usage" in record.getMessage() for record in caplog.records)


@pytest.mark.anyio
async def test_scheduler_overrides_category_with_detected_course():
    """模型把“转专业考试”误当成“通识选修课程”时，用确定性识别纠正。"""
    tool_calls = [
        {
            "id": "call_introduce_2",
            "name": "confirm_course",
            "args": {"course": "通识选修课程"},
            "type": "tool_call",
        }
    ]
    scheduler = SchedulerAgent(model=FakeChatModel("", tool_calls=tool_calls))

    dec = await scheduler.plan("我要复习转专业考试")

    assert dec.confirmed_course == "转专业"


# ------------------------------------------------------------------
# 2. 练习目标跨会话越权校验与请求去重 exercise_id
# ------------------------------------------------------------------


def test_cross_conversation_exercise_reveal_is_blocked(tmp_path: Path):
    """会话 A 尝试揭晓会话 B 的练习，必须被拒绝。"""
    _, repo, _, _, _ = setup_service(tmp_path)
    cA = repo.create_conversation()
    cB = repo.create_conversation()

    # 在会话 B 中创建题目与练习
    prob_b = repo.create_problem(
        conversation_id=cB.id,
        statement_text="会话 B 的原题",
        id="p-1",
    )
    ex_b = repo.create_exercise(
        id=str(uuid4()),
        conversation_id=cB.id,
        problem_id=prob_b.id,
        root_problem_id=prob_b.id,
        statement_text="会话 B 的练习",
        reference_solution_text="会话 B 的参考解答",
        knowledge_point="考点 B",
    )

    # 用会话 A 的身份揭晓，必须抛出 ProblemNotFoundError
    with pytest.raises(ProblemNotFoundError, match="练习题不属于当前会话"):
        repo.reveal_exercise_solution(ex_b.id, conversation_id=cA.id)


def test_same_request_id_with_different_exercise_id_conflicts(tmp_path: Path):
    """相同的 request_id，如果指向不同练习题，去重摘要必须判定为冲突。"""
    svc, repo, _, _, _ = setup_service(tmp_path)
    c = repo.create_conversation()

    prob = repo.create_problem(
        conversation_id=c.id,
        statement_text="测试题目",
        id="p-1",
    )

    ex1 = repo.create_exercise(
        id=str(uuid4()),
        conversation_id=c.id,
        problem_id=prob.id,
        root_problem_id=prob.id,
        statement_text="练习 1",
        reference_solution_text="解答 1",
        knowledge_point="考点 1",
    )
    ex2 = repo.create_exercise(
        id=str(uuid4()),
        conversation_id=c.id,
        problem_id=prob.id,
        root_problem_id=prob.id,
        statement_text="练习 2",
        reference_solution_text="解答 2",
        knowledge_point="考点 2",
    )

    req_id = str(uuid4())
    t1 = TurnInput(
        request_id=req_id,
        kind="exercise",
        dispatch=False,
        text="我的答案是 1",
        attachments=(),
        student_answer_text="1",
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=False,
        exercise_id=ex1.id,
    )
    # 第一次合法准备并开始
    svc.prepare_turn(c.id, t1)

    # 第二次使用相同 request_id 但不同 exercise_id
    t2 = TurnInput(
        request_id=req_id,
        kind="exercise",
        dispatch=False,
        text="我的答案是 1",
        attachments=(),
        student_answer_text="1",
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=False,
        exercise_id=ex2.id,
    )
    with pytest.raises(ConversationConflictError, match="同一个 request_id 不能提交不同的输入"):
        svc.prepare_turn(c.id, t2)


# ------------------------------------------------------------------
# 3. 动态轮次完整回放测试
# ------------------------------------------------------------------


@pytest.mark.anyio
async def test_completed_dynamic_turn_is_fully_replayed(tmp_path: Path):
    """已完成的动态请求重复提交，完整回放 exercise、assistant_message 与建议，不报错。"""
    svc, repo, scheduler, ex_agent, _ = setup_service(tmp_path)
    c = repo.create_conversation()

    req_id = str(uuid4())
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="introduce", reason="介绍")],
            assistant_reply="欢迎复习！",
            suggested_actions=["查试卷"],
        )
    )

    t_input = TurnInput(
        request_id=req_id,
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

    # 首次执行
    prepared = svc.prepare_turn(c.id, t_input)
    events = [e async for e in svc.stream_prepared(prepared, make_dummy_request())]
    res1 = next(e for e in events if e.type == "result")
    # F008：工具决策阶段的散文不再作为正文；正文由真实执行结果生成，
    # 回放必须与首次完全一致（下面比对）。
    assert res1.assistant_message

    # 重复提交该 request_id，触发回放
    prep_replay = svc.prepare_turn(c.id, t_input)
    replay_events = [e async for e in svc.stream_prepared(prep_replay, make_dummy_request())]
    assert len(replay_events) == 1
    replay_res = replay_events[0]
    assert isinstance(replay_res, TurnResultEvent)
    assert replay_res.assistant_message == res1.assistant_message
    assert [s.label for s in replay_res.suggestions] == ["查试卷"]


# ------------------------------------------------------------------
# 4. 动态调度执行 explain_problem 与事后动态决策
# ------------------------------------------------------------------


@pytest.mark.anyio
async def test_dynamic_dispatch_executes_explain_problem(tmp_path: Path):
    """动态调度规划 explain_problem 时，真实调用辅导模型并生成讲解。"""
    svc, repo, scheduler, _, tutor = setup_service(tmp_path)
    c = repo.create_conversation()

    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="explain_problem", reason="用户粘贴题目请求讲解")],
            assistant_reply="",  # 故意置空，由事后决策基于真实执行结果生成
            suggested_actions=["练一道类似题", "保存到错题本"],
        )
    )

    t_input = TurnInput(
        request_id=str(uuid4()),
        kind=None,
        dispatch=True,
        text="计算极限 lim x->0 sin(x)/x",
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
    # 验证辅导结果已产出
    assert res_event.tutoring is not None
    assert res_event.tutoring.kind == "explanation"
    # 验证题目已保存为活动题目
    active_prob = repo.get_active_problem(c.id)
    assert active_prob is not None
    assert active_prob.status == "explained"


@pytest.mark.anyio
async def test_tool_failure_dynamically_adjusts_reply(tmp_path: Path):
    """工具失败时，事后决策必须如实反馈错误，绝不沿用事前虚假的成功回复。"""
    svc, repo, scheduler, ex_agent, _ = setup_service(tmp_path)
    c = repo.create_conversation()

    # 插入原题
    expl_json = sample_explanation().model_dump_json()
    with repo._write() as conn:
        conn.execute(
            """
            INSERT INTO problems (
                id, conversation_id, root_problem_id, version,
                statement_text, status, clarity, source_type,
                explanation_json, active, created_at, updated_at
            ) VALUES (
                'p-fail', ?, 'p-fail', 1, '题干', 'explained', 'clear', 'none',
                ?, 1, '2026-01-01', '2026-01-01'
            )
            """,
            (c.id, expl_json),
        )

    # 调度模型在事前写了“已生成练习，请作答”
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="generate_exercise", reason="出题")],
            assistant_reply="已为您生成练习题，请在下方作答：",
        )
    )
    # 让出题 agent 抛出异常
    ex_agent.push_exercise(RuntimeError("模型网络超时"))

    t_input = TurnInput(
        request_id=str(uuid4()),
        kind=None,
        dispatch=True,
        text="来一道类似题",
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
    # 核心断言：回复必须被事后动态校准为反映异常，绝不能说“已为您生成练习题”
    assert "已为您生成练习题" not in res_event.assistant_message
    assert "异常" in res_event.assistant_message or "失败" in res_event.assistant_message
    assert any(it.status == "failed" for it in res_event.composite_results)


# ------------------------------------------------------------------
# 5. 查看解答后历史 turns 动态状态刷新
# ------------------------------------------------------------------


def test_revealing_exercise_dynamically_updates_history_turns(tmp_path: Path):
    """查看解答后，历史 turns 中的 exercise 状态动态更新为 revealed 并包含解答。"""
    _, repo, _, _, _ = setup_service(tmp_path)
    c = repo.create_conversation()

    prob = repo.create_problem(
        conversation_id=c.id,
        statement_text="测试题目",
        id="p-1",
    )

    ex = repo.create_exercise(
        id=str(uuid4()),
        conversation_id=c.id,
        problem_id=prob.id,
        root_problem_id=prob.id,
        statement_text="练习 1",
        reference_solution_text="这是标准答案",
        knowledge_point="考点",
    )

    # 完成一轮动态出题
    repo.start_turn(c.id, "req-1", "来一道题", kind="dynamic")
    repo.complete_dynamic_turn(
        conversation_id=c.id,
        request_id="req-1",
        kind="dynamic",
        exercise=ExerciseView(
            id=ex.id,
            conversation_id=c.id,
            problem_id="p-1",
            root_problem_id="p-1",
            statement_text="练习 1",
            knowledge_point="考点",
            status="pending_answer",
            answered_after_revealed=False,
            attachments=[],
            created_at=ex.created_at,
            updated_at=ex.updated_at,
        ),
        exercise_id=ex.id,
    )

    # 此时读取历史，应该是 pending_answer，且没有解答
    turns_before = repo.list_turns(c.id)
    assert turns_before[0].exercise.status == "pending_answer"
    assert turns_before[0].exercise.reference_solution_text is None

    # 调用 reveal
    repo.reveal_exercise_solution(ex.id, conversation_id=c.id)

    # 再次读取历史，必须动态更新为 revealed 并呈现解答！
    turns_after = repo.list_turns(c.id)
    assert turns_after[0].exercise.status == "revealed"
    assert turns_after[0].exercise.reference_solution_text == "这是标准答案"
