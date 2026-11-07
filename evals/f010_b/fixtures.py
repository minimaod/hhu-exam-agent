"""F010 B 专项评测夹具：真实产品服务 + 真实题库副本 + 脚本替身。

与 A 批评测共用离线基础设施（题库副本、目录副本、离线守卫），但运行目录、
用例、评分点完全独立；不读取、不改写 ``evals/f010_a`` 的任何文件。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
B_EVAL_DIR = Path(__file__).resolve().parent
RUN_ROOT = PROJECT_ROOT / "tmp" / "f010-b"

# 与 A 批一致的离线题库来源：只读一致性副本，复制后使用。
from evals.f010_a import fixtures as _a  # noqa: E402

FORMAL_QB = _a.FORMAL_QB
R001_QB = _a.R001_QB
BANK_BUILD = _a.BANK_BUILD
FORMAL_PAPERS = _a.FORMAL_PAPERS
OfflineGuard = _a.OfflineGuard
TraceWriter = _a.TraceWriter
parse_sse = _a.parse_sse
db_write_snapshot = _a.db_write_snapshot

PY_COURSE = "Python语言程序设计"
PAPER = "py-2024-2025-s1-final"
PAPER_TITLE = "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）及答案"
ALT_PAPER = "py-2024-2025-s2-final"


def prepare_run_dir(run_id: str) -> Path:
    if not run_id or "/" in run_id or "\\" in run_id:
        raise SystemExit("run-id 必须是单段目录名")
    target = RUN_ROOT / run_id
    if target.exists():
        raise SystemExit(f"运行目录已存在，拒绝覆盖历史证据：{target}")
    target.mkdir(parents=True)
    return target


def prepare_bank(run_dir: Path) -> Path:
    _a.RUN_ROOT = RUN_ROOT
    return _a.prepare_bank(run_dir)


def prepare_papers(run_dir: Path) -> Path:
    return _a.prepare_papers(run_dir)


def install_offline_config() -> None:
    _a.RUN_ROOT = RUN_ROOT
    _a.install_offline_config()


def install_adapters() -> None:
    _a.install_adapters()


def install_path_audit() -> None:
    _a.install_path_audit()


def install_real_call_guard() -> None:
    _a.install_real_call_guard()


def install_stream_audit() -> None:
    _a.install_stream_audit()


def bootstrap(run_id: str) -> "BEnv":

    run_dir = prepare_run_dir(run_id)
    bank = prepare_bank(run_dir)
    papers = prepare_papers(run_dir)
    os.environ["HHU_QB_ROOT"] = str(bank)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    temp = run_dir / "system-temp"
    temp.mkdir(exist_ok=True)
    os.environ["TEMP"] = str(temp)
    os.environ["TMP"] = str(temp)
    install_offline_config()
    guard = OfflineGuard()
    guard.install()
    guard.self_test()
    return BEnv(run_dir=run_dir, bank=bank, papers=papers, guard=guard)


@dataclass
class BEnv:
    run_dir: Path
    bank: Path
    papers: Path
    guard: OfflineGuard
    results: list[dict[str, Any]] = field(default_factory=list)
    case_dirs: dict[str, Path] = field(default_factory=dict)

    def case_dir(self, case_id: str) -> Path:
        target = self.run_dir / "cases" / case_id
        target.mkdir(parents=True, exist_ok=True)
        self.case_dirs[case_id] = target
        return target

    def add(self, case: str, unit: str, ok: bool, detail: Any = "") -> None:
        self.results.append(
            {
                "case": case,
                "unit": unit,
                "ok": bool(ok),
                "detail": detail if isinstance(detail, str) else str(detail),
            }
        )

    @property
    def score(self) -> int:
        return sum(1 for item in self.results if item["ok"])


class CaseService:
    """一个用例的真实服务栈：真实 SQLite、真实题库、脚本决策/回复。"""

    def __init__(self, env: BEnv, case_id: str) -> None:
        from app.conversations.repository import ConversationRepository
        from app.conversations.service import ConversationService
        from app.memory.service import LearningMemoryService
        from app.mistakes.service import MistakeRecordService
        from app.papers.repository import PaperRepository
        from app.papers.service import PaperService
        from app.questions.runtime import build_question_runtime
        from tests.tool_fakes import ReplyFactory
        from tests.tutoring_fakes import (
            FakeExerciseAgent,
            FakeScheduler,
            FakeTutor,
            clear_understanding,
            sample_explanation,
        )
        from tests.test_f009_evidence import FakePapers

        self.env = env
        self.case_id = case_id
        self.dir = env.case_dir(case_id)
        self.db = self.dir / "study.sqlite3"
        self.repo = ConversationRepository(self.db)
        self.repo.initialize()
        self.papers = PaperService(PaperRepository(env.papers))
        self.runtime = build_question_runtime()
        self.tutor = FakeTutor(
            understandings=[clear_understanding()],
            explanations=[sample_explanation()],
        )
        self.records = MistakeRecordService(
            repository=self.repo,
            papers=self.papers,
            make_tutor=lambda: self.tutor,
            tutoring_error=None,
            attachments_root=self.dir / "attachments",
        )
        self.memory = LearningMemoryService(
            self.db,
            papers=FakePapers(),
            taxonomy_path=self.dir / "absent-taxonomy.json",
            current_file=self.dir / "absent-current.json",
        )
        self.scheduler = FakeScheduler()
        self.reply = ReplyFactory(["好的。"])
        self.service = ConversationService(
            repository=self.repo,
            make_query=None,
            query_error=None,
            make_tutor=lambda: self.tutor,
            make_router=None,
            make_exercise_agent=lambda: FakeExerciseAgent(),
            make_scheduler=lambda: self.scheduler,
            papers=self.papers,
            records=self.records,
            memory=self.memory,
            attachments_root=self.dir / "attachments",
            make_reply=self.reply,
            question_runtime=self.runtime,
        )
        self.conversation_id: str = ""
        self.turns: list[dict[str, Any]] = []

    def conversation(self) -> str:
        if not self.conversation_id:
            self.conversation_id = self.service.create_conversation().id
        return self.conversation_id

    def restart(self) -> None:
        """模拟进程重启：同一库、同目录重建服务与题库运行时。"""

        self.service.close()
        fresh = type(self)(self.env, self.case_id)
        fresh.conversation_id = self.conversation_id
        fresh.turns = self.turns
        self.__dict__.update(fresh.__dict__)

    async def turn(
        self,
        text: str,
        *,
        decisions: list[Any] = (),
        replies: list[str] | None = None,
        retry_of: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        from tests.test_f010b_support import dummy_request, turn_input

        request_id = request_id or str(uuid4())
        for item in decisions:
            self.scheduler.push(item)
        if replies is not None:
            from tests.tool_fakes import ReplyFactory

            self.service._make_reply = ReplyFactory(replies)
        prepared = self.service.prepare_turn(
            self.conversation(), turn_input(text, retry_of=retry_of)
        )
        # prepare_turn 会生成自己的 request_id；用真实请求号覆盖记录。
        request_id = prepared.request_id
        events = [
            event async for event in self.service.stream_prepared(prepared, dummy_request())
        ]
        record = {
            "request_id": request_id,
            "text": text,
            "events": events,
            "types": [event.type for event in events],
            "result": next(
                (event for event in events if event.type == "result"), None
            ),
        }
        self.turns.append(record)
        return record

    def raw(self, request_id: str) -> dict[str, Any]:
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM conversation_turns WHERE conversation_id=? AND request_id=?",
                (self.conversation_id, request_id),
            ).fetchone()
        return dict(row) if row is not None else {}

    def transcript(self, request_id: str) -> dict[str, Any]:
        import json

        row = self.raw(request_id)
        payload = json.loads(row.get("output_json") or "{}")
        return payload.get("_transcript") or {}

    def last_history(self) -> list[dict[str, Any]]:
        calls = self.scheduler.calls
        return (calls[-1].get("history") if calls else None) or []

    def sql(self, statement: str, params: tuple = ()) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(statement, params).fetchall()]

    def mutate_output(self, request_id: str, mutate) -> None:
        import json

        row = self.raw(request_id)
        payload = json.loads(row["output_json"])
        mutate(payload)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE conversation_turns SET output_json=? WHERE conversation_id=? AND request_id=?",
                (json.dumps(payload, ensure_ascii=False), self.conversation_id, request_id),
            )