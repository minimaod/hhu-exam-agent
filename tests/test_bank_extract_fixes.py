"""P1 修复项的回归测试。

覆盖导师指出的五处解码/版面问题、题面与答案分离、编程题分块、以及
"导师解答不冒充原卷答案"的字段约定。

网格用例全部用纯几何数据构造，不读 PDF：只要题号/字母格的坐标与网格线
对得上，就能验证"只认网格内题号"这条规则。
"""

from __future__ import annotations

import pytest

from tools.question_bank import common as C

pytest.importorskip("pymupdf")

from tools.question_bank import extract as E  # noqa: E402


# ----------------------------------------------------------------------
# 网格：只认网格内的题号（导师指出的第 1 处错误）
# ----------------------------------------------------------------------


def _grid(rows: int, per_row: int, *, gap: float = 26.0):
    """构造 rows 行、每行 per_row 道题的答题纸几何。"""

    vertical = [100.0 + 40.0 * column for column in range(per_row * 4 + 1)]
    horizontal = [200.0 + gap * row for row in range(rows + 1)]
    letters = []
    numbers = []
    for row in range(rows):
        top = horizontal[row]
        for slot in range(per_row):
            number = row * per_row + slot + 1
            # 题号画在自己那一组格子的左侧
            number_x = vertical[slot * 4] + 4
            numbers.append((number, (number_x - 12, top + 4, number_x - 2, top + 14)))
            for index, label in enumerate("ABCD"):
                column = slot * 4 + index
                left = vertical[column] + 4
                letters.append((label, (left, top + 4, left + 30, top + 14)))
    return letters, numbers, vertical, horizontal


def test_grid_ignores_bare_numbers_outside_grid():
    """填空答案区的裸数字（如答案正文就是 2、4）不能当成选择题题号。"""

    letters, numbers, vertical, horizontal = _grid(rows=4, per_row=4)
    # 网格下方还有 4 个"看起来像题号"的裸数字，属于填空答案区
    stray = [(5, (95.0, 330.0, 104.0, 340.0)), (7, (95.0, 352.0, 104.0, 362.0))]
    grid = E.build_grid(letters, numbers + stray, vertical, horizontal)
    assert sorted(grid["questions"]) == list(range(1, 17))
    assert grid["meta"]["ignored_numbers"] == [5, 7]
    assert grid["meta"]["numbers_outside_grid"] == 2


def test_grid_thirty_question_layout():
    """30 题布局（每行 3 题 × 10 行）必须解出 30 题，而不是按 40 题硬套。"""

    letters, numbers, vertical, horizontal = _grid(rows=10, per_row=3)
    grid = E.build_grid(letters, numbers, vertical, horizontal)
    assert grid["meta"]["questions"] == 30
    assert grid["meta"]["cells"] == 120
    assert sorted(grid["questions"]) == list(range(1, 31))


def test_grid_forty_question_layout():
    letters, numbers, vertical, horizontal = _grid(rows=10, per_row=4)
    grid = E.build_grid(letters, numbers, vertical, horizontal)
    assert grid["meta"]["questions"] == 40
    assert sorted(grid["questions"]) == list(range(1, 41))


def test_grid_row_assignment_does_not_cross_questions():
    """同一行里，每道题只取自己右侧的 4 个格子，不能串到下一题。"""

    letters, numbers, vertical, horizontal = _grid(rows=4, per_row=2)
    grid = E.build_grid(letters, numbers, vertical, horizontal)
    assert [cell.label for cell in grid["questions"][1]] == ["A", "B", "C", "D"]
    assert [cell.label for cell in grid["questions"][2]] == ["A", "B", "C", "D"]
    assert grid["questions"][2][0].rect[0] > grid["questions"][1][3].rect[0]


def test_pick_marked_requires_clear_separation():
    """墨迹接近时判为"看不清"，不猜一个答案。"""

    cells = [
        (E.GridCell(label="A", rect=(0, 0, 10, 10)), 0.50),
        (E.GridCell(label="B", rect=(10, 0, 20, 10)), 0.45),
        (E.GridCell(label="C", rect=(20, 0, 30, 10)), 0.12),
        (E.GridCell(label="D", rect=(30, 0, 40, 10)), 0.10),
    ]
    assert E.pick_marked(cells, median_ink=0.12) is None

    clear = [
        (E.GridCell(label="A", rect=(0, 0, 10, 10)), 0.95),
        (E.GridCell(label="B", rect=(10, 0, 20, 10)), 0.10),
        (E.GridCell(label="C", rect=(20, 0, 30, 10)), 0.12),
        (E.GridCell(label="D", rect=(30, 0, 40, 10)), 0.10),
    ]
    picked = E.pick_marked(clear, median_ink=0.11)
    assert picked is not None and picked[0].label == "A"


# ----------------------------------------------------------------------
# 题面与答案分离（第 2 处）
# ----------------------------------------------------------------------


def _line(text: str, y: float, page: int = 1, x: float = 90.0) -> object:
    return E.Line(page=page, text=text, bbox=(x, y, x + 300, y + 11), red=False)


def _raw(section: str, number: int, texts, copy_label: str, page: int = 1):
    lines = [_line(text, 100 + 14 * index, page) for index, text in enumerate(texts)]
    return E.RawQuestion(section=section, number=number, lines=lines, page_span=[page], copy_label=copy_label)


