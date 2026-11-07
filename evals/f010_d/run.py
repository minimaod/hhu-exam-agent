"""F010 D 专项离线评测（D01–D14，每题产品/机制 2 分，共 28 分）。

复用 B 批的真实产品栈夹具（真实 SQLite、真实题库副本、离线守卫），
只把决策/正文/讲解模型换成脚本替身。与 B 的用例、评分点独立。

用法::

    python -m evals.f010_d.run --run-id 20261105-D2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from app.conversations.graph import ActionPlan, ScheduleDecision

from evals.f010_b import fixtures as fx

D_ROOT = Path(__file__).resolve().parents[2] / "tmp" / "f010-d"
PY = fx.PY_COURSE


def plan(*actions: ActionPlan, course: str | None = PY) -> ScheduleDecision:
    return ScheduleDecision(actions=list(actions), confirmed_course=course, assistant_reply=None)


def act(name: str, **params: Any) -> ActionPlan:
    return ActionPlan(action=name, parameters=params)


def composites(turn: dict[str, Any]) -> list[Any]:
    result = turn.get("result")
    return list(getattr(result, "composite_results", None) or [])


def first(turn: dict[str, Any], action: str) -> Any:
    return next((item for item in composites(turn) if item.action == action), None)


def check(env: fx.BEnv, case: str, product: bool, mechanism: bool, note: str) -> None:
    env.add(case, f"{case}-product", product, note)
    env.add(case, f"{case}-mechanism", mechanism, note)


async def seed_choice_page(cs: fx.CaseService) -> dict[str, Any]:
    return await cs.turn(
        "列一下这份卷的选择题",
        decisions=[plan(act("list_paper_questions", paper_id=fx.PAPER, question_type="choice"))],
        replies=["已列出选择题。"],
    )


async def seed_fill_page(cs: fx.CaseService) -> dict[str, Any]:
    return await cs.turn(
        "列一下这份卷的填空题",
        decisions=[plan(act("list_paper_questions", paper_id=fx.PAPER, question_type="fill"))],
        replies=["已列出填空题。"],
    )


async def seed_selected(cs: fx.CaseService) -> None:
    await seed_choice_page(cs)
    await cs.turn(
        "第一题",
        decisions=[plan(act("select_question", ref="Q1"))],
        replies=["已选中第一题。"],
    )


async def seed_explain(cs: fx.CaseService) -> dict[str, Any]:
    await seed_selected(cs)
    return await cs.turn(
        "讲讲这道题",
        decisions=[plan(act("explain_problem", mode="new"))],
        replies=["这道题考循环。"],
    )


# ----------------------------------------------------------------------
# D01–D14
# ----------------------------------------------------------------------
async def case_d01(env: fx.BEnv) -> None:
    """年份/学期参数直接落到唯一试卷。"""

    cs = fx.CaseService(env, "D01")
    turn = await cs.turn(
        "查 2024-2025 第一学期 Python 期末卷",
        decisions=[plan(act(
            "query_papers", course=PY, exam_type="final",
            academic_year="2024-2025", term="s1",
        ))],
        replies=["找到这份卷。"],
    )
    item = first(turn, "query_papers")
    product = bool(
        item and item.details.get("paper_id") == fx.PAPER
        and item.details.get("total") == 1
    )
    step = cs.transcript(turn["request_id"]).get("steps", [])[0]
    mechanism = (
        step["plan"]["parameters"].get("academic_year") == "2024-2025"
        and step["plan"]["parameters"].get("term") == "s1"
        and item is not None and item.details.get("total") == 1
    )
    check(env, "D01", product, mechanism, "年份/学期参数化后唯一命中")


async def case_d02(env: fx.BEnv) -> None:
    """显式 paper_id 优先于原话里的另一份卷。"""

    cs = fx.CaseService(env, "D02")
    await seed_choice_page(cs)
    turn = await cs.turn(
        "还是看 2024-2025 第一学期那份",
        decisions=[plan(act("list_paper_questions", paper_id=fx.ALT_PAPER, question_type="choice"))],
        replies=["已列另一份卷。"],
    )
    item = first(turn, "list_paper_questions")
    product = bool(item and item.details.get("paper_id") == fx.ALT_PAPER)
    step = cs.transcript(turn["request_id"]).get("steps", [])[0]
    mechanism = step["plan"]["parameters"].get("paper_id") == fx.ALT_PAPER
    check(env, "D02", product, mechanism, "paper_id 参数优先")


async def case_d03(env: fx.BEnv) -> None:
    """scope=bank 不继承当前卷。"""

    cs = fx.CaseService(env, "D03")
    await seed_choice_page(cs)
    turn = await cs.turn(
        "去题库里找一道循环题",
        decisions=[plan(act("search_questions", scope="bank", query="循环", question_type="choice"))],
        replies=["按题库范围找。"],
    )
    item = first(turn, "search_questions")
    product = bool(item and item.status == "success")
    mechanism = bool(item and item.details.get("paper_id") is None)
    check(env, "D03", product, mechanism, "跨卷检索不锁定当前卷")


async def case_d04(env: fx.BEnv) -> None:
    """同号跨题型必须澄清，不能默认选择题。"""

    cs = fx.CaseService(env, "D04")
    await seed_choice_page(cs)
    await seed_fill_page(cs)
    turn = await cs.turn(
        "第五题讲一下",
        decisions=[
            plan(act("view_workspace")),
            plan(act("select_question", ref="Q41")),
        ],
        replies=["选择题还是填空题？"],
    )
    item = first(turn, "select_question")
    product = bool(item and item.status == "clarification")
    mechanism = bool(
        item and item.status == "clarification"
        and item.details.get("error_code") == "AMBIGUOUS_TARGET"
    )
    check(env, "D04", product, mechanism, "同号跨题型先澄清")


async def case_d05(env: fx.BEnv) -> None:
    """先只给提示，同一轮后面的看答案调用必须被拒。"""

    cs = fx.CaseService(env, "D05")
    await seed_selected(cs)
    turn = await cs.turn(
        "这题只给提示，但最后把答案给我",
        decisions=[plan(
            act("explain_problem", scope="hint"),
            act("view_question_answer"),
        )],
        replies=["只给提示。"],
    )
    hint = first(turn, "explain_problem")
    answer = first(turn, "view_question_answer")
    product = bool(
        hint and hint.status == "success"
        and answer and answer.status == "success"
        and answer.details.get("mode") == "public_material"
    )
    mechanism = bool(
        answer and answer.details.get("mode") == "public_material"
        and "answer" not in answer.details
        and "answer_detail" not in answer.details
    )
    check(env, "D05", product, mechanism, "同轮提示范围只回公开材料，不泄露答案")


async def case_d06(env: fx.BEnv) -> None:
    """defer_answer 标记后，后续答案调用被拒。"""

    cs = fx.CaseService(env, "D06")
    await seed_selected(cs)
    turn = await cs.turn(
        "先自己做，做完告诉我答案",
        decisions=[plan(
            act("search_questions", course=PY, query="循环", defer_answer=True),
            act("view_question_answer"),
        )],
        replies=["先自己做。"],
    )
    search = first(turn, "search_questions")
    answer = first(turn, "view_question_answer")
    product = bool(search and answer and answer.status == "failed")
    mechanism = bool(answer and answer.details.get("error_code") == "ANSWER_SCOPE_DENIED")
    check(env, "D06", product, mechanism, "defer_answer 延迟答案")


async def case_d07(env: fx.BEnv) -> None:
    """反馈成功后同轮不得继续出题或保存。"""

    cs = fx.CaseService(env, "D07")
    await seed_explain(cs)
    turn = await cs.turn(
        "我懂了，再来一道",
        decisions=[plan(act("record_feedback"), act("generate_exercise"))],
        replies=["已记录。"],
    )
    feedback = first(turn, "record_feedback")
    exercise = first(turn, "generate_exercise")
    product = bool(feedback and feedback.status == "success" and exercise and exercise.status == "failed")
    mechanism = bool(
        exercise
        and exercise.details.get("error_code") == "RED_LINE_CONFLICT"
        and exercise.details.get("needs_user") is False
    )
    check(env, "D07", product, mechanism, "我懂了后同轮不再出题")


async def case_d08(env: fx.BEnv) -> None:
    """助手建议练习后，学生说“不用了”：不出新练习，也不强推同一建议。"""

    cs = fx.CaseService(env, "D08")
    await seed_explain(cs)
    before = cs.sql("SELECT COUNT(*) AS n FROM exercises")[0]["n"]
    turn = await cs.turn(
        "不用了",
        decisions=[plan(act("generate_exercise"))],
        replies=["好的，需要时再告诉我。"],
    )
    item = first(turn, "generate_exercise")
    after = cs.sql("SELECT COUNT(*) AS n FROM exercises")[0]["n"]
    text = str(getattr(turn.get("result"), "assistant_message", "") or "")
    product = bool(
        item
        and item.status == "failed"
        and after == before
        and "变式" not in text
    )
    mechanism = bool(
        item
        and item.details.get("error_code") == "AUTHORIZATION_REQUIRED"
        and item.details.get("rejected") is True
        and item.details.get("needs_user") is False
    )
    check(env, "D08", product, mechanism, "拒绝建议后不出新练习，不强推变式")


async def case_d09(env: fx.BEnv) -> None:
    """上轮两条建议，学生只说“好”：先问指哪一条，确认前零写入。"""

    cs = fx.CaseService(env, "D09")
    await seed_explain(cs)
    cs.repo.upsert_context(
        cs.conversation(),
        last_suggestions_json=json.dumps(
            [
                {"id": "s-1", "label": "练一道", "action": "generate_exercise"},
                {"id": "s-2", "label": "保存错题", "action": "record_mistake"},
            ],
            ensure_ascii=False,
        ),
    )
    before = cs.sql("SELECT COUNT(*) AS n FROM mistake_records")[0]["n"]
    turn = await cs.turn(
        "好",
        decisions=[plan(act("record_mistake"))],
        replies=["你想先做哪一条？"],
    )
    item = first(turn, "record_mistake")
    after = cs.sql("SELECT COUNT(*) AS n FROM mistake_records")[0]["n"]
    product = bool(item and item.status == "clarification" and after == before)
    mechanism = bool(
        item
        and item.details.get("error_code") == "AMBIGUOUS_BASIS"
        and item.details.get("needs_user") is True
    )
    check(env, "D09", product, mechanism, "两条建议只回“好”先澄清，零写入")


async def case_d10(env: fx.BEnv) -> None:
    """伪造建议 ID 不能作为依据。"""

    cs = fx.CaseService(env, "D10")
    await seed_explain(cs)
    turn = await cs.turn(
        "保存这道题",
        decisions=[plan(act(
            "record_mistake",
            basis={"kind": "confirmed_suggestion", "suggestion_id": "s-99"},
        ))],
        replies=["没有这条建议。"],
    )
    item = first(turn, "record_mistake")
    product = bool(item and item.status == "failed" and item.details.get("needs_user") is False)
    mechanism = bool(
        item and item.details.get("error_code") in ("BASIS_UNVERIFIED", "AMBIGUOUS_BASIS")
    )
    check(env, "D10", product, mechanism, "伪造建议 ID 被拒")


async def case_d11(env: fx.BEnv) -> None:
    """删除学习记忆必须有明确原话；无依据的删除不执行。"""

    cs = fx.CaseService(env, "D11")
    turn = await cs.turn(
        "这题我其实会做",
        decisions=[plan(act("delete_memory", entry_id="exercise:missing"))],
        replies=["没有删除。"],
    )
    item = first(turn, "delete_memory")
    product = bool(item and item.status in ("failed", "clarification"))
    mechanism = bool(
        item
        and item.details.get("error_code") == "AUTHORIZATION_REQUIRED"
        and item.details.get("needs_user") is True
    )
    check(env, "D11", product, mechanism, "无明确删除原话先澄清，不执行")


async def case_d12(env: fx.BEnv) -> None:
    """换课程不串用上一门课的卷。"""

    cs = fx.CaseService(env, "D12")
    await cs.turn(
        "查 Python 期末卷",
        decisions=[plan(act("query_papers", course=PY, exam_type="final"))],
        replies=["找到 Python 卷。"],
    )
    turn = await cs.turn(
        "换高数期末卷",
        decisions=[plan(act("query_papers", course="高等数学", exam_type="final"))],
        replies=["查高数。"],
    )
    item = first(turn, "query_papers")
    product = bool(item and item.status in ("success", "clarification"))
    mechanism = bool(item and item.details.get("course") == "高等数学")
    check(env, "D12", product, mechanism, "换课程不继承旧卷")


async def case_d13(env: fx.BEnv) -> None:
    """offset 参数翻页，不重复同一批。"""

    cs = fx.CaseService(env, "D13")
    first_turn = await cs.turn(
        "第一页选择题",
        decisions=[plan(act(
            "list_paper_questions",
            paper_id=fx.PAPER, question_type="choice", offset=4, limit=10,
        ))],
        replies=["第一页。"],
    )
    second_turn = await cs.turn(
        "下一批",
        decisions=[plan(act(
            "list_paper_questions",
            paper_id=fx.PAPER, question_type="choice", offset=14, limit=10,
        ))],
        replies=["下一批。"],
    )
    one = first(first_turn, "list_paper_questions")
    two = first(second_turn, "list_paper_questions")
    product = bool(one and two and one.details.get("offset") == 4 and two.details.get("offset") == 14)
    mechanism = bool(
        two and two.details.get("question_ids")
        and not set(one.details.get("question_ids") or []) & set(two.details.get("question_ids") or [])
    )
    check(env, "D13", product, mechanism, "offset 翻页不重复")


async def case_d14(env: fx.BEnv) -> None:
    """错误依据 → 同轮改正 → 成功。"""

    cs = fx.CaseService(env, "D14")
    await seed_choice_page(cs)
    await seed_explain(cs)
    turn = await cs.turn(
        "讲这道题，保存这道题",
        decisions=[
            plan(act(
                "record_mistake",
                basis={"kind": "tool_result", "depends_on_call_id": "missing"},
            )),
            plan(act(
                "record_mistake",
                basis={"kind": "student_request", "quote": "保存这道题"},
            )),
        ],
        replies=["已保存。"],
    )
    steps = cs.transcript(turn["request_id"]).get("steps") or []
    saves = [step for step in steps if step["plan"]["action"] == "record_mistake"]
    product = bool(
        turn.get("result") is not None
        and turn["result"].business_status == "succeeded"
        and len(saves) == 2
    )
    mechanism = (
        len(saves) == 2
        and saves[0]["outcome"]["status"] == "failed"
        and saves[0]["outcome"]["needs_user"] is False
        and saves[1]["outcome"]["status"] == "success"
    )
    check(env, "D14", product, mechanism, "错误依据同轮改正后成功")


CASES = {
    "D01": case_d01, "D02": case_d02, "D03": case_d03, "D04": case_d04,
    "D05": case_d05, "D06": case_d06, "D07": case_d07, "D08": case_d08,
    "D09": case_d09, "D10": case_d10, "D11": case_d11, "D12": case_d12,
    "D13": case_d13, "D14": case_d14,
}


def write_summary(env: fx.BEnv, started: float) -> dict[str, Any]:
    summary = {
        "run_id": env.run_dir.name,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "seconds": round(time.perf_counter() - started, 2),
        "score": env.score,
        "max_score": len(env.results),
        "units": env.results,
        "all_passed": env.score == len(env.results) and bool(env.results),
        "egress_attempts": env.guard.egress_attempts,
        "offline_self_test_blocked": env.guard.self_test_blocked,
        "bank_build": fx.BANK_BUILD,
    }
    (env.run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="F010 D 专项离线评测")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--only", default="", help="只跑指定用例（逗号分隔）")
    args = parser.parse_args(argv)
    if not args.run_id or "/" in args.run_id or "\\" in args.run_id:
        raise SystemExit("run-id 必须是单段目录名")
    D_ROOT.mkdir(parents=True, exist_ok=True)
    # B 夹具默认写到 tmp/f010-b；D 改为自己的目录，保持证据隔离。
    fx.RUN_ROOT = D_ROOT
    fx._a.RUN_ROOT = D_ROOT
    started = time.perf_counter()
    env = fx.bootstrap(args.run_id)
    selected = [item.strip() for item in args.only.split(",") if item.strip()]
    try:
        for case_id, case in CASES.items():
            if selected and case_id not in selected:
                continue
            try:
                asyncio.run(case(env))
            except Exception as exc:
                traceback.print_exc()
                env.add(case_id, f"{case_id}-product", False, f"{type(exc).__name__}: {exc}")
                env.add(case_id, f"{case_id}-mechanism", False, f"{type(exc).__name__}: {exc}")
    finally:
        summary = write_summary(env, started)
    print(json.dumps({k: summary[k] for k in ("run_id", "score", "max_score", "all_passed")}, ensure_ascii=False))
    for unit in summary["units"]:
        print(("PASS " if unit["ok"] else "FAIL ") + unit["unit"] + "  " + unit["detail"][:120])
    return 0


if __name__ == "__main__":
    sys.exit(main())