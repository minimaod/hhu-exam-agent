"""P3 评估集与冻结结果：40 条、20/10/10、dev/test 各半，失败案例保留。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from app.question_bank.config import RetrievalConfig
from app.question_bank.paths import BankPaths, read_current
from tools.question_bank.evaluate import load_queries

BANK = BankPaths()


@pytest.fixture(scope="module")
def queries():
    return load_queries(BANK.eval_dir / "p3-queries.jsonl")


@pytest.fixture(scope="module")
def current():
    payload = read_current(BANK)
    if payload is None:
        pytest.skip("P3 索引尚未构建")
    return payload


def test_eval_set_has_20_relevant_10_no_match_10_filter(queries) -> None:
    assert len(queries) == 40
    assert Counter(row["kind"] for row in queries) == {
        "relevant": 20,
        "no_match": 10,
        "filter": 10,
    }
    assert Counter(row["split"] for row in queries) == {"dev": 20, "test": 20}
    for split in ("dev", "test"):
        assert Counter(row["kind"] for row in queries if row["split"] == split) == {
            "relevant": 10,
            "no_match": 5,
            "filter": 5,
        }
    assert len({row["id"] for row in queries}) == 40


def test_expected_ids_exist_and_are_searchable(queries, current) -> None:
    import sqlite3

    build_dir = BANK.build_dir(current["build_id"])
    conn = sqlite3.connect(build_dir / "questions.sqlite3")
    try:
        rows = dict(conn.execute("SELECT question_id, searchable FROM questions"))
    finally:
        conn.close()
    for row in queries:
        for question_id in row.get("expected_ids") or []:
            assert question_id in rows, f"{row['id']} 引用了不存在的题"
            assert rows[question_id] == 1, f"{row['id']} 引用了不可检索题"
        filters = row.get("filters") or {}
        if filters.get("paper_id"):
            assert filters["paper_id"].startswith("py-"), row["id"]


def test_thresholds_are_frozen() -> None:
    config = RetrievalConfig()
    assert config.vector_threshold == 0.58
    assert config.idf_coverage_threshold == 0.30


def _load_result(name: str) -> dict:
    path = BANK.evidence_dir / f"eval-{name}.json"
    if not path.is_file():
        pytest.skip(f"缺少 {path}；先运行对应 split 的评估")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mode", ["keyword_only", "vector_only", "hybrid"])
def test_dev_metrics_are_clean(mode: str) -> None:
    payload = _load_result("dev")
    metrics = payload["modes"][mode]["metrics"]["dev"]
    assert metrics["relevant"]["hit_at_5"] == 10
    assert metrics["no_match"]["passed"] == 5
    # vector-only 对 3 条过滤查询的余弦低于 0.58，宁可 no_match 也不返回
    # 无关题；BM25/hybrid 仍 5/5。越界必须为 0。
    assert metrics["filter"]["passed"] == (2 if mode == "vector_only" else 5)
    assert metrics["filter"]["violations"] == 0
    assert metrics["cross_course_violations"] == 0


@pytest.mark.parametrize("mode", ["keyword_only", "vector_only", "hybrid"])
def test_test_metrics_meet_target_and_keep_failure(mode: str) -> None:
    payload = _load_result("test")
    metrics = payload["modes"][mode]["metrics"]["test"]
    assert metrics["relevant"]["hit_at_5"] == 9  # 19/20 总体；困难同义词正例失败
    assert metrics["relevant"]["hit_rate"] >= 0.90
    assert metrics["no_match"]["passed"] == 5
    assert metrics["filter"]["passed"] == 5
    assert metrics["filter"]["violations"] == 0
    assert metrics["cross_course_violations"] == 0
    failed = [
        row
        for row in payload["modes"][mode]["per_query"]
        if row["kind"] == "relevant" and not row["passed"]
    ]
    assert [row["id"] for row in failed] == ["rel-20"]


def test_overall_report_is_honest_about_false_hits() -> None:
    for name in ("dev", "test"):
        payload = _load_result(name)
        for mode in ("keyword_only", "vector_only", "hybrid"):
            metrics = payload["modes"][mode]["metrics"]["overall"]
            assert metrics["no_match"]["false_hits"] == 0
            assert metrics["filter"]["violations"] == 0
            assert metrics["cross_course_violations"] == 0