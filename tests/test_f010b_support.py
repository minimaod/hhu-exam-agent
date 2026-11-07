"""F010 B 测试夹具：真实服务 + 假题库/调度/辅导/回复。

只替换模型与题库，SQLite、恢复记录、SSE、正文材料都走真实实现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from starlette.requests import Request

from app.conversations.models import Turn
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.mistakes.service import MistakeRecordService
from app.papers.repository import PaperRepository
from app.papers.service import DEFAULT_PAPERS_ROOT, PaperService
from app.questions.models import QuestionCard
from app.tutoring.models import Source
from tests.f009_support import Seed
from tests.question_fakes import FakeQuestionRuntime, sample_card
from tests.tool_fakes import ReplyFactory
from tests.tutoring_fakes import (
    FakeExerciseAgent,
    FakeScheduler,
    FakeTutor,
    clear_understanding,
    sample_explanation,
)


async def _idle_receive() -> dict:
    import asyncio

    # 让 Starlette 的 is_disconnected 轮询超时返回 False，而不是抛
    # “Receive channel has not been made available”。
    await asyncio.sleep(1)
    return {"type": "http.request", "body": b"", "more_body": False}


def dummy_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/conversations/test/turns",
            "headers": [],
        },
        receive=_idle_receive,
    )


def turn_input(text: str, *, retry_of: str | None = None) -> TurnInput:
    return TurnInput(
        request_id=str(uuid4()),
        kind=None,
        dispatch=True,
        text=text,
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=False,
        retry_of=retry_of,
    )


@dataclass
class Harness:
    service: ConversationService
    repo: ConversationRepository
    scheduler: Any
    tutor: FakeTutor
    reply: ReplyFactory
    questions: FakeQuestionRuntime
    db: Path


def make_harness(
    tmp_path: Path,
    *,
    cards: list[QuestionCard] | None = None,
    replies: list[str] | None = None,
    memory: Any | None = None,
    scheduler: Any | None = None,
) -> Harness:
    db = tmp_path / "study.sqlite3"
    repo = ConversationRepository(db)
    repo.initialize()
    papers = PaperService(PaperRepository(DEFAULT_PAPERS_ROOT))
    questions = FakeQuestionRuntime(cards or [sample_card()])
    tutor = FakeTutor(
        understandings=[clear_understanding()],
        explanations=[sample_explanation()],
    )
    records = MistakeRecordService(
        repository=repo,
        papers=papers,
        make_tutor=lambda: tutor,
        tutoring_error=None,
        attachments_root=tmp_path / "attachments",
    )
    scheduler = scheduler or FakeScheduler()
    reply = ReplyFactory(replies or ["好的。"])
    service = ConversationService(
        repository=repo,
        make_query=None,
        query_error=None,
        make_tutor=lambda: tutor,
        make_router=None,
        make_exercise_agent=lambda: FakeExerciseAgent(),
        make_scheduler=lambda: scheduler,
        papers=papers,
        records=records,
        memory=memory,
        attachments_root=tmp_path / "attachments",
        make_reply=reply,
        question_runtime=questions,
    )
    return Harness(service, repo, scheduler, tutor, reply, questions, db)


def seed_question_turn(
    repo: ConversationRepository,
    conversation_id: str,
    cards: list[QuestionCard],
    *,
    request_id: str = "req-seed",
    query: str = "这张卷的选择题",
    paper_id: str | None = None,
    paper_title: str = "2024-2025 学年第一学期 Python 期末试卷",
    question_type: str | None = "choice",
    assistant: str = "已列出这份卷的题目。",
) -> Turn:
    """直接落一轮“查题成功”，让工作区有真实题卡与引用。"""

    paper_id = paper_id or cards[0].source.paper_id
    repo.start_turn(conversation_id, request_id, query, kind="dynamic")
    repo.complete_dynamic_turn(
        conversation_id,
        request_id,
        kind="dynamic",
        question_results=cards,
        question_result_status="ok",
        assistant_message=assistant,
        question_context={
            "paper_id": paper_id,
            "paper_title": paper_title,
            "question_type": question_type,
            "number_label": None,
            "total": len(cards),
            "offset": 0,
            "returned": len(cards),
            "next_offset": None,
            "limit": 64,
            "request_id": request_id,
            "query": "",
            "shown_ids": [card.question_id for card in cards],
        },
        transcript={
            "version": 1,
            "steps": [
                {
                    "id": "seed-step-1",
                    "call_id": "seed-call-1",
                    "tool": "list_paper_questions",
                    "plan": {
                        "action": "list_paper_questions",
                        "parameters": {"question_type": question_type},
                    },
                    "outcome": {
                        "status": "success",
                        "message": assistant,
                        "details": {"total": len(cards), "returned": len(cards)},
                    },
                }
            ],
        },
    )
    repo.upsert_context(conversation_id, confirmed_course="Python语言程序设计")
    turn = repo.find_turn(conversation_id, request_id)
    assert turn is not None
    return turn


def raw_turn_row(db: Path, conversation_id: str, request_id: str) -> dict[str, Any]:
    """独立连接读取，用于核对落盘事实（不经过服务层缓存）。"""

    import sqlite3

    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM conversation_turns WHERE conversation_id=? AND request_id=?",
            (conversation_id, request_id),
        ).fetchone()
    return dict(row) if row is not None else {}


def transcript_of(row: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(row.get("output_json") or "{}")
    return payload.get("_transcript") or {}


def mistake_rows(db: Path, conversation_id: str) -> list[dict[str, Any]]:
    import sqlite3

    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, problem_version, request_id, updated_at FROM mistake_records "
            "WHERE conversation_id=? ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
    return [dict(row) for row in rows]