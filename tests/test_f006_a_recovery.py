"""A 批：作答全链路、真实业务结果与只恢复失败步骤。全部使用隔离库。"""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.conversations.errors import ConversationConflictError, ConversationStorageError, ProblemNotFoundError
from app.conversations.graph import ActionPlan
from app.querying.errors import QueryError
from app.tutoring.exercise_llm import DeepSeekExerciseAgent
from tests.test_f006_review_fixes import (
    answered_input, collect, create_problem_with_exercise, decision, dispatch_input,
    install_http, remove_http_override, setup_service,
)
from tests.tutoring_fakes import clear_understanding, sample_explanation


def run(service, conversation_id, data):
    return asyncio.run(collect(service, service.prepare_turn(conversation_id, data)))


def retry_input(origin):
    return replace(dispatch_input(str(uuid4()), ""), retry_of=origin)


@pytest.mark.parametrize("answer", [None, "[10,20]"])
def test_exercise_process_through_http_model_storage_detail_and_replay(tmp_path, answer):
    service, repo, _, agent, _ = setup_service(tmp_path)
    conversation = repo.create_conversation()
    _, exercise = create_problem_with_exercise(repo, conversation_id=conversation.id)
    req = {"request_id": str(uuid4()), "kind": "exercise", "exercise_id": exercise.id,
           "solution_text": "别名引用同一列表，+= 原地修改。"}
    if answer: req["student_answer"] = answer
    client = install_http(service)
    try:
        response = client.post(f"/api/conversations/{conversation.id}/turns", json=req)
        assert response.status_code == 200, response.text
        assert agent.evaluate_calls[0]["student_solution_text"] == req["solution_text"]
        assert agent.evaluate_calls[0]["student_answer"] == answer
        saved = repo.get_exercise(exercise.id)
        assert saved.student_answer_text == answer
        assert saved.student_solution_text == req["solution_text"]
        detail = client.get(f"/api/conversations/{conversation.id}").json()
        assert detail["turns"][-1]["exercise"]["student_solution_text"] == req["solution_text"]
        assert detail["turns"][-1]["business_status"] == "succeeded"
        replay = client.post(f"/api/conversations/{conversation.id}/turns", json=req)
        assert replay.status_code == 200
        assert "student_solution_text" in replay.text
        assert len(agent.evaluate_calls) == 1
        repo.initialize()
        assert service.get_detail(conversation.id).turns[-1].exercise.student_solution_text == req["solution_text"]
    finally:
        remove_http_override()


def test_real_adapter_receives_separate_solution_field(tmp_path):
    class Model:
        messages = None
        async def ainvoke(self, messages):
            self.messages = messages
            class Response:
                content = json.dumps({"is_correct": None, "final_answer_assessment": "仅过程",
                                      "step_by_step_analysis": "别名推理正确", "guidance": "补答案", "needs_supplement": True})
            return Response()
    model = Model()
    asyncio.run(DeepSeekExerciseAgent(model=model).evaluate_answer(
        statement="列表题", reference_solution="[10,20]", student_answer=None, student_solution_text="独立过程证据"))
    prompt = str(model.messages)
    assert "独立过程证据" in prompt and "未单独写最终答案" in prompt


def test_dynamic_all_failure_retry_restores_original_target_and_does_not_replan(tmp_path):
    service, repo, scheduler, agent, _ = setup_service(tmp_path, scheduler_decisions=[
        decision(ActionPlan(action="generate_exercise"))], exercises=[QueryError("MODEL_TIMEOUT", "模型响应超时，请重试。")])
    conversation = repo.create_conversation()
    problem, _ = create_problem_with_exercise(repo, conversation_id=conversation.id)
    origin = dispatch_input(str(uuid4()), "再来一道类似题")
    events = run(service, conversation.id, origin)
    result = events[-1]
    assert result.business_status == "failed" and result.retryable
    assert "。。" not in result.assistant_message
    assert service.get_detail(conversation.id).turns[-1].business_status == "failed"
    # 当前目标已经变化；重试仍绑定原题快照。
    repo.create_problem(conversation_id=conversation.id, statement_text="另一道题", explanation=sample_explanation())
    before = len(scheduler.calls)
    retried = retry_input(origin.request_id)
    second = run(service, conversation.id, retried)[-1]
    assert second.business_status == "succeeded"
    assert second.exercise.problem_id == problem.id
    assert len(scheduler.calls) == before
    count = len(agent.generate_calls)
    assert run(service, conversation.id, retried)[-1].exercise.id == second.exercise.id
    assert len(agent.generate_calls) == count
    assert not repo.find_turn(conversation.id, origin.request_id).retryable
    assert not service.get_detail(conversation.id).turns[0].retryable
    assert not run(service, conversation.id, origin)[-1].retryable
    with pytest.raises(ConversationConflictError):
        service.prepare_turn(conversation.id, retry_input(origin.request_id))