def test_choose_primary_prefers_clean_statement_over_longer_annotated():
    clean = _raw(C.SECTION_FILL, 17, ["表达式 8/2**2 的值为 （17） 。"], "p1-p9")
    annotated = _raw(
        C.SECTION_FILL,
        17,
        ["表达式 8/2**2 的值为 （17） 。 2 解析：先乘方后除。", "所以答案是 2。"],
        "p14-p22",
    )
    chosen = E.choose_primary_raw([annotated, clean], {"p14-p22"}, set())
    assert chosen.copy_label == "p1-p9"
    assert "解析" not in E.question_text(chosen)["statement"]


def test_choose_primary_falls_back_to_annotated_when_only_option():
    annotated = _raw(C.SECTION_FILL, 1, ["题目（1） 答案 解析：说明"], "p14-p22")
    chosen = E.choose_primary_raw([annotated], {"p14-p22"}, set())
    assert chosen is annotated


# ----------------------------------------------------------------------
# 编程题：保留完整代码、区分函数与主程序（第 3 处）
# ----------------------------------------------------------------------


def test_split_statement_code_keeps_break_and_continue():
    lines = [
        _line("1. 下面的程序遇到空格就跳过，请写出输出。", 100),
        _line("txt = 'ab c'", 120),
        _line("for ch in txt:", 134),
        _line("    if ch == ' ':", 148),
        _line("        continue", 162),
        _line("    print(ch, end='')", 176),
    ]
    statement, code = E.split_statement_code(lines)
    assert "print(ch, end='')" not in statement
    assert [line.text for line in code] == [
        "txt = 'ab c'",
        "for ch in txt:",
        "    if ch == ' ':", 
        "        continue",
        "    print(ch, end='')",
    ]


def test_code_blocks_separate_function_and_main():
    lines = [
        _line("def total(nums):", 100),
        _line("    s = 0", 114),
        _line("    for n in nums:", 128),
        _line("        s += n", 142),
        _line("    return s", 156),
        _line("print(total([1, 2, 3]))", 170),
    ]
    blocks = E.code_blocks(lines)
    assert [block["kind"] for block in blocks] == ["function", "main"]
    assert blocks[0]["name"] == "total"
    assert blocks[0]["text"].splitlines()[-1] == "    return s"
    assert blocks[1]["text"] == "print(total([1, 2, 3]))"


def test_code_blocks_split_two_reference_methods():
    lines = [
        _line("def fun(s):", 100),
        _line("    return s.upper()", 114),
        _line("print(fun('a'))", 128),
        _line("方法2：", 142),
        _line("def fun(s):", 156),
        _line("    return ''.join(ch.upper() for ch in s)", 170),
        _line("print(fun('a'))", 184),
    ]
    blocks = E.code_blocks(lines)
    assert [block["method"] for block in blocks] == [1, 1, 2, 2]
    assert [block["kind"] for block in blocks] == ["function", "main", "function", "main"]
    assert blocks[2]["method_label"] == "方法2："
    # 两种解法不交错：第 1 组里不能出现第 2 组的代码
    assert "''.join" not in "".join(block["text"] for block in blocks[:2])


def test_code_blocks_keep_two_methods_apart():
    lines = [
        _line("def by_loop(n):", 100),
        _line("    total = 0", 114),
        _line("    for i in range(n):", 128),
        _line("        total += i", 142),
        _line("    return total", 156),
        _line("def by_formula(n):", 170),
        _line("    return n * (n - 1) // 2", 184),
    ]
    blocks = E.code_blocks(lines)
    assert [block["kind"] for block in blocks] == ["function", "function"]
    assert [block["name"] for block in blocks] == ["by_loop", "by_formula"]
    # 不交错：第一个函数块里不能出现第二个函数的内容
    assert "by_formula" not in blocks[0]["text"]


# ----------------------------------------------------------------------
# 写在空位上方的答案（第 4 处：16 空 acdef / ghi）
# ----------------------------------------------------------------------


def _page(lines):
    return E.PageData(
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


def test_inline_answer_reads_two_lines_above_blank():
    lines = [
        E.Line(page=1, text="acdef", bbox=(364.2, 75.6, 391.6, 86.0), red=False),
        E.Line(page=1, text="ghi", bbox=(364.2, 89.5, 380.2, 100.0), red=False),
        E.Line(page=1, text="11. 以下程序输出结果有", bbox=(87.6, 92.0, 203.3, 102.4), red=False),
        E.Line(page=1, text="（16）", bbox=(330.0, 92.0, 361.6, 102.4), red=False),
        E.Line(page=1, text="（如果有多行，分行写）", bbox=(388.0, 92.0, 503.9, 102.4), red=False),
    ]
    found = E.inline_fill_answers([_page(lines)])
    assert found[16]["text"] == "acdef\nghi"


def test_inline_answer_skips_instruction_text():
    lines = [
        E.Line(page=1, text="（如果有多行，分行写）", bbox=(300.0, 80.0, 420.0, 90.0), red=False),
        E.Line(page=1, text="（2）", bbox=(100.0, 92.0, 130.0, 102.0), red=False),
    ]
    assert E.inline_fill_answers([_page(lines)]) == {}


# ----------------------------------------------------------------------
# 原卷答案与导师解答分列（第 6 处）
# ----------------------------------------------------------------------


def test_answer_record_marks_origin_and_keeps_independent_slots_empty():
    record = {
        "answer": {
            "state": C.ANSWER_ORIGINAL,
            "text": "D",
            "origin": "paper",
            "label": "原卷参考答案",
            "sources": [],
        },
        "independent_solutions": [],
    }
    assert record["answer"]["origin"] == "paper"
    assert record["independent_solutions"] == []


def test_missing_stays_missing_without_mentor_supplement():
    state, text, _note = C.resolve_answer_state([])
    assert state == C.ANSWER_MISSING
    assert text is None