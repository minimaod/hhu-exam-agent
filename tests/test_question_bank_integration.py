"""F007 P4：真实题库装配与“未审核不返回”门禁（本地数据缺失时跳过）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.question_bank.models import SearchRequest
from app.question_bank.service import QuestionBankService
from app.questions.runtime import QuestionRuntime, bank_root_from_env

BANK_ROOT = bank_root_from_env()


@pytest.fixture(scope="module")
def real_runtime():
    if not (BANK_ROOT / "db" / "current.json").is_file():
        pytest.skip("本机没有题库构建产物")
    service = QuestionBankService(BANK_ROOT)
    runtime = QuestionRuntime(service, bank_root=BANK_ROOT)
    try:
        yield runtime
    finally:
        runtime.close()


def test_real_bank_capability_and_unreviewed_gate(real_runtime: QuestionRuntime) -> None:
    capability = real_runtime.capability("Python语言程序设计")
    assert capability.status in ("unreviewed", "ready")
    assert capability.searchable >= 1000
    assert "索引损坏" not in capability.message
    assert "preview" not in capability.message.lower()

    result = real_runtime.search(
        SearchRequest(course="Python语言程序设计", query="循环 列表", limit=5),
        conversation_id="conv-real",
    )
    assert result.status in ("ok", "no_match", "unreviewed")
    assert result.status != "bank_unavailable"
    if capability.approved == 0:
        assert result.status == "unreviewed"
        assert result.cards == []


def test_cross_paper_runtime_preserves_individual_sources_without_claiming_scope(real_runtime):
    result = real_runtime.search(SearchRequest(course="Python", query="循环", question_type="choice", limit=3),
                                 conversation_id="conv-cross-paper")
    assert result.status == "ok" and result.cards
    assert result.paper_id is None and result.paper_title is None
    assert all(card.source.paper_id and card.source.paper_title for card in result.cards)
    assert all(card.answer_detail is None for card in result.cards)
