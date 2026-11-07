"""F010 A 批离线评测：装配、夹具、模型替身、故障挂钩与透明观测。

边界（与 A 批批准范围一致）：
- 只在本评测进程内替换**模型适配器**与题库运行时；产品代码、产品测试、
  正式数据、任务书均不修改。
- 每个用例独立 SQLite v6、附件根和工作目录；题库、目录、PDF 都是本次
  run 目录内的只读副本。
- 真实模型传输为零：替身之外还有 socket/DNS/HTTP 封锁，并单独自证生效。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_ROOT = PROJECT_ROOT / "tmp" / "f010-a"
FORMAL_DB = PROJECT_ROOT / "data" / "local" / "study.sqlite3"
FORMAL_ATTACHMENTS = PROJECT_ROOT / "data" / "local" / "attachments"
FORMAL_PAPERS = PROJECT_ROOT / "data" / "papers"
FORMAL_CATALOG = FORMAL_PAPERS / "catalog.json"
FORMAL_QB = PROJECT_ROOT / "data" / "local" / "question-bank"
R001_QB = PROJECT_ROOT / "tmp" / "r001-20261101" / "qb"

BANK_BUILD = "iv-6ed04bac2252c23d"
BANK_KEY_FILES = (
    "db/current.json",
    "db/reviews.json",
    f"db/builds/{BANK_BUILD}/build.json",
    f"db/builds/{BANK_BUILD}/questions.sqlite3",
    f"db/builds/{BANK_BUILD}/qdrant/meta.json",
    f"db/builds/{BANK_BUILD}/bm25/index.json",
    "models/models--Qdrant--bge-small-zh-v1.5/refs/main",
    "overrides.json",
)
FORMAL_WATCH_FILES = (
    FORMAL_DB,
    FORMAL_CATALOG,
    FORMAL_QB / "db" / "current.json",
    FORMAL_QB / "db" / "reviews.json",
    FORMAL_QB / "db" / "builds" / BANK_BUILD / "questions.sqlite3",
)

WRITE_TABLES = {
    "conversations": "id",
    "conversation_turns": "request_id",
    "problems": "id",
    "exercises": "id",
    "exercise_attempts": "id",
    "mistake_records": "id",
    "question_selections": "id",
    "memory_corrections": "entry_key",
    "memory_exclusions": "entry_key",
}

PY_COURSE = "Python语言程序设计"
MATH_COURSE = "高等数学"
PAPER_PY_2425_FINAL = "py-2024-2025-s1-final"
PAPER_PY_2324_FINAL = "py-2023-2024-s1-final"
GATE_Q_A = "py-2024-2025-s1-final-choice-05"
GATE_Q_B = "py-2024-2025-s1-final-choice-06"

MATH_FINAL_IDS = [
    "math-bi-20250116-final",
    "math-aii-20250625-final",
    "math-up-2014-final",
    "math-up-2015-final",
    "math-up-2016-final",
    "math-up-2017-final",
    "math-up-2018-final",
]


# ----------------------------------------------------------------------
# 外部传输封锁
# ----------------------------------------------------------------------
class OfflineViolation(RuntimeError):
    """任何非回环网络目标在评测进程内都必须失败。"""


class OfflineGuard:
    """封锁 socket / DNS / urllib / http.client，并分开计数。

    - ``self_test_blocked``：guard 自证时的拦截次数（证明封锁生效）；
    - ``egress_attempts``：评测期间的越界尝试次数（必须为 0）。
    """

    def __init__(self) -> None:
        self.self_test_blocked = 0
        self.egress_attempts = 0
        self.attempt_log: list[dict[str, Any]] = []
        self._installed = False
        self._self_test = False
        self._saved: dict[str, Any] = {}

    @staticmethod
    def _allowed(host: Any) -> bool:
        text = str(host or "").strip().lower()
        return text in {"", "localhost", "127.0.0.1", "::1", "0.0.0.0", "testserver"}

    def _block(self, kind: str, target: Any) -> None:
        if self._self_test:
            self.self_test_blocked += 1
        else:
            self.egress_attempts += 1
        self.attempt_log.append(
            {
                "kind": kind,
                "target": str(target),
                "phase": "self_test" if self._self_test else "eval",
                "at": time.time(),
            }
        )
        raise OfflineViolation(f"F010-A offline: 已封锁外部传输 {kind} {target}")

    def install(self) -> None:
        if self._installed:
            return
        guard = self

        real_getaddrinfo = socket.getaddrinfo
        real_connect = socket.socket.connect
        real_create = socket.create_connection
        real_urlopen = urllib.request.urlopen
        real_http_connect = None
        try:
            import http.client as http_client

            real_http_connect = http_client.HTTPConnection.connect
        except Exception:  # pragma: no cover - 防御性
            http_client = None  # type: ignore[assignment]

        def getaddrinfo(host, *args, **kwargs):
            if not guard._allowed(host):
                guard._block("dns", host)
            return real_getaddrinfo(host, *args, **kwargs)

        def connect(self_socket, address):
            host = address[0] if isinstance(address, tuple) and address else address
            if not guard._allowed(host):
                guard._block("socket", address)
            return real_connect(self_socket, address)

        def create_connection(address, *args, **kwargs):
            host = address[0] if isinstance(address, tuple) and address else address
            if not guard._allowed(host):
                guard._block("connect", address)
            return real_create(address, *args, **kwargs)

        def urlopen(url, *args, **kwargs):
            guard._block("urllib", getattr(url, "full_url", url))

        self._saved = {
            "getaddrinfo": real_getaddrinfo,
            "connect": real_connect,
            "create_connection": real_create,
            "urlopen": real_urlopen,
            "http_connect": real_http_connect,
        }
        socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
        socket.socket.connect = connect  # type: ignore[assignment]
        socket.create_connection = create_connection  # type: ignore[assignment]
        urllib.request.urlopen = urlopen  # type: ignore[assignment]
        if real_http_connect is not None:
            http_client.HTTPConnection.connect = lambda self_conn: guard._block(  # type: ignore[assignment]
                "http", getattr(self_conn, "host", "?")
            )
        self._installed = True

    def self_test(self) -> dict[str, Any]:
        """自证：封锁生效时，连 api.deepseek.com 必须抛 OfflineViolation。"""

        self._self_test = True
        blocked = False
        detail = ""
        try:
            socket.create_connection(("api.deepseek.com", 443), timeout=2)
        except OfflineViolation as exc:
            blocked = True
            detail = str(exc)
        except Exception as exc:  # pragma: no cover - 不应发生
            detail = f"{type(exc).__name__}: {exc}"
        finally:
            self._self_test = False
        return {
            "blocked": blocked,
            "detail": detail,
            "self_test_blocked": self.self_test_blocked,
            "egress_attempts": self.egress_attempts,
        }


# ----------------------------------------------------------------------
# 观测：trace / 文字位置
# ----------------------------------------------------------------------
REAL_CALLS: dict[str, Any] = {"model_constructions": 0, "at": []}
PATH_AUDIT: dict[str, Any] = {"enabled": False, "writes": [], "sqlite": [], "temp": None, "env": {}}


def install_real_call_guard() -> None:
    """真实模型客户端只允许构造 0 次：构造即计数并拒绝。"""

    if REAL_CALLS.get("installed"):
        return
    import langchain_openai

    original = langchain_openai.ChatOpenAI.__init__

    def guarded(self: Any, *args: Any, **kwargs: Any) -> None:
        REAL_CALLS["model_constructions"] += 1
        REAL_CALLS["at"].append(round(time.time(), 3))
        raise RuntimeError("F010-A：禁止在离线评测中构造真实模型客户端")

    langchain_openai.ChatOpenAI.__init__ = guarded  # type: ignore[method-assign]
    REAL_CALLS["installed"] = True


def install_path_audit() -> None:
    """记录实际发生的写路径与 sqlite 连接，供隔离报告核对。"""

    if PATH_AUDIT.get("enabled"):
        return
    import builtins
    import sqlite3
    import tempfile

    PATH_AUDIT["enabled"] = True
    PATH_AUDIT["temp"] = str(tempfile.gettempdir())
    PATH_AUDIT["cwd"] = str(Path.cwd())

    real_connect = sqlite3.connect

    def audited_connect(database: Any, *args: Any, **kwargs: Any) -> Any:
        PATH_AUDIT["sqlite"].append(str(database))
        return real_connect(database, *args, **kwargs)

    sqlite3.connect = audited_connect  # type: ignore[assignment]

    real_open = builtins.open

    def audited_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            PATH_AUDIT["writes"].append(str(file))
        return real_open(file, mode, *args, **kwargs)

    builtins.open = audited_open  # type: ignore[assignment]


def path_report(run_dir: Path, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """把审计原始记录折叠成「是否越界」的结论。"""

    run_abs = run_dir.resolve()

    def inside(raw: str) -> bool:
        try:
            Path(raw).resolve().relative_to(run_abs)
            return True
        except (ValueError, OSError):
            return False

    from urllib.parse import unquote

    def normalize(raw: str) -> str:
        text = raw
        if text.startswith("file:"):
            text = unquote(text[5:]).lstrip("/")
            text = text.split("?")[0]
            text = str(Path(text))
            text = text if Path(text).is_absolute() else "/" + text
        return text

    writes = sorted(set(PATH_AUDIT.get("writes") or []))
    sqlite_paths = sorted(set(PATH_AUDIT.get("sqlite") or []))
    normalized = {item: normalize(item) for item in sqlite_paths}
    outside_writes = [item for item in writes if not inside(item)]
    outside_sqlite = [
        item
        for item in sqlite_paths
        if not inside(normalized[item])
        and ":memory:" not in item
        and not item.startswith("file::memory:")
    ]
    return {
        "run_dir": str(run_abs),
        "temp_dir": PATH_AUDIT.get("temp"),
        "cwd": PATH_AUDIT.get("cwd"),
        "env": {
            name: os.environ.get(name)
            for name in ("TEMP", "TMP", "HHU_STUDY_DB", "HHU_QB_ROOT", "HF_HOME", "XDG_CACHE_HOME")
        },
        "sqlite_resolved": normalized,
        "write_paths": writes[:60],
        "sqlite_paths": sqlite_paths[:60],
        "writes_outside_run": outside_writes[:20],
        "sqlite_outside_run_readonly_ok": outside_sqlite[:20],
        "confined": not outside_writes,
        **(extra or {}),
    }


class TraceWriter:
    """按 span 追加 JSONL；同时把关键文本交给门禁扫描。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self.span_seq = 0
        self.last_span_by_case: dict[str, str] = {}
        self.texts: dict[str, list[str]] = {
            "decision": [],
            "reply": [],
            "outbound": [],
            "tutor": [],
        }

    def span(
        self, case_id: str, kind: str, *, parent: str | None = None, **fields: Any
    ) -> str:
        self.span_seq += 1
        span_id = f"s{self.span_seq:05d}"
        record = {
            "span_id": span_id,
            "parent_span_id": (
                parent if parent is not None else self.last_span_by_case.get(case_id)
            ),
            "case_id": case_id,
            "kind": kind,
            "at": round(time.time(), 4),
            **fields,
        }
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        self.last_span_by_case[case_id] = span_id
        return span_id

    def collect(self, position: str, payload: Any) -> None:
        if payload is None:
            return
        text = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False, default=str
        )
        self.texts.setdefault(position, []).append(text)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._fh.close()


