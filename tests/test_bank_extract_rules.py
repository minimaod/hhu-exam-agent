"""题库抽取工具的纯逻辑测试。

这些用例不读 PDF，只验证切分/判定规则，避免把"某份卷能跑通"当成正确性。
涉及 PDF 渲染的几何解码不在这里测（需要真实页面），由抽取产物的
`grid` 元数据与复核工作表体现。
"""

from __future__ import annotations

import pytest

from tools.question_bank import common as C


# ----------------------------------------------------------------------
# 文本清洗与页眉页脚
# ----------------------------------------------------------------------


def test_normalize_fullwidth_digits_and_parens():
    assert C.normalize_text("（15）") == "(15)"
    assert C.normalize_text("１２３") == "123"


def test_clean_line_collapses_spaces():
    assert C.clean_line("  A.  20     B. 56 ") == "A. 20 B. 56"


def test_page_decl_requires_pair_even_on_separate_lines():
    text = "2022~2023 学年第二学期\n第1页\n共8页\n"
    assert C.parse_page_decl(text) == (1, 8)


def test_page_decl_rejects_incomplete():
    assert C.parse_page_decl("第1页") is None
    assert C.parse_page_decl("第9页 共8页") is None


def test_running_line_matches_page_decl_but_not_bare_number():
    assert C.is_running_line("第3页  共8页")
    # 答题纸网格里的题号是裸数字，不能当页码丢掉
    assert not C.is_running_line("14")


def test_page_number_only_near_top_or_bottom():
    assert C.is_page_number("14", (90, 30, 100, 40), 842)
    assert C.is_page_number("14", (90, 800, 100, 810), 842)
    assert not C.is_page_number("14", (95.8, 207.8, 110, 216), 842)


def test_header_line_recognises_year_course_and_cohort():
    assert C.is_header_line("2023~2024 学年第一学期《Python 语言程序设计》期中考试试卷", (100, 43, 400, 60))
    assert C.is_header_line("（2023 级水文、水工、港航、土木等专业）", (150, 57, 400, 70))
    # 正文里的《》不作为页眉
    assert not C.is_header_line("《Python 语言程序设计》", (100, 400, 400, 420))


def test_header_signature_extracts_year_and_cohort():
    signature = C.header_signature(
        [
            "2022~2023 学年第二学期《Python 语言程序设计》期中考试试卷",
            "（2021 级财务、人力、信息、农水等专业）",
            "第1页 共2页",
        ]
    )
    assert signature["academic_year"] == "2022-2023"
    assert signature["cohort"] == "2021"
    assert signature["course_line"] == "《Python 语言程序设计》"


# ----------------------------------------------------------------------
# 大题与小题
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("一、选择题 (用铅笔把选中的方格涂黑 30×1 分)", C.SECTION_CHOICE),
        ("二、 填 空 题（按空填写）", C.SECTION_FILL),
        ("三、编程题(20 分)", C.SECTION_CODE),
        ("1. 以下叙述中，正确的是（ ）。", None),
    ],
)
def test_section_of(line, expected):
    assert C.section_of(line) == expected


def test_split_question_number():
    assert C.split_question_number("12. 设有 a,b=4,2") == (12, "设有 a,b=4,2")
    assert C.split_question_number("13.下面程序输出的结果是") == (13, "下面程序输出的结果是")
    assert C.split_question_number("A. 20") is None


def test_blank_marks_ignores_function_calls():
    text = "Ls.insert(2,3) 与 print(Ls.index('3')) 的结果是 (11) ，另一空 ( 15 ) 。"
    # 只认独立括号里的数字，(2,3) 与 ('3') 都不算
    assert C.blank_marks(text) == [11, 15]


def test_blank_marks_fullwidth():
    assert C.blank_marks("第一个空（3），第二个空 (4)") == [3, 4]


# ----------------------------------------------------------------------
# 选项切分
# ----------------------------------------------------------------------


def test_split_options_inline():
    stem, options = C.split_options(
        '下列选项中，使用bool()函数测试，值不是False的是( )。 A.0  B.[]  C. "False"  D. ""'
    )
    assert "bool()" in stem
    assert [item["label"] for item in options] == ["A", "B", "C", "D"]
    assert options[2]["text"] == '"False"' or options[2]["text"] == '"False"'


