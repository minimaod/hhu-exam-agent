"""F008：工具协议与参数校验（工具定义、JSON Schema、解析与拒绝）。"""

from __future__ import annotations

import json

import pytest

from app.conversations.protocol import ActionPlan
from app.conversations.tools import (
    TOOL_SPECS,
    TOOLS_BY_NAME,
    ToolRejection,
    openai_tools,
    parse_tool_call,
    public_details,
    tool_label,
)

EXPECTED_ACTIONS = {
    "confirm_course": "introduce",
    "query_papers": "query_papers",
    "search_questions": "search_questions",
    "list_paper_questions": "list_paper_questions",
    "select_question": "select_question",
    "explain_problem": "explain_problem",
    "answer_followup": "followup_problem",
    "generate_exercise": "generate_exercise",
    "evaluate_exercise": "evaluate_exercise",
    "reveal_solution": "reveal_solution",
    "show_question_answer": "view_question_answer",
    "save_mistake": "record_mistake",
    "record_feedback": "record_feedback",
    "ask_user": "clarify_or_reply",
}


def test_fourteen_tools_and_one_to_one_action_mapping() -> None:
    """工具集合固定为 14 个，且与内部动作一一对应（不重复、不缺项）。"""

    assert len(TOOL_SPECS) == 14
    assert {spec.name for spec in TOOL_SPECS} == set(EXPECTED_ACTIONS)
    assert {spec.action for spec in TOOL_SPECS} == set(EXPECTED_ACTIONS.values())
    assert len({spec.action for spec in TOOL_SPECS}) == 14
    for spec in TOOL_SPECS:
        assert spec.description.strip(), spec.name
        assert spec.args_model.model_json_schema()["type"] == "object"


def test_openai_schema_is_flat_and_declares_all_parameters() -> None:
    """工具定义可直接交给 bind_tools；参数模型字段与描述全部出现在 schema 中。"""

    payload = openai_tools()
    assert len(payload) == 14
    for item in payload:
        assert item["type"] == "function"
        function = item["function"]
        assert function["name"] in EXPECTED_ACTIONS
        assert function["description"].strip()
        parameters = function["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert isinstance(parameters["properties"], dict)
        # 供应商工具调用不支持 $ref/$defs；可选值用 type 数组表达。
        assert "$defs" not in json.dumps(parameters)
        assert "$ref" not in json.dumps(parameters)
        spec = TOOLS_BY_NAME[function["name"]]
        # 公开 schema 里的每个参数都必须能被解析器接受（否则模型照文档调用
        # 也会被拒绝）；解析模型可以保留不外露的内部默认值。
        assert set(parameters["properties"]) <= set(spec.args_model.model_fields)
        assert set(parameters.get("required", [])) <= set(parameters["properties"])


def test_parse_tool_call_returns_actionplan_with_protocol_fields() -> None:
    """合法调用解析为内部动作，并保留 tool_call_id 供结果回传。"""

    plan, rejection = parse_tool_call(
        "query_papers",
        {"course": "Python", "exam_type": "final"},
        tool_call_id="call_1",
    )
    assert rejection is None
    assert isinstance(plan, ActionPlan)
    assert plan.action == "query_papers"
    assert plan.parameters["course"] == "Python"
    assert plan.tool_call_id == "call_1"
    assert plan.tool == "query_papers"
    # 原生工具调用没有 reason 字段；保留空串，说明由工具名决定动作。
    assert plan.reason == ""


def test_optional_parameters_are_omitted_instead_of_sent_as_null() -> None:
    """未填写的可选参数不会以 None 写入动作参数，避免下游误判。"""

    plan, rejection = parse_tool_call("query_papers", {"course": "高数"}, tool_call_id="c2")
    assert rejection is None
    assert plan is not None
    assert plan.parameters == {"course": "高数"}


def test_unknown_tool_is_rejected_without_raising() -> None:
    """未知工具返回可回传的拒绝载荷，而不是抛异常打断整轮。"""

    plan, rejection = parse_tool_call("launch_rocket", {}, tool_call_id="c3")
    assert plan is None
    assert isinstance(rejection, ToolRejection)
    assert rejection.code == "UNKNOWN_TOOL"
    assert rejection.tool_call_id == "c3"
    payload = rejection.to_payload()
    assert payload["status"] == "failed"
    assert "launch_rocket" in payload["message"]
    observation = rejection.to_observation()
    assert observation["status"] != "success"
    assert observation["executed"] is False


@pytest.mark.parametrize(
    "name,args",
    [
        ("confirm_course", {}),
        ("query_papers", {"course": "Python", "unknown_field": 1}),
        ("select_question", {"number_label": 3}),
        ("explain_problem", {"mode": "explode"}),
        ("ask_user", {"question": ""}),
    ],
)
def test_invalid_arguments_are_rejected_with_reason(name: str, args: dict) -> None:
    """缺必填、多字段、类型错、枚举外取值都归为 INVALID_ARGUMENTS。"""

    plan, rejection = parse_tool_call(name, args, tool_call_id="c4")
    assert plan is None
    assert rejection is not None
    assert rejection.code == "INVALID_ARGUMENTS"
    assert rejection.message.strip()
    assert rejection.tool == name


def test_tool_label_is_chinese_and_falls_back_to_raw_name() -> None:
    assert tool_label("query_papers") == "查找试卷"
    assert tool_label("unknown_tool") == "unknown_tool"


def test_public_details_whitelist_blocks_answer_material() -> None:
    """回传给模型与正文的细节必须过滤，答案/参考解答不能进入公开载荷。"""

    filtered = public_details(
        {
            "course": "Python",
            "count": 3,
            "reference_solution_text": "答案是 B",
            "answer": "B",
            "solution": "先求导",
            "student_answer_text": "A",
        }
    )
    assert filtered == {"course": "Python", "count": 3}


def test_tool_lookup_table_matches_specs() -> None:
    assert set(TOOLS_BY_NAME) == {spec.name for spec in TOOL_SPECS}
    assert len(TOOLS_BY_NAME) == 14
    assert TOOLS_BY_NAME["query_papers"].action == "query_papers"