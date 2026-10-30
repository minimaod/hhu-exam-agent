"""剩余题发布包：建议不是决定，正式应用必须有导师声明。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.question_bank.paths import BankPaths
from tools.question_bank import publish


class FakeRepository:
    def __init__(self, ids):
        self.ids = ids

        class Conn:
            def __init__(self, ids):
                self._ids = ids

            def execute(self, _sql):
                return [(item,) for item in self._ids]

        self.conn = Conn(ids)

    def fetch_summaries(self, ids):
        return {}


def test_scope_remaining_excludes_approved(tmp_path):
    root = tmp_path / "bank"
    (root / "db").mkdir(parents=True)
    (root / "db" / "reviews.json").write_text(
        json.dumps({"decisions": {"a": {"action": "approve"}}}), encoding="utf-8"
    )
    ids, reasons, approved, _first = publish._scope_ids(
        FakeRepository(["a", "b", "c"]),
        BankPaths(root),
        scope="remaining",
        task_doc=publish.TASK_DOC,
    )
    assert ids == ["b", "c"]
    assert reasons["b"] == ["remaining"]
    assert approved == ["a"]


def test_suggested_decision_is_not_formal():
    row = {"answer_available": True, "content_sha256": "a" * 64}
    visual = {"result": "pass", "vision": {"confidence": "high"}, "issues": []}
    item = publish._suggest_decision(row, visual)
    assert item["action"] == "approve"
    assert item["formal_decision"] is False
    assert item["mentor_review_required"] is True


def test_missing_answer_stays_review():
    item = publish._suggest_decision(
        {"answer_available": False, "content_sha256": "a" * 64},
        {"result": "missing_answer", "issues": ["缺答案"]},
    )
    assert item["action"] == "review"


def test_isolated_is_rejected():
    item = publish._suggest_decision(
        {"answer_available": True, "content_sha256": "a" * 64},
        {"result": "isolated", "isolated": True, "issues": []},
    )
    assert item["action"] == "reject"


def test_remaining_apply_requires_mentor_reviewer(tmp_path, monkeypatch):
    packet = tmp_path / "packet"
    packet.mkdir()
    (packet / "decisions-approved.json").write_text(
        json.dumps(
            {
                "build_id": "iv-0000000000000000",
                "scope": "remaining",
                "reviewed_by": "vision_model",
                "reviewed_at": "2026-10-28T00:00:00+08:00",
                "reviewer_type": "model",
                "decisions": {"q1": {"action": "approve", "content_sha256": "a" * 64}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        publish,
        "read_current",
        lambda _paths: {"build_id": "iv-0000000000000000", "source_version": "sv-test"},
    )
    with pytest.raises(SystemExit, match="reviewer_type=mentor"):
        publish.apply(packet, bank_root=tmp_path / "bank", write=False)


def test_all_apply_requires_mentor_reviewer_too(tmp_path, monkeypatch):
    packet = tmp_path / "packet"
    packet.mkdir()
    (packet / "decisions-approved.json").write_text(
        json.dumps(
            {
                "build_id": "iv-0000000000000000",
                "scope": "all",
                "reviewed_by": "vision_model",
                "reviewed_at": "2026-10-28T00:00:00+08:00",
                "reviewer_type": "model",
                "decisions": {"q1": {"action": "approve", "content_sha256": "a" * 64}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        publish,
        "read_current",
        lambda _paths: {"build_id": "iv-0000000000000000", "source_version": "sv-test"},
    )
    with pytest.raises(SystemExit, match="reviewer_type=mentor"):
        publish.apply(packet, bank_root=tmp_path / "bank", write=False)


def test_remaining_apply_with_mentor_still_needs_candidate_packet(tmp_path, monkeypatch):
    packet = tmp_path / "packet"
    packet.mkdir()
    (packet / "decisions-approved.json").write_text(
        json.dumps(
            {
                "build_id": "iv-0000000000000000",
                "scope": "remaining",
                "reviewed_by": "导师",
                "reviewed_at": "2026-10-28T00:00:00+08:00",
                "reviewer_type": "mentor",
                "decisions": {"q1": {"action": "approve", "content_sha256": "a" * 64}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        publish,
        "read_current",
        lambda _paths: {"build_id": "iv-0000000000000000", "source_version": "sv-test"},
    )
    with pytest.raises(SystemExit, match="publish-candidates"):
        publish.apply(packet, bank_root=tmp_path / "bank", write=False)