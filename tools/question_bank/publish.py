"""F007 发布候选包与逐题批准应用。

``export --scope first83`` 保留首轮 83 题候选包的行为；``remaining`` 为尚未
批准的 1069 道普通题生成候选清单、视觉建议和空白导师决定模板；``all``
合并两者。建议文件不是决定文件，``apply`` 只读 ``decisions-approved.json``，
且剩余题的批准文件必须声明 ``reviewer_type="mentor"``。

不会重建索引、不会修改 build 目录，也不会批量批准。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.question_bank.config import DEFAULT_BANK_ROOT
from app.question_bank.paths import BankPaths, atomic_write_json, read_current
from app.question_bank.repository import QuestionRepository

TASK_DOC = Path("docs/tasks/F007-Python真题题库.md")
CODE_EXTRAS = (
    "py-2022-2023-s1-midterm-code-01",
    "py-2022-2023-s1-midterm-code-02",
    "py-2023-2024-s1-final-code-01",
)
MAX_MARKDOWN_STATEMENT = 180


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json_block_after(marker: str, text: str) -> list[dict[str, Any]]:
    position = text.find(marker)
    if position < 0:
        raise SystemExit(f"任务书缺少标记 {marker}")
    start = text.find("```json", position)
    end = text.find("```", start + 7)
    if start < 0 or end < 0:
        raise SystemExit(f"标记 {marker} 后缺少 JSON 代码块")
    payload = json.loads(text[start + 7 : end])
    if not isinstance(payload, list):
        raise SystemExit(f"标记 {marker} 的 JSON 不是数组")
    return payload


def _formula_ids(text: str) -> list[str]:
    section = text[text.find("### 18.6") : text.find("### 18.7")]
    ids = []
    for line in section.splitlines():
        match = re.match(r"\|\s*(py-[a-z0-9-]+)\s*\|\s*([a-z]+-\d+)", line)
        if match:
            ids.append(f"{match.group(1)}-{match.group(2)}")
    return ids


def _option_ids(text: str) -> list[str]:
    section = text[text.find("### 21.2") : text.find("### 21.3")]
    return re.findall(r"`(py-[a-z0-9-]+-choice-\d+)`", section)


def candidate_ids(task_doc: Path = TASK_DOC) -> tuple[list[str], dict[str, list[str]]]:
    """返回去重后的候选顺序，以及每题命中的来源类别。"""

    text = task_doc.read_text(encoding="utf-8")
    samples = [
        item["question_id"]
        for paper in _json_block_after("<!-- F007_SOURCE_VISUAL_SAMPLES -->", text)
        for item in paper["questions"]
    ]
    formulas = _formula_ids(text)
    images = [
        f"{item['paper_id']}-{item['question']}"
        for item in _json_block_after("<!-- F007_SOURCE_VISUAL_FIGURES -->", text)
    ]
    options = _option_ids(text)
    reason_map: dict[str, list[str]] = {}
    for reason, values in (
        ("sample", samples),
        ("formula", formulas),
        ("image", images),
        ("code_extra", list(CODE_EXTRAS)),
        ("option_restored", options),
    ):
        for question_id in values:
            reason_map.setdefault(question_id, []).append(reason)
    return list(reason_map), reason_map


def _record_row(record: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    return {
        "question_id": record["question_id"],
        "paper_id": record["paper_id"],
        "paper_title": record["paper_title"],
        "question_type": record["question_type"],
        "number_label": record["number_label"],
        "statement": record["statement"],
        "options": [
            {"label": item["label"], "text": item["text"]}
            for item in record["options"]
        ],
        "answer_state": record["answer_state"],
        "answer_available": record["answer_available"],
        "content_sha256": record["content_sha256"],
        "content_status": record["content_status"],
        "content_issue": record["content_issue"],
        "review_status": record["review_status"],
        "visual_status": record["visual_status"],
        "pages_statement": record["pages_statement"],
        "pages_answer": record["pages_answer"],
        "assets": len(record["assets"]),
        "knowledge_points": [
            {"slug": item["slug"], "name": item["name"], "status": item["status"]}
            for item in record["knowledge_points"]
        ],
        "candidate_sources": reasons,
    }


def _suggest_decision(row: dict[str, Any], visual: dict[str, Any] | None) -> dict[str, Any]:
    if not row.get("answer_available"):
        action = "review"
        reason = "缺原卷答案，保持 missing，等待导师决定是否用独立解答"
    elif visual is None:
        action = "review"
        reason = "没有视觉复核结果"
    elif visual.get("isolated") or visual.get("result") == "isolated":
        action = "reject"
        reason = "原卷双 B 异常，继续隔离"
    elif visual.get("result") in ("pass", "format_only", "corrected"):
        action = "approve"
        reason = f"视觉+程序结论 {visual.get('result')}；正式决定仍需导师"
    else:
        action = "review"
        reason = "视觉+程序结论 " + str(visual.get("result")) + "；需导师优先复核"
    return {
        "action": action,
        "content_sha256": row["content_sha256"],
        "visual_result": (visual or {}).get("result"),
        "visual_confidence": ((visual or {}).get("vision") or {}).get("confidence"),
        "issues": list((visual or {}).get("issues") or []),
        "reason": reason,
        "reviewer_type": "vision_model+program",
        "formal_decision": False,
        "mentor_review_required": True,
    }


def _scope_ids(
    repository: QuestionRepository,
    paths: BankPaths,
    *,
    scope: str,
    task_doc: Path,
) -> tuple[list[str], dict[str, list[str]], list[str], list[str]]:
    first83, reasons = candidate_ids(task_doc)
    all_ids = [
        str(row[0])
        for row in repository.conn.execute(
            "SELECT question_id FROM questions ORDER BY paper_id, question_type, "
            "CAST(number_label AS INTEGER), question_id"
        )
    ]
    approved = set((json.loads(paths.reviews_file.read_text(encoding="utf-8")).get("decisions") or {})) if paths.reviews_file.is_file() else set()
    first_set = set(first83) & approved
    if scope == "first83":
        ids = first83
    elif scope == "remaining":
        ids = [qid for qid in all_ids if qid not in approved]
        reasons = {qid: ["remaining"] for qid in ids}
    elif scope == "all":
        ids = list(first83) + [qid for qid in all_ids if qid not in approved and qid not in set(first83)]
        for qid in ids:
            reasons.setdefault(qid, ["remaining"])
    else:
        raise SystemExit(f"未知 scope：{scope}")
    return ids, reasons, sorted(approved), sorted(first_set)


def export(
    packet: Path,
    *,
    bank_root: Path = DEFAULT_BANK_ROOT,
    task_doc: Path = TASK_DOC,
    scope: str = "first83",
) -> dict[str, Any]:
    paths = BankPaths(bank_root)
    current = read_current(paths)
    if current is None:
        raise SystemExit("题目库尚未建立，先运行 build")
    repository = QuestionRepository(paths.build_dir(current["build_id"]))
    try:
        ids, reasons, approved, first_set = _scope_ids(
            repository, paths, scope=scope, task_doc=task_doc
        )
        records = repository.fetch_summaries(ids)
        missing = [question_id for question_id in ids if question_id not in records]
        rows = [
            _record_row(records[question_id], reasons.get(question_id, ["remaining"]))
            for question_id in ids
            if question_id in records and not records[question_id]["content_issue"]
        ]
        excluded = [
            question_id
            for question_id in ids
            if question_id in records and records[question_id]["content_issue"]
        ]
        if scope == "remaining" and set(item["question_id"] for item in rows) & set(first_set):
            raise SystemExit("剩余题候选包混入了首批已批准题")
    finally:
        repository.close()

    visual_results = {}
    visual_path = bank_root / "review" / "visual" / "results.jsonl"
    if visual_path.is_file():
        for line in visual_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                visual_results[item["question_id"]] = item
    visual_summary_path = bank_root / "review" / "visual" / "summary.json"
    visual_summary = (
        json.loads(visual_summary_path.read_text(encoding="utf-8"))
        if visual_summary_path.is_file()
        else {}
    )
    suggested = {
        row["question_id"]: _suggest_decision(row, visual_results.get(row["question_id"]))
        for row in rows
    }

    packet.mkdir(parents=True, exist_ok=True)
    jsonl = packet / "publish-candidates.jsonl"
    with jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    proposed = {
        "generated_at": _now(),
        "build_id": current["build_id"],
        "source_version": current["source_version"],
        "note": "候选只是建议；逐题确认后把决定写入 decisions-approved.json，再 apply。",
        "decisions": {
            row["question_id"]: {
                "action": "approve"
                if row["answer_available"] and not row["content_issue"]
                else "review",
                "content_sha256": row["content_sha256"],
                "visual": row["visual_status"],
                "labels_verified": [],
                "note": "待导师逐题确认",
            }
            for row in rows
        },
    }
    (packet / "proposed-decisions.json").write_text(
        json.dumps(proposed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (packet / "suggested-decisions.json").write_text(
        json.dumps(
            {
                "generated_at": _now(),
                "build_id": current["build_id"],
                "source_version": current["source_version"],
                "note": "视觉模型+程序建议，不是导师决定；apply 不读取本文件。",
                "decisions": suggested,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    template = {
        "build_id": current["build_id"],
        "source_version": current["source_version"],
        "scope": scope,
        "reviewed_by": "",
        "reviewed_at": "",
        "reviewer_type": "",
        "note": "只填写已亲自看过的题；action 仅 approve/reject，未确认的题不要出现在 decisions 中。",
        "decisions": {},
    }
    (packet / "decisions-approved.json").write_text(
        json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    markdown = packet / "publish-candidates.md"
    with markdown.open("w", encoding="utf-8", newline="\n") as handle:
        title = {
            "first83": "F007 首轮发布候选（待逐题确认）",
            "remaining": "F007 剩余题发布候选（待导师逐题确认）",
            "all": "F007 全量发布候选（待逐题确认）",
        }.get(scope, "F007 发布候选")
        handle.write(f"# {title}\n\n")
        handle.write(f"- 生成时间：{_now()}\n")
        handle.write(f"- build：`{current['build_id']}`\n")
        handle.write(f"- scope：`{scope}`；候选：{len(rows)} 道；缺失：{len(missing)} 道\n")
        handle.write(
            f"- 已有批准：{len(approved)} 道；视觉汇总清晰：{visual_summary.get('clear', '未生成')} 道\n"
        )
        if excluded:
            handle.write(
                "- 暂不发布（内容异常，需先人工裁定）："
                + "、".join(f"`{item}`" for item in excluded)
                + "\n"
            )
        handle.write("- 勾选只表示导师已看过；真正生效要填写 `decisions-approved.json` 后运行 apply。\n\n")
        paper = None
        for row in rows:
            if row["paper_id"] != paper:
                paper = row["paper_id"]
                handle.write(f"\n## {paper} · {row['paper_title']}\n\n")
            statement = row["statement"].replace("\n", " ")
            if len(statement) > MAX_MARKDOWN_STATEMENT:
                statement = statement[: MAX_MARKDOWN_STATEMENT - 1] + "…"
            handle.write(
                f"- [ ] `{row['question_id']}` · {row['question_type']} 第 {row['number_label']} 题 · "
                f"答案 {row['answer_state']} · 来源 {'、'.join(row['candidate_sources'])}\n"
                f"  - 题干：{statement}\n"
            )
            tip = suggested.get(row["question_id"]) or {}
            handle.write(
                f"  - 视觉建议：{tip.get('action')}（{(tip.get('issues') or ['无'])[0]}）；"
                "不是导师决定\n"
            )
            if row["content_issue"]:
                handle.write(f"  - ⚠ 内容异常：{row['content_issue']}\n")

    summary = {
        "generated_at": _now(),
        "build_id": current["build_id"],
        "source_version": current["source_version"],
        "source_file": str(task_doc),
        "scope": scope,
        "approved_existing": len(approved),
        "candidate_count": len(rows),
        "missing_ids": missing,
        "by_type": _count(rows, "question_type"),
        "by_answer_state": _count(rows, "answer_state"),
        "by_review_status": _count(rows, "review_status"),
        "by_candidate_source": _count_sources(rows),
        "content_issues": [row["question_id"] for row in rows if row["content_issue"]],
        "excluded_content_issues": excluded,
        "answer_missing": [row["question_id"] for row in rows if not row["answer_available"]],
    }
    (packet / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _count(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _count_sources(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for reason in row["candidate_sources"]:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _valid_decision(question_id: str, decision: Any) -> tuple[str, str]:
    if not isinstance(decision, dict):
        raise SystemExit(f"{question_id} 的决定不是对象")
    action = decision.get("action")
    if action not in ("approve", "reject"):
        raise SystemExit(f"{question_id} 的 action 必须是 approve 或 reject")
    digest = decision.get("content_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise SystemExit(f"{question_id} 缺少合法 content_sha256")
    return action, digest


def apply(
    packet: Path,
    *,
    bank_root: Path = DEFAULT_BANK_ROOT,
    write: bool = False,
) -> dict[str, Any]:
    paths = BankPaths(bank_root)
    current = read_current(paths)
    if current is None:
        raise SystemExit("题目库尚未建立，不能应用审核决定")
    approved_path = packet / "decisions-approved.json"
    if not approved_path.exists():
        raise SystemExit(f"缺少 {approved_path}")
    payload = json.loads(approved_path.read_text(encoding="utf-8"))
    if payload.get("build_id") != current["build_id"]:
        raise SystemExit("批准文件与当前 build 不一致，拒绝应用")
    decisions = payload.get("decisions")
    if not isinstance(decisions, dict) or not decisions:
        raise SystemExit("decisions-approved.json 还没有逐题决定")
    if not payload.get("reviewed_by") or not payload.get("reviewed_at"):
        raise SystemExit("批准文件必须填写 reviewed_by 与 reviewed_at")
    scope = str(payload.get("scope") or "")
    if not scope:
        scope = "remaining" if packet.name == "publish-remaining" else "first83"
    if scope in ("remaining", "all"):
        if payload.get("reviewer_type") != "mentor":
            raise SystemExit("剩余/全部题批准文件必须声明 reviewer_type=mentor；视觉建议不能直接应用")
    candidate_file = packet / "publish-candidates.jsonl"
    packet_ids: list[str] = []
    if candidate_file.is_file():
        for line in candidate_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                packet_ids.append(json.loads(line)["question_id"])
    if scope in ("remaining", "all"):
        if not packet_ids:
            raise SystemExit("候选包缺少 publish-candidates.jsonl，不能应用剩余题决定")
        ids = packet_ids
    else:
        ids, _reasons = candidate_ids()
    candidate_set = set(ids)
    repository = QuestionRepository(paths.build_dir(current["build_id"]))
    try:
        all_records = repository.fetch_summaries(ids)
    finally:
        repository.close()
    candidate_set = {
        question_id
        for question_id in candidate_set
        if question_id in all_records and not all_records[question_id]["content_issue"]
    }
    if scope == "remaining":
        protected = set(json.loads(paths.reviews_file.read_text(encoding="utf-8")).get("decisions") or {}) if paths.reviews_file.is_file() else set()
        if candidate_set & protected:
            raise SystemExit("剩余题候选包包含已批准题，拒绝应用")
    unknown = sorted(set(decisions) - candidate_set)
    if unknown:
        raise SystemExit("批准文件包含非候选题目：" + "、".join(unknown[:5]))
    records = {
        question_id: all_records[question_id]
        for question_id in decisions
        if question_id in all_records
    }
    missing = sorted(set(decisions) - set(records))
    if missing:
        raise SystemExit("批准文件包含当前 build 不存在的题目：" + "、".join(missing[:5]))
    validated: dict[str, dict[str, Any]] = {}
    for question_id, decision in decisions.items():
        action, digest = _valid_decision(question_id, decision)
        if digest != records[question_id]["content_sha256"]:
            raise SystemExit(f"{question_id} 的内容哈希已变化，必须重新查看")
        validated[question_id] = {
            "action": action,
            "content_sha256": digest,
            "visual": str(decision.get("visual") or records[question_id]["visual_status"]),
            "labels_verified": list(decision.get("labels_verified") or []),
            "note": str(decision.get("note") or ""),
            "reviewed_by": payload["reviewed_by"],
            "reviewed_at": payload["reviewed_at"],
            "reviewer_type": str(payload.get("reviewer_type") or "mentor"),
        }

    reviews_path = paths.reviews_file
    existing = json.loads(reviews_path.read_text(encoding="utf-8")) if reviews_path.exists() else {}
    protected_before = set(existing.get("decisions") or {})
    merged = dict(existing.get("decisions") or {})
    merged.update(validated)
    if not protected_before.issubset(set(merged)):
        raise SystemExit("应用决定会删除已有批准题，拒绝写入")
    result = {
        "build_id": current["build_id"],
        "scope": scope,
        "candidate_count": len(candidate_set),
        "validated": len(validated),
        "approved": sum(1 for item in validated.values() if item["action"] == "approve"),
        "rejected": sum(1 for item in validated.values() if item["action"] == "reject"),
        "dry_run": not write,
        "reviews_path": str(reviews_path),
    }
    if not write:
        return result

    reviews_path.parent.mkdir(parents=True, exist_ok=True)
    if reviews_path.exists():
        backup = reviews_path.with_name(
            f"{reviews_path.name}.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        shutil.copy2(reviews_path, backup)
        result["backup"] = str(backup)
    document = {
        "schema_version": 1,
        "updated_at": _now(),
        "decisions": merged,
    }
    atomic_write_json(reviews_path, document)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="F007 首轮发布候选导出与逐题批准应用")
    parser.add_argument("command", choices=("export", "apply"))
    parser.add_argument("--bank-root", type=Path, default=DEFAULT_BANK_ROOT)
    parser.add_argument("--packet", type=Path, default=None)
    parser.add_argument("--scope", choices=("first83", "remaining", "all"), default="first83")
    parser.add_argument("--apply", action="store_true", help="真正写入 reviews.json")
    args = parser.parse_args(argv)
    packet = args.packet or (
        args.bank_root / "review" / "publish-remaining"
        if args.scope == "remaining"
        else args.bank_root / "review" / "p4"
    )
    if args.command == "export":
        summary = export(packet, bank_root=args.bank_root, scope=args.scope)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    result = apply(packet, bank_root=args.bank_root, write=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())