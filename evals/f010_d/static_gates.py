"""F010 D / S1 静态闸门：S1 参数优先、S2 续页绑定范围、S3 调度路径隔离。

只读检查，不修改任何文件。输出 JSON 到 stdout，并（可选）写入
``tmp/f010-d/static-gates.json``。这些检查不是动态验收的替代品：最终结论
仍以 A/B/C/D 回放和 pytest 为准。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
C_ROOT = Path("D:/实习/河海大学考试复习-C")
ANSWER_GATE_SHA = "392296ddc98db16a"
C_CORE_SHA = {
    "answer_gate.py": ANSWER_GATE_SHA,
    "tool_projection.py": "db249f63b656a6dd",
}
RECOVERY_BASELINE_SHA = "abe9b11eea6dbe60"
RECOVERY_CLOSE_SHA = "3425ee7b394d9c17"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def s1_checks() -> list[dict]:
    checks: list[dict] = []
    production = list((ROOT / "app").rglob("*.py"))
    forbidden = ("evals.f010_d.legacy_intent", "evals.f010_d.legacy_params")
    offenders = [
        str(path.relative_to(ROOT))
        for path in production
        if any(token in path.read_text(encoding="utf-8") for token in forbidden)
    ]
    checks.append(_check(
        "S1.production_no_legacy_import",
        not offenders,
        f"production legacy imports: {offenders}",
    ))

    tools = _read("app/conversations/tools.py")
    required = (
        "continue_from_previous",
        "exclude_previous",
        "all_exam_types",
        'target: Literal["current", "new"]',
    )
    missing = [token for token in required if token not in tools]
    checks.append(_check(
        "S1.parameter_first_tool_schema",
        not missing,
        f"missing: {missing}",
    ))

    search = _read("app/conversations/search.py")
    checks.append(_check(
        "S1.search_has_no_text_intent_import",
        "extract_intent" not in search and "legacy_intent" not in search,
        "search.py 不得反向导入旧意图解析",
    ))
    checks.append(_check(
        "S1.search_uses_intent_from_params",
        "_intent_from_params" in search,
        "search.py 必须从 params/context 构造 intent",
    ))
    detect_offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "app/conversations").rglob("*.py")
        if "detect_course" in path.read_text(encoding="utf-8")
    ]
    graph = _read("app/conversations/graph.py")
    checks.append(_check(
        "S1.no_active_course_detection",
        not detect_offenders
        and "has_attachments and not query.strip()" in graph,
        f"course detection offenders: {detect_offenders}",
    ))

    manifest = json.loads(
        (ROOT / "docs/f010-d/snapshot-manifest.json").read_text(encoding="utf-8")
    )
    frozen = manifest.get("evals", {})
    frozen_actual = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in frozen
    }
    frozen_changed = [
        path for path, expected in frozen.items() if frozen_actual[path] != expected
    ]
    checks.append(_check(
        "S1.frozen_eval_hashes",
        not frozen_changed,
        f"A/B frozen changes: {frozen_changed}",
    ))

    answer_gate = hashlib.sha256(
        (ROOT / "app/conversations/answer_gate.py").read_bytes()
    ).hexdigest()[:16]
    checks.append(_check(
        "S1.answer_gate_frozen",
        answer_gate == ANSWER_GATE_SHA,
        f"sha256[:16]={answer_gate}",
    ))
    if C_ROOT.is_dir():
        c_core_mismatch = []
        for name, expected in C_CORE_SHA.items():
            current = hashlib.sha256(
                (ROOT / "app/conversations" / name).read_bytes()
            ).hexdigest()[:16]
            baseline = hashlib.sha256(
                (C_ROOT / "app/conversations" / name).read_bytes()
            ).hexdigest()[:16]
            if current != expected or baseline != expected or current != baseline:
                c_core_mismatch.append(
                    f"{name}: expected={expected}, C={baseline}, current={current}"
                )
        checks.append(_check(
            "S1.c_core_matches_C",
            not c_core_mismatch,
            "; ".join(c_core_mismatch) or "answer_gate/tool_projection 与 C 冻结件一致；final_reply 单列收尾修复",
        ))
        current_reply = hashlib.sha256(
            (ROOT / "app/conversations/final_reply.py").read_bytes()
        ).hexdigest()[:16]
        baseline_reply = hashlib.sha256(
            (C_ROOT / "app/conversations/final_reply.py").read_bytes()
        ).hexdigest()[:16]
        checks.append(_check(
            "S1.recovery_reviewed_close_revision",
            current_reply == RECOVERY_CLOSE_SHA and baseline_reply == RECOVERY_BASELINE_SHA,
            f"final_reply 已变更：C={baseline_reply}, close={current_reply}；"
            "真实 ExplanationResult 指纹与 /reply 验证见 tests/test_f010_close.py 和收尾证据",
        ))
    return checks


def s2_checks() -> list[dict]:
    search = _read("app/conversations/search.py")
    checks = [
        _check(
            "S2.page_error_blocks_scope_change",
            "page_error" in search and "PAGE_SCOPE_CHANGED" in search,
            "续页条件变化必须显式拒绝，而不是用旧 offset 查新条件",
        ),
        _check(
            "S2.continue_requires_previous_query",
            "_last_search_filter" in search and "continue_from_previous" in search,
            "继续必须从上一轮查询上下文取 next_offset/筛选",
        ),
        _check(
            "S2.pagination_tests_exist",
            (ROOT / "tests/test_f010_s1_pagination.py").is_file(),
            "首页/续页/列完/显式 offset 测试必须存在",
        ),
    ]
    prompt = _read("app/conversations/prompts.py")
    required_sections = [
        "【每轮怎么做】",
        "【目标与范围】",
        "【回答边界】",
        "【事实与故障】",
        "【示例】",
    ]
    missing_sections = [section for section in required_sections if section not in prompt]
    checks.append(_check(
        "S2.prompt_structure_sections",
        not missing_sections,
        f"missing prompt sections: {missing_sections}",
    ))
    checks.append(_check(
        "S2.prompt_fact_source_and_boundary",
        "你只能依据工具返回的事实说话" in prompt
        and "完整答案、参考代码和完整解法只能通过" in prompt
        and "目录、题库、记忆的故障分别说明" in prompt,
        "提示词需包含事实来源、回答边界和故障处理",
    ))
    example_section = prompt.split("【示例】", 1)[-1]
    examples = re.findall(r"(?m)^\d+\)", example_section)
    checks.append(_check(
        "S2.prompt_has_five_examples",
        len(examples) >= 5,
        f"example count: {len(examples)}",
    ))
    checks.append(_check(
        "S2.prompt_not_keyword_ban_list",
        not re.search(r"学生说[^\n]{0,80}(必须|只能|不要)[^\n]{0,40}调用", prompt),
        "不得回到逐条“学生说 X → 必须调用 Y”的禁令清单",
    ))
    prompt_length = len(prompt)
    checks.append(_check(
        "S2.prompt_length",
        prompt_length <= 6000,
        f"prompt chars: {prompt_length} (limit 6000)",
    ))
    return checks


def s3_checks() -> list[dict]:
    service = _read("app/conversations/service.py")
    legacy = _read("app/conversations/legacy.py")
    checks = [
        _check(
            "S3.legacy_router_only_in_legacy_path",
            "DeepSeekIntentRouter" in service
            and "assert self._make_router is not None" in legacy,
            "调度器路径存在时不得进入 legacy 路由",
        ),
        _check(
            "S3.dispatch_regression_test_exists",
            (ROOT / "tests/test_f010_s1_dispatch.py").is_file(),
            "必须有调度路径不调用旧 router 的动态测试",
        ),
        _check(
            "S3.legacy_router_not_default",
            "make_router: Callable[[], DeepSeekIntentRouter] | None = None" in service,
            "make_router 默认必须为 None",
        ),
    ]
    inventory = _read("docs/f010-d/01-路由盘点.md")
    route_ids = set(re.findall(r"(?m)^\| (R\d{2}) \|", inventory))
    expected_routes = {f"R{index:02d}" for index in range(1, 37)}
    checks.append(_check(
        "S3.route_inventory_36",
        expected_routes <= route_ids,
        f"active inventory={len(expected_routes)}, missing={sorted(expected_routes - route_ids)}, "
        f"retained={sorted(route_ids - expected_routes)}",
    ))
    mapping = _read("docs/f010-d/03-工具参数提案.md").split("## 六、", 1)[-1].split("## 七、", 1)[0]
    mapped_routes: set[str] = set()
    mapped_cases: set[str] = set()
    for line in mapping.splitlines():
        found = re.match(r"\| (R\d{2})(?:[–-](R\d{2}))?", line)
        if not found:
            continue
        start = int(found.group(1)[1:])
        end = int(found.group(2)[1:]) if found.group(2) else start
        mapped_routes.update(f"R{index:02d}" for index in range(start, end + 1))
        mapped_cases.update(re.findall(r"D\d{2}", line))
    active_routes = expected_routes - {"R01"}
    run_cases = set(re.findall(r"case_d(\d{2})", _read("evals/f010_d/run.py")))
    expected_cases = {f"D{index:02d}" for index in range(1, 15)}
    checks.append(_check(
        "S3.route_case_mapping_complete",
        active_routes <= mapped_routes
        and mapped_cases <= expected_cases
        and run_cases == {case[1:] for case in expected_cases},
        f"active mapped={len(active_routes & mapped_routes)}/{len(active_routes)}, "
        f"missing_routes={sorted(active_routes - mapped_routes)}, "
        f"d_cases_in_runner={len(run_cases)}/14",
    ))
    checks.append(_check(
        "S3.r01_fallback_documented",
        "R01" in route_ids and "不参与 D 的活动路径计数" in inventory,
        "R01 仅作为旧链路兜底，不参与活动路径计数",
    ))
    return checks


def main() -> int:
    report = {
        "S1": s1_checks(),
        "S2": s2_checks(),
        "S3": s3_checks(),
    }
    report["ok"] = all(item["ok"] for group in report.values() if isinstance(group, list) for item in group)
    output = ROOT / "tmp" / "f010-d" / "static-gates.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
