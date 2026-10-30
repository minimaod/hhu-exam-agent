"""针对真实原卷的精确回归断言（导师核对过的题号与选项）。

只读原始 PDF，断言解码结果；PDF 不在时自动跳过，不影响常规回归。
这些用例对应导师指出的五道具体题目，以及"第 23 空 = 'g'"的空号校正。
"""

from __future__ import annotations

from pathlib import Path
import ast

import pytest

from tools.question_bank import common as C

pytest.importorskip("pymupdf")

from tools.question_bank import extract as E  # noqa: E402

CATALOG = C.load_catalog()
PAPER_ROOT = C.PAPER_ROOT
OVERRIDES_PATH = C.QB_ROOT / "overrides.json"


def _paper(paper_id: str) -> dict:
    item = [entry for entry in CATALOG if entry["id"] == paper_id][0]
    if not C.paper_pdf_path(item, PAPER_ROOT).exists():
        pytest.skip(f"缺少原始 PDF：{paper_id}")
    overrides = C.read_json(OVERRIDES_PATH) if OVERRIDES_PATH.exists() else {}
    item["_overrides"] = overrides.get(paper_id, {})
    return item


def _extract(paper_id: str):
    item = _paper(paper_id)
    records, extra, _issues = E.extract_paper(item, pdf_path=C.paper_pdf_path(item, PAPER_ROOT))
    return records


@pytest.fixture(scope="module")
def midterm_2020() -> dict[int, dict]:
    records = _extract("py-2020-2021-s1-midterm")
    return {
        int(record["number_label"]): record
        for record in records
        if record["section"]["code"] == C.SECTION_CHOICE
    }


@pytest.fixture(scope="module")
def midterm_2022() -> dict[int, dict]:
    records = _extract("py-2022-2023-s2-midterm")
    return {
        int(record["number_label"]): record
        for record in records
        if record["section"]["code"] == C.SECTION_CHOICE
    }


# ----------------------------------------------------------------------
# 导师核对过的五道选择题
# ----------------------------------------------------------------------


@pytest.mark.parametrize("number,expected", [(2, "C"), (3, "B"), (4, "B")])
def test_2020_midterm_choice_decoded(midterm_2020, number, expected):
    record = midterm_2020[number]
    assert record["answer"]["text"] == expected
    assert record["answer"]["state"] == C.ANSWER_ORIGINAL


@pytest.mark.parametrize("number,expected", [(1, "A"), (2, "D")])
def test_2022_s2_midterm_choice_decoded(midterm_2022, number, expected):
    record = midterm_2022[number]
    assert record["answer"]["text"] == expected
    assert record["answer"]["state"] == C.ANSWER_ORIGINAL


# ----------------------------------------------------------------------
# 第 23 空：印刷标号 17 → 规范空号 23
# ----------------------------------------------------------------------


def test_blank_23_uses_printed_label_17():
    records = _extract("py-2020-2021-s1-midterm")
    target = [
        record
        for record in records
        if record["section"]["code"] == C.SECTION_FILL and 23 in record["blank_marks"]
    ][0]
    assert target["answer"]["text"].endswith("(23) 'g'")
    assert target["answer"]["state"] == C.ANSWER_ORIGINAL
    corrections = target["label_corrections"]
    assert corrections and corrections[0]["printed"] == 17
    assert corrections[0]["author"] == "mentor"
    # 原卷 PDF 不被修改：证据只来自答案纸与批注页
    assert corrections[0]["page"] == 8
    assert 14 in corrections[0]["evidence_pages"]


def test_blank_23_statement_is_clean():
    """第 15 题题干取自 p7 干净题面，不能混入答案或解析。"""

    records = _extract("py-2020-2021-s1-midterm")
    target = [
        record
        for record in records
        if record["section"]["code"] == C.SECTION_FILL and 23 in record["blank_marks"]
    ][0]
    statement = target["statement"]
    # 题干本身就含 ['abc','12','g','8']，所以要查的是"只出现在解析页"的内容
    assert "解析" not in statement
    assert "按第一个字符的ascii码排序" not in statement
    assert "['abc','12','g','8']" in statement
    assert 7 in target["pages"]["statement"]


# ----------------------------------------------------------------------
# 两栏代码：不交错、按栏按块切分
# ----------------------------------------------------------------------


def _code_records(paper_id: str) -> dict[int, dict]:
    return {
        int(record["number_label"]): record
        for record in _extract(paper_id)
        if record["section"]["code"] == C.SECTION_CODE
    }