def test_partial_success_keeps_record_and_only_retries_generation(tmp_path):
    service, repo, scheduler, agent, _ = setup_service(tmp_path, scheduler_decisions=[decision(
        ActionPlan(action="record_mistake"), ActionPlan(action="generate_exercise"))],
        exercises=[QueryError("MODEL_TIMEOUT", "超时。")])
    conversation = repo.create_conversation()
    create_problem_with_exercise(repo, conversation_id=conversation.id)
    origin = dispatch_input(str(uuid4()), "保存这题，再出一道类似题")
    result = run(service, conversation.id, origin)[-1]
    assert result.business_status == "partial" and result.retryable
    calls = []
    original = service.records.create_from_problem
    def track(**kwargs):
        calls.append(kwargs); return original(**kwargs)
    service.records.create_from_problem = track
    replay = run(service, conversation.id, retry_input(origin.request_id))[-1]
    assert replay.business_status == "succeeded"
    assert not calls
    assert len([it for it in replay.composite_results if it.action == "record_mistake"]) == 1
    assert len(agent.generate_calls) == 2


@pytest.mark.parametrize("dynamic", [False, True])
def test_failed_evaluation_retry_keeps_answer_process_images(tmp_path, dynamic, monkeypatch):
    from tests.tutoring_fakes import png_bytes
    from app import images
    monkeypatch.setattr(images, "ATTACHMENTS_ROOT", tmp_path / "attachments")
    service, repo, _, agent, _ = setup_service(tmp_path, scheduler_decisions=[decision(ActionPlan(action="evaluate_exercise"))],
                                             evaluations=[QueryError("MODEL_TIMEOUT", "超时。")])
    conversation = repo.create_conversation()
    _, exercise = create_problem_with_exercise(repo, conversation_id=conversation.id)
    # 真实上传路由负责图片落盘与角色关联。
    client = install_http(service, repo)
    try:
        upload = client.post(f"/api/conversations/{conversation.id}/attachments", files={"file": ("steps.png", png_bytes(), "image/png")})
        assert upload.status_code == 201, upload.text
        aid = upload.json()["id"]
        request_id = str(uuid4())
        req = {"request_id": request_id, "kind": "auto" if dynamic else "exercise", "query": "我的答案是 1" if dynamic else "1",
               "attachments": [{"id": aid, "role": "solution"}]}
        if not dynamic: req.update(exercise_id=exercise.id, student_answer="1", solution_text="我的原过程")
        response = client.post(f"/api/conversations/{conversation.id}/turns", json=req)
        assert response.status_code == 200, response.text
        failed = service.get_detail(conversation.id).turns[-1]
        assert failed.business_status == "failed" and failed.retryable
        saved = repo.get_exercise(exercise.id)
        assert saved.student_answer_text
        restored = client.post(f"/api/conversations/{conversation.id}/turns", json={"request_id": str(uuid4()), "retry_of": request_id})
        assert restored.status_code == 200, restored.text
        assert len(agent.evaluate_calls) == 2
        assert len(agent.evaluate_calls[-1]["images"]) == 1
        assert agent.evaluate_calls[0]["student_answer"] == agent.evaluate_calls[1]["student_answer"]
        assert agent.evaluate_calls[0]["student_solution_text"] == agent.evaluate_calls[1]["student_solution_text"]
        assert service.get_detail(conversation.id).turns[-1].business_status == "succeeded"
    finally:
        remove_http_override()


def test_business_rejection_is_not_retryable_and_cross_conversation_retry_is_rejected(tmp_path):
    service, repo, _, _, _ = setup_service(tmp_path, scheduler_decisions=[decision(ActionPlan(action="evaluate_exercise"))])
    conversation = repo.create_conversation()
    _, exercise = create_problem_with_exercise(repo, conversation_id=conversation.id)
    first = answered_input(str(uuid4()), "1", exercise_id=exercise.id)
    run(service, conversation.id, first)
    bad = dispatch_input(str(uuid4()), "再交一次")
    result = run(service, conversation.id, bad)[-1]
    assert result.business_status == "failed" and not result.retryable
    assert "可以重试" not in result.assistant_message
    with pytest.raises(ConversationConflictError): service.prepare_turn(conversation.id, retry_input(bad.request_id))
    other = repo.create_conversation()
    with pytest.raises(ProblemNotFoundError): service.prepare_turn(other.id, retry_input(bad.request_id))


def test_needs_supplement_has_distinct_business_state(tmp_path):
    from app.tutoring.exercise_llm import AnswerEvaluation
    service, repo, _, _, _ = setup_service(tmp_path, evaluations=[AnswerEvaluation(
        is_correct=None, final_answer_assessment="不足", step_by_step_analysis="需步骤", guidance="请补充", needs_supplement=True)])
    conversation = repo.create_conversation()
    _, exercise = create_problem_with_exercise(repo, conversation_id=conversation.id)
    result = run(service, conversation.id, answered_input(str(uuid4()), "1", exercise_id=exercise.id))[-1]
    assert result.business_status == "needs_input" and not result.retryable


