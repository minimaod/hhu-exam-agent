"""P3 校验必须能发现内容被改、BM25/向量 ID 与事实库不对应。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.question_bank.config import COLLECTION, MODEL_DIM
from app.question_bank.paths import BankPaths, read_current
from app.question_bank.vector_store import VectorStore
from tools.question_bank import build as B


@pytest.fixture()
def copied_build(tmp_path: Path) -> Path:
    current = read_current(BankPaths())
    if current is None:
        pytest.skip("P3 索引尚未构建")
    source = BankPaths().build_dir(current["build_id"])
    target = tmp_path / "build"
    shutil.copytree(source, target)
    return target


def test_verify_detects_statement_tampering(copied_build: Path) -> None:
    conn = sqlite3.connect(copied_build / "questions.sqlite3")
    try:
        conn.execute(
            "UPDATE questions SET statement = statement || '【篡改】' WHERE question_id = ?",
            ("py-2020-2021-s1-final-code-01",),
        )
        conn.commit()
    finally:
        conn.close()
    result = B.verify_build(copied_build)
    assert result["ok"] is False
    assert result["checks"]["material_mismatch"] >= 1
    assert any("事实库字段" in item for item in result["errors"])


def test_verify_detects_bm25_question_ids_not_in_db(copied_build: Path) -> None:
    path = copied_build / "bm25" / "index.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for doc in payload["docs"]:
        doc["question_id"] = "ghost-question"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = B.verify_build(copied_build)
    assert result["ok"] is False
    assert result["checks"]["bm25_alignment_mismatch"] > 0
    assert any("BM25 的片段/题目 ID" in item for item in result["errors"])


def test_verify_detects_qdrant_parent_mismatch(copied_build: Path) -> None:
    build = json.loads((copied_build / "build.json").read_text(encoding="utf-8"))
    store = VectorStore(copied_build / "qdrant", str(build.get("collection") or COLLECTION), MODEL_DIM)
    try:
        store.open()
        points, _ = store.client.scroll(
            collection_name=str(build.get("collection") or COLLECTION),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        assert points
        store.client.set_payload(
            collection_name=str(build.get("collection") or COLLECTION),
            payload={"question_id": "ghost-question"},
            points=[points[0].id],
        )
    finally:
        store.close()
    result = B.verify_build(copied_build)
    assert result["ok"] is False
    assert result["checks"]["vector_alignment_mismatch"] > 0
    assert any("向量的片段/题目 ID" in item for item in result["errors"])