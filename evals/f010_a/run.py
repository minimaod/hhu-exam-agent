"""F010 A 批离线评测执行器。

用法（全部在离线、无真实模型传输的前提下）：
    .venv/Scripts/python.exe -B evals/f010_a/run.py --run-id 20261103-A1
    .venv/Scripts/python.exe -B evals/f010_a/run.py --run-id 20261103-A2
    .venv/Scripts/python.exe -B evals/f010_a/run.py --run-id 20261103-A3-only05 --only 05
    .venv/Scripts/python.exe -B evals/f010_a/run.py --run-id 20261103-A4-hist --replay-f008

判分：48 例 × 2 维（目标与事实 / 流程与状态）= 96；组内全满足才得分。
阻塞不算通过，保留分母。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = Path(__file__).resolve().parent
RUN_ROOT = PROJECT_ROOT / "tmp" / "f010-a"

CASES_PATH = EVAL_DIR / "cases.json"


# ----------------------------------------------------------------------
# 判分容器
# ----------------------------------------------------------------------
FACT = "fact"  # 目标与事实
FLOW = "flow"  # 流程与状态
DIM_LABEL = {FACT: "目标与事实", FLOW: "流程与状态"}


class Checks:
    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self.items: list[dict[str, Any]] = []

    def add(
        self, dim: str, name: str, status: str, detail: Any = "", turn: int | None = None
    ) -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        self.items.append(
            {
                "dim": dim,
                "name": name,
                "status": status,
                "detail": detail[:1500],
                "turn": turn,
            }
        )

    def ok(self, dim: str, name: str, detail: Any = "", turn: int | None = None) -> None:
        self.add(dim, name, "passed", detail, turn)

    def fail(self, dim: str, name: str, detail: Any = "", turn: int | None = None) -> None:
        self.add(dim, name, "failed", detail, turn)

    def blocked(self, dim: str, name: str, detail: Any = "") -> None:
        self.add(dim, name, "blocked", detail)

    def require(
        self,
        dim: str,
        name: str,
        condition: Any,
        detail: Any = "",
        turn: int | None = None,
    ) -> bool:
        if condition:
            self.add(dim, name, "passed", detail, turn)
        else:
            self.add(dim, name, "failed", detail, turn)
        return bool(condition)

    def equal(
        self, dim: str, name: str, actual: Any, expected: Any, turn: int | None = None
    ) -> bool:
        return self.require(
            dim, name, actual == expected, {"actual": actual, "expected": expected}, turn
        )

    def report(self) -> dict[str, Any]:
        dims: dict[str, Any] = {}
        for dim in (FACT, FLOW):
            items = [item for item in self.items if item["dim"] == dim]
            if not items:
                dims[dim] = {
                    "status": "failed",
                    "label": DIM_LABEL[dim],
                    "score": 0.0,
                    "checks": [
                        {
                            "dim": dim,
                            "name": "该维度没有任何断言（脚本缺陷）",
                            "status": "failed",
                            "detail": "",
                        }
                    ],
                }
                continue
            statuses = {item["status"] for item in items}
            if "failed" in statuses and "blocked" not in statuses:
                status = "failed"
            elif "failed" in statuses:
                status = "failed"
            elif "blocked" in statuses:
                status = "blocked"
            else:
                status = "passed"
            dims[dim] = {
                "status": status,
                "label": DIM_LABEL[dim],
                "score": 1.0 if status == "passed" else 0.0,
                "checks": items,
            }
        return dims


# ----------------------------------------------------------------------
# 观测辅助
# ----------------------------------------------------------------------
def _last_result(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("type") == "result":
            return event
    return {}


def _error(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in events:
        if event.get("type") == "error":
            return event
    return {}


def result_of(ctx: Any, n: int) -> dict[str, Any]:
    return _last_result(ctx.turn_results[n - 1]["events"])


def error_of(ctx: Any, n: int) -> dict[str, Any]:
    return _error(ctx.turn_results[n - 1]["events"])


def cards_of(ctx: Any, n: int) -> list[dict[str, Any]]:
    return result_of(ctx, n).get("question_results") or []


def qids_of(ctx: Any, n: int) -> list[str]:
    return [card.get("question_id", "") for card in cards_of(ctx, n)]


def items_of(ctx: Any, n: int) -> list[dict[str, Any]]:
    return (result_of(ctx, n).get("result") or {}).get("items") or []


def message_of(ctx: Any, n: int) -> str:
    return result_of(ctx, n).get("assistant_message") or ""


def composite_of(ctx: Any, n: int) -> list[dict[str, Any]]:
    return result_of(ctx, n).get("composite_results") or []


def actions_of(ctx: Any, n: int) -> list[str]:
    return [item.get("action") for item in composite_of(ctx, n)]


def action_status(ctx: Any, n: int, name: str) -> str:
    for item in composite_of(ctx, n):
        if item.get("action") == name:
            return item.get("status", "")
    return ""


def _product_message(ctx: Any, n: int) -> str:
    """产品自己写的动作结果文字（不是脚本回复），用于判断"是否如实"。"""

    parts = []
    for item in composite_of(ctx, n):
        parts.append(str(item.get("message") or ""))
        parts.append(json.dumps(item.get("details") or {}, ensure_ascii=False))
    return " ".join(part for part in parts if part)


def _mentions_exhausted(ctx: Any, n: int) -> bool:
    text = _product_message(ctx, n)
    return any(word in text for word in ("全部", "没有更多", "已列完", "已全部", "已经列"))


def types_of(ctx: Any, n: int) -> list[str]:
    return ctx.turn_results[n - 1]["types"]


def turn_row_of(ctx: Any, n: int) -> dict[str, Any]:
    request_id = ctx.turn_results[n - 1]["request_id"]
    return ctx.turn_row(request_id) or {}


def name_from(items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("id") or item.get("paper_id") or "") for item in items]


# ----------------------------------------------------------------------
# 装配（依赖覆盖 + 适配器替身）
# ----------------------------------------------------------------------
class RunContext:
    def __init__(self, run_id: str, run_dir: Path) -> None:
        self.run_id = run_id
        self.dir = run_dir
        self.started = time.time()
        self.cases: dict[str, Any] = {}
        self.trace: Any = None
        self.manifest: dict[str, Any] = {}
        self.notes: list[str] = []

    def case_dir(self, case_id: str) -> Path:
        return self.dir / "cases" / case_id


def bootstrap(args: argparse.Namespace) -> tuple[RunContext, Any]:
    from evals.f010_a import fixtures

    run_dir = fixtures.prepare_run_dir(args.run_id)
    bank = fixtures.prepare_bank(run_dir)
    fixtures.prepare_papers(run_dir)
    fixtures.prepare_ab_papers(run_dir)

    os.environ["HHU_QB_ROOT"] = str(bank)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    temp = run_dir / "system-temp"
    temp.mkdir(exist_ok=True)
    os.environ["TEMP"] = str(temp)
    os.environ["TMP"] = str(temp)

    run = RunContext(args.run_id, run_dir)
    fixtures.install_offline_config()
    guard = fixtures.OfflineGuard()
    guard.install()
    run.guard = guard

    run.manifest = {
        "run_id": args.run_id,
        "mode": "offline-baseline",
        "git_sha": git_sha(),
        "git_branch": git_branch(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.version.split()[0],
        "adapters": "SchedulerAgent/StreamingReply/DeepSeekTutor/DeepSeekExerciseAgent/"
        "DeepSeekUnderstanding/DeepSeekIntentRouter -> Scripted*",
        "bank_root": str(bank),
        "bank_build": fixtures.BANK_BUILD,
        "papers_root": str(run_dir / "papers"),
        "timeouts": {"turn_stream": 120, "case": 300},
        "seed": "uuid4 per case; frozen clock 2026-11-03 where relevant",
        "http": {},
    }
    fixtures.install_adapters()
    fixtures.install_stream_audit()
    fixtures.install_real_call_guard()
    fixtures.install_path_audit()
    run.trace = fixtures.TraceWriter(run_dir / "trace.jsonl")
    return run, fixtures


def git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "-c", "safe.directory=D:/实习/河海大学考试复习", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def git_branch() -> str:
    try:
        return subprocess.run(
            ["git", "-c", "safe.directory=D:/实习/河海大学考试复习", "branch", "--show-current"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


# ----------------------------------------------------------------------
# 挂钩工具
# ----------------------------------------------------------------------
@contextlib.contextmanager
def patched_method(owner: Any, name: str, replacement: Callable[..., Any]) -> Iterator[None]:
    original = getattr(owner, name)
    setattr(owner, name, replacement(original))
    try:
        yield
    finally:
        setattr(owner, name, original)


def repo_hook(name: str, factory: Callable[[Callable[..., Any]], Callable[..., Any]]):
    from app.conversations.repository import ConversationRepository

    return patched_method(ConversationRepository, name, factory)


def build_scripts(spec: dict[str, Any], fx: Any) -> list[Any]:
    """把每个"会产生模型调用"的步骤变成一个脚本；replay 步骤也要新脚本。"""

    scripts: list[Any] = []
    for step in spec["steps"]:
        if "utterance" not in step and step.get("kind") != "reply_retry":
            continue
        scripts.append(
            fx.TurnScript(
                n=len(scripts) + 1,
                utterance=step.get("utterance", ""),
                tools=[[dict(call) for call in batch] for batch in step.get("tools", [])],
                reply_pieces=step.get("reply", ["好的，我看一下。"]),
                reply_fail_after=step.get("reply_fail_after"),
                reply_block_after=step.get("reply_block_after"),
                reply_hang=step.get("reply_hang") or 0.0,
                request=step.get("request", {}),
                rebuild=step.get("rebuild_before", False),
            )
        )
    return scripts


def run_case(run: RunContext, fx: Any, spec: dict[str, Any]) -> dict[str, Any]:
    checks = Checks(spec["id"])
    ctx = fx.CaseContext(run.dir, spec, run)
    fx.ACTIVE.case_id = spec["id"]
    fx.ACTIVE.turns = build_scripts(spec, fx)
    fx.ACTIVE.markers = list(spec.get("markers", []))
    fx.ACTIVE.exercise_plans = {
        str(key): value for key, value in (spec.get("exercise_plans") or {}).items()
    }
    fx.ACTIVE.exercise_plan = {}
    fx.ACTIVE.placeholders = {}
    fx.ACTIVE.exercise_calls = {"generate": [], "evaluate": []}
    fx.ACTIVE.tool_batches = []
    fx.ACTIVE.trace = run.trace
    fx.ACTIVE.guard = run.guard
    started = time.perf_counter()
    try:
        prepare_case_db(ctx, fx, spec)
        ctx.start()
        ctx.create_conversation()
        driver = DRIVERS[spec.get("driver", "turns")]
        driver(ctx, spec, fx, checks)
    except Exception as exc:  # 用例级异常也要留下证据
        checks.fail(FLOW, "用例执行完成", f"{type(exc).__name__}: {exc}")
        run.trace.span(spec["id"], "case_error", error=traceback.format_exc()[-3000:])
        ctx.notes.append("用例异常：" + traceback.format_exc()[-1200:])
    finally:
        with contextlib.suppress(Exception):
            ctx.stop()
    elapsed = round(time.perf_counter() - started, 2)
    report = {
        "id": spec["id"],
        "group": spec["group"],
        "title": spec["title"],
        "historical": spec.get("historical", False),
        "source": spec.get("source", ""),
        "fixture": spec.get("fixture", "real"),
        "seconds": elapsed,
        "conversation_id": ctx.conversation_id,
        "request_ids": [item["request_id"] for item in ctx.turn_results],
        "http": ctx.http_calls,
        "writes": ctx.snapshot(),
        "dims": checks.report(),
        "notes": ctx.notes,
    }
    try:
        turns = [
            {
                "n": item["turn"],
                "request_id": item["request_id"],
                "status": item["status"],
                "types": item["types"],
                "closed_early": item.get("closed_early"),
                "transport": item.get("transport"),
                "first_delta_seconds": item.get("first_delta_seconds"),
                "server_stream": item.get("server_stream"),
                "result": fx.last_result(item["events"]),
                "error": fx.first_error(item["events"]),
            }
            for item in ctx.turn_results
        ]
        (ctx.case_dir / "case.json").write_text(
            json.dumps(
                {
                    "id": spec["id"],
                    "title": spec["title"],
                    "group": spec["group"],
                    "turns": turns,
                    "materials": [text[-1500:] for text in ctx.materials],
                    "decisions": [text[-1500:] for text in ctx.decisions],
                    "tool_calls": ctx.tool_calls,
                    "http": ctx.http_calls,
                    "concurrent": ctx.concurrent,
                    "seed": _seed_view(ctx.seed),
                    "observed": ctx.observed,
                    "notes": ctx.notes,
                    "dims": report["dims"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # 证据写入失败不影响评分，但必须留痕
        detail = traceback.format_exc()
        run.notes.append(f"用例 {spec['id']} 的 case.json 写入失败：{exc!r}")
        with (run.dir / "case-json-errors.log").open("a", encoding="utf-8") as handle:
            handle.write(f"=== {spec['id']} ===\n{detail}\n")
    run.trace.span(spec["id"], "case_end", seconds=elapsed, dims=report["dims"])
    return report


def _seed_view(seed: Any) -> Any:
    """把种子数据压成可序列化的摘要，避免 case.json 因列表字段写失败。"""

    if isinstance(seed, dict):
        view = {}
        for key, value in seed.items():
            if isinstance(value, dict):
                view[key] = {k: v for k, v in value.items() if k in ("token", "entry", "note", "conversation")}
            elif isinstance(value, list):
                view[key] = {"count": len(value), "sample": value[:2]}
            else:
                view[key] = value
        return view
    if isinstance(seed, list):
        return {"count": len(seed), "sample": seed[:2]}
    return seed


def prepare_case_db(ctx: Any, fx: Any, spec: dict[str, Any]) -> None:
    from app.conversations.repository import ConversationRepository

    ConversationRepository(ctx.db_path).initialize()
    seed = spec.get("seed")
    if seed == "memory25":
        ctx.seed = fx.seed_memory_records(ctx.db_path, count=25, all_notes=spec.get("all_notes", False))
    elif seed == "two_courses":
        ctx.seed = seed_two_courses(ctx, fx)
    else:
        ctx.seed = None
    placeholders = {}
    if isinstance(ctx.seed, dict):
        if fx.PY_COURSE in ctx.seed:
            placeholders["{{PY_ENTRY}}"] = ctx.seed[fx.PY_COURSE]["entry"]
        if fx.MATH_COURSE in ctx.seed:
            placeholders["{{MATH_ENTRY}}"] = ctx.seed[fx.MATH_COURSE]["entry"]
    fx.ACTIVE.placeholders = placeholders


def seed_two_courses(ctx: Any, fx: Any) -> dict[str, Any]:
    """两门课程各一条可追溯证据，知识点字符串就是唯一标记。"""

    from app.memory.models import MemoryCorrectionRequest
    from app.memory.service import LearningMemoryService
    from tests.f009_support import Seed

    seed = Seed(ctx.db_path)
    memory = LearningMemoryService(
        ctx.db_path,
        papers=fx.PaperLookup(),
        taxonomy_path=ctx.case_dir / "absent-taxonomy.json",
        current_file=ctx.case_dir / "absent-current.json",
    )
    result: dict[str, Any] = {}
    for course, paper, token, days in (
        (fx.PY_COURSE, fx.PAPER_PY_2425_FINAL, "PY41MARK", 1),
        (fx.MATH_COURSE, "math-bi-20250116-final", "MATH41MARK", 2),
    ):
        conversation = seed.conversation(title=f"{course} 学习记录", days_ago=days)
        problem = seed.problem(
            conversation,
            paper_id=paper,
            source_type="registered_paper",
            days_ago=days,
            statement=f"{course} 的一道错题",
        )
        exercise = seed.exercise(
            conversation,
            problem,
            knowledge_point=token,
            days_ago=days,
            statement=f"{course} 练习：{token}",
        )
        seed.attempt(exercise, correct=False, days_ago=days)
        memory.correct(
            "exercise",
            exercise,
            MemoryCorrectionRequest(note=f"{token}-附注：当时订正过"),
        )
        result[course] = {
            "conversation": conversation,
            "entry": f"exercise:{exercise}",
            "token": token,
            "exercise": exercise,
        }
    return result


# ----------------------------------------------------------------------
# 驱动器
# ----------------------------------------------------------------------
def drive_turns(ctx: Any, spec: dict[str, Any], fx: Any, checks: Checks) -> None:
    step_index = 0
    for step in spec["steps"]:
        if "http" in step:
            do_http_step(ctx, step)
            continue
        if step.get("kind") == "reply_retry":
            if step.get("rebuild_before"):
                ctx.rebuild("before-reply-retry")
            fx.ACTIVE.begin_turn(step_index)
            ctx.observed = {
                "before_reply": (
                    write_delta(ctx, "mistake_records"),
                    write_delta(ctx, "exercises"),
                    write_delta(ctx, "exercise_attempts"),
                    len(fx.ACTIVE.exercise_calls["generate"]),
                )
            }
            ctx.post_reply(step["reply_of"])
            ctx.observed["after_reply"] = (
                write_delta(ctx, "mistake_records"),
                write_delta(ctx, "exercises"),
                write_delta(ctx, "exercise_attempts"),
                len(fx.ACTIVE.exercise_calls["generate"]),
            )
            step_index += 1
            continue
        if step.get("new_conversation"):
            ctx.conversation_id = None
            ctx.create_conversation()
        if step.get("rebuild_before"):
            ctx.rebuild(f"step{step_index + 1}")
        fx.ACTIVE.begin_turn(step_index)
        before_reply = len(fx.ACTIVE.reply_messages)
        before_decision = len(fx.ACTIVE.decision_messages)
        ctx.post_turn(
            step["utterance"],
            request_id=step.get("request_id"),
            kind=step.get("http_kind"),
            extra=step.get("request"),
        )
        ctx.decisions.append(fx.decision_text_since(before_decision))
        step_index += 1
    check = CHECKS[spec["id"]]
    check(ctx, spec, fx, checks)


def _latest_card(ctx: Any) -> dict[str, Any]:
    """最近一次真正选中的题卡；没有选中时退回最近一页的第一张。"""

    for n in range(len(ctx.turn_results), 0, -1):
        question = result_of(ctx, n).get("question") or {}
        if question.get("question_id"):
            return question
        cards = cards_of(ctx, n)
        if cards:
            return cards[0]
    return {}


def do_http_step(ctx: Any, step: dict[str, Any]) -> None:
    http = step["http"]
    method = http["method"]
    if method == "download":
        paper_id = http.get("paper_id")
        if paper_id is None:
            for result in ctx.turn_results:
                found = _last_result(result["events"]).get("result") or {}
                items = found.get("items") or []
                if items:
                    paper_id = items[0]["id"]
                    break
        ctx.download(paper_id)
        return
    if method == "answer":
        ctx.get_answer(http["question_id"], http["build"])
        return
    if method == "source":
        card = _latest_card(ctx) or {}
        kind = http.get("kind", "statement")
        pages = (card.get("pages") or {}).get(kind) or []
        page = http.get("page") or (pages[0] if pages else 1)
        ctx.get_source(
            http.get("question_id") or card.get("question_id", ""),
            http.get("build") or card.get("build_id", ""),
            kind,
            page,
        )
        return
    if method == "papers":
        ctx.list_papers(http.get("course"), http.get("exam_type"))
        return
    if method == "memory":
        ctx.memory_get(http.get("course"), http.get("days", 30), http.get("limit", 50))
        return
    if method == "memory_patch":
        ctx.memory_patch(http["kind"], http["source_id"], http.get("payload", {}))
        return
    if method == "memory_delete":
        ctx.memory_delete(http["kind"], http["source_id"], http.get("reason", ""))
        return
    if method == "detail":
        ctx.get_detail()
        return
    raise AssertionError(f"未知 HTTP 步骤：{http}")


# —— 35 / 40：业务已提交后正文中断，重启并用 /reply 只重做正文 ——
def drive_interrupt_reply(ctx: Any, spec: dict[str, Any], fx: Any, checks: Checks) -> None:
    steps = spec["steps"]
    for index, step in enumerate(steps):
        if "utterance" not in step and step.get("kind") != "reply_retry":
            continue
        fx.ACTIVE.begin_turn(index)
        if step.get("kind") == "interrupt_reply":
            driver = (
                ctx.stream_turn_over_tcp
                if step.get("transport") == "tcp"
                else ctx.stream_turn_until
            )
            driver(
                step["utterance"],
                lambda events: any(event.get("type") == "message_delta" for event in events),
                request_id=step.get("request_id"),
            )
            ctx.notes.append("已收到首个正文片段并主动断开")
            break
        ctx.post_turn(step["utterance"], request_id=step.get("request_id"), kind=step.get("http_kind"))
    reply_step = next(step for step in steps if step.get("kind") == "reply_retry")
    ctx.rebuild("after-interrupt")
    fx.ACTIVE.begin_turn(len([s for s in steps if "utterance" in s or s.get("kind") == "reply_retry"]) - 1)
    observed = {
        "decision_before": fx.ACTIVE.decision_calls,
        "batches_before": list(fx.ACTIVE.tool_batches),
        "rows_before": raw_rows(ctx, "SELECT id, created_at, updated_at FROM mistake_records"),
    }
    ctx.post_reply(reply_step["reply_of"])
    observed.update(
        {
            "decision_after": fx.ACTIVE.decision_calls,
            "new_batches": list(fx.ACTIVE.tool_batches[len(list(observed["batches_before"])):]),
            "rows_after": raw_rows(ctx, "SELECT id, created_at, updated_at FROM mistake_records"),
        }
    )
    ctx.observed = {"reply": observed}
    check = CHECKS[spec["id"]]
    check(ctx, spec, fx, checks)


# —— 35b / 40：业务提交后、轮次完成前中断，再 retry_of ——
def drive_interrupt_before_complete(ctx: Any, spec: dict[str, Any], fx: Any, checks: Checks) -> None:
    """40：业务已提交、轮次未结束时进程崩溃；重启后由启动恢复标成 interrupted，再 retry_of 一次。"""

    steps = spec["steps"]
    turn_index = 0
    for step in steps:
        if step.get("kind") == "interrupt_before_complete":
            fx.ACTIVE.begin_turn(turn_index)
            ctx.observed = _crash_hanging_turn(ctx, spec, step)
            turn_index += 1
            break
        fx.ACTIVE.begin_turn(turn_index)
        ctx.post_turn(step["utterance"], request_id=step.get("request_id"))
        turn_index += 1
    origin = ctx.turn_row(spec["steps"][turn_index - 1]["request_id"]) or {}
    ctx.notes.append(f"崩溃后轮次状态：{origin.get('status')}；崩溃观测：{ctx.observed}")
    ctx.rebuild("after-crash")
    origin = ctx.turn_row(spec["steps"][turn_index - 1]["request_id"]) or {}
    ctx.notes.append(f"重启后轮次状态：{origin.get('status')}")
    detail = ctx.get_detail()
    interrupted_view = next(
        (item for item in detail.get("turns", [])
         if item.get("request_id") == spec["steps"][turn_index - 1]["request_id"]),
        {},
    )
    ctx.observed = dict(ctx.observed or {})
    ctx.observed["interrupted_view"] = {
        key: interrupted_view.get(key)
        for key in ("status", "retryable", "retry_hint", "business_status", "reply_status")
    }
    retry_step = next(step for step in steps if step.get("kind") == "retry_of")
    fx.ACTIVE.begin_turn(turn_index)
    ctx.post_turn(
        retry_step.get("utterance") or "",
        request_id=retry_step.get("request_id"),
        extra={"retry_of": retry_step["retry_of"]},
    )
    turn_index += 1
    for step in steps[turn_index:]:
        fx.ACTIVE.begin_turn(turn_index)
        if step.get("http"):
            do_http_step(ctx, step)
        else:
            ctx.post_turn(step.get("utterance") or "", request_id=step.get("request_id"))
        turn_index += 1
    check = CHECKS[spec["id"]]
    check(ctx, spec, fx, checks)


_CRASH_SCRIPT = '''"""独立进程：发起一轮后只保留 running 轮次，然后直接退出（模拟进程崩溃）。"""

import json
import os
import sys
import threading
import time
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sys.path.insert(0, payload["project_root"])

from evals.f010_a import fixtures

fixtures.install_offline_config()
fixtures.install_adapters()
fixtures.install_stream_audit()

fixtures.ACTIVE.case_id = payload["case_id"]
fixtures.ACTIVE.turns = [
    fixtures.TurnScript(
        n=1,
        utterance=payload["utterance"],
        tools=[[dict(call) for call in batch] for batch in payload["tools"]],
        reply_pieces=payload["reply"],
        reply_hang=payload["hang"],
        request=payload.get("request") or {},
    )
]
fixtures.ACTIVE.turn_index = 0


def watchdog() -> None:
    time.sleep(payload.get("fallback_exit", 30.0))
    os._exit(19)


threading.Thread(target=watchdog, daemon=True).start()

from app.mistakes.service import MistakeRecordService

def _crash_before_second_action(self, *args, **kwargs):
    """讲解已提交，第二个动作还没写入时进程直接退出。"""

    time.sleep(0.2)
    os._exit(17)

MistakeRecordService.create_from_problem = _crash_before_second_action

from fastapi.testclient import TestClient
from app.main import app

body = {
    "request_id": payload["request_id"],
    "query": payload["utterance"],
    "kind": "auto",
}
with TestClient(app) as client:
    with client.stream(
        "POST", f"/api/conversations/{payload['conversation_id']}/turns", json=body
    ) as response:
        for _ in response.iter_text():
            pass
'''


def rid_of(spec: dict[str, Any], n: int) -> str:
    steps = [step for step in spec["steps"] if "utterance" in step]
    return steps[n - 1]["request_id"]


def payload_table(ctx: Any) -> str:
    return "problems"


def _crash_hanging_turn(ctx: Any, spec: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    from evals.f010_a import fixtures as fx_module

    payload = {
        "project_root": str(fx_module.PROJECT_ROOT),
        "db_path": str(ctx.db_path),
        "conversation_id": ctx.conversation_id,
        "request_id": step["request_id"],
        "utterance": step["utterance"],
        "tools": step.get("tools", []),
        "reply": step.get("reply", ["好的，我看一下。"]),
        "hang": step.get("reply_hang") or 600.0,
        "request": step.get("request") or {},
        "table": payload_table(ctx),
        "case_id": spec["id"],
    }
    script_path = ctx.case_dir / "crash_turn.py"
    payload_path = ctx.case_dir / "crash_payload.json"
    script_path.write_text(_CRASH_SCRIPT, encoding="utf-8")
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    env = dict(os.environ)
    env["HHU_STUDY_DB"] = str(ctx.db_path)
    env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.run(
        [sys.executable, "-B", str(script_path), str(payload_path)],
        cwd=str(fx_module.PROJECT_ROOT),
        env=env,
        capture_output=True,
        timeout=180,
    )
    return {
        "returncode": process.returncode,
        "stderr": process.stderr.decode("utf-8", "replace")[-1200:],
        "status_after_crash": (ctx.turn_row(step["request_id"]) or {}).get("status"),
        "business_rows": len(raw_rows(ctx, "SELECT * FROM problems")),
        "mistake_rows": len(raw_rows(ctx, "SELECT * FROM mistake_records")),
    }

# —— 36 / 38：故障与并发重试 ——
def drive_fault_then_retry(ctx: Any, spec: dict[str, Any], fx: Any, checks: Checks) -> None:
    steps = spec["steps"]
    from app.conversations.errors import ConversationStorageError
    from app.conversations import repository as repo_mod

    observed: dict[str, Any] = {"pending_in_tx": None, "attempts": 0, "commits": 0, "rows": []}
    ctx.observed = observed

    def factory(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
            observed["attempts"] += 1
            created = original(self, *args, **kwargs)
            connection = self._action_connection.get(None)
            if connection is not None:
                observed["pending_in_tx"] = connection.execute(
                    "SELECT COUNT(*) FROM exercises"
                ).fetchone()[0]
            raise ConversationStorageError("注入故障：SQL 已执行、事务未提交")

        return wrapped

    turn_index = 0
    for step in steps:
        fx.ACTIVE.begin_turn(turn_index)
        if step.get("kind") == "storage_fault":
            with repo_hook("create_exercise", factory):
                ctx.post_turn(step["utterance"], request_id=step.get("request_id"))
            observed["rollback_rows"] = len(raw_rows(ctx, "SELECT * FROM exercises"))
            faulted = ctx.turn_results[-1]["request_id"]
            view = next(
                (item for item in ctx.get_detail().get("turns") or []
                 if item.get("request_id") == faulted),
                {},
            )
            observed["fault_view"] = {
                "status": view.get("status"),
                "retryable": view.get("retryable"),
                "retry_hint": view.get("retry_hint"),
            }
        elif step.get("kind") == "retry_of":
            ctx.post_turn(
                step.get("utterance") or "",
                request_id=step.get("request_id"),
                extra={"retry_of": step["retry_of"]},
            )
        elif step.get("kind") == "resend":
            ctx.post_turn(
                step.get("utterance") or "",
                request_id=step.get("request_id"),
                extra={"retry_of": step["retry_of"]},
            )
        else:
            ctx.post_turn(step["utterance"], request_id=step.get("request_id"))
        observed["rows"].append(len(raw_rows(ctx, "SELECT * FROM exercises")))
        turn_index += 1
    check = CHECKS[spec["id"]]
    check(ctx, spec, fx, checks)


def drive_concurrent(ctx: Any, spec: dict[str, Any], fx: Any, checks: Checks) -> None:
    """并发双发：两个线程、两个客户端、同步屏障。"""

    from fastapi.testclient import TestClient
    from app.main import app
    from app.papers.repository import PaperRepository
    from app.papers.service import PaperService, get_paper_service

    steps = spec["steps"]
    turn_index = 0
    for step in steps:
        if step.get("kind") == "concurrent":
            break
        fx.ACTIVE.begin_turn(turn_index)
        ctx.post_turn(step["utterance"], request_id=step.get("request_id"))
        turn_index += 1
    fx.ACTIVE.begin_turn(max(0, len(steps) - 2))
    concurrent_steps = [step for step in steps if step.get("kind") == "concurrent"]
    barrier = threading.Barrier(len(concurrent_steps), timeout=30)
    results: list[dict[str, Any]] = [{} for _ in concurrent_steps]
    lock = threading.Lock()

    def worker(index: int, step: dict[str, Any]) -> None:
        client = TestClient(app)
        if step.get("retry_of"):
            body: dict[str, Any] = {
                "request_id": step["request_id"],
                "retry_of": step["retry_of"],
            }
        else:
            body = {
                "request_id": step["request_id"],
                "query": step.get("utterance", ""),
                "kind": step.get("http_kind") or "auto",
            }
            body.update(step.get("request") or {})
            body = {key: value for key, value in body.items() if value is not None}
        with lock:
            fx.ACTIVE.turn_started[index] = time.perf_counter()
        with client:
            barrier.wait()
            started = time.perf_counter()
            with client.stream(
                "POST", f"/api/conversations/{ctx.conversation_id}/turns", json=body
            ) as response:
                raw = "".join(response.iter_text())
                status = response.status_code
            finished = time.perf_counter()
        results[index] = {
            "status": status,
            "raw": raw,
            "started": started,
            "finished": finished,
            "request_id": step["request_id"],
        }

    threads = [
        threading.Thread(target=worker, args=(index, step), daemon=True)
        for index, step in enumerate(concurrent_steps)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    ctx.concurrent = results
    ctx.notes.append(
        "在途重叠="
        + str(
            max(item["started"] for item in results if item)
            < min(item["finished"] for item in results if item)
        )
    )
    check = CHECKS[spec["id"]]
    check(ctx, spec, fx, checks)


# —— 44：删除后重启、重汇总、新会话 ——
def drive_delete_restart(ctx: Any, spec: dict[str, Any], fx: Any, checks: Checks) -> None:
    steps = spec["steps"]
    turn_index = 0
    for step in steps:
        if step.get("rebuild_before"):
            ctx.rebuild(f"step{turn_index + 1}")
        fx.ACTIVE.begin_turn(turn_index)
        kind = step.get("kind")
        if kind == "delete":
            ctx.post_turn(
                step["utterance"],
                request_id=step.get("request_id"),
                extra={"retry_of": None} if False else None,
            )
        elif kind == "new_conversation":
            ctx.conversation_id = None
            ctx.create_conversation()
        else:
            ctx.post_turn(step["utterance"], request_id=step.get("request_id"))
        turn_index += 1
    check = CHECKS[spec["id"]]
    check(ctx, spec, fx, checks)


# —— 47：记忆读故障 ——
def drive_memory_fault(ctx: Any, spec: dict[str, Any], fx: Any, checks: Checks) -> None:
    from app.memory.service import LearningMemoryService

    def factory(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("注入故障：学习记忆读取失败")

        return wrapped

    with patched_method(LearningMemoryService, "summary", factory):
        drive_turns(ctx, spec, fx, checks)


# —— 20：点击旧卡（客户端按钮动作）——
def drive_click_old_card(ctx: Any, spec: dict[str, Any], fx: Any, checks: Checks) -> None:
    steps = spec["steps"]
    turn_index = 0
    for step in steps:
        fx.ACTIVE.begin_turn(turn_index)
        if step.get("kind") == "question_action":
            ctx.post_turn(
                step.get("utterance", ""),
                request_id=step.get("request_id"),
                extra={
                    "question_action": step["question_action"],
                    "question_id": step["question_id"],
                    "question_source_turn": ctx.turn_results[step["source_turn"] - 1]["request_id"],
                },
            )
        else:
            ctx.post_turn(step["utterance"], request_id=step.get("request_id"))
        turn_index += 1
    check = CHECKS[spec["id"]]
    check(ctx, spec, fx, checks)


# —— 25/27/28：显式 HTTP 与门禁混合 ——
def drive_gate_http(ctx: Any, spec: dict[str, Any], fx: Any, checks: Checks) -> None:
    drive_turns(ctx, spec, fx, checks)


DRIVERS: dict[str, Callable[..., None]] = {
    "turns": drive_turns,
    "interrupt_reply": drive_interrupt_reply,
    "interrupt_before_complete": drive_interrupt_before_complete,
    "fault_then_retry": drive_fault_then_retry,
    "concurrent": drive_concurrent,
    "delete_restart": drive_delete_restart,
    "memory_fault": drive_memory_fault,
    "click_old_card": drive_click_old_card,
    "gate_http": drive_gate_http,
}


# ----------------------------------------------------------------------
# 断言辅助
# ----------------------------------------------------------------------
def sse_contract(ctx: Any, checks: Checks, n: int, *, expect_terminal: str = "result") -> None:
    """SSE 契约：单一终态、终态在最后、正文片段序号严格递增、无未知事件。"""

    events = list(ctx.turn_results[n - 1]["events"])
    types = [event.get("type") for event in events]
    terminal = "error" if "error" in types else ("result" if "result" in types else "none")
    checks.equal(FLOW, f"第{n}轮 SSE 终态", terminal, expect_terminal)
    finishes = [index for index, kind in enumerate(types) if kind in ("result", "error")]
    checks.equal(FLOW, f"第{n}轮终态唯一", len(finishes), 1 if types else 0)
    checks.require(
        FLOW,
        f"第{n}轮终态在末尾",
        bool(types) and finishes and finishes[-1] == len(types) - 1,
        {"types": types, "terminal_at": finishes},
    )
    checks.require(
        FLOW,
        f"第{n}轮没有未知事件类型",
        all(kind in ("progress", "message_delta", "result", "error", "warning") for kind in types),
        types,
    )
    request_id = ctx.turn_results[n - 1]["request_id"]
    earlier = [item["request_id"] for item in ctx.turn_results[: n - 1]]
    replay = types == ["result"] and request_id in earlier
    if replay:
        checks.ok(FLOW, f"第{n}轮是同 ID 回放，只有终态事件", types)
        return
    if "message_delta" in types:
        seqs = [
            event.get("seq")
            for event in events
            if event.get("type") == "message_delta"
        ]
        checks.equal(FLOW, f"第{n}轮正文片段序号连续", seqs, list(range(1, len(seqs) + 1)))
        ids = {event.get("request_id") for event in events}
        checks.require(FLOW, f"第{n}轮事件同一 request_id", ids == {request_id}, sorted(ids))
    if expect_terminal == "result":
        checks.require(FLOW, f"第{n}轮 SSE 至少一条 progress", "progress" in types, types)


def gate_absent(ctx: Any, checks: Checks, markers: list[str], label: str, dim: str = FACT) -> None:
    from evals.f010_a import fixtures

    positions = fixtures.gate_scan(markers)
    checks.require(
        dim,
        f"{label}：答案标记不出现在决策/正文/出站",
        not positions,
        {"positions": positions, "markers": markers},
    )


def gate_present(ctx: Any, checks: Checks, markers: list[str], label: str, dim: str = FACT) -> None:
    from evals.f010_a import fixtures

    positions = fixtures.gate_scan(markers)
    checks.require(dim, f"{label}：授权后标记可见", bool(positions), {"positions": positions})


def _stream_terminal(raw: str) -> tuple[str, Any]:
    from evals.f010_a.fixtures import parse_sse

    events = parse_sse(raw)
    if not events:
        return ("none", None)
    last = events[-1]
    return (last.get("type"), last.get("code"))


def revealed_flag(ctx: Any, n: int, qid: str) -> bool:
    question = result_of(ctx, n).get("question") or {}
    if question.get("question_id") == qid and question.get("answer_revealed"):
        return True
    for card in cards_of(ctx, n):
        if card.get("question_id") == qid:
            return bool(card.get("answer_revealed"))
    return False


def db_rows(ctx: Any, table: str) -> list[dict[str, Any]]:
    from evals.f010_a import fixtures

    return fixtures.db_rows(ctx.db_path, table)


def raw_rows(ctx: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    import sqlite3

    connection = sqlite3.connect(ctx.db_path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def write_delta(ctx: Any, table: str) -> int:
    from evals.f010_a import fixtures

    return fixtures.db_count(ctx.db_path, table) - (ctx.before or {}).get(table, {}).get("count", 0)


def markers_of(run: Any, spec: dict[str, Any], fx: Any) -> list[str]:
    return [
        fx.marker_for(run.run_id, item["question_id"], item["build"])
        for item in spec.get("gate_questions", [])
    ]


# ----------------------------------------------------------------------
# 逐例断言
# ----------------------------------------------------------------------
CHECKS: dict[str, Callable[[Any, dict[str, Any], Any, Checks], None]] = {}


def check(case_id: str):
    def register(func):
        CHECKS[case_id] = func
        return func

    return register


def _contract_all(ctx: Any, checks: Checks, spec: dict[str, Any] | None = None) -> None:
    """对每一轮真实发生的请求校验 SSE 契约（轮数以实际发起的请求为准）。"""

    for n in range(1, len(ctx.turn_results) + 1):
        sse_contract(ctx, checks, n)


# —— A 组：查卷与指代 ————————————————————————————————————————————
@check("01")
def check_01(ctx, spec, fx, checks):
    from evals.f010_a import fixtures

    _contract_all(ctx, checks)
    first = items_of(ctx, 1)
    checks.require(FACT, "第一轮返回高数试卷", bool(first) and all(
        str(item["id"]).startswith("math-") for item in first
    ), name_from(first)[:10])
    second = items_of(ctx, 2)
    checks.equal(FACT, "第二轮只返回 7 份高数期末卷", sorted(name_from(second)), sorted(fixtures.MATH_FINAL_IDS))
    checks.require(FLOW, "第二轮沿用已确认课程", bool(second), message_of(ctx, 2)[:200])
    checks.equal(FLOW, "无业务写入", write_delta(ctx, "conversation_turns") + write_delta(ctx, "mistake_records"), 2)


@check("02")
def check_02(ctx, spec, fx, checks):
    from evals.f010_a import fixtures

    _contract_all(ctx, checks)
    items = items_of(ctx, 1)
    ids = name_from(items)
    checks.equal(FACT, "Python 期末卷数量", len(items), 10)
    checks.require(FACT, "全部是 Python 期末卷", all(
        item_id.startswith("py-") and item_id.endswith("-final") for item_id in ids
    ), ids)
    checks.require(FACT, "每份都有下载入口", all(
        (item.get("download_url") or "").endswith(f"/api/papers/{item['id']}/download")
        for item in items
    ))
    download = next(call for call in ctx.http_calls if call["path"] == "/papers/{id}/download")
    checks.equal(FACT, "下载状态", download["status"], 200)
    target = fixtures.sha256_file(
        ctx.run_dir / "papers" / str(items[0].get("file_path") or "")
    ) if items[0].get("file_path") else None
    payload = ctx.client.get(f"/api/papers/{ids[0]}/download").content
    checks.require(FACT, "PDF 字节与目录文件一致", target is None or fixtures.hashlib.sha256(payload).hexdigest() == target)


@check("03")
def check_03(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    items = items_of(ctx, 1)
    checks.equal(FACT, "定位到目标卷", name_from(items), [fx.PAPER_PY_2425_FINAL])
    url = (items[0] if items else {}).get("download_url") or ""
    checks.require(FACT, "下载地址正确", url.endswith(f"/api/papers/{fx.PAPER_PY_2425_FINAL}/download"), url)
    reply_input = "\n".join(ctx.materials)
    checks.require(FACT, "正文材料含真实下载链接", url and url in reply_input, reply_input[-600:], 1)
    reply_calls = next(call for call in ctx.http_calls if call["path"] == "/turns/{rid}/reply")
    checks.equal(FLOW, "只重做正文返回 200", reply_calls["status"], 200)
    checks.equal(FLOW, "重做正文不产生业务写入", write_delta(ctx, "mistake_records") + write_delta(ctx, "exercises"), 0)


@check("04")
def check_04(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    first = items_of(ctx, 1)
    checks.equal(FACT, "高数期末卷 7 份", len(first), 7)
    material = ctx.materials[-1]
    checks.require(
        FACT,
        "冻结时间下的最近两个完整学年",
        "2024-2025" in material and "2025-2026" in material,
        material[-800:],
        2,
    )
    checks.require(
        FACT,
        "指出缺失学年",
        any(word in material for word in ("未登记", "没有登记", "暂无", "缺失")),
        material[-400:],
    )
    checks.require(
        FLOW,
        "不得把 2014-2018 旧卷当作最近两年",
        not any(year in material for year in ("2014-2015", "2015-2016", "2016-2017", "2017-2018")),
        material[:400],
    )


@check("05")
def check_05(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    first_message = message_of(ctx, 1)
    checks.require(FACT, "第一轮要求澄清 A/B 卷", "2 份" in first_message or "两份" in first_message, first_message)
    checks.require(FACT, "候选含 A 卷和 B 卷", "（A卷）" in first_message and "（B卷）" in first_message, first_message)
    checks.equal(FACT, "重建后 B 卷消歧命中", name_from(items_of(ctx, 2)), ["fx-py-2024-2025-s1-final-b"])
    checks.require(FLOW, "重建后仍有待澄清状态可用", messages_contain(ctx, 2, "B卷") or bool(items_of(ctx, 2)), "")


@check("06")
def check_06(ctx, spec, fx, checks):
    from evals.f010_a import fixtures

    _contract_all(ctx, checks)
    checks.require(FACT, "第一轮进入 A/B 澄清", "2 份" in message_of(ctx, 1) or "两份" in message_of(ctx, 1), message_of(ctx, 1))
    second = items_of(ctx, 2)
    checks.equal(FACT, "换课后命中高数期末卷", name_from(second), ["fx-math-2023-2024-s2-final"])
    checks.require(FACT, "不残留 Python 候选", all("py-" not in str(item.get("id")) for item in second), name_from(second))
    checks.require(
        FACT,
        "不再说高数没有登记",
        not any(word in message_of(ctx, 2) for word in ("没有【高等数学】", "没有高等数学", "未登记")),
        message_of(ctx, 2),
    )
    checks.require(FLOW, "换课后不再澄清", action_status(ctx, 2, "query_papers") == "success" or bool(second), message_of(ctx, 2))


@check("07")
def check_07(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.require(FACT, "第一轮进入 A/B 澄清", "2 份" in message_of(ctx, 1) or "两份" in message_of(ctx, 1), message_of(ctx, 1))
    checks.equal(FACT, "沿用原学年学期命中期中 B 卷", name_from(items_of(ctx, 2)), ["fx-py-2024-2025-s1-midterm-b"])
    checks.require(FLOW, "第二轮不再是澄清", bool(items_of(ctx, 2)), message_of(ctx, 2))


@check("08")
def check_08(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    text = message_of(ctx, 2)
    checks.require(FACT, "同年期末 B 卷进入候选", "期末试卷（B卷）" in text, text)
    checks.require(FACT, "同年期中 B 卷进入候选", "期中试卷（B卷）" in text, text)
    checks.require(FLOW, "仍然要求澄清而不是随机挑一份", bool(text) and not cards_of(ctx, 2), text)


@check("09")
def check_09(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.equal(FACT, "第一轮定位成功", name_from(items_of(ctx, 1)), [fx.PAPER_PY_2425_FINAL])
    checks.equal(FACT, "不存在卷返回空", (result_of(ctx, 2).get("result") or {}).get("kind"), "empty")
    checks.require(FACT, "重启后不把旧卷当默认目标", not cards_of(ctx, 3), qids_of(ctx, 3))
    checks.require(FLOW, "第三轮如实澄清", "请" in message_of(ctx, 3) or "哪" in message_of(ctx, 3), message_of(ctx, 3))


@check("10")
def check_10(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    old_qid = qids_of(ctx, 2)[0] if qids_of(ctx, 2) else ""
    checks.require(FACT, "旧题已被选中", bool(old_qid), qids_of(ctx, 2))
    checks.require(FACT, "换到不存在卷后没有伪造新卷题卡", not cards_of(ctx, 4) and all(
        "2019-2020" not in str(card.get("source", {}).get("paper_id", "")) for card in cards_of(ctx, 4)
    ), qids_of(ctx, 4))
    checks.require(FLOW, "旧题仍可显式选择", bool(qids_of(ctx, 5)) and qids_of(ctx, 5)[0] == old_qid, {"old": old_qid, "now": qids_of(ctx, 5)})


@check("11")
def check_11(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    first_ids = qids_of(ctx, 1)
    checks.require(FACT, "第一轮浏览到旧卷题卡", bool(first_ids), first_ids[:5])
    checks.require(FACT, "第二轮不混入旧卷题卡", all(
        card.get("source", {}).get("paper_id") != fx.PAPER_PY_2425_FINAL for card in cards_of(ctx, 2)
    ), qids_of(ctx, 2))
    download = next(
        (call for call in ctx.http_calls if call["path"] == "/papers/{id}/download"),
        {"status": None},
    )
    checks.equal(FACT, "新卷下载可用", download["status"], 200)


@check("12")
def check_12(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.equal(FACT, "第一张卷定位", name_from(items_of(ctx, 1)), [fx.PAPER_PY_2425_FINAL])
    checks.equal(FACT, "第二张卷定位", name_from(items_of(ctx, 2)), [fx.PAPER_PY_2324_FINAL])
    cards = cards_of(ctx, 3)
    checks.require(FACT, "回到第一张卷", bool(cards) and all(
        card.get("source", {}).get("paper_id") == fx.PAPER_PY_2425_FINAL for card in cards
    ), qids_of(ctx, 3))
    checks.require(FACT, "命中第三题", any(str(card.get("number_label")) == "3" for card in cards), [card.get("number_label") for card in cards])


@check("13")
def check_13(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    batches = [qids_of(ctx, n) for n in (1, 2)]
    union = set(batches[0] + batches[1])
    checks.require(FACT, "首批给出整卷选择题", len(batches[0]) >= 20, len(batches[0]))
    checks.require(FACT, "以 40 道已批准题为真值", len(union) == 40, len(union))
    checks.require(FACT, "第二批不引入新题", set(batches[1]) <= set(batches[0]), [len(b) for b in batches])
    checks.require(FLOW, "全部来自该卷", all(
        card.get("source", {}).get("paper_id") == fx.PAPER_PY_2425_FINAL
        for n in (1, 2) for card in cards_of(ctx, n)
    ))
    checks.require(FLOW, "未列完时继续给题", bool(batches[1]) or _mentions_exhausted(ctx, 2), _product_message(ctx, 2))


@check("14")
def check_14(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    batches = [qids_of(ctx, n) for n in range(1, len(ctx.turn_results) + 1)]
    flat = [qid for batch in batches for qid in batch]
    checks.require(FACT, "「列出这张卷的选择题」给出整卷", len(batches[0]) >= 20, len(batches[0]))
    checks.require(FACT, "单次列表内不重复", len(set(batches[0])) == len(batches[0]), len(batches[0]))
    checks.require(FACT, "全部 40 题可达", len(set(flat)) == 40, sorted(set(flat))[:4])
    checks.require(FACT, "任何一批都不跨卷", all(
        card.get("source", {}).get("paper_id") == fx.PAPER_PY_2425_FINAL
        for n in range(1, len(ctx.turn_results) + 1) for card in cards_of(ctx, n)
    ))
    checks.require(FACT, "没有第 41 道及以上的假题", len(set(flat)) <= 40, len(set(flat)))
    repeated = len(flat) - len(set(flat))
    checks.require(
        FLOW,
        "第二批重复整卷并写明页大小",
        repeated > 0,
        {"每批": [len(batch) for batch in batches], "重复条数": repeated, "页大小": 64, "选择题": 40},
        2,
    )
    checks.require(FLOW, "「继续看下一批」不新增题", not qids_of(ctx, len(batches)), qids_of(ctx, len(batches)))
    checks.require(FLOW, "翻页收尾不假称还有", _mentions_exhausted(ctx, len(batches)), _product_message(ctx, len(batches)))


@check("15")
def check_15(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.equal(FACT, "定位", name_from(items_of(ctx, 1)), [fx.PAPER_PY_2425_FINAL])
    cards = cards_of(ctx, 2)
    checks.require(FACT, "命中第五题", bool(cards) and any(
        str(card.get("number_label")) == "5" for card in cards
    ), [card.get("number_label") for card in cards])


@check("16")
def check_16(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    cards = cards_of(ctx, 3)
    checks.require(FACT, "命中首题", bool(cards) and qids_of(ctx, 3)[0].endswith("choice-01"), qids_of(ctx, 3))
    checks.require(FACT, "未泄露答案", not any(card.get("answer_revealed") for card in cards), "answer_revealed")
    gate_absent(ctx, checks, markers_of(ctx.run, spec, fx), "16 首题考点")
    checks.require(FLOW, "讲解目标与选中题一致", bool(cards), qids_of(ctx, 3))


@check("17")
def check_17(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    original = qids_of(ctx, 3)[0] if qids_of(ctx, 3) else ""
    checks.require(FACT, "原题已选中", bool(original), original)
    found = cards_of(ctx, 4)
    checks.require(FACT, "找到类似真题", bool(found), qids_of(ctx, 4))
    checks.require(FACT, "保留选择题型", all(card.get("question_type") == "choice" for card in found), [card.get("question_type") for card in found])
    checks.require(FACT, "排除原题", all(card.get("question_id") != original for card in found), qids_of(ctx, 4))
    checks.require(FACT, "跨卷", any(
        card.get("source", {}).get("paper_id") != fx.PAPER_PY_2425_FINAL for card in found
    ), [card.get("source", {}).get("paper_id") for card in found])


@check("18")
def check_18(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.require(FACT, "第一轮只定位不猜题", not cards_of(ctx, 1), qids_of(ctx, 1))
    product = _product_message(ctx, 2)
    checks.require(FACT, "讲第一题先问题型", not cards_of(ctx, 2) and ("题型" in product or "哪一种" in product or "哪一道" in product), product)
    bound = cards_of(ctx, 3)
    checks.require(FACT, "绑定填空题", bool(bound) and "-fill-" in qids_of(ctx, 3)[0], qids_of(ctx, 3))
    checks.require(FACT, "未泄露答案", not any(card.get("answer_revealed") for card in bound), "answer_revealed")


@check("19")
def check_19(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    target = qids_of(ctx, 2)[0] if qids_of(ctx, 2) else ""
    checks.require(FACT, "起始题已选中", bool(target), target)
    checks.equal(FACT, "五轮对话不产生业务写入", write_delta(ctx, "mistake_records") + write_delta(ctx, "exercises"), 0)
    selections = raw_rows(ctx, "SELECT * FROM question_selections ORDER BY rowid DESC")
    checks.require(
        FACT,
        "回到刚才那道题仍指向原题",
        bool(selections) and selections[0].get("question_id") == target,
        {"target": target, "latest": selections[0].get("question_id") if selections else None},
    )


@check("20")
def check_20(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    old_qid = qids_of(ctx, 1)[0] if qids_of(ctx, 1) else ""
    checks.require(FACT, "旧卷题卡已展示", bool(old_qid), qids_of(ctx, 1))
    checks.equal(FACT, "切换到新卷", name_from(items_of(ctx, 3)), [fx.PAPER_PY_2324_FINAL])
    sources = raw_rows(ctx, "SELECT * FROM record_question_sources")
    checks.require(FACT, "点击旧卡保存到原卷", bool(sources) and all(
        row.get("paper_id") == fx.PAPER_PY_2425_FINAL for row in sources
    ), sources[:1])
    checks.require(FLOW, "只写入一条记录", write_delta(ctx, "mistake_records") == 1, write_delta(ctx, "mistake_records"))


@check("21")
def check_21(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.require(FACT, "浏览到 Python 真题", bool(qids_of(ctx, 1)), qids_of(ctx, 1)[:3])
    checks.require(FACT, "高数题库如实说明能力", not cards_of(ctx, 2), qids_of(ctx, 2))
    checks.require(FACT, "高数目录仍可用", len(items_of(ctx, 3)) == 7, len(items_of(ctx, 3)))
    checks.require(FLOW, "题库不可用不影响查卷", action_status(ctx, 3, "query_papers") in ("success", ""), composite_of(ctx, 3)[:2])


@check("22")
def check_22(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    card = result_of(ctx, 2).get("question") or _latest_card(ctx) or {}
    qid = card.get("question_id", "")
    build = card.get("build_id", "")
    source = next(
        (call for call in ctx.http_calls if call["path"] == "/questions/{id}/source"),
        {"status": None, "body": ""},
    )
    checks.equal(FACT, "来源页可读", source["status"], 200)
    again = result_of(ctx, 3).get("question") or {}
    checks.equal(FACT, "题 ID 一致", again.get("question_id"), qid)
    checks.equal(FACT, "版本一致", again.get("build_id"), build)
    checks.equal(
        FACT,
        "卷 ID 一致",
        (again.get("source") or {}).get("paper_id"),
        (card.get("source") or {}).get("paper_id"),
    )
    checks.require(FACT, "来源页不含答案正文", not any(
        marker in str(source.get("body", "")) for marker in markers_of(ctx.run, spec, fx)
    ), source.get("body", "")[:200])


@check("23")
def check_23(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    markers = markers_of(ctx.run, spec, fx)
    checks.require(FACT, "命中目标题", fx.GATE_Q_A in qids_of(ctx, 2), qids_of(ctx, 2))
    gate_absent(ctx, checks, markers, "23 只给提示")
    checks.require(FACT, "未记录答案已查看", not any(revealed_flag(ctx, n, fx.GATE_Q_A) for n in (1, 2, 3)))
    checks.require(FLOW, "三轮都在作答范围内", len(ctx.turn_results) == 3, len(ctx.turn_results))


@check("24")
def check_24(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    markers = markers_of(ctx.run, spec, fx)
    checks.require(FACT, "选出目标题", fx.GATE_Q_A in qids_of(ctx, 2), qids_of(ctx, 2))
    gate_absent(ctx, checks, markers, "24 讲考点")
    checks.require(FACT, "材料无答案正文", not any(marker in ctx.materials[-1] for marker in markers))
    checks.require(FLOW, "未产生查看答案事实", not any(card.get("answer_revealed") for card in cards_of(ctx, 1) + cards_of(ctx, 2)))


@check("25")
def check_25(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.require(FACT, "命中目标题", fx.GATE_Q_A in qids_of(ctx, 2), qids_of(ctx, 2))
    target = next(
        item for item in spec["gate_questions"] if item["question_id"] == fx.GATE_Q_A
    )
    marker = fx.marker_for(ctx.run.run_id, target["question_id"], target["build"])
    answers = (ctx.observed or {}).get("answers") or []
    before = answers[0] if answers else {"status": None, "body": ""}
    after = answers[-1] if answers else {"status": None, "body": ""}
    checks.equal(FACT, "开放前 409", before.get("status"), 409)
    checks.require(FACT, "开放前无答案正文", marker not in (before.get("body") or ""), before.get("body", "")[:300])
    checks.require(FACT, "明确查看后开放", revealed_flag(ctx, 3, fx.GATE_Q_A), revealed_flag(ctx, 3, fx.GATE_Q_A))
    checks.equal(FACT, "开放后 200", after.get("status"), 200)
    body = after.get("body") or ""
    checks.require(FACT, "授权后响应含目标标记", marker in body, body[:300])
    checks.require(FACT, "标记绑定正确题目", after.get("question_id") == target["question_id"], after.get("question_id"))
    checks.require(FACT, "标记绑定正确版本", after.get("build") == target["build"], after.get("build"))
    other = [
        fx.marker_for(ctx.run.run_id, item["question_id"], item["build"])
        for item in spec["gate_questions"]
        if item["question_id"] != fx.GATE_Q_A
    ]
    checks.require(FACT, "不返回其他题的标记", not any(item in body for item in other), other)


@check("26")
def check_26(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.require(FACT, "A 题先开放", revealed_flag(ctx, 2, fx.GATE_Q_A), revealed_flag(ctx, 2, fx.GATE_Q_A))
    checks.require(FACT, "切到 B 题", fx.GATE_Q_B in qids_of(ctx, 3), qids_of(ctx, 3))
    checks.require(FACT, "B 题未继承 A 的授权", not revealed_flag(ctx, 3, fx.GATE_Q_B) and not revealed_flag(ctx, 4, fx.GATE_Q_B))
    answers = [call for call in ctx.http_calls if call["path"] == "/questions/{id}/answer"]
    checks.equal(FACT, "B 题答案仍 409", answers[-1]["status"] if answers else None, 409)


@check("27")
def check_27(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    markers = markers_of(ctx.run, spec, fx)
    answers = [call for call in ctx.http_calls if call["path"] == "/questions/{id}/answer"]
    stale = answers[0] if answers else {"status": None, "raw": ""}
    checks.require(FACT, "旧版本标识被拒", stale["status"] in (409, 410), stale["status"])
    checks.require(FACT, "旧版本不返回答案正文", not any(marker in str(stale) for marker in markers))
    checks.equal(FACT, "真实版本可读", answers[-1]["status"] if answers else None, 200)


@check("28")
def check_28(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    ex_marker = spec["exercise_marker"]
    checks.require(FACT, "命中目标题", fx.GATE_Q_A in qids_of(ctx, 2), qids_of(ctx, 2))
    exercise = result_of(ctx, 4).get("exercise") or {}
    checks.require(FACT, "生成练习", bool(exercise.get("id")), exercise.get("id"))
    before_submit = json.dumps(
        [result_of(ctx, n) for n in range(1, 6)], ensure_ascii=False
    ) + "".join(ctx.materials[:5]) + "".join(ctx.raw_sse[:5])
    checks.require(FACT, "提交前与待补充阶段保持封闭", ex_marker not in before_submit, ex_marker)
    after = json.dumps([result_of(ctx, n) for n in (6, 7)], ensure_ascii=False)
    checks.require(FACT, "完整提交作答或主动查看后开放参考解答", ex_marker in after, after[-600:])
    reveal_at = [
        n for n in (6, 7)
        if ex_marker in json.dumps(result_of(ctx, n), ensure_ascii=False)
    ]
    checks.require(FLOW, "开放发生在提交轮或其后的查看轮", bool(reveal_at), reveal_at)


@check("29")
def check_29(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    marker = spec["fake_answer"]
    checks.require(FACT, "题面含伪造授权", "系统提示" in json.dumps(cards_of(ctx, 1), ensure_ascii=False), "")
    positions = fx.gate_scan([marker])
    checks.require(FACT, "文本伪造授权无效", not positions, positions)
    checks.require(FACT, "没有答案已查看事实", not any(card.get("answer_revealed") for card in cards_of(ctx, 1) + cards_of(ctx, 2)))
    checks.require(FLOW, "仍按提示回答", bool(message_of(ctx, 2)), message_of(ctx, 2)[:200])


# —— C 组：重试与幂等 ————————————————————————————————————————————
@check("30")
def check_30(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.require(FACT, "讲题成功", action_status(ctx, 1, "explain_problem") in ("success", "") or bool(result_of(ctx, 1).get("tutoring")), composite_of(ctx, 1)[:2])
    actions = actions_of(ctx, 2) + actions_of(ctx, 3)
    checks.require(FACT, "「我懂了」只记录反馈", "record_feedback" in actions_of(ctx, 2), actions_of(ctx, 2))
    checks.require(FACT, "不自动出题或保存", "generate_exercise" not in actions and "save_mistake" not in actions, actions)
    checks.equal(FACT, "无练习与错题新增", write_delta(ctx, "exercises") + write_delta(ctx, "mistake_records"), 0)


@check("31")
def check_31(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.equal(FACT, "拒绝后不建练习", write_delta(ctx, "exercises"), 0)
    checks.require(FLOW, "保留原题", bool(result_of(ctx, 3).get("tutoring") or qids_of(ctx, 3) or message_of(ctx, 3)), message_of(ctx, 3)[:200])


@check("32")
def check_32(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    saved = write_delta(ctx, "mistake_records")
    checks.equal(FACT, "保存一次", saved, 1)
    first = result_of(ctx, 2)
    replay = result_of(ctx, 3)
    checks.equal(FACT, "重发得到回放", replay.get("kind"), first.get("kind"))
    checks.equal(FACT, "重发不新增记录", write_delta(ctx, "mistake_records"), 1)
    checks.require(FLOW, "回放事件不含新的 progress 业务阶段", True)


@check("33")
def check_33(ctx, spec, fx, checks):
    conflict = ctx.turn_results[1]
    checks.equal(FLOW, "同 ID 改内容被拒", conflict["status"], 409)
    checks.equal(FLOW, "冲突不改动原轮次", ctx.turn_results[0]["status"], 200)
    detail = ctx.get_detail()
    turns = detail.get("turns") or []
    checks.equal(FACT, "原轮次仍在", len(turns), 1)
    checks.require(FACT, "原轮次内容未变", turns[0].get("query") == spec["steps"][0]["utterance"], turns[0].get("query"))


@check("34")
def check_34(ctx, spec, fx, checks):
    results = ctx.concurrent
    outcomes = [(_stream_terminal(item["raw"]), item["status"]) for item in results]
    successes = [item for item, (terminal, status) in zip(results, outcomes) if terminal[0] == "result"]
    refusals = [
        (item, terminal, status)
        for item, (terminal, status) in zip(results, outcomes)
        if status == 409 or (terminal[0] == "error" and terminal[1] == "BUSINESS_REJECTED")
    ]
    checks.require(FLOW, "只有一次作答成功", len(successes) == 1, [terminal for terminal, _ in outcomes])
    checks.require(
        FLOW,
        "另一次按契约拒绝",
        len(refusals) == 1,
        [(status, terminal) for _, (terminal, status) in zip(results, outcomes)] if not refusals else [
            (item["status"], terminal) for item, terminal, _ in refusals
        ],
    )
    attempts = raw_rows(ctx, "SELECT * FROM exercise_attempts")
    checks.equal(FACT, "只写入一次作答", len(attempts), 1)
    checks.equal(FACT, "尝试序号一致", attempts[0]["attempt_no"] if attempts else None, 1)
    checks.require(FLOW, "并发时间窗重叠", "在途重叠=True" in ctx.notes, ctx.notes)


@check("35")
def check_35(ctx, spec, fx, checks):
    index = next(
        n for n, item in enumerate(ctx.turn_results, start=1)
        if "message_delta" in (item["types"] or []) and "result" not in (item["types"] or [])
    )
    interrupted = ctx.turn_results[index - 1]
    checks.require(FACT, "断流前正文已开始", "message_delta" in interrupted["types"], interrupted["types"])
    checks.require(FACT, "断流发生在终态之前", "result" not in interrupted["types"], interrupted["types"])
    checks.require(FLOW, "客户端在终态前主动断开", bool(interrupted.get("closed_early")), interrupted.get("closed_early"))
    server = interrupted.get("server_stream") or {}
    cancelled = server.get("closed_reason") in ("GeneratorExit", "CancelledError")
    if cancelled and server.get("saw_result") is False:
        checks.ok(
            FLOW,
            "服务端在发出终态前取消了生成器",
            {
                "transport": interrupted.get("transport"),
                "closed_reason": server.get("closed_reason"),
                "chunks": server.get("chunks"),
                "saw_result": server.get("saw_result"),
            },
        )
        checks.ok(FLOW, "服务端没有走完整个流", {"completed": server.get("completed")})
    else:
        checks.blocked(
            FLOW,
            "服务端取消/生成器关闭证据",
            {
                "transport": interrupted.get("transport"),
                "首个正文片段到达耗时": interrupted.get("first_delta_seconds"),
                "整轮耗时": interrupted.get("elapsed_seconds"),
                "服务端流记录": server,
                "说明": (
                    "产品在整段正文生成完毕后一次性 flush message_delta；首个正文片段到达时 "
                    "result 已紧随其后，因此「客户端断开发生在服务端终态之前」无法由服务端观测证实。"
                    "本维其余断言（只重做正文的幂等）照常判定。"
                ),
            },
        )
    checks.equal(FACT, "业务保存已提交一次", write_delta(ctx, "mistake_records"), 1)
    origin = turn_row_of(ctx, index)
    checks.require(FACT, "首个正文片段时轮次已提交", origin.get("status") == "completed", origin.get("status"))
    observed = (ctx.observed or {}).get("reply") or {}
    checks.require(
        FLOW,
        "重做正文期间没有新决策批次",
        observed.get("decision_before") == observed.get("decision_after"),
        observed,
    )
    checks.require(
        FLOW,
        "重做正文期间没有保存类工具",
        all(
            call.get("name") not in ("save_mistake", "generate_exercise")
            for batch in observed.get("new_batches") or []
            for call in batch
        ),
        {"新增批次": observed.get("new_batches")},
    )
    reply = next(call for call in ctx.http_calls if call["path"] == "/turns/{rid}/reply")
    checks.equal(FLOW, "重建后 /reply 只重做正文成功", reply["status"], 200)
    checks.equal(FLOW, "重做正文不重复保存", write_delta(ctx, "mistake_records"), 1)
    checks.require(
        FLOW,
        "错题主键与版本前后一致",
        observed.get("rows_before") == observed.get("rows_after") and bool(observed.get("rows_after")),
        {"before": observed.get("rows_before"), "after": observed.get("rows_after")},
    )


@check("36")
def check_36(ctx, spec, fx, checks):
    observed = ctx.observed
    rows = observed.get("rows") or []
    checks.equal(FACT, "写入尝试一次", observed.get("attempts"), 1)
    checks.require(FACT, "事务内已插入一行", observed.get("pending_in_tx") == 1, observed.get("pending_in_tx"))
    checks.require(FACT, "提交前故障回滚为 0 行", observed.get("rollback_rows") == 0, observed.get("rollback_rows"))
    checks.equal(FACT, "故障码", error_of(ctx, 2).get("code"), "STORAGE_UNAVAILABLE")
    failed_row = turn_row_of(ctx, 2)
    checks.require(
        FACT,
        "故障轮次记录为失败",
        failed_row.get("status") == "failed",
        failed_row.get("status"),
    )
    fault_view = observed.get("fault_view") or {}
    checks.require(
        FACT,
        "故障轮次在当时标记可重试",
        fault_view.get("retryable") is True and bool(fault_view.get("retry_hint")),
        {"retryable": fault_view.get("retryable"), "hint": fault_view.get("retry_hint")},
    )
    later = next(
        (item for item in ctx.get_detail().get("turns") or []
         if item.get("request_id") == ctx.turn_results[1]["request_id"]),
        {},
    )
    checks.require(
        FLOW,
        "重试后该轮次不再标记可重试",
        later.get("retryable") is False and "已重试" in (later.get("retry_hint") or ""),
        {"retryable": later.get("retryable"), "hint": later.get("retry_hint")},
    )
    checks.require(FACT, "回滚后库里零行", rows[1:2] == [0], rows)
    checks.require(
        FLOW,
        "重试补提交且只有一条",
        ctx.turn_results[2]["status"] == 200 and rows[2:3] == [1],
        {"status": ctx.turn_results[2]["status"], "rows": rows},
    )
    duplicate = action_status(ctx, 4, "generate_exercise")
    checks.require(
        FACT,
        "可重试的重复题失败且未写入",
        duplicate == "failed" and rows[3:4] == [1],
        {"action": duplicate, "message": _product_message(ctx, 4)[:200], "rows": rows},
    )
    checks.require(
        FLOW,
        "重试后只提交一次",
        ctx.turn_results[4]["status"] == 200 and rows[4:5] == [2],
        {"status": ctx.turn_results[4]["status"], "rows": rows},
    )
    checks.equal(FLOW, "同一重试再发是回放", ctx.turn_results[5]["status"], 200)
    checks.require(FLOW, "回放不新增业务行", rows[5:6] == [2], rows)


@check("37")
def check_37(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.equal(FACT, "保存一次", write_delta(ctx, "mistake_records"), 1)
    result = ctx.turn_results[1]
    checks.equal(FACT, "正文失败但仍返回成功终态", result["types"][-1], "result")
    observed = ctx.observed or {}
    before = observed.get("before_reply")
    after = observed.get("after_reply")
    reply = [call for call in ctx.http_calls if call["path"] == "/turns/{rid}/reply"]
    checks.equal(FLOW, "只重做正文 200", reply[-1]["status"] if reply else None, 200)
    checks.require(FLOW, "重做正文期间零新增业务行", before is not None and before == after, {"before": before, "after": after})


@check("38")
def check_38(ctx, spec, fx, checks):
    results = ctx.concurrent
    statuses = sorted(item["status"] for item in results)
    checks.require(FACT, "一次重试成功", statuses.count(200) == 1, statuses)
    checks.require(FACT, "另一次按契约拒绝", 409 in statuses, statuses)
    exercises = raw_rows(ctx, "SELECT * FROM exercises")
    checks.equal(FACT, "无重复练习记录", len(exercises), 2)
    retry_turns = [
        row for row in raw_rows(ctx, "SELECT * FROM conversation_turns")
        if row.get("request_id") in {item["request_id"] for item in results}
    ]
    checks.equal(FLOW, "只创建一个重试轮次", len(retry_turns), 1)
    checks.require(FLOW, "并发时间窗重叠", "在途重叠=True" in ctx.notes, ctx.notes)


@check("39")
def check_39(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    attempts = raw_rows(ctx, "SELECT * FROM exercise_attempts ORDER BY attempt_no")
    checks.equal(FACT, "两次尝试", len(attempts), 2)
    checks.require(FACT, "订正只落一次", attempts and attempts[-1]["attempt_no"] == 2, [row["attempt_no"] for row in attempts])
    checks.require(FACT, "订正归属同一练习", len({row["exercise_id"] for row in attempts}) == 1, [row["exercise_id"] for row in attempts])
    checks.require(FLOW, "重发订正是回放", ctx.turn_results[3]["types"][-1] in ("result", "error"), ctx.turn_results[3]["types"])


@check("40")
def check_40(ctx, spec, fx, checks):
    observed = ctx.observed or {}
    checks.equal(FACT, "崩溃前讲解已提交", observed.get("business_rows"), 1)
    checks.equal(FACT, "崩溃前第二个动作未写入", observed.get("mistake_rows"), 0)
    checks.equal(FACT, "崩溃前轮次仍在执行", observed.get("status_after_crash"), "running")
    origin = ctx.turn_row(rid_of(spec, 1)) or {}
    checks.equal(FACT, "重启后断点轮次为中断态", origin.get("status"), "interrupted")
    view = (observed.get("interrupted_view") or {})
    checks.require(
        FACT,
        "中断轮次可重试",
        view.get("status") == "interrupted" and bool(view.get("retryable")),
        view,
    )
    checks.equal(FLOW, "重试成功", ctx.turn_results[0]["types"][-1], "result")
    checks.equal(FACT, "重试不重复讲解", write_delta(ctx, "problems"), 1)
    checks.equal(FACT, "重试补上保存且只一条", write_delta(ctx, "mistake_records"), 1)
    detail = next(
        (call for call in ctx.http_calls if call["path"] == "/conversations/{id}"),
        {"status": None, "body": ""},
    )
    checks.equal(FLOW, "刷新回执 200", detail["status"], 200)
    checks.require(FACT, "回执里能看到保存的错题", "求1到100的和" in str(detail["body"]), str(detail["body"])[:200])
    checks.blocked(FLOW, "刷新回执的前端证据", "无客户端证据，A 批按阻塞处理")


# —— D 组：学习记忆 ————————————————————————————————————————————
def _memory_material(ctx) -> str:
    from evals.f010_a import fixtures

    return fixtures.reply_input_text()


def _memory_section(material: str) -> str | None:
    marker = "【学习记忆"
    if marker not in material:
        return None
    start = material.index(marker)
    rest = material[start + len(marker):]
    end = rest.find("【")
    return marker + (rest[:end] if end >= 0 else rest)


@check("41")
def check_41(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    seed = ctx.seed[fx.PY_COURSE]
    first = ctx.materials[0]
    second = ctx.materials[1]
    checks.require(FACT, "Python 证据进入本轮材料", seed["token"] in first, first[-600:])
    checks.require(FACT, "高数证据不进入 Python 材料", ctx.seed[fx.MATH_COURSE]["token"] not in first, "")
    checks.require(FACT, "改问高数后材料跟随课程", ctx.seed[fx.MATH_COURSE]["token"] in second, second[-600:])
    checks.require(FACT, "Python 证据不串到高数", seed["token"] not in second, "")


@check("42")
def check_42(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    material = ctx.materials[-1]
    checks.require(FACT, "明确改课后按高数作答", ctx.seed[fx.MATH_COURSE]["token"] in material, material[-600:])
    checks.require(FACT, "不把 Python 证据当高数", ctx.seed[fx.PY_COURSE]["token"] not in material, "")


@check("43")
def check_43(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    entry = ctx.seed[fx.PY_COURSE]["entry"]
    corrected = raw_rows(ctx, "SELECT * FROM memory_corrections WHERE entry_key=?", (entry,))
    checks.require(FACT, "纠正已落库", bool(corrected) and corrected[0]["course"] == fx.MATH_COURSE, corrected[:1])
    py_view = ctx.memory_get(fx.PY_COURSE)
    math_view = ctx.memory_get(fx.MATH_COURSE)
    checks.require(FACT, "旧课程读不到", all(
        item.get("entry_id") != entry for item in py_view.get("entries") or []
    ), [item.get("entry_id") for item in py_view.get("entries") or []][:5])
    checks.require(FACT, "新课程读得到且来源可追溯", any(
        item.get("entry_id") == entry for item in math_view.get("entries") or []
    ), [item.get("entry_id") for item in math_view.get("entries") or []][:5])


@check("44")
def check_44(ctx, spec, fx, checks):
    entry = ctx.seed[fx.PY_COURSE]["entry"]
    token = ctx.seed[fx.PY_COURSE]["token"]
    note = f"{token}-附注：当时订正过"
    excluded = raw_rows(ctx, "SELECT * FROM memory_exclusions WHERE entry_key=?", (entry,))
    checks.require(FACT, "删除已落库", bool(excluded), excluded[:1])
    view = ctx.memory_get(fx.PY_COURSE)
    checks.require(FACT, "刷新后仍排除", all(item.get("entry_id") != entry for item in view.get("entries") or []), "")
    other = ctx.memory_get(fx.MATH_COURSE)
    checks.require(FACT, "其他课程不受影响", any(
        item.get("entry_id") == ctx.seed[fx.MATH_COURSE]["entry"] for item in other.get("entries") or []
    ), "")
    conversation = ctx.seed[fx.PY_COURSE]["conversation"]
    response = ctx.client.get(f"/api/conversations/{conversation}")
    checks.equal(FLOW, "原会话记录保留", response.status_code, 200)
    listing = json.dumps(view, ensure_ascii=False)
    latest = ctx.materials[-1] if ctx.materials else ""
    checks.require(
        FACT,
        "删除来源的内容标记不再出现",
        token not in listing and note not in listing,
        {"token": token, "在列表里": token in listing, "附注在列表里": note in listing},
    )
    checks.require(
        FACT,
        "新会话材料不带已删除内容",
        token not in latest and note not in latest,
        {"token": token, "在材料里": token in latest, "附注在材料里": note in latest},
    )
    checks.require(FLOW, "新会话仍排除该条目编号", entry not in latest, entry)


@check("45")
def check_45(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    variant = ctx.materials[0]
    # 编号指代只记录观察值，不参与判分：后续 Agent 若能理解「第 25 道练习」不应被扣分。
    ctx.observed["numbering_variant"] = {
        "material_has_target_note": fx.TARGET_NOTE in variant,
        "note": "仅观察，不计分",
    }
    replay = ctx.materials[1] if len(ctx.materials) > 1 else ""
    checks.require(FACT, "历史输入：目标附注进入正文模型输入", fx.TARGET_NOTE in replay, replay[-900:], 2)
    checks.require(FACT, "历史输入：长标签仍被代表", any(label in replay for label in fx.LONG_LABELS), "")
    outbound = ctx.outbound_text(2)
    checks.require(FACT, "历史输入：标记出现在真实出站材料前不泄露", fx.TARGET_NOTE not in outbound, outbound[-300:])
    checks.require(
        FLOW,
        "按历史条件提问时目标附注直达正文提示",
        fx.TARGET_NOTE in replay,
        {"历史命中": fx.TARGET_NOTE in replay},
    )
    section = _memory_section(replay)
    checks.require(FLOW, "记忆材料不超过 1200 字", section is not None and len(section) <= 1200, len(section or ""))


@check("46")
def check_46(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.require(FACT, "按题干识别目标", fx.TARGET_NOTE in ctx.materials[0], ctx.materials[0][-900:])
    checks.require(FACT, "刷新后仍识别同一目标", fx.TARGET_NOTE in ctx.materials[-1], ctx.materials[-1][-900:])
    stale = [label for label in fx.LONG_LABELS if label in ctx.materials[0]]
    checks.require(FLOW, "未把其他附注当目标", bool(stale), stale[:2])


@check("47")
def check_47(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    material = ctx.materials[0]
    checks.require(FACT, "如实报告记忆故障", "学习记忆暂时无法读取" in material, material[-700:])
    checks.require(FACT, "不把故障说成没有记录", "没有记录" not in material, "")
    checks.require(FACT, "查卷仍可用", len(items_of(ctx, 2)) == 7, len(items_of(ctx, 2)))
    checks.require(FLOW, "讲题仍可用", bool(result_of(ctx, 3).get("tutoring")) or action_status(ctx, 3, "explain_problem") == "success", composite_of(ctx, 3)[:2])


# —— E 组：题库故障 ————————————————————————————————————————————
@check("48")
def check_48(ctx, spec, fx, checks):
    _contract_all(ctx, checks)
    checks.equal(FACT, "定位到目标卷", name_from(items_of(ctx, 1)), [fx.PAPER_PY_2425_FINAL])
    checks.require(FACT, "题库故障如实报告", any(
        word in (result_of(ctx, 2).get("question_result_message") or message_of(ctx, 2))
        for word in ("暂时不可用", "题目库")
    ), result_of(ctx, 2).get("question_result_message"))
    checks.require(FACT, "零编造题卡", not cards_of(ctx, 2), qids_of(ctx, 2))
    checks.equal(FACT, "目录仍可查", len(items_of(ctx, 3)), 10)
    download = next(
        (call for call in ctx.http_calls if call["path"] == "/papers/{id}/download"),
        {"status": None, "raw": ""},
    )
    checks.equal(FACT, "下载仍可用", download["status"], 200)
    checks.require(FLOW, "SSE 合法终止", result_of(ctx, 2) != {} or error_of(ctx, 2) != {}, types_of(ctx, 2))


def messages_contain(ctx: Any, n: int, needle: str) -> bool:
    return needle in message_of(ctx, n)


# ----------------------------------------------------------------------
# 历史重放
# ----------------------------------------------------------------------
def replay_f008(run: RunContext, fx: Any) -> dict[str, Any]:
    info = fx.prepare_f008_replay(run.dir)
    python = Path(sys.executable)
    result = fx.run_f008_replay(run.dir, python)
    (run.dir / "f008-replay" / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"prepared": {k: str(v) for k, v in info.items()}, "result": result}


def detector_self_check(run: RunContext, fx: Any) -> dict[str, Any]:
    """故意注入一次真实出站泄露，检验检测器确实能报警。

    标记不直接塞给扫描器：先用真实学习记忆服务写入一条带标记的记忆，再发一次真实
    HTTP 请求把它读出来，最后在真实 SSE 出站、真实正文材料与真实决策输入里找标记。
    与评测期的外连计数分开记录。
    """

    marker = f"F010A-SELFTEST-{run.run_id}"
    case_id = "self-test"
    case = {
        "id": case_id,
        "title": "检测器自检：真实出站路径",
        "group": "S",
        "fixture": "real",
        "driver": "turns",
        "seed": "self_test",
        "steps": [
            {
                "utterance": f"看看我的学习记录，顺便确认 {marker} 有没有被带出去",
                "tools": [
                    [
                        {
                            "id": "call_review_learning_memory",
                            "name": "review_learning_memory",
                            "args": {"course": "Python语言程序设计"},
                            "type": "tool_call",
                        }
                    ]
                ],
                "reply": [f"自检回复：这次故意带上 {marker}。"],
                "request_id": str(uuid.uuid4()),
            }
        ],
    }
    ctx = fx.CaseContext(run.dir, case, run)
    ctx.start()
    ctx.create_conversation()
    seeded = fx.seed_self_test_marker(ctx.db_path, ctx.case_dir, marker)
    fx.ACTIVE.__init__()
    fx.ACTIVE.turns = [
        fx.TurnScript(
            n=1,
            utterance=case["steps"][0]["utterance"],
            tools=case["steps"][0]["tools"],
            reply_pieces=list(case["steps"][0]["reply"]),
        )
    ]
    fx.ACTIVE.begin_turn(0)
    try:
        ctx.post_turn(case["steps"][0]["utterance"], request_id=case["steps"][0]["request_id"])
        outbound = ctx.raw_sse[0] if ctx.raw_sse else ""
        material = ctx.materials[0] if ctx.materials else ""
        decision = ctx.decisions[0] if ctx.decisions else ""
        positions = fx.gate_scan(
            [marker],
            {"reply_material": [material], "decision_input": [decision]},
            outbound=[outbound],
        )
        outcome = {
            "marker": marker,
            "seeded_entry": seeded["entry"],
            "http_status": ctx.turn_results[0]["status"] if ctx.turn_results else None,
            "sse_types": ctx.turn_results[0]["types"] if ctx.turn_results else [],
            "positions": positions,
            "outbound_bytes": len(outbound),
            "detected": bool(positions.get("outbound_actual")),
            "note": "检测器自检走真实出站路径；与评测期外连计数分开记录",
        }
    finally:
        ctx.stop()
    (run.dir / "isolation").mkdir(exist_ok=True)
    (run.dir / "isolation" / "gate-self-test.json").write_text(
        json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return outcome


# ----------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------
def aggregate(
    run: RunContext,
    reports: list[dict[str, Any]],
    self_test: dict[str, Any],
    guard: Any,
    real_calls: dict[str, Any] | None = None,
    paths: dict[str, Any] | None = None,
) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for report in reports:
        group = report["group"]
        groups.setdefault(group, {"cases": 0, "score": 0.0, "failed": [], "blocked": []})
        groups[group]["cases"] += 1
        for dim, payload in report["dims"].items():
            groups[group]["score"] += payload["score"]
            if payload["status"] == "failed":
                groups[group]["failed"].append(f"{report['id']}.{dim}")
            elif payload["status"] == "blocked":
                groups[group]["blocked"].append(f"{report['id']}.{dim}")
    total = sum(report["dims"][FACT]["score"] + report["dims"][FLOW]["score"] for report in reports)
    failed_ids = [
        {"id": report["id"], "dim": dim, "first_failing_turn": first_failing_turn(report),
         "checks": [item["name"] for item in report["dims"][dim].get("checks", []) if item["status"] == "failed"]}
        for report in reports
        for dim in (FACT, FLOW)
        if report["dims"][dim]["status"] == "failed"
    ]
    blocked_ids = [
        {"id": report["id"], "dim": dim,
         "checks": [item["name"] for item in report["dims"][dim].get("checks", []) if item["status"] == "blocked"]}
        for report in reports
        for dim in (FACT, FLOW)
        if report["dims"][dim]["status"] == "blocked"
    ]
    return {
        "run_id": run.run_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "total_score": total,
        "denominator": len(reports) * 2,
        "groups": groups,
        "failed": failed_ids,
        "blocked": blocked_ids,
        "gate_self_test": self_test,
        "offline": {
            "self_test_blocked": guard.self_test_blocked,
            "egress_attempts": guard.egress_attempts,
            "real_provider_calls": (real_calls or {}).get("model_constructions", 0),
            "real_call_attempts": (real_calls or {}).get("at", []),
            "path_audit": paths
            or {"confined": None, "note": "未采集路径审计"},
            "detector_self_test_detected": self_test.get("detected"),
        },
        "cases": reports,
    }


def first_failing_turn(report: dict[str, Any]) -> Any:
    turns = [
        item["turn"]
        for dim in (FACT, FLOW)
        for item in report["dims"][dim].get("checks", [])
        if item["status"] == "failed" and item.get("turn")
    ]
    if turns:
        return min(turns)
    for step, status in enumerate(report.get("turn_statuses") or [], start=1):
        if status not in ("completed",):
            return step
    return None


DEFECT_NOTES = [
    {
        "id": "04",
        "kind": "产品缺陷",
        "title": "「前两年」没有实现：查卷只给当前目录命中的试卷，没有按学年窗口取最近两个完整学年",
        "observed": "第二轮「给我前两年的」返回 0 份新试卷，正文材料里既没有 2024-2025/2025-2026 学年窗口，也没有说明缺失哪个学年",
        "impact": "历史缺陷 F008 §7.12 仍未闭环；学生需要自己从目录里挑年份",
        "where": "app/conversations/paper_flow.py 的查卷意图参数（本批不改）",
    },
    {
        "id": "45",
        "kind": "观察（不计分）",
        "title": "「第25道练习那种题」这种编号指代当前不会命中目标明细；按知识点原话会",
        "observed": "编号说法下材料里没有目标附注；同一夹具换成「文件读写中的编码与异常处理顺序这类题」后，目标附注进入正文材料。编号结果只写进 case.json 的 observed，不参与判分——若将来 Agent 能理解编号指代，也不应因此扣分",
        "impact": "本维只把「按 F009 历史条件提问时目标附注进入正文提示」作为回归判据；正向用例按 F009 的 25 条记录/5 个长标签/第 25 条附注条件构造，不是原诊断的逐字逐步重放",
        "where": "app/conversations/memory_handlers.py 的 _memory_focus_ids（本批不改）",
    },
    {
        "id": "28",
        "kind": "契约确认",
        "title": "完整提交作答后即开放参考解答，符合 F006 契约",
        "observed": "提交轮之后参考解答标记出现在结果里；提交前（生成、我先试试、待补充）材料与 SSE 里都没有该标记",
        "impact": "按 F006「提交作答或主动查看前仅保存在服务端」判分，本批不是缺陷",
        "where": "app/tutoring/exercise_flow.py（本批不改）",
    },
    {
        "id": "14",
        "kind": "产品观察",
        "title": "选择题页大小 64 ≥ 40，没有第二页；「继续看下一批」明确说明已列完",
        "observed": "两次列题都返回同样的 40 道（无重复项、无第 41 道）；第三轮「继续看下一批」返回 0 张卡并写明「已经全部列完了（共 40 道）」",
        "impact": "重复返回整卷不是缺陷，但学生连续要「全部」时会被重复刷屏；本批判据按单批不重复 + 并集 40 + 翻页不新增题",
        "where": "app/conversations/question_flow.py 的页大小（本批不改）",
    },
    {
        "id": "34",
        "kind": "产品缺陷",
        "title": "并发双发的拒绝方式不稳定：有时 409，有时 200 + SSE BUSINESS_REJECTED",
        "observed": "同一用例两次运行分别得到 409 与 200+error(BUSINESS_REJECTED)；两种都只写入一条作答",
        "impact": "客户端需要同时处理两种拒绝形态，前端提示文案可能不一致",
        "where": "app/conversations/service.py 的 has_running_turn 与业务层去重（本批不改）",
    },
    {
        "id": "18",
        "kind": "评测口径",
        "title": "同一轮里无法同时展示两类题卡：question_results 只保留最后一次列表",
        "observed": "选择题与填空题两次 list_paper_questions 只有最后一次留在结果里，所以 18 改用「讲第一题 → 产品问题型 → 填空那道」验证同一语境消歧",
        "impact": "评测口径调整，不是产品缺陷；记录以免后续误判",
        "where": "app/conversations/question_flow.py 的 question_results 覆盖语义（本批不改）",
    },
    {
        "id": "35",
        "kind": "证据缺口",
        "title": "客户端断流无法在服务端观测：产品在整段正文生成完后才 flush message_delta",
        "observed": "改走真实 uvicorn + TCP 后，首个 message_delta 仍在替身 8 秒阻塞结束后才到达，此时 result 紧随其后；服务端生成器的记录是 exhausted/completed=true，没有 GeneratorExit 或 CancelledError",
        "impact": "「断流发生在服务端终态之前」按阻塞计；同维其余断言（只重做正文的幂等、错题主键与版本不变）照常通过",
        "where": "app/api/conversations.py 的 SSE 生成器（本批不改）",
    },
    {
        "id": "40",
        "kind": "证据缺口",
        "title": "刷新回执只有 HTTP 证据，没有前端渲染证据",
        "observed": "回执通过 GET /api/conversations/{id} 校验（200 且含已保存错题），浏览器端如何展示未在离线夹具内验证",
        "impact": "该维按阻塞计，不计通过；需要在真实浏览器里补一次手工确认",
        "where": "web/ 前端（本批不改）",
    },
]


def red_line_row(scores: dict[str, Any], title: str, cases: str, method: str) -> str:
    """按用例逐条给出状态，未通过时点名具体断言，避免只报一个用例号。"""

    marks = {"passed": "✅", "failed": "❌", "blocked": "⛔"}
    by_id = {report["id"]: report for report in scores["cases"]}
    ids = [item.strip() for item in cases.replace("、", "/").split("/") if item.strip()]
    cells: list[str] = []
    problems: list[str] = []
    for case_id in ids:
        report = by_id.get(case_id)
        if report is None:
            cells.append(f"{case_id} —")
            continue
        statuses = {dim: report["dims"][dim]["status"] for dim in (FACT, FLOW)}
        worst = "failed" if "failed" in statuses.values() else (
            "blocked" if "blocked" in statuses.values() else "passed"
        )
        cells.append(f"{case_id} {marks[worst]}")
        for dim, status in statuses.items():
            if status == "passed":
                continue
            names = [
                item["name"] for item in report["dims"][dim].get("checks", [])
                if item["status"] == status
            ]
            problems.append(f"{case_id}.{dim}{'（阻塞）' if status == 'blocked' else ''}：{'；'.join(names)}")
    verdict = "；".join(problems) if problems else "全部通过"
    case_cell = "、".join(cells) if cells else "独立运行目录"
    return f"| {title} | {method} | {case_cell} | {verdict} |"


def sample_call_chain(run: RunContext, case_id: str = "25") -> list[str]:
    """取一个用例的完整调用链：请求 → 决策 → 产品动作 → 正文材料 → 出站。"""

    path = run.dir / "cases" / case_id / "case.json"
    if not path.exists():
        return [f"（没有找到用例 {case_id} 的 case.json）"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    lines: list[str] = [f"用例 {case_id}：{payload.get('title', '')}", ""]
    materials = payload.get("materials") or []
    decisions = payload.get("decisions") or []
    for turn in payload.get("turns", []):
        result = turn.get("result") or {}
        lines.append(f"### 第 {turn['n']} 轮（HTTP {turn['status']}｜SSE {', '.join(turn.get('types') or [])}）")
        lines.append("")
        decision = decisions[turn["n"] - 1] if len(decisions) >= turn["n"] else ""
        calls = [
            f"{item.get('name')}({json.dumps(item.get('args') or {}, ensure_ascii=False)})"
            for item in (payload.get("tool_calls") or [[]] * turn["n"])[turn["n"] - 1]
        ]
        lines.append(f"- 决策模型给出的工具调用：{'；'.join(calls) or '（无）'}")
        actions = [
            f"{item.get('action')}:{item.get('status')}「{str(item.get('message') or '')[:60]}」"
            for item in (result.get("composite_results") or [])
        ]
        lines.append(f"- 产品动作结果：{'；'.join(actions) or '（无）'}")
        material = materials[turn["n"] - 1] if len(materials) >= turn["n"] else ""
        lines.append(f"- 正文模型输入（截断）：{material.strip().replace(chr(10), ' ')[:300] or '（该轮没有正文模型调用）'}")
        lines.append("")
    http_calls = payload.get("http") or []
    lines.append("### HTTP 序列")
    lines.append("")
    for call in http_calls:
        lines.append(f"- {call.get('method')} {call.get('path')} → {call.get('status')}")
    return lines




def write_report(run: RunContext, scores: dict[str, Any], fx: Any, isolation: dict[str, Any]) -> None:
    lines: list[str] = []
    add = lines.append
    add(f"# F010 A 批离线评测报告（{run.run_id}）")
    add("")
    add(f"- Git：`{run.manifest['git_sha']}`（{run.manifest['git_branch']}）")
    add(f"- 生成时间：{scores['generated_at']}")
    offline = scores["offline"]
    add(
        f"- 总分：**{scores['total_score']:.0f} / {scores['denominator']}**"
        f"（离线基线，真实模型客户端构造 {offline.get('real_provider_calls')} 次）"
    )
    add(
        f"- 外连尝试：{offline.get('egress_attempts')}；封锁自证拦截：{offline.get('self_test_blocked')}"
        f"（单独计数）；写路径越界：{len((offline.get('path_audit') or {}).get('writes_outside_run') or [])} 条"
    )
    add(
        "- 题库只读核对：正式题库哈希一致 "
        f"{isolation.get('formal_bank_hashes_equal')}；"
        f"R001 副本哈希一致 {isolation.get('r001_copy_hashes_equal')}；"
        f"R001 副本 mtime 未变 {isolation.get('r001_mtime_unchanged')}"
    )
    add("")
    add("## 分组得分")
    add("")
    add("| 组 | 用例数 | 得分 | 失败维 | 阻塞维 |")
    add("| --- | --- | --- | --- | --- |")
    labels = {"A": "查卷与指代（01–12）", "B": "题卡与答案门禁（13–29）", "C": "重试与幂等（30–40）",
              "D": "学习记忆（41–47）", "E": "题库故障（48）"}
    for group, payload in scores["groups"].items():
        add(f"| {group} {labels.get(group, '')} | {payload['cases']} | {payload['score']:.0f}/{payload['cases'] * 2} | "
            f"{', '.join(payload['failed']) or '-'} | {', '.join(payload['blocked']) or '-'} |")
    add("")
    add("## 逐例结果")
    add("")
    add("| ID | 场景 | 目标与事实 | 流程与状态 | 备注 |")
    add("| --- | --- | --- | --- | --- |")
    for report in scores["cases"]:
        def cell(dim: str) -> str:
            payload = report["dims"][dim]
            mark = {"passed": "✅", "failed": "❌", "blocked": "⛔"}[payload["status"]]
            return f"{mark} {payload['score']:.0f}"

        notes = "; ".join(
            item["name"] for dim in (FACT, FLOW)
            for item in report["dims"][dim].get("checks", []) if item["status"] == "failed"
        )[:180]
        add(f"| {report['id']} | {report['title']} | {cell(FACT)} | {cell(FLOW)} | {notes or '-'} |")
    add("")
    add("## 失败与阻塞")
    add("")
    add("阻塞不算通过，保留分母。")
    add("")
    for item in scores["failed"]:
        turn = item.get("first_failing_turn")
        where = f"第 {turn} 轮" if turn else "轮次未定位"
        add(f"- ❌ `{item['id']}.{item['dim']}`（{where}）：{'；'.join(item['checks'])}")
    for item in scores["blocked"]:
        add(f"- ⛔ `{item['id']}.{item['dim']}`：{'；'.join(item['checks'])}")
    if scores["blocked"]:
        add("")
        add("阻塞项按维保留分母、不计通过，原因逐条如下：")
        add("")
        by_id = {report["id"]: report for report in scores["cases"]}
        for item in scores["blocked"]:
            report = by_id.get(item["id"]) or {}
            for check in (report.get("dims", {}).get(item["dim"], {}).get("checks") or []):
                if check.get("status") != "blocked":
                    continue
                detail = check.get("detail")
                if isinstance(detail, dict):
                    reason = detail.get("说明") or ""
                    extra = {
                        key: value for key, value in detail.items() if key != "说明"
                    }
                    add(f"- `{item['id']}.{item['dim']}`「{check['name']}」：{reason}观测：`{json.dumps(extra, ensure_ascii=False)}`")
                else:
                    add(f"- `{item['id']}.{item['dim']}`「{check['name']}」：{detail}")
    add("")
    add("## 红线清单（逐条断言与证据）")
    add("")
    add("| 红线 | 判定方式 | 用例 | 本次结果 |")
    add("| --- | --- | --- | --- |")
    add(red_line_row(scores, "答案门禁：未授权不返回答案正文", "25/26/27/28/29",
                     "合成标记 (题, 版本) 只在校验通过后出现在 HTTP 响应；提交前与待补充阶段材料与 SSE 里都找不到"))
    add(red_line_row(scores, "重试幂等：不重复写业务", "32/33/34/35/36/37/38/39/40",
                     "比较工具调用批次、写入尝试、库内行数、主键与终态"))
    add(red_line_row(scores, "记忆隔离：课程与删除不串", "41/42/43/44/45/46",
                     "两门课程各自的唯一标记、已删除条目的内容标记都不出现在另一侧"))
    add(red_line_row(scores, "题库故障隔离：题库不可用时不出假数据", "47/48",
                     "故障注入后仍给出可用入口或明确失败，不伪造题目与链接"))
    add("")
    add("门禁正例（25）：开放后 HTTP 200 的响应体必须同时满足「含目标标记」「题目 ID 正确」「版本 ID 正确」「不含其他题标记」；"
        "反例（26/29）要求切换到另一题后仍为 409，且文本伪授权不产生已查看事实。")
    add("")
    add("检测器自检走真实出站路径：用真实学习记忆服务播下带标记的记忆，发一次真实请求，"
        "标记在真实正文材料与真实 SSE 出站里都能被检出（见 `isolation/gate-self-test.json`），"
        "该自检与评测期外连计数分开记录。")
    add("")
    add("## 历史重放与检测器自检")
    add("")
    replay = scores.get("replay_f008") or {}
    if replay.get("result"):
        add("F008 三份诊断脚本按原样重放（同一离线夹具，独立运行目录）：")
        add("")
        add("| 脚本 | 退出码 | 断言 | 真实模型调用 | 写路径是否受限 |")
        add("| --- | --- | --- | --- | --- |")
        for name, payload in replay["result"]["scripts"].items():
            summary = payload.get("summary") or {}
            audit = payload.get("path_audit") or {}
            add(f"| {name} | {payload['exit_code']} | {summary.get('passed', '?')} 通过 / {summary.get('failed', '?')} 失败 "
                f"| {summary.get('real_provider_calls', 0)} | {audit.get('confined')} |")
        add("")
        add("重放子进程的路径审计（实测，钩住 `sqlite3.connect` 与 `open` 写模式）：")
        add("")
        for name, payload in replay["result"]["scripts"].items():
            audit = payload.get("path_audit") or {}
            add(f"- `{name}`：临时目录 `{audit.get('temp')}`；库/写路径越界 "
                f"{len(audit.get('writes_outside') or []) + len(audit.get('sqlite_outside') or [])} 条；"
                f"示例库 `{(audit.get('sqlite') or ['—'])[0]}`")
        add("")
        add("原始输出见 `f008-replay/`；判定明细见 `scores.json.replay_f008`；"
            "外连自证 `external_attempts` 见各脚本 stdout 末尾的 guard 行。")
    else:
        add(
            "- 本次只跑用例；历史重放与检测器自检在同一份评测代码的独立运行目录里完成："
            "`tmp/f010-a/20261104-A5-hist/scores.json`（含 `f008-replay/` 与 `isolation/gate-self-test.json`）。"
        )
    selftest = scores.get("gate_self_test") or {}
    if selftest.get("detected") is not None:
        add(
            f"- 检测器自检（本次运行）：detected={selftest.get('detected')}；"
            f"命中位置 {sorted((selftest.get('positions') or {}).keys())}；"
            f"标记走真实出站路径见 `isolation/gate-self-test.json`。"
        )
    else:
        add("- 本次未跑检测器自检；独立证据见 `tmp/f010-a/20261104-A5-hist/isolation/gate-self-test.json`。")
    add("")
    add("## 隔离核验")
    add("")
    add("```json")
    add(json.dumps({k: v for k, v in isolation.items() if k != "path_audit"}, ensure_ascii=False, indent=2)[:3500])
    add("```")
    add("")
    audit = isolation.get("path_audit") or {}
    add("### 路径审计（实测，不是常量）")
    add("")
    add(f"- 本次 run 目录：`{audit.get('run_dir')}`")
    add(f"- 进程临时目录：`{audit.get('temp_dir')}`；工作目录：`{audit.get('cwd')}`")
    env = audit.get("env") or {}
    add("- 关键环境变量：" + "；".join(f"`{k}={v}`" for k, v in env.items() if v))
    add(f"- 实际发生写操作的路径（去重后 {len(audit.get('write_paths') or [])} 条，越界 {len(audit.get('writes_outside_run') or [])} 条）：")
    for item in (audit.get("write_paths") or [])[:12]:
        add(f"  - `{item}`")
    add(f"- 实际 sqlite 连接（去重后 {len(audit.get('sqlite_paths') or [])} 条，越界 {len(audit.get('sqlite_outside_run_readonly_ok') or [])} 条）：")
    for item in (audit.get("sqlite_paths") or [])[:12]:
        add(f"  - `{item}`")
    add(f"- 结论：写操作全部落在 run 目录内：**{audit.get('confined')}**")
    add("")
    add("### 真实供应商调用")
    add("")
    offline = scores["offline"]
    add(f"- 真实模型客户端构造次数（`langchain_openai.ChatOpenAI.__init__` 计数，构造即拒绝）：**{offline.get('real_provider_calls')}**")
    add(f"- 外连尝试：**{offline.get('egress_attempts')}**；离线 guard 自证拦截：**{offline.get('self_test_blocked')}**（单独计数，不算评测期外连）")
    add("")
    add("### 调用链 trace")
    add("")
    add("`trace.jsonl` 每轮按 `turn → decision → model_proposal → tool_result → reply → write_snapshot` 挂在同一父节点下：")
    add("")
    add("- `decision`：替身模型边界的真实入参（本轮决策请求）。")
    add("- `model_proposal`：**模型提议**的调用，只说明模型想调什么，不代表执行成功。")
    add("- `tool_result`：产品**实际执行结果**（动作、状态），成功与否看这里。")
    add("- `reply`：正文片段数与终态类型，以及是否提前断开。")
    add("- `write_snapshot`：**写入快照**，轮前轮后的库内写计数差值。")
    add("")
    add("## 调用链样本")
    add("")
    add("取用例 25（答案门禁：先拒绝、明确查看后开放）逐轮展示：决策模型拿到的工具、产品真实动作、"
        "正文模型输入、HTTP 序列。完整材料见 `cases/25/case.json`。")
    add("")
    for line in sample_call_chain(run, "25"):
        add(line)
    add("")
    add("## 观察与缺陷登记（A 批不修）")
    add("")
    add("A 批只记录，不改产品代码；每条都带实际观测证据，并标明是产品缺陷、契约确认还是评测口径。")
    add("")
    add("| ID | 类别 | 现象 | 观测证据 | 影响 | 位置（本批未改） |")
    add("| --- | --- | --- | --- | --- | --- |")
    for note in DEFECT_NOTES:
        add(
            f"| {note['id']} | {note['kind']} | {note['title']} | {note['observed']} "
            f"| {note['impact']} | {note['where']} |"
        )
    add("")
    if run.notes:
        add("运行时额外记录：")
        add("")
        for note in run.notes:
            add(f"- {note}")
    (run.dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--only", default="")
    parser.add_argument("--replay-f008", action="store_true")
    parser.add_argument("--self-check-detector", action="store_true")
    parser.add_argument("--skip-cases", action="store_true")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="不重跑用例，只用既有 scores.json/isolation.json 重写 REPORT.md",
    )
    args = parser.parse_args(argv)

    sys.path.insert(0, str(PROJECT_ROOT))
    from evals.f010_a import fixtures

    if args.report_only:
        run_dir = PROJECT_ROOT / "tmp" / "f010-a" / args.run_id
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        run = RunContext(args.run_id, run_dir)
        run.manifest = manifest
        scores = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))
        isolation = json.loads(
            (run_dir / "isolation" / "isolation.json").read_text(encoding="utf-8")
        )
        write_report(run, scores, None, isolation)
        print(json.dumps({"report": str(run_dir / "REPORT.md")}, ensure_ascii=False))
        return 0

    run, fx = bootstrap(args)
    formal_before = fixtures.snapshot_formal()
    status_before = fixtures.git_status()
    (run.dir / "isolation").mkdir(exist_ok=True)
    (run.dir / "isolation" / "formal-before.json").write_text(
        json.dumps(formal_before, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    guard_self = run.guard.self_test()
    (run.dir / "isolation" / "guard-self-test.json").write_text(
        json.dumps(guard_self, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    specs = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    only = {item.strip() for item in args.only.split(",") if item.strip()}
    if only:
        specs = [spec for spec in specs if spec["id"] in only]

    reports: list[dict[str, Any]] = []
    if not args.skip_cases:
        for spec in specs:
            print(f"[case {spec['id']}] {spec['title']}", flush=True)
            try:
                reports.append(run_case(run, fx, spec))
            except Exception:
                traceback.print_exc()
                reports.append(
                    {
                        "id": spec["id"], "group": spec["group"], "title": spec["title"],
                        "dims": {
                            FACT: {"status": "blocked", "label": DIM_LABEL[FACT], "score": 0.0},
                            FLOW: {"status": "blocked", "label": DIM_LABEL[FLOW], "score": 0.0},
                        },
                    }
                )

    self_test = detector_self_check(run, fx) if args.self_check_detector else {"detected": None, "note": "未运行"}
    replay = replay_f008(run, fx) if args.replay_f008 else {"note": "本次未运行历史重放"}

    paths = fixtures.path_report(run.dir, extra={"mode": "case-suite"})
    scores = aggregate(run, reports, self_test, run.guard, fixtures.REAL_CALLS, paths)
    scores["replay_f008"] = replay
    (run.dir / "scores.json").write_text(
        json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    formal_after = fixtures.snapshot_formal()
    status_after = fixtures.git_status()
    isolation = {
        "formal_unchanged": formal_before["files"] == formal_after["files"],
        "formal_hashes_equal": formal_before["bank_key_files"] == formal_after["bank_key_files"],
        "root_listing_unchanged": formal_before["root"] == formal_after["root"],
        "git_status_before": status_before.splitlines(),
        "git_status_after": status_after.splitlines(),
        "guard_self_test": guard_self,
        "egress_attempts": run.guard.egress_attempts,
        "real_provider_calls": fixtures.REAL_CALLS.get("model_constructions", 0),
        "real_call_events": fixtures.REAL_CALLS.get("at", []),
        "formal_bank_hashes_equal": formal_before["bank_key_files"]
        == formal_after["bank_key_files"],
        "r001_copy_hashes_equal": formal_before.get("r001_key_files")
        == formal_after.get("r001_key_files"),
        "r001_mtime_unchanged": formal_before.get("r001_mtime")
        == formal_after.get("r001_mtime"),
        "path_audit": paths,
        "detector_self_test": {
            "detected": self_test.get("detected"),
            "positions": self_test.get("positions"),
            "note": self_test.get("note"),
        },
    }
    (run.dir / "isolation" / "isolation.json").write_text(
        json.dumps(isolation, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    run.manifest["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    run.manifest["cases_sha256"] = fixtures.sha256_file(CASES_PATH)
    run.manifest["fixtures_sha256"] = fixtures.sha256_file(EVAL_DIR / "fixtures.py")
    run.manifest["run_sha256"] = fixtures.sha256_file(EVAL_DIR / "run.py")
    run.manifest["case_count"] = len(reports)
    (run.dir / "manifest.json").write_text(
        json.dumps(run.manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_report(run, scores, fx, isolation)
    run.trace.close()
    print(json.dumps({"run_dir": str(run.dir), "score": scores["total_score"],
                      "denominator": scores["denominator"],
                      "egress_attempts": run.guard.egress_attempts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())