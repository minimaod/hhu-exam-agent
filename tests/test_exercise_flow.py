"""F006 练习题生成、防泄露、作答评价与回看测试。"""

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.conversations.repository import ConversationRepository
from app.tutoring.exercise_llm import AnswerEvaluation, GeneratedExercise
from tests.tutoring_fakes import FakeExerciseAgent, FakeTutor, sample_explanation


@pytest.fixture
def test_setup(tmp_path: Path):
    db_path = tmp_path / "study.sqlite3"
    app = create_app()
    client = TestClient(app)

    repo = ConversationRepository(db_path)
    repo.initialize()

    # 替换 app.state.conversation_store.repository 为隔离库
    app.state.conversation_store = type(
        "Store", (), {"repository": repo, "error": None}
    )()

    fake_ex_agent = FakeExerciseAgent()
    fake_tutor = FakeTutor(explanations=[sample_explanation(final_answer="1")])

    # 注入到当前 service 依赖
    return client, repo, fake_ex_agent, fake_tutor


def insert_test_problem(repo: ConversationRepository, conversation_id: str, problem_id: str = "prob-1") -> str:
    with repo._write() as conn:
        conn.execute(
            """
            INSERT INTO problems (
                id, conversation_id, root_problem_id, version,
                statement_text, status, clarity, source_type,
                active, created_at, updated_at
            ) VALUES (?, ?, ?, 1, '求极限 lim(x->0) sin(x)/x', 'explained', 'clear', 'none', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """,
            (problem_id, conversation_id, problem_id),
        )
    return problem_id


def test_exercise_creation_and_solution_leak_prevention(tmp_path: Path) -> None:
    """严格验证：未查看/未作答前，会话详情和公开接口绝不泄露 reference_solution_text。"""
    db_path = tmp_path / "study.sqlite3"
    repo = ConversationRepository(db_path)
    repo.initialize()

    c = repo.create_conversation()
    pid = insert_test_problem(repo, c.id, "prob-1")

    ex = repo.create_exercise(
        id="ex-1",
        conversation_id=c.id,
        problem_id=pid,
        root_problem_id=pid,
        statement_text="类似题：求极限 lim(x->0) tan(x)/x",
        reference_solution_text="利用 tan(x) ~ x，得极限为 1。",
        knowledge_point="等价无穷小代换",
    )

    from app.conversations.service import ConversationService
    svc = ConversationService(repo, None, None)
    view = svc._build_exercise_view(ex)

    assert view.statement_text == "类似题：求极限 lim(x->0) tan(x)/x"
    assert view.status == "pending_answer"
    # 严格防泄露校验：此时 reference_solution_text 必须为空！
    assert view.reference_solution_text is None

    # 主动查看解答后
    revealed = repo.reveal_exercise_solution("ex-1")
    revealed_view = svc._build_exercise_view(revealed, allow_solution=True)
    assert revealed_view.status == "revealed"
    assert revealed_view.reference_solution_text == "利用 tan(x) ~ x，得极限为 1。"


def test_exercise_evaluation_and_after_revealed_flag(tmp_path: Path) -> None:
    """验证作答评价及查看解答后作答标记。"""
    db_path = tmp_path / "study.sqlite3"
    repo = ConversationRepository(db_path)
    repo.initialize()

    c = repo.create_conversation()
    pid = insert_test_problem(repo, c.id, "prob-2")

    repo.create_exercise(
        id="ex-2",
        conversation_id=c.id,
        problem_id=pid,
        root_problem_id=pid,
        statement_text="练习题",
        reference_solution_text="标准答案为 2",
        knowledge_point="积分计算",
    )

    # 模拟学生先点击了查看解答
    repo.reveal_exercise_solution("ex-2")

    # 之后提交作答
    eval_payload = AnswerEvaluation(
        is_correct=True,
        final_answer_assessment="结果为 2，完全正确。",
        step_by_step_analysis="推导过程完整严密。",
        guidance="掌握很好。",
        needs_supplement=False,
    )
    up_ex = repo.record_exercise_answer_and_evaluation(
        "ex-2",
        student_answer_text="2",
        student_solution_text="按分部积分法计算得到 2",
        evaluation_json=json.dumps(eval_payload.model_dump(mode="json")),
        answered_after_revealed=True,
    )

    from app.conversations.service import ConversationService
    svc = ConversationService(repo, None, None)
    view = svc._build_exercise_view(up_ex)

    assert view.status == "submitted"
    assert view.answered_after_revealed is True
    assert view.evaluation is not None
    assert view.evaluation.is_correct is True
    assert view.reference_solution_text == "标准答案为 2"


def test_exercise_needs_supplement_when_unclear(tmp_path: Path) -> None:
    """验证当学生过程依据不足或图片模糊时，不直接判错，给出补充提示。"""
    db_path = tmp_path / "study.sqlite3"
    repo = ConversationRepository(db_path)
    repo.initialize()

    c = repo.create_conversation()
    pid = insert_test_problem(repo, c.id, "prob-3")

    repo.create_exercise(
        id="ex-3",
        conversation_id=c.id,
        problem_id=pid,
        root_problem_id=pid,
        statement_text="练习题",
        reference_solution_text="答案为 5",
        knowledge_point="导数应用",
    )

    eval_payload = AnswerEvaluation(
        is_correct=None,
        final_answer_assessment="只写出了答案 5，未提供推导过程。",
        step_by_step_analysis="无法根据当前信息判定是否掌握求导法则。",
        guidance="请补充写出求导的具体步骤，方便为您详细分析。",
        needs_supplement=True,
    )
    up_ex = repo.record_exercise_answer_and_evaluation(
        "ex-3",
        student_answer_text="5",
        student_solution_text=None,
        evaluation_json=json.dumps(eval_payload.model_dump(mode="json")),
    )

    from app.conversations.service import ConversationService
    svc = ConversationService(repo, None, None)
    view = svc._build_exercise_view(up_ex)

    assert view.evaluation is not None
    assert view.evaluation.is_correct is None
    assert view.evaluation.needs_supplement is True
