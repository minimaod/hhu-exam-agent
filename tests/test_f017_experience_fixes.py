"""F017 第二批体验修复后端回归测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from app.conversations.authorization import authorize_action, is_save_affirmative
from app.conversations.final_reply import evaluation_brief_text
from app.conversations.graph import ActionPlan
from app.conversations.models import ExerciseEvaluation, ExerciseView
from app.conversations.narration import NarrationMixin
from app.conversations.service import _DynamicContext
from app.tutoring.models import (
    Explanation,
    KnowledgePoint,
    Step,
)
from tests.test_f009_agent_memory import build, seed_wrong
from tests.test_mistake_records import env  # noqa: F401
from tests.tutoring_fakes import png_bytes


def _count(db: Path, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.mark.parametrize(
    "phrase",
    [
        "添加到错题本里面",
        "把这道题加入错题本",
        "这题帮我记下来",
        "收进错题本",
        "先存起来",
    ],
)
def test_natural_save_phrases_authorize_record_mistake(
    tmp_path: Path, phrase: str
) -> None:
    assert authorize_action("record_mistake", phrase, transcript=phrase) is None
    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)
    action = ActionPlan(
        action="record_mistake",
        parameters={"basis": {"kind": "student_request", "quote": phrase}},
    )
    outcome = service._guard_dynamic_action(
        action, ctx, {"last_suggestions": []}, phrase
    )
    assert outcome is None


@pytest.mark.parametrize(
    "phrase",
    [
        "不要保存",
        "如果保存会怎样",
        "上次保存了吗",
        "这道题有没有保存过",
    ],
)
def test_negative_or_meta_save_phrases_blocked(tmp_path: Path, phrase: str) -> None:
    blocked = authorize_action("record_mistake", phrase, transcript=phrase)
    assert blocked is not None

    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)
    action = ActionPlan(action="record_mistake", parameters={})
    before = _count(tmp_path / "study.sqlite3", "mistake_records")
    outcome = service._guard_dynamic_action(
        action, ctx, {"last_suggestions": [{"label": "保存这道题"}]}, phrase
    )
    assert outcome is not None
    assert outcome.error_code == "AUTHORIZATION_REQUIRED"
    assert outcome.status in ("failed", "clarification")
    assert _count(tmp_path / "study.sqlite3", "mistake_records") == before


def test_affirmative_reply_after_single_save_suggestion_authorizes(
    tmp_path: Path,
) -> None:
    assert is_save_affirmative("好")
    assert is_save_affirmative("可以")
    assert not is_save_affirmative("不要")

    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)
    action = ActionPlan(action="record_mistake", parameters={})
    context_snap = {
        "last_suggestions": [{"label": "保存这道题", "action": "保存这道题"}],
        "last_action_summary": "是否把这道题加入错题本？",
    }
    outcome = service._guard_dynamic_action(action, ctx, context_snap, "可以")
    assert outcome is None


def test_ambiguous_multi_option_save_asks_which_problem_only(tmp_path: Path) -> None:
    service, seed, _ = build(tmp_path)
    conversation, _, _ = seed_wrong(seed)
    ctx = _DynamicContext(conversation_id=conversation)
    action = ActionPlan(action="record_mistake", parameters={})
    before = _count(tmp_path / "study.sqlite3", "mistake_records")
    context_snap = {
        "last_suggestions": [
            {"label": "保存第 1 题", "action": "保存第 1 题"},
            {"label": "保存第 2 题", "action": "保存第 2 题"},
        ],
        "last_action_summary": "第 1 题和第 2 题都在本次对话中，请确认。",
    }
    outcome = service._guard_dynamic_action(action, ctx, context_snap, "好")
    assert outcome is not None
    assert outcome.status == "clarification"
    assert outcome.needs_user is True
    assert outcome.message == "要保存哪一道？"
    assert outcome.error_code == "AMBIGUOUS_BASIS"
    assert _count(tmp_path / "study.sqlite3", "mistake_records") == before


def test_evaluation_brief_text_keeps_concise_and_separates_card() -> None:
    exercise = ExerciseView(
        id="ex-1",
        conversation_id="conv-1",
        problem_id="prob-1",
        root_problem_id="prob-1",
        statement_text="求极限",
        knowledge_point="等价无穷小",
        status="submitted",
        evaluation=ExerciseEvaluation(
            is_correct=False,
            final_answer_assessment="你的最终答案 2 有误。",
            step_by_step_analysis=("未检测到过程文本。" * 40) + "SUPER-LONG-ANALYSIS-TAIL",
            guidance="请补充推导步骤后重试。",
            needs_supplement=True,
        ),
        created_at="2026-11-15T00:00:00Z",
        updated_at="2026-11-15T00:00:00Z",
    )
    brief = evaluation_brief_text(
        exercise,
        json.dumps(exercise.evaluation.model_dump(mode="json"), ensure_ascii=False),
    )
    assert brief is not None
    assert "展示分工说明" in brief
    assert "严禁在正文复述" in brief
    assert "正文不要复述上述评价内容" in brief
    assert "SUPER-LONG-ANALYSIS-TAIL" not in brief

    # narration._teaching_material 与 evaluation_brief_text 保持一致口径
    rendered = NarrationMixin._teaching_material(
        _DynamicContext(
            conversation_id="conv-1",
            exercise_view=exercise,
            evaluation_view=exercise.evaluation,
        )
    )
    assert rendered is not None
    assert "展示分工说明" in rendered


def test_mistake_detail_standalone_exercise_replaces_anchor_and_preserves_attachments(
    env,
) -> None:
    conversation_id = env.client.post("/api/conversations", json={}).json()["id"]
    uploaded = env.client.post(
        f"/api/conversations/{conversation_id}/attachments",
        files={"file": ("work.png", png_bytes(), "image/png")},
    ).json()

    anchor_problem = env.repo.create_problem(
        conversation_id=conversation_id,
        statement_text="【自主练习主题】求极限等价无穷小",
        explanation=Explanation(
            knowledge_points=[
                KnowledgePoint(name="独立作答提示", explanation="请先独立完成作答。"),
            ],
            steps=[
                Step(index=1, title="独立作答", content="请先独立推导后再对照参考解答。"),
            ],
            final_answer="（请先独立作答，完成后再查看参考解答）",
        ),
    )
    ex = env.repo.create_exercise(
        id=str(uuid4()),
        conversation_id=conversation_id,
        problem_id=anchor_problem.id,
        root_problem_id=anchor_problem.root_problem_id,
        statement_text=r"计算 $\lim_{x\to 0}\frac{\sin 2x}{x}$",
        knowledge_point="等价无穷小",
        reference_solution_text="因为 sin(2x) ~ 2x，所以极限为 2。",
    )
    env.repo.create_exercise_attempt(
        ex.id,
        kind="initial",
        student_answer_text="1",
        student_solution_text="",
        attachments=[(uploaded["id"], "student_solution")],
        request_id="req-att-1",
    )
    eval_payload = ExerciseEvaluation(
        is_correct=False,
        final_answer_assessment="答案有误。",
        step_by_step_analysis="未提供推导过程，无法定位具体错误步骤。",
        guidance="请拍手写过程图或写出关键步骤。",
        needs_supplement=True,
    )
    env.repo.save_exercise_evaluation(
        ex.id,
        evaluation_json=json.dumps(eval_payload.model_dump(mode="json"), ensure_ascii=False),
    )

    first_save = env.client.post(
        "/api/mistake-records",
        json={
            "request_id": str(uuid4()),
            "conversation_id": conversation_id,
            "problem_id": anchor_problem.id,
        },
    )
    assert first_save.status_code in (200, 201)
    record_id = first_save.json()["id"]

    # 重复保存同一根题：幂等去重，总条目保持 1
    dup_save = env.client.post(
        "/api/mistake-records",
        json={
            "request_id": str(uuid4()),
            "conversation_id": conversation_id,
            "problem_id": anchor_problem.id,
        },
    )
    assert dup_save.status_code == 200
    assert dup_save.json()["id"] == record_id

    listing = env.client.get("/api/mistake-records").json()
    assert listing["total"] == 1
    assert listing["items"][0]["is_model_generated"] is True
    assert not listing["items"][0]["title"].startswith("【自主练习")
    assert "计算" in listing["items"][0]["statement_excerpt"]

    detail = env.client.get(f"/api/mistake-records/{record_id}").json()
    assert detail["is_model_generated"] is True
    assert detail["statement_text"] == r"计算 $\lim_{x\to 0}\frac{\sin 2x}{x}$"
    # 占位解析过滤且不提前泄露未解锁的参考解答
    assert detail["explanation"] is None
    assert detail["primary_exercise"] is not None
    assert detail["primary_exercise"]["id"] == ex.id
    assert detail["primary_exercise"]["reference_solution_text"] is None
    assert len(detail["primary_exercise"]["attachments"]) == 1
    assert detail["primary_exercise"]["attachments"][0]["id"] == uploaded["id"]
    # 主练习不会重复出现在关联类似练习列表中
    assert detail["exercises"] == []

    # 对话详情同步返回 records_by_root
    conv_detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    assert conv_detail["records_by_root"][anchor_problem.root_problem_id]["id"] == record_id
