"""F007 P4：v5 结构、选中事实、条件抽取与状态映射。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from app.conversations import schema
from app.conversations.models import TurnResultEvent
from app.conversations.repository import ConversationRepository, NewProblem
from app.tutoring.models import Source
from app.question_bank.models import (
    AnswerSummary,
    BuildInfo,
    CourseRef,
    PageRefs,
    PaperRef,
    QuestionSummary,
    ReviewSummary,
    SearchRequest,
    SearchResponse,
)
from app.questions.intent import candidate_number, extract_intent
from app.questions.models import QuestionFormula, QuestionOption
from app.questions.runtime import QuestionRuntime
from tests.question_fakes import BUILD_ID, SOURCE_VERSION, sample_card


def _repo(tmp_path: Path) -> ConversationRepository:
    repository = ConversationRepository(tmp_path / "study.sqlite3")
    repository.initialize()
    return repository


def test_v5_schema_creates_selection_tables_and_partial_index(
    study_db_path: Path,
) -> None:
    repository = ConversationRepository(study_db_path)
    repository.initialize()

    with sqlite3.connect(study_db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "question_selections",
            "exercise_question_sources",
            "record_question_sources",
        } <= tables
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='question_selections_one_active'"
        ).fetchone()[0]
        assert "WHERE active = 1" in index_sql


def test_v4_database_migrates_to_v5_without_touching_old_rows(
    study_db_path: Path,
) -> None:
    repository = ConversationRepository(study_db_path)
    repository.initialize()
    conversation = repository.create_conversation()

    with sqlite3.connect(study_db_path) as connection:
        connection.execute("DROP INDEX question_selections_one_active")
        connection.execute("DROP INDEX question_selections_conversation")
        connection.execute("DROP TABLE question_selections")
        connection.execute("DROP TABLE exercise_question_sources")
        connection.execute("DROP TABLE record_question_sources")
        connection.execute("PRAGMA user_version = 4")
        connection.commit()

    repository.initialize()

    with sqlite3.connect(study_db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute(
            "SELECT title FROM conversations WHERE id = ?", (conversation.id,)
        ).fetchone()[0]


def test_v5_migration_failure_rolls_back(
    study_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = ConversationRepository(study_db_path)
    repository.initialize()
    with sqlite3.connect(study_db_path) as connection:
        connection.execute("DROP INDEX question_selections_one_active")
        connection.execute("DROP INDEX question_selections_conversation")
        connection.execute("DROP TABLE question_selections")
        connection.execute("DROP TABLE exercise_question_sources")
        connection.execute("DROP TABLE record_question_sources")
        connection.execute("PRAGMA user_version = 4")
        connection.commit()

    monkeypatch.setattr(
        schema,
        "_v5_migration_statements",
        lambda: ("CREATE TABLE broken (",),
    )
    with pytest.raises(Exception):
        repository.initialize()

    with sqlite3.connect(study_db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "question_selections" not in names


def test_selection_is_idempotent_and_only_one_active(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    conversation = repository.create_conversation()
    card = sample_card()
    request_id = str(uuid4())

    first = repository.create_question_selection(
        conversation_id=conversation.id,
        request_id=request_id,
        source_request_id=request_id,
        card=card,
        build_id=BUILD_ID,
        source_version=SOURCE_VERSION,
    )
    again = repository.create_question_selection(
        conversation_id=conversation.id,
        request_id=request_id,
        source_request_id=request_id,
        card=card,
        build_id=BUILD_ID,
        source_version=SOURCE_VERSION,
    )
    assert first.id == again.id
    assert repository.get_active_question(conversation.id).id == first.id

    other = repository.create_question_selection(
        conversation_id=conversation.id,
        request_id=str(uuid4()),
        source_request_id=str(uuid4()),
        card=sample_card("py-2024-2025-s1-final-fill-2", number="2"),
        build_id=BUILD_ID,
        source_version=SOURCE_VERSION,
    )
    active = repository.get_active_question(conversation.id)
    assert active is not None and active.id == other.id
    assert repository.get_question_selection(
        conversation.id, card.question_id
    ).active is False


def test_reselecting_same_question_keeps_problem_and_reveal(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    conversation = repository.create_conversation()
    card = sample_card()
    request_id = str(uuid4())
    first = repository.create_question_selection(
        conversation_id=conversation.id,
        request_id=request_id,
        source_request_id=request_id,
        card=card,
        build_id=BUILD_ID,
        source_version=SOURCE_VERSION,
    )
    problem = repository.create_problem_version(
        conversation_id=conversation.id,
        problem=NewProblem(
            statement_text=card.statement,
            status="explained",
            clarity="clear",
            unclear_points=[],
            uncertainties=[],
            source=Source(),
            student_answer_text=None,
            solution_text=None,
            explanation=None,
        ),
    )
    repository.bind_question_problem(first.id, problem.id)
    revealed = repository.reveal_question_answer(
        conversation.id, card.question_id, source="explain", selection_id=first.id
    )
    assert revealed is not None

    # 同一道题在后续“看答案/再选一次”时复用原选中，不能把已绑定题目冲掉。
    again = repository.create_question_selection(
        conversation_id=conversation.id,
        request_id=str(uuid4()),
        source_request_id=str(uuid4()),
        card=card,
        build_id=BUILD_ID,
        source_version=SOURCE_VERSION,
    )
    assert again.id == first.id
    assert again.problem_id == problem.id
    assert again.answer_revealed_at == revealed.answer_revealed_at
    with sqlite3.connect(tmp_path / "study.sqlite3") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM question_selections WHERE conversation_id = ?",
            (conversation.id,),
        ).fetchone()[0]
    assert count == 1


def test_answer_reveal_records_first_fact_only(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    conversation = repository.create_conversation()
    card = sample_card()
    selection = repository.create_question_selection(
        conversation_id=conversation.id,
        request_id=str(uuid4()),
        source_request_id=str(uuid4()),
        card=card,
        build_id=BUILD_ID,
        source_version=SOURCE_VERSION,
    )
    assert selection.answer_revealed_at is None

    revealed = repository.reveal_question_answer(
        conversation.id, card.question_id, source="explain", selection_id=selection.id
    )
    assert revealed is not None
    first_time = revealed.answer_revealed_at
    assert first_time and revealed.answer_reveal_source == "explain"

    again = repository.reveal_question_answer(
        conversation.id, card.question_id, source="view", selection_id=selection.id
    )
    assert again is not None
    assert again.answer_revealed_at == first_time
    assert again.answer_reveal_source == "explain"


def test_exercise_and_record_sources_are_stored(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    conversation = repository.create_conversation()
    card = sample_card()
    problem = repository.create_problem_version(
        conversation_id=conversation.id,
        problem=__import__(
            "app.conversations.repository", fromlist=["NewProblem"]
        ).NewProblem(
            statement_text=card.statement,
            status="explained",
            clarity="clear",
            unclear_points=[],
            uncertainties=[],
            source=__import__(
                "app.tutoring.models", fromlist=["Source"]
            ).Source(type="none"),
            student_answer_text=None,
            solution_text=None,
            explanation=None,
        ),
        attachments=[],
    )
    exercise = repository.create_exercise(
        id=str(uuid4()),
        conversation_id=conversation.id,
        problem_id=problem.id,
        root_problem_id=problem.root_problem_id,
        statement_text="变式题",
        reference_solution_text="1",
        knowledge_point="循环",
    )
    repository.save_exercise_question_source(
        exercise_id=exercise.id,
        card=card,
        build_id=BUILD_ID,
        source_version=SOURCE_VERSION,
    )
    source = repository.get_exercise_question_source(exercise.id)
    assert source is not None
    assert source.question_id == card.question_id
    assert source.paper_title == card.source.paper_title
    assert repository.view_from_exercise(exercise).source_question == source


def test_intent_prefers_text_and_never_trusts_unmentioned_filters() -> None:
    intent = extract_intent(
        "帮我找一道 Python 期末选择题，直接讲解",
        confirmed_course="Python",
        planned={
            "question_type": "code",
            "exam_type": "midterm",
            "knowledge_points": ["循环", "函数"],
            "auto_explain": True,
        },
    )
    assert intent.course == "Python语言程序设计"
    assert intent.question_type == "choice"
    assert intent.exam_type == "final"
    assert intent.auto_explain is True
    assert intent.limit == 1
    # 只有原文确实提到的知识点才保留，避免模型偷偷扩大筛选。
    assert intent.knowledge_points == ("函数",) or intent.knowledge_points == ()


@pytest.mark.parametrize("text", [
    "找一道Python真题", "找一道Python真题讲讲", "来一道Python真题练练",
    "找一道Python真题，不要直接讲解", "找一道Python真题，先让我作答",
])
def test_lookup_never_accepts_model_auto_explain_without_explicit_request(text):
    intent = extract_intent(text, confirmed_course="Python", planned={"auto_explain": True})
    assert intent.auto_explain is False


@pytest.mark.parametrize("text", ["找一道Python真题，直接讲解", "找一道Python真题，直接看答案"])
def test_explicit_immediate_answer_remains_available(text):
    intent = extract_intent(text, confirmed_course="Python", planned={"auto_explain": True})
    assert intent.auto_explain is True


def test_correction_and_generated_exercise_are_not_real_question_lookup():
    from app.questions.intent import is_question_lookup
    assert not is_question_lookup("纠正题干，再出一道类似题")


def test_intent_uses_paper_item_id_and_never_silently_broadens() -> None:
    papers = [
        {
            "id": "py-2024-2025-s1-midterm",
            "academic_year": "2024-2025",
            "exam_type": "midterm",
        },
        {
            "id": "py-2024-2025-s1-final",
            "academic_year": "2024-2025",
            "exam_type": "final",
        },
        {
            "id": "py-2023-2024-s1-midterm",
            "academic_year": "2023-2024",
            "exam_type": "midterm",
        },
    ]
    intent = extract_intent(
        "找2024–2025学年第一学期期中Python选择题",
        confirmed_course="Python",
        papers=papers,
    )
    assert intent.paper_id == "py-2024-2025-s1-midterm"
    assert intent.paper_unresolved is False

    # 原文限制无法唯一落到一份卷子时澄清，不能静默取消试卷限制。
    ambiguous = extract_intent(
        "找2024–2025学年第一学期Python题",
        confirmed_course="Python",
        papers=papers,
    )
    assert ambiguous.paper_id is None
    assert ambiguous.paper_unresolved is True

    # 模型建议的试卷若与原文限制冲突，也不能照单全收。
    conflict = extract_intent(
        "找2024–2025学年第一学期期中Python选择题",
        confirmed_course="Python",
        planned={"paper_id": "py-2024-2025-s1-final"},
        papers=papers,
    )
    assert conflict.paper_id is None
    assert conflict.paper_unresolved is True


def test_compose_statement_keeps_options_code_formulas_and_blanks() -> None:
    from app.questions.cards import compose_statement

    card = sample_card().model_copy(
        update={
            "options": [
                QuestionOption(label="A", text="1"),
                QuestionOption(label="B", text="2"),
            ],
            "code_lines": ["for i in range(3):", "    print(i)"],
            "formulas": [QuestionFormula(text="x = y + 1", page=2)],
            "blank_marks": [1, 2],
        }
    )
    statement = compose_statement(card)
    assert card.statement in statement
    assert "A. 1" in statement and "B. 2" in statement
    assert "for i in range(3):" in statement
    assert "    print(i)" in statement
    assert "公式：x = y + 1（第 2 页）" in statement
    assert "空位：1、2" in statement


def test_candidate_number_supports_chinese() -> None:
    assert candidate_number("讲一下第 2 题") == 2
    assert candidate_number("第三题呢") == 3
    assert candidate_number("没有编号") is None


class _StubBank:
    build_id = BUILD_ID
    source_version = SOURCE_VERSION
    approved_ids = frozenset()

    def __init__(self, *responses: SearchResponse) -> None:
        self._responses = list(responses)

    def capability(self, course: str):
        from app.question_bank.models import CourseCapability

        return CourseCapability(
            course=course,
            known=True,
            status="unreviewed",
            question_count=1154,
            searchable_count=1152,
            approved_count=0,
        )

    def search(self, request):
        return self._responses.pop(0)

    def close(self) -> None:
        pass


def _response(status: str, items: list[QuestionSummary] | None = None) -> SearchResponse:
    return SearchResponse(
        status=status,
        mode="keyword_only",
        course=CourseRef(input="Python", canonical="Python语言程序设计", known=True),
        build=BuildInfo(
            build_id=BUILD_ID,
            source_version=SOURCE_VERSION,
            index_version="test",
        ),
        items=items or [],
    )


def _summary() -> QuestionSummary:
    card = sample_card()
    return QuestionSummary(
        question_id=card.question_id,
        paper=PaperRef(
            paper_id=card.source.paper_id,
            title=card.source.paper_title or "",
            course="Python语言程序设计",
            exam_type="final",
            academic_year="2024-2025",
        ),
        question_type="choice",
        number_label="1",
        statement=card.statement,
        content_sha256=card.content_sha256,
        pages=PageRefs(statement=[1], answer=[2]),
        answer=AnswerSummary(state="original", available=True),
        review=ReviewSummary(
            status="approved", visual="ok", approved=True, stale=False
        ),
    )


def test_runtime_ignores_unknown_model_knowledge_point_and_retries(
    study_db_path: Path,
) -> None:
    from app.question_bank.errors import QuestionBankRequestError

    class _RejectingBank(_StubBank):
        def __init__(self) -> None:
            super().__init__()
            self.requests: list[SearchRequest] = []

        def search(self, request):
            self.requests.append(request)
            if request.knowledge_points:
                raise QuestionBankRequestError(
                    "knowledge_points", "未知知识点：完全数"
                )
            return _response("ok", [_summary()])

    bank = _RejectingBank()
    runtime = QuestionRuntime(bank, bank_root=study_db_path.parent)
    result = runtime.search(
        SearchRequest(
            course="Python语言程序设计",
            query="完全数编程题",
            knowledge_points=["完全数"],
        ),
        conversation_id="c",
    )
    assert result.status == "ok"
    assert len(result.cards) == 1
    assert len(bank.requests) == 2
    assert bank.requests[0].knowledge_points == ["完全数"]
    assert bank.requests[1].knowledge_points == []


def test_runtime_maps_unreviewed_and_ok_states(study_db_path: Path) -> None:
    request = SearchRequest(course="Python语言程序设计", query="循环")
    unreviewed = QuestionRuntime(
        _StubBank(_response("no_match")), bank_root=study_db_path.parent
    ).search(request, conversation_id="c")
    assert unreviewed.status == "unreviewed"
    assert unreviewed.cards == []
    assert "审核" in unreviewed.message

    ok = QuestionRuntime(
        _StubBank(_response("ok", [_summary()])), bank_root=study_db_path.parent
    ).search(request, conversation_id="c", request_id="r")
    assert ok.status == "ok"
    assert len(ok.cards) == 1
    assert ok.cards[0].source_request_id == "r"
    assert ok.cards[0].answer_url


def test_runtime_unavailable_never_raises(study_db_path: Path) -> None:
    runtime = QuestionRuntime(None, error="no bank", bank_root=study_db_path.parent)
    result = runtime.search(
        SearchRequest(course="Python语言程序设计", query="循环"),
        conversation_id="c",
    )
    assert result.status == "bank_unavailable"
    assert runtime.capability("Python语言程序设计").status == "error"
    assert runtime.card_for_selection is not None


def test_turn_result_accepts_question_payload() -> None:
    card = sample_card()
    event = TurnResultEvent(
        conversation_id="c",
        request_id="r",
        kind="select_question",
        question=card,
        question_result_status="ok",
        question_results=[card],
    )
    assert event.business_status == "succeeded"
    dumped = event.model_dump(mode="json")
    assert dumped["question"]["question_id"] == card.question_id
    assert dumped["question_results"][0]["answer_detail"] is None
    assert "answer_detail" not in json.dumps(
        card.public_snapshot(), ensure_ascii=False
    )
