"""第二十卷（第十八节口径）全量回归：题量、答案状态、校录值与裁定。

这些用例以导师在任务书第十八节给出的机器可读块为唯一依据：
验的不是"脚本能跑"，而是"脚本结论与导师目视/校录结论一致"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.question_bank import common as C
from tools.question_bank import extract as E
from tools.question_bank import mentor as M


@pytest.fixture(scope="module")
def papers() -> dict[str, dict]:
    """一次性抽取全部 20 卷，避免每个用例重复读 PDF。"""

    data = M.load()
    catalog = C.load_catalog()
    overrides = C.read_json(C.QB_ROOT / "overrides.json")
    result: dict[str, dict] = {}
    for item in catalog:
        if item["id"] not in data.profiles:
            continue
        item = dict(item)
        item["_overrides"] = overrides.get(item["id"], {})
        records, extra, issues = E.extract_paper(
            item,
            pdf_path=C.paper_pdf_path(item),
            profile=data.profiles[item["id"]],
            mentor=data,
        )
        result[item["id"]] = {
            "records": records,
            "extra": extra,
            "issues": issues,
        }
    return result


@pytest.fixture(scope="module")
def mentor_data() -> M.MentorData:
    return M.load()


def by_label(records: list[dict], section: str) -> dict[int, dict]:
    """按题号（int）索引某一大题下的记录。"""

    return {
        int(record["number_label"]): record
        for record in records
        if record["section"]["code"] == section
    }


def test_all_twenty_papers_are_extracted(papers: dict[str, dict], mentor_data: M.MentorData) -> None:
    assert len(papers) == 20
    assert set(papers) == set(mentor_data.profiles)
    assert sum(entry["extra"]["summary"]["pages"] for entry in papers.values()) == 285


def test_per_paper_counts_match_profiles(papers: dict[str, dict], mentor_data: M.MentorData) -> None:
    bad = []
    for paper_id, entry in papers.items():
        expected = mentor_data.expected(paper_id)
        got = entry["extra"]["summary"]["by_section"]
        if (got.get("choice"), got.get("fill"), got.get("code")) != (
            expected["choice"],
            expected["fill"],
            expected["code"],
        ):
            bad.append((paper_id, expected, got))
    assert not bad


def test_totals_are_1154_790_324_40(papers: dict[str, dict]) -> None:
    summaries = [entry["extra"]["summary"] for entry in papers.values()]
    assert sum(paper["questions"] for paper in summaries) == 1154
    for section, expected in (("choice", 790), ("fill", 324), ("code", 40)):
        assert sum(paper["by_section"].get(section, 0) for paper in summaries) == expected


def test_answer_states_are_1096_original_and_58_missing(papers: dict[str, dict]) -> None:
    states: dict[str, int] = {}
    for entry in papers.values():
        for state, count in entry["extra"]["summary"]["answer_states"].items():
            states[state] = states.get(state, 0) + count
    assert states == {"original": 1096, "missing": 58}


def test_only_the_answerless_paper_is_missing(papers: dict[str, dict]) -> None:
    for paper_id, entry in papers.items():
        missing = entry["extra"]["summary"]["answer_states"].get("missing", 0)
        if paper_id == "py-2023-2024-s2-midterm":
            assert missing == 58
        else:
            assert missing == 0, paper_id


def test_no_question_is_pending_or_conflict(papers: dict[str, dict]) -> None:
    for paper_id, entry in papers.items():
        states = entry["extra"]["summary"]["answer_states"]
        assert "pending_review" not in states, paper_id
        assert "conflict" not in states, paper_id


def test_choice_answers_equal_mentor_gold(papers: dict[str, dict], mentor_data: M.MentorData) -> None:
    """19 卷的选项序列逐题比对：脚本结论必须与目视校录完全一致。"""

    bad = []
    for paper_id, entry in papers.items():
        _page, values = mentor_data.choice_values(paper_id)
        if not values:
            continue
        choices = by_label(entry["records"], "choice")
        assert len(choices) == len(values), paper_id
        for index, expected in enumerate(values, start=1):
            record = choices[index]
            source = record["answer"]["sources"][0]
            assert source["kind"] == "mentor_visual", (paper_id, index)
            if record["answer"].get("text") != expected:
                bad.append((paper_id, index, expected, record["answer"].get("text")))
    assert not bad


def test_fill_answers_equal_mentor_transcription(
    papers: dict[str, dict], mentor_data: M.MentorData
) -> None:
    """18.5 的四卷校录：每个空号的值逐字一致（含引号、反斜杠、空格）。"""

    checked = 0
    for paper_id, entry in papers.items():
        values = mentor_data.fill_values(paper_id)
        if not values:
            continue
        fill_answers = entry["extra"]["fill_answers"]
        for blank, expected in enumerate(values, start=1):
            assert blank in fill_answers, (paper_id, blank)
            assert fill_answers[blank]["text"] == expected, (paper_id, blank)
            checked += 1
    assert checked == sum(len(mentor_data.fill_values(pid)) for pid in mentor_data.fill_gold)


def test_range_choice_lists_are_parsed(papers: dict[str, dict]) -> None:
    """2024–25 s2 两卷的选项答案印成 "1-5:B B B A B" 区间，不是涂黑格。"""

    for paper_id in ("py-2024-2025-s2-midterm", "py-2024-2025-s2-final"):
        choices = by_label(papers[paper_id]["records"], "choice")
        assert len(choices) == 40
        assert all(
            record["answer"]["state"] == "original" for record in choices.values()
        )
        assert {source["kind"] for record in choices.values() for source in record["answer"]["sources"]} == {
            "mentor_visual"
        }


def test_table_fill_answers_keep_blank_ids_41_to_60(papers: dict[str, dict]) -> None:
    """2024–25 s2 两卷的填空答案按原表 41–60 编号，不减 40、不改写。"""

    for paper_id in ("py-2024-2025-s2-midterm", "py-2024-2025-s2-final"):
        blanks = set(papers[paper_id]["extra"]["fill_answers"])
        assert min(blanks) == 41 and max(blanks) == 60, paper_id
        assert len(blanks) == 20


def test_2021_2022_s2_midterm_has_56_questions(papers: dict[str, dict]) -> None:
    entry = papers["py-2021-2022-s2-midterm"]
    assert entry["extra"]["summary"]["questions"] == 56
    assert entry["extra"]["summary"]["by_section"] == {"choice": 40, "fill": 14, "code": 2}


def test_2021_2022_s2_final_splits_q6(papers: dict[str, dict]) -> None:
    entry = papers["py-2021-2022-s2-final"]
    # 40 选择 + 17 填空（第 6 小题按 18.3 第 2 条拆成 6、7）+ 2 编程
    assert entry["extra"]["summary"]["questions"] == 59
    fills = by_label(entry["records"], "fill")
    assert set(fills) == set(range(1, 18))
    assert fills[6]["blank_marks"] == [6]
    # 最后两小题按 18.3 第 2 条吃掉 16–18 与 19–20 五个空
    assert fills[16]["blank_marks"] == [16, 17, 18]
    assert fills[17]["blank_marks"] == [19, 20]


def test_2022_2023_s1_midterm_fill_15_maps_to_18_19_20(papers: dict[str, dict]) -> None:
    entry = papers["py-2022-2023-s1-midterm"]
    # 40 + 16 + 2
    assert entry["extra"]["summary"]["questions"] == 58
    fills = by_label(entry["records"], "fill")
    assert fills[15]["blank_marks"] in ([15, 16, 17], [18, 19, 20])
    assert fills[16]["blank_marks"] == [18, 19, 20]


def test_2023_2024_s1_final_fill_17_uses_19_and_20(papers: dict[str, dict]) -> None:
    entry = papers["py-2023-2024-s1-final"]
    assert entry["extra"]["summary"]["questions"] == 59
    fills = by_label(entry["records"], "fill")
    assert fills[17]["blank_marks"] == [19, 20]
    assert fills[17]["answer"]["state"] == "original"


def test_2024_2025_s1_midterm_code_merge(papers: dict[str, dict]) -> None:
    """原卷印作 5.、答案页标为 2.，按 18.3 第 5 条合并为第 2 题。"""

    entry = papers["py-2024-2025-s1-midterm"]
    codes = by_label(entry["records"], "code")
    assert set(codes) == {1, 2}
    assert codes[2]["pages"]["statement"] == [7]
    assert codes[2]["answer"]["state"] == "original"


def test_blank_template_is_not_a_question(papers: dict[str, dict]) -> None:
    """空白答题模板的编号列不能变成题目：每题都要有题干正文。"""

    empty = [
        record["question_id"]
        for entry in papers.values()
        for record in entry["records"]
        if not record["statement"].strip()
    ]
    assert not empty


def test_statement_pages_stay_inside_clean_range(
    papers: dict[str, dict], mentor_data: M.MentorData
) -> None:
    """18.3 第 8 条：题面页只能是干净页，不能把答案/解析页算作题面。"""

    bad = []
    for paper_id, entry in papers.items():
        clean = set(mentor_data.clean_pages(paper_id))
        for record in entry["records"]:
            pages = set(record["pages"]["statement"])
            if not pages <= clean:
                bad.append((record["question_id"], sorted(pages - clean)))
    assert not bad


def test_all_sixteen_formula_transcriptions_are_attached(
    papers: dict[str, dict], mentor_data: M.MentorData
) -> None:
    """18.6 的 16 条公式必须逐条挂上；每条都要写明来源与任务书哈希。"""

    attached = {
        record["question_id"]: record["formulas"]
        for entry in papers.values()
        for record in entry["records"]
        if record.get("formulas")
    }
    assert len(attached) == len(mentor_data.formulas) == 16
    for paper_id in {row["paper_id"] for row in mentor_data.formulas}:
        expected = mentor_data.formulas_for(paper_id)
        got = {
            question_id
            for question_id in attached
            if question_id.startswith(paper_id)
        }
        assert len(got) == len(expected), (paper_id, expected, got)
    for formulas in attached.values():
        assert formulas[0]["source"] == "mentor_visual_transcription"
        assert formulas[0]["doc_sha256"] == mentor_data.doc_sha256
        assert formulas[0]["text"].strip()


def test_figures_are_attached_with_bbox(papers: dict[str, dict], mentor_data: M.MentorData) -> None:
    assets = [
        (record["question_id"], asset)
        for entry in papers.values()
        for record in entry["records"]
        for asset in record.get("assets") or []
    ]
    assert len(assets) == len(mentor_data.figures)
    for _qid, asset in assets:
        assert asset["kind"] == "figure"
        assert len(asset["bbox"]) == 4
        assert asset["source"] == "mentor_visual_region"

def test_sixty_sample_questions_from_section_18_8(papers: dict[str, dict], mentor_data: M.MentorData) -> None:
    """18.8 的 60 条抽样（20 卷 × 选择/填空/编程各一题）逐条对过。"""

    checked = 0
    bad = []
    for paper_id, sample in mentor_data.samples.items():
        records = {record["question_id"]: record for record in papers[paper_id]["records"]}
        for expected in sample["questions"]:
            question_id = expected["question_id"]
            record = records.get(question_id)
            if record is None:
                bad.append((question_id, "缺失"))
                continue
            if expected["page"] not in record["pages"]["statement"]:
                bad.append((question_id, f"题面页 {record['pages']['statement']} 不含 {expected['page']}"))
                continue
            statement = record["statement"]
            if len(statement.strip()) < 10:
                bad.append((question_id, f"题干过短：{statement!r}"))
                continue
            if "参考答案" in statement or "解析" in statement:
                bad.append((question_id, "题面混入答案解析文字"))
                continue
            section = record["section"]["code"]
            if section == "choice" and len(record["options"]) != 4:
                bad.append((question_id, f"选项数为 {len(record['options'])}"))
                continue
            if section == "fill" and not record["blank_marks"]:
                bad.append((question_id, "未识别到空位编号"))
                continue
            if section == "code" and not any(
                word in statement
                for word in ("编写", "设计", "要求", "输出", "输入", "求", "计算", "统计")
            ):
                # 有的编程题只有一句命题（"编写程序…"），不强制带代码段，
                # 但题干必须是一道可执行的题目要求
                bad.append((question_id, "编程题干缺少题目要求"))
                continue
            checked += 1
    assert checked == 60
    assert not bad


def test_ugly_number_statement_keeps_the_2_3_5_line(papers: dict[str, dict]) -> None:
    """丑数定义里的“2、3、5 的正数…”是正文，不是新题号，也不能被丢掉。"""

    fills = by_label(papers["py-2023-2024-s1-final"]["records"], "fill")
    statement = fills[17]["statement"]
    assert "2、3、5 的正数" in statement
    assert "所以6 是丑数" in statement
    assert fills[17]["blank_marks"] == [19, 20]


def test_reviewed_code_regions_keep_notes_out_of_program(papers: dict[str, dict]) -> None:
    """18.7：红字替代条件、方法标题、示例输入输出不能混进参考代码。"""

    midterm = by_label(papers["py-2022-2023-s1-midterm"]["records"], "code")
    code01 = midterm[1]
    text = code01["reference_code"]
    assert "'a'<ch<'z'" not in text
    assert 'ch in "0123456789"' not in text
    assert 'ch==" "' not in text
    assert len(text.splitlines()) == 15
    assert [a["role"] for a in code01["code_annotations"]] == ["alternative"] * 3

    code02 = midterm[2]
    assert "嵌套列表" not in code02["reference_code"]
    assert "注意输出格式" not in code02["reference_code"]

    final = by_label(papers["py-2023-2024-s1-final"]["records"], "code")
    solutions = final[1]["code_blocks"]
    assert [block["block_id"] for block in solutions] == ["method1", "method2"]
    assert len(solutions[0]["text"].splitlines()) == 10
    assert len(solutions[1]["text"].splitlines()) == 7
    assert "参考代码" not in final[1]["reference_code"]

    counter = final[2]
    assert [block["kind"] for block in counter["code_blocks"]] == ["function", "main"]
    assert "请输入列表元素" not in counter["reference_code"]
    assert counter["code_examples"][0]["label"] == "示例输入输出"


def test_cross_page_code_answer_records_both_pages(papers: dict[str, dict]) -> None:
    """2024–25 s2 期中 code-02 从第 9 页续到第 10 页，出处必须是 [9,10]。"""

    codes = by_label(papers["py-2024-2025-s2-midterm"]["records"], "code")
    assert codes[2]["pages"]["answer"] == [9, 10]
    assert codes[2]["reference_code_pages"] == [9, 10]
    assert codes[2]["code_blocks"][0]["pages"] == [9, 10]
    assert "if n%i==0" in codes[2]["reference_code"]
    assert codes[1]["pages"]["answer"] == [9]


def test_mentor_data_is_bound_to_the_real_pdf(papers: dict[str, dict], mentor_data: M.MentorData) -> None:
    """把 profile 里的哈希改成全零后，抽取必须失败，不能照常套用校录。"""

    import copy

    paper_id = "py-2024-2025-s2-midterm"
    item = next(entry for entry in C.load_catalog() if entry["id"] == paper_id)
    profile = copy.deepcopy(mentor_data.profiles[paper_id])
    profile["pdf_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="PDF 哈希"):
        E.extract_paper(
            item,
            pdf_path=C.paper_pdf_path(item),
            profile=profile,
            mentor=mentor_data,
        )


def test_code_region_rules_are_bound_to_pdf_hash(mentor_data: M.MentorData) -> None:
    """code_regions 自身也绑定哈希：改哈希后必须在读区域前失败。"""

    config = C.read_json(C.QB_ROOT / "overrides.json")["py-2023-2024-s1-final"]["code_regions"]
    with pytest.raises(ValueError, match="代码区域规则与原 PDF 哈希不符"):
        E.apply_code_regions({}, {}, config, "0" * 64)


def test_paired_option_layouts_are_split_into_four_choices(papers: dict[str, dict]) -> None:
    """两栏配对排版（A/C 同行、B/D 同行）必须还原成 A–D 四个选项。

    P3 前置修正：45 条此前只切出 2 个字段（A+C、B+D）。导师目视又确认
    4 条文字层漏项可补，剩 2 条原卷确实印了两个 B；后者保留 A、B、B、D
    四项原文，不能静默改标签。
    """

    complete = 0
    duplicate_b: list[str] = []
    unexpected: list[str] = []
    for payload in papers.values():
        for record in payload["records"]:
            if record["question_type"] != "choice":
                continue
            labels = [item["label"] for item in record["options"]]
            if labels == ["A", "B", "C", "D"]:
                complete += 1
            elif labels == ["A", "B", "B", "D"]:
                duplicate_b.append(record["question_id"])
            else:
                unexpected.append(record["question_id"])

    assert complete == 788
    assert sorted(duplicate_b) == [
        "py-2022-2023-s1-final-choice-11",
        "py-2023-2024-s1-final-choice-19",
    ]
    assert unexpected == []

    fixed = by_label(papers["py-2021-2022-s1-final"]["records"], "choice")
    assert [item["text"] for item in fixed[6]["options"]] == [
        "五个内切圆",
        "五个同心圆",
        "六个内切圆",
        "六个同心圆",
    ]
    assert fixed[6]["checks"]["options_complete"] is True
    assert fixed[6]["review"]["automated"] == "pass"
