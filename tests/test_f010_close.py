"""F010 收尾：模型猜题型不能代替用户，真实讲解须可恢复。"""

import json

import pytest

from app.conversations.final_reply import explanation_sha256
from app.conversations.graph import ActionPlan, ScheduleDecision
from tests.test_f008_ux_repair import (
    PAPER, build_service, mixed_type_cards, result_of, run, seed_page, turn_input,
)
from tests.test_f010b_support import make_harness
from tests.test_f010c_recovery import run as run_harness
from tests.tool_fakes import ReplyFactory
from tests.tutoring_fakes import FakeScheduler, FakeTutor, sample_explanation


@pytest.mark.anyio
@pytest.mark.parametrize("last_type", ["choice", "fill"])
@pytest.mark.parametrize("entry", ["number", "ref"])
async def test_model_guessed_type_or_ref_cannot_resolve_ambiguity(tmp_path, last_type, entry):
    cards = mixed_type_cards()
    parameters = {"number_label": "5", "question_type": last_type, "paper_id": PAPER}
    if entry == "ref":
        parameters = {"ref": "Q5", "question_type": last_type}
    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
        ActionPlan(action="select_question", parameters=parameters),
        ActionPlan(action="explain_problem", parameters={}),
    ])])
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, repo, _ = build_service(tmp_path, cards=cards, scheduler=scheduler, tutor=tutor)
    conversation = service.create_conversation().id
    seed_page(repo, conversation, cards=[c for c in cards if c.question_type == last_type],
              question_type=last_type, returned=5, user_message="列出选择题和填空题")

    result = result_of(await run(service, conversation, turn_input(text="第五题给我讲讲")))

    assert result.business_status == "needs_input"
    assert "题型" in result.assistant_message
    assert repo.get_active_question(conversation) is None
    assert repo.get_active_problem(conversation) is None
    assert tutor.explain_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("with_type", [True, False])
async def test_student_single_type_history_allows_number_selection(tmp_path, with_type):
    parameters = {"number_label": "5"}
    if with_type:
        parameters["question_type"] = "choice"
    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[ActionPlan(
        action="select_question", parameters=parameters,
    )])])
    cards = mixed_type_cards()
    service, repo, _ = build_service(tmp_path, cards=cards, scheduler=scheduler)
    conversation = service.create_conversation().id
    seed_page(repo, conversation, cards=[c for c in cards if c.question_type == "choice"],
              returned=5, user_message="只列这份卷的选择题")
    result = result_of(await run(service, conversation, turn_input(text="我选第五题")))
    if with_type:
        assert result.business_status == "succeeded"
        assert repo.get_active_question(conversation).question_id == f"{PAPER}-choice-5"
    else:
        assert result.business_status == "needs_input"
        assert repo.get_active_question(conversation) is None


@pytest.mark.anyio
async def test_student_explicit_ref_remains_selectable(tmp_path):
    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
        ActionPlan(action="select_question", parameters={"ref": "Q5"}),
    ])])
    cards = mixed_type_cards()
    service, repo, _ = build_service(tmp_path, cards=cards, scheduler=scheduler)
    conversation = service.create_conversation().id
    seed_page(repo, conversation, cards=[c for c in cards if c.question_type == "fill"],
              question_type="fill", returned=5)
    result = result_of(await run(service, conversation, turn_input(text="选 Q5")))
    assert result.business_status == "succeeded"
    assert repo.get_active_question(conversation).question_id == f"{PAPER}-fill-5"


@pytest.mark.anyio
async def test_real_explanation_result_persists_recoverable_fingerprint(tmp_path):
    harness = make_harness(tmp_path)
    conversation = harness.service.create_conversation().id
    harness.scheduler.push(ScheduleDecision(actions=[ActionPlan(
        action="explain_problem", parameters={"target": "new", "course": "Python语言程序设计"},
    )]))
    prepared, events = await run_harness(harness, conversation, "讲这道题：求1到100的和")
    result = result_of(events)
    assert result.tutoring.kind == "explanation"
    assert not hasattr(result.tutoring.problem, "explanation")
    record = harness.repo.get_problem(result.tutoring.problem.id)
    turn = harness.repo.find_turn(conversation, prepared.request_id)
    ref = next(x for x in turn.recovery["reply_context"]["authorized"] if x["kind"] == "explanation")
    assert ref["explanation_sha256"] == explanation_sha256(record.explanation)
    assert ref["explanation_sha256"] is not None

    reply = ReplyFactory(["重新讲解这道题。"])
    harness.service._make_reply = reply
    regenerated = await harness.service.regenerate_reply(conversation, prepared.request_id)
    prompt = json.dumps(reply.models[-1].calls, ensure_ascii=False, default=str)
    assert record.explanation.final_answer in prompt
    assert "答案版本已变化" not in regenerated["assistant_message"]
    assert regenerated["reply_status"] == "complete"
