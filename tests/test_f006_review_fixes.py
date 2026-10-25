"""F006 复核问题的针对性回归测试。

覆盖：
1. 作答轮次的 SSE / 历史 / 回放 / 前端解析契约；
2. 动态追问的关键字参数、历史追问传入与保存；
3. 动态纠正生成新版本，旧练习来源不变；
4. 前置动作失败后不再执行依赖动作，并重新决策；
5. 首次不清晰和补充不清晰的澄清链路；
6. 先保存作答再评价、失败保留作答与重试；
7. 显式无效/跨会话 exercise_id 被拒绝且不操作当前练习。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.conversations import (
    ConversationServiceState,
    get_conversation_service_state,
)
from app.conversations.errors import (
    ConversationConflictError,
    ProblemNotFoundError,
)
from app.conversations.graph import (
    ActionOutcome,
    ActionPlan,
    DynamicReviewGraph,
    ExecutionResult,
    ScheduleDecision,
    SchedulerAgent,
)
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.main import app
from app.mistakes.service import MistakeRecordService
from app.papers.repository import PaperRepository
from app.papers.service import DEFAULT_PAPERS_ROOT, PaperService
from app.querying.errors import QueryError
from app.tutoring.exercise_llm import AnswerEvaluation, GeneratedExercise
from app.tutoring.models import FollowupAnswer, FollowupResult, Source
from tests.fakes import parse_sse
from tests.tutoring_fakes import (
    FakeExerciseAgent,
    FakeScheduler,
    FakeTutor,
    clear_understanding,
    png_bytes,
    sample_explanation,
    unclear_understanding,
)


def make_dummy_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/conversations/test/turns",
            "headers": [],
        }
    )


class RecordingExerciseAgent(FakeExerciseAgent):
    """在评价模型被调用时记录数据库里的学生作答，验证“先保存再评价”。"""

    def __init__(self, repository: ConversationRepository, exercise_id: str, **kwargs):
        super().__init__(**kwargs)
        self._repository = repository
        self._exercise_id = exercise_id
        self.observed_answers: list[str | None] = []

    async def evaluate_answer(self, **kwargs) -> AnswerEvaluation:
        exercise = self._repository.get_exercise(self._exercise_id)
        self.observed_answers.append(
            exercise.student_answer_text if exercise is not None else None
        )
        return await super().evaluate_answer(**kwargs)


def setup_service(
    tmp_path: Path,
    *,
    scheduler_decisions=(),
    understandings=(),
    explanations=(),
    followups=(),
    exercises=(),
    evaluations=(),
):
    repository = ConversationRepository(tmp_path / "study.sqlite3")
    repository.initialize()
    papers = PaperService(PaperRepository(DEFAULT_PAPERS_ROOT))
    tutor = FakeTutor(
        understandings=understandings,
        explanations=explanations,
        followups=followups,
    )
    exercise_agent = FakeExerciseAgent(
        exercises=exercises, evaluations=evaluations
    )
    scheduler = FakeScheduler(decisions=scheduler_decisions)
    mistakes = MistakeRecordService(
        repository=repository,
        papers=papers,
        make_tutor=lambda: tutor,
        tutoring_error=None,
        attachments_root=tmp_path / "attachments",
    )
    service = ConversationService(
        repository=repository,
        make_query=None,
        query_error=None,
        make_tutor=lambda: tutor,
        make_router=None,
        make_exercise_agent=lambda: exercise_agent,
        make_scheduler=lambda: scheduler,
        papers=papers,
        records=mistakes,
        attachments_root=tmp_path / "attachments",
    )
    return service, repository, scheduler, exercise_agent, tutor


def dispatch_input(
    request_id: str,
    text: str,
    *,
    attachments=(),
    start_new: bool = False,
) -> TurnInput:
    return TurnInput(
        request_id=request_id,
        kind=None,
        dispatch=True,
        text=text,
        attachments=attachments,
        student_answer_text=None,
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=start_new,
    )


def answered_input(
    request_id: str,
    text: str,
    *,
    exercise_id: str | None,
    student_answer: str | None = None,
    solution_text: str | None = None,
) -> TurnInput:
    return TurnInput(
        request_id=request_id,
        kind="exercise",
        text=text,
        attachments=(),
        student_answer_text=student_answer,
        solution_text=solution_text,
        source=Source(),
        problem_id=None,
        start_new=False,
        exercise_id=exercise_id,
    )


def create_problem_with_exercise(
    repository: ConversationRepository,
    *,
    conversation_id: str,
    statement: str = "计算极限 lim x->0 sin(x)/x",
    exercise_statement: str = "计算极限 lim x->0 tan(x)/x",
    reference_solution: str = "利用 tan x ~ x，答案为 1。",
):
    problem = repository.create_problem(
        conversation_id=conversation_id,
        statement_text=statement,
        explanation=sample_explanation(),
    )
    exercise = repository.create_exercise(
        id=str(uuid4()),
        conversation_id=conversation_id,
        problem_id=problem.id,
        root_problem_id=problem.root_problem_id,
        statement_text=exercise_statement,
        reference_solution_text=reference_solution,
        knowledge_point="等价无穷小",
    )
    return problem, exercise


def decision(
    *actions: ActionPlan,
    reply: str | None = None,
    course: str | None = None,
) -> ScheduleDecision:
    return ScheduleDecision(
        actions=list(actions),
        assistant_reply=reply,
        confirmed_course=course,
    )


async def collect(service: ConversationService, prepared) -> list:
    return [
        event
        async for event in service.stream_prepared(prepared, make_dummy_request())
    ]


def install_http(
    service: ConversationService,
    repository: ConversationRepository | None = None,
) -> TestClient:
    app.dependency_overrides[get_conversation_service_state] = lambda: (
        ConversationServiceState(service=service, error=None)
    )
    if repository is not None:
        from app.conversations.service import ConversationStoreState

        app.state.conversation_store = ConversationStoreState(
            repository=repository, error=None
        )
    return TestClient(app)


def remove_http_override() -> None:
    app.dependency_overrides.pop(get_conversation_service_state, None)


# --------------------------------------------------------------------------
# 1. 契约：SSE 进度/结果、历史读取、重复请求回放、真实前端解析器
# --------------------------------------------------------------------------


def test_exercise_answer_contract_history_and_replay(tmp_path: Path) -> None:
    service, repository, _, exercise_agent, _ = setup_service(tmp_path)
    conversation = repository.create_conversation()
    problem, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    request_id = str(uuid4())

    client = install_http(service)
    try:
        response = client.post(
            f"/api/conversations/{conversation.id}/turns",
            json={
                "request_id": request_id,
                "query": "1",
                "kind": "exercise",
                "exercise_id": exercise.id,
                "student_answer": "1",
            },
        )
        assert response.status_code == 200, response.text
        events = parse_sse(response.text)
        progress = [item for item in events if item["type"] == "progress"]
        results = [item for item in events if item["type"] == "result"]
        assert progress and progress[-1]["stage"] == "evaluating_exercise"
        assert results and results[-1]["kind"] == "exercise"
        assert results[-1]["exercise"]["student_answer_text"] == "1"
        assert results[-1]["evaluation"]["is_correct"] is True

        # 历史读取：轮次类型、作答与评价均已保存。
        detail = client.get(f"/api/conversations/{conversation.id}").json()
        assert detail["turns"][0]["kind"] == "exercise"
        assert detail["turns"][0]["exercise"]["student_answer_text"] == "1"
        assert detail["turns"][0]["evaluation"]["is_correct"] is True

        # 重复 request_id 直接回放，不再次调用评价模型。
        replay = client.post(
            f"/api/conversations/{conversation.id}/turns",
            json={
                "request_id": request_id,
                "query": "1",
                "kind": "exercise",
                "exercise_id": exercise.id,
                "student_answer": "1",
            },
        )
        assert replay.status_code == 200, replay.text
        replay_events = parse_sse(replay.text)
        assert replay_events[-1]["kind"] == "exercise"
        assert len(exercise_agent.evaluate_calls) == 1

        # 旧库中的 exercise_answer 轮次读取时归一为 exercise。
        with repository._write() as connection:
            connection.execute(
                "UPDATE conversation_turns SET kind = 'exercise_answer' "
                "WHERE conversation_id = ?",
                (conversation.id,),
            )
        legacy_detail = client.get(f"/api/conversations/{conversation.id}").json()
        assert legacy_detail["turns"][0]["kind"] == "exercise"

        # 将真实后端产生的 SSE 事件交给前端实际解析器。
        _assert_frontend_parses(events, conversation.id, request_id)
    finally:
        remove_http_override()


def _assert_frontend_parses(events: list[dict], conversation_id: str, request_id: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("本机没有 Node.js，跳过前端解析器交叉验证。")

    module_url = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "src"
        / "lib"
        / "conversationsApi.ts"
    ).as_uri()
    payloads = [
        {
            "payload": event,
            "conversation_id": conversation_id,
            "request_id": request_id,
        }
        for event in events
        if event.get("type") in ("progress", "result")
    ]
    env = dict(os.environ)
    env["F006_FIXTURE"] = json.dumps(payloads, ensure_ascii=False)
    env["F006_MODULE_URL"] = module_url
    script = """
