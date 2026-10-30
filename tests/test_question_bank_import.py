"""P3 事实库回归：题量、内容哈希、6 条内容问题、schema 与题图。"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.question_bank.paths import BankPaths, read_current
from tools.question_bank import build as B
from tools.question_bank import db as QDB

BANK = BankPaths()
BAD_IDS = {
    "py-2022-2023-s1-final-choice-11",
    "py-2023-2024-s1-final-choice-19",
}
RECOVERED_IDS = {
    "py-2021-2022-s2-midterm-choice-22",
    "py-2021-2022-s2-midterm-choice-30",
    "py-2024-2025-s1-midterm-choice-40",
    "py-2024-2025-s2-final-choice-11",
}


@pytest.fixture(scope="module")
def inputs():
    current = read_current(BANK) or {}
    replay = current.get("source_corrections")
    if replay is None:
        return B.load_bank_inputs(BANK)
    assert isinstance(replay, list) and replay
    paths = []
    for relative in replay:
        path = (BANK.root / relative).resolve()
        assert path.is_relative_to(BANK.root.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == current["source_correction_sha256"][relative]
        paths.append(path)
    return B.load_bank_inputs(BANK, corrections=paths)


@pytest.fixture(scope="module")
def current():
    payload = read_current(BANK)
    if payload is None:
        pytest.skip("P3 索引尚未构建：先运行 python -m tools.question_bank.build build")
    return payload


@pytest.fixture(scope="module")
def build_dir(current):
    path = BANK.build_dir(current["build_id"])
    if not path.is_dir():
        pytest.skip("current.json 指向的 build 不存在")
    return path


def test_source_counts_and_searchable(inputs) -> None:
    records, manifest, papers = inputs
    assert len(papers) == 20
    assert len(records) == 1154
    assert sum(1 for r in records if r["question_type"] == "choice") == 790
    assert sum(1 for r in records if r["question_type"] == "fill") == 324
    assert sum(1 for r in records if r["question_type"] == "code") == 40
    issues = {r["question_id"] for r in records if QDB.content_issue(r)}
    assert issues == BAD_IDS
    assert len(records) - len(issues) == 1152
    assert len(manifest.get("issues") or []) == 20


def test_content_hash_is_stable_and_matches_db(inputs, build_dir: Path) -> None:
    records, _, _ = inputs
    conn = sqlite3.connect(build_dir / "questions.sqlite3")
    try:
        rows = dict(conn.execute("SELECT question_id, content_sha256 FROM questions"))
    finally:
        conn.close()
    assert len(rows) == 1154
    for record in records:
        assert rows[record["question_id"]] == QDB.content_sha256(record)


def test_two_label_anomalies_are_not_searchable(build_dir: Path) -> None:
    conn = sqlite3.connect(build_dir / "questions.sqlite3")
    try:
        rows = conn.execute(
            "SELECT question_id, content_status, content_issue, searchable "
            "FROM questions WHERE content_status!='ok'"
        ).fetchall()
        option_shape = conn.execute(
            "SELECT COUNT(*) FROM ("
            " SELECT o.question_id FROM options o JOIN questions q ON q.question_id=o.question_id "
            "WHERE q.question_type='choice' GROUP BY o.question_id HAVING COUNT(*)=4)"
        ).fetchone()[0]
    finally:
        conn.close()
    assert {row[0] for row in rows} == BAD_IDS
    assert all(row[1] == "options_label_anomaly" and row[2] and row[3] == 0 for row in rows)
    assert option_shape == 790


def test_schema_contains_required_fact_tables(build_dir: Path) -> None:
    conn = sqlite3.connect(build_dir / "questions.sqlite3")
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    required = {
        "papers",
        "questions",
        "options",
        "code_lines",
        "blank_marks",
        "formulas",
        "assets",
        "code_blocks",
        "code_annotations",
        "code_examples",
        "independent_solutions",
        "answer_sources",
        "knowledge_points",
        "question_knowledge_points",
        "retrieval_texts",
        "fragments",
    }
    assert required <= tables


def test_assets_are_copied_and_hashed(build_dir: Path) -> None:
    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert len(manifest["assets"]) == 9
    for asset in manifest["assets"]:
        path = build_dir / asset["path"]
        assert path.is_file()
        assert B._sha_file(path) == asset["sha256"]


def test_build_manifest_versions_are_consistent(current, build_dir: Path) -> None:
    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert manifest["build_id"] == current["build_id"] == manifest["index_version"]
    assert manifest["source_version"] == current["source_version"]
    assert manifest["counts"]["questions"] == 1154
    assert manifest["counts"]["searchable"] == 1152
    assert manifest["counts"]["fragments"] == 1152
    assert manifest["model"]["id"] == "BAAI/bge-small-zh-v1.5"
    assert (build_dir / "verify.json").is_file()

def test_four_visually_recovered_options_are_complete(inputs) -> None:
    records, _, _ = inputs
    by_id = {record["question_id"]: record for record in records}
    expected = {
        "py-2021-2022-s2-midterm-choice-22": [
            ("A", "语法错误"),
            ("B", "当a=7 时，表达式结果为False"),
            ("C", "当a=2 时，表达式结果为False"),
            ("D", "当a=10时，表达式结果为True"),
        ],
        "py-2021-2022-s2-midterm-choice-30": [
            ("A", "15"), ("B", "7"), ("C", "5"), ("D", "死循环")
        ],
        "py-2024-2025-s1-midterm-choice-40": [
            ("A", "45"), ("B", "55"), ("C", "18"), ("D", "7")
        ],
        "py-2024-2025-s2-final-choice-11": [
            ("A", "x=int('16/4')"),
            ("B", "x= eval ('16/4')"),
            ("C", "x= float('16/4')"),
            ("D", "x=eval(16/4)"),
        ],
    }
    for question_id, options in expected.items():
        record = by_id[question_id]
        assert [(item["label"], item["text"]) for item in record["options"]] == options
        assert QDB.content_issue(record) is None
        assert record["checks"]["options_complete"] is True


def test_two_duplicate_b_labels_keep_all_four_texts(inputs) -> None:
    records, _, _ = inputs
    by_id = {record["question_id"]: record for record in records}
    expected = {
        "py-2022-2023-s1-final-choice-11": [
            ("A", "1 3 5 7 9 11 13"),
            ("B", "1 4 7 10 13"),
            ("B", "1 3 5 7 9 13 15"),
            ("D", "1 3 5 7 9 11"),
        ],
        "py-2023-2024-s1-final-choice-19": [
            ("A", "[2,8,9,11,7,15]"),
            ("B", "[15,11,9,8,7,2]"),
            ("B", "[2,7,8,9,11,15]"),
            ("D", "[15,8,9,11,2,7]"),
        ],
    }
    for question_id, options in expected.items():
        record = by_id[question_id]
        assert [(item["label"], item["text"]) for item in record["options"]] == options
        assert QDB.content_issue(record) is not None
        assert QDB.content_status(record) == "options_label_anomaly"


def test_option_fix_by_number_does_not_leak_into_fill_records(inputs) -> None:
    records, _, _ = inputs
    by_id = {record["question_id"]: record for record in records}
    for question_id in (
        "py-2022-2023-s1-final-fill-11",
        "py-2024-2025-s2-final-fill-11",
    ):
        record = by_id[question_id]
        assert record["question_type"] == "fill"
        assert record["options"] == []