def test_effect_and_checkpoint_rollback_together(tmp_path, monkeypatch):
    service, repo, _, agent, _ = setup_service(tmp_path, scheduler_decisions=[decision(ActionPlan(action="generate_exercise"))])
    conversation = repo.create_conversation()
    _, old = create_problem_with_exercise(repo, conversation_id=conversation.id)
    save = repo.save_recovery
    def fail_after_effect(cid, rid, recovery):
        if recovery.get("actions") and recovery["actions"][-1].get("outcome", {}).get("status") == "success":
            raise ConversationStorageError("检查点保存失败")
        save(cid, rid, recovery)
    monkeypatch.setattr(repo, "save_recovery", fail_after_effect)
    origin = dispatch_input(str(uuid4()), "再来一道")
    run(service, conversation.id, origin)
    assert repo.get_active_exercise(conversation.id).id == old.id
    monkeypatch.setattr(repo, "save_recovery", save)
    # 重启恢复未完成轮次；不自动执行模型。
    repo.initialize()
    result = run(service, conversation.id, retry_input(origin.request_id))[-1]
    assert result.business_status == "succeeded"
    assert len(agent.generate_calls) == 2


def test_saved_checkpoint_survives_late_failure_and_restart(tmp_path, monkeypatch):
    service, repo, _, agent, _ = setup_service(tmp_path, scheduler_decisions=[decision(
        ActionPlan(action="record_mistake"), ActionPlan(action="generate_exercise"))])
    conversation = repo.create_conversation()
    create_problem_with_exercise(repo, conversation_id=conversation.id)
    complete = repo.complete_dynamic_turn
    monkeypatch.setattr(repo, "complete_dynamic_turn", lambda **kwargs: (_ for _ in ()).throw(ConversationStorageError("最终保存失败")))
    origin = dispatch_input(str(uuid4()), "保存再练习")
    run(service, conversation.id, origin)
    generated_id = repo.get_active_exercise(conversation.id).id
    repo.initialize()
    failed = service.get_detail(conversation.id).turns[-1]
    assert len(failed.recovery["actions"]) == 2
    # 所有副作用均已成功时不提供重复执行入口，结果仍可按目标读取。
    assert not failed.retryable
    assert failed.business_status == "succeeded"
    assert generated_id
    assert len(agent.generate_calls) == 1
    assert failed.exercise.id == generated_id
    assert failed.tutoring is not None


def test_retry_runs_original_dependency_after_explanation_recovers(tmp_path):
    service, repo, scheduler, agent, tutor = setup_service(tmp_path, scheduler_decisions=[decision(
        ActionPlan(action="explain_problem", parameters={"mode": "new"}),
        ActionPlan(action="generate_exercise"))],
        understandings=[QueryError("MODEL_TIMEOUT", "理解超时。"), clear_understanding()],
        explanations=[sample_explanation()])
    conversation = repo.create_conversation()
    origin = dispatch_input(str(uuid4()), "讲解这题然后出类似题")
    first = run(service, conversation.id, origin)[-1]
    assert first.business_status == "failed" and first.retryable
    assert not agent.generate_calls
    assert repo.find_turn(conversation.id, origin.request_id).recovery["pending"][0]["action"] == "generate_exercise"
    recovered = run(service, conversation.id, retry_input(origin.request_id))[-1]
    assert recovered.business_status == "succeeded"
    assert recovered.exercise is not None
    assert len(agent.generate_calls) == 1


def test_explicit_evaluation_and_turn_commit_rollback_together(tmp_path, monkeypatch):
    service, repo, _, agent, _ = setup_service(tmp_path)
    conversation = repo.create_conversation()
    _, exercise = create_problem_with_exercise(repo, conversation_id=conversation.id)
    original = repo.complete_dynamic_turn
    monkeypatch.setattr(repo, "complete_dynamic_turn", lambda **kwargs: (_ for _ in ()).throw(ConversationStorageError("保存失败")))
    origin = answered_input(str(uuid4()), "1", exercise_id=exercise.id, solution_text="原过程")
    run(service, conversation.id, origin)
    assert repo.get_exercise(exercise.id).evaluation_json is None
    assert repo.get_exercise(exercise.id).student_solution_text == "原过程"
    monkeypatch.setattr(repo, "complete_dynamic_turn", original)
    retry = run(service, conversation.id, retry_input(origin.request_id))[-1]
    assert retry.business_status == "succeeded"
    assert agent.evaluate_calls[-1]["student_solution_text"] == "原过程"


def test_existing_intent_router_failure_retries_original_dispatch(tmp_path):
    from app.conversations.dispatch import IntentDecision
    service, repo, _, _, _ = setup_service(tmp_path)
    class Router:
        calls = 0
        async def __call__(self, text, **kwargs):
            self.calls += 1
            assert text == "这题怎么做"
            if self.calls == 1: raise QueryError("MODEL_TIMEOUT", "调度超时。")
            return IntentDecision(intent="ambiguous", reason="请用户明确目标")
    router = Router()
    service._make_scheduler = None
    service._make_router = lambda: router
    conversation = repo.create_conversation()
    origin = dispatch_input(str(uuid4()), "这题怎么做")
    assert run(service, conversation.id, origin)[-1].type == "error"
    result = run(service, conversation.id, retry_input(origin.request_id))[-1]
    assert result.business_status == "needs_input"
    assert router.calls == 2