import { parseTurnEvent } from process.env.F006_MODULE_URL;
"""
    # 动态 import 才能直接读取环境变量中的 file URL。
    script = """
const mod = await import(process.env.F006_MODULE_URL);
const input = JSON.parse(process.env.F006_FIXTURE);
const output = input.map((item) => {
  const event = mod.parseTurnEvent(
    JSON.stringify(item.payload),
    item.conversation_id,
    item.request_id,
  );
  return { type: event.type, kind: event.kind ?? null, stage: event.stage ?? null };
});
console.log(JSON.stringify(output));
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    parsed = json.loads(completed.stdout)
    assert any(item["stage"] == "evaluating_exercise" for item in parsed)
    assert any(item["kind"] == "exercise" for item in parsed)


# --------------------------------------------------------------------------
# 2. 动态追问
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dynamic_followup_uses_keywords_and_saves_history(tmp_path: Path) -> None:
    service, repository, scheduler, _, tutor = setup_service(
        tmp_path,
        scheduler_decisions=[
            decision(ActionPlan(action="followup_problem", reason="追问")),
        ],
        followups=[
            FollowupAnswer(answer="因为要用等价无穷小替换。", related_steps=[1])
        ],
    )
    conversation = repository.create_conversation()
    problem = repository.create_problem(
        conversation_id=conversation.id,
        statement_text="计算极限 lim x->0 sin(x)/x",
        explanation=sample_explanation(),
    )
    earlier = FollowupResult(
        problem_id=problem.id,
        answer="第一步是识别 0/0 型。",
        related_steps=[1],
    )
    earlier_request = str(uuid4())
    repository.start_turn(
        conversation.id, earlier_request, "第一步为什么这样做？", kind="followup"
    )
    repository.save_followup_turn(
        problem=problem,
        request_id=earlier_request,
        question="第一步为什么这样做？",
        answer=earlier.answer,
        output_json=json.dumps(
            earlier.model_dump(mode="json"), ensure_ascii=False
        ),
    )

    prepared = service.prepare_turn(
        conversation.id,
        dispatch_input(str(uuid4()), "第二步为什么能替换？"),
    )
    events = await collect(service, prepared)
    result = next(event for event in events if event.type == "result")

    assert result.tutoring is not None
    assert result.tutoring.kind == "followup_answer"
    assert result.tutoring.answer == "因为要用等价无穷小替换。"
    assert len(tutor.followup_calls) == 1
    call = tutor.followup_calls[0]
    assert set(call) == {
        "statement",
        "explanation",
        "recent_followups",
        "question",
    }
    assert call["statement"] == problem.statement_text
    assert call["question"] == "第二步为什么能替换？"
    assert call["recent_followups"][0][0] == "第一步为什么这样做？"

    history = repository.get_problem_followups(problem.id)
    assert [item["question"] for item in history] == [
        "第一步为什么这样做？",
        "第二步为什么能替换？",
    ]
    turns = repository.list_turns(conversation.id)
    assert turns[-1].kind == "dynamic"
    assert turns[-1].tutoring is not None
    assert turns[-1].tutoring.kind == "followup_answer"


