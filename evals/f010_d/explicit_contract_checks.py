"""F010 D 显式 A 用例的补充契约断言。

A 冻结集的 check 函数不能改。本模块只读 A runner 产出的 ``cases/<id>/case.json``，
对本轮三条对照补上“实际走了哪条状态转换”的断言：

* 08：首轮限定期末，次轮显式取消考试类型；
* 19：最后讲解确实成功，且用的是第 3 题；
* 47：记忆故障发生在高数，而不是拿 Python 的记录替代。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RUN_ROOT = Path(__file__).resolve().parents[2] / "tmp" / "f010-a"


def _case(run_dir: Path, case_id: str) -> dict[str, Any]:
    path = run_dir / "cases" / case_id / "case.json"
    if not path.is_file():
        raise AssertionError(f"missing case artifact: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _composite(turn: dict[str, Any], action: str) -> dict[str, Any] | None:
    for item in turn.get("result", {}).get("composite_results") or []:
        if item.get("action") == action:
            return item
    return None


def _message(turn: dict[str, Any]) -> str:
    return str(turn.get("result", {}).get("assistant_message") or "")


def verify_08(run_dir: Path) -> list[dict[str, Any]]:
    case = _case(run_dir, "08")
    calls = case.get("tool_calls") or []
    turns = case.get("turns") or []
    checks: list[dict[str, Any]] = []

    def require(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"case": "08", "name": name, "ok": bool(ok), "detail": detail})

    require(
        "首轮限定 exam_type=final",
        bool(calls) and calls[0]
        and calls[0][0].get("args", {}).get("exam_type") == "final",
        calls[0] if calls else None,
    )
    require(
        "次轮显式 all_exam_types=true 且取消 exam_type",
        len(calls) > 1 and calls[1]
        and calls[1][0].get("args", {}).get("all_exam_types") is True
        and "exam_type" not in calls[1][0].get("args", {}),
        calls[1] if len(calls) > 1 else None,
    )
    first = _message(turns[0]) if turns else ""
    second = _message(turns[1]) if len(turns) > 1 else ""
    require("首轮只出现期末 B，不出现期中 B", "期末试卷（B卷）" in first and "期中试卷（B卷）" not in first, first)
    require("次轮同时出现期中 B 和期末 B", "期末试卷（B卷）" in second and "期中试卷（B卷）" in second, second)
    cards = (turns[1].get("result", {}).get("question_results") or []) if len(turns) > 1 else []
    require("次轮仍以澄清候选呈现而非随机选题", not cards, cards)
    return checks


def verify_19(run_dir: Path) -> list[dict[str, Any]]:
    case = _case(run_dir, "19")
    turns = case.get("turns") or []
    checks: list[dict[str, Any]] = []

    def require(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"case": "19", "name": name, "ok": bool(ok), "detail": detail})

    selected = _composite(turns[1], "select_question") if len(turns) > 1 else None
    target = str((selected or {}).get("details", {}).get("question_id") or "")
    last = turns[-1].get("result", {}) if turns else {}
    explain = _composite(turns[-1], "explain_problem")
    question = last.get("question") or {}
    require("第 3 题选中并留下题目 ID", bool(target) and str(question.get("number_label") or "") == "3", target)
    require("最后讲解动作成功", bool(explain) and explain.get("status") == "success", explain)
    require(
        "讲解使用第 3 题的题目 ID",
        bool(target) and str(question.get("question_id") or "") == target
        and str((explain or {}).get("details", {}).get("question_id") or "") == target,
        {"target": target, "question": question.get("question_id")},
    )
    require("讲解结果非空", bool(last.get("tutoring")), bool(last.get("tutoring")))
    return checks


def verify_47(run_dir: Path) -> list[dict[str, Any]]:
    case = _case(run_dir, "47")
    calls = case.get("tool_calls") or []
    turns = case.get("turns") or []
    checks: list[dict[str, Any]] = []

    def require(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"case": "47", "name": name, "ok": bool(ok), "detail": detail})

    memory_call = calls[0][0] if calls and calls[0] else {}
    memory = _composite(turns[0], "review_memory") if turns else None
    require(
        "记忆故障请求的是高数",
        memory_call.get("name") == "review_learning_memory"
        and memory_call.get("args", {}).get("course") == "高等数学",
        memory_call,
    )
    require(
        "故障结果指向记忆读取失败",
        bool(memory) and memory.get("status") == "failed"
        and (memory.get("details") or {}).get("error_code") == "MEMORY_UNAVAILABLE",
        memory,
    )
    material = chr(10).join(str(item) for item in case.get("materials") or [])
    require(
        "故障不伪装成没有记录",
        "学习记忆暂时无法读取" in material and "没有记录" not in material,
        material[-700:],
    )
    query = _composite(turns[1], "query_papers") if len(turns) > 1 else None
    query_items = ((turns[1].get("result", {}).get("result") or {}).get("items") or []) if len(turns) > 1 else []
    require("随后高数查卷成功", bool(query) and query.get("status") == "success" and len(query_items) == 7, len(query_items))
    explain = _composite(turns[2], "explain_problem") if len(turns) > 2 else None
    require(
        "随后讲解成功",
        bool(explain) and explain.get("status") == "success"
        and bool(turns[2].get("result", {}).get("tutoring")),
        explain,
    )
    return checks


VERIFIERS = {"08": verify_08, "19": verify_19, "47": verify_47}


def verify(run_id: str, only: str = "") -> tuple[int, dict[str, Any]]:
    run_dir = RUN_ROOT / run_id
    selected = {item.strip() for item in only.split(",") if item.strip()}
    checks: list[dict[str, Any]] = []
    for case_id, verifier in VERIFIERS.items():
        if selected and case_id not in selected:
            continue
        checks.extend(verifier(run_dir))
    ok = all(item["ok"] for item in checks)
    return (0 if ok else 1), {"ok": ok, "checks": checks}
