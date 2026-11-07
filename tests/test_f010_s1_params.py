"""S1 参数适配器：显式启用、留痕，以及三类反例不被补齐。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from tests.legacy_param_cases import LegacyParameterAdapter
from tests.tutoring_fakes import FakeScheduler


def _adapter(tmp_path: Path, *keys: str) -> LegacyParameterAdapter:
    return LegacyParameterAdapter(allowed=set(keys), log_path=tmp_path / "added.jsonl")


def test_enabled_key_fills_missing_params_and_logs(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, "01:0:0:query_papers")
    params, added = adapter.enrich(
        "01", 0, 0, "query_papers", {},
        "给出2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）及答案",
    )
    assert params["course"] == "Python语言程序设计"
    assert params["academic_year"] == "2024-2025"
    assert params["term"] == "s1"
    assert params["variant"] == "A"
    assert set(added) == {"course", "exam_type", "academic_year", "term", "variant"}
    logged = [json.loads(line) for line in adapter.log_path.read_text(encoding="utf-8").splitlines()]
    assert logged == [{
        "case": "01", "turn": 0, "batch": 0, "action": "query_papers",
        "added": added,
    }]


def test_unapproved_key_is_untouched(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, "01:0:0:query_papers")
    original = {"number_label": "5"}
    params, added = adapter.enrich(
        "41", 0, 0, "search_questions", original,
        "2024-2025学年第一学期 Python 期末选择题第 5 题",
    )
    assert params == original and added == {}
    assert not adapter.log_path.exists()


def test_missing_target_stays_missing(tmp_path: Path) -> None:
    # 41 是故意缺参的反例，不在审批表内；即使适配器存在也不能动它。
    adapter = _adapter(tmp_path, "01:0:0:query_papers")
    original = {"course": "Python"}
    params, added = adapter.enrich(
        "41", 0, 0, "search_questions", original, "再找一道题",
    )
    assert params == original
    assert "number_label" not in params and "paper_id" not in params
    assert added == {}


def test_wrong_paper_is_not_overwritten(tmp_path: Path) -> None:
    # 42 是故意指错卷的反例，不在审批表内，必须原样进入执行。
    adapter = _adapter(tmp_path, "01:0:0:query_papers")
    original = {"course": "Python", "paper_id": "py-2024-2025-s1-final"}
    params, added = adapter.enrich(
        "42", 0, 0, "query_papers", original,
        "换成 2023-2024 学年第一学期 Python 期末试卷",
    )
    assert params == original
    assert added == {}


def test_same_turn_correction_is_not_applied_twice(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, "01:0:0:query_papers")
    first, _ = adapter.enrich(
        "01", 0, 0, "query_papers", {},
        "2024-2025学年第一学期 Python 期末试卷",
    )
    second, added = adapter.enrich(
        "01", 0, 0, "query_papers", first,
        "不是，换成 2023-2024 学年第一学期 Python 期末试卷",
    )
    assert second == first
    assert added == {}
    assert len(adapter.records) == 1

@pytest.mark.anyio
async def test_shared_fake_only_adapts_when_explicitly_enabled(tmp_path: Path) -> None:
    plan = ScheduleDecision(actions=[ActionPlan(
        action="query_papers", parameters={"course": "Python语言程序设计"},
    )])
    plain = FakeScheduler(decisions=[plan.model_copy(deep=True)])
    kept = await plain.plan("2024-2025学年第一学期 Python 期末试卷")
    assert kept.actions[0].parameters == {"course": "Python语言程序设计"}

    adapter = _adapter(tmp_path, "01:0:0:query_papers")
    enabled = FakeScheduler(
        decisions=[plan.model_copy(deep=True)],
        parameter_adapter=adapter,
        parameter_case="01",
    )
    got = await enabled.plan("2024-2025学年第一学期 Python 期末试卷")
    assert got.actions[0].parameters["academic_year"] == "2024-2025"
    assert adapter.records and adapter.records[0]["case"] == "01"
