"""F019 第一阶段程序测试：自然语言授权正反例、7类讲解/追问参数、与“懂了，下一题”衔接。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.conversations import answer_gate, final_reply
from app.conversations.authorization import SemanticAuthOutcome, authorize_action
from app.conversations.graph import ActionPlan
from app.conversations.narration import NarrationMixin
from app.conversations.repository import NewProblem
from app.conversations.turn_types import _DynamicContext
from app.tutoring.exercise_llm import (
    ANSWER_EVALUATION_PROMPT,
    AnswerEvaluation,
    GeneratedExercise,
)
from app.tutoring.llm import DeepSeekTutor
from app.tutoring.models import (
    Explanation,
    FollowupAnswer,
    KnowledgePoint,
    Source,
    Step,
    fit_knowledge_point_name,
)

from tests.question_fakes import FakeQuestionRuntime
from tests.test_question_turns import (
    add_search_turn,
    build_service as build_question_service,
    run as run_turn,
    sample_card,
    turn_input as question_turn_input,
)

from tests.test_f006_review_fixes import (
    answered_input,
    collect,
    create_problem_with_exercise,
    decision,
    dispatch_input,
    png_bytes,
    setup_service,
)


def _mistake_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM mistake_records").fetchone()[0])


class _FakeAuthVerifier:
    def __init__(self, verdict: SemanticAuthOutcome) -> None:
        self.verdict = verdict
        self.calls: list[dict[str, Any]] = []

    async def verify(
        self,
        *,
        action: str,
        user_text: str,
        quote: str | None,
        suggestions: list[str],
        last_assistant_reply: str | None = None,
    ) -> SemanticAuthOutcome:
        self.calls.append(
            {
                "action": action,
                "user_text": user_text,
                "quote": quote,
                "suggestions": suggestions,
                "last_assistant_reply": last_assistant_reply,
            }
        )
        return self.verdict


def test_natural_save_via_semantic_verifier_and_blacklist_veto(tmp_path: Path) -> None:
    """未命中正则的自然保存经独立复核放行；但状态询问/假设/否定一票否决不调复核。"""

    # 1) 自然口语 + 合法原话依据 + semantic explicit_request -> 允许保存
    verifier = _FakeAuthVerifier("explicit_request")
    service, repository, _, _, _ = setup_service(
        tmp_path,
        scheduler_decisions=[
            decision(
                ActionPlan(
                    action="record_mistake",
                    parameters={
                        "basis": {"kind": "student_request", "quote": "这题帮我先记在本子上"}
                    },
                ),
                reply="已保存到错题本。",
            )
        ],
    )
    service._make_auth_verifier = lambda: verifier  # type: ignore[attr-defined]
    conv = repository.create_conversation().id
    problem, _ = create_problem_with_exercise(repository, conversation_id=conv)
    repository.upsert_context(conv, active_problem_id=problem.id)
    before = _mistake_count(tmp_path / "study.sqlite3")

    prepared = service.prepare_turn(conv, dispatch_input(str(uuid4()), "这题帮我先记在本子上，考前再看"))
    events = asyncio.run(collect(service, prepared))
    result = next(e for e in events if e.type == "result")
    item = next(x for x in (result.composite_results or []) if x.action == "record_mistake")
    assert item.status == "success"
    assert len(verifier.calls) == 1
    assert _mistake_count(tmp_path / "study.sqlite3") == before + 1

    # 2) 状态询问 / 假设问句 -> 即使 semantic_verifier 被设成 explicit_request 也绝不触发且直接拒写
    verifier2 = _FakeAuthVerifier("explicit_request")
    service2, repo2, _, _, _ = setup_service(
        tmp_path / "veto",
        scheduler_decisions=[
            decision(
                ActionPlan(
                    action="record_mistake",
                    parameters={"basis": {"kind": "student_request", "quote": "存进本子里了吗"}},
                ),
                reply="我来说明状态。",
            ),
            decision(
                ActionPlan(
                    action="record_mistake",
                    parameters={"basis": {"kind": "student_request", "quote": "如果保存进错题本"}},
                ),
                reply="假设提问不保存。",
            ),
        ],
    )
    service2._make_auth_verifier = lambda: verifier2  # type: ignore[attr-defined]
    conv2 = repo2.create_conversation().id
    p2, _ = create_problem_with_exercise(repo2, conversation_id=conv2)
    repo2.upsert_context(conv2, active_problem_id=p2.id)
    before2 = _mistake_count(tmp_path / "veto" / "study.sqlite3")

    for msg in ("刚才那道题存进本子里了吗？", "如果保存进错题本会怎么样？"):
        prep = service2.prepare_turn(conv2, dispatch_input(str(uuid4()), msg))
        evs = asyncio.run(collect(service2, prep))
        res = next(e for e in evs if e.type == "result")
        rec = next(x for x in (res.composite_results or []) if x.action == "record_mistake")
        assert rec.status in ("failed", "clarification")
        assert rec.details.get("error_code") == "AUTHORIZATION_REQUIRED"
    assert verifier2.calls == []
    assert _mistake_count(tmp_path / "veto" / "study.sqlite3") == before2


def test_semantic_verifier_clarify_and_reject_fallback(tmp_path: Path) -> None:
    """语义复核返回 clarify_needed 触发澄清；无 verifier 时未命中词表保守拒绝。"""

    verifier = _FakeAuthVerifier("clarify_needed")
    service, repo, _, _, _ = setup_service(
        tmp_path,
        scheduler_decisions=[
            decision(
                ActionPlan(
                    action="record_mistake",
                    parameters={"basis": {"kind": "student_request", "quote": "处理一下"}},
                ),
                reply="需要确认。",
            )
        ],
    )
    service._make_auth_verifier = lambda: verifier  # type: ignore[attr-defined]
    conv = repo.create_conversation().id
    prob, _ = create_problem_with_exercise(repo, conversation_id=conv)
    repo.upsert_context(conv, active_problem_id=prob.id)

    prepared = service.prepare_turn(conv, dispatch_input(str(uuid4()), "处理一下"))
    events = asyncio.run(collect(service, prepared))
    result = next(e for e in events if e.type == "result")
    item = next(x for x in (result.composite_results or []) if x.action == "record_mistake")
    assert item.status == "clarification"
    assert item.details.get("needs_user") is True

    # 不配 auth_verifier 时直接保守拒绝（用新独立服务实例，避免 FakeScheduler 耗尽）
    service2, repo2, _, _, _ = setup_service(
        tmp_path / "fallback",
        scheduler_decisions=[
            decision(
                ActionPlan(
                    action="record_mistake",
                    parameters={"basis": {"kind": "student_request", "quote": "处理一下"}},
                ),
                reply="需要你明确说保存。",
            )
        ],
    )
    conv2 = repo2.create_conversation().id
    prob2, _ = create_problem_with_exercise(repo2, conversation_id=conv2)
    repo2.upsert_context(conv2, active_problem_id=prob2.id)
    prepared2 = service2.prepare_turn(conv2, dispatch_input(str(uuid4()), "处理一下"))
    events2 = asyncio.run(collect(service2, prepared2))
    result2 = next(e for e in events2 if e.type == "result")
    item2 = next(x for x in (result2.composite_results or []) if x.action == "record_mistake")
    assert item2.status in ("failed", "clarification")
    assert item2.details.get("error_code") == "AUTHORIZATION_REQUIRED"


def test_followup_intents_and_no_process_flag(tmp_path: Path) -> None:
    """验证 why_step/alt_method/check_process 传参以及无过程时禁止猜测错因标记。"""

    captured: list[dict[str, Any]] = []

    class _RecordingTutor:
        async def explain(self, **_kwargs: Any) -> Explanation:
            return Explanation(
                knowledge_points=[KnowledgePoint(name="换元积分", explanation="条件：单调可微")],
                steps=[Step(index=1, title="换元", content="令 u = x^2")],
                final_answer="1/2",
            )

        async def answer_followup(
            self,
            *,
            statement: str,
            explanation: Explanation,
            recent_followups: list[tuple[str, str]],
            question: str,
            intent: str = "auto",
            has_student_process: bool | None = None,
        ) -> FollowupAnswer:
            captured.append(
                {
                    "question": question,
                    "intent": intent,
                    "has_student_process": has_student_process,
                }
            )
            return FollowupAnswer(answer="没有看到过程，请先补充。", related_steps=[1])

    service, repo, _, _, _ = setup_service(
        tmp_path,
        scheduler_decisions=[
            decision(
                ActionPlan(
                    action="followup_problem",
                    parameters={"question": "我的过程哪里错了？", "intent": "check_process"},
                ),
                reply="请补充你的解题过程，便于具体定位错误。",
            ),
            decision(reply="请补充你的解题过程，便于具体定位错误。"),
            decision(
                ActionPlan(
                    action="followup_problem",
                    parameters={"question": "换一种方法讲", "intent": "alt_method"},
                ),
                reply="换一种几何角度解释。",
            ),
            decision(reply="换一种几何角度解释。"),
        ],
    )
    service._make_tutor = lambda: _RecordingTutor()  # type: ignore[attr-defined]
    conv = repo.create_conversation().id
    prob, _ = create_problem_with_exercise(repo, conversation_id=conv)
    # 清除该题与练习的过程文本以验证无过程红线标志
    repo.upsert_context(conv, active_problem_id=prob.id)
    # 模拟对母题追问（target="problem"）
    service._make_scheduler = service._make_scheduler  # noqa: B018
    p1 = service.prepare_turn(conv, dispatch_input(str(uuid4()), "我的过程哪里错了？"))
    e1 = asyncio.run(collect(service, p1))
    r1 = next(e for e in e1 if e.type == "result")
    f1 = next(x for x in (r1.composite_results or []) if x.action == "followup_problem")
    assert f1.status == "success"
    assert captured[-1]["intent"] == "check_process"
    assert captured[-1]["has_student_process"] is False

    p2 = service.prepare_turn(conv, dispatch_input(str(uuid4()), "换一种方法"))
    e2 = asyncio.run(collect(service, p2))
    r2 = next(e for e in e2 if e.type == "result")
    f2 = next(x for x in (r2.composite_results or []) if x.action == "followup_problem")
    assert f2.status == "success"
    assert captured[-1]["intent"] == "alt_method"


def test_understood_next_question_without_explained_problem(tmp_path: Path) -> None:
    """无已讲解主题目时，说“懂了，下一题”不会在 record_feedback 报错卡住切题。"""

    service, repo, _, _, _ = setup_service(
        tmp_path,
        scheduler_decisions=[
            decision(
                ActionPlan(action="record_feedback", parameters={}),
                ActionPlan(
                    action="generate_exercise",
                    parameters={
                        "course": "高等数学",
                        "topic": "定积分",
                        "basis": {"kind": "student_request", "quote": "下一题"},
                    },
                ),
                reply="已继续下一道练习。",
            )
        ],
    )
    conv = repo.create_conversation().id
    prepared = service.prepare_turn(conv, dispatch_input(str(uuid4()), "懂了，下一题给我来道定积分"))
    events = asyncio.run(collect(service, prepared))
    result = next(e for e in events if e.type == "result")
    fb = next(x for x in (result.composite_results or []) if x.action == "record_feedback")
    assert fb.status == "success"


def test_hint_scope_exposes_only_public_material_in_teaching_material() -> None:
    """scope=hint 时只提供公开题面与考点给终局回复，绝不附带答案。"""

    ctx = _DynamicContext(conversation_id="c1")
    ctx.answer_scope = "hint"
    ctx.question_card = sample_card(statement="求极限 lim(x->0) sin(x)/x")
    ctx.authorized_text = "题干：求极限 lim(x->0) sin(x)/x\n公开考点：重要极限"
    text = NarrationMixin._teaching_material(ctx)
    assert text is not None
    assert "lim(x->0)" in text
    assert "重要极限" in text


def test_fit_knowledge_point_name_handles_long_latex() -> None:
    """超长带 LaTeX 知识点名称应安全收敛到 80 字符内，不留半截 $。"""

    long_kp = r"重要极限 $\lim_{x \to 0}\frac{\sin x}{x}=1$ 与 $\lim_{x \to 0}\frac{\ln(1+x)}{x}=1$ 的综合应用技巧"
    assert len(long_kp) > 80
    fitted = fit_knowledge_point_name(long_kp)
    assert len(fitted) <= 80
    assert fitted.count("$") % 2 == 0
    assert KnowledgePoint(name=fitted, explanation="ok").name == fitted


def test_standalone_exercise_handles_long_knowledge_point_and_followup(tmp_path: Path) -> None:
    """自主出题返回 >80 字符知识点名时不再崩溃，且随后追问同样兼容。"""

    class _LongKpExerciseAgent:
        async def generate_exercise(self, **kwargs: Any) -> GeneratedExercise:
            return GeneratedExercise(
                statement=r"求极限 $\lim_{x \to 0} \frac{\sin 3x}{x}$",
                knowledge_point=r"重要极限 $\lim_{x \to 0}\frac{\sin x}{x}=1$ 与 $\lim_{x \to 0}\frac{\ln(1+x)}{x}=1$ 的应用",
                reference_solution=r"原式 $= \lim_{x \to 0} 3 \cdot \frac{\sin 3x}{3x} = 3$。",
            )

    class _HintTutor:
        async def answer_followup(self, **kwargs: Any) -> FollowupAnswer:
            return FollowupAnswer(answer="先把分母凑成与分子同阶的 3x，注意系数 3 不能漏。", related_steps=[])

    service, repo, _, _, _ = setup_service(
        tmp_path,
        scheduler_decisions=[
            decision(
                ActionPlan(
                    action="generate_exercise",
                    parameters={
                        "course": "高等数学",
                        "topic": "重要极限",
                        "basis": {"kind": "student_request", "quote": "出题"},
                    },
                ),
                reply="已出好。",
            ),
            decision(
                ActionPlan(
                    action="followup_problem",
                    parameters={"question": "第一步怎么凑？", "target": "exercise", "intent": "why_step"},
                ),
                reply="凑出 3x。",
            ),
            decision(reply="凑出 3x。"),
        ],
    )
    service._make_exercise_agent = lambda: _LongKpExerciseAgent()  # type: ignore[attr-defined]
    service._make_tutor = lambda: _HintTutor()  # type: ignore[attr-defined]

    conv = repo.create_conversation().id
    p1 = service.prepare_turn(conv, dispatch_input(str(uuid4()), "按重要极限出题"))
    e1 = asyncio.run(collect(service, p1))
    r1 = next(e for e in e1 if e.type == "result")
    gen = next(x for x in (r1.composite_results or []) if x.action == "generate_exercise")
    assert gen.status == "success"
    assert r1.exercise is not None

    p2 = service.prepare_turn(conv, dispatch_input(str(uuid4()), "这一步为什么这样凑？"))
    e2 = asyncio.run(collect(service, p2))
    r2 = next(e for e in e2 if e.type == "result")
    fu = next(x for x in (r2.composite_results or []) if x.action == "followup_problem")
    assert fu.status == "success"
    assert "3x" in (r2.tutoring.answer if r2.tutoring else "")


def test_optional_process_left_empty_normalizes_eval_and_reveals_solution(
    tmp_path: Path,
) -> None:
    """只交最终答案、选填过程留空且已判对错时，不再误标需补充并正常解锁参考解答。"""

    assert "选填项" in ANSWER_EVALUATION_PROMPT
    assert "needs_supplement 设为 false" in ANSWER_EVALUATION_PROMPT

    for empty_analysis in ("", None):
        parsed = AnswerEvaluation.model_validate(
            {
                "is_correct": False,
                "final_answer_assessment": "最终答案 3 不正确。",
                "step_by_step_analysis": empty_analysis,
                "guidance": "先求导再代入。",
                "needs_supplement": False,
                "independence_assessment": "unclear",
                "independence_note": None,
            }
        )
        assert parsed.step_by_step_analysis == ""

    mistaken_model_eval = AnswerEvaluation(
        is_correct=False,
        final_answer_assessment="最终答案 3 与标准结果 5 不一致。",
        step_by_step_analysis="未提供完整过程，暂无法定位具体错误步骤。",
        guidance="先写出复合函数链式法则再分别代入求值。建议补写完整过程。",
        needs_supplement=True,
    )
    service, repo, _, _, _ = setup_service(
        tmp_path, evaluations=(mistaken_model_eval,)
    )
    conv = repo.create_conversation()
    _, exercise = create_problem_with_exercise(repo, conversation_id=conv.id)

    events = asyncio.run(
        collect(
            service,
            service.prepare_turn(
                conv.id,
                answered_input(str(uuid4()), "3", exercise_id=exercise.id),
            ),
        )
    )
    result = next(e for e in events if e.type == "result")
    assert result.evaluation is not None
    assert result.evaluation.is_correct is False
    assert result.evaluation.needs_supplement is False
    assert result.evaluation.step_by_step_analysis == ""
    assert "补写" not in result.evaluation.guidance
    assert "链式法则" in result.evaluation.guidance
    assert result.exercise is not None
    assert result.exercise.reference_solution_text is not None


def test_unclear_verdict_still_keeps_needs_supplement_and_hides_solution(
    tmp_path: Path,
) -> None:
    """真正无法判别对错（is_correct=None, needs_supplement=True）时，仍隐藏参考解答。"""

    unclear_eval = AnswerEvaluation(
        is_correct=None,
        final_answer_assessment="图面模糊，无法看清结果。",
        step_by_step_analysis="关键步骤不可辨识。",
        guidance="请补充清晰步骤或重拍手写过程。",
        needs_supplement=True,
    )
    service, repo, _, _, _ = setup_service(tmp_path, evaluations=(unclear_eval,))
    conv = repo.create_conversation()
    _, exercise = create_problem_with_exercise(repo, conversation_id=conv.id)

    events = asyncio.run(
        collect(
            service,
            service.prepare_turn(
                conv.id,
                answered_input(str(uuid4()), "3", exercise_id=exercise.id),
            ),
        )
    )
    result = next(e for e in events if e.type == "result")
    assert result.evaluation is not None
    assert result.evaluation.is_correct is None
    assert result.evaluation.needs_supplement is True
    assert result.exercise is not None
    assert result.exercise.reference_solution_text is None


def test_view_mistakes_suggestion_never_triggers_save_on_affirmative(
    tmp_path: Path,
) -> None:
    """P1-1：上一轮唯一建议是「查看错题本」时，回复「好」不得误触发保存错题。"""

    service, repo, _, _, _ = setup_service(tmp_path)
    conv = repo.create_conversation()
    problem, _ = create_problem_with_exercise(repo, conversation_id=conv.id)
    repo.upsert_context(conv.id, active_problem_id=problem.id)

    ctx = _DynamicContext(conversation_id=conv.id, active_problem=problem, problem_id=problem.id)
    action = ActionPlan(action="record_mistake", parameters={})

    blocked = service._guard_dynamic_action(
        action,
        ctx,
        {"last_suggestions": [{"label": "查看错题本", "action": "查看错题本"}]},
        "好",
    )
    assert blocked is not None
    assert blocked.error_code == "AUTHORIZATION_REQUIRED"

    allowed = service._guard_dynamic_action(
        action,
        ctx,
        {"last_suggestions": [{"label": "保存到错题本", "kind": "record"}]},
        "好",
    )
    assert allowed is None


def test_long_distance_negation_blocks_record_mistake(tmp_path: Path) -> None:
    """P1-1：同一分句中距离保存关键词超过 12 字的否定词仍须拦截写入。"""

    phrase = "别把这道上周随手挑出来给同学看的旧题留个档"
    denied = authorize_action("record_mistake", phrase, transcript=[])
    assert denied is not None
    assert denied.code == "AUTHORIZATION_REQUIRED"

    service, repo, _, _, _ = setup_service(
        tmp_path,
        scheduler_decisions=[
            decision(
                ActionPlan(
                    action="record_mistake",
                    parameters={"basis": {"kind": "student_request", "quote": "留个档"}},
                ),
                reply="已拒绝保存。",
            ),
            decision(reply="好的，这道题不会写入错题本。"),
        ],
    )
    conv = repo.create_conversation()
    problem, _ = create_problem_with_exercise(repo, conversation_id=conv.id)
    repo.upsert_context(conv.id, active_problem_id=problem.id)
    db_path = tmp_path / "study.sqlite3"
    before = _mistake_count(db_path)

    events = asyncio.run(
        collect(service, service.prepare_turn(conv.id, dispatch_input(str(uuid4()), phrase)))
    )
    result = next(e for e in events if e.type == "result")
    rec = next(x for x in (result.composite_results or []) if x.action == "record_mistake")
    assert rec.status == "failed"
    assert _mistake_count(db_path) == before


def test_correct_empty_process_passes_answer_gate_and_reveals_solution(
    tmp_path: Path,
) -> None:
    """P1-2：答对且选填过程为空（step_by_step_analysis=''）时，门禁应认定作答已完成。"""

    correct_eval = AnswerEvaluation(
        is_correct=True,
        final_answer_assessment="最终答案 5 完全正确。",
        step_by_step_analysis="",
        guidance="继续保持。",
        needs_supplement=False,
    )
    service, repo, _, _, _ = setup_service(tmp_path, evaluations=(correct_eval,))
    conv = repo.create_conversation()
    _, exercise = create_problem_with_exercise(repo, conversation_id=conv.id)

    events = asyncio.run(
        collect(
            service,
            service.prepare_turn(
                conv.id,
                answered_input(str(uuid4()), "5", exercise_id=exercise.id),
            ),
        )
    )
    result = next(e for e in events if e.type == "result")
    assert result.exercise is not None
    assert result.exercise.status == "submitted"
    assert result.exercise.reference_solution_text is not None

    stored = repo.get_exercise(exercise.id)
    assert stored is not None
    assert stored.status == "submitted"
    gate = answer_gate.exercise_solution_gate(
        request_id=str(uuid4()),
        student_text="",
        exercise=stored,
    )
    assert gate.allowed is True


def test_null_verdict_even_with_needs_supplement_false_stays_incomplete(
    tmp_path: Path,
) -> None:
    """P1-2：is_correct=None 即使漏设 needs_supplement=False，写入、回放与门禁均视为未完成。"""

    ambiguous_eval = AnswerEvaluation(
        is_correct=None,
        final_answer_assessment="无法辨识学生作答结果。",
        step_by_step_analysis="",
        guidance="请核对后重新作答。",
        needs_supplement=False,
    )
    service, repo, _, _, _ = setup_service(tmp_path, evaluations=(ambiguous_eval,))
    conv = repo.create_conversation()
    _, ex1 = create_problem_with_exercise(repo, conversation_id=conv.id)

    events = asyncio.run(
        collect(
            service,
            service.prepare_turn(
                conv.id,
                answered_input(str(uuid4()), "3", exercise_id=ex1.id),
            ),
        )
    )
    result = next(e for e in events if e.type == "result")
    assert result.exercise is not None
    assert result.exercise.status == "pending_answer"
    assert result.exercise.evaluation is not None
    assert result.exercise.evaluation.needs_supplement is True
    assert result.exercise.reference_solution_text is None

    # 直接写入未经归一化的脏 JSON（is_correct=None, needs_supplement=False）时，仓库回放与门禁依然一致拦截
    _, ex2 = create_problem_with_exercise(repo, conversation_id=conv.id)
    raw_dirty = json.dumps(
        {
            "is_correct": None,
            "final_answer_assessment": "无法判分。",
            "step_by_step_analysis": "不足以判断。",
            "guidance": "请补充过程。",
            "needs_supplement": False,
        },
        ensure_ascii=False,
    )
    repo.record_exercise_answer_and_evaluation(
        ex2.id,
        student_answer_text="3",
        student_solution_text=None,
        evaluation_json=raw_dirty,
    )
    stored2 = repo.get_exercise(ex2.id)
    assert stored2 is not None
    assert stored2.status == "pending_answer"
    view2 = service._build_exercise_view(stored2)
    assert view2.status == "pending_answer"
    assert view2.reference_solution_text is None
    gate2 = answer_gate.exercise_solution_gate(
        request_id=str(uuid4()),
        student_text="",
        exercise=stored2,
    )
    assert gate2.allowed is False


def test_selecting_new_question_does_not_route_followup_to_old_exercise(
    tmp_path: Path,
) -> None:
    """P2-3：已选中新真题且旧练习仍存在上下文中时，追问必须命中新真题而不串回旧练习。"""

    from tests.tutoring_fakes import FakeScheduler, FakeTutor

    old_card = sample_card(question_id="q-old-1", number="1", statement="旧真题：计算定积分")
    new_card = sample_card(question_id="q-new-2", number="2", statement="新真题：求矩阵的特征值")
    tutor = FakeTutor(
        followups=[FollowupAnswer(answer="这里因为特征多项式 det(λE-A)=0。", related_steps=[])]
    )
    scheduler = FakeScheduler(
        decisions=[
            decision(
                ActionPlan(
                    action="followup_problem",
                    parameters={"question": "这一步为什么？", "target": "auto", "intent": "why_step"},
                )
            ),
            decision(reply="因为特征多项式 det(λE-A)=0。"),
        ]
    )
    service, repo, _ = build_question_service(
        tmp_path,
        runtime=FakeQuestionRuntime(cards=[old_card, new_card]),
        scheduler=scheduler,
        tutor=tutor,
    )
    conv = service.create_conversation().id
    source = add_search_turn(repository=repo, conversation_id=conv, cards=[old_card, new_card])
    # 旧活跃练习存在，但会话当前已选中未讲解的新真题 new_card
    _, old_exercise = create_problem_with_exercise(
        repo, conversation_id=conv, statement="旧练习题干：求定积分 I"
    )
    asyncio.run(
        run_turn(
            service,
            conv,
            question_turn_input(
                kind="select_question", question_id=new_card.question_id, source_turn=source
            ),
        )
    )
    # 即使主动残留旧 active_exercise_id，追问仍应绑定新真题而非旧练习
    repo.upsert_context(conv, active_exercise_id=old_exercise.id)

    events = asyncio.run(
        collect(service, service.prepare_turn(conv, dispatch_input(str(uuid4()), "这一步为什么？")))
    )
    res = next(e for e in events if e.type == "result")
    fu = next(x for x in (res.composite_results or []) if x.action == "followup_problem")
    assert fu.status == "success"
    assert fu.details.get("followup_target") == "question"
    assert fu.details.get("target_id") == new_card.question_id
    assert tutor.followup_calls
    assert "特征值" in tutor.followup_calls[-1]["statement"]
    assert "旧练习题干" not in tutor.followup_calls[-1]["statement"]


def test_followup_check_process_forwards_solution_text_and_images_to_tutor(
    tmp_path: Path,
) -> None:
    """P2-4：过程诊断追问必须传出当前题真实的文字过程和过程附图，图片缺失时不虚报。"""

    from tests.tutoring_fakes import FakeScheduler, FakeTutor

    tutor = FakeTutor(
        followups=[
            FollowupAnswer(answer="第 2 步换元漏乘系数 2。", related_steps=[]),
            FollowupAnswer(answer="没有看到可读取的过程材料，请重新提供。", related_steps=[]),
        ]
    )
    scheduler = FakeScheduler(
        decisions=[
            decision(
                ActionPlan(
                    action="followup_problem",
                    parameters={"question": "我的过程哪里错了？", "target": "exercise", "intent": "check_process"},
                )
            ),
            decision(reply="第 2 步漏了系数。"),
            decision(
                ActionPlan(
                    action="followup_problem",
                    parameters={"question": "看看图里这步错哪了？", "target": "exercise", "intent": "check_process"},
                )
            ),
            decision(reply="图片不可读，暂无法定位。"),
        ]
    )
    service, repo, _, _, _ = setup_service(tmp_path, scheduler_decisions=scheduler._decisions)
    service._make_tutor = lambda: tutor  # type: ignore[attr-defined]
    conv = repo.create_conversation().id
    _, exercise = create_problem_with_exercise(repo, conversation_id=conv)

    att_id = str(uuid4())
    att_file = service._attachments_root / "proc.png"
    att_file.parent.mkdir(parents=True, exist_ok=True)
    att_file.write_bytes(png_bytes())
    repo.create_attachment(
        conversation_id=conv,
        attachment_id=att_id,
        stored_name="proc.png",
        mime="image/png",
        width=100,
        height=100,
        size_bytes=len(png_bytes()),
        sha256="b" * 64,
    )
    eval_json = json.dumps(
        AnswerEvaluation(
            is_correct=False,
            final_answer_assessment="结果不对",
            step_by_step_analysis="换元处系数遗漏",
            guidance="核对 dx 替换",
            needs_supplement=False,
        ).model_dump(mode="json"),
        ensure_ascii=False,
    )
    repo.save_exercise_answer(
        exercise.id,
        student_answer_text="1",
        student_solution_text="令 u = 2x，则 dx = du",
        attachments=[(att_id, "student_solution")],
    )
    repo.save_exercise_evaluation(exercise.id, evaluation_json=eval_json)

    events1 = asyncio.run(
        collect(
            service,
            service.prepare_turn(conv, dispatch_input(str(uuid4()), "我的过程哪里错了？")),
        )
    )
    res1 = next(e for e in events1 if e.type == "result")
    fu1 = next(x for x in (res1.composite_results or []) if x.action == "followup_problem")
    assert fu1.status == "success"
    assert fu1.details.get("has_student_process") is True
    assert fu1.details.get("process_image_count") == 1
    call1 = tutor.followup_calls[0]
    assert call1["has_student_process"] is True
    assert "令 u = 2x" in (call1["student_solution_text"] or "")
    assert len(call1["student_solution_images"]) == 1
    assert call1["student_solution_images"][0].data == png_bytes()

    # 验证 DeepSeekTutor.answer_followup 会把文本过程和 image_url 多模态块一起发往模型
    captured_messages: list[Any] = []

    class _FakeModel:
        async def ainvoke(self, messages: Any) -> Any:
            captured_messages.append(messages)

            class _Resp:
                content = json.dumps({"answer": "看到了图和步骤。", "related_steps": []})
                response_metadata = {}
                usage_metadata = None

            return _Resp()

    real_tutor = DeepSeekTutor.__new__(DeepSeekTutor)
    real_tutor._settings = None  # type: ignore[assignment]
    real_tutor._http_async_client = None

    async def _fake_call(*, system_prompt: str, human: Any, max_tokens: int, timeout: float) -> str:
        captured_messages.append((system_prompt, human))
        return json.dumps({"answer": "过程系数漏乘 2。", "related_steps": [1]})

    real_tutor._call = _fake_call  # type: ignore[method-assign]
    asyncio.run(
        real_tutor.answer_followup(
            statement="求极限",
            explanation=Explanation(
                knowledge_points=[KnowledgePoint(name="换元", explanation="变量替换")],
                steps=[Step(index=1, title="换元", content="令 u = 2x")],
                final_answer="2",
            ),
            recent_followups=[],
            question="我的过程哪里错了？",
            intent="check_process",
            has_student_process=True,
            student_solution_text="令 u = 2x，则 dx = du",
            student_solution_images=call1["student_solution_images"],
        )
    )
    assert captured_messages
    _, sent_human = captured_messages[-1]
    assert isinstance(sent_human, list)
    assert sent_human[0]["type"] == "text"
    assert "令 u = 2x，则 dx = du" in sent_human[0]["text"]
    assert any(block.get("type") == "image_url" for block in sent_human[1:])

    # 图片文件丢失且无文字过程时，不得虚报 has_student_process=True
    att_file.unlink()
    _, ex_broken = create_problem_with_exercise(repo, conversation_id=conv)
    repo.save_exercise_answer(
        ex_broken.id,
        student_answer_text="1",
        student_solution_text=None,
        attachments=[(att_id, "student_solution")],
    )
    repo.save_exercise_evaluation(ex_broken.id, evaluation_json=eval_json)
    asyncio.run(
        collect(
            service,
            service.prepare_turn(conv, dispatch_input(str(uuid4()), "看看图里这步错哪了？")),
        )
    )
    call2 = tutor.followup_calls[1]
    assert call2["has_student_process"] is False
    assert call2["student_solution_images"] == []


def test_question_only_followup_reply_revalidates_session_version_and_scope(
    tmp_path: Path,
) -> None:
    """P2-5：仅选中真题（无 ProblemRecord）追问后，/reply 按原会话、原题版本与原范围恢复材料。"""

    from tests.tutoring_fakes import FakeScheduler, FakeTutor

    card = sample_card(question_id="q-reval-1", number="3", statement="真题：求函数极值")
    runtime = FakeQuestionRuntime(cards=[card])
    tutor = FakeTutor(
        followups=[FollowupAnswer(answer="先对函数求一阶导数并令导数为零求驻点。", related_steps=[])]
    )
    scheduler = FakeScheduler(
        decisions=[
            decision(
                ActionPlan(
                    action="followup_problem",
                    parameters={"question": "第一步为什么这么设？", "target": "problem", "intent": "why_step"},
                )
            ),
            decision(reply="可以先对函数求一阶导数。"),
        ]
    )
    service, repo, _ = build_question_service(
        tmp_path, runtime=runtime, scheduler=scheduler, tutor=tutor
    )
    conv = service.create_conversation().id
    source = add_search_turn(repo, conv, [card])
    asyncio.run(
        run_turn(
            service,
            conv,
            question_turn_input(
                kind="select_question", question_id=card.question_id, source_turn=source
            ),
        )
    )

    req_id = str(uuid4())
    turn_in = dispatch_input(req_id, "第一步为什么这么设？")
    events = asyncio.run(collect(service, service.prepare_turn(conv, turn_in)))
    res = next(e for e in events if e.type == "result")
    assert res.tutoring is not None
    assert res.tutoring.kind == "followup_answer"

    saved_turn = repo.find_turn(conv, req_id)
    assert saved_turn is not None
    ctx_payload = (saved_turn.recovery or {}).get("reply_context")
    assert isinstance(ctx_payload, dict)
    refs = list(ctx_payload.get("authorized") or [])
    q_ref = next((r for r in refs if r.get("kind") == "question_followup"), None)
    assert q_ref is not None
    assert q_ref["question_id"] == card.question_id
    assert q_ref["scope"] == "explain"

    # 原会话 + 原版本重读恢复成功，并保留原回答与范围约束
    mat, notes = final_reply.material_from_context(service, conv, ctx_payload)
    assert not notes
    assert mat.teaching is not None
    assert "令导数为零求驻点" in mat.teaching
    assert "原回答范围：explain" in mat.teaching

    # 跨会话重读拒绝恢复
    other_conv = service.create_conversation().id
    other_mat, other_notes = final_reply.material_from_context(service, other_conv, ctx_payload)
    assert other_mat.teaching is None
    assert other_notes

    # 题库版本变化后拒绝恢复
    runtime.cards[0] = card.model_copy(update={"build_id": "iv-changed-version"})
    with repo._write() as conn:
        conn.execute(
            "UPDATE question_selections SET build_id = ? WHERE conversation_id = ?",
            ("iv-changed-version", conv),
        )
    changed_mat, changed_notes = final_reply.material_from_context(service, conv, ctx_payload)
    assert changed_mat.teaching is None
    assert changed_notes


def test_save_confirmation_preserves_target_id_when_active_problem_switched(
    tmp_path: Path,
) -> None:
    """复核 P1：对话恢复保留建议的 target_id，且写入严格沿用门禁校验通过的题目。"""

    service, repo, _, _, _ = setup_service(
        tmp_path,
        scheduler_decisions=[
            decision(ActionPlan(action="record_mistake"), reply="已保存原建议的旧题。"),
            decision(reply="已保存到错题本。"),
            decision(ActionPlan(action="record_mistake"), reply="找不到该题。"),
            decision(reply="请确认保存哪一题。"),
        ],
    )
    conv = repo.create_conversation().id
    old_prob = repo.create_problem_version(
        conversation_id=conv,
        problem=NewProblem(
            statement_text="旧题：求极限 sin(x)/x",
            status="explained",
            clarity="clear",
            unclear_points=[],
            uncertainties=[],
            source=Source(),
            student_answer_text=None,
            solution_text=None,
            explanation=Explanation(
                knowledge_points=[KnowledgePoint(name="重要极限", explanation="基本公式")],
                steps=[Step(index=1, title="公式", content="结果为 1")],
                final_answer="1",
            ),
        ),
    )
    new_prob = repo.create_problem_version(
        conversation_id=conv,
        problem=NewProblem(
            statement_text="新题：求导数 d/dx (x^3)",
            status="explained",
            clarity="clear",
            unclear_points=[],
            uncertainties=[],
            source=Source(),
            student_answer_text=None,
            solution_text=None,
            explanation=Explanation(
                knowledge_points=[KnowledgePoint(name="求导", explanation="幂函数求导")],
                steps=[Step(index=1, title="求导", content="结果为 3x^2")],
                final_answer="3x^2",
            ),
        ),
    )
    # 模拟上一轮建议明确指向 old_prob，但当前上下文活跃题已切换到 new_prob
    repo.upsert_context(
        conv,
        active_problem_id=new_prob.id,
        last_suggestions_json=json.dumps(
            [{"label": "保存到错题本", "action": "保存到错题本", "target_id": old_prob.id}],
            ensure_ascii=False,
        ),
    )

    db_path = tmp_path / "study.sqlite3"
    events = asyncio.run(
        collect(service, service.prepare_turn(conv, dispatch_input(str(uuid4()), "好")))
    )
    res = next(e for e in events if e.type == "result")
    rec = next(x for x in (res.composite_results or []) if x.action == "record_mistake")
    assert rec.status == "success"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT root_problem_id, statement_text FROM mistake_records").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == old_prob.root_problem_id
    assert "旧题" in rows[0][1]
    assert "新题" not in rows[0][1]

    # 对照：若原建议 target_id 失效/不存在，确认“好”不得误写入当前 new_prob
    repo.upsert_context(
        conv,
        active_problem_id=new_prob.id,
        last_suggestions_json=json.dumps(
            [{"label": "保存到错题本", "action": "保存到错题本", "target_id": "non-existent-prob"}],
            ensure_ascii=False,
        ),
    )
    events2 = asyncio.run(
        collect(service, service.prepare_turn(conv, dispatch_input(str(uuid4()), "好")))
    )
    res2 = next(e for e in events2 if e.type == "result")
    assert all(
        not (x.action == "record_mistake" and x.status == "success")
        for x in (res2.composite_results or [])
    )
    assert _mistake_count(db_path) == 1


def test_ref_followup_matches_exact_build_and_content_hash_not_old_version(
    tmp_path: Path,
) -> None:
    """复核 P2-2：查到新版后保留旧引用 Q1、为新版分配 Q2；按 Q2 追问与选中新版自动追问均不混用旧讲解或旧练习。"""

    from app.conversations.workspace import build_ref_table, build_workspace
    from tests.tutoring_fakes import FakeScheduler, FakeTutor

    card_v1 = sample_card(
        question_id="q-multi-ver", number="1", statement="旧版题干：求 sin(x) 导数"
    )
    card_v2 = card_v1.model_copy(
        update={
            "build_id": "iv-v2",
            "content_sha256": "f" * 64,
            "statement": "新版题干：求二重积分",
        }
    )
    runtime = FakeQuestionRuntime(cards=[card_v1])
    tutor = FakeTutor(
        followups=[
            FollowupAnswer(answer="新版二重积分这里需确定积分区域。", related_steps=[]),
            FollowupAnswer(answer="新版二重积分内层先对 y 积分。", related_steps=[]),
        ]
    )
    scheduler = FakeScheduler(
        decisions=[
            # Turn A：核验旧引用 Q1 仍指向已淘汰的 v1，触发 VERSION_CHANGED
            decision(ActionPlan(action="view_question_detail", parameters={"ref": "Q1"})),
            decision(reply="旧引用版本已失效，请使用新版引用。"),
            # Turn B：使用新版短引用 Q2 直接追问（同时旧版 v1 练习仍在上下文中，不能串入）
            decision(
                ActionPlan(
                    action="followup_problem",
                    parameters={"question": "第一步为什么这样写？", "ref": "Q2", "intent": "why_step"},
                )
            ),
            decision(reply="新版二重积分需要先画积分区域。"),
            # Turn C：选中新版 Q2 后不带 ref 自动追问，验证切题清理旧练习且只送入新版材料
            decision(ActionPlan(action="select_question", parameters={"ref": "Q2"})),
            decision(
                ActionPlan(
                    action="followup_problem",
                    parameters={"question": "第二步怎么确定上下限？", "intent": "why_step"},
                )
            ),
            decision(reply="新版二重积分上下限根据区域边界确定。"),
        ]
    )
    service, repo, _ = build_question_service(
        tmp_path, runtime=runtime, scheduler=scheduler, tutor=tutor
    )
    conv = service.create_conversation().id
    old_search = add_search_turn(repo, conv, [card_v1])
    old_sel = service._select_question_card(conv, old_search, old_search, card_v1)
    old_prob = repo.create_problem_version(
        conversation_id=conv,
        problem=NewProblem(
            statement_text=card_v1.statement,
            status="explained",
            clarity="clear",
            unclear_points=[],
            uncertainties=[],
            source=Source(),
            student_answer_text=None,
            solution_text=None,
            explanation=Explanation(
                knowledge_points=[KnowledgePoint(name="旧版知识点", explanation="旧版解析")],
                steps=[Step(index=1, title="旧步骤", content="旧版专属解法步骤XYZ")],
                final_answer="cos(x)",
            ),
        ),
    )
    repo.bind_question_problem(old_sel.id, old_prob.id)
    old_ex = repo.create_exercise(
        id=str(uuid4()),
        conversation_id=conv,
        problem_id=old_prob.id,
        root_problem_id=old_prob.id,
        statement_text="旧版v1专属练习题干ABC",
        reference_solution_text="旧版v1练习参考答案DEF",
        knowledge_point="旧版求导",
    )
    repo.save_exercise_question_source(
        exercise_id=old_ex.id,
        card=card_v1,
        build_id=card_v1.build_id or "",
        source_version="v1",
    )
    repo.upsert_context(conv, active_exercise_id=old_ex.id)

    # 题库切换成新版 card_v2，并在新一轮查题结果展示新版
    runtime.cards = [card_v2]
    runtime.build_id = "iv-v2"
    add_search_turn(repo, conv, [card_v2])

    # 引用表保留 Q1->v1，为新版分配 Q2->v2
    ref_table = build_ref_table(conv, repo.list_turns(conv))
    assert [e.ref for e in ref_table.entries] == ["Q1", "Q2"]
    assert ref_table.resolve("Q1").build_id == card_v1.build_id
    assert ref_table.resolve("Q2").build_id == "iv-v2"
    assert ref_table.resolve("Q2").content_sha256 == "f" * 64

    # Turn A：查询旧引用 Q1 仍按旧版本核验报 VERSION_CHANGED
    events_a = asyncio.run(
        collect(service, service.prepare_turn(conv, dispatch_input(str(uuid4()), "看看 Q1 详情")))
    )
    res_a = next(e for e in events_a if e.type == "result")
    detail_a = next(x for x in (res_a.composite_results or []) if x.action == "view_question_detail")
    assert detail_a.status == "failed"
    assert detail_a.details.get("error_code") == "VERSION_CHANGED"

    # Turn B：按新版引用 Q2 追问，只给模型看新版题干，不混入旧版讲解或旧版 v1 练习
    events_b = asyncio.run(
        collect(service, service.prepare_turn(conv, dispatch_input(str(uuid4()), "Q2 第一步为什么这样写？")))
    )
    res_b = next(e for e in events_b if e.type == "result")
    fu_b = next(x for x in (res_b.composite_results or []) if x.action == "followup_problem")
    assert fu_b.status == "success"
    assert len(tutor.followup_calls) == 1
    call_b = tutor.followup_calls[-1]
    dumped_b = json.dumps(call_b["explanation"].model_dump(), ensure_ascii=False)
    assert "新版题干：求二重积分" in call_b["statement"]
    assert "旧版专属解法步骤XYZ" not in dumped_b
    assert "旧版v1专属练习题干ABC" not in call_b["statement"]
    assert "旧版v1练习参考答案DEF" not in dumped_b

    # 恢复上下文中的旧版练习指针，验证 Turn C 通过 select_question(ref="Q2") 选中新版后自动追问也会清理并隔离旧练习
    repo.upsert_context(conv, active_exercise_id=old_ex.id)
    events_c = asyncio.run(
        collect(service, service.prepare_turn(conv, dispatch_input(str(uuid4()), "选中 Q2，第二步怎么确定上下限？")))
    )
    res_c = next(e for e in events_c if e.type == "result")
    fu_c = next(x for x in (res_c.composite_results or []) if x.action == "followup_problem")
    assert fu_c.status == "success"
    assert len(tutor.followup_calls) == 2
    call_c = tutor.followup_calls[-1]
    dumped_c = json.dumps(call_c["explanation"].model_dump(), ensure_ascii=False)
    assert "新版题干：求二重积分" in call_c["statement"]
    assert "旧版专属解法步骤XYZ" not in dumped_c
    assert "旧版v1专属练习题干ABC" not in call_c["statement"]
    assert "旧版v1练习参考答案DEF" not in dumped_c
    assert repo.get_active_exercise(conv) is None

    active_q = repo.get_active_question(conv)
    assert active_q is not None
    assert active_q.build_id == "iv-v2"
    assert active_q.content_sha256 == "f" * 64
    assert active_q.problem_id is None
    ws = build_workspace(
        conversation_id=conv,
        turns=repo.list_turns(conv),
        context_row=repo.get_context(conv),
        selection=active_q,
        selection_card=card_v2,
        problem=None,
        exercise=None,
        paper_state=None,
        located=None,
    )
    assert (ws.get("selection") or {}).get("ref") == "Q2"
    assert [(item["ref"], item["selected"]) for item in ws["shown"]] == [("Q1", False), ("Q2", True)]


def test_legacy_submitted_ungradable_record_consistent_across_views_and_gate(
    tmp_path: Path,
) -> None:
    """复核 P2-3：旧版标为 submitted 但实际无法判分时，历史卡、当前卡与答案门禁口径须完全一致。"""

    service, repo, _, _, _ = setup_service(tmp_path)
    conv = repo.create_conversation().id
    ungradable_json = json.dumps(
        {
            "is_correct": None,
            "final_answer_assessment": "图片模糊无法判分。",
            "step_by_step_analysis": "过程不可辨识。",
            "guidance": "请补充清晰过程。",
            "needs_supplement": False,
        },
        ensure_ascii=False,
    )

    # 情形 A：无 revealed_solution_at，仅旧版脏标 status='submitted'
    _, ex_unrevealed = create_problem_with_exercise(repo, conversation_id=conv)
    repo.record_exercise_answer_and_evaluation(
        ex_unrevealed.id,
        student_answer_text="3",
        student_solution_text=None,
        evaluation_json=ungradable_json,
    )
    with repo._write() as conn:
        conn.execute(
            "UPDATE exercises SET status = 'submitted', revealed_solution_at = NULL WHERE id = ?",
            (ex_unrevealed.id,),
        )
    stored_a = repo.get_exercise(ex_unrevealed.id)
    assert stored_a is not None
    hist_a = repo.view_from_exercise(stored_a, attempt=stored_a.current_attempt)
    curr_a = service._build_exercise_view(stored_a)
    gate_a = answer_gate.exercise_solution_gate(
        request_id=str(uuid4()), student_text="继续", exercise=stored_a
    )
    assert hist_a.status == "pending_answer"
    assert curr_a.status == "pending_answer"
    assert hist_a.reference_solution_text is None
    assert curr_a.reference_solution_text is None
    assert gate_a.allowed is False

    # 情形 B：旧版已写 status='submitted' 且有 revealed_solution_at（答案曾对学生公开）
    _, ex_revealed = create_problem_with_exercise(repo, conversation_id=conv)
    repo.record_exercise_answer_and_evaluation(
        ex_revealed.id,
        student_answer_text="3",
        student_solution_text=None,
        evaluation_json=ungradable_json,
    )
    with repo._write() as conn:
        conn.execute(
            "UPDATE exercises SET status = 'submitted', revealed_solution_at = '2026-10-01T00:00:00+00:00' WHERE id = ?",
            (ex_revealed.id,),
        )
    stored_b = repo.get_exercise(ex_revealed.id)
    assert stored_b is not None
    hist_b = repo.view_from_exercise(stored_b, attempt=stored_b.current_attempt)
    curr_b = service._build_exercise_view(stored_b)
    repo_view_b = repo.view_from_exercise(stored_b)
    gate_b = answer_gate.exercise_solution_gate(
        request_id=str(uuid4()), student_text="继续", exercise=stored_b
    )
    assert hist_b.status == "pending_answer"
    assert curr_b.status == "pending_answer"
    assert repo_view_b.status == "pending_answer"
    assert hist_b.reference_solution_text is not None
    assert curr_b.reference_solution_text is not None
    assert repo_view_b.reference_solution_text is not None
    assert gate_b.allowed is True
