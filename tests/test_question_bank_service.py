"""P3 查题服务契约：课程能力、公开结果不泄露答案、无匹配与未建库。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.question_bank.models import SearchRequest
from app.question_bank.paths import BankPaths, read_current, write_current
from app.question_bank.service import QuestionBankService


@pytest.fixture(scope="module")
def service():
    if read_current(BankPaths()) is None:
        pytest.skip("P3 索引尚未构建")
    with QuestionBankService() as instance:
        yield instance


def test_capability_distinguishes_ready_catalog_only_unknown(service: QuestionBankService) -> None:
    python = service.capability("Python")
    assert python.status == ("ready" if service.approved_count else "unreviewed")
    assert python.question_count == 1154
    assert python.searchable_count == 1152
    assert python.by_type == {"choice": 790, "fill": 324, "code": 40}
    assert python.answer_coverage == {"original": 1096, "missing": 58}
    assert python.approved_count == len(service.approved_ids)
    assert python.verified_label_count >= service.repository.verified_label_count()
    assert python.paper_count == 20

    math = service.capability("高等数学")
    assert math.status == "catalog_only"
    assert math.paper_count and math.paper_count > 0
    assert math.searchable_count == 0

    unknown = service.capability("星际航行")
    assert unknown.status == "unknown"
    assert unknown.known is False


def test_other_course_is_bank_not_built_not_no_match(service: QuestionBankService) -> None:
    response = service.search(SearchRequest(course="高等数学", query="极限"))
    assert response.status == "bank_not_built"
    assert response.returned == 0
    assert "尚未建立题目库" in (response.message or "")
    assert response.course.known is True


def test_public_summary_does_not_leak_answer_or_reference_code(service: QuestionBankService) -> None:
    response = service.search(SearchRequest(course="Python", query="完全数", limit=1, preview_candidates=True))
    assert response.status == "ok"
    payload = response.items[0].model_dump()
    assert "answer_detail" not in payload
    assert "reference_code" not in payload
    assert payload["answer"]["state"] == "original"
    assert payload["answer"]["available"] is True

    detail = service.detail("py-2020-2021-s1-final-code-01")
    assert detail is not None
    assert detail.answer_detail is None
    assert detail.reference_code is None
    assert detail.independent_solutions == []
    assert detail.code_blocks == []

    with_answer = service.detail("py-2020-2021-s1-final-code-01", include_answer=True)
    assert with_answer is not None
    assert with_answer.answer_detail is not None
    assert with_answer.answer_detail.text == "[参考代码]"
    assert with_answer.reference_code is None

    full = service.detail(
        "py-2020-2021-s1-final-code-01",
        include_answer=True,
        include_reference_code=True,
        include_code_blocks=True,
        include_annotations=True,
    )
    assert full is not None
    assert full.reference_code and "def fun" in full.reference_code
    assert full.code_blocks


def test_no_match_is_distinct_from_unavailable(service: QuestionBankService) -> None:
    response = service.search(SearchRequest(course="Python", query="量子计算 量子比特", preview_candidates=True))
    assert response.status == "no_match"
    assert response.returned == 0
    assert response.diagnostics.gate["gate_applied"] is True
    assert response.diagnostics.gate["best_vector_cosine"] < response.diagnostics.gate["vector_threshold"]


def test_default_search_hides_unreviewed_until_explicit_preview(
    service: QuestionBankService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 本用例隔离审核状态，不能把开发者真实题库永远固定成“0条批准”。
    monkeypatch.setattr(service, "_approved", set())
    monkeypatch.setattr(service, "_reviews", {})
    hidden = service.search(SearchRequest(course="Python", query="完全数"))
    assert hidden.status == "no_match"
    assert hidden.returned == 0
    assert hidden.preview is False
    assert "尚未审核" in (hidden.message or "")

    preview = service.search(SearchRequest(course="Python", query="完全数", preview_candidates=True))
    assert preview.status == "ok"
    assert preview.returned > 0
    assert preview.preview is True
    assert all(item.review.status == "candidate" for item in preview.items)


def test_verified_labels_count_union_not_build_plus_reviews() -> None:
    import sqlite3
    from types import SimpleNamespace

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE question_knowledge_points(question_id,kp_slug,status)")
    connection.execute("INSERT INTO question_knowledge_points VALUES('q1','math','verified')")
    instance = object.__new__(QuestionBankService)
    instance._repository = SimpleNamespace(
        conn=connection, all_knowledge_points=lambda: [{"slug": "math"}, {"slug": "functions"}],
    )
    instance._content_hashes = {"q1": "current", "q2": "current"}
    instance._reviews = {
        "q1": {"content_sha256": "current", "labels_verified": ["math", "math", "functions", "unknown"]},
        "q2": {"content_sha256": "stale", "labels_verified": ["math"]},
    }
    try:
        assert instance._verified_label_count() == 2
    finally:
        connection.close()


def test_approved_scope_returns_only_approved_questions(service: QuestionBankService) -> None:
    approved_id = "py-2020-2021-s1-final-code-01"
    previous = set(service._approved)
    service._approved = {approved_id}
    try:
        response = service.search(SearchRequest(course="Python", query="完全数"))
        assert response.status == "ok"
        assert [item.question_id for item in response.items] == [approved_id]
    finally:
        service._approved = previous


def test_browse_returns_filtered_page_without_query(service: QuestionBankService) -> None:
    response = service.search(SearchRequest(course="Python", question_type="fill", limit=3, preview_candidates=True))
    assert response.mode == "browse"
    assert response.status == "ok"
    assert len(response.items) == 3
    assert all(item.question_type == "fill" for item in response.items)
    assert response.matched_total and response.matched_total >= 324


def test_detail_unknown_returns_none(service: QuestionBankService) -> None:
    assert service.detail("py-2099-2099-s1-final-choice-99") is None


def test_both_indexes_missing_is_unavailable_not_no_match(service: QuestionBankService) -> None:
    bm25, vector = service._bm25, service._vector
    service._bm25 = None
    service._vector = None
    try:
        response = service.search(SearchRequest(course="Python", query="完全数", preview_candidates=True))
        assert response.status == "bank_unavailable"
        assert response.returned == 0
        assert "均不可用" in (response.message or "")
    finally:
        service._bm25, service._vector = bm25, vector


def test_missing_bank_is_reported_as_not_built(tmp_path: Path) -> None:
    root = tmp_path / "empty-bank"
    with QuestionBankService(root) as service:
        status = service.status()
        assert status.available is False
        response = service.search(SearchRequest(course="Python", query="字典"))
        assert response.status == "bank_not_built"
        capability = service.capability("Python")
        assert capability.status == "error"
        assert "尚未建立" in (capability.reason or "")


def test_dangling_current_pointer_is_bank_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "broken-bank"
    paths = BankPaths(root)
    paths.db_dir.mkdir(parents=True)
    write_current(
        paths,
        build_id="iv-0000000000000000",
        source_version="sv-0000000000000000",
        index_version="iv-0000000000000000",
        previous_build_id=None,
        activated_at="2026-01-01T00:00:00+08:00",
    )
    with QuestionBankService(root) as service:
        response = service.search(SearchRequest(course="Python", query="字典"))
        assert response.status == "bank_unavailable"
        assert response.message
        assert service.capability("Python").status == "error"
