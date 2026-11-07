"""F010 C 专项离线评测执行器。

用法（离线、无真实模型）：
    PYTHONPATH=. .venv/Scripts/python.exe -B evals/f010_c/run.py --run-id 20261104-C1

判分：11 组 × 2 维（目标与事实 / 流程与状态）= 22；组内两项都满足才得分。
覆盖 C 批四项验收（跨题/跨版本授权、工具轮文字不出站、业务提交后断流、
只重做正文）以及逐字段投影、拒绝信封、终态判定、delta 契约、重复请求、
旧 reply_input 兼容与进程重启恢复。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from app.conversations.graph import ActionPlan, ScheduleDecision, SchedulerAgent

from evals.f010_c import fixtures as fx

RUN_KIND = "f010-c"


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def plan(*actions: ActionPlan, reply: str | None = None) -> ScheduleDecision:
    return ScheduleDecision(
        actions=list(actions), confirmed_course=fx.PY_COURSE, assistant_reply=reply
    )


def act(name: str, **params: Any) -> ActionPlan:
    return ActionPlan(action=name, parameters=params)


def composites(turn: dict[str, Any]) -> list[Any]:
    result = turn.get("result")
    return list(getattr(result, "composite_results", None) or [])


def first(turn: dict[str, Any], action: str) -> Any:
    return next((item for item in composites(turn) if item.action == action), None)


def sse_text(turn: dict[str, Any]) -> str:
    return json.dumps(
        [event.model_dump(mode="json") for event in turn["events"]],
        ensure_ascii=False,
    )


def turn_text(turn: dict[str, Any]) -> str:
    result = turn.get("result")
    return str(getattr(result, "assistant_message", None) or "") if result else ""


def deltas(turn: dict[str, Any]) -> list[Any]:
    return [event for event in turn["events"] if event.type == "message_delta"]


def check(env: fx.BEnv, case: str, product: bool, mechanism: bool, note: str) -> None:
    env.add(case, f"{case}-product", product, note)
    env.add(case, f"{case}-mechanism", mechanism, note)


async def seed_choice_page(cs: fx.CaseService, offset: int = 4) -> dict[str, Any]:
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


def transcript(cs: fx.CaseService, request_id: str) -> dict[str, Any]:
    return cs.transcript(request_id)


def codes_in(cs: fx.CaseService, request_id: str) -> list[str]:
    steps = transcript(cs, request_id).get("steps") or []
    return [
        str((step.get("outcome") or {}).get("error_code"))
        for step in steps
        if (step.get("outcome") or {}).get("error_code")
    ]


def reply_meta(cs: fx.CaseService, request_id: str) -> dict[str, Any]:
    payload = json.loads(cs.raw(request_id).get("output_json") or "{}")
    return (payload.get("_recovery") or {}).get("reply") or {}


def reply_context(cs: fx.CaseService, request_id: str) -> dict[str, Any]:
    payload = json.loads(cs.raw(request_id).get("output_json") or "{}")
    return (payload.get("_recovery") or {}).get("reply_context") or {}


# ----------------------------------------------------------------------
# 11 组用例
# ----------------------------------------------------------------------
async def case_c01(env: fx.BEnv) -> None:
    """跨题授权：上一题看过答案，不代表下一题可以看。"""

    cs = fx.CaseService(env, "C01")
    await seed_choice_page(cs)
    t1 = await cs.turn(
        "告诉我答案",
        decisions=[plan(act("select_question", index=1), act("view_question_answer"))],
        replies=["答案是 B。"],
    )
    item = first(t1, "view_question_answer")
    answer = ""
    selection = cs.repo.get_question_selection(
        cs.conversation(), "py-2024-2025-s1-final-choice-05"
    )
    if selection is not None:
        card, _evidence = cs.runtime.answer_view(selection)
        if card.answer_detail is not None:
            answer = str(card.answer_detail.text or "")
    t2 = await cs.turn(
        "继续",
        decisions=[plan(act("select_question", index=2), act("view_question_answer"))],
        replies=["已切到第 2 题，需要看答案请告诉我。"],
    )
    text2 = sse_text(t2)
    product = (
        item is not None
        and item.status == "success"
        and bool(answer)
        and first(t2, "view_question_answer") is None
        and "answer_state" not in json.dumps(
            [dict(getattr(item, "details", {}) or {}) for item in composites(t2)],
            ensure_ascii=False,
        )
        and '"answer_detail": {' not in text2
        and bool(turn_text(t2))
    )
    context = reply_context(cs, t2["request_id"])
    mechanism = (
        "ANSWER_NOT_REQUESTED" in codes_in(cs, t2["request_id"])
        and all(entry.get("kind") != "question_answer" for entry in context.get("authorized") or [])
        and "answer_detail" not in json.dumps(context, ensure_ascii=False)
    )
    check(env, "C01", product, mechanism, "跨题不沿用答案授权，拒绝留在 transcript")


async def case_c02(env: fx.BEnv) -> None:
    """跨版本授权：旧 build 的题不能被当前选择顶替或复用。"""

    cs = fx.CaseService(env, "C02")
    seed = await seed_choice_page(cs)

    def break_build(payload: dict[str, Any]) -> None:
        payload["question_results"][0]["build_id"] = "iv-outdated-build"

    cs.mutate_output(seed["request_id"], break_build)
    turn = await cs.turn(
        "给我答案",
        decisions=[plan(act("select_question", index=1), act("view_question_answer"))],
        replies=["这道题的版本需要重新查询。"],
    )
    question = getattr(turn.get("result"), "question", None)
    product = (
        (question is None or question.answer_detail is None)
        and bool(turn_text(turn))
        and "answer_detail" not in json.dumps(
            [event.model_dump(mode="json") for event in turn["events"]],
            ensure_ascii=False,
        )
    )
    codes = codes_in(cs, turn["request_id"])
    messages = [
        str((step.get("outcome") or {}).get("message") or "")
        for step in transcript(cs, turn["request_id"]).get("steps") or []
    ]
    mechanism = any(code in codes for code in ("VERSION_CHANGED", "ANSWER_NOT_SELECTED")) or any(
        "更新" in message or "重新" in message for message in messages
    )
    check(env, "C02", product, mechanism, "旧 build 不授权，需重新查题")


async def case_c03(env: fx.BEnv) -> None:
    """工具调用的伴随文字留在内部，只有终局文本出站。"""

    cs = fx.CaseService(env, "C03")
    turn = await cs.turn(
        "我要复习 Python",
        decisions=[
            plan(act("introduce", course=fx.PY_COURSE), reply="工具阶段内部说明。"),
            plan(reply="已确认复习科目是 Python语言程序设计。"),
        ],
        replies=["已确认复习科目是 Python语言程序设计。"],
    )
    joined = "".join(event.delta for event in deltas(turn))
    text = sse_text(turn)
    product = (
        "工具阶段内部说明" not in text
        and turn_text(turn) == "已确认复习科目是 Python语言程序设计。"
        and joined == turn_text(turn)
    )
    meta = reply_meta(cs, turn["request_id"])
    mechanism = (
        bool(deltas(turn))
        and [event.seq for event in deltas(turn)] == list(range(1, len(deltas(turn)) + 1))
        and all(len(event.delta) <= 400 for event in deltas(turn))
        and meta.get("mode") == "finalize"
    )
    check(env, "C03", product, mechanism, "工具轮文字不提前出站，delta 才拼成正文")


class _BlockingModel:
    """终局流产出第一段后挂起；取消时记录服务端关闭证据。"""

    def __init__(self, rounds: list[dict[str, Any]], pieces: list[str]) -> None:
        self.rounds = list(rounds)
        self.pieces = list(pieces)
        self.calls: list[Any] = []
        self.streamed: list[Any] = []
        self.cancelled = False
        self.started = asyncio.Event()

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_BlockingModel":
        return self

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        from tests.tool_fakes import FakeToolMessage

        self.calls.append(messages)
        response = self.rounds.pop(0) if self.rounds else {"content": ""}
        if "tool_calls" in response:
            return FakeToolMessage(
                content=response.get("content") or "",
                tool_calls=response["tool_calls"],
            )
        return FakeToolMessage(content=response.get("content") or "")

    async def astream(self, messages: Any, **kwargs: Any):
        from tests.tool_fakes import FakeToolMessage

        self.streamed.append(messages)
        self.started.set()
        try:
            for piece in self.pieces:
                yield FakeToolMessage(content=piece)
            await asyncio.sleep(30)
        except (asyncio.CancelledError, GeneratorExit):
            self.cancelled = True
            raise


async def case_c04(env: fx.BEnv) -> None:
    """业务提交后断流：服务端取消，正文只留 pending，恢复只重做正文。"""

    from tests.test_f010b_support import dummy_request, turn_input
    from tests.tool_fakes import ReplyFactory, tool_call

    cs = fx.CaseService(env, "C04")
    model = _BlockingModel(
        [
            {"tool_calls": [tool_call("confirm_course", {"course": fx.PY_COURSE})]},
            {"content": "工具阶段内部说明。"},
        ],
        ["第一段正文。"],
    )
    agent = SchedulerAgent(model=model)
    cs.scheduler = agent
    prepared = cs.service.prepare_turn(
        cs.conversation(), turn_input("我要复习 Python")
    )
    stream = cs.service.stream_prepared(prepared, dummy_request())
    seen: list[str] = []

    async def consume() -> None:
        async for event in stream:
            seen.append(event.type)

    consumer = asyncio.create_task(consume())
    for _ in range(300):
        if "message_delta" in seen:
            break
        await asyncio.sleep(0.01)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass
    for _ in range(100):
        if model.cancelled:
            break
        await asyncio.sleep(0.01)

    payload = json.loads(cs.raw(prepared.request_id).get("output_json") or "{}")
    recovery = payload.get("_recovery") or {}
    calls_before = len(model.calls)
    cs.service._make_reply = ReplyFactory(["恢复后的正文。"])
    replay = await cs.service.regenerate_reply(
        cs.conversation(), prepared.request_id
    )
    product = (
        model.cancelled
        and "result" not in seen
        and [entry["action"] for entry in payload.get("composite_results") or []]
        == ["introduce"]
        and (recovery.get("reply") or {}).get("mode") == "pending"
        and replay["assistant_message"] == "恢复后的正文。"
    )
    mechanism = (
        len(model.calls) == calls_before
        and len(cs.repo.list_turns(cs.conversation())) >= 1
        and (recovery.get("reply_context") or {}).get("version") == 2
    )
    check(env, "C04", product, mechanism, "取消证据 + /reply 只重做正文")


async def case_c05(env: fx.BEnv) -> None:
    """只重做正文：按引用重读并重校验；版本失效时丢弃并如实提示。"""

    cs = fx.CaseService(env, "C05")
    await seed_choice_page(cs)
    t1 = await cs.turn(
        "告诉我答案",
        decisions=[plan(act("select_question", index=1), act("view_question_answer"))],
        replies=["答案是 B。"],
    )
    question = getattr(t1.get("result"), "question", None)
    answer = str(question.answer_detail.text or "") if (
        question is not None and question.answer_detail is not None
    ) else ""
    cs.sql(
        "UPDATE question_selections SET build_id=? WHERE conversation_id=?",
        ("iv-stale", cs.conversation()),
    )
    from tests.tool_fakes import ReplyFactory

    cs.service._make_reply = ReplyFactory(["重做后的正文。"])
    result = await cs.service.regenerate_reply(cs.conversation(), t1["request_id"])
    meta = reply_meta(cs, t1["request_id"])
    product = (
        "答案版本已变化" in result["assistant_message"]
        and bool(answer)
        and answer not in result["assistant_message"]
        and meta.get("reason") == "answer_revalidated"
    )
    recovery = json.loads(cs.raw(t1["request_id"]).get("output_json") or "{}").get(
        "_recovery"
    ) or {}
    context = recovery.get("reply_context") or {}
    mechanism = (
        len(cs.repo.list_turns(cs.conversation())) == 2
        and context.get("version") == 2
        and "reply_input" not in recovery
    )
    check(env, "C05", product, mechanism, "/reply 重读材料并重校验")


async def case_c06(env: fx.BEnv) -> None:
    """模型可见数据：公开题面进结果，答案/内部字段不进投影与历史。"""

    cs = fx.CaseService(env, "C06")
    page = await seed_choice_page(cs)
    cards = list(getattr(page.get("result"), "question_results", None) or [])
    payload = [card.model_dump(mode="json") for card in cards]
    text = json.dumps(payload, ensure_ascii=False)
    history = json.dumps(cs.last_history(), ensure_ascii=False, default=str)
    context = reply_context(cs, page["request_id"])
    product = (
        len(cards) >= 5
        and all(card.statement and card.number_label for card in cards)
        and all(card.answer_detail is None for card in cards)
        and all(card.reference_code is None for card in cards)
    )
    mechanism = (
        all(entry.get("kind") != "question_answer" for entry in context.get("authorized") or [])
        and context.get("teaching") is None
        and '"answer_detail": {' not in history
        and '"reference_code": "' not in history
    )
    check(env, "C06", product, mechanism, "逐字段投影，历史不重复答案")


async def case_c07(env: fx.BEnv) -> None:
    """拒绝信封：结构化原因可供模型改参数或澄清，且不含答案。"""

    cs = fx.CaseService(env, "C07")
    await seed_choice_page(cs)
    turn = await cs.turn(
        "继续",
        decisions=[plan(act("select_question", index=1), act("view_question_answer"))],
        replies=["需要我展开讲讲这道题吗？"],
    )
    text = sse_text(turn)
    steps = transcript(cs, turn["request_id"]).get("steps") or []
    rejected = [
        step.get("outcome") or {}
        for step in steps
        if (step.get("outcome") or {}).get("error_code")
        and (step.get("outcome") or {}).get("executed")
        and not (step.get("outcome") or {}).get("details")
    ]
    product = (
        "ANSWER_NOT_REQUESTED" in codes_in(cs, turn["request_id"])
        and first(turn, "view_question_answer") is None
        and '"answer_detail": {' not in text
        and "answer_state" not in json.dumps(
            [dict(getattr(item, "details", {}) or {}) for item in composites(turn)],
            ensure_ascii=False,
        )
        and bool(turn_text(turn))
    )
    mechanism = bool(rejected) and all(
        item.get("status") == "failed"
        and item.get("error_code") == "ANSWER_NOT_REQUESTED"
        and item.get("needs_user") is False
        for item in rejected
    )
    check(env, "C07", product, mechanism, "拒绝是工具结果，不是学生可见失败")


async def case_c08(env: fx.BEnv) -> None:
    """终态判定：收尾决策的文字就是最终回复；澄清轮不再调回复模型。"""

    cs = fx.CaseService(env, "C08")
    turn = await cs.turn(
        "我要复习 Python",
        decisions=[plan(act("introduce", course=fx.PY_COURSE))],
        replies=["已确认复习科目是 Python语言程序设计。"],
    )
    context = reply_context(cs, turn["request_id"])
    meta = reply_meta(cs, turn["request_id"])
    product = (
        bool(turn_text(turn))
        and "已确认复习科目" in turn_text(turn)
        and context.get("reason") == "model_finished"
    )
    mechanism = (
        meta.get("mode") == "finalize"
        and meta.get("reason") == context.get("reason") == "model_finished"
    )
    check(env, "C08", product, mechanism, "终态由循环决定，文字即最终回复")


async def case_c09(env: fx.BEnv) -> None:
    """delta 契约：seq 连续、单段有上限、拼起来等于落库正文。"""

    cs = fx.CaseService(env, "C09")
    turn = await cs.turn(
        "我要复习 Python",
        decisions=[plan(act("introduce", course=fx.PY_COURSE))],
        replies=["已确认复习科目是 Python语言程序设计。"],
    )
    chunks = deltas(turn)
    text = turn_text(turn)
    meta = reply_meta(cs, turn["request_id"])
    product = bool(chunks) and "".join(event.delta for event in chunks) == text and bool(text)
    mechanism = (
        [event.seq for event in chunks] == list(range(1, len(chunks) + 1))
        and all(len(event.delta) <= 400 for event in chunks)
        and isinstance(meta.get("finalize_first_delta_ms"), (int, type(None)))
    )
    check(env, "C09", product, mechanism, "SSE 正文与最终落库一致")


async def case_c10(env: fx.BEnv) -> None:
    """重复请求：同一 request_id 重复提交不重复执行工具。"""

    from dataclasses import replace

    from tests.test_f010b_support import dummy_request, turn_input

    cs = fx.CaseService(env, "C10")
    text = "我要复习 Python"
    request_id = "c10-duplicate-request"
    second = None
    for index in range(2):
        prepared = cs.service.prepare_turn(
            cs.conversation(),
            replace(turn_input(text), request_id=request_id),
        )
        if index == 1:
            second = prepared
        events = [
            event async for event in cs.service.stream_prepared(prepared, dummy_request())
        ]
    turns = cs.repo.list_turns(cs.conversation())
    detail = cs.service.get_detail(cs.conversation())
    product = (
        len(turns) == 1
        and len(cs.scheduler.calls) == 1
        and len(detail.turns) == 1
    )
    mechanism = type(second).__name__ == "ReplayTurn" if second is not None else False
    check(env, "C10", product, mechanism, "同 request_id 幂等，不重复执行")


async def case_c11(env: fx.BEnv) -> None:
    """旧 reply_input 兼容：B 库里的旧轮次仍能只重做正文。"""

    from dataclasses import replace

    from app.conversations.reply import build_material, material_to_payload
    from uuid import uuid4

    from tests.test_f010b_support import turn_input

    cs = fx.CaseService(env, "C11")
    # 模拟 B 库旧轮次：业务结果在，恢复记录只有 reply_input。
    material = build_material(
        user_text="我要复习 Python",
        course=fx.PY_COURSE,
        composite_results=[],
        waiting_for=None,
    )
    material = replace(material, learning_memory=None)
    request_id = f"c11-legacy-{uuid4().hex[:8]}"
    cs.repo.start_turn(cs.conversation(), request_id, "我要复习 Python")
    cs.repo.complete_dynamic_turn(
        cs.conversation(), request_id, kind="dynamic", assistant_message="旧正文"
    )
    cs.repo.update_turn_reply(
        cs.conversation(),
        request_id,
        assistant_message="旧正文",
        reply_meta={"mode": "model", "deltas": 1},
        reply_input=material_to_payload(material),
    )
    from tests.tool_fakes import ReplyFactory

    cs.service._make_reply = ReplyFactory(["兼容旧材料的正文。"])
    result = await cs.service.regenerate_reply(cs.conversation(), request_id)
    meta = reply_meta(cs, request_id)
    product = (
        result["assistant_message"] == "兼容旧材料的正文。"
        and result.get("reply_status") == "complete"
    )
    recovery = json.loads(cs.raw(request_id).get("output_json") or "{}").get(
        "_recovery"
    ) or {}
    mechanism = (
        meta.get("mode") in ("model", "finalize")
        and "reply_context" not in recovery
        and "reply_input" in recovery
    )
    check(env, "C11", product, mechanism, "旧 reply_input 仍可只重做正文")


CASES = {
    "C01": case_c01,
    "C02": case_c02,
    "C03": case_c03,
    "C04": case_c04,
    "C05": case_c05,
    "C06": case_c06,
    "C07": case_c07,
    "C08": case_c08,
    "C09": case_c09,
    "C10": case_c10,
    "C11": case_c11,
}

TITLES = {
    "C01": "跨题授权：上一题看过答案，不代表下一题可以看",
    "C02": "跨版本授权：旧 build 不被当前选择顶替",
    "C03": "工具调用轮的文字留在内部，只有终局文本出站",
    "C04": "业务提交后断流：服务端取消证据 + 只重做正文",
    "C05": "/reply 使用已提交材料并重新校验，失效则如实提示",
    "C06": "模型可见数据逐字段投影，历史不重复答案",
    "C07": "答案拒绝以工具结果返回，含结构化原因与事实",
    "C08": "终态判定：收尾文字即最终回复，澄清不调回复模型",
    "C09": "SSE delta 契约：seq 连续、单段上限、拼合等于落库正文",
    "C10": "同 request_id 重复提交幂等，不重复执行业务",
    "C11": "B 批旧 reply_input 记录仍支持只重做正文",
}


def write_summary(env: fx.BEnv, started: float) -> dict[str, Any]:
    units = env.results
    score = sum(1 for unit in units if unit["ok"])
    summary = {
        "run_id": env.run_dir.name,
        "kind": RUN_KIND,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
        "seconds": round(time.perf_counter() - started, 2),
        "score": score,
        "max_score": len(units),
        "all_passed": score == len(units) and bool(units),
        "egress_attempts": env.guard.egress_attempts,
        "offline_self_test_blocked": env.guard.self_test_blocked,
        "units": units,
        "cases": [
            {"id": case_id, "title": TITLES[case_id]}
            for case_id in CASES
        ],
    }
    (env.run_dir / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"# F010 C 专项评测（{summary['run_id']}）",
        "",
        f"- 得分：{score}/{len(units)}",
        f"- 离线守卫：拦截 {summary['egress_attempts']} 次外联，自检 "
        f"{'通过' if summary['offline_self_test_blocked'] else '未通过'}",
        "",
        "| 用例 | 产品判据 | 机制判据 |",
        "| --- | --- | --- |",
    ]
    for case_id in CASES:
        pair = {unit["unit"]: unit for unit in units if unit["case"] == case_id}
        product = pair.get(f"{case_id}-product")
        mechanism = pair.get(f"{case_id}-mechanism")
        lines.append(
            f"| {case_id} | {'✅' if product and product['ok'] else '❌'} | "
            f"{'✅' if mechanism and mechanism['ok'] else '❌'} |"
        )
    (env.run_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="F010 C 专项离线评测")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--only", default="", help="只跑指定用例（逗号分隔）")
    args = parser.parse_args(argv)
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
    print(
        json.dumps(
            {key: summary[key] for key in ("run_id", "score", "max_score", "all_passed")},
            ensure_ascii=False,
        )
    )
    for unit in summary["units"]:
        print(("PASS " if unit["ok"] else "FAIL ") + unit["unit"] + "  " + unit["detail"][:120])
    return 0


if __name__ == "__main__":
    sys.exit(main())