# --------------------------------------------------------------------------
# 3. 纠正版本与旧练习来源
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dynamic_correction_creates_version_and_keeps_old_exercise(
    tmp_path: Path,
) -> None:
    service, repository, scheduler, _, _ = setup_service(
        tmp_path,
        scheduler_decisions=[
            decision(
                ActionPlan(
                    action="explain_problem",
                    parameters={"mode": "correction"},
                    reason="纠正题干",
                )
            )
        ],
        understandings=[clear_understanding("计算极限 lim x->0 sin(2x)/x")],
        explanations=[sample_explanation(final_answer="2")],
    )
    conversation = repository.create_conversation()
    old_problem, old_exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )

    prepared = service.prepare_turn(
        conversation.id,
        dispatch_input(str(uuid4()), "纠正：极限应为 sin(2x)/x"),
    )
    events = await collect(service, prepared)
    result = next(event for event in events if event.type == "result")

    assert result.tutoring is not None
    assert result.tutoring.kind == "explanation"
    corrected = result.tutoring.problem
    assert corrected.id != old_problem.id
    assert corrected.version == 2
    assert corrected.root_problem_id == old_problem.root_problem_id
    assert corrected.statement_text == "计算极限 lim x->0 sin(2x)/x"

    # 旧版本内容、讲解和练习来源都没有被覆盖。
    stored_old = repository.get_problem(old_problem.id)
    assert stored_old is not None
    assert stored_old.statement_text == old_problem.statement_text
    assert stored_old.explanation is not None
    assert stored_old.explanation.final_answer == "1"
    old_exercise_after = repository.get_exercise(old_exercise.id)
    assert old_exercise_after is not None
    assert old_exercise_after.problem_id == old_problem.id
    assert old_exercise_after.statement_text == old_exercise.statement_text
    assert old_exercise_after.reference_solution_text == old_exercise.reference_solution_text

    active = repository.get_active_problem(conversation.id)
    assert active is not None and active.id == corrected.id
    # 旧练习仍可按 ID 读取，但题目换版后不再作为“当前练习”指向。
    assert repository.get_active_exercise(conversation.id) is None
    assert repository.get_exercise(old_exercise.id) is not None


# --------------------------------------------------------------------------
# 4. 失败后重新决策，依赖动作不执行
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_failed_correction_skips_dependent_exercise(tmp_path: Path) -> None:
    service, repository, scheduler, exercise_agent, tutor = setup_service(
        tmp_path,
        scheduler_decisions=[
            decision(
                ActionPlan(action="explain_problem", parameters={"mode": "correction"}),
                ActionPlan(action="generate_exercise"),
            ),
            decision(
                ActionPlan(action="clarify_or_reply"),
                reply="纠正没有成功，请补充正确题干后重试。",
            ),
        ],
        understandings=[clear_understanding("纠正后的题干")],
    )
    tutor.push_explanation(QueryError("MODEL_TIMEOUT", "训练模型超时"))
    conversation = repository.create_conversation()
    repository.create_problem(
        conversation_id=conversation.id,
        statement_text="原始题干",
        explanation=sample_explanation(),
    )

    prepared = service.prepare_turn(
        conversation.id,
        dispatch_input(str(uuid4()), "纠正题干，再出一道类似题"),
    )
    events = await collect(service, prepared)
    result = next(event for event in events if event.type == "result")

    # 调度器被再次调用，而不是机械执行原计划。
    assert len(scheduler.calls) == 2
    assert exercise_agent.generate_calls == []
    assert result.exercise is None
    actions = {item.action: item for item in result.composite_results}
    assert actions["explain_problem"].status == "failed"
    assert actions["generate_exercise"].status == "failed"
    assert actions["generate_exercise"].details.get("skipped") is True
    assert "未成功" in result.assistant_message


