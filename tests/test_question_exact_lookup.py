"""Whole-paper titles are locators, and question numbers are exact filters."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.conversations.graph import ActionPlan
from app.conversations.service import ConversationService, _DynamicContext
from app.question_bank.models import SearchRequest
from app.question_bank.service import QuestionBankService
from app.questions.intent import extract_intent
from app.questions.runtime import QuestionRuntime

TITLE = "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）"
PAPER = "py-2024-2025-s1-final"
PAPERS = [{"id": PAPER, "academic_year": "2024-2025", "exam_type": "final"}]


def intent(text, **planned):
    return extract_intent(text, confirmed_course="Python", papers=PAPERS, planned=planned)


def request(value):
    return SearchRequest(course=value.course, query=value.query, paper_id=value.paper_id,
                         question_type=value.question_type, exam_type=value.exam_type,
                         number_label=value.number_label, limit=value.limit)


def test_screenshot_returns_typed_first_questions_instead_of_topic_query():
    value = intent(TITLE + "的大题第一题", query=TITLE + "大题第一题", question_type="大题", limit=1)
    assert value.paper_id == PAPER and value.number_label == "1"
    assert value.query == "" and value.question_type is None and value.limit == 3


@pytest.mark.parametrize(("text", "kind", "number"), [
    (TITLE + "选择题第1题", "choice", "1"),
    (TITLE + "选择题第四十题", "choice", "40"),
    (TITLE + "编程题第一题", "code", "1"),
    (TITLE + "填空题第01小题", "fill", "1"),
])
def test_course_title_is_not_a_question_type(text, kind, number):
    value = intent(text)
    assert value.question_type == kind and value.number_label == number
    assert value.query == "" and not value.paper_unresolved


def test_explicit_topic_is_not_removed_by_exact_locator():
    value = intent(TITLE + "选择题第1题 量子计算 量子比特")
    assert value.query == "量子计算 量子比特"


def test_question_number_without_unique_paper_requires_clarification():
    value = intent("找Python编程题第1题")
    assert value.paper_unresolved and value.paper_id is None


def test_planner_cannot_invent_a_question_number():
    value = intent(TITLE + "选择题", number_label="40")
    assert value.number_label is None


@pytest.fixture(scope="module")
def bank():
    if not Path("data/local/question-bank/db/current.json").is_file():
        pytest.skip("本机无真实题库")
    with QuestionBankService() as service:
        yield service


def test_real_paper_first_program_and_last_choice(bank):
    for text, qid in [(TITLE + "编程题第一题", PAPER + "-code-01"),
                      (TITLE + "选择题第四十题", PAPER + "-choice-40")]:
        result = bank.search(request(intent(text)))
        assert result.status == "ok" and result.mode == "browse"
        assert [q.question_id for q in result.items] == [qid]
        assert result.preview is False and result.items[0].review.approved


def test_real_screenshot_offers_each_type_first_question(bank):
    result = bank.search(request(intent(TITLE + "的大题第一题")))
    assert {q.question_id for q in result.items} == {PAPER + "-" + k + "-01" for k in ("choice", "fill", "code")}


def test_out_of_range_does_not_fall_back_to_other_number(bank):
    result = bank.search(request(intent(TITLE + "编程题第99题")))
    assert result.status == "no_match" and result.items == []


def test_exact_lookup_preserves_approval_gate(bank, monkeypatch):
    monkeypatch.setattr(bank, "_approved", set())
    result = bank.search(request(intent(TITLE + "编程题第1题")))
    assert result.status == "no_match" and result.items == []


def test_conversation_passes_number_and_explains_type_ambiguity(bank):
    runtime = QuestionRuntime(bank)
    service = object.__new__(ConversationService)
    service.questions = runtime
    service._search_intent = lambda text, ctx, action: intent(text, **action.parameters)
    service._action_result = lambda prepared, ctx, outcome: outcome
    ctx = _DynamicContext(conversation_id="isolated-exact-lookup", confirmed_course="Python")
    prepared = SimpleNamespace(input=SimpleNamespace(text=TITLE + "的大题第一题"),
                               conversation_id=ctx.conversation_id, request_id="exact-lookup")
    action = ActionPlan(action="search_questions", parameters={"query": TITLE + "大题第一题"})
    outcome = asyncio.run(service._dynamic_search_questions(prepared, action, ctx, lambda *_: None))
    assert outcome.status == "success"
    assert len(ctx.question_results) == 3
    assert "请按题型选择" in outcome.message
    assert "选择题" in ctx.question_result_message and "编程题" in ctx.question_result_message
