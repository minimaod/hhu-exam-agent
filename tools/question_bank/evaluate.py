"""P3 离线检索评估：20 正例 + 10 无匹配 + 10 显式过滤。

先在 dev 上校准阈值并冻结，再允许跑 test；结果原样写入 evidence/p3/，
失败案例也保留，不用测试集回调阈值。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from app.question_bank.config import COURSE, DEFAULT_BANK_ROOT, RetrievalConfig
from app.question_bank.models import SearchRequest, SearchResponse
from app.question_bank.paths import BankPaths
from app.question_bank.service import QuestionBankService

MODES = ("keyword_only", "vector_only", "hybrid")
FORCE = {"keyword_only": "keyword_only", "vector_only": "vector_only", "hybrid": "hybrid"}


def load_queries(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"第 {line_no} 行不是 JSON") from exc
        rows.append(row)
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("评估 query id 重复")
    kinds = defaultdict(int)
    for row in rows:
        kinds[row["kind"]] += 1
    if kinds != {"relevant": 20, "no_match": 10, "filter": 10}:
        raise ValueError(f"评估集数量不符合 20/10/10：{dict(kinds)}")
    return rows


def _request(case: dict[str, Any]) -> SearchRequest:
    filters = case.get("filters") or {}
    return SearchRequest(
        course=COURSE,
        query=case["query"],
        knowledge_points=filters.get("knowledge_points") or [],
        question_type=filters.get("question_type"),
        exam_type=filters.get("exam_type"),
        paper_id=filters.get("paper_id"),
        limit=5,
        preview_candidates=True,
    )


def _violations(response: SearchResponse, filters: dict[str, Any]) -> int:
    bad = 0
    for item in response.items:
        if filters.get("question_type") and item.question_type != filters["question_type"]:
            bad += 1
        if filters.get("exam_type") and item.paper.exam_type != filters["exam_type"]:
            bad += 1
        if filters.get("paper_id") and item.paper.paper_id != filters["paper_id"]:
            bad += 1
        if item.paper.course != COURSE:
            bad += 1
    return bad


def evaluate_case(service: QuestionBankService, case: dict[str, Any], mode: str) -> dict[str, Any]:
    response = service.search(_request(case), force_mode=FORCE[mode])
    ids = [item.question_id for item in response.items]
    cross = sum(1 for item in response.items if item.paper.course != COURSE)
    filters = case.get("filters") or {}
    violations = _violations(response, filters)
    expected = case.get("expected_ids") or []
    if case["kind"] == "relevant":
        passed = bool(set(ids[:5]) & set(expected))
        detail = {"hit": passed, "expected": expected}
    elif case["kind"] == "no_match":
        passed = response.status == "no_match" and response.returned == 0
        detail = {"false_hit": not passed}
    else:
        passed = response.status == "ok" and response.returned > 0 and violations == 0
        detail = {"violations": violations, "returned": response.returned}
    return {
        "id": case["id"],
        "split": case["split"],
        "kind": case["kind"],
        "query": case["query"],
        "mode": mode,
        "status": response.status,
        "returned": response.returned,
        "ids": ids,
        "passed": passed,
        "cross_course": cross,
        "violations": violations,
        "gate": response.diagnostics.gate,
        "degraded": response.degraded,
        "latency_ms": response.diagnostics.elapsed_ms,
        "detail": detail,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    relevant = [row for row in rows if row["kind"] == "relevant"]
    no_match = [row for row in rows if row["kind"] == "no_match"]
    filters = [row for row in rows if row["kind"] == "filter"]
    hits = sum(1 for row in relevant if row["passed"])
    no_match_ok = sum(1 for row in no_match if row["passed"])
    filter_ok = sum(1 for row in filters if row["passed"])
    latencies = [row["latency_ms"] for row in rows if row["latency_ms"]]
    return {
        "queries": len(rows),
        "relevant": {
            "count": len(relevant),
            "hit_at_5": hits,
            "hit_rate": round(hits / len(relevant), 4) if relevant else None,
        },
        "no_match": {
            "count": len(no_match),
            "passed": no_match_ok,
            "false_hits": len(no_match) - no_match_ok,
            "pass_rate": round(no_match_ok / len(no_match), 4) if no_match else None,
        },
        "filter": {
            "count": len(filters),
            "passed": filter_ok,
            "violations": sum(row["violations"] for row in filters),
        },
        "cross_course_violations": sum(row["cross_course"] for row in rows),
        "mean_latency_ms": round(statistics.mean(latencies), 2) if latencies else None,
    }


def metrics_for(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "overall": aggregate(rows),
        "dev": aggregate([row for row in rows if row["split"] == "dev"]),
        "test": aggregate([row for row in rows if row["split"] == "test"]),
    }


def calibrate(rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    vector_relevant = [
        row for row in rows["vector_only"] if row["kind"] == "relevant" and row["split"] == "dev"
    ]
    vector_no_match = [
        row for row in rows["vector_only"] if row["kind"] == "no_match" and row["split"] == "dev"
    ]
    bm25_relevant = [
        row for row in rows["keyword_only"] if row["kind"] == "relevant" and row["split"] == "dev"
    ]
    bm25_no_match = [
        row for row in rows["keyword_only"] if row["kind"] == "no_match" and row["split"] == "dev"
    ]
    rel_cos = [float(row["gate"].get("best_vector_cosine") or 0.0) for row in vector_relevant]
    no_cos = [float(row["gate"].get("best_vector_cosine") or 0.0) for row in vector_no_match]
    rel_cov = [float(row["gate"].get("best_idf_coverage") or 0.0) for row in bm25_relevant]
    no_cov = [float(row["gate"].get("best_idf_coverage") or 0.0) for row in bm25_no_match]
    return {
        "dev_relevant": {
            "count": len(rel_cos),
            "min_vector_cosine": round(min(rel_cos), 6) if rel_cos else None,
            "min_idf_coverage": round(min(rel_cov), 6) if rel_cov else None,
        },
        "dev_no_match": {
            "count": len(no_cos),
            "max_vector_cosine": round(max(no_cos), 6) if no_cos else None,
            "max_idf_coverage": round(max(no_cov), 6) if no_cov else None,
        },
        "note": "cosine 取 vector_only 路线，IDF 覆盖取 keyword_only 路线；阈值只依据 dev 分布选择，test 只运行一次。",
    }


def run(split: str, config: RetrievalConfig, queries_path: Path, output: Path) -> dict[str, Any]:
    cases = [row for row in load_queries(queries_path) if split == "all" or row["split"] == split]
    rows: dict[str, list[dict[str, Any]]] = {}
    with QuestionBankService(retrieval=config) as service:
        for mode in MODES:
            rows[mode] = [evaluate_case(service, case, mode) for case in cases]
        status = service.status()
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "split": split,
        "build": status.build.model_dump() if status.build else None,
        "thresholds": config.as_dict(),
        "modes": {mode: {"metrics": metrics_for(mode_rows), "per_query": mode_rows} for mode, mode_rows in rows.items()},
        "calibration": calibrate(rows),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# P3 检索评估结果",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 数据集：`{payload['split']}`",
        f"- build：`{(payload['build'] or {}).get('build_id')}`",
        f"- 阈值：vector≥{payload['thresholds']['vector_threshold']}，"
        f"idf_coverage≥{payload['thresholds']['idf_coverage_threshold']}（两者同时低于才判 no_match）",
        "",
        "| 路线 | 正例 Hit@5 | 无匹配通过 | 过滤通过 | 过滤越界 | 跨课程 | 平均耗时 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for mode in MODES:
        metrics = payload["modes"][mode]["metrics"]
        for split_name in ("dev", "test", "overall"):
            part = metrics[split_name]
            if not part["queries"]:
                continue
            rel = part["relevant"]
            nom = part["no_match"]
            filt = part["filter"]
            lines.append(
                f"| {mode} / {split_name} | {rel['hit_at_5']}/{rel['count']} "
                f"({(rel['hit_rate'] or 0) * 100:.1f}%) | {nom['passed']}/{nom['count']} | "
                f"{filt['passed']}/{filt['count']} | {filt['violations']} | "
                f"{part['cross_course_violations']} | {part['mean_latency_ms']} ms |"
            )
    lines += [
        "",
        "## 失败案例",
        "",
        "| id | split | kind | mode | 状态 | 返回 | 原因 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    failed = 0
    for mode in MODES:
        for row in payload["modes"][mode]["per_query"]:
            if row["passed"]:
                continue
            failed += 1
            reason = (
                "未命中期望题"
                if row["kind"] == "relevant"
                else "被判为有结果"
                if row["kind"] == "no_match"
                else f"越界 {row['violations']} 条"
            )
            lines.append(
                f"| {row['id']} | {row['split']} | {row['kind']} | {mode} | {row['status']} | "
                f"{row['returned']} | {reason} |"
            )
    if failed == 0:
        lines.append("| - | - | - | - | - | - | 无 |")
    cal = payload.get("calibration") or {}
    lines += [
        "",
        "## dev 阈值依据",
        "",
        f"- 正例最小 cosine：{cal.get('dev_relevant', {}).get('min_vector_cosine')}",
        f"- 正例最小 IDF 覆盖：{cal.get('dev_relevant', {}).get('min_idf_coverage')}",
        f"- 无匹配最大 cosine：{cal.get('dev_no_match', {}).get('max_vector_cosine')}",
        f"- 无匹配最大 IDF 覆盖：{cal.get('dev_no_match', {}).get('max_idf_coverage')}",
        "",
        cal.get("note", ""),
        "",
        "> 评估集标签为 Harness 依题面整理的候选期望，待导师复核；不把未复核标签当人工金标准。",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P3 检索离线评估")
    parser.add_argument("--bank-root", default=str(DEFAULT_BANK_ROOT))
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--allow-test", action="store_true", help="确认 dev 已冻结后才允许跑 test")
    parser.add_argument("--vector-threshold", type=float, default=RetrievalConfig().vector_threshold)
    parser.add_argument("--idf-threshold", type=float, default=RetrievalConfig().idf_coverage_threshold)
    parser.add_argument("--queries", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    paths = BankPaths(Path(args.bank_root))
    queries_path = Path(args.queries) if args.queries else paths.eval_dir / "p3-queries.jsonl"
    if args.split in ("test", "all") and not args.allow_test:
        print("拒绝直接跑 test：先在 dev 上校准并冻结，再加 --allow-test。", file=sys.stderr)
        return 2
    config = RetrievalConfig(
        vector_threshold=args.vector_threshold,
        idf_coverage_threshold=args.idf_threshold,
    )
    output = Path(args.out) if args.out else paths.evidence_dir / f"eval-{args.split}.json"
    payload = run(args.split, config, queries_path, output)
    markdown = output.with_suffix(".md")
    markdown.write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps({mode: payload["modes"][mode]["metrics"]["overall"] for mode in MODES}, ensure_ascii=False, indent=2))
    print(f"结果：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())