def test_split_options_multiline_with_code():
    text = "下面程序的输出结果是（ ）。\nx=2.5\nprint(complex(round(x)))\nA.(2+0j)  B.(3+0j)  C.(2+0j)  D.运行出错"
    stem, options = C.split_options(text)
    assert "print(complex" in stem
    assert [item["label"] for item in options] == ["A", "B", "C", "D"]


def test_split_options_returns_empty_when_no_chain():
    stem, options = C.split_options("这题没有选项")
    assert options == []
    assert stem == "这题没有选项"


def test_split_options_two_column_paired_layout():
    """A/C 同一行、B/D 同一行的两栏选项按标号归位。"""

    text = (
        "以下代码绘制的图形是( )。\n"
        "from turtle import *\n"
        "for i in range(10,100,20):\n"
        "    circle(i)\n"
        "A. 五个内切圆          C. 六个内切圆\n"
        "B. 五个同心圆          D. 六个同心圆"
    )
    stem, options = C.split_options(text)
    assert "circle(i)" in stem
    assert [item["label"] for item in options] == ["A", "B", "C", "D"]
    assert options[0]["text"] == "五个内切圆"
    assert options[1]["text"] == "五个同心圆"
    assert options[2]["text"] == "六个内切圆"
    assert options[3]["text"] == "六个同心圆"


def test_split_options_does_not_force_paired_layout_on_repeated_letters():
    """字母重复时不能按“唯一标号”规则硬拆。"""

    text = "下面程序输出是( )。\nA. 1\nA. 2\nC. 3\nD. 4"
    _stem, options = C.split_options(text)
    assert len(options) < 4


def test_looks_like_code():
    assert C.looks_like_code("    print(x)")
    assert C.looks_like_code("s = input()")
    assert not C.looks_like_code("以下程序输出的结果是")


# ----------------------------------------------------------------------
# 答案状态四值
# ----------------------------------------------------------------------


def test_state_missing_when_no_source():
    assert C.resolve_answer_state([])[0] == C.ANSWER_MISSING


def test_state_original_when_single_verified_source():
    source = C.AnswerSource(kind="grid_mark", text="D", pages=[9], pairing_verified=True)
    state, text, note = C.resolve_answer_state([source])
    assert (state, text, note) == (C.ANSWER_ORIGINAL, "D", None)


def test_state_conflict_when_sources_disagree():
    sources = [
        C.AnswerSource(kind="grid_mark", text="D", pages=[9], pairing_verified=True),
        C.AnswerSource(kind="red_annotation", text="B", pages=[10], pairing_verified=True),
    ]
    state, text, note = C.resolve_answer_state(sources)
    assert state == C.ANSWER_CONFLICT
    assert text is None
    assert "矛盾" in note


def test_state_pending_when_pairing_unverified():
    sources = [
        C.AnswerSource(kind="layout_text", text="2", pages=[12], pairing_verified=False)
    ]
    state, text, note = C.resolve_answer_state(sources)
    assert state == C.ANSWER_PENDING
    assert text == "2"
    assert "尚未确认" in note


def test_state_original_when_one_of_two_verified():
    sources = [
        C.AnswerSource(kind="grid_mark", text="C", pages=[9], pairing_verified=True),
        C.AnswerSource(kind="red_annotation", text="C", pages=[10], pairing_verified=False),
    ]
    assert C.resolve_answer_state(sources)[0] == C.ANSWER_ORIGINAL


def test_choose_primary_prefers_verified_then_grid():
    sources = [
        C.AnswerSource(kind="red_annotation", text="B", pages=[10], pairing_verified=False),
        C.AnswerSource(kind="grid_mark", text="B", pages=[9], pairing_verified=True),
        C.AnswerSource(kind="typed_list", text="B", pages=[9], pairing_verified=True),
    ]
    assert C.choose_primary(sources).kind == "grid_mark"


# ----------------------------------------------------------------------
# 填空答案页的切分（含原卷编号缺陷）
# ----------------------------------------------------------------------


def _fake_line(page: int, text: str, y: float) -> object:
    from tools.question_bank.extract import Line

    return Line(page=page, text=text, bbox=(80.0, y, 400.0, y + 12), red=False)


