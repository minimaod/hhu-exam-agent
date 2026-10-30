"""P3 故障恢复：Qdrant 目录锁导致向量不可用时应降级，释放后恢复。"""

from __future__ import annotations

from app.question_bank.models import SearchRequest
from app.question_bank.paths import BankPaths, read_current
from app.question_bank.service import QuestionBankService

import pytest


def test_vector_lock_degrades_and_recovers() -> None:
    if read_current(BankPaths()) is None:
        pytest.skip("P3 索引尚未构建")

    holder = QuestionBankService()
    try:
        assert holder.search(SearchRequest(course="Python", query="完全数", preview_candidates=True)).mode == "hybrid"
        assert holder._vector is not None

        blocked = QuestionBankService()
        try:
            blocked._embedder = holder._embedder
            degraded = blocked.search(SearchRequest(course="Python", query="完全数", preview_candidates=True))
            assert degraded.status == "ok"
            assert degraded.mode == "keyword_only"
            assert degraded.degraded is True
            assert degraded.degradation.code == "vector_unavailable"
        finally:
            blocked.close()
    finally:
        holder.close()

    recovered = QuestionBankService()
    try:
        response = recovered.search(SearchRequest(course="Python", query="完全数", preview_candidates=True))
        assert response.mode == "hybrid"
        assert response.degraded is False
        assert response.items
    finally:
        recovered.close()