def test_code_blocks_do_not_interleave_left_and_right_columns():
    code = _code_records("py-2020-2021-s1-final")[1]
    blocks = code["code_blocks"]
    assert [block["kind"] for block in blocks] == ["function", "main"]
    function_text = blocks[0]["text"]
    main_text = blocks[1]["text"]
    # 函数里不能混进主程序的循环与计数
    assert "for i in range(1,1000)" not in function_text
    assert "count=0" not in function_text
    # 主程序里不能混进函数定义
    assert "def fun" not in main_text
    assert blocks[0]["column"] == 0 and blocks[1]["column"] == 1


def test_code_two_reference_methods_stay_apart():
    code = _code_records("py-2020-2021-s1-final")[2]
    blocks = code["code_blocks"]
    assert [block["method"] for block in blocks] == [1, 1, 2, 2]
    assert [block["kind"] for block in blocks] == ["function", "main", "function", "main"]
    assert "d[chr(ord('a')+i)]" not in blocks[0]["text"]


def test_code_variants_in_s2_are_separated():
    code = _code_records("py-2022-2023-s2-midterm")[1]
    blocks = code["code_blocks"]
    assert len(blocks) >= 2
    # 每个变体都以 k=0 重新开始，说明没有被粘成一段
    assert all(block["text"].splitlines()[0].strip() == "k=0" for block in blocks[:2])


def test_s2_prime_programs_exclude_margin_notes():
    code = _code_records("py-2022-2023-s2-midterm")[1]
    assert len(code["code_blocks"]) == 3
    for block in code["code_blocks"]:
        ast.parse(block["text"])
        assert all(note not in block["text"] for note in ["注意range参数", "素数判断", "输出格式控制"])
    assert code["code_annotations"][0]["text"].splitlines() == [
        "注意range参数", "素数判断", "输出格式控制"
    ]
    assert code["reference_code"] == code["code_blocks"][0]["text"]


def test_s2_random_list_methods_keep_their_own_columns():
    code = _code_records("py-2022-2023-s2-midterm")[2]
    blocks = {block["block_id"]: block for block in code["code_blocks"]}
    assert set(blocks) == {"original", "shared", "method1", "method2", "method3", "method4", "method5", "set"}
    for block in blocks.values():
        ast.parse(block["text"])
        assert block["page"] == 10
        assert block["extraction"] == "reviewed_region"
    fourth, fifth = blocks["method4"]["text"], blocks["method5"]["text"]
    assert "if num not in lst4:" in fourth and "lst4.append(num)" in fourth
    assert "lst5" not in fourth
    assert "if num in lst5:" in fifth and "continue" in fifth and "lst5.append(num)" in fifth
    assert "lst4" not in fifth
    assert blocks["shared"]["text"] == "from random import *\nn=10"
    assert all(blocks[key]["depends_on"] == ["shared"] for key in blocks if key not in {"original", "shared"})
    assert code["reference_code"] == blocks["original"]["text"]
    assert blocks["set"]["text"].endswith("print(lsS)")
    assert not code["independent_solutions"]


def test_code_region_rule_rejects_different_pdf():
    with pytest.raises(ValueError, match="哈希"):
        E.apply_code_regions([], {}, {"pdf_sha256": "expected"}, "changed")


# ----------------------------------------------------------------------
# 空号校正函数本身
# ----------------------------------------------------------------------


def test_apply_fill_label_fixes_maps_second_occurrence():
    fill_all = {22: {"text": "'abc'", "page": 8}}
    problems = [
        {"kind": "fill_answer_duplicate_number", "number": 17, "page": 8, "text": "'g'"}
    ]
    fixes = [{"printed": 17, "canonical": 23, "occurrence": 2, "basis": "批注页确认"}]
    applied = E.apply_fill_label_fixes(fill_all, problems, fixes)
    assert fill_all[23]["text"] == "'g'"
    assert fill_all[23]["printed_label"] == 17
    assert applied[0]["canonical"] == 23
    # 不覆盖已存在的规范空号
    assert fill_all[22]["text"] == "'abc'"


def test_apply_fill_label_fixes_skips_when_occurrence_missing():
    fill_all: dict = {}
    assert E.apply_fill_label_fixes(fill_all, [], [{"printed": 17, "canonical": 23}]) == []
    assert fill_all == {}