@dataclass
class TurnScript:
    """一轮的脚本：模型决策批次 + 正文片段。"""

    n: int
    utterance: str
    tools: list[list[dict[str, Any]]] = field(default_factory=list)
    reply_pieces: list[str] = field(default_factory=lambda: ["好的，我看一下。"])
    reply_fail_after: int | None = None
    reply_block_after: int | None = None
    reply_hang: float = 0.0
    request: dict[str, Any] = field(default_factory=dict)
    rebuild: bool = False


class ActiveCase:
    """当前用例的脚本与观测状态（替身从全局读它）。"""

    def __init__(self) -> None:
        self.case_id: str = ""
        self.turns: list[TurnScript] = []
        self.turn_index = 0
        self.batch_index = 0
        self.decision_calls = 0
        self.tool_batches: list[list[dict[str, Any]]] = []
        self.reply_calls = 0
        self.tool_overrun = 0
        self.decision_messages: list[list[Any]] = []
        self.reply_messages: list[list[Any]] = []
        self.tutor_calls: list[dict[str, Any]] = []
        self.exercise_calls: dict[str, list[dict[str, Any]]] = {
            "generate": [],
            "evaluate": [],
        }
        self.query_calls: list[str] = []
        self.release = threading.Event()
        self.first_delta = threading.Event()
        self.turn_started: dict[int, float] = {}
        self.turn_finished: dict[int, float] = {}
        self.markers: list[str] = []
        self.exercise_plans: dict[str, dict[str, Any]] = {}
        self.exercise_plan: dict[str, Any] = {}
        self.placeholders: dict[str, str] = {}
        self.trace: TraceWriter | None = None
        self.guard: OfflineGuard | None = None
        self.started_at = 0.0
        self.timeouts: dict[str, float] = {}

    @property
    def turn(self) -> TurnScript:
        return self.turns[self.turn_index]

    def begin_turn(self, index: int) -> None:
        self.turn_index = index
        self.batch_index = 0
        self.exercise_plan = self.exercise_plans.get(str(index), {}) or {}
        self.release.clear()
        self.first_delta.clear()
        self.turn_started[index] = time.perf_counter()

    def next_batch(self) -> list[dict[str, Any]] | None:
        if self.turn_index >= len(self.turns):
            return None
        tools = self.turns[self.turn_index].tools
        if self.batch_index >= len(tools):
            return None
        batch = copy.deepcopy(tools[self.batch_index])
        self.batch_index += 1
        if self.batch_index > 8:
            self.tool_overrun += 1
        if self.placeholders:
            for call in batch:
                args = call.get("args") or {}
                for key, value in list(args.items()):
                    if isinstance(value, str):
                        for placeholder, replacement in self.placeholders.items():
                            value = value.replace(placeholder, replacement)
                        args[key] = value
                call["args"] = args
        return batch

    def reply_spec(self) -> tuple[list[str], int | None, int | None, float]:
        turn = self.turn
        return (
            list(turn.reply_pieces),
            turn.reply_fail_after,
            turn.reply_block_after,
            turn.reply_hang,
        )


ACTIVE = ActiveCase()


STREAM_AUDIT: dict[str, Any] = {"installed": False, "streams": []}


async def _record_stream(inner: Any) -> Any:
    """包装产品返回的 SSE 生成器，记录服务端视角的结束方式。

    客户端断开时 ASGI 层会向生成器抛 GeneratorExit（或 CancelledError），这里把它记下来，
    用来区分「服务端已发完终态」与「服务端生成器被取消」。
    """

    record: dict[str, Any] = {
        "chunks": 0,
        "saw_result": False,
        "closed_reason": None,
        "completed": False,
        "finished_at": None,
    }
    STREAM_AUDIT["streams"].append(record)
    try:
        async for chunk in inner:
            record["chunks"] += 1
            text = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)
            if '"result"' in text:
                record["saw_result"] = True
            yield chunk
        record["completed"] = True
        record["closed_reason"] = "exhausted"
    except (GeneratorExit, asyncio.CancelledError) as exc:
        record["closed_reason"] = type(exc).__name__
        raise
    finally:
        record["finished_at"] = time.time()


def install_stream_audit() -> None:
    """把产品 SSE 端点的 StreamingResponse 换成带服务端审计的版本（只改本进程内的引用）。"""

    if STREAM_AUDIT["installed"]:
        return
    from fastapi.responses import StreamingResponse as _Base

    from app.api import conversations as conv

    class AuditedStreamingResponse(_Base):
        def __init__(self, content: Any, *args: Any, **kwargs: Any) -> None:
            if hasattr(content, "__aiter__"):
                content = _record_stream(content)
            super().__init__(content, *args, **kwargs)

    AuditedStreamingResponse.__name__ = "StreamingResponse"
    conv.StreamingResponse = AuditedStreamingResponse
    STREAM_AUDIT["installed"] = True


UVICORN: dict[str, Any] = {"base": None, "server": None}


def ensure_local_server() -> str:
    """在本进程内起一个真实的 uvicorn 服务，用来观察真实 TCP 断连。"""

    if UVICORN["base"]:
        return UVICORN["base"]
    import socket
    import threading

    import uvicorn

    from app.main import app

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="f010a-uvicorn")
    thread.start()
    deadline = time.perf_counter() + 30.0
    while not server.started and time.perf_counter() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("F010-A：本地 uvicorn 未能启动")
    UVICORN.update({"base": f"http://127.0.0.1:{port}", "server": server})
    return UVICORN["base"]


def last_stream_record() -> dict[str, Any]:
    return STREAM_AUDIT["streams"][-1] if STREAM_AUDIT["streams"] else {}


