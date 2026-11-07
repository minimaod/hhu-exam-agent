"""F010 B 专项离线评测（12 组 × 2 个评分点 = 24 分）。

每个用例都走真实产品栈：真实 SQLite、真实题库副本、真实调度图与 SSE；
只把“决策模型 / 正文模型 / 讲解模型”换成脚本替身。两组评分点分别是
“学生看到的产品结果”和“实现机制是否真的按约定工作”。

用法::

    python -m evals.f010_b.run --run-id 20261105-B1
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from app.conversations.graph import ActionPlan, ScheduleDecision

from evals.f010_b import fixtures as fx

CASES_PATH = Path(__file__).resolve().parent / "cases.json"
PY = fx.PY_COURSE


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def plan(*actions: ActionPlan, course: str | None = PY) -> ScheduleDecision:
    return ScheduleDecision(
        actions=list(actions), confirmed_course=course, assistant_reply=None
    )


def act(name: str, **params: Any) -> ActionPlan:
    return ActionPlan(action=name, parameters=params)


def last_result(turn: dict[str, Any]) -> Any:
    return turn.get("result")


def composites(turn: dict[str, Any]) -> list[Any]:
    result = last_result(turn)
    return list(getattr(result, "composite_results", None) or [])


def first(turn: dict[str, Any], action: str) -> Any:
    for item in composites(turn):
        if item.action == action:
            return item
    return None


def sse_text(turn: dict[str, Any]) -> str:
    return json.dumps(
        [event.model_dump(mode="json") for event in turn["events"]],
        ensure_ascii=False,
    )


def turn_text(turn: dict[str, Any]) -> str:
    result = last_result(turn)
    return str(getattr(result, "assistant_message", None) or "") if result else ""


async def seed_choice_page(cs: fx.CaseService, offset: int = 4) -> dict[str, Any]:
    """真实“列这张卷的选择题”轮次；offset=4 时 Q1 就是原卷第 5 题。"""

    return await cs.turn(
        "这张卷的选择题列一下",
        decisions=[
            plan(
                act(
                    "list_paper_questions",
                    paper_id=fx.PAPER,
                    question_type="choice",
                    offset=offset,
                )
            )
        ],
        replies=["已列出这份卷的选择题。"],
    )


async def seed_fill_page(cs: fx.CaseService, offset: int = 0) -> dict[str, Any]:
    return await cs.turn(
        "这张卷的填空题列一下",
        decisions=[
            plan(
                act(
                    "list_paper_questions",
                    paper_id=fx.PAPER,
                    question_type="fill",
                    offset=offset,
                )
            )
        ],
        replies=["已列出这份卷的填空题。"],
    )


def check(env: fx.BEnv, case: str, product: bool, mechanism: bool, note: str) -> None:
    env.add(case, f"{case}-product", product, note)
    env.add(case, f"{case}-mechanism", mechanism, note)


# ----------------------------------------------------------------------
# 12 组用例
# ----------------------------------------------------------------------
async def case_b01(env: fx.BEnv) -> None:
    """跨轮找回试卷和“第五题”：ref 与原卷题号不是一回事。"""

    cs = fx.CaseService(env, "B01")
    await seed_choice_page(cs, offset=4)
    turn = await cs.turn(
        "刚才那张卷第五题给我讲讲",
        decisions=[
            plan(act("view_workspace")),
            plan(act("select_question", ref="Q1"), act("explain_problem", mode="new")),
        ],
        replies=["这道题考运算符与数据类型。"],
    )
    detail = first(turn, "select_question")
    explain = first(turn, "explain_problem")
    product = (
        detail is not None
        and detail.status == "success"
        and detail.details.get("number_label") == "5"
        and explain is not None
        and explain.status == "success"
        and bool(cs.tutor.explain_calls)
    )
    steps = cs.transcript(turn["request_id"]).get("steps") or []
    select_step = next(
        (item for item in steps if item["plan"]["action"] == "select_question"), None
    )
    mechanism = (
        [item["plan"]["action"] for item in steps]
        == ["view_workspace", "select_question", "explain_problem"]
        and select_step is not None
        and select_step["plan"]["parameters"].get("ref") == "Q1"
        and select_step["outcome"]["details"].get("question_id")
        == "py-2024-2025-s1-final-choice-05"
        and any(bool(call.get("history")) for call in cs.scheduler.calls)
        and "当前试卷" in (cs.scheduler.calls[-1].get("workspace_text") or "")
    )
    check(env, "B01", product, mechanism, "第五题=Q1（原卷编号 5），讲解命中同一版本")


async def case_b02(env: fx.BEnv) -> None:
    """同号跨题型：选择第 5 题与填空第 5 题同时在页面上，必须先澄清。"""

    cs = fx.CaseService(env, "B02")
    await seed_choice_page(cs, offset=4)
    await seed_fill_page(cs, offset=0)
    turn = await cs.turn(
        "第五题给我讲讲",
        decisions=[
            plan(act("view_workspace")),
            plan(act("select_question", ref="Q41"), act("explain_problem", mode="new")),
        ],
        replies=["你想听选择题还是填空题？"],
    )
    item = first(turn, "select_question")
    product = (
        item is not None
        and item.status == "clarification"
        and "选择" in item.message
        and "填空" in item.message
        and not cs.tutor.explain_calls
    )
    selections = cs.sql(
        "SELECT COUNT(*) AS n FROM question_selections WHERE conversation_id=?",
        (cs.conversation_id,),
    )
    mechanism = (
        item is not None
        and item.details.get("error_code") == "AMBIGUOUS_TARGET"
        and selections[0]["n"] == 0
        and first(turn, "explain_problem") is None
    )
    check(env, "B02", product, mechanism, "同号两题型先澄清，不建立选中、不讲解")


async def case_b03(env: fx.BEnv) -> None:
    """长历史：只注入预算内的最近对话，更早轮次折叠成摘要。"""

    cs = fx.CaseService(env, "B03")
    for _ in range(7):
        await cs.turn(
            "看看现在的题目",
            decisions=[plan(act("view_workspace"))],
            replies=["当前工作区如下。"],
        )
    turn = await cs.turn(
        "刚才那张卷是哪份",
        decisions=[plan(act("view_workspace"))],
        replies=["是这份卷。"],
    )
    history = cs.last_history()
    product = first(turn, "view_workspace") is not None and first(turn, "view_workspace").status == "success"
    mechanism = (
        0 < len(history) <= 24
        and any(
            str(item.get("content", "")).startswith("[r")
            for item in history
            if item.get("role") == "user"
        )
        or (
            0 < len(history) <= 24
            and sum(1 for item in history if item.get("role") == "user") < 7
        )
    )
    check(env, "B03", product, mechanism, "历史有上限；被裁掉的旧轮次只留摘要")


async def case_b04(env: fx.BEnv) -> None:
    """换卷失败：只报题号不回落旧卷，显式引用仍可打开历史题卡。"""

    cs = fx.CaseService(env, "B04")
    await seed_choice_page(cs, offset=4)
    await cs.turn(
        "换到 2030-2031 学年的期末卷",
        decisions=[
            plan(
                act(
                    "query_papers",
                    course=PY,
                    academic_year="2030-2031",
                    exam_type="final",
                )
            )
        ],
        replies=["目录里没有这个学年的卷。"],
    )
    turn = await cs.turn(
        "刚才那张卷第五题给我看看",
        decisions=[
            plan(
                act("view_workspace"),
                act("view_question_detail", ref="Q1"),
                act("select_question", number_label="5"),
            ),
        ],
        replies=["没有定位到新的试卷；显式打开的是当时那张卡。"],
    )
    workspace = first(turn, "view_workspace")
    detail = first(turn, "view_question_detail")
    select = first(turn, "select_question")
    paper = (((workspace.details or {}).get("workspace") or {}).get("paper") or {}) if workspace else {}
    product = (
        detail is not None
        and detail.status == "success"
        and detail.details.get("number_label") == "5"
        and str(detail.details.get("question_id") or "").startswith(fx.PAPER)
        and select is not None
        and select.status in ("failed", "clarification")
        and "试卷" in select.message
    )
    mechanism = (
        detail is not None
        and detail.details.get("from_snapshot") is True
        and paper.get("status") == "unlocated"
        and ((workspace.details or {}).get("workspace") or {}).get("shown_count") == 36
        and cs.sql(
            "SELECT COUNT(*) AS n FROM question_selections WHERE conversation_id=?",
            (cs.conversation_id,),
        )[0]["n"]
        == 0
    )
    check(env, "B04", product, mechanism, "失败换卷后题号不回落；显式 Q 引用仍打开历史题卡")


async def case_b05(env: fx.BEnv) -> None:
    """刷新/重启后只答“B 卷”：状态从库里重建，不依赖内存快照。"""

    cs = fx.CaseService(env, "B05")
    await seed_choice_page(cs, offset=4)
    cs.restart()
    restored = await cs.turn(
        "现在看的是哪份卷",
        decisions=[plan(act("view_workspace"))],
        replies=["还是刚才那份。"],
    )
    await cs.turn(
        "换到 2024-2025 学年第二学期的期末卷，把选择题列一下",
        decisions=[
            plan(act("query_papers", course=PY, exam_type="final")),
            plan(
                act(
                    "list_paper_questions",
                    paper_id=fx.ALT_PAPER,
                    question_type="choice",
                    offset=0,
                )
            ),
        ],
        replies=["已定位到第二学期的卷。"],
    )
    turn = await cs.turn(
        "第一题给我讲讲",
        decisions=[
            plan(act("view_workspace"), act("select_question", number_label="1")),
            plan(act("explain_problem", mode="new")),
        ],
        replies=["这道题来自第二学期期末卷。"],
    )
    workspace = first(restored, "view_workspace")
    product = (
        workspace is not None
        and workspace.status == "success"
        and "2024-2025" in workspace.message
        and first(turn, "select_question") is not None
        and first(turn, "select_question").status == "success"
    )
    paper_id = (first(turn, "select_question").details.get("paper_id") if first(turn, "select_question") else None)
    mechanism = paper_id == fx.ALT_PAPER and bool(cs.scheduler.calls[1].get("workspace_text"))
    check(env, "B05", product, mechanism, "重启后工作区由库重建；换卷后选中属于新卷")


async def case_b06(env: fx.BEnv) -> None:
    """重叠分页：同题只占一个引用，Q 编号稳定。"""

    cs = fx.CaseService(env, "B06")
    await seed_choice_page(cs, offset=0)
    await seed_choice_page(cs, offset=10)
    turn = await cs.turn(
        "第十一题给我讲讲",
        decisions=[
            plan(act("view_workspace"), act("select_question", ref="Q11")),
            plan(act("explain_problem", mode="new")),
        ],
        replies=["这是第 11 题。"],
    )
    workspace = first(turn, "view_workspace")
    select = first(turn, "select_question")
    product = (
        select is not None
        and select.status == "success"
        and select.details.get("number_label") == "11"
        and select.details.get("question_id") == "py-2024-2025-s1-final-choice-11"
    )
    shown = ((workspace.details or {}).get("workspace") or {}).get("shown_count") if workspace else None
    mechanism = shown == 40 and select is not None and select.details.get("ref") == "Q11"
    check(env, "B06", product, mechanism, "重叠页只保留 40 个引用，Q11 命中第 11 题")


async def case_b07(env: fx.BEnv) -> None:
    """历史题卡：页面之后变了，旧引用仍指向当时那张卡。"""

    cs = fx.CaseService(env, "B07")
    await seed_choice_page(cs, offset=4)
    await seed_fill_page(cs, offset=0)
    turn = await cs.turn(
        "最早看的那道第五题是什么",
        decisions=[plan(act("view_question_detail", ref="Q1"))],
        replies=["这是最早那道题。"],
    )
    detail = first(turn, "view_question_detail")
    product = (
        detail is not None
        and detail.status == "success"
        and detail.details.get("number_label") == "5"
        and detail.details.get("question_type") == "choice"
    )
    mechanism = (
        detail is not None
        and detail.details.get("from_snapshot") is True
        and detail.details.get("question_id") == "py-2024-2025-s1-final-choice-05"
        and bool(detail.details.get("content_sha256"))
    )
    check(env, "B07", product, mechanism, "旧引用带版本快照，不被后一页覆盖")


async def case_b08(env: fx.BEnv) -> None:
    """版本失效：引用校验失败，不能拿当前选中的别题顶替。"""

    cs = fx.CaseService(env, "B08")
    seed = await seed_choice_page(cs, offset=4)

    def break_build(payload: dict[str, Any]) -> None:
        payload["question_results"][0]["build_id"] = "iv-outdated-build"

    cs.mutate_output(seed["request_id"], break_build)
    first_turn = await cs.turn(
        "看看最早那道第五题",
        decisions=[plan(act("view_question_detail", ref="Q1"))],
        replies=["这道题需要重新查询。"],
    )
    await cs.turn(
        "换成第二题",
        decisions=[plan(act("select_question", index=2))],
        replies=["已换到第二题。"],
    )
    second_turn = await cs.turn(
        "再把第一题给我看看",
        decisions=[plan(act("view_question_detail", ref="Q1"))],
        replies=["这道题还是需要重新查询。"],
    )
    a = first(first_turn, "view_question_detail")
    b = first(second_turn, "view_question_detail")
    product = (
        a is not None
        and a.status == "failed"
        and "重新" in a.message
        and b is not None
        and b.status == "failed"
    )
    mechanism = (
        a is not None
        and b is not None
        and a.details.get("error_code") == "VERSION_CHANGED"
        and b.details.get("error_code") == "VERSION_CHANGED"
    )
    check(env, "B08", product, mechanism, "旧 build 永远拒绝，选中别的题也不顶替")


async def case_b09(env: fx.BEnv) -> None:
    """答案与练习解答的授权边界：当轮可以给，跨轮历史只留结构化事实。"""

    import dataclasses
    from datetime import datetime, timezone

    from app.conversations.models import ExerciseView
    from app.question_bank.models import AnswerDetail

    cs = fx.CaseService(env, "B09")
    marker = "F010B-EVAL-ANSWER-A"
    solution_marker = "F010B-EVAL-SOLUTION-A"
    original = cs.runtime.answer_view

    def marked(selection):
        card, evidence = original(selection)
        detail = AnswerDetail(state="original", text=f"B（{marker}）")
        return (
            card.model_copy(update={"answer_detail": detail}),
            dataclasses.replace(evidence, answer_detail=detail),
        )

    cs.runtime.answer_view = marked  # type: ignore[method-assign]
    await seed_choice_page(cs, offset=4)
    answered = await cs.turn(
        "第一题答案是什么",
        decisions=[
            plan(
                act("select_question", ref="Q1"),
                act("view_question_answer"),
            )
        ],
        replies=["这题答案是 B。"],
    )
    leaked_to_student = marker in sse_text(answered)

    # 再落一轮“练习参考解答已展开”，它的正文同样不能进入下一轮历史。
    conversation = cs.conversation()
    now = datetime.now(timezone.utc)
    seed_id = "seed-exercise"
    cs.repo.start_turn(conversation, seed_id, "看看这道练习", kind="dynamic")
    cs.repo.complete_dynamic_turn(
        conversation,
        seed_id,
        kind="dynamic",
        exercise=ExerciseView(
            id="ex-eval",
            conversation_id=conversation,
            problem_id="p-eval",
            root_problem_id="p-eval",
            statement_text="写一个求和函数",
            knowledge_point="循环",
            status="revealed",
            revealed_solution_at=now,
            reference_solution_text=f"参考解答：{solution_marker}",
            created_at=now,
            updated_at=now,
        ),
        assistant_message="参考解答已经展开。",
        exercise_id="ex-eval",
    )
    later = await cs.turn(
        "第二题讲一下",
        decisions=[
            plan(act("select_question", ref="Q2"), act("explain_problem", mode="new")),
        ],
        replies=["第二题考变量。"],
    )
    history = json.dumps(cs.last_history(), ensure_ascii=False)
    product = leaked_to_student and marker not in sse_text(later)
    mechanism = (
        marker not in history
        and solution_marker not in history
        and "参考解答" not in history
        and marker
        not in json.dumps(cs.transcript(answered["request_id"]), ensure_ascii=False)
    )
    check(env, "B09", product, mechanism, "答案/解答当轮可给，历史投影只留结构化事实")


async def case_b10(env: fx.BEnv) -> None:
    """学习摘要：读事实、不改课程；没有记录就直说没有。"""

    cs = fx.CaseService(env, "B10")
    cs.repo.upsert_context(cs.conversation(), confirmed_course=PY)
    before = cs.repo.get_context(cs.conversation()).confirmed_course
    turn = await cs.turn(
        "高等数学最近学得怎么样",
        decisions=[
            plan(
                act(
                    "view_learning_summary",
                    course="高等数学",
                    days=30,
                    limit=10,
                )
            )
        ],
        replies=["还没有高数的学习记录。"],
    )
    item = first(turn, "view_learning_summary")
    after = cs.repo.get_context(cs.conversation()).confirmed_course
    product = (
        item is not None
        and item.status in ("success", "failed")
        and item.status != "clarification"
    )
    mechanism = (
        before == PY
        and after == PY
        and cs.scheduler.calls[-1].get("confirmed_course") == PY
    )
    check(env, "B10", product, mechanism, "学习摘要是只读事实，course 参数不切换课程")


async def case_b11(env: fx.BEnv) -> None:
    """中断后重试：已提交的保存不重复，恢复步骤以 carried 形式进入新轮。"""

    from tests.f009_support import Seed
    from tests.tutoring_fakes import sample_explanation

    cs = fx.CaseService(env, "B11")
    seed = Seed(cs.db)
    problem = seed.problem(
        cs.conversation(),
        statement="求列表中出现次数最多的元素",
        paper_id=fx.PAPER,
        source_type="registered_paper",
    )
    with sqlite3.connect(cs.db) as connection:
        connection.execute(
            "UPDATE problems SET explanation_json=? WHERE id=?",
            (
                json.dumps(
                    sample_explanation().model_dump(mode="json"), ensure_ascii=False
                ),
                problem,
            ),
        )
    cs.repo.upsert_context(
        cs.conversation(), confirmed_course=PY, active_problem_id=problem
    )
    original = cs.repo.save_recovery

    def flaky(state: dict[str, Any]) -> Any:
        actions = state.get("actions") or []
        if (
            len(actions) == 3
            and actions[-1].get("plan", {}).get("action") == "view_workspace"
            and actions[-1].get("outcome") is None
        ):
            raise RuntimeError("模拟进程中断")
        return original(state)

    # save_recovery(conversation_id, request_id, recovery)
    def hooked(conversation_id: str, request_id: str, recovery: dict[str, Any]) -> None:
        actions = recovery.get("actions") or []
        if (
            len(actions) == 3
            and actions[-1].get("plan", {}).get("action") == "view_workspace"
            and actions[-1].get("outcome") is None
        ):
            raise RuntimeError("模拟进程中断")
        return original(conversation_id, request_id, recovery)

    cs.repo.save_recovery = hooked  # type: ignore[method-assign]
    text = "讲讲这道题，然后保存，再看看工作区"
    first_turn = await cs.turn(
        text,
        decisions=[
            plan(
                act("explain_problem", mode="new"),
                act("record_mistake"),
                act("view_workspace"),
            )
        ],
        replies=["已保存。"],
    )
    cs.repo.save_recovery = original  # type: ignore[method-assign]
    before = cs.sql(
        "SELECT id, problem_version, request_id, updated_at FROM mistake_records "
        "WHERE conversation_id=?",
        (cs.conversation_id,),
    )
    retried = await cs.turn(text, retry_of=first_turn["request_id"], replies=["已保存。"], decisions=[])
    after = cs.sql(
        "SELECT id, problem_version, request_id, updated_at FROM mistake_records "
        "WHERE conversation_id=?",
        (cs.conversation_id,),
    )
    steps = cs.transcript(retried["request_id"]).get("steps") or []
    carried = [step for step in steps if step.get("carried")]
    product = (
        retried["result"] is not None
        and retried["result"].kind == "dynamic"
        and len(before) == 1
        and len(after) == 1
        and before == after
    )
    mechanism = (
        before == after
        and len(carried) == 2
        and {step["from_request_id"] for step in carried}
        == {first_turn["request_id"]}
        and [step["plan"]["action"] for step in steps]
        == ["explain_problem", "record_mistake", "view_workspace"]
    )
    check(env, "B11", product, mechanism, "崩溃重试不重复保存，carried 步骤只记录不执行")


async def case_b12(env: fx.BEnv) -> None:
    """重新生成正文：只换文字，不重跑工具、不改 transcript。"""

    cs = fx.CaseService(env, "B12")
    await seed_choice_page(cs, offset=4)
    turn = await cs.turn(
        "刚才那张卷第五题给我讲讲",
        decisions=[
            plan(act("select_question", ref="Q1"), act("explain_problem", mode="new"))
        ],
        replies=["第一次正文。"],
    )
    before = cs.transcript(turn["request_id"])
    from tests.tool_fakes import ReplyFactory

    cs.service._make_reply = ReplyFactory(["重做后的正文。"])
    regenerated = await cs.service.regenerate_reply(
        cs.conversation_id, turn["request_id"]
    )
    after = cs.transcript(turn["request_id"])
    rows = cs.sql(
        "SELECT COUNT(*) AS n FROM conversation_turns WHERE conversation_id=?",
        (cs.conversation_id,),
    )
    product = str(regenerated.get("assistant_message") or "").startswith(
        "重做后的正文。"
    )
    mechanism = before == after and rows[0]["n"] == 2
    check(env, "B12", product, mechanism, "/reply 只换正文，transcript 与轮次数量不变")


CASES = {
    "B01": case_b01,
    "B02": case_b02,
    "B03": case_b03,
    "B04": case_b04,
    "B05": case_b05,
    "B06": case_b06,
    "B07": case_b07,
    "B08": case_b08,
    "B09": case_b09,
    "B10": case_b10,
    "B11": case_b11,
    "B12": case_b12,
}


def write_summary(env: fx.BEnv, started: float) -> dict[str, Any]:
    import hashlib

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
        "cases_sha256": hashlib.sha256(CASES_PATH.read_bytes()).hexdigest(),
        "bank_build": fx.BANK_BUILD,
    }
    (env.run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="F010 B 专项离线评测")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--only", default="", help="只跑指定用例（逗号分隔）")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    if not CASES_PATH.exists():
        raise SystemExit(f"缺少用例清单：{CASES_PATH}")
    meta = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    env = fx.bootstrap(args.run_id)
    selected = [item.strip() for item in args.only.split(",") if item.strip()]
    try:
        import asyncio

        for case_id in meta["cases"]:
            if selected and case_id not in selected:
                continue
            case = CASES.get(case_id)
            if case is None:
                env.add(case_id, f"{case_id}-product", False, "未实现的用例")
                env.add(case_id, f"{case_id}-mechanism", False, "未实现的用例")
                continue
            try:
                asyncio.run(case(env))
            except Exception as exc:  # 一个用例失败不阻断其余证据
                import traceback

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