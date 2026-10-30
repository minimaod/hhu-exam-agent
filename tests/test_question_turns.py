"""F007 P4：真题轮次、动态调度与答案边界。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.querying.errors import QueryError
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.tutoring.models import Source
from tests.question_fakes import FakeQuestionRuntime, sample_card
from tests.tutoring_fakes import FakeScheduler, FakeTutor, sample_explanation

SENTINEL = "SENTINEL-ANSWER-TEXT-DO-NOT-PERSIST"


@pytest.mark.anyio
async def test_choice_submission_targets_historical_card_and_survives_reload(tmp_path: Path) -> None:
    old = sample_card(statement="历史选择题")
    other = sample_card("other-code-1", statement="当前编程题").model_copy(update={"question_type": "code", "options": []})
    tutor = FakeTutor(explanations=[sample_explanation("程序"), sample_explanation("B")])
    service, repository, _ = build_service(tmp_path, tutor=tutor, runtime=FakeQuestionRuntime(cards=[old, other]))
    conversation = service.create_conversation()
    source = add_search_turn(repository, conversation.id, [old, other])
    await run(service, conversation.id, turn_input(kind="explain_question", question_id=other.question_id, source_turn=source))
    data = replace(turn_input(kind="explain_question", question_id=old.question_id, source_turn=source,
                              text="这道题我选 B，请核对并讲解。"), student_answer_text="B")
    events = await run(service, conversation.id, data)
    result = next(event for event in events if event.type == "result")
    assert result.question.question_id == old.question_id
    assert tutor.explain_calls[-1]["student_answer"] == "B"
    assert "历史选择题" in tutor.explain_calls[-1]["statement"]
    assert "当前编程题" not in tutor.explain_calls[-1]["statement"]
    assert result.tutoring.problem.student_answer_text == "B"
    assert service.get_detail(conversation.id).active_problem.student_answer_text == "B"
    assert repository.get_active_question(conversation.id).question_id == old.question_id
    # 同一个请求重放不会再次调用模型或创建作答版本。
    await run(service, conversation.id, data)
    assert len(tutor.explain_calls) == 2


@pytest.mark.parametrize("answer", ["C", "Z", "B.另一个题的选项"])
def test_invalid_choice_never_creates_turn_or_calls_model(tmp_path: Path, answer: str) -> None:
    from app.conversations.errors import TurnInvalidError
    tutor = FakeTutor(explanations=[])
    service, repository, _ = build_service(tmp_path, tutor=tutor)
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])
    data = replace(turn_input(kind="explain_question", question_id=card.question_id, source_turn=source),
                   student_answer_text=answer)
    with pytest.raises(TurnInvalidError, match="实际提供的选项"):
        service.prepare_turn(conversation.id, data)
    assert repository.find_turn(conversation.id, data.request_id) is None
    assert repository.get_active_question(conversation.id) is None
    assert not tutor.explain_calls


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["generate_exercise", "record_mistake", "followup_problem"])
async def test_natural_action_never_falls_back_to_old_problem(tmp_path: Path, action: str) -> None:
    old = sample_card(statement="旧题")
    new = sample_card("py-new-choice-2", number="2", statement="新题")
    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[ActionPlan(action=action)])])
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, repository, _ = build_service(
        tmp_path, runtime=FakeQuestionRuntime(cards=[old, new]), scheduler=scheduler, tutor=tutor
    )
    conversation = service.create_conversation()
    source = add_search_turn(repository, conversation.id, [old, new])
    await run(service, conversation.id, turn_input(kind="explain_question", question_id=old.question_id, source_turn=source))
    await run(service, conversation.id, turn_input(kind="select_question", question_id=new.question_id, source_turn=source))
    events = await run(service, conversation.id, turn_input(kind=None, text="对这道题继续操作", dispatch=True))
    result = next(event for event in events if event.type == "result")
    assert result.business_status == "needs_input"
    assert result.exercise is None
    assert len(tutor.explain_calls) == 1
    assert not tutor.followup_calls
    assert repository.get_active_question(conversation.id).question_id == new.question_id


@pytest.mark.anyio
async def test_natural_generation_uses_reselected_bound_question(tmp_path: Path) -> None:
    from tests.tutoring_fakes import FakeExerciseAgent
    old = sample_card(statement="旧题")
    new = sample_card("py-new-choice-2", number="2", statement="新题")
    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[ActionPlan(action="generate_exercise")])])
    service, repository, _ = build_service(
        tmp_path, runtime=FakeQuestionRuntime(cards=[old, new]), scheduler=scheduler,
        tutor=FakeTutor(explanations=[sample_explanation("B"), sample_explanation("B")]),
    )
    agent = FakeExerciseAgent()
    service._make_exercise_agent = lambda: agent
    conversation = service.create_conversation()
    source = add_search_turn(repository, conversation.id, [old, new])
    for card in (old, new):
        await run(service, conversation.id, turn_input(kind="explain_question", question_id=card.question_id, source_turn=source))
    await run(service, conversation.id, turn_input(kind="select_question", question_id=old.question_id, source_turn=source))
    events = await run(service, conversation.id, turn_input(kind=None, text="为这道题生成变式", dispatch=True))
    result = next(event for event in events if event.type == "result")
    assert result.exercise.source_question.question_id == old.question_id
    assert "旧题" in agent.generate_calls[0]["original_statement"]
    assert "新题" not in agent.generate_calls[0]["original_statement"]


@pytest.mark.anyio
async def test_old_version_answer_and_asset_remain_readable(tmp_path: Path) -> None:
    old = sample_card(statement="原版本")
    runtime = FakeQuestionRuntime(cards=[old])
    service, repository, _ = build_service(tmp_path, runtime=runtime)
    conversation = service.create_conversation()
    source = add_search_turn(repository, conversation.id, [old])
    await run(service, conversation.id, turn_input(kind="view_question_answer", question_id=old.question_id, source_turn=source))
    new = old.model_copy(update={"build_id": "iv-ffffffffffffffff", "content_sha256": "b" * 64, "statement": "新版本"})
    runtime.cards = [new]
    runtime.build_id = new.build_id
    source2 = add_search_turn(repository, conversation.id, [new])
    await run(service, conversation.id, turn_input(kind="select_question", question_id=new.question_id, source_turn=source2))
    answer = service.question_answer(conversation.id, old.question_id, old.build_id)
    assert answer.build_id == old.build_id
    assert answer.statement == "原版本"
    assert answer.answer_detail is not None
    assert service.question_asset(conversation.id, old.question_id, "asset-1", old.build_id)
    with pytest.raises(Exception):
        service.question_answer(conversation.id, new.question_id, new.build_id)


class NeverDisconnected:
    async def is_disconnected(self) -> bool:
        return False


def build_service(
    tmp_path: Path,
    *,
    runtime: FakeQuestionRuntime | None = None,
    scheduler=None,
    tutor: FakeTutor | None = None,
) -> tuple[ConversationService, ConversationRepository, FakeQuestionRuntime]:
    repository = ConversationRepository(tmp_path / "study.sqlite3")
    repository.initialize()
    fake_runtime = runtime or FakeQuestionRuntime(answer_text=SENTINEL)
    service = ConversationService(
        repository=repository,
        make_query=None,
        query_error="模型未配置",
        make_tutor=(lambda: tutor) if tutor is not None else None,
        tutoring_error=None if tutor is not None else "模型未配置",
        make_scheduler=(lambda: scheduler) if scheduler is not None else None,
        question_runtime=fake_runtime,
        attachments_root=tmp_path / "attachments",
    )
    return service, repository, fake_runtime


def add_search_turn(
    repository: ConversationRepository, conversation_id: str, cards
) -> str:
    request_id = str(uuid4())
    repository.start_turn(
        conversation_id, request_id, "找一道 Python 真题", kind="dynamic"
    )
    repository.complete_dynamic_turn(
        conversation_id,
        request_id,
        kind="dynamic",
        assistant_message="找到题目",
        question_results=list(cards),
        question_result_status="ok",
    )
    return request_id


def turn_input(
    *,
    kind: str,
    question_id: str | None = None,
    source_turn: str | None = None,
    text: str = "",
    question_action: str | None = None,
    dispatch: bool = False,
) -> TurnInput:
    return TurnInput(
        request_id=str(uuid4()),
        kind=kind,
        text=text,
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(type="none"),
        problem_id=None,
        start_new=False,
        dispatch=dispatch,
        question_id=question_id,
        question_source_turn=source_turn,
        question_action=question_action,
    )


async def run(service: ConversationService, conversation_id: str, data: TurnInput):
    prepared = service.prepare_turn(conversation_id, data)
    events = [event async for event in service.stream_prepared(prepared, NeverDisconnected())]
    return events


@pytest.mark.anyio
async def test_select_question_creates_active_selection_without_answer(
    tmp_path: Path,
) -> None:
    service, repository, runtime = build_service(tmp_path)
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])

    events = await run(
        service,
        conversation.id,
        turn_input(
            kind="select_question", question_id=card.question_id, source_turn=source
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.kind == "select_question"
    assert result.question is not None
    assert result.question.question_id == card.question_id
    assert result.question.selection_id
    assert result.question.answer_detail is None
    assert result.question.answer_revealed is False
    assert result.question_result_status == "ok"

    active = repository.get_active_question(conversation.id)
    assert active is not None and active.question_id == card.question_id
    assert active.answer_revealed_at is None

    detail = json.dumps(
        service.get_detail(conversation.id).model_dump(mode="json"), ensure_ascii=False
    )
    assert SENTINEL not in detail
    assert SENTINEL.encode() not in (tmp_path / "study.sqlite3").read_bytes()
    assert '"answer_detail": {' not in detail


@pytest.mark.anyio
async def test_select_rejects_unapproved_or_foreign_candidate(tmp_path: Path) -> None:
    service, repository, runtime = build_service(tmp_path)
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])

    with pytest.raises(Exception) as excinfo:
        service.prepare_turn(
            conversation.id,
            turn_input(
                kind="select_question",
                question_id="py-2024-2025-s1-final-choice-9",
                source_turn=source,
            ),
        )
    assert "题目" in str(excinfo.value)

    # 来源轮次不属于本会话。
    other = service.create_conversation()
    with pytest.raises(Exception):
        service.prepare_turn(
            other.id,
            turn_input(
                kind="select_question", question_id=card.question_id, source_turn=source
            ),
        )


@pytest.mark.anyio
async def test_explain_question_uses_real_material_and_reveals_answer(
    tmp_path: Path,
) -> None:
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, repository, runtime = build_service(tmp_path, tutor=tutor)
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])

    await run(
        service,
        conversation.id,
        turn_input(
            kind="select_question", question_id=card.question_id, source_turn=source
        ),
    )

    events = await run(
        service,
        conversation.id,
        turn_input(
            kind="explain_question", question_id=card.question_id, source_turn=source
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.kind == "explain_question"
    assert result.tutoring is not None
    assert result.tutoring.kind == "explanation"
    assert result.question is not None
    assert result.question.answer_revealed is True
    assert result.question.answer_detail is not None
    assert result.question.answer_detail.text == SENTINEL
    active_problem = repository.get_active_problem(conversation.id)
    assert active_problem is not None
    problem_id = active_problem.id

    assert runtime.material_calls == [card.question_id]
    assert tutor.explain_calls
    call = tutor.explain_calls[0]
    assert "【真题材料】" in call["statement"]
    assert card.statement in call["statement"]

    active = repository.get_active_question(conversation.id)
    assert active is not None
    assert active.answer_revealed_at is not None
    assert active.answer_reveal_source == "explain"
    assert active.problem_id == problem_id

    detail = service.get_detail(conversation.id)
    assert detail.active_question is not None
    assert detail.active_question.answer_revealed is True
    assert detail.active_question.answer_detail is not None
    assert detail.active_question.answer_detail.text == SENTINEL

    # 答案正文不落学习库；只有详情接口对已开放对象按需读取。
    assert SENTINEL.encode() not in (tmp_path / "study.sqlite3").read_bytes()


@pytest.mark.anyio
async def test_view_answer_reveals_candidate_and_stays_out_of_study_db(
    tmp_path: Path,
) -> None:
    service, repository, runtime = build_service(tmp_path)
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])

    events = await run(
        service,
        conversation.id,
        turn_input(
            kind="view_question_answer",
            question_id=card.question_id,
            source_turn=source,
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.kind == "view_question_answer"
    assert result.question is not None
    assert result.question.answer_revealed is True
    assert result.question.answer_detail is not None
    assert result.question.answer_detail.text == SENTINEL

    active = repository.get_active_question(conversation.id)
    assert active is not None
    assert active.answer_reveal_source == "view"
    assert active.answer_revealed_at is not None

    answer = service.question_answer(
        conversation.id, card.question_id, active.build_id
    )
    assert answer.answer_detail is not None
    assert answer.answer_detail.text == SENTINEL
    assert SENTINEL.encode() not in (tmp_path / "study.sqlite3").read_bytes()


@pytest.mark.anyio
async def test_dynamic_search_and_auto_explain_binds_source(tmp_path: Path) -> None:
    scheduler = FakeScheduler(
        decisions=[
            ScheduleDecision(
                actions=[
                    ActionPlan(
                        action="search_questions",
                        parameters={
                            "query": "Python 期末 循环",
                            "question_type": "choice",
                            "auto_explain": True,
                        },
                    ),
                    ActionPlan(
                        action="explain_problem",
                        parameters={},
                    ),
                ]
            )
        ]
    )
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, repository, runtime = build_service(
        tmp_path, scheduler=scheduler, tutor=tutor
    )
    conversation = service.create_conversation()

    events = await run(
        service,
        conversation.id,
        TurnInput(
            request_id=str(uuid4()),
            kind=None,
            text="找一道 Python 期末选择题，直接讲解",
            attachments=(),
            student_answer_text=None,
            solution_text=None,
            source=Source(type="none"),
            problem_id=None,
            start_new=False,
            dispatch=True,
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.kind == "dynamic"
    assert len(result.question_results) == 1
    assert result.question is not None
    assert result.question.question_id == sample_card().question_id
    assert result.tutoring is not None
    assert result.business_status == "succeeded"

    assert runtime.search_calls
    request = runtime.search_calls[0]["request"]
    assert request.preview_candidates is False
    assert request.limit <= 5
    assert "\u771f\u9898" in scheduler.calls[0]["question_context"]

    active = repository.get_active_question(conversation.id)
    assert active is not None and active.problem_id is not None
    assert active.answer_reveal_source == "explain"


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["找一道Python真题"])
@pytest.mark.parametrize("extra_action", ["explain_problem", "view_question_answer"])
async def test_model_cannot_disclose_answer_without_student_request(tmp_path, text, extra_action):
    """学生只是找题，模型自作主张的讲解/看答案仍被拦下。"""

    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
        ActionPlan(action="search_questions", parameters={"auto_explain": True}),
        ActionPlan(action=extra_action, parameters={}),
    ])])
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, repository, _ = build_service(tmp_path, scheduler=scheduler, tutor=tutor)
    conversation = service.create_conversation()
    events = await run(service, conversation.id, turn_input(kind=None, text=text, dispatch=True))
    result = next(event for event in events if event.type == "result")
    assert result.question_results
    # 没有公开答案，也没有把答案写进卡片。
    assert all(not c.answer_revealed and c.answer_detail is None for c in result.question_results)
    assert result.question is None and result.tutoring is None
    assert tutor.explain_calls == []
    if extra_action == "view_question_answer":
        assert repository.get_active_question(conversation.id) is None
    # 模型排队的讲解/看答案没有变成成功动作；至多留下一条等待学生的说明。
    queued = [item for item in result.composite_results if item.action == extra_action]
    assert all(item.status != "success" for item in queued)


@pytest.mark.anyio
async def test_explain_request_runs_without_forced_student_choice(tmp_path):
    """“找一道真题讲讲”可以查题后直接讲解，不再强制学生先选题或作答。"""

    scheduler = FakeScheduler(decisions=[
        ScheduleDecision(actions=[
            ActionPlan(action="search_questions", parameters={"auto_explain": True}),
        ]),
        ScheduleDecision(actions=[ActionPlan(action="explain_problem", parameters={})]),
    ])
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, repository, _ = build_service(tmp_path, scheduler=scheduler, tutor=tutor)
    conversation = service.create_conversation()
    events = await run(
        service, conversation.id, turn_input(kind=None, text="找一道Python真题讲讲", dispatch=True)
    )
    result = next(event for event in events if event.type == "result")
    assert result.question is not None
    assert result.tutoring is not None
    assert tutor.explain_calls
    assert not any(
        item.action == "ask_user" for item in result.composite_results
    )


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["explain_problem", "view_question_answer"])
async def test_lookup_cannot_skip_search_and_disclose_answer(tmp_path, action):
    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[ActionPlan(action=action, parameters={})])])
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, repository, _ = build_service(tmp_path, scheduler=scheduler, tutor=tutor)
    conversation = service.create_conversation()
    events = await run(service, conversation.id, turn_input(kind=None, text="找一道Python真题讲讲", dispatch=True))
    result = next(event for event in events if event.type == "result")
    assert result.tutoring is None and result.question is None
    assert tutor.explain_calls == []
    assert repository.get_active_question(conversation.id) is None


@pytest.mark.anyio
async def test_new_lookup_does_not_echo_previous_revealed_selection(tmp_path):
    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
        ActionPlan(action="search_questions", parameters={"auto_explain": True}),
        ActionPlan(action="explain_problem", parameters={}),
    ])])
    # 两道题：换一道要能给出没看过的那一道，而不是回显旧题。
    runtime = FakeQuestionRuntime(
        cards=[sample_card(), sample_card("py-2024-2025-s1-final-choice-2", number="2")]
    )
    service, repository, _ = build_service(tmp_path, runtime=runtime, scheduler=scheduler)
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])
    await run(service, conversation.id, turn_input(kind="select_question", question_id=card.question_id, source_turn=source))
    await run(service, conversation.id, turn_input(kind="view_question_answer", question_id=card.question_id, source_turn=source))
    before = repository.get_active_question(conversation.id)
    assert before.answer_revealed_at
    # 换一道题但没说要讲：不能借旧题目的已公开答案回显。
    events = await run(service, conversation.id, turn_input(kind=None, text="换一道Python真题", dispatch=True))
    result = next(event for event in events if event.type == "result")
    assert result.question is None and result.tutoring is None
    assert result.question_results and not result.question_results[0].answer_revealed
    assert repository.get_active_question(conversation.id).answer_revealed_at == before.answer_revealed_at


@pytest.mark.anyio
async def test_dynamic_search_unreviewed_is_a_clear_clarification(
    tmp_path: Path,
) -> None:
    scheduler = FakeScheduler(
        decisions=[
            ScheduleDecision(
                actions=[ActionPlan(action="search_questions", parameters={})]
            )
        ]
    )
    runtime = FakeQuestionRuntime(
        search_status="unreviewed",
        search_message="Python 题库尚未审核，暂不能按题号选题。",
    )
    service, repository, _ = build_service(tmp_path, runtime=runtime, scheduler=scheduler)
    conversation = service.create_conversation()
    events = await run(
        service,
        conversation.id,
        TurnInput(
            request_id=str(uuid4()),
            kind=None,
            text="找一道 Python 期末真题",
            attachments=(),
            student_answer_text=None,
            solution_text=None,
            source=Source(type="none"),
            problem_id=None,
            start_new=False,
            dispatch=True,
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.question_results == []
    assert repository.get_active_question(conversation.id) is None
    assert "审核" in (result.assistant_message or "")


@pytest.mark.anyio
async def test_dynamic_search_bank_unavailable_is_retryable(tmp_path: Path) -> None:
    scheduler = FakeScheduler(
        decisions=[
            ScheduleDecision(
                actions=[ActionPlan(action="search_questions", parameters={})]
            )
        ]
    )
    runtime = FakeQuestionRuntime(
        search_status="bank_unavailable",
        search_message="题目库暂时不可用，请稍后重试。",
    )
    service, repository, _ = build_service(tmp_path, runtime=runtime, scheduler=scheduler)
    conversation = service.create_conversation()
    events = await run(
        service,
        conversation.id,
        TurnInput(
            request_id=str(uuid4()),
            kind=None,
            text="找一道 Python 期末真题",
            attachments=(),
            student_answer_text=None,
            solution_text=None,
            source=Source(type="none"),
            problem_id=None,
            start_new=False,
            dispatch=True,
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.business_status in ("failed", "partial")
    assert result.retryable is True
    assert "不可用" in (result.assistant_message or "")


@pytest.mark.anyio
async def test_followup_uses_real_options_after_explain(tmp_path: Path) -> None:
    from app.tutoring.models import FollowupAnswer

    scheduler = FakeScheduler(
        decisions=[
            ScheduleDecision(
                actions=[ActionPlan(action="followup_problem", parameters={})]
            )
        ]
    )
    tutor = FakeTutor(
        explanations=[sample_explanation("B")],
        followups=[FollowupAnswer(answer="因为选项 B 符合题意。")],
    )
    service, repository, runtime = build_service(
        tmp_path, scheduler=scheduler, tutor=tutor
    )
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])
    await run(
        service,
        conversation.id,
        turn_input(
            kind="select_question", question_id=card.question_id, source_turn=source
        ),
    )
    await run(
        service,
        conversation.id,
        turn_input(
            kind="explain_question", question_id=card.question_id, source_turn=source
        ),
    )
    events = await run(
        service,
        conversation.id,
        TurnInput(
            request_id=str(uuid4()),
            kind=None,
            text="为什么选 B？",
            attachments=(),
            student_answer_text=None,
            solution_text=None,
            source=Source(type="none"),
            problem_id=None,
            start_new=False,
            dispatch=True,
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.tutoring is not None
    assert result.tutoring.kind == "followup_answer"
    assert tutor.followup_calls
    statement = tutor.followup_calls[0]["statement"]
    assert card.statement in statement
    assert "A. 1" in statement and "B. 2" in statement


@pytest.mark.anyio
async def test_exercise_from_real_question_binds_source_and_dedupes_original(
    tmp_path: Path,
) -> None:
    from tests.tutoring_fakes import FakeExerciseAgent
    from app.tutoring.exercise_llm import GeneratedExercise

    agent = FakeExerciseAgent(
        exercises=[
            GeneratedExercise(
                statement="编写程序输出 1 到 5 的平方。",
                reference_solution="for i in range(1, 6): print(i*i)",
                knowledge_point="循环",
            )
        ]
    )
    scheduler = FakeScheduler(
        decisions=[
            ScheduleDecision(actions=[ActionPlan(action="generate_exercise")])
        ]
    )
    service, repository, runtime = build_service(
        tmp_path,
        scheduler=scheduler,
        tutor=FakeTutor(explanations=[sample_explanation("B")]),
    )
    service._make_exercise_agent = lambda: agent
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])
    await run(
        service,
        conversation.id,
        turn_input(
            kind="select_question", question_id=card.question_id, source_turn=source
        ),
    )
    events = await run(
        service,
        conversation.id,
        turn_input(
            kind="explain_question", question_id=card.question_id, source_turn=source
        ),
    )
    result = next(event for event in events if event.type == "result")
    active_problem = repository.get_active_problem(conversation.id)
    assert active_problem is not None
    problem_id = active_problem.id

    exercise_events = await run(
        service,
        conversation.id,
        TurnInput(
            request_id=str(uuid4()),
            kind=None,
            text="来一道变式",
            attachments=(),
            student_answer_text=None,
            solution_text=None,
            source=Source(type="none"),
            problem_id=None,
            start_new=False,
            dispatch=True,
        ),
    )
    exercise_result = next(
        event for event in exercise_events if event.type == "result"
    )
    assert exercise_result.exercise is not None
    source_view = exercise_result.exercise.source_question
    assert source_view is not None
    assert source_view.question_id == card.question_id
    assert source_view.paper_title == card.source.paper_title
    original = agent.generate_calls[0]["original_statement"]
    assert card.statement in original
    assert "A. 1" in original
    assert "B. 2" in original


@pytest.mark.anyio
async def test_history_keeps_original_build_and_content_after_new_version(
    tmp_path: Path,
) -> None:
    old_card = sample_card(statement="旧版本题干")
    runtime = FakeQuestionRuntime(cards=[old_card])
    service, repository, _ = build_service(
        tmp_path,
        runtime=runtime,
        tutor=FakeTutor(
            explanations=[sample_explanation("B"), sample_explanation("B")]
        ),
    )
    conversation = service.create_conversation()
    old_source = add_search_turn(repository, conversation.id, [old_card])
    await run(
        service,
        conversation.id,
        turn_input(
            kind="explain_question",
            question_id=old_card.question_id,
            source_turn=old_source,
        ),
    )
    old_selection = repository.get_question_selection(
        conversation.id, old_card.question_id
    )
    assert old_selection is not None

    new_card = old_card.model_copy(
        update={
            "content_sha256": "b" * 64,
            "statement": "新版本题干",
            "source_request_id": None,
        }
    )
    runtime.cards = [new_card]
    new_source = add_search_turn(repository, conversation.id, [new_card])
    await run(
        service,
        conversation.id,
        turn_input(
            kind="explain_question",
            question_id=new_card.question_id,
            source_turn=new_source,
        ),
    )

    # 模拟旧版本 P4 轮次没有 selection_id，只剩 build + 内容哈希。
    with sqlite3.connect(tmp_path / "study.sqlite3") as connection:
        row = connection.execute(
            "SELECT request_id, output_json FROM conversation_turns "
            "WHERE conversation_id = ? AND kind = 'explain_question' "
            "ORDER BY rowid ASC LIMIT 1",
            (conversation.id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[1])
        payload["question"]["selection_id"] = None
        connection.execute(
            "UPDATE conversation_turns SET output_json = ? "
            "WHERE conversation_id = ? AND request_id = ?",
            (json.dumps(payload), conversation.id, row[0]),
        )
        connection.commit()

    detail = service.get_detail(conversation.id)
    old_turn = next(
        turn
        for turn in detail.turns
        if turn.kind == "explain_question"
        and turn.question is not None
        and turn.question.statement == "旧版本题干"
    )
    assert old_turn.question is not None
    assert old_turn.question.selection_id == old_selection.id
    assert old_turn.question.content_sha256 == old_card.content_sha256
    new_turns = [
        turn
        for turn in detail.turns
        if turn.kind == "explain_question"
        and turn.question is not None
        and turn.question.statement == "新版本题干"
    ]
    assert len(new_turns) == 1
    assert new_turns[0].question is not None
    assert new_turns[0].question.content_sha256 == new_card.content_sha256


@pytest.mark.anyio
async def test_card_exercise_targets_clicked_card_not_current_problem(
    tmp_path: Path,
) -> None:
    from tests.tutoring_fakes import FakeExerciseAgent
    from app.tutoring.exercise_llm import GeneratedExercise

    agent = FakeExerciseAgent(
        exercises=[
            GeneratedExercise(
                statement="旧题变式",
                reference_solution="print('old')",
                knowledge_point="循环",
            )
        ]
    )
    old_card = sample_card(
        "py-2024-2025-s1-final-choice-1",
        number="1",
        statement="旧题：输出 1。",
    )
    new_card = sample_card(
        "py-2024-2025-s1-final-choice-2",
        number="2",
        statement="新题：输出 2。",
    )
    service, repository, _ = build_service(
        tmp_path,
        runtime=FakeQuestionRuntime(cards=[old_card, new_card]),
        tutor=FakeTutor(
            explanations=[sample_explanation("B"), sample_explanation("B")]
        ),
    )
    service._make_exercise_agent = lambda: agent
    conversation = service.create_conversation()
    source = add_search_turn(repository, conversation.id, [old_card, new_card])
    for card in (old_card, new_card):
        await run(
            service,
            conversation.id,
            turn_input(
                kind="explain_question",
                question_id=card.question_id,
                source_turn=source,
            ),
        )
    current = repository.get_active_problem(conversation.id)
    assert current is not None and new_card.statement in current.statement_text
    old_selection = repository.get_question_selection(
        conversation.id, old_card.question_id
    )
    assert old_selection is not None and old_selection.problem_id is not None

    events = await run(
        service,
        conversation.id,
        turn_input(
            kind=None,
            text="",
            dispatch=True,
            question_id=old_card.question_id,
            source_turn=source,
            question_action="exercise",
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.exercise is not None
    assert result.exercise.source_question is not None
    assert result.exercise.source_question.question_id == old_card.question_id
    assert result.exercise.problem_id == old_selection.problem_id
    assert old_card.statement in agent.generate_calls[0]["original_statement"]
    assert new_card.statement not in agent.generate_calls[0]["original_statement"]


@pytest.mark.anyio
async def test_card_record_targets_clicked_card_not_current_problem(
    tmp_path: Path, sample_root: Path
) -> None:
    from app.mistakes.service import MistakeRecordService
    from app.papers.repository import PaperRepository
    from app.papers.service import PaperService

    old_card = sample_card(
        "py-2024-2025-s1-final-choice-1",
        number="1",
        statement="旧题：输出 1。",
    )
    new_card = sample_card(
        "py-2024-2025-s1-final-choice-2",
        number="2",
        statement="新题：输出 2。",
    )
    service, repository, _ = build_service(
        tmp_path,
        runtime=FakeQuestionRuntime(cards=[old_card, new_card]),
        tutor=FakeTutor(
            explanations=[sample_explanation("B"), sample_explanation("B")]
        ),
    )
    papers = PaperService(PaperRepository(sample_root))
    service._records = MistakeRecordService(
        repository,
        papers,
        make_tutor=service._make_tutor,
        tutoring_error=None,
        attachments_root=tmp_path / "attachments",
    )
    conversation = service.create_conversation()
    source = add_search_turn(repository, conversation.id, [old_card, new_card])
    for card in (old_card, new_card):
        await run(
            service,
            conversation.id,
            turn_input(
                kind="explain_question",
                question_id=card.question_id,
                source_turn=source,
            ),
        )
    old_selection = repository.get_question_selection(
        conversation.id, old_card.question_id
    )
    assert old_selection is not None and old_selection.problem_id is not None

    events = await run(
        service,
        conversation.id,
        turn_input(
            kind=None,
            text="",
            dispatch=True,
            question_id=old_card.question_id,
            source_turn=source,
            question_action="record",
        ),
    )
    result = next(event for event in events if event.type == "result")
    assert result.tutoring is not None
    record = repository.get_record(
        result.tutoring.record.id  # type: ignore[union-attr]
    )
    assert record is not None
    assert record.problem_id == old_selection.problem_id
    source_view = repository.get_record_question_source(record.id)
    assert source_view is not None
    assert source_view.question_id == old_card.question_id


@pytest.mark.anyio
async def test_view_after_explain_keeps_bound_problem_and_reveal(
    tmp_path: Path,
) -> None:
    service, repository, _ = build_service(
        tmp_path, tutor=FakeTutor(explanations=[sample_explanation("B")])
    )
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])
    await run(
        service,
        conversation.id,
        turn_input(
            kind="select_question", question_id=card.question_id, source_turn=source
        ),
    )
    await run(
        service,
        conversation.id,
        turn_input(
            kind="explain_question", question_id=card.question_id, source_turn=source
        ),
    )
    before = repository.get_active_question(conversation.id)
    assert before is not None and before.problem_id is not None
    assert before.answer_reveal_source == "explain"
    reveal_time = before.answer_revealed_at

    await run(
        service,
        conversation.id,
        turn_input(
            kind="view_question_answer",
            question_id=card.question_id,
            source_turn=source,
        ),
    )
    after = repository.get_active_question(conversation.id)
    assert after is not None
    assert after.id == before.id
    assert after.problem_id == before.problem_id
    # 首次开放答案的事实不因再次查看而改写。
    assert after.answer_revealed_at == reveal_time
    assert after.answer_reveal_source == "explain"
    with repository._read() as connection:  # noqa: SLF001 - 测试只读计数
        count = connection.execute(
            "SELECT COUNT(*) FROM question_selections WHERE conversation_id = ?",
            (conversation.id,),
        ).fetchone()[0]
    assert count == 1


@pytest.mark.anyio
async def test_question_explain_failure_can_retry_without_losing_selection(
    tmp_path: Path,
) -> None:
    service, repository, _ = build_service(
        tmp_path,
        tutor=FakeTutor(
            explanations=[QueryError("MODEL_TIMEOUT", "模型响应超时，请重试。")]
        ),
    )
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])
    select_events = await run(
        service,
        conversation.id,
        turn_input(
            kind="select_question", question_id=card.question_id, source_turn=source
        ),
    )
    select_result = next(event for event in select_events if event.type == "result")
    selection_id = select_result.question.selection_id
    assert selection_id

    explain_request = str(uuid4())
    failed_events = await run(
        service,
        conversation.id,
        TurnInput(
            request_id=explain_request,
            kind="explain_question",
            text="",
            attachments=(),
            student_answer_text=None,
            solution_text=None,
            source=Source(type="none"),
            problem_id=None,
            start_new=False,
            question_id=card.question_id,
            question_source_turn=source,
        ),
    )
    failed = next(event for event in failed_events if event.type == "error")
    assert failed.code in ("MODEL_TIMEOUT", "TUTORING_FAILED")
    failed_turn = repository.find_turn(conversation.id, explain_request)
    assert failed_turn is not None and failed_turn.retryable is True
    # 失败不能把选中项或答案开放状态提前写坏。
    active = repository.get_active_question(conversation.id)
    assert active is not None and active.id == selection_id
    assert active.answer_revealed_at is None

    service._make_tutor = lambda: FakeTutor(explanations=[sample_explanation("B")])
    retry_events = await run(
        service,
        conversation.id,
        TurnInput(
            request_id=str(uuid4()),
            kind=None,
            text="",
            attachments=(),
            student_answer_text=None,
            solution_text=None,
            source=Source(type="none"),
            problem_id=None,
            start_new=False,
            retry_of=explain_request,
        ),
    )
    retry_result = next(event for event in retry_events if event.type == "result")
    assert retry_result.kind == "explain_question"
    assert retry_result.tutoring is not None
    assert retry_result.question is not None and retry_result.question.answer_revealed
    active_after = repository.get_active_question(conversation.id)
    assert active_after is not None
    assert active_after.id == selection_id
    assert active_after.problem_id is not None
    with repository._read() as connection:  # noqa: SLF001 - 测试只读计数
        count = connection.execute(
            "SELECT COUNT(*) FROM question_selections WHERE conversation_id = ?",
            (conversation.id,),
        ).fetchone()[0]
    assert count == 1
