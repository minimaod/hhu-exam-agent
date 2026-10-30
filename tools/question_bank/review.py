"""把抽取产物整理成可人工复核的工作表与覆盖统计。

输出三样东西：
- `worksheets/<paper>.md`：逐题卡片（题干、选项、答案状态与证据、页码、裁片路径）。
- `coverage.json`：机器可读的逐卷覆盖与异常。
- `coverage.md`：给人看的汇总表。

本工具不会把任何题目标成"已审核"：`visual` 一律保留 `not_reviewed`，
由导师或用户看裁片后再改。
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from tools.question_bank import common as C

TOOL_VERSION = "review.py@0.1"
STATE_LABEL = {
    C.ANSWER_ORIGINAL: "原卷答案",
    C.ANSWER_MISSING: "原卷无答案",
    C.ANSWER_PENDING: "待复核",
    C.ANSWER_CONFLICT: "证据冲突",
}


def _excerpt(text: str, limit: int = 220) -> str:
    value = C.clean_line(text.replace("\n", " ⏎ "))
    return value if len(value) <= limit else value[: limit - 1] + "…"


def question_card(record: dict[str, Any]) -> str:
    section = record["section"]
    answer = record["answer"]
    lines = [
        f"### {record['number_label']}. {section['title']}（{record['question_id']}）",
        "",
        f"- 题干页码：{record['pages']['statement']}；答案页码：{record['pages']['answer'] or '—'}",
        f"- 答案状态：**{STATE_LABEL.get(answer['state'], answer['state'])}**"
        + (f"（{answer['text']}）" if answer.get("text") else ""),
    ]
    if record.get("blank_marks"):
        lines.append(f"- 空位编号：{record['blank_marks']}")
    if record.get("duplicates"):
        lines.append(f"- 重复出现的副本：{record['duplicates']}")
    lines.append("")
    lines.append("```text")
    lines.append(record["statement"] or "（题干为空）")
    lines.append("```")
    if record["options"]:
        lines.append("")
        for option in record["options"]:
            lines.append(f"- **{option['label']}** {option['text']}")
    if record.get("reference_code"):
        lines.append("")
        lines.append("参考代码（原卷答案版，按版面分块）：")
        for position, block in enumerate(record.get("code_blocks") or [], start=1):
            name = f" {block['name']}" if block.get("name") else ""
            method = ""
            if block.get("method_label") or (block.get("method") or 1) > 1:
                method = f" · {block.get('method_label') or ('方法' + str(block['method']))}"
            lines.append(
                f"- 块 {position}：第 {block['page']} 页{method} · {block['kind']}{name}"
                f"（{block['lines']} 行"
                + (f"，第 {block['column'] + 1} 栏" if block.get("column") else "")
                + "）"
            )
            lines.append("")
            lines.append("```python")
            lines.append(block["text"])
            lines.append("```")
            lines.append("")
            if block.get("depends_on"):
                lines.append("共同前置块：" + ", ".join(block["depends_on"]) + "（按块引用，不重复拼入原文）。")
            if block.get("extraction") == "reviewed_region":
                lines.append(
                    f"区域裁片：`crops/{record['paper_id']}/"
                    f"{record['code_layout']['copy']}/"
                    f"p{block['page']:02d}-{record['question_id']}-region-{block['block_id']}.png`"
                )
        if not record.get("code_blocks"):
            lines.append("")
            lines.append("```python")
            lines.append(record["reference_code"])
            lines.append("```")
    if record.get("code_annotations"):
        lines.append("原卷旁注（保留为解释，不属于代码正文）：")
        for annotation in record["code_annotations"]:
            lines.append(f"- 第 {annotation['page']} 页：{annotation['text']}")
    if answer["sources"]:
        lines.append("")
        lines.append("答案证据：")
        for source in answer["sources"]:
            verified = "已核对" if source["pairing_verified"] else "未核对"
            pages = source["pages"] or "—"
            detail = f"；{source['detail']}" if source["detail"] else ""
            lines.append(
                f"  - `{source['kind']}` @ {pages} = {source['text'] or '(无文本)'}（{verified}）{detail}"
            )
    if answer.get("note"):
        lines.append("")
        lines.append(f"> {answer['note']}")
    if record.get("header_notes"):
        lines.append("")
        lines.append("卷头差异（只记录，不作为判定依据）：" + "；".join(record["header_notes"]))
    if record.get("label_corrections"):
        lines.append("")
        for fix in record["label_corrections"]:
            lines.append(
                f"> 空号校正（导师核对）：印作 {fix['printed']} → 第 {fix['canonical']} 空，"
                f"答案 `{fix['text']}`，来源页 {fix['page']}；依据：{fix['basis']}"
            )
    # 导师独立解答与原卷答案分开显示，避免把补充答案当成原卷答案。
    lines.append("")
    independent = record.get("independent_solutions") or []
    if independent:
        lines.append("**导师独立解答 / 补充讲解（非原卷答案）**：")
        for item in independent:
            lines.append(f"- 来源：{item.get('author', '导师')}；{item.get('note', '')}")
            lines.append("")
            lines.append("```text")
            lines.append(item.get("text", ""))
            lines.append("```")
    else:
        lines.append("**导师独立解答**：无（本卷只记录原卷答案，未补充额外解答）。")
    checks = record.get("checks", {})
    problems = checks.get("problems") or []
    if problems:
        lines.append("")
        lines.append("自动检查提示：" + "；".join(problems))
    lines.append("")
    lines.append(
        f"裁片：`crops/{record['paper_id']}/{record.get('copies', {}).get('primary', '')}/"
        f"p<页号>-{record['question_id']}.png`（跨页题每页一张）"
    )
    lines.append("")
    lines.append(f"自检：自动={record['review']['automated']}，目视={record['review']['visual']}，"
                 f"approved={'是' if record['review'].get('approved') else '否'}")
    lines.append("")
    return "\n".join(lines)


def worksheet(paper_id: str, title: str, records: Sequence[dict[str, Any]], summary: dict[str, Any]) -> str:
    head = [
        f"# {title}（{paper_id}）复核工作表",
        "",
        "本表由 `tools/question_bank/review.py` 生成，**所有条目都未经过目视核对**。",
        "几何检查与文字层检查结果写在每题末尾；裁片路径相对于 `data/local/question-bank/review/sample/crops/`。",
        "",
        f"- 页数：{summary.get('pages')}；题目数：{summary.get('questions')}；分布：{summary.get('by_section')}",
        f"- 答案状态：{summary.get('answer_states')}",
        "- 答案来源：`原卷参考答案` 只放原卷证据；导师独立解答（如有）单列为 `independent_solutions`，脚本不生成答案，也不会把补充解答写回原卷字段。",
        f"- 涂黑格：{summary.get('grid')}；题号集合一致：{summary.get('grid_verified')}",
        f"- 填空答案：{summary.get('fill_answers')} 条；空号覆盖一致：{summary.get('fill_verified')}",
        "",
        "## 分副本统计",
        "",
        "| 副本 | 页 | 声明页数 | 题目 | 选择 | 填空 | 编程 | 涂黑 | 填空答案 | 红字 | 代码 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for copy in summary.get("copies", []):
        pages = copy["pages"]
        page_text = f"{pages[0]}–{pages[-1]}" if len(pages) > 1 else str(pages[0])
        head.append(
            f"| {copy['label']} | {page_text} | {copy['declared_total']} | {copy['questions']} | "
            f"{copy['choice']} | {copy['fill']} | {copy['code']} | {copy['grid_answers']} | "
            f"{copy['fill_answers']} | {copy['red_answers']} | {copy['code_answers']} |"
        )
    head.append("")
    head.append("## 逐题")
    head.append("")
    body = [question_card(record) for record in records]
    return "\n".join(head + body) + "\n"


def coverage_markdown(manifest: dict[str, Any], issues: Sequence[dict[str, Any]]) -> str:
    lines = [
        "# 样本覆盖与异常汇总",
        "",
        f"生成时间：{C.now_iso()}；工具：{manifest.get('tool')}；导出工具：{TOOL_VERSION}",
        "",
        "| 试卷 | 页数 | 题数 | 选择/填空/编程 | 答案状态 | 涂黑题号一致 | 填空空号一致 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for paper in manifest.get("papers", []):
        by_section = paper.get("by_section", {})
        lines.append(
            f"| {paper['paper_id']} | {paper.get('pages')} | {paper.get('questions')} | "
            f"{by_section.get('choice', 0)}/{by_section.get('fill', 0)}/{by_section.get('code', 0)} | "
            f"{paper.get('answer_states')} | {paper.get('grid_verified')} | {paper.get('fill_verified')} |"
        )
    lines.append("")
    lines.append("## 异常清单")
    lines.append("")
    if not issues:
        lines.append("无。")
    else:
        lines.append("| 试卷 | 类型 | 说明 |")
        lines.append("| --- | --- | --- |")
        for issue in issues:
            detail = issue.get("detail", "")
            extra = {k: v for k, v in issue.items() if k not in ("paper_id", "kind", "detail")}
            lines.append(
                f"| {issue.get('paper_id', '—')} | {issue.get('kind')} | {detail} {extra if extra else ''} |"
            )
    lines.append("")
    lines.append("## 复核状态口径")
    lines.append("")
    lines.append("- `automated`：脚本自动检查（题数、选项数、空号、题号一致性）是否通过。")
    lines.append("- `geometry`：涂黑格按渲染墨迹密度解码；记录了每格墨迹值与分离度。")
    lines.append("- `visual`：**全部为 `not_reviewed`**。需要导师或用户看裁片后逐题确认，脚本不代替目视。")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成复核工作表与覆盖统计")
    parser.add_argument("--source", default=str(C.SOURCE_ROOT / "sample"))
    parser.add_argument("--out", default=str(C.REVIEW_ROOT / "sample"))
    args = parser.parse_args(argv)

    source = Path(args.source)
    out = Path(args.out)
    manifest = C.read_json(source / "manifest.json")
    out.mkdir(parents=True, exist_ok=True)
    worksheets = out / "worksheets"
    worksheets.mkdir(parents=True, exist_ok=True)

    for paper in manifest["papers"]:
        paper_id = paper["paper_id"]
        rows = C.read_jsonl(source / f"{paper_id}.jsonl")
        text = worksheet(paper_id, paper.get("title", paper_id), rows, paper)
        (worksheets / f"{paper_id}.md").write_text(text, encoding="utf-8", newline="\n")
        print(f"worksheet -> {C.relative(worksheets / (paper_id + '.md'))}  ({len(rows)} 题)")

    issues = manifest.get("issues", [])
    coverage = {
        "tool": TOOL_VERSION,
        "generated_at": C.now_iso(),
        "papers": [
            {
                "paper_id": paper["paper_id"],
                "pages": paper.get("pages"),
                "questions": paper.get("questions"),
                "by_section": paper.get("by_section"),
                "answer_states": paper.get("answer_states"),
                "grid_verified": paper.get("grid_verified"),
                "fill_verified": paper.get("fill_verified"),
                "fill_answers": paper.get("fill_answers"),
                "fill_inline_answers": paper.get("fill_inline_answers", []),
                "overrides": paper.get("overrides", []),
            }
            for paper in manifest["papers"]
        ],
        "totals": manifest.get("totals", {}),
        "issues": issues,
        "review_status": {
            "automated": "pass/warn 见各题",
            "geometry": "见 grid 字段",
            "visual": "not_reviewed",
            "approved": False,
        },
    }
    C.write_json(out / "coverage.json", coverage)
    (out / "coverage.md").write_text(
        coverage_markdown(manifest, issues), encoding="utf-8", newline="\n"
    )
    print(f"coverage -> {C.relative(out / 'coverage.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
