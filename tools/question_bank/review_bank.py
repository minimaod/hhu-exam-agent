"""导出 P3 知识点候选标签复核工作表（只导出，不自动批准）。

导师复核后把决定写进 db/reviews.json；只有 `labels_verified` 中的标签会
在下次 build 时进入检索文本。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from app.question_bank.config import DEFAULT_BANK_ROOT
from app.question_bank.paths import BankPaths, read_current
from app.question_bank.repository import QuestionRepository


def export(paths: BankPaths, out_dir: Path | None = None) -> dict[str, Any]:
    current = read_current(paths)
    if current is None:
        raise SystemExit("题目库尚未建立，先运行 build")
    build_dir = paths.build_dir(current["build_id"])
    repository = QuestionRepository(build_dir)
    out = out_dir or (paths.root / "review" / "p3")
    out.mkdir(parents=True, exist_ok=True)
    try:
        ids = [
            row[0]
            for row in repository.conn.execute(
                "SELECT question_id FROM questions ORDER BY paper_id, section_code, "
                "CAST(number_label AS INTEGER)"
            )
        ]
        records = repository.fetch_summaries(ids)
        jsonl_path = out / "knowledge-labels.jsonl"
        rows: list[dict[str, Any]] = []
        with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
            for question_id in ids:
                record = records[question_id]
                row = {
                    "question_id": question_id,
                    "paper_id": record["paper_id"],
                    "paper_title": record["paper_title"],
                    "question_type": record["question_type"],
                    "number_label": record["number_label"],
                    "statement": record["statement"],
                    "options": record["options"],
                    "answer_state": record["answer_state"],
                    "content_status": record["content_status"],
                    "content_issue": record["content_issue"],
                    "review_status": record["review_status"],
                    "knowledge_points": [
                        {
                            "slug": item["slug"],
                            "name": item["name"],
                            "status": item["status"],
                            "basis": item["basis"],
                            "rule_id": item["rule_id"],
                        }
                        for item in repository.conn.execute(
                            "SELECT k.kp_slug AS slug, p.name, k.status, k.basis, k.rule_id "
                            "FROM question_knowledge_points k JOIN knowledge_points p ON p.slug=k.kp_slug "
                            "WHERE k.question_id=? ORDER BY k.ordinal",
                            (question_id,),
                        )
                    ],
                }
                rows.append(row)
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        markdown = out / "knowledge-labels.md"
        label_counter: Counter[str] = Counter()
        verified_counter: Counter[str] = Counter()
        no_label = 0
        for row in rows:
            if not row["knowledge_points"]:
                no_label += 1
            for item in row["knowledge_points"]:
                label_counter[item["slug"]] += 1
                if item["status"] == "verified":
                    verified_counter[item["slug"]] += 1
        with markdown.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("# P3 知识点候选标签复核工作表\n\n")
            handle.write(f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}\n")
            handle.write(f"- build：`{current['build_id']}`\n")
            handle.write(f"- 题目：{len(rows)}；无候选标签：{no_label}\n")
            handle.write("- 规则标签一律为 `candidate`；确认后才写 `labels_verified`。\n\n")
            handle.write("## 覆盖统计\n\n| slug | 题数 | verified |\n| --- | --- | --- |\n")
            for slug, count in label_counter.most_common():
                handle.write(f"| {slug} | {count} | {verified_counter.get(slug, 0)} |\n")
            handle.write("\n## 逐题候选\n\n")
            paper = None
            for row in rows:
                if row["paper_id"] != paper:
                    paper = row["paper_id"]
                    handle.write(f"\n### {paper} {row['paper_title']}\n\n")
                labels = "；".join(
                    f"{item['name']}({item['status']}，{item['basis']})"
                    for item in row["knowledge_points"]
                ) or "无"
                statement = row["statement"].replace("\n", " ")[:140]
                handle.write(
                    f"- `{row['question_id']}` {row['question_type']} 第{row['number_label']}题："
                    f"{statement}\n  - 标签：{labels}\n"
                )
        coverage = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "build_id": current["build_id"],
            "questions": len(rows),
            "no_label": no_label,
            "content_issues": sum(1 for row in rows if row["content_status"] != "ok"),
            "by_slug": dict(label_counter.most_common()),
            "verified_by_slug": dict(verified_counter),
        }
        (out / "coverage.json").write_text(
            json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return {"out": str(out), "questions": len(rows), "no_label": no_label, "coverage": coverage}
    finally:
        repository.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 P3 知识点复核工作表")
    parser.add_argument("--bank-root", default=str(DEFAULT_BANK_ROOT))
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    result = export(BankPaths(Path(args.bank_root)), Path(args.out) if args.out else None)
    print(json.dumps({key: value for key, value in result.items() if key != "coverage"}, ensure_ascii=False, indent=2))
    print(json.dumps(result["coverage"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())