@pytest.mark.anyio
async def test_execution_budget_marks_remaining_actions_skipped() -> None:
    class BudgetScheduler:
        async def plan(self, *args, **kwargs) -> ScheduleDecision:
            return ScheduleDecision(
                actions=[
                    ActionPlan(action="explain_problem"),
                    ActionPlan(action="generate_exercise"),
                    ActionPlan(action="record_mistake"),
                ]
            )

    async def execute(action, state) -> ExecutionResult:
        return ExecutionResult(
            ActionOutcome(action.action, "success", f"done {action.action}"), {}
        )

    graph = DynamicReviewGraph(
        BudgetScheduler(), execute, max_rounds=1, max_actions=2
    )
    result = await graph.run(
        {
            "query": "讲解后出题并保存",
            "confirmed_course": None,
            "active_problem_text": None,
            "active_problem_status": None,
            "active_exercise_statement": None,
            "active_exercise_status": None,
            "last_suggestions": [],
            "recent_turns_summary": "",
            "has_attachments": False,
            "rounds": 0,
            "executed_count": 0,
            "pending": [],
            "observations": [],
            "assistant_reply": None,
            "suggested_actions": [],
            "replan_error": None,
        }
    )
    observations = result["observations"]
    assert [item["status"] for item in observations] == [
        "success",
        "success",
        "failed",
    ]
    assert observations[-1]["details"]["skipped"] is True
    assert observations[-1]["action"] == "record_mistake"


# --------------------------------------------------------------------------
# 5. 澄清与补充
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unclear_problem_is_saved_then_supplemented(tmp_path: Path) -> None:
    service, repository, _, _, tutor = setup_service(
        tmp_path,
        scheduler_decisions=[
            decision(
                ActionPlan(
                    action="explain_problem", parameters={"mode": "new"}
                )
            ),
            decision(ActionPlan(action="explain_problem")),
        ],
        understandings=[
            unclear_understanding("计算积分 ∫_0^1 f(x) dx"),
            clear_understanding("计算积分 ∫_0^1 x^2 dx"),
        ],
        explanations=[sample_explanation(final_answer="1/3")],
    )
    conversation = repository.create_conversation()

    first = await collect(
        service,
        service.prepare_turn(
            conversation.id,
            dispatch_input(str(uuid4()), "这道积分题怎么做"),
        ),
    )
    first_result = next(event for event in first if event.type == "result")
    assert first_result.tutoring is not None
    assert first_result.tutoring.kind == "needs_clarification"
    pending = first_result.tutoring.problem
    assert pending.status == "awaiting_supplement"
    assert pending.version == 1
    assert first_result.tutoring.questions

    second = await collect(
        service,
        service.prepare_turn(
            conversation.id,
            dispatch_input(str(uuid4()), "被积函数是 x 的平方"),
        ),
    )
    second_result = next(event for event in second if event.type == "result")
    assert second_result.tutoring is not None
    assert second_result.tutoring.kind == "explanation"
    explained = second_result.tutoring.problem
    assert explained.id == pending.id
    assert explained.version == 1
    assert explained.status == "explained"
    assert explained.statement_text == "计算积分 ∫_0^1 x^2 dx"

    # 第二次理解同时收到原题干和补充文字，接续同一个目标。
    second_prompt = tutor.understand_calls[1][0]
    assert "计算积分" in second_prompt
    assert "被积函数是 x 的平方" in second_prompt


