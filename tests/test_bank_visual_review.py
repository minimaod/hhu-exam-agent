"""视觉复核只做逐题比对与建议，不替导师下结论。"""

from __future__ import annotations

import json
from pathlib import Path

from tools.question_bank.visual_compare import (
    classify,
    compare_code,
    compare_text,
    embedded_code,
    len_literal_check,
    next_question_contamination,
)
from tools.question_bank.visual_review import (
    _payload_without_solutions,
    _merge_parsed,
    _trailing_math_fragment,
    build_jobs,
    suggest_corrections,
)


def test_compare_text_rejects_unconfirmed_extra_blank_marker():
    check = compare_text("len('x') 的值为", "len('x') 的值为  （2）")
    assert check["ok"] is False
    assert check["extra_tokens"]


def test_compare_text_accepts_whitelisted_whitespace_and_quotes():
    check = compare_text("len('x') 的值为", " len(\"x\") 的值为 ")
    assert check["ok"] is True
    assert check["format_only"] is True


def test_compare_text_missing_token_is_hard_failure():
    check = compare_text("print 输出结果", "输出结果")
    assert check["ok"] is False
    assert check["missing_tokens"]


def test_compare_code_placeholder_and_quote_normalization():
    check = compare_code(
        ["__blank_15__", "with open('score1.csv', 'r') as file:", "    print(data)"],
        '(15)\nwith open("score1.csv", "r") as file:\n    print(data)',
    )
    assert check["tokens_equal"] is True
    assert check["ok"] is True


def test_compare_code_detects_lost_indentation():
    check = compare_code(["for i in range(3):", "    print(i)"], "for i in range(3):\nprint(i)")
    assert check["tokens_equal"] is True
    assert check["indent_equal"] is False
    assert check["format_only"] is True


def test_compare_text_rejects_negation_polarity_change():
    check = compare_text("下列选项中是可变对象的是", "下列选项中是不可变对象的是")
    assert check["ok"] is False
    assert check["semantic_diff"] is True


def test_compare_text_rejects_operator_parenthesis_change():
    check = compare_text("a/(b+c)", "a/b+c")
    assert check["ok"] is False
    assert "(" in check["missing_tokens"] or ")" in check["missing_tokens"]


def test_compare_text_rejects_nested_bracket_loss_even_when_ratio_high():
    check = compare_text(
        "[1,2,3,[],[[4,5,6],[7,8,9]]]",
        "[1,2,3,[],[4,5,6],[7,8,9]]",
    )
    assert check["ok"] is False
    assert "[" in check["missing_tokens"]
    assert "]" in check["missing_tokens"]


def test_compare_text_detects_second_negation_loss():
    check = compare_text(
        "以下不正确的说法是：列表是不可变对象",
        "以下不正确的说法是：列表是可变对象",
    )
    assert check["ok"] is False
    assert check["semantic_diff"] is True
    assert check["negation_missing"] == ["不"]


def test_compare_text_detects_operator_order_change_with_same_counts():
    check = compare_text("a/(b)+c", "a/(b+c)")
    assert check["ok"] is False
    assert check["operator_order_diff"] is True


def test_compare_text_rejects_extra_minus_in_fill_09():
    check = compare_text(
        "已知 x = [1,2,3,4,5,6,7]，那么执行语句 x[::3] = range(3) 之后，x 的值为 (9)。",
        "已知 x = [1,2,3,4,5,6,7]，那么执行语句 x[::-3] = range(3) 之后，x 的值为____(9)____。",
    )
    assert check["ok"] is False
    assert check["extra_tokens"] == ["-"]


def test_compare_text_rejects_extra_plus_one():
    check = compare_text("x = range(3)", "x = range(3) + 1")
    assert check["ok"] is False
    assert "+" in check["extra_tokens"]
    assert "1" in check["extra_tokens"]


def test_compare_text_rejects_changed_number_inside_call():
    check = compare_text("f(2)", "f(3)")
    assert check["ok"] is False
    assert "2" in check["missing_tokens"]
    assert "3" in check["extra_tokens"]


def test_compare_text_rejects_changed_number_in_expression():
    check = compare_text("x = 2 + (3)", "x = 2 + (9)")
    assert check["ok"] is False
    assert check["full_text_equal"] is False


def test_compare_text_rejects_changed_number_inside_string():
    check = compare_text('s = " (2) "', 's = " (8) "')
    assert check["ok"] is False
    assert check["string_missing"] or check["string_extra"]


def test_solution_only_change_does_not_alter_visual_text_payload():
    base = {
        "question_id": "q",
        "paper_id": "p",
        "course": "c",
        "section": {"code": "choice"},
        "number_label": "1",
        "question_type": "choice",
        "statement": "题干",
        "options": [],
        "code_lines": [],
        "blank_marks": [],
        "pages": {"statement_all": [1]},
        "answer": {"state": "missing", "text": None},
        "independent_solutions": [],
    }
    with_solution = dict(base)
    with_solution["independent_solutions"] = [
        {"author": "Harness", "text": "B", "note": "独立解答"}
    ]
    assert _payload_without_solutions(base) == _payload_without_solutions(with_solution)


