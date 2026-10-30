"""P3 检索组件：分片不截断、BM25、过滤、长查询与向量锁降级。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.question_bank.bm25 import Bm25Document, Bm25Index
from app.question_bank.errors import QuestionBankRequestError
from app.question_bank.models import SearchRequest
from app.question_bank.paths import BankPaths, read_current
from app.question_bank.service import QuestionBankService
from app.question_bank.textbuild import FragmentTooLongError, build_retrieval_text, fragment_text
from app.question_bank.vector_store import VectorStore


@pytest.fixture(scope="module")
def service():
    if read_current(BankPaths()) is None:
        pytest.skip("P3 索引尚未构建")
    with QuestionBankService() as instance:
        yield instance


def test_fragment_text_covers_input_without_truncation() -> None:
    text = "\n".join(f"第{i}行：字典与列表的用法" for i in range(20))
    pieces = fragment_text(text, len, budget=60)
    assert len(pieces) > 1
    assert "".join(pieces) == text
    assert all(len(piece) <= 60 for piece in pieces)


def test_single_overlong_line_fails_loudly() -> None:
    with pytest.raises(FragmentTooLongError):
        fragment_text("x" * 100, len, budget=20)


def test_retrieval_text_never_contains_answers_or_reference_code() -> None:
    record = {
        "statement": "求字典长度",
        "options": [{"label": "A", "text": "len(d)"}],
        "code_lines": ["d = {'a': 1}"],
        "answer": {"state": "original", "text": "SECRET-ANSWER"},
        "reference_code": "SECRET-CODE",
        "formulas": [{"text": "n=len(d)"}],
        "assets": [{"description": "图示", "transcription": "图内文字"}],
        "code_examples": [{"text": "示例输入"}],
    }
    text = build_retrieval_text(record, ["字典与集合"])
    assert "SECRET" not in text
    assert "len(d)" in text
    assert "知识点：字典与集合" in text


def test_bm25_prefers_exact_and_aggregates_parent() -> None:
    index = Bm25Index.build(
        [
            Bm25Document("a#f01", "a", ("字典", "长度", "len")),
            Bm25Document("a#f02", "a", ("字典", "键值")),
            Bm25Document("b#f01", "b", ("列表", "排序")),
        ],
        k1=1.5,
        b=0.75,
    )
    hits = index.search(["字典", "长度"], {"a", "b"}, limit=5)
    assert [hit.question_id for hit in hits] == ["a"]
    assert hits[0].idf_coverage == 1.0
    assert hits[0].fragment_id == "a#f01"


def test_service_filters_apply_to_search_and_browse(service: QuestionBankService) -> None:
    response = service.search(
        SearchRequest(course="Python", query="turtle", paper_id="py-2020-2021-s1-final", preview_candidates=True)
    )
    assert response.status == "ok"
    assert response.items
    assert all(item.paper.paper_id == "py-2020-2021-s1-final" for item in response.items)

    browse = service.search(
        SearchRequest(course="Python", question_type="code", limit=5, preview_candidates=True)
    )
    assert browse.mode == "browse"
    assert browse.items
    assert all(item.question_type == "code" for item in browse.items)


def test_knowledge_point_filter_is_candidate_but_usable(service: QuestionBankService) -> None:
    response = service.search(
        SearchRequest(course="Python", query="列表", knowledge_points=["列表与元组"], preview_candidates=True)
    )
    assert response.status == "ok"
    for item in response.items:
        assert any(point.slug == "lists" for point in item.knowledge_points)


def test_overlong_query_is_rejected_not_truncated(service: QuestionBankService) -> None:
    with pytest.raises(QuestionBankRequestError, match="token"):
        service.search(SearchRequest(course="Python", query="字" * 600))


def test_filters_do_not_turn_irrelevant_query_into_results(service: QuestionBankService) -> None:
    for kwargs in (
        {"question_type": "choice"},
        {"paper_id": "py-2020-2021-s1-final"},
        {"question_type": "choice", "paper_id": "py-2020-2021-s1-final"},
    ):
        response = service.search(
            SearchRequest(
                course="Python",
                query="量子计算 量子比特",
                preview_candidates=True,
                **kwargs,
            )
        )
        assert response.status == "no_match", kwargs
        assert response.returned == 0, kwargs
        assert response.diagnostics.gate["gate_applied"] is True


def test_vector_fragments_are_aggregated_before_rrf(service: QuestionBankService) -> None:
    hits = [
        {"question_id": "a", "fragment_id": "a#f1", "score": 0.70},
        {"question_id": "b", "fragment_id": "b#f1", "score": 0.60},
        {"question_id": "b", "fragment_id": "b#f2", "score": 0.59},
    ]
    aggregated = service._aggregate_vector_hits(hits)
    assert [item["question_id"] for item in aggregated] == ["a", "b"]
    fused = dict(service._fuse([], hits))
    assert set(fused) == {"a", "b"}
    assert fused["a"]["score"] == pytest.approx(1.0 / (service.config.rrf_k + 1))
    assert fused["b"]["score"] == pytest.approx(1.0 / (service.config.rrf_k + 2))


def test_vector_scope_is_applied_before_topk(service: QuestionBankService) -> None:
    target = "py-2020-2021-s2-midterm-choice-32"
    previous = set(service._approved)
    service._approved = {target}
    try:
        response = service.search(
            SearchRequest(course="Python", query="以下代码的输出结果", limit=1),
            force_mode="vector",
        )
        assert response.status == "ok"
        assert response.mode == "vector_only"
        assert [item.question_id for item in response.items] == [target]
    finally:
        service._approved = previous


def test_vector_only_degradation_still_recalls_approved_question(
    service: QuestionBankService,
) -> None:
    target = "py-2020-2021-s2-midterm-choice-32"
    previous = set(service._approved)
    bm25 = service._bm25
    service._approved = {target}
    service._bm25 = None
    try:
        response = service.search(
            SearchRequest(course="Python", query="以下代码的输出结果", limit=1)
        )
        assert response.status == "ok"
        assert response.mode == "vector_only"
        assert response.degraded is True
        assert [item.question_id for item in response.items] == [target]
    finally:
        service._approved = previous
        service._bm25 = bm25


def test_qdrant_filter_contains_eligible_question_ids() -> None:
    query_filter = VectorStore.build_filter(
        course="Python", question_ids=["b", "a", "a"]
    )
    conditions = [item for item in query_filter.must if item.key == "question_id"]
    assert len(conditions) == 1
    assert conditions[0].match.any == ["a", "b"]


def test_rejected_question_is_excluded_from_vector_scope(service: QuestionBankService) -> None:
    target = "py-2020-2021-s2-midterm-choice-32"
    rejected = "py-2020-2021-s2-midterm-choice-31"
    previous_approved = set(service._approved)
    previous_rejected = set(service._rejected)
    captured: list[list[str]] = []
    original_search = service._vector.search

    def spy(vector, query_filter, limit):
        conditions = [
            item for item in query_filter.must if item.key == "question_id"
        ]
        captured.append(
            list(conditions[0].match.any) if conditions else []
        )
        return original_search(vector, query_filter, limit)

    service._approved = {target}
    service._rejected = {rejected}
    service._vector.search = spy
    try:
        response = service.search(
            SearchRequest(course="Python", query="以下代码的输出结果", limit=1),
            force_mode="vector",
        )
        assert response.status == "ok"
        assert captured == [[target]]
    finally:
        service._vector.search = original_search
        service._approved = previous_approved
        service._rejected = previous_rejected