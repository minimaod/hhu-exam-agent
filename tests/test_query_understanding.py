"""理解结果模型与 JSON 解析的边界。

覆盖任务书要求的空内容、坏 JSON、截断、非法字段/枚举和组合约束；
任何一项失败都必须得到 MODEL_RESPONSE_INVALID，而不是空结果或全部试卷。
"""

import pytest

from app.querying.errors import QueryError
from app.querying.models import Understanding
from app.querying.prompt import parse_understanding


def test_search_understanding_is_parsed() -> None:
    result = parse_understanding(
        '{"action":"search","course":"高数","exam_type":null,"reason":null}'
    )

    assert result == Understanding(
        action="search",
        course="高数",
        exam_type=None,
        reason=None,
    )


def test_course_whitespace_is_stripped() -> None:
    result = parse_understanding(
        '{"action":"search","course":"  高等数学 AⅠ  ","exam_type":"final","reason":null}'
    )

    assert result.course == "高等数学 AⅠ"
    assert result.exam_type == "final"


@pytest.mark.parametrize(
    ("raw", "action", "reason"),
    [
        (
            '{"action":"clarify","course":null,"exam_type":null,"reason":"missing_course"}',
            "clarify",
            "missing_course",
        ),
        (
            '{"action":"clarify","course":null,"exam_type":null,"reason":"ambiguous_course"}',
            "clarify",
            "ambiguous_course",
        ),
        (
            '{"action":"unsupported","course":"高数","exam_type":null,"reason":"unsupported_constraint"}',
            "unsupported",
            "unsupported_constraint",
        ),
        (
            '{"action":"unsupported","course":"高数","exam_type":null,"reason":"out_of_scope"}',
            "unsupported",
            "out_of_scope",
        ),
    ],
)
def test_non_search_understandings_are_parsed(
    raw: str,
    action: str,
    reason: str,
) -> None:
    result = parse_understanding(raw)

    assert result.action == action
    assert result.reason == reason


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "这不是 JSON",
        "[]",
        '"search"',
        '{"action":"search","course":"高数","exam_type":null,"reason":null',
        '{"action":"search","course":null,"exam_type":null,"reason":null}',
        '{"action":"search","course":"   ","exam_type":null,"reason":null}',
        '{"action":"search","course":"高数","exam_type":null}',
        '{"action":"search","course":"高数","exam_type":null,"reason":null,"extra":1}',
        '{"action":"lookup","course":"高数","exam_type":null,"reason":null}',
        '{"action":"search","course":"高数","exam_type":"quiz","reason":null}',
        '{"action":"search","course":"高数","exam_type":null,"reason":"missing_course"}',
        '{"action":"clarify","course":null,"exam_type":null,"reason":null}',
        '{"action":"clarify","course":null,"exam_type":null,"reason":"unsupported_constraint"}',
        '{"action":"unsupported","course":"高数","exam_type":null,"reason":"missing_course"}',
    ],
)
def test_invalid_model_output_becomes_query_error(raw: str | None) -> None:
    with pytest.raises(QueryError) as excinfo:
        parse_understanding(raw)

    assert excinfo.value.code == "MODEL_RESPONSE_INVALID"
    assert "约定" in excinfo.value.message
