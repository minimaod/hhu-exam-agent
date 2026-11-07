"""F010 D：参数、依据与提示词的最小回归。

这些用例只验证 D 新增的协议层，不改冻结 A/B 评测；业务场景证据在
``tmp/f010-d`` 的运行记录里。
"""

from __future__ import annotations

from app.conversations.basis import validate_basis
from app.conversations.prompts import TOOLING_SYSTEM_PROMPT
from app.conversations.tools import TOOLS_BY_NAME, parse_tool_call


def test_basis_quote_must_be_real_substring() -> None:
    assert validate_basis(
        {"kind": "student_request", "quote": "保存这题"},
        current_text="把这题保存到错题本，保存这题",
        transcript=[],
        suggestions=[],
    ) is None
    error = validate_basis(
        {"kind": "student_request", "quote": "保存这题"},
        current_text="帮我看看这题",
        transcript=[],
        suggestions=[],
    )
    assert error is not None and error[0] == "BASIS_UNVERIFIED"


def test_ambiguous_suggestion_requires_id() -> None:
    error = validate_basis(
        {"kind": "confirmed_suggestion"},
        current_text="好",
        transcript=[],
        suggestions=["练一道", "保存错题"],
    )
    assert error is not None and error[0] == "AMBIGUOUS_BASIS"


def test_tool_result_basis_must_point_to_completed_call() -> None:
    transcript = [{"call_id": "call_1", "action": "search_questions",
                   "outcome": {"status": "success"}}]
    assert validate_basis(
        {"kind": "tool_result", "depends_on_call_id": "call_1"},
        current_text="就这道",
        transcript=transcript,
        suggestions=[],
    ) is None
    error = validate_basis(
        {"kind": "tool_result", "depends_on_call_id": "call_404"},
        current_text="就这道",
        transcript=transcript,
        suggestions=[],
    )
    assert error is not None and error[0] == "BASIS_UNVERIFIED"


def test_protected_tools_accept_optional_basis() -> None:
    for name in ("save_mistake", "generate_exercise", "delete_learning_memory"):
        spec = TOOLS_BY_NAME[name]
        assert "basis" in spec.schema["properties"], name
    plan, error = parse_tool_call(
        "save_mistake",
        {"basis": {"kind": "student_request", "quote": "保存"}},
        tool_call_id="c1",
    )
    assert error is None and plan is not None
    assert plan.parameters["basis"]["kind"] == "student_request"


def test_new_target_parameters_are_exposed() -> None:
    expected = {
        "query_papers": {"academic_year", "term", "variant", "paper_id"},
        "search_questions": {"scope", "exclude_question_id", "defer_answer"},
        "list_paper_questions": {"limit"},
        "select_question": {"question_type", "paper_id"},
        "explain_problem": {"scope", "ref"},
    }
    for name, fields in expected.items():
        assert fields <= set(TOOLS_BY_NAME[name].schema["properties"]), name


def test_prompt_uses_principles_and_examples_not_keyword_list() -> None:
    assert "严格守则" not in TOOLING_SYSTEM_PROMPT
    assert TOOLING_SYSTEM_PROMPT.count("学生：") >= 5
    assert "每轮怎么做" in TOOLING_SYSTEM_PROMPT
    assert len(TOOLING_SYSTEM_PROMPT) < 6000