@pytest.mark.anyio
async def test_unclear_image_supplement_continues_same_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已有未清晰题目时，图片补充交给动态调度并保留原附件。"""
    from app import images

    monkeypatch.setattr(images, "ATTACHMENTS_ROOT", tmp_path / "attachments")
    service, repository, scheduler, _, tutor = setup_service(
        tmp_path,
        understandings=[
            unclear_understanding("计算积分 ∫_0^1 f(x) dx"),
            clear_understanding("计算积分 ∫_0^1 x^2 dx"),
        ],
        explanations=[sample_explanation(final_answer="1/3")],
    )
    tutor.push_explanation(sample_explanation(final_answer="1/3"))
    conversation = repository.create_conversation()
    client = install_http(service, repository)
    try:
        first_upload = client.post(
            f"/api/conversations/{conversation.id}/attachments",
            files={"file": ("problem.png", png_bytes(), "image/png")},
        )
        assert first_upload.status_code == 201, first_upload.text
        first_attachment = first_upload.json()
        first = client.post(
            f"/api/conversations/{conversation.id}/turns",
            json={
                "request_id": str(uuid4()),
                "query": "这题看不清，怎么做",
                "attachments": [
                    {"id": first_attachment["id"], "role": "statement"}
                ],
            },
        )
        assert first.status_code == 200, first.text
        first_events = parse_sse(first.text)
        pending = first_events[-1]["tutoring"]["problem"]
        assert pending["status"] == "awaiting_supplement"

        second_upload = client.post(
            f"/api/conversations/{conversation.id}/attachments",
            files={"file": ("more.png", png_bytes(), "image/png")},
        )
        assert second_upload.status_code == 201, second_upload.text
        second_attachment = second_upload.json()

        scheduler.push(decision(ActionPlan(action="explain_problem")))
        second = client.post(
            f"/api/conversations/{conversation.id}/turns",
            json={
                "request_id": str(uuid4()),
                "query": "",
                "attachments": [
                    {"id": second_attachment["id"], "role": "supplement"}
                ],
            },
        )
        assert second.status_code == 200, second.text
        second_events = parse_sse(second.text)
        result = second_events[-1]
        assert result["kind"] == "dynamic"
        explained = result["tutoring"]["problem"]
        assert explained["id"] == pending["id"]
        assert explained["status"] == "explained"
        assert explained["statement_text"] == "计算积分 ∫_0^1 x^2 dx"
        attachment_ids = {item["id"] for item in explained["attachments"]}
        assert attachment_ids == {
            first_attachment["id"],
            second_attachment["id"],
        }
        # 第二次理解同时包含原题干和“只补充了图片”的说明。
        second_prompt = tutor.understand_calls[1][0]
        assert "计算积分" in second_prompt
        assert "只补充了图片" in second_prompt
    finally:
        remove_http_override()


# --------------------------------------------------------------------------
# 6. 先保存作答再评价；失败保留、重试、重复评价拒绝
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_answer_saved_before_evaluation_and_retry(tmp_path: Path) -> None:
    service, repository, _, _, _ = setup_service(tmp_path)
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    retry_agent = RecordingExerciseAgent(
        repository,
        exercise.id,
        evaluations=[RuntimeError("模型超时")],
    )
    # 第一次：评价失败，但作答必须已经保存。
    svc1 = ConversationService(
        repository=repository,
        make_query=None,
        query_error=None,
        make_tutor=None,
        make_exercise_agent=lambda: retry_agent,
        make_scheduler=None,
    )
    first_request = str(uuid4())
    first_events = await collect(
        svc1,
        svc1.prepare_turn(
            conversation.id,
            answered_input(
                first_request, "1", exercise_id=exercise.id, student_answer="1"
            ),
        ),
    )
    first_result = next(event for event in first_events if event.type == "result")
    assert first_result.kind == "exercise"
    assert first_result.exercise is not None
    assert first_result.exercise.student_answer_text == "1"
    assert first_result.evaluation is None
    assert "评价没有完成" in (first_result.assistant_message or "")
    assert retry_agent.observed_answers == ["1"]

    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert stored.student_answer_text == "1"
    assert stored.evaluation_json is None
    assert repository.list_turns(conversation.id)[0].kind == "exercise"

    # 重复同一个 request_id 回放，不再次评价。
    replay_events = await collect(
        svc1,
        svc1.prepare_turn(
            conversation.id,
            answered_input(
                first_request, "1", exercise_id=exercise.id, student_answer="1"
            ),
        ),
    )
    replay_result = next(event for event in replay_events if event.type == "result")
    assert replay_result.exercise is not None
    assert retry_agent.observed_answers == ["1"]
    assert len(retry_agent.evaluate_calls) == 1

    # 新请求重试：评价成功，原作答保留。
    success_agent = RecordingExerciseAgent(repository, exercise.id)
    svc2 = ConversationService(
        repository=repository,
        make_query=None,
        query_error=None,
        make_tutor=None,
        make_exercise_agent=lambda: success_agent,
        make_scheduler=None,
    )
    retry_events = await collect(
        svc2,
        svc2.prepare_turn(
            conversation.id,
            answered_input(
                str(uuid4()), "1", exercise_id=exercise.id, student_answer="1"
            ),
        ),
    )
    retry_result = next(event for event in retry_events if event.type == "result")
    assert retry_result.evaluation is not None
    assert success_agent.observed_answers == ["1"]
    stored_after = repository.get_exercise(exercise.id)
    assert stored_after is not None
    assert stored_after.status == "submitted"
    assert stored_after.student_answer_text == "1"

    # 已完成评价后原样重发：不新建尝试，也不覆盖已有评价。
    same_events = await collect(
        svc2,
        svc2.prepare_turn(
            conversation.id,
            answered_input(
                str(uuid4()), "1", exercise_id=exercise.id, student_answer="1"
            ),
        ),
    )
    same_error = next(event for event in same_events if event.type == "error")
    assert same_error.code == "BUSINESS_REJECTED"
    final = repository.get_exercise(exercise.id)
    assert final is not None
    assert final.student_answer_text == "1"
    assert len(final.attempts) == 1

    # 新内容进入订正：旧答案与评价保留，新尝试标记为查看解答后作答。
    correction_agent = RecordingExerciseAgent(repository, exercise.id)
    svc3 = ConversationService(
        repository=repository,
        make_query=None,
        query_error=None,
        make_tutor=None,
        make_exercise_agent=lambda: correction_agent,
        make_scheduler=None,
    )
    correction_events = await collect(
        svc3,
        svc3.prepare_turn(
            conversation.id,
            answered_input(
                str(uuid4()), "2", exercise_id=exercise.id, student_answer="2"
            ),
        ),
    )
    correction_result = next(
        event for event in correction_events if event.type == "result"
    )
    assert correction_result.business_status == "succeeded"
    corrected = repository.get_exercise(exercise.id)
    assert corrected is not None
    assert [item.kind for item in corrected.attempts] == ["initial", "correction"]
    assert corrected.attempts[0].student_answer_text == "1"
    assert corrected.attempts[0].evaluation_json is not None
    assert corrected.attempts[1].student_answer_text == "2"
    assert corrected.attempts[1].answered_after_revealed is True


@pytest.mark.anyio
async def test_missing_evaluation_model_still_keeps_answer(tmp_path: Path) -> None:
    service, repository, _, _, _ = setup_service(tmp_path)
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    no_model = ConversationService(
        repository=repository,
        make_query=None,
        query_error=None,
        make_tutor=None,
        make_exercise_agent=None,
    )
    events = await collect(
        no_model,
        no_model.prepare_turn(
            conversation.id,
            answered_input(
                str(uuid4()), "1", exercise_id=exercise.id, student_answer="1"
            ),
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.kind == "exercise"
    assert result.exercise is not None
    assert result.exercise.student_answer_text == "1"
    assert result.evaluation is None
    assert "尚未配置" in (result.assistant_message or "")
    stored = repository.get_exercise(exercise.id)
    assert stored is not None and stored.student_answer_text == "1"


@pytest.mark.anyio
async def test_dynamic_evaluate_saves_answer_before_model(tmp_path: Path) -> None:
    service_holder = setup_service(tmp_path)
    service, repository, scheduler, _, _ = service_holder
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    agent = RecordingExerciseAgent(
        repository, exercise.id, evaluations=[RuntimeError("模型超时")]
    )
    service._make_exercise_agent = lambda: agent  # type: ignore[attr-defined]
    scheduler.push(decision(ActionPlan(action="evaluate_exercise")))

    events = await collect(
        service,
        service.prepare_turn(
            conversation.id,
            dispatch_input(str(uuid4()), "我的答案是 1"),
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.kind == "dynamic"
    assert result.exercise is not None
    assert result.exercise.student_answer_text == "我的答案是 1"
    assert result.evaluation is None
    assert "作答已保存" in result.assistant_message
    assert agent.observed_answers == ["我的答案是 1"]
    assert any(
        item.action == "evaluate_exercise" and item.status == "failed"
        for item in result.composite_results
    )


# --------------------------------------------------------------------------
# 7. 显式练习目标校验
# --------------------------------------------------------------------------


def test_invalid_or_cross_conversation_exercise_is_rejected(tmp_path: Path) -> None:
    service, repository, _, _, _ = setup_service(tmp_path)
    conversation_a = repository.create_conversation()
    conversation_b = repository.create_conversation()
    _, exercise_a = create_problem_with_exercise(
        repository, conversation_id=conversation_a.id
    )
    _, exercise_b = create_problem_with_exercise(
        repository, conversation_id=conversation_b.id
    )

    with pytest.raises(ProblemNotFoundError):
        service.prepare_turn(
            conversation_a.id,
            answered_input(str(uuid4()), "1", exercise_id=str(uuid4())),
        )
    with pytest.raises(ProblemNotFoundError):
        service.prepare_turn(
            conversation_a.id,
            answered_input(str(uuid4()), "1", exercise_id=exercise_b.id),
        )

    active = repository.get_active_exercise(conversation_a.id)
    assert active is not None and active.id == exercise_a.id
    assert active.student_answer_text is None
    assert active.evaluation_json is None


def test_invalid_exercise_id_http_404_does_not_touch_active(tmp_path: Path) -> None:
    service, repository, _, _, _ = setup_service(tmp_path)
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    client = install_http(service)
    try:
        response = client.post(
            f"/api/conversations/{conversation.id}/turns",
            json={
                "request_id": str(uuid4()),
                "query": "1",
                "kind": "exercise",
                "exercise_id": str(uuid4()),
                "student_answer": "1",
            },
        )
        assert response.status_code == 404
        active = repository.get_active_exercise(conversation.id)
        assert active is not None and active.id == exercise.id
        assert active.student_answer_text is None
    finally:
        remove_http_override()


# --------------------------------------------------------------------------
# 8. 本轮复核：附件视图、纯图片作答、重试、补充过程、课程切换
# --------------------------------------------------------------------------


def test_exercise_view_with_attachment_uses_attachment_contract(
    tmp_path: Path,
) -> None:
    service, repository, _, _, _ = setup_service(tmp_path)
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    attachment = repository.create_attachment(
        conversation_id=conversation.id,
        attachment_id=str(uuid4()),
        stored_name="answer.png",
        mime="image/png",
        width=120,
        height=80,
        size_bytes=10,
        sha256="a" * 64,
    )
    repository.link_exercise_attachments(
        exercise.id, [(attachment.id, "student_solution")]
    )

    view = service._build_exercise_view(repository.get_exercise(exercise.id))

    assert len(view.attachments) == 1
    assert view.attachments[0].role == "student_solution"
    assert view.attachments[0].url.endswith(f"/attachments/{attachment.id}")


@pytest.mark.anyio
async def test_image_only_exercise_answer_http_returns_attachment_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import images

    monkeypatch.setattr(images, "ATTACHMENTS_ROOT", tmp_path / "attachments")
    service, repository, _, _, _ = setup_service(tmp_path)
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    client = install_http(service, repository)
    try:
        upload = client.post(
            f"/api/conversations/{conversation.id}/attachments",
            files={"file": ("work.png", png_bytes(), "image/png")},
        )
        assert upload.status_code == 201, upload.text
        attachment = upload.json()
        response = client.post(
            f"/api/conversations/{conversation.id}/turns",
            json={
                "request_id": str(uuid4()),
                "query": "",
                "kind": "exercise",
                "exercise_id": exercise.id,
                "attachments": [{"id": attachment["id"], "role": "solution"}],
            },
        )
        assert response.status_code == 200, response.text
        result = parse_sse(response.text)[-1]
        assert result["kind"] == "exercise"
        assert result["exercise"]["attachments"] == [
            {
                "id": attachment["id"],
                "role": "student_solution",
                "mime": "image/png",
                "width": attachment["width"],
                "height": attachment["height"],
                "url": (
                    f"/api/conversations/{conversation.id}/attachments/"
                    f"{attachment['id']}"
                ),
            }
        ]
    finally:
        remove_http_override()


@pytest.mark.anyio
async def test_image_only_dispatch_evaluates_active_exercise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """纯图片提交给当前练习时，调度快捷路径必须评价而不是纠正原题。"""

    from app import images

    monkeypatch.setattr(images, "ATTACHMENTS_ROOT", tmp_path / "attachments")
    service, repository, _, _, _ = setup_service(tmp_path)
    service._make_scheduler = lambda: SchedulerAgent(None)  # type: ignore[arg-type]
    conversation = repository.create_conversation()
    problem, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    client = install_http(service, repository)
    try:
        upload = client.post(
            f"/api/conversations/{conversation.id}/attachments",
            files={"file": ("work.png", png_bytes(), "image/png")},
        )
        assert upload.status_code == 201, upload.text
        attachment = upload.json()
        response = client.post(
            f"/api/conversations/{conversation.id}/turns",
            json={
                "request_id": str(uuid4()),
                "query": "",
                "attachments": [{"id": attachment["id"], "role": "solution"}],
            },
        )
        assert response.status_code == 200, response.text
        result = parse_sse(response.text)[-1]
        assert result["kind"] == "dynamic"
        assert result["exercise"]["id"] == exercise.id
        assert result["exercise"]["student_answer_text"] is None
        assert result["exercise"]["attachments"][0]["role"] == "student_solution"
        assert result["evaluation"]["is_correct"] is True
        assert any(
            item["action"] == "evaluate_exercise" and item["status"] == "success"
            for item in result["composite_results"]
        )
        active = repository.get_active_problem(conversation.id)
        assert active is not None and active.id == problem.id
        assert active.version == 1
    finally:
        remove_http_override()


@pytest.mark.anyio
async def test_image_only_input_dispatches_to_exercise_when_answerable() -> None:
    scheduler = SchedulerAgent(settings=None)  # type: ignore[arg-type]

    pending = await scheduler.plan(
        "",
        has_attachments=True,
        active_exercise_status="pending_answer",
    )
    assert [item.action for item in pending.actions] == ["evaluate_exercise"]

    supplement = await scheduler.plan(
        "",
        has_attachments=True,
        active_exercise_status="submitted",
        active_exercise_needs_supplement=True,
    )
    assert [item.action for item in supplement.actions] == ["evaluate_exercise"]

    new_problem = await scheduler.plan("", has_attachments=True)
    assert [item.action for item in new_problem.actions] == ["explain_problem"]


@pytest.mark.anyio
async def test_retry_text_keeps_previously_saved_answer(tmp_path: Path) -> None:
    service_holder = setup_service(
        tmp_path,
        scheduler_decisions=(decision(ActionPlan(action="evaluate_exercise")),),
    )
    service, repository, scheduler, _, _ = service_holder
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    repository.save_exercise_answer(
        exercise.id,
        student_answer_text="1",
        student_solution_text=None,
    )

    events = await collect(
        service,
        service.prepare_turn(
            conversation.id,
            dispatch_input(str(uuid4()), "重试评价"),
        ),
    )
    result = next(event for event in events if event.type == "result")
    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert stored.student_answer_text == "1"
    assert stored.evaluation_json is not None
    assert result.exercise is not None
    assert result.exercise.student_answer_text == "1"

    # 独立作答入口同样不能把“重试”写成答案。
    _, second = create_problem_with_exercise(
        repository,
        conversation_id=conversation.id,
        statement="另一道题",
        exercise_statement="计算 1+1",
    )
    repository.save_exercise_answer(
        second.id,
        student_answer_text="2",
        student_solution_text=None,
    )
    dedicated = await collect(
        service,
        service.prepare_turn(
            conversation.id,
            answered_input(str(uuid4()), "重试", exercise_id=second.id),
        ),
    )
    dedicated_result = next(
        event for event in dedicated if event.type == "result"
    )
    assert dedicated_result.exercise is not None
    assert dedicated_result.exercise.student_answer_text == "2"
    assert repository.get_exercise(second.id).evaluation_json is not None


@pytest.mark.anyio
async def test_retry_reloads_saved_answer_images(tmp_path: Path) -> None:
    service_holder = setup_service(
        tmp_path,
        scheduler_decisions=(decision(ActionPlan(action="evaluate_exercise")),),
    )
    service, repository, _, exercise_agent, _ = service_holder
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    root = tmp_path / "attachments"
    root.mkdir(parents=True, exist_ok=True)
    stored_name = "saved-answer.png"
    (root / stored_name).write_bytes(png_bytes())
    attachment = repository.create_attachment(
        conversation_id=conversation.id,
        attachment_id=str(uuid4()),
        stored_name=stored_name,
        mime="image/png",
        width=320,
        height=140,
        size_bytes=100,
        sha256="b" * 64,
    )
    repository.link_exercise_attachments(
        exercise.id, [(attachment.id, "student_solution")]
    )
    repository.save_exercise_answer(
        exercise.id,
        student_answer_text="1",
        student_solution_text=None,
    )

    events = await collect(
        service,
        service.prepare_turn(
            conversation.id,
            dispatch_input(str(uuid4()), "重试评价"),
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.exercise is not None
    assert result.exercise.student_answer_text == "1"
    assert exercise_agent.evaluate_calls
    assert len(exercise_agent.evaluate_calls[-1]["images"]) == 1
    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert [item.id for item in stored.attachments] == [attachment.id]


@pytest.mark.anyio
async def test_dedicated_retry_reloads_saved_answer_images(tmp_path: Path) -> None:
    service, repository, _, exercise_agent, _ = setup_service(tmp_path)
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    root = tmp_path / "attachments"
    root.mkdir(parents=True, exist_ok=True)
    stored_name = "saved-dedicated-answer.png"
    (root / stored_name).write_bytes(png_bytes())
    attachment = repository.create_attachment(
        conversation_id=conversation.id,
        attachment_id=str(uuid4()),
        stored_name=stored_name,
        mime="image/png",
        width=320,
        height=140,
        size_bytes=100,
        sha256="c" * 64,
    )
    repository.link_exercise_attachments(
        exercise.id, [(attachment.id, "student_answer")]
    )
    repository.save_exercise_answer(
        exercise.id,
        student_answer_text="1",
        student_solution_text=None,
    )

    events = await collect(
        service,
        service.prepare_turn(
            conversation.id,
            answered_input(str(uuid4()), "重试", exercise_id=exercise.id),
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.exercise is not None
    assert result.exercise.student_answer_text == "1"
    assert exercise_agent.evaluate_calls
    assert len(exercise_agent.evaluate_calls[-1]["images"]) == 1


@pytest.mark.anyio
async def test_needs_supplement_allows_supplement_and_reevaluation(
    tmp_path: Path,
) -> None:
    supplement_eval = AnswerEvaluation(
        is_correct=None,
        final_answer_assessment="只写了答案，过程不足。",
        step_by_step_analysis="无法判断推导是否掌握。",
        guidance="请补充求导步骤。",
        needs_supplement=True,
    )
    final_eval = AnswerEvaluation(
        is_correct=True,
        final_answer_assessment="答案正确。",
        step_by_step_analysis="补充后的求导步骤完整。",
        guidance="步骤清楚。",
        needs_supplement=False,
    )
    service, repository, _, _, _ = setup_service(
        tmp_path, evaluations=(final_eval,)
    )
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    repository.record_exercise_answer_and_evaluation(
        exercise.id,
        student_answer_text="5",
        student_solution_text=None,
        evaluation_json=json.dumps(supplement_eval.model_dump(mode="json")),
    )

    events = await collect(
        service,
        service.prepare_turn(
            conversation.id,
            answered_input(
                str(uuid4()),
                "6",
                exercise_id=exercise.id,
                solution_text="先对 x^2 求导得到 2x。",
            ),
        ),
    )
    result = next(event for event in events if event.type == "result")
    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert stored.student_answer_text == "6"
    assert stored.student_solution_text == "先对 x^2 求导得到 2x。"
    assert result.evaluation is not None
    assert result.evaluation.needs_supplement is False


@pytest.mark.anyio
async def test_dynamic_supplement_appends_steps_without_losing_answer(
    tmp_path: Path,
) -> None:
    supplement_eval = AnswerEvaluation(
        is_correct=None,
        final_answer_assessment="过程不足。",
        step_by_step_analysis="需要补充。",
        guidance="请补充步骤。",
        needs_supplement=True,
    )
    final_eval = AnswerEvaluation(
        is_correct=True,
        final_answer_assessment="补充后正确。",
        step_by_step_analysis="步骤完整。",
        guidance="很好。",
        needs_supplement=False,
    )
    service_holder = setup_service(
        tmp_path,
        scheduler_decisions=(decision(ActionPlan(action="evaluate_exercise")),),
        evaluations=(final_eval,),
    )
    service, repository, _, _, _ = service_holder
    conversation = repository.create_conversation()
    _, exercise = create_problem_with_exercise(
        repository, conversation_id=conversation.id
    )
    repository.record_exercise_answer_and_evaluation(
        exercise.id,
        student_answer_text="5",
        student_solution_text=None,
        evaluation_json=json.dumps(supplement_eval.model_dump(mode="json")),
    )

    events = await collect(
        service,
        service.prepare_turn(
            conversation.id,
            dispatch_input(str(uuid4()), "补充：先对 x^2 求导得到 2x"),
        ),
    )
    result = next(event for event in events if event.type == "result")
    stored = repository.get_exercise(exercise.id)
    assert stored is not None
    assert stored.student_answer_text == "5"
    assert stored.student_solution_text == "补充：先对 x^2 求导得到 2x"
    assert result.evaluation is not None
    assert result.evaluation.needs_supplement is False


@pytest.mark.anyio
async def test_successful_action_updates_confirmed_course(tmp_path: Path) -> None:
    service_holder = setup_service(
        tmp_path,
        scheduler_decisions=(
            decision(
                ActionPlan(
                    action="query_papers",
                    parameters={"exam_type": "final"},
                ),
                course="线性代数",
            ),
        ),
    )
    service, repository, _, _, _ = service_holder
    conversation = repository.create_conversation()
    repository.upsert_context(conversation.id, confirmed_course="高等数学")

    events = await collect(
        service,
        service.prepare_turn(
            conversation.id,
            dispatch_input(str(uuid4()), "我要复习线性代数，找期末试卷"),
        ),
    )
    result_event = next(event for event in events if event.type == "result")
    assert result_event.result is not None
    assert result_event.result.filters is not None
    assert result_event.result.filters.course == "线性代数"
    context = repository.get_context(conversation.id)
    assert context is not None
    assert context.confirmed_course == "线性代数"