def _blocking_wait() -> Any:
    """正文替身的人为阻塞：跨线程轮询，最长 8 秒。"""

    async def _wait() -> None:
        deadline = time.perf_counter() + 8.0
        while not ACTIVE.release.is_set() and time.perf_counter() < deadline:
            await asyncio.sleep(0.02)

    return _wait()


# ----------------------------------------------------------------------
# 模型替身（只替换“模型”，业务仍走真实代码）
# ----------------------------------------------------------------------
class _FakeMessage:
    def __init__(self, content: str = "", tool_calls: list[dict] | None = None):
        self.content = content
        self.tool_calls = list(tool_calls or [])
        self.response_metadata = {
            "model_name": "f010a-offline-stub",
            "finish_reason": "tool_calls" if self.tool_calls else "stop",
        }
        self.usage_metadata = {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
        }


class ScriptedToolModel:
    """按用例脚本返回原生 tool_calls；脚本用尽表示模型主动收尾。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.bound_tools: list[dict[str, Any]] | None = None

    def bind_tools(self, tools: list[dict[str, Any]], **kwargs: Any) -> "ScriptedToolModel":
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> _FakeMessage:
        ACTIVE.decision_calls += 1
        snapshot = copy.deepcopy(messages)
        ACTIVE.decision_messages.append(snapshot)
        if ACTIVE.trace is not None:
            ACTIVE.trace.collect("decision", snapshot)
        batch = ACTIVE.next_batch()
        if batch is None:
            ACTIVE.tool_batches.append([])
            return _FakeMessage(content="")
        ACTIVE.tool_batches.append(
            [{"name": call.get("name"), "args": call.get("args") or {}} for call in batch]
        )
        return _FakeMessage(content="", tool_calls=batch)


class ScriptedReplyModel:
    """按用例脚本流式输出正文；可失败、可阻塞（用于断流）。"""

    async def astream(self, messages: list[Any], **kwargs: Any):
        ACTIVE.reply_calls += 1
        snapshot = copy.deepcopy(messages)
        ACTIVE.reply_messages.append(snapshot)
        if ACTIVE.trace is not None:
            ACTIVE.trace.collect("reply", snapshot)
        pieces, fail_after, block_after, hang = ACTIVE.reply_spec()
        if hang:
            await asyncio.sleep(hang)
        for index, piece in enumerate(pieces):
            if block_after is not None and index >= block_after:
                await _blocking_wait()
            if fail_after is not None and index >= fail_after:
                raise RuntimeError("供应商流中断")
            ACTIVE.first_delta.set()
            yield _FakeMessage(content=piece)


class ScriptedUnderstander:
    """旧查询入口的替身（本批不触发真实查询模型）。"""

    async def __call__(self, query: str) -> Any:
        ACTIVE.query_calls.append(query)
        raise AssertionError("F010-A：本批不测试旧查询理解入口")


class ScriptedTutor:
    """辅导替身：真实参数被记录，供门禁与材料断言。"""

    def __init__(self) -> None:
        from app.tutoring.models import (
            Explanation,
            FollowupAnswer,
            KnowledgePoint,
            ProblemUnderstanding,
            Step,
        )

        self._Explanation = Explanation
        self._FollowupAnswer = FollowupAnswer
        self._KnowledgePoint = KnowledgePoint
        self._ProblemUnderstanding = ProblemUnderstanding
        self._Step = Step

    async def understand_problem(self, text: str, images: list[Any]) -> Any:
        ACTIVE.tutor_calls.append({"call": "understand", "text": text})
        return self._ProblemUnderstanding(
            statement=(text or "（无题干）")[:4000], clarity="clear"
        )

    async def explain(self, **kwargs: Any) -> Any:
        ACTIVE.tutor_calls.append({"call": "explain"})
        if ACTIVE.trace is not None:
            ACTIVE.trace.collect("tutor", kwargs)
        return self._Explanation(
            knowledge_points=[
                self._KnowledgePoint(name="循环与条件", explanation="本题核心考点")
            ],
            steps=[self._Step(index=1, title="读题", content="先明确问的是什么。")],
            final_answer="答案见讲解步骤。",
            error_analysis=None,
        )

    async def answer_followup(self, **kwargs: Any) -> Any:
        ACTIVE.tutor_calls.append({"call": "followup"})
        if ACTIVE.trace is not None:
            ACTIVE.trace.collect("tutor", kwargs)
        return self._FollowupAnswer(
            answer="这一步先想清楚判断条件。",
            related_steps=[1],
        )


class ScriptedExerciseAgent:
    """练习生成/评价替身；参考解答可在用例里换成带标记的合成值。"""

    async def generate_exercise(self, **kwargs: Any) -> Any:
        from app.tutoring.exercise_llm import GeneratedExercise

        plan = ACTIVE.exercise_plan or {}
        ACTIVE.exercise_calls["generate"].append(kwargs)
        statement = plan.get("statement") or "类似练习：读程序写出输出。"
        if plan.get("duplicate"):
            recent = kwargs.get("recent_statements") or []
            statement = recent[0] if recent else kwargs.get("original_statement") or statement
        return GeneratedExercise(
            statement=statement,
            knowledge_point=plan.get("knowledge_point") or "循环与条件",
            reference_solution=plan.get("reference")
            or "参考解答：先判断循环边界，再逐轮累加。",
        )

    async def evaluate_answer(self, **kwargs: Any) -> Any:
        from app.tutoring.exercise_llm import AnswerEvaluation

        ACTIVE.exercise_calls["evaluate"].append(kwargs)
        return AnswerEvaluation(
            is_correct=True,
            final_answer_assessment="答案正确。",
            step_by_step_analysis="步骤清楚。",
            guidance="继续练习同类题。",
            needs_supplement=False,
        )


# ----------------------------------------------------------------------
# 题库运行时替换
# ----------------------------------------------------------------------
class NoCloseRuntime:
    """共享真实题库运行时：只读，禁止被 lifespan 关闭。"""

    def __init__(self, base: Any) -> None:
        self._base = base

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def close(self) -> None:
        return None


class FaultRuntime(NoCloseRuntime):
    """注入题库故障：能力与检索都返回“暂时不可用”。"""

    def capability(self, course: str) -> Any:
        from app.questions.models import QuestionCapabilityView

        return QuestionCapabilityView(
            course=course or PY_COURSE,
            status="error",
            message="题目库暂时不可用，请稍后重试；仍可查看或下载原卷。",
        )

    def search(self, request: Any, **kwargs: Any) -> Any:
        from app.questions.models import QuestionSearchResult

        return QuestionSearchResult(
            status="bank_unavailable",
            message="题目库暂时不可用，请稍后重试；仍可查看或下载原卷。",
        )


class GateRuntime(NoCloseRuntime):
    """答案门禁：把指定题的答案文本换成唯一合成标记。

    只替换 ``evidence()/answer_view()`` 的答案文本；卡片字段、内容哈希、
    来源、页码保持真实，避免破坏产品侧的版本校验。
    """

    def __init__(self, base: Any, markers: dict[tuple[str, str], str]) -> None:
        super().__init__(base)
        self._markers = dict(markers)

    def _marked(self, question_id: str, build_id: str | None, text: str | None) -> str | None:
        marker = self._markers.get((question_id, build_id or ""))
        if marker is None:
            return text
        return marker

    def evidence(self, selection: Any) -> Any:
        bundle = self._base.evidence(selection)
        marker = self._markers.get((selection.question_id, selection.build_id or ""))
        if marker is None or getattr(bundle, "answer_detail", None) is None:
            return bundle
        detail = bundle.answer_detail.model_copy(update={"text": marker})
        return bundle.model_copy(update={"answer_detail": detail})

    def answer_view(self, selection: Any) -> Any:
        card, bundle = self._base.answer_view(selection)
        marker = self._markers.get((selection.question_id, selection.build_id or ""))
        if marker is None:
            return card, bundle
        if card.answer_detail is not None:
            card = card.model_copy(
                update={"answer_detail": card.answer_detail.model_copy(update={"text": marker})}
            )
        if getattr(bundle, "answer_detail", None) is not None:
            bundle = dataclasses.replace(
                bundle,
                answer_detail=bundle.answer_detail.model_copy(update={"text": marker}),
            )
        return card, bundle


_SHARED: dict[str, Any] = {"runtime": None}


def shared_runtime() -> Any:
    """整个评测进程共用一个只读真实题库运行时。"""

    if _SHARED["runtime"] is None:
        from app.questions.runtime import build_question_runtime

        _SHARED["runtime"] = NoCloseRuntime(build_question_runtime())
    return _SHARED["runtime"]


def marker_for(run_id: str, question_id: str, build_id: str) -> str:
    return f"F010A-GATE-{run_id}-{question_id}-{(build_id or '')[:8]}"


def build_case_runtime(run_id: str, spec: dict[str, Any]) -> Any:
    fixture = spec.get("fixture", "real")
    if fixture == "fault":
        return FaultRuntime(shared_runtime())
    if fixture == "fake":
        from tests.question_fakes import FakeQuestionRuntime, sample_card

        card_spec = spec.get("fake_card") or {}
        card = sample_card(
            question_id=card_spec.get("question_id", "fx-29-choice-1"),
            number=card_spec.get("number", "1"),
            statement=card_spec.get("statement", "合成题：下列表达式的值是？"),
        )
        return FakeQuestionRuntime(
            [card], answer_text=spec.get("fake_answer", "F010A-ANSWER")
        )
    markers = {
        (item["question_id"], item["build"]): marker_for(run_id, item["question_id"], item["build"])
        for item in spec.get("gate_questions", [])
    }
    base = shared_runtime()
    if markers:
        return GateRuntime(base, markers)
    return base


# ----------------------------------------------------------------------
# 正式数据哈希与目录清单
# ----------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_listing(root: Path, *, limit: int = 4000) -> dict[str, Any]:
    files: list[str] = []
    total = 0
    for entry in root.rglob("*"):
        if entry.is_file():
            files.append(str(entry.relative_to(root)).replace("\\", "/"))
            total += 1
            if total >= limit:
                break
    files.sort()
    return {"count": total, "files": files}


def snapshot_formal() -> dict[str, Any]:
    result: dict[str, Any] = {"files": {}, "root": tree_listing(PROJECT_ROOT)}
    for path in FORMAL_WATCH_FILES:
        result["files"][str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")] = (
            sha256_file(path) if path.exists() else None
        )
    bank = FORMAL_QB
    result["bank_key_files"] = {
        name: sha256_file(bank / name) if (bank / name).exists() else None
        for name in BANK_KEY_FILES
    }
    result["r001_key_files"] = {
        name: sha256_file(R001_QB / name) if (R001_QB / name).exists() else None
        for name in BANK_KEY_FILES
    }
    result["r001_mtime"] = (
        max((path.stat().st_mtime for path in R001_QB.rglob("*") if path.is_file()), default=None)
        if R001_QB.exists()
        else None
    )
    return result


def git_status() -> str:
    completed = subprocess.run(
        ["git", "-c", "safe.directory=D:/实习/河海大学考试复习", "status", "--porcelain", "-uall"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (completed.stdout or "") + (completed.stderr or "")


# ----------------------------------------------------------------------
# 运行目录、题库与目录副本
# ----------------------------------------------------------------------
def prepare_run_dir(run_id: str) -> Path:
    if not run_id or "/" in run_id or "\\" in run_id:
        raise SystemExit("run-id 必须是单段目录名")
    target = RUN_ROOT / run_id
    if target.exists():
        raise SystemExit(f"运行目录已存在，拒绝覆盖历史证据：{target}")
    target.mkdir(parents=True)
    return target


def prepare_bank(run_dir: Path) -> Path:
    """从 R001 一致性副本读取，再复制到本次目录。

    索引锁、模型缓存都落在本次副本，R001 副本保持只读。
    """

    if not R001_QB.exists():
        raise SystemExit(f"缺少 R001 题库副本：{R001_QB}")
    for name in BANK_KEY_FILES:
        source = R001_QB / name
        formal = FORMAL_QB / name
        if not source.exists():
            raise SystemExit(f"R001 副本缺少 {name}")
        if sha256_file(source) != sha256_file(formal):
            raise SystemExit(f"R001 副本与正式题库不一致：{name}")
    target = run_dir / "qb"
    shutil.copytree(
        R001_QB,
        target,
        ignore=shutil.ignore_patterns(".lock", "*.lock", "*.pid"),
    )
    return target


def prepare_papers(run_dir: Path) -> Path:
    target = run_dir / "papers"
    shutil.copytree(FORMAL_PAPERS, target)
    return target


AB_PAPERS: list[dict[str, Any]] = [
    {
        "id": "fx-py-2024-2025-s1-final-a",
        "title": "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）及答案",
        "course": PY_COURSE,
        "academic_year": "2024-2025",
        "exam_type": "final",
        "exam_date": "2025-01-10",
        "file_path": "Python/fx-a.pdf",
    },
    {
        "id": "fx-py-2024-2025-s1-final-b",
        "title": "2024-2025学年第一学期《Python语言程序设计》期末试卷（B卷）及答案",
        "course": PY_COURSE,
        "academic_year": "2024-2025",
        "exam_type": "final",
        "exam_date": "2025-01-10",
        "file_path": "Python/fx-b.pdf",
    },
    {
        "id": "fx-py-2023-2024-s1-final-a",
        "title": "2023-2024学年第一学期《Python语言程序设计》期末试卷（A卷）及答案",
        "course": PY_COURSE,
        "academic_year": "2023-2024",
        "exam_type": "final",
        "exam_date": "2024-01-11",
        "file_path": "Python/fx-a.pdf",
    },
    {
        "id": "fx-py-2023-2024-s1-final-b",
        "title": "2023-2024学年第一学期《Python语言程序设计》期末试卷（B卷）及答案",
        "course": PY_COURSE,
        "academic_year": "2023-2024",
        "exam_type": "final",
        "exam_date": "2024-01-11",
        "file_path": "Python/fx-b.pdf",
    },
    {
        "id": "fx-py-2024-2025-s1-midterm-b",
        "title": "2024-2025学年第一学期《Python语言程序设计》期中试卷（B卷）及答案",
        "course": PY_COURSE,
        "academic_year": "2024-2025",
        "exam_type": "midterm",
        "exam_date": "2024-11-08",
        "file_path": "Python/fx-b.pdf",
    },
    {
        "id": "fx-py-2023-2024-s1-midterm-b",
        "title": "2023-2024学年第一学期《Python语言程序设计》期中试卷（B卷）及答案",
        "course": PY_COURSE,
        "academic_year": "2023-2024",
        "exam_type": "midterm",
        "exam_date": "2023-11-10",
        "file_path": "Python/fx-b.pdf",
    },
    {
        "id": "fx-math-2023-2024-s2-final",
        "title": "2023-2024学年第二学期《高等数学BⅠ》期末试题及答案",
        "course": MATH_COURSE,
        "academic_year": "2023-2024",
        "exam_type": "final",
        "exam_date": "2024-06-20",
        "file_path": "高等数学/fx-math.pdf",
    },
]


def prepare_ab_papers(run_dir: Path) -> Path:
    root = run_dir / "papers-ab"
    (root / "Python").mkdir(parents=True, exist_ok=True)
    (root / "高等数学").mkdir(parents=True, exist_ok=True)
    (root / "Python" / "fx-a.pdf").write_bytes(b"%PDF-1.4\n% f010a fixture A\n")
    (root / "Python" / "fx-b.pdf").write_bytes(b"%PDF-1.4\n% f010a fixture B\n")
    (root / "高等数学" / "fx-math.pdf").write_bytes(b"%PDF-1.4\n% f010a fixture math\n")
    (root / "catalog.json").write_text(
        json.dumps(AB_PAPERS, ensure_ascii=False), encoding="utf-8"
    )
    return root


# ----------------------------------------------------------------------
# 学习库读写证据
# ----------------------------------------------------------------------
def db_rows(db_path: Path, table: str, key_column: str | None = None) -> list[dict[str, Any]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
    finally:
        connection.close()
    return rows


def db_keys(db_path: Path, table: str) -> set[str]:
    column = WRITE_TABLES.get(table, "id")
    return {str(row.get(column)) for row in db_rows(db_path, table) if row.get(column)}


def db_count(db_path: Path, table: str) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def db_write_snapshot(db_path: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for table, column in WRITE_TABLES.items():
        rows = db_rows(db_path, table)
        snapshot[table] = {
            "count": len(rows),
            "keys": sorted(str(row.get(column)) for row in rows if row.get(column)),
        }
    return snapshot


def db_turn(db_path: Path, conversation_id: str, request_id: str) -> dict[str, Any] | None:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM conversation_turns WHERE conversation_id=? AND request_id=?",
            (conversation_id, request_id),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


# ----------------------------------------------------------------------
# 适配器安装
# ----------------------------------------------------------------------
_ORIGINAL_ADAPTERS: dict[str, Any] = {}
_adapters_installed = False


def install_offline_config() -> None:
    """固定配置来源：不读本地 .env，真实密钥不出现在进程里。"""

    import app.config as query_config

    query_config.ENV_FILE = RUN_ROOT / "absent.env"
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        os.environ.pop(name, None)
    os.environ["DEEPSEEK_API_KEY"] = "f010a-offline-dummy"
    os.environ["DEEPSEEK_BASE_URL"] = "http://127.0.0.1:9"
    os.environ["DEEPSEEK_MODEL"] = "f010a-offline-stub"


def install_adapters() -> None:
    """把六个模型适配器换成脚本替身；产品装配路径保持不变。"""

    global _adapters_installed
    if _adapters_installed:
        return
    import app.api.conversations as api

    _ORIGINAL_ADAPTERS.update(
        {
            "SchedulerAgent": api.SchedulerAgent,
            "StreamingReply": api.StreamingReply,
            "DeepSeekTutor": api.DeepSeekTutor,
            "DeepSeekExerciseAgent": api.DeepSeekExerciseAgent,
            "DeepSeekUnderstanding": api.DeepSeekUnderstanding,
            "DeepSeekIntentRouter": api.DeepSeekIntentRouter,
        }
    )
    real_scheduler = api.SchedulerAgent
    real_reply = api.StreamingReply

    api.SchedulerAgent = lambda settings=None, **kw: real_scheduler(  # type: ignore[assignment]
        model=ScriptedToolModel()
    )
    api.StreamingReply = lambda settings=None, **kw: real_reply(  # type: ignore[assignment]
        model=ScriptedReplyModel()
    )
    api.DeepSeekTutor = lambda settings=None, **kw: ScriptedTutor()  # type: ignore[assignment]
    api.DeepSeekExerciseAgent = lambda settings=None, **kw: ScriptedExerciseAgent()  # type: ignore[assignment]
    api.DeepSeekUnderstanding = lambda settings=None, **kw: ScriptedUnderstander()  # type: ignore[assignment]
    api.DeepSeekIntentRouter = lambda settings=None, **kw: ScriptedUnderstander()  # type: ignore[assignment]
    _adapters_installed = True


def exercise_agent() -> ScriptedExerciseAgent:
    """当前用例的练习替身（每轮新建，配置挂在 ACTIVE 上）。"""

    agent = ScriptedExerciseAgent()
    agent.configure(getattr(ACTIVE, "exercise_plan", {}) or {})
    return agent


# ----------------------------------------------------------------------
# 用例上下文
# ----------------------------------------------------------------------
class CaseContext:
    """一个用例的隔离环境：目录、客户端、重建、HTTP 驱动与证据收集。"""

    def __init__(self, run_dir: Path, case: dict[str, Any], run: Any) -> None:
        self.run = run
        self.case = case
        self.case_id = case["id"]
        self.run_dir = run_dir
        self.case_dir = run_dir / "cases" / self.case_id
        (self.case_dir / "attachments").mkdir(parents=True, exist_ok=True)
        self.db_path = self.case_dir / "study.sqlite3"
        self.attachments = self.case_dir / "attachments"
        self.fixture = case.get("fixture", "real")
        self.client: Any = None
        self.conversation_id: str | None = None
        self.events: list[dict[str, Any]] = []
        self.raw_sse: list[str] = []
        self.turn_results: list[dict[str, Any]] = []
        self.http_calls: list[dict[str, Any]] = []
        self.blocks: list[str] = []
        self.notes: list[str] = []
        self.materials: list[str] = []
        self.tool_calls: list[list[dict[str, Any]]] = []
        self.decisions: list[str] = []
        self.concurrent: list[dict[str, Any]] = []
        self.observed: dict[str, Any] = {}
        self.seed: Any = None
        self.memory: Any = None
        self._client_stack: contextlib.ExitStack | None = None
        self.before = db_write_snapshot(self.db_path) if self.db_path.exists() else None

    # -- 装配 ---------------------------------------------------------
    @property
    def papers_root(self) -> Path:
        if self.fixture == "ab":
            return self.run_dir / "papers-ab"
        return self.run_dir / "papers"

    def _enter_client(self) -> None:
        from fastapi.testclient import TestClient

        import app.main as main_module
        from app import images
        from app.main import app
        from app.papers.repository import PaperRepository
        from app.papers.service import PaperService, get_paper_service

        os.environ["HHU_STUDY_DB"] = str(self.db_path)
        images.ATTACHMENTS_ROOT = self.attachments
        app.dependency_overrides[get_paper_service] = lambda: PaperService(
            PaperRepository(self.papers_root)
        )
        runtime = build_case_runtime(self.run.run_id, self.case)
        self._runtime = runtime
        main_module.build_question_runtime = lambda: runtime
        self._client_stack = contextlib.ExitStack()
        self.client = self._client_stack.enter_context(TestClient(app))

    def start(self) -> None:
        self._enter_client()

    def rebuild(self, label: str = "rebuild") -> None:
        """重建服务（同库、同目录）：验证启动恢复与持久化，不重放模型。"""

        self.close_client()
        before = db_write_snapshot(self.db_path)
        self._enter_client()
        after = db_write_snapshot(self.db_path)
        if self.run.trace is not None:
            self.run.trace.span(
                self.case_id,
                "rebuild",
                label=label,
                writes_before=before,
                writes_after=after,
            )

    def close_client(self) -> None:
        if self._client_stack is not None:
            self._client_stack.close()
            self._client_stack = None
            self.client = None

    def stop(self) -> None:
        self.close_client()
        from app.main import app

        app.dependency_overrides.clear()

    # -- HTTP ---------------------------------------------------------
    def _record_http(self, method: str, path: str, status: int, body: Any) -> None:
        text = body if isinstance(body, str) else json.dumps(
            body, ensure_ascii=False, default=str
        )
        entry = {"method": method, "path": path, "status": status, "body": text[:400]}
        self.http_calls.append(entry)
        if self.run.trace is not None:
            self.run.trace.collect("outbound", text)
            self.run.trace.span(self.case_id, "http", method=method, path=path, status=status)
        if self.run.manifest["http"].get(path) is None:
            self.run.manifest["http"][path] = status

    def create_conversation(self) -> str:
        response = self.client.post("/api/conversations", json={})
        self._record_http("POST", "/api/conversations", response.status_code, response.text)
        if response.status_code != 201:
            raise AssertionError(f"创建会话失败：{response.status_code} {response.text}")
        self.conversation_id = response.json()["id"]
        return self.conversation_id

    def post_turn(
        self,
        text: str,
        *,
        request_id: str | None = None,
        kind: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        extra = dict(extra or {})
        retry_of = extra.pop("retry_of", None)
        if retry_of:
            body: dict[str, Any] = {
                "request_id": request_id,
                "retry_of": retry_of,
            }
        else:
            body = {"request_id": request_id, "query": text, "kind": kind or "auto"}
            body.update(extra)
        reply_before = len(ACTIVE.reply_messages)
        batches_before = len(ACTIVE.tool_batches)
        writes_before = db_write_snapshot(self.db_path)
        start = time.perf_counter()
        with self.client.stream(
            "POST", f"/api/conversations/{self.conversation_id}/turns", json=body
        ) as response:
            raw = "".join(response.iter_text())
            status = response.status_code
        elapsed = round(time.perf_counter() - start, 3)
        events = parse_sse(raw)
        if status >= 400:
            print(f"[turn {status}] {raw[:800]}", file=sys.stderr)
        self.raw_sse.append(raw)
        self.events.extend(events)
        self._record_http("POST", "/turns", status, raw)
        result = {
            "turn": len(self.turn_results) + 1,
            "request_id": request_id,
            "status": status,
            "events": events,
            "types": [event.get("type") for event in events],
            "seconds": elapsed,
            "raw": raw,
        }
        self.turn_results.append(result)
        self.materials.append(reply_text_since(reply_before) if status == 200 else "")
        batches = ACTIVE.tool_batches[batches_before:]
        self.tool_calls.append([call for batch in batches for call in batch])
        turn_span = None
        if self.run.trace is not None:
            turn_span = self.run.trace.span(
                self.case_id,
                "turn",
                step=result["turn"],
                request_id=request_id,
                status=status,
                sse_types=result["types"],
                seconds=elapsed,
                text=text,
            )
        self.phase_spans(result, batches, writes_before, parent=turn_span)
        return result

    def phase_spans(
        self,
        result: dict[str, Any],
        batches: list[list[dict[str, Any]]],
        writes_before: Any,
        *,
        parent: str | None = None,
    ) -> None:
        """给一轮挂上决策、模型提议、执行结果、正文、写入快照五类 span，父节点都是本轮 turn span。

        `model_proposal` 是模型提议的调用（不代表执行成功）；`tool_result` 是产品实际执行结果；
        `write_snapshot` 是轮前轮后的库内写计数快照。
        """

        trace = getattr(self.run, "trace", None)
        if trace is None:
            return
        turn_span = parent
        if not turn_span:
            return
        trace.span(
            self.case_id,
            "decision",
            parent=turn_span,
            step=result["turn"],
            batches=[
                [call.get("name") for call in batch] for batch in batches
            ],
            decision_calls=len(batches),
        )
        for batch in batches:
            for call in batch:
                trace.span(
                    self.case_id,
                    "model_proposal",
                    parent=turn_span,
                    step=result["turn"],
                    name=call.get("name"),
                    args=call.get("args") or {},
                )
        terminal = (result["events"][-1] if result.get("events") else {}) or {}
        for item in terminal.get("composite_results") or []:
            trace.span(
                self.case_id,
                "tool_result",
                parent=turn_span,
                step=result["turn"],
                action=item.get("action"),
                status=item.get("status"),
            )
        trace.span(
            self.case_id,
            "reply",
            parent=turn_span,
            step=result["turn"],
            deltas=sum(1 for event in result["events"] if event.get("type") == "message_delta"),
            terminal=(result["events"][-1].get("type") if result["events"] else None),
            closed_early=result.get("closed_early"),
        )
        writes_after = db_write_snapshot(self.db_path)
        trace.span(
            self.case_id,
            "write_snapshot",
            parent=turn_span,
            step=result["turn"],
            writes_before=writes_before,
            writes_after=writes_after,
        )

    def outbound_text(self, n: int) -> str:
        """第 n 轮真实出站的原始 SSE 文本。"""

        if not self.raw_sse or n < 1 or n > len(self.raw_sse):
            return ""
        return self.raw_sse[n - 1]

    def stream_turn_until(
        self,
        text: str,
        predicate: Callable[[list[dict[str, Any]]], bool],
        *,
        request_id: str | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """读取 SSE 直到 predicate 成立或超时，然后关闭连接（制造断流）。"""

        request_id = request_id or str(uuid.uuid4())
        body = {"request_id": request_id, "query": text, "kind": "auto"}
        buffer = ""
        events: list[dict[str, Any]] = []
        deadline = time.perf_counter() + timeout
        closed_early = False
        with self.client.stream(
            "POST", f"/api/conversations/{self.conversation_id}/turns", json=body
        ) as response:
            for chunk in response.iter_text():
                buffer += chunk
                events = parse_sse(buffer)
                if predicate(events) or time.perf_counter() > deadline:
                    closed_early = True
                    break
        self.raw_sse.append(buffer)
        self.events.extend(events)
        self._record_http("POST", "/turns(stream)", 200, buffer)
        result = {
            "turn": len(self.turn_results) + 1,
            "request_id": request_id,
            "status": 200,
            "events": events,
            "types": [event.get("type") for event in events],
            "closed_early": closed_early,
            "server_stream": dict(last_stream_record()),
            "raw": buffer,
        }
        self.turn_results.append(result)
        return result

    def stream_turn_over_tcp(
        self,
        text: str,
        predicate: Callable[[list[dict[str, Any]]], bool],
        *,
        request_id: str | None = None,
        timeout: float = 30.0,
        settle: float = 2.0,
    ) -> dict[str, Any]:
        """经真实 TCP 读 SSE，读到 predicate 成立就断开连接，让服务端真的看到断连。"""

        import httpx

        base = ensure_local_server()
        request_id = request_id or str(uuid.uuid4())
        body = {"request_id": request_id, "query": text, "kind": "auto"}
        buffer = ""
        events: list[dict[str, Any]] = []
        closed_early = False
        started = time.perf_counter()
        first_delta_seconds: float | None = None
        url = f"{base}/api/conversations/{self.conversation_id}/turns"
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            with client.stream("POST", url, json=body) as response:
                for chunk in response.iter_text():
                    buffer += chunk
                    events = parse_sse(buffer)
                    if first_delta_seconds is None and any(
                        event.get("type") == "message_delta" for event in events
                    ):
                        first_delta_seconds = round(time.perf_counter() - started, 2)
                    if predicate(events):
                        closed_early = True
                        break
        time.sleep(settle)
        self.raw_sse.append(buffer)
        self.events.extend(events)
        self._record_http("POST", "/turns(tcp-stream)", 200, buffer)
        result = {
            "turn": len(self.turn_results) + 1,
            "request_id": request_id,
            "status": 200,
            "events": events,
            "types": [event.get("type") for event in events],
            "closed_early": closed_early,
            "transport": "uvicorn+tcp",
            "first_delta_seconds": first_delta_seconds,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "server_stream": dict(last_stream_record()),
            "raw": buffer,
        }
        self.turn_results.append(result)
        return result

    def get_detail(self) -> dict[str, Any]:
        response = self.client.get(f"/api/conversations/{self.conversation_id}")
        self._record_http("GET", "/conversations/{id}", response.status_code, response.text)
        return response.json()

    def get_answer(self, question_id: str, build: str) -> dict[str, Any]:
        response = self.client.get(
            f"/api/conversations/{self.conversation_id}/questions/{question_id}/answer",
            params={"build": build},
        )
        self._record_http("GET", "/questions/{id}/answer", response.status_code, response.text)
        entry = {
            "question_id": question_id,
            "build": build,
            "status": response.status_code,
            "body": response.text,
        }
        self.observed.setdefault("answers", []).append(entry)
        return {"status": response.status_code, "body": response.text}

    def get_source(self, question_id: str, build: str, kind: str = "statement", page: int = 1) -> dict[str, Any]:
        response = self.client.get(
            f"/api/conversations/{self.conversation_id}/questions/{question_id}/source",
            params={"build": build, "kind": kind, "page": page},
        )
        self._record_http("GET", "/questions/{id}/source", response.status_code, response.text)
        return {
            "status": response.status_code,
            "bytes": response.content,
            "content_type": response.headers.get("content-type", ""),
        }

    def download(self, paper_id: str) -> dict[str, Any]:
        response = self.client.get(f"/api/papers/{paper_id}/download")
        self._record_http("GET", "/papers/{id}/download", response.status_code, response.text[:0])
        return {"status": response.status_code, "bytes": response.content}

    def list_papers(self, course: str | None = None, exam_type: str | None = None) -> dict[str, Any]:
        params = {}
        if course:
            params["course"] = course
        if exam_type:
            params["exam_type"] = exam_type
        response = self.client.get("/api/papers", params=params)
        self._record_http("GET", "/papers", response.status_code, response.text)
        return response.json()

    def memory_get(self, course: str | None = None, days: int = 30, limit: int = 50) -> dict[str, Any]:
        params: dict[str, Any] = {"days": days, "limit": limit}
        if course:
            params["course"] = course
        response = self.client.get("/api/learning-memory", params=params)
        self._record_http("GET", "/learning-memory", response.status_code, response.text)
        return response.json()

    def memory_patch(self, kind: str, source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.patch(
            f"/api/learning-memory/{kind}/{source_id}", json=payload
        )
        self._record_http("PATCH", "/learning-memory", response.status_code, response.text)
        return {"status": response.status_code, "body": response.json()}

    def memory_delete(self, kind: str, source_id: str, reason: str = "") -> dict[str, Any]:
        response = self.client.delete(
            f"/api/learning-memory/{kind}/{source_id}", params={"reason": reason}
        )
        self._record_http("DELETE", "/learning-memory", response.status_code, response.text)
        return {"status": response.status_code, "body": response.json()}

    def post_reply(self, origin_request_id: str, *, request_id: str | None = None) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        response = self.client.post(
            f"/api/conversations/{self.conversation_id}/turns/{origin_request_id}/reply",
            json={"request_id": request_id},
        )
        self._record_http("POST", "/turns/{rid}/reply", response.status_code, response.text)
        return {"status": response.status_code, "body": response.text}

    # -- 证据 ---------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return db_write_snapshot(self.db_path)

    def row_delta(self, table: str) -> int:
        before = (self.before or {}).get(table, {}).get("count", 0)
        return db_count(self.db_path, table) - before

    def keys(self, table: str) -> set[str]:
        return db_keys(self.db_path, table)

    def turn_row(self, request_id: str) -> dict[str, Any] | None:
        return db_turn(self.db_path, self.conversation_id or "", request_id)


def parse_sse(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for frame in raw.split("\n\n"):
        frame = frame.strip()
        if not frame or not frame.startswith("data: "):
            continue
        try:
            events.append(json.loads(frame[len("data: "):]))
        except json.JSONDecodeError:
            continue
    return events


def last_result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("type") == "result":
            return event
    return None


def first_error(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in events:
        if event.get("type") == "error":
            return event
    return None


def reply_text(events: list[dict[str, Any]]) -> str:
    pieces = [event.get("delta", "") for event in events if event.get("type") == "message_delta"]
    return "".join(pieces)


# ----------------------------------------------------------------------
# 门禁扫描
# ----------------------------------------------------------------------
def gate_scan(
    markers: Iterable[str],
    extra_positions: dict[str, list[str]] | None = None,
    *,
    outbound: Iterable[str] = (),
) -> dict[str, Any]:
    """检查标记出现在哪一层：decision / reply / outbound / tutor。"""

    positions: dict[str, list[str]] = {}
    texts_all = list(outbound)
    if texts_all:
        hits = [marker for marker in markers if any(marker in text for text in texts_all)]
        if hits:
            positions["outbound_actual"] = sorted(set(hits))
    for position, texts in (ACTIVE.trace.texts if ACTIVE.trace else {}).items():
        hits = [marker for marker in markers if any(marker in text for text in texts)]
        if hits:
            positions[position] = sorted(set(hits))
    for position, texts in (extra_positions or {}).items():
        hits = [marker for marker in markers if any(marker in text for text in texts)]
        if hits:
            positions[position] = sorted(set(hits))
    return positions


def find_in_texts(texts: Iterable[str], needles: Iterable[str]) -> list[str]:
    joined = "\n".join(texts)
    return [needle for needle in needles if needle in joined]


# ----------------------------------------------------------------------
# 学习记忆播种（复用测试种子，不改测试代码）
# ----------------------------------------------------------------------
class PaperLookup:
    """记忆服务需要的极小目录接口。"""

    COURSES = {
        PAPER_PY_2425_FINAL: PY_COURSE,
        PAPER_PY_2324_FINAL: PY_COURSE,
        "py-2023-2024-s2-final": PY_COURSE,
        "py-2022-2023-s1-midterm": PY_COURSE,
        "math-bi-20250116-final": MATH_COURSE,
    }

    def get_paper(self, paper_id: str) -> Any:
        from types import SimpleNamespace

        course = self.COURSES.get(paper_id)
        if course is None:
            return None
        return SimpleNamespace(id=paper_id, course=course, title=paper_id)


LONG_LABELS = [
    "循环结构与终止条件的边界判断及常见错误分析",
    "列表与元组的可变性差异及适用场景选择",
    "字符串切片与索引越界的边界处理",
    "函数参数传递与默认参数的求值时机",
    "文件读写中的编码与异常处理顺序",
]
TARGET_NOTE = "目标附注：这就是学生本轮问到的第25条记录"


def seed_memory_records(db_path: Path, *, count: int = 25, all_notes: bool = False) -> dict[str, Any]:
    """按历史条件播种：25 条记录、5 个长标签、附注、真实 uuid4。"""

    from app.memory.models import MemoryCorrectionRequest
    from app.memory.service import LearningMemoryService
    from tests.f009_support import Seed

    seed = Seed(db_path)
    memory = LearningMemoryService(
        db_path,
        papers=PaperLookup(),
        taxonomy_path=db_path.parent / "absent-taxonomy.json",
        current_file=db_path.parent / "absent-current.json",
    )
    entries: list[str] = []
    target_entry = ""
    target_statement = ""
    for index in range(1, count + 1):
        conversation = seed.conversation(days_ago=index)
        problem = seed.problem(
            conversation,
            paper_id=PAPER_PY_2425_FINAL,
            source_type="registered_paper",
            days_ago=index,
        )
        statement = f"第{index}道练习：{LONG_LABELS[(index - 1) % len(LONG_LABELS)]}"
        exercise = seed.exercise(
            conversation,
            problem,
            knowledge_point=LONG_LABELS[(index - 1) % len(LONG_LABELS)],
            days_ago=index,
            statement=statement,
        )
        kind = index % 5
        if kind == 1:
            seed.attempt(exercise, correct=False, days_ago=index)
        elif kind == 2:
            seed.attempt(exercise, correct=True, after_reveal=True, days_ago=index)
        elif kind == 3:
            seed.attempt(exercise, correct=None, needs_supplement=True, days_ago=index)
        elif kind == 4:
            seed.attempt(exercise, correct=False, independence="unclear", days_ago=index)
        if all_notes or index == count:
            note = f"第{index}条附注：当时的错误原因与订正" if index != count else TARGET_NOTE
        else:
            note = f"第{index}条附注：当时错误原因"
        memory.correct("exercise", exercise, MemoryCorrectionRequest(note=note))
        entries.append(f"exercise:{exercise}")
        if index == count:
            target_entry = f"exercise:{exercise}"
            target_statement = statement
    return {
        "entries": entries,
        "target_entry": target_entry,
        "target_statement": target_statement,
        "labels": LONG_LABELS,
        "note": TARGET_NOTE,
    }


def seed_self_test_marker(db_path: Path, case_dir: Path, marker: str) -> dict[str, Any]:
    """自检用：通过真实学习记忆服务写入一条带唯一标记的记忆。"""

    from app.memory.models import MemoryCorrectionRequest
    from app.memory.service import LearningMemoryService
    from tests.f009_support import Seed

    seed = Seed(db_path)
    memory = LearningMemoryService(
        db_path,
        papers=PaperLookup(),
        taxonomy_path=case_dir / "absent-taxonomy.json",
        current_file=case_dir / "absent-current.json",
    )
    conversation = seed.conversation(days_ago=1)
    problem = seed.problem(
        conversation,
        paper_id=PAPER_PY_2425_FINAL,
        source_type="registered_paper",
        days_ago=1,
        statement="自检题：读程序写出输出",
    )
    exercise = seed.exercise(
        conversation,
        problem,
        knowledge_point=f"SELFTEST-{marker}",
        days_ago=1,
        statement=f"自检练习：{marker}",
    )
    seed.attempt(exercise, correct=False, days_ago=1)
    memory.correct(
        "exercise",
        exercise,
        MemoryCorrectionRequest(note=f"{marker}-注：自检写入的记忆附注"),
    )
    return {"entry": f"exercise:{exercise}", "note": f"{marker}-注：自检写入的记忆附注"}


# ----------------------------------------------------------------------
# F008 三份诊断脚本的原样重放
# ----------------------------------------------------------------------
F008_SCRIPTS = ("manual_review.py", "state_boundaries.py", "remaining_boundaries.py")
F008_SOURCE_DIR = PROJECT_ROOT / "tmp" / "f008-ux-fix-20261101" / "closure-rereview-3"


def prepare_f008_replay(run_dir: Path) -> dict[str, Any]:
    """复制脚本并只改写 work 一行；写路径必须落在本次 run 目录。"""

    target = run_dir / "f008-replay"
    src_dir = target / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    audit: list[dict[str, Any]] = []
    for name in F008_SCRIPTS:
        source = F008_SOURCE_DIR / name
        text = source.read_text(encoding="utf-8")
        replacement = f'work = Path({str(target / "out" / name.removesuffix(".py")).replace(chr(92), "/")!r})'
        lines = []
        rewritten = False
        for line in text.splitlines(keepends=True):
            if line.startswith("work = Path("):
                lines.append(replacement + "\n")
                rewritten = True
                audit.append(
                    {
                        "script": name,
                        "source_line": line.strip(),
                        "rewritten": replacement.strip(),
                        "target_dir": str(target),
                    }
                )
            else:
                lines.append(line)
        if not rewritten:
            raise SystemExit(f"{name} 没有找到 work = Path(...) 行，拒绝盲目重放")
        (src_dir / name).write_text("".join(lines), encoding="utf-8")
    (target / "out").mkdir(parents=True, exist_ok=True)
    bootstrap = src_dir / "_bootstrap.py"
    bootstrap.write_text(
        "\n".join(
            [
                '"""离线重放引导：先封锁外部传输，再执行诊断脚本并审计实际路径。"""',
                "import atexit, builtins, json, os, runpy, socket, sqlite3, sys, tempfile, urllib.request",
                "attempts = []",
                "WRITES, SQLITE = [], []",
                "_connect, _open = sqlite3.connect, builtins.open",
                "",
                "def _connect_audit(database, *a, **k):",
                "    SQLITE.append(str(database))",
                "    return _connect(database, *a, **k)",
                "",
                "def _open_audit(file, mode='r', *a, **k):",
                "    if any(flag in mode for flag in ('w', 'a', 'x', '+')):",
                "        WRITES.append(str(file))",
                "    return _open(file, mode, *a, **k)",
                "",
                "sqlite3.connect = _connect_audit",
                "builtins.open = _open_audit",
                "",
                "def _dump_audit():",
                "    payload = {",
                "        'cwd': os.getcwd(),",
                "        'temp': tempfile.gettempdir(),",
                "        'env': {name: os.environ.get(name) for name in (",
                "            'TEMP', 'TMP', 'HHU_STUDY_DB', 'HHU_QB_ROOT', 'HF_HOME', 'XDG_CACHE_HOME')},",
                "        'writes': sorted(set(WRITES)),",
                "        'sqlite': sorted(set(SQLITE)),",
                "        'external_attempts': attempts,",
                "    }",
                "    path = os.environ.get('F010A_AUDIT_OUT')",
                "    if path:",
                "        with _open(path, 'w', encoding='utf-8') as handle:",
                "            json.dump(payload, handle, ensure_ascii=False, indent=2)",
                "",
                "atexit.register(_dump_audit)",
                "",
                "def _deny(kind, target):",
                "    attempts.append({'kind': kind, 'target': str(target)})",
                "    raise RuntimeError(f'F010-A offline: blocked {kind} {target}')",
                "",
                "socket.create_connection = lambda address, *a, **k: _deny('connect', address)",
                "socket.getaddrinfo = lambda host, *a, **k: _deny('dns', host)",
                "urllib.request.urlopen = lambda url, *a, **k: _deny('urllib', url)",
                "script = sys.argv[1]",
                "try:",
                "    runpy.run_path(script, run_name='__main__')",
                "    code = 0",
                "except SystemExit as exc:",
                "    code = int(exc.code or 0)",
                "finally:",
                "    print(json.dumps({'guard_active': True, 'external_attempts': attempts},",
                "                     ensure_ascii=False))",
                "sys.exit(code)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (target / "path-audit.json").write_text(
        json.dumps(
            {
                "source_dir": str(F008_SOURCE_DIR),
                "target_dir": str(target),
                "rewrites": audit,
                "rule": "只改写 work = Path(...) 一行；其余原样",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"dir": target, "scripts": list(F008_SCRIPTS)}


def _json_summary(text: str) -> dict[str, Any]:
    """取重放脚本打印的那份 JSON（后面可能紧跟一行 guard 自检）。"""

    cut = text.rfind('{"guard_active"')
    body = text[:cut] if cut >= 0 else text
    start = body.find("{")
    while start >= 0:
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(body[start:])
            if isinstance(payload, dict) and "passed" in payload:
                return payload
        start = body.find("{", start + 1)
    return {}


def _fold_audit(audit: dict[str, Any], target: Path) -> dict[str, Any]:
    """重放子进程的路径审计结论：写操作与 sqlite 连接都必须在重放目录内。"""

    from urllib.parse import unquote

    target_abs = target.resolve()

    def inside(raw: str) -> bool:
        text = unquote(raw[5:]).lstrip("/") if raw.startswith("file:") else raw
        text = text.split("?")[0]
        try:
            Path(text).resolve().relative_to(target_abs)
            return True
        except (ValueError, OSError):
            return False

    if not audit:
        return {"available": False}
    writes = audit.get("writes") or []
    sqlite_paths = audit.get("sqlite") or []
    outside_writes = [item for item in writes if not inside(item)]
    outside_sqlite = [
        item
        for item in sqlite_paths
        if not inside(item) and ":memory:" not in item and not item.startswith("file::memory:")
    ]
    return {
        "available": True,
        "temp": audit.get("temp"),
        "cwd": audit.get("cwd"),
        "env": audit.get("env"),
        "writes": writes[:40],
        "sqlite": sqlite_paths[:40],
        "writes_outside": outside_writes[:20],
        "sqlite_outside": outside_sqlite[:20],
        "confined": not outside_writes,
    }


def run_f008_replay(run_dir: Path, python: Path) -> dict[str, Any]:
    target = run_dir / "f008-replay"
    results: dict[str, Any] = {"scripts": {}, "external_attempts": 0}
    env = os.environ.copy()
    env.update(
        {
            "NO_PROXY": "*",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "LANGSMITH_TRACING": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(run_dir / "system-temp"),
            "TMP": str(run_dir / "system-temp"),
            "HHU_QB_ROOT": str(run_dir / "qb"),
            "HHU_STUDY_DB": str(run_dir / "f008-replay" / "replay.sqlite3"),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    (run_dir / "system-temp").mkdir(parents=True, exist_ok=True)
    for name in F008_SCRIPTS:
        script = target / "src" / name
        audit_out = target / "out" / f"{name}.audit.json"
        env["F010A_AUDIT_OUT"] = str(audit_out)
        completed = subprocess.run(
            [str(python), "-B", str(target / "src" / "_bootstrap.py"), str(script)],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        tail = (completed.stdout or "").strip().splitlines()
        guard_line = ""
        for line in reversed(tail):
            if line.strip().startswith("{") and "guard_active" in line:
                guard_line = line
                break
        guard = {}
        if guard_line:
            with contextlib.suppress(json.JSONDecodeError):
                guard = json.loads(guard_line)
        out_file = target / "out" / name.removesuffix(".py")
        payload = None
        candidate = out_file.with_suffix(".json")
        if candidate.exists():
            with contextlib.suppress(json.JSONDecodeError):
                payload = json.loads(candidate.read_text(encoding="utf-8"))
        elif out_file.is_file():
            with contextlib.suppress(json.JSONDecodeError):
                payload = json.loads(out_file.read_text(encoding="utf-8"))
        else:
            for produced in sorted((target / "out").rglob("*.json")):
                if name.removesuffix(".py") in produced.name:
                    with contextlib.suppress(json.JSONDecodeError):
                        payload = json.loads(produced.read_text(encoding="utf-8"))
                    break
        stdout_text = completed.stdout or ""
        log_dir = target / "out" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{name}.log").write_text(stdout_text, encoding="utf-8")
        summary = _json_summary(stdout_text)
        audit = {}
        if audit_out.exists():
            with contextlib.suppress(json.JSONDecodeError):
                audit = json.loads(audit_out.read_text(encoding="utf-8"))
        results["scripts"][name] = {
            "exit_code": completed.returncode,
            "summary": summary,
            "path_audit": _fold_audit(audit, target),
            "stdout_tail": tail[-40:],
            "stderr_tail": (completed.stderr or "").strip().splitlines()[-20:],
            "guard": guard,
            "result": payload,
        }
        results["external_attempts"] += len(guard.get("external_attempts") or [])
    return results


# ----------------------------------------------------------------------
# 模型输入/输出位置视图
# ----------------------------------------------------------------------
def _message_texts(messages: Iterable[Any], *, skip_system: bool = True) -> Iterator[str]:
    """只取"真正喂给模型/学生的内容"：跳过固定系统提示，避免误命中。"""

    for message in messages or []:
        if isinstance(message, dict):
            role = message.get("role")
            if skip_system and role == "system":
                continue
            content = message.get("content")
            if role == "tool":
                yield json.dumps(message, ensure_ascii=False, default=str)
            elif isinstance(content, str):
                yield content
            elif content is not None:
                yield json.dumps(content, ensure_ascii=False, default=str)
        else:
            yield str(message)


def messages_text(messages: Iterable[Any]) -> str:
    return "\n".join(_message_texts(messages))


def decision_text() -> str:
    return "\n".join(messages_text(messages) for messages in ACTIVE.decision_messages)


def reply_input_text() -> str:
    return "\n".join(messages_text(messages) for messages in ACTIVE.reply_messages)


def reply_text_since(index: int) -> str:
    return "\n".join(messages_text(messages) for messages in ACTIVE.reply_messages[index:])


def decision_text_since(index: int) -> str:
    return "\n".join(messages_text(messages) for messages in ACTIVE.decision_messages[index:])


def tutor_text() -> str:
    return "\n".join(
        json.dumps(ACTIVE.tutor_calls, ensure_ascii=False, default=str)
    )


def reset_case_state() -> None:
    ACTIVE.__init__()  # type: ignore[misc]