def test_compare_text_rejects_chinese_content_change():
    for expected, observed in (
        ("列表对象", "元组对象"),
        ("按升序排列", "按降序排列"),
        ("目的只是为了增加代码复用", "目的是为了增加代码复用"),
        ("使用函数的目的只是为了增加代码复用", "使用函数的目的是为了增加代码复用"),
    ):
        check = compare_text(expected, observed)
        assert check["ok"] is False
        assert check["full_text_equal"] is False


def test_compare_text_rejects_identifier_order_change():
    check = compare_text("a + b", "b + a")
    assert check["ok"] is False
    assert check["token_order_diff"] is True


def test_compare_text_rejects_string_content_space_change():
    check = compare_text('len("python\\t 编程")', 'len("python\\t编程")')
    assert check["ok"] is False
    assert check["string_missing"] or check["string_extra"]


def test_embedded_code_preserves_indentation_and_detects_loss():
    statement = "下面代码的输出是（ ）。\ndef change(a,b):\n    a=10\n    b+=a"
    segment = embedded_code(statement)
    assert "def change" in segment
    assert compare_code(segment, "def change(a,b):\na=10\nb+=a")["indent_equal"] is False


def test_len_literal_check_is_narrow_and_catches_answer_mismatch():
    check = len_literal_check('len("python\\t 编程")的值为 （2）', ["9"])
    assert check is not None
    assert check["actual"] == 10
    assert check["ok"] is False
    assert len_literal_check("普通题面", ["9"]) is None


def test_next_question_contamination_is_explicit():
    assert next_question_contamination("stem\n4. next question body", "4", "next question body")
    assert not next_question_contamination("stem\n4. next question body", "5", "other body")


def test_classify_never_labels_uncertain_as_pass():
    conclusion, issues = classify(
        isolated=False,
        missing_answer=False,
        model_ok=True,
        confidence="medium",
        uncertain=["第 3 题代码跨页"],
        statement=compare_text("题干", "题干"),
        options=None,
        code=None,
        blanks=None,
        answer=None,
        contamination=False,
    )
    assert conclusion == "needs_review"
    assert any("不确定" in item for item in issues)


def test_merge_parsed_keeps_longer_statement_and_option():
    merged = _merge_parsed(
        "statement",
        [
            {"questions": [{"number": "2", "statement": "短", "options": []}]},
            {"questions": [{"number": "2", "statement": "更长的题干", "options": [{"label": "A", "text": "x"}]}]},
        ],
    )
    assert merged["questions"][0]["statement"] == "更长的题干"
    assert merged["questions"][0]["options"][0]["label"] == "A"


def test_trailing_math_fragment_and_correction_are_reproducible(tmp_path):
    record = {
        "question_id": "q1",
        "paper_id": "p1",
        "question_type": "fill",
        "statement": "len('x') 的值为 （2）\n−b+√b²−4ac",
        "content_sha256": "a" * 64,
        "pages_statement": [7],
        "pages_answer": [10],
        "answer_json": {"state": "original", "text": "9"},
    }
    cleaned = _trailing_math_fragment(record["statement"])
    assert cleaned == "len('x') 的值为 （2）"
    inventory = {
        "build_id": "iv-test",
        "records": {"q1": record},
        "papers": {"p1": {"pdf_sha256": "b" * 64}},
    }
    results = [
        {
            "question_id": "q1",
            "result": "needs_review",
            "isolated": False,
            "missing_answer": False,
            "issues": ["题干不一致"],
            "observed": {"statement": "len('x') 的值为 ____(2)____"},
            "vision": {"model": "vision-test", "confidence": "high"},
        }
    ]
    items = suggest_corrections(inventory, tmp_path, results)
    assert len(items) == 1
    assert items[0]["before_content_sha256"] == "a" * 64
    assert items[0]["patch"] == {"statement": cleaned}
    assert items[0]["mentor_review_required"] is True
    assert items[0]["applied"] is False
    saved = json.loads((tmp_path / "corrections.json").read_text(encoding="utf-8"))
    assert saved["corrections"][0]["patch_sha256"] == items[0]["patch_sha256"]


def test_build_jobs_adds_cross_page_question_job():
    inventory = {
        "bank_root": ".",
        "papers": {"p1": {"title": "卷 1"}},
        "remaining": ["q1"],
        "records": {
            "q1": {
                "question_id": "q1",
                "paper_id": "p1",
                "question_type": "choice",
                "number_label": "10",
                "pages_statement": [3, 4],
                "pages_answer": [10],
                "answer_json": {"state": "original"},
                "blank_marks": [],
            }
        },
    }
    jobs = build_jobs(inventory, ids=["q1"], single=True)
    roles = {job["role"] for job in jobs}
    assert "statement_question" in roles
    assert "answer" in roles
    cross = next(job for job in jobs if job["role"] == "statement_question")
    assert cross["pages"] == [3, 4]