def _fake_copy(lines, grid_answers=None):
    from tools.question_bank.extract import Copy, PageData

    page = PageData(
        index=1,
        width=595,
        height=842,
        lines=lines,
        running_lines=[],
        letters=[],
        numbers=[],
        header={},
        drawing_count=0,
        page_decl=(1, 1),
    )
    page.grid_answers = grid_answers or {}
    return Copy(start_page=1, pages=[page], declared_total=1)


def test_typed_fill_answers_pairs_and_inline():
    pytest.importorskip("pymupdf")
    from tools.question_bank.extract import typed_fill_answers

    lines = [
        _fake_line(1, "二、填空题", 100),
        _fake_line(1, "答案", 110),  # 让页面被识别为答案页
        _fake_line(1, "1.", 120),
        _fake_line(1, "wel", 130),
        _fake_line(1, "2. 4", 140),
        _fake_line(1, "3.", 150),
        _fake_line(1, "OK!", 160),
    ]
    answers, problems = typed_fill_answers(_fake_copy(lines, grid_answers={1: {}}))
    assert answers[1]["text"] == "wel"
    assert answers[2]["text"] == "4"
    assert answers[3]["text"] == "OK!"
    assert problems == []


def test_typed_fill_answers_does_not_read_decimal_as_item():
    pytest.importorskip("pymupdf")
    from tools.question_bank.extract import typed_fill_answers

    lines = [
        _fake_line(1, "二、填空题", 100),
        _fake_line(1, "1.", 110),
        _fake_line(1, "2.0", 120),  # 是第 1 空的答案，不是第 2 题
        _fake_line(1, "2.", 130),
        _fake_line(1, "<", 140),
    ]
    answers, problems = typed_fill_answers(_fake_copy(lines, grid_answers={1: {}}))
    assert answers[1]["text"] == "2.0"
    assert answers[2]["text"] == "<"
    assert problems == []


def test_typed_fill_answers_records_original_numbering_defect():
    pytest.importorskip("pymupdf")
    from tools.question_bank.extract import typed_fill_answers

    lines = [
        _fake_line(1, "二、填空题", 100),
        _fake_line(1, "17.", 105),
        _fake_line(1, "6", 108),
        _fake_line(1, "22.", 110),
        _fake_line(1, "'abc'", 120),
        _fake_line(1, "17.", 130),  # 原卷把 23 写成 17，与前面的 17 重复
        _fake_line(1, "'g'", 140),
        _fake_line(1, "24.", 150),
        _fake_line(1, "'0'", 160),
    ]
    answers, problems = typed_fill_answers(_fake_copy(lines, grid_answers={1: {}}))
    kinds = {problem["kind"] for problem in problems}
    assert "fill_answer_duplicate_number" in kinds
    assert "fill_answer_number_gap" in kinds
    # 不擅自改题号：重复出现时保留第一次的文本
    assert answers[17]["text"] == "6"


def test_typed_fill_answers_shares_text_for_merged_blank_labels():
    pytest.importorskip("pymupdf")
    from tools.question_bank.extract import typed_fill_answers

    lines = [
        _fake_line(1, "二、填空题", 100),
        _fake_line(1, "13.14.", 110),
        _fake_line(1, "整型，字符串，元组任选2个", 120),
    ]
    answers, _problems = typed_fill_answers(_fake_copy(lines, grid_answers={1: {}}))
    assert answers[13]["text"] == answers[14]["text"] == "整型，字符串，元组任选2个"


def test_embedded_fill_answers_extracts_suffix():
    pytest.importorskip("pymupdf")
    from tools.question_bank.extract import embedded_fill_answers

    statements = {
        1: [
            "若x=5,y=2,z=3,则表达式x and y or z 的值为：",
            "若x=5,y=2,z=3,则表达式x and y or z 的值为： 2\n解析：数值逻辑运算",
        ]
    }
    found = embedded_fill_answers(statements)
    assert found[1]["text"] == "2"


def test_cohort_mismatch_flags_different_cohorts():
    pytest.importorskip("pymupdf")
    from tools.question_bank.extract import cohort_mismatch

    headers = {1: {"cohort": "2022"}, 9: {"cohort": "2021"}}
    assert cohort_mismatch(headers, [1, 2], [9]) is not None
    assert cohort_mismatch(headers, [1, 2], [2]) is None
    assert cohort_mismatch({}, [1], [9]) is None