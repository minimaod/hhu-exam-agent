"""F005 辅导、图片与错题记录的主流程测试。

模型一律使用 FakeTutor/FakeRouter；数据库、附件目录和资料目录都是临时路径，
不读写项目真实的 data/local 或 data/papers。
"""

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.images as images
from app.api.conversations import (
    ConversationServiceState,
    get_conversation_service_state,
)
from app.conversations.dispatch import IntentDecision
from app.conversations.errors import ConversationStorageError
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.main import app
from app.mistakes.service import MistakeRecordService
from app.papers.repository import PaperRepository
from app.papers.service import PaperService
from app.querying.errors import QueryError
from app.tutoring.models import FollowupAnswer, Source
from tests.fakes import parse_sse
from tests.tutoring_fakes import (
    FakeRouter,
    FakeTutor,
    clear_understanding,
    jpeg_bytes,
    png_bytes,
    sample_explanation,
    unclear_understanding,
)


class NeverDisconnected:
    async def is_disconnected(self) -> bool:
        return False


@dataclass
class Env:
    repo: ConversationRepository
    papers: PaperService
    tutor: FakeTutor
    router: FakeRouter
    records: MistakeRecordService
    service: ConversationService
    client: TestClient


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sample_root: Path):
    monkeypatch.setattr(images, "ATTACHMENTS_ROOT", tmp_path / "attachments")
    repository = ConversationRepository(tmp_path / "study.sqlite3")
    repository.initialize()
    papers = PaperService(PaperRepository(sample_root))
    tutor = FakeTutor()
    router = FakeRouter([])

    def make_tutor() -> FakeTutor:
        return tutor

    records = MistakeRecordService(
        repository,
        papers,
        make_tutor=make_tutor,
        tutoring_error=None,
        attachments_root=images.ATTACHMENTS_ROOT,
    )
    service = ConversationService(
        repository=repository,
        make_query=None,
        query_error="测试未配置查询",
        make_tutor=make_tutor,
        tutoring_error=None,
        make_router=lambda: router,
        papers=papers,
        records=records,
        attachments_root=images.ATTACHMENTS_ROOT,
    )
    app.dependency_overrides[get_conversation_service_state] = lambda: (
        ConversationServiceState(service=service, error=None)
    )
    with TestClient(app) as client:
        yield Env(
            repo=repository,
            papers=papers,
            tutor=tutor,
            router=router,
            records=records,
            service=service,
            client=client,
        )
    app.dependency_overrides.pop(get_conversation_service_state, None)


def create_conversation(env: Env) -> str:
    response = env.client.post("/api/conversations", json={})
    assert response.status_code == 201
    return response.json()["id"]


def turn(
    env: Env,
    conversation_id: str,
    *,
    kind: str | None = "problem",
    text: str = "",
    request_id: str | None = None,
    attachments: list[dict] | None = None,
    student_answer: str | None = None,
    solution_text: str | None = None,
    source: dict | None = None,
    problem_id: str | None = None,
    start_new: bool = False,
):
    body: dict = {
        "request_id": request_id or str(uuid4()),
        "query": text,
    }
    if kind is not None:
        body["kind"] = kind
    if attachments is not None:
        body["attachments"] = attachments
    if student_answer is not None:
        body["student_answer"] = student_answer
    if solution_text is not None:
        body["solution_text"] = solution_text
    if source is not None:
        body["source"] = source
    if problem_id is not None:
        body["problem_id"] = problem_id
    if start_new:
        body["start_new"] = True
    return env.client.post(
        f"/api/conversations/{conversation_id}/turns",
        json=body,
    )


def upload(
    env: Env,
    conversation_id: str,
    data: bytes | None = None,
    *,
    filename: str = "problem.png",
    mime: str = "image/png",
):
    return env.client.post(
        f"/api/conversations/{conversation_id}/attachments",
        files={"file": (filename, data or png_bytes(), mime)},
    )


def result_event(response) -> dict:
    events = parse_sse(response.text)
    assert events, response.text
    return events[-1]


def test_text_problem_is_explained_and_auto_saved(env: Env) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(clear_understanding("计算极限 lim_{x→0} (sin x)/x。"))
    env.tutor.push_explanation(sample_explanation())

    response = turn(
        env,
        conversation_id,
        kind="problem",
        text="计算极限 lim_{x→0} (sin x)/x。",
    )

    assert response.status_code == 200
    event = result_event(response)
    assert event["type"] == "result"
    assert event["kind"] == "problem"
    assert event["tutoring"]["kind"] == "explanation"
    assert event["tutoring"]["final_answer"] == "1"
    assert event["tutoring"]["problem"]["status"] == "explained"

    detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["turns"][0]["status"] == "completed"
    assert detail["turns"][0]["kind"] == "problem"
    assert detail["turns"][0]["tutoring"]["kind"] == "explanation"
    assert detail["active_problem"]["status"] == "explained"
    assert detail["record"] is None

    # 自动保存会话不等于自动加入错题本。
    assert env.client.get("/api/mistake-records").json()["total"] == 0


def test_image_problem_uses_uploaded_attachment(env: Env) -> None:
    conversation_id = create_conversation(env)
    data = png_bytes(400, 200)
    uploaded = upload(env, conversation_id, data)
    assert uploaded.status_code == 201
    attachment = uploaded.json()

    env.tutor.push_understanding(clear_understanding("计算定积分。"))
    env.tutor.push_explanation(sample_explanation("1/3"))
    response = turn(
        env,
        conversation_id,
        kind="problem",
        text="",
        attachments=[{"id": attachment["id"], "role": "statement"}],
    )

    event = result_event(response)
    assert event["type"] == "result"
    assert event["tutoring"]["problem"]["attachments"][0]["id"] == attachment["id"]
    _, images_seen = env.tutor.understand_calls[0]
    assert len(images_seen) == 1
    assert images_seen[0].data == data
    assert images_seen[0].mime == "image/png"

    preview = env.client.get(
        f"/api/conversations/{conversation_id}/attachments/{attachment['id']}"
    )
    assert preview.status_code == 200
    assert preview.content == data
    assert preview.headers["cache-control"] == "private, no-store"


def test_unclear_problem_requests_supplement_then_explains(env: Env) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(unclear_understanding())
    first = turn(env, conversation_id, kind="problem", text="求积分，图片看不清")
    first_event = result_event(first)
    assert first_event["tutoring"]["kind"] == "needs_clarification"
    assert first_event["tutoring"]["questions"]
    assert env.tutor.explain_calls == []

    detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["active_problem"]["status"] == "awaiting_supplement"
    assert detail["active_problem"]["clarity"] == "unclear"

    env.tutor.push_understanding(clear_understanding("求 ∫_0^1 x^2 dx。"))
    env.tutor.push_explanation(sample_explanation("1/3"))
    second = turn(
        env,
        conversation_id,
        kind="supplement",
        text="被积函数是 x^2。",
    )
    second_event = result_event(second)
    assert second_event["tutoring"]["kind"] == "explanation"
    detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["active_problem"]["status"] == "explained"
    assert detail["active_problem"]["id"] == first_event["tutoring"]["problem"]["id"]


def test_student_wrong_solution_is_sent_to_model(env: Env) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(clear_understanding())
    env.tutor.push_explanation(
        sample_explanation(
            "1",
            error_analysis="你在第一步把 0/0 直接代入为 0；这是未定式，不能直接代入。",
        )
    )

    response = turn(
        env,
        conversation_id,
        kind="problem",
        text="计算极限。",
        student_answer="0",
        solution_text="sin0/0 = 0",
    )
    event = result_event(response)
    assert event["tutoring"]["kind"] == "explanation"
    assert "0/0" in event["tutoring"]["error_analysis"]
    call = env.tutor.explain_calls[0]
    assert call["student_answer"] == "0"
    assert call["solution_text"] == "sin0/0 = 0"


def test_correction_creates_new_version_and_old_explanation_is_not_current(env: Env) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(clear_understanding("计算 lim_{x→0} sin x / x。"))
    env.tutor.push_explanation(sample_explanation("1"))
    turn(env, conversation_id, kind="problem", text="计算 lim_{x→0} sin x / x。")
    detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    old_id = detail["active_problem"]["id"]
    old_problem = env.repo.get_problem(old_id)
    assert old_problem is not None and old_problem.active

    env.tutor.push_understanding(clear_understanding("计算 lim_{x→∞} sin x / x。"))
    env.tutor.push_explanation(sample_explanation("0"))
    correction = turn(
        env,
        conversation_id,
        kind="correction",
        text="题目里是 x→∞，不是 x→0。",
    )
    event = result_event(correction)
    assert event["tutoring"]["kind"] == "explanation"
    assert event["tutoring"]["problem"]["version"] == 2

    detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["active_problem"]["version"] == 2
    assert detail["active_problem"]["id"] != old_id
    old_after = env.repo.get_problem(old_id)
    assert old_after is not None and not old_after.active
    assert old_after.explanation is not None
    assert detail["turns"][0]["tutoring"]["problem"]["id"] == old_id


def test_followup_feedback_and_record_turn(env: Env) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(clear_understanding())
    env.tutor.push_explanation(sample_explanation())
    turn(env, conversation_id, kind="problem", text="计算极限。")

    env.tutor.push_followup(FollowupAnswer(answer="因为这是重要极限。", related_steps=[1]))
    followup = turn(env, conversation_id, kind="followup", text="为什么等于 1？")
    followup_event = result_event(followup)
    assert followup_event["tutoring"]["kind"] == "followup_answer"
    assert env.tutor.followup_calls[0]["question"] == "为什么等于 1？"

    feedback = turn(env, conversation_id, kind="feedback", text="我懂了")
    feedback_event = result_event(feedback)
    assert feedback_event["tutoring"]["kind"] == "feedback_recorded"
    assert "不代表" in feedback_event["tutoring"]["message"]

    record_turn = turn(env, conversation_id, kind="record", text="记录这题")
    record_event = result_event(record_turn)
    assert record_event["tutoring"]["kind"] == "record_created"
    record_id = record_event["tutoring"]["record"]["id"]

    detail = env.client.get(f"/api/mistake-records/{record_id}").json()
    assert detail["self_reported_understood"] is True
    assert detail["followups"][0]["answer"] == "因为这是重要极限。"
    assert detail["feedback"]["self_reported_understood"] is True
    assert detail["explanation"]["final_answer"] == "1"


def test_record_turn_is_idempotent_and_does_not_duplicate(env: Env) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(clear_understanding())
    env.tutor.push_explanation(sample_explanation())
    turn(env, conversation_id, kind="problem", text="计算极限。")

    first = turn(env, conversation_id, kind="record", text="记录这题")
    first_event = result_event(first)
    second = turn(env, conversation_id, kind="record", text="再记录一次")
    second_event = result_event(second)

    assert first_event["tutoring"]["kind"] == "record_created"
    assert second_event["tutoring"]["kind"] == "record_exists"
    assert second_event["tutoring"]["needs_update"] is False
    assert second_event["tutoring"]["record"]["id"] == first_event["tutoring"]["record"]["id"]
    assert env.client.get("/api/mistake-records").json()["total"] == 1


def test_new_problem_requires_explicit_start(env: Env) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(clear_understanding("第一题"))
    env.tutor.push_explanation(sample_explanation())
    turn(env, conversation_id, kind="problem", text="第一题")

    conflict = turn(env, conversation_id, kind="problem", text="第二题")
    assert conflict.status_code == 409

    env.tutor.push_understanding(clear_understanding("第二题"))
    env.tutor.push_explanation(sample_explanation("2"))
    second = turn(
        env,
        conversation_id,
        kind="problem",
        text="第二题",
        start_new=True,
    )
    event = result_event(second)
    assert event["tutoring"]["kind"] == "explanation"
    detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["active_problem"]["statement_text"] == "第二题"


def test_cross_conversation_attachment_is_rejected(env: Env) -> None:
    first = create_conversation(env)
    second = create_conversation(env)
    attachment = upload(env, first).json()

    response = turn(
        env,
        second,
        kind="problem",
        text="带图题",
        attachments=[{"id": attachment["id"], "role": "statement"}],
    )
    assert response.status_code == 422


def test_unregistered_paper_source_is_rejected(env: Env) -> None:
    conversation_id = create_conversation(env)
    response = turn(
        env,
        conversation_id,
        kind="problem",
        text="题目",
        source={"type": "registered_paper", "paper_id": "not-registered"},
    )
    assert response.status_code == 422
    assert env.tutor.understand_calls == []


def test_duplicate_request_compares_full_input(env: Env) -> None:
    conversation_id = create_conversation(env)
    request_id = str(uuid4())
    env.tutor.push_understanding(clear_understanding())
    env.tutor.push_explanation(sample_explanation())
    first = turn(
        env,
        conversation_id,
        kind="problem",
        text="计算极限。",
        request_id=request_id,
    )
    assert result_event(first)["type"] == "result"
    calls_after_first = len(env.tutor.understand_calls)

    replay = turn(
        env,
        conversation_id,
        kind="problem",
        text="计算极限。",
        request_id=request_id,
    )
    assert result_event(replay)["type"] == "result"
    assert len(env.tutor.understand_calls) == calls_after_first

    conflict = turn(
        env,
        conversation_id,
        kind="problem",
        text="计算极限。",
        request_id=request_id,
        student_answer="不同的解法",
    )
    assert conflict.status_code == 409


def test_auto_dispatch_chooses_query_or_problem(env: Env) -> None:
    conversation_id = create_conversation(env)
    env.router.push(IntentDecision(intent="problem", reason="学生在问题目"))
    env.tutor.push_understanding(clear_understanding())
    env.tutor.push_explanation(sample_explanation())
    response = turn(
        env,
        conversation_id,
        kind="auto",
        text="这题怎么做：求极限。",
    )
    event = result_event(response)
    assert event["kind"] == "problem"
    assert event["tutoring"]["kind"] == "explanation"
    assert env.router.calls[0]["active_problem"] is None


def test_model_failure_is_saved_as_failed(env: Env) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(QueryError("MODEL_TIMEOUT", "模型超时，请重试。"))
    response = turn(env, conversation_id, kind="problem", text="题目")
    event = result_event(response)
    assert event["type"] == "error"
    assert event["code"] == "MODEL_TIMEOUT"
    detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["turns"][0]["status"] == "failed"
    assert detail["active_problem"] is None


def test_save_failure_does_not_emit_result(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(clear_understanding())
    env.tutor.push_explanation(sample_explanation())

    def fail(*args, **kwargs):
        raise ConversationStorageError("数据库不可用")

    monkeypatch.setattr(env.repo, "save_problem_turn", fail)
    response = turn(env, conversation_id, kind="problem", text="题目")
    event = result_event(response)
    assert event["type"] == "error"
    assert event["code"] == "STORAGE_UNAVAILABLE"
    detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["turns"][0]["status"] == "failed"


def test_stopping_before_save_marks_interrupted(env: Env) -> None:
    conversation_id = create_conversation(env)
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingTutor(FakeTutor):
        async def understand_problem(self, text, images):
            started.set()
            await release.wait()
            return clear_understanding()

    blocker = BlockingTutor()
    env.service._make_tutor = lambda: blocker  # type: ignore[attr-defined]
    data = TurnInput(
        request_id=str(uuid4()),
        kind="problem",
        text="题目",
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(type="none"),
        problem_id=None,
        start_new=False,
    )
    prepared = env.service.prepare_turn(conversation_id, data)

    async def scenario() -> None:
        stream = env.service.stream_prepared(prepared, NeverDisconnected())
        first = await anext(stream)
        assert first.type == "progress"
        await wait_for(started)
        await stream.aclose()

    asyncio.run(scenario())
    stored = env.repo.find_turn(conversation_id, data.request_id)
    assert stored is not None
    assert stored.status == "interrupted"
    assert env.repo.get_active_problem(conversation_id) is None


async def wait_for(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=2)


def test_historical_feedback_record_and_correction_target_clicked_problem(
    env: Env,
) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(clear_understanding("第一题"))
    env.tutor.push_explanation(sample_explanation("1"))
    turn(env, conversation_id, kind="problem", text="第一题")
    detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    first_id = detail["turns"][0]["tutoring"]["problem"]["id"]

    env.tutor.push_understanding(clear_understanding("第二题"))
    env.tutor.push_explanation(sample_explanation("2"))
    turn(env, conversation_id, kind="problem", text="第二题", start_new=True)
    detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    second_id = detail["active_problem"]["id"]

    recorded = turn(
        env,
        conversation_id,
        kind="record",
        text="记录这题",
        problem_id=first_id,
    )
    event = result_event(recorded)
    assert event["tutoring"]["kind"] == "record_created"
    record_id = event["tutoring"]["record"]["id"]
    record = env.client.get(f"/api/mistake-records/{record_id}").json()
    assert record["statement_text"] == "第一题"

    feedback = turn(
        env,
        conversation_id,
        kind="feedback",
        text="我懂了",
        problem_id=first_id,
    )
    assert result_event(feedback)["tutoring"]["kind"] == "feedback_recorded"
    assert env.repo.get_problem(first_id).self_reported_understood is True
    assert env.repo.get_problem(second_id).self_reported_understood is False

    env.tutor.push_understanding(clear_understanding("第一题修正"))
    env.tutor.push_explanation(sample_explanation("3"))
    corrected = turn(
        env,
        conversation_id,
        kind="correction",
        text="第一题修正",
        problem_id=first_id,
    )
    corrected_event = result_event(corrected)
    assert corrected_event["tutoring"]["kind"] == "explanation"
    assert corrected_event["tutoring"]["problem"]["version"] == 2
    assert corrected_event["tutoring"]["problem"]["root_problem_id"] == first_id
    assert env.repo.get_problem(first_id).active is False
    assert env.repo.get_problem(second_id).active is False
    detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["active_problem"]["statement_text"] == "第一题修正"


def test_supplement_gets_original_statement_and_inherited_student_work(
    env: Env,
) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(unclear_understanding("求极限 sin(x)/?。"))
    first = turn(
        env,
        conversation_id,
        kind="problem",
        text="求极限 sin(x)/?。",
        student_answer="旧答案",
        solution_text="旧过程",
    )
    assert result_event(first)["tutoring"]["kind"] == "needs_clarification"

    env.tutor.push_understanding(clear_understanding("求极限 sin(x)/x。"))
    env.tutor.push_explanation(sample_explanation("1"))
    supplement = turn(
        env,
        conversation_id,
        kind="supplement",
        text="分母是 x。",
    )
    assert result_event(supplement)["tutoring"]["kind"] == "explanation"
    combined = env.tutor.understand_calls[1][0]
    assert "原题干" in combined
    assert "求极限 sin(x)/?。" in combined
    assert "分母是 x。" in combined
    explain_call = env.tutor.explain_calls[0]
    assert explain_call["student_answer"] == "旧答案"
    assert explain_call["solution_text"] == "旧过程"


def test_attachment_total_and_duplicate_limits(env: Env) -> None:
    conversation_id = create_conversation(env)
    ids = []
    for _index in range(4):
        attachment_id = str(uuid4())
        env.repo.create_attachment(
            conversation_id=conversation_id,
            attachment_id=attachment_id,
            stored_name=f"{conversation_id}/{attachment_id}.png",
            mime="image/png",
            width=100,
            height=100,
            size_bytes=6 * 1024 * 1024,
            sha256="0" * 64,
        )
        ids.append(attachment_id)

    total = turn(
        env,
        conversation_id,
        kind="problem",
        text="四张图",
        attachments=[
            {
                "id": attachment_id,
                "role": "statement" if index < 3 else "solution",
            }
            for index, attachment_id in enumerate(ids)
        ],
    )
    assert total.status_code == 413

    duplicate = turn(
        env,
        conversation_id,
        kind="problem",
        text="重复图",
        attachments=[
            {"id": ids[0], "role": "statement"},
            {"id": ids[0], "role": "solution"},
        ],
    )
    assert duplicate.status_code == 422

    extra = str(uuid4())
    env.repo.create_attachment(
        conversation_id=conversation_id,
        attachment_id=extra,
        stored_name=f"{conversation_id}/{extra}.png",
        mime="image/png",
        width=100,
        height=100,
        size_bytes=100,
        sha256="0" * 64,
    )
    too_many = turn(
        env,
        conversation_id,
        kind="problem",
        text="五张图",
        attachments=[
            *[
                {"id": attachment_id, "role": "statement"}
                for attachment_id in ids
            ],
            {"id": extra, "role": "statement"},
        ],
    )
    assert too_many.status_code == 422


def test_mistake_correction_and_delete_keep_attachment(env: Env) -> None:
    conversation_id = create_conversation(env)
    attachment = upload(env, conversation_id, png_bytes()).json()
    env.tutor.push_understanding(clear_understanding("原题干"))
    env.tutor.push_explanation(sample_explanation())
    turn(
        env,
        conversation_id,
        kind="problem",
        text="原题干",
        attachments=[{"id": attachment["id"], "role": "statement"}],
    )
    record_turn = turn(env, conversation_id, kind="record", text="记录")
    record_id = result_event(record_turn)["tutoring"]["record"]["id"]

    env.tutor.push_understanding(clear_understanding("纠正后的题干"))
    env.tutor.push_explanation(sample_explanation("2"))
    correction = env.client.post(
        f"/api/mistake-records/{record_id}/corrections",
        json={
            "request_id": str(uuid4()),
            "statement_text": "纠正后的题干",
            "attachments": [{"id": attachment["id"], "role": "statement"}],
        },
    )
    correction_event = result_event(correction)
    assert correction_event["type"] == "result"
    assert correction_event["result"]["kind"] == "record_updated"
    detail = env.client.get(f"/api/mistake-records/{record_id}").json()
    assert detail["statement_text"] == "纠正后的题干"
    assert len(detail["revisions"]) == 1
    assert detail["revisions"][0]["statement_text"] == "原题干"

    deleted = env.client.delete(f"/api/mistake-records/{record_id}")
    assert deleted.status_code == 204
    assert env.client.get(f"/api/mistake-records/{record_id}").status_code == 404
    assert (
        env.client.get(
            f"/api/conversations/{conversation_id}/attachments/{attachment['id']}"
        ).status_code
        == 200
    )
    assert env.client.get(f"/api/conversations/{conversation_id}").status_code == 200


def test_record_adopts_newer_conversation_version(env: Env) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(clear_understanding("第一版"))
    env.tutor.push_explanation(sample_explanation("1"))
    turn(env, conversation_id, kind="problem", text="第一版")
    record_id = result_event(
        turn(env, conversation_id, kind="record", text="记录")
    )["tutoring"]["record"]["id"]

    env.tutor.push_understanding(clear_understanding("第二版"))
    env.tutor.push_explanation(sample_explanation("2"))
    turn(env, conversation_id, kind="correction", text="第二版")

    detail = env.client.get(f"/api/mistake-records/{record_id}").json()
    assert detail["conversation_has_newer_version"] is True
    newer = detail["newer_problem"]
    adopted = env.client.post(
        f"/api/mistake-records/{record_id}/adopt-problem",
        json={"request_id": str(uuid4()), "problem_id": newer["id"]},
    )
    assert adopted.status_code == 200
    assert adopted.json()["problem_version"] == 2
    assert adopted.json()["statement_text"] == "第二版"
    assert len(adopted.json()["revisions"]) == 1


def test_invalid_image_upload_is_rejected(env: Env) -> None:
    conversation_id = create_conversation(env)
    garbage = upload(env, conversation_id, b"not an image")
    assert garbage.status_code == 422

    from PIL import Image
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGB", (10, 10), "white").save(buffer, "BMP")
    bmp = upload(env, conversation_id, buffer.getvalue(), mime="image/bmp")
    assert bmp.status_code == 415

    too_large = upload(env, conversation_id, b"x" * (6 * 1024 * 1024 + 1))
    assert too_large.status_code == 413

    missing = env.client.get(
        f"/api/conversations/{conversation_id}/attachments/{uuid4()}"
    )
    assert missing.status_code == 404


def test_duplicate_request_with_different_attachment_conflicts(env: Env) -> None:
    conversation_id = create_conversation(env)
    first = upload(env, conversation_id, png_bytes()).json()
    second = upload(env, conversation_id, png_bytes(300, 150)).json()
    request_id = str(uuid4())
    env.tutor.push_understanding(clear_understanding())
    env.tutor.push_explanation(sample_explanation())
    response = turn(
        env,
        conversation_id,
        kind="problem",
        text="题目",
        request_id=request_id,
        attachments=[{"id": first["id"], "role": "statement"}],
    )
    assert result_event(response)["type"] == "result"

    conflict = turn(
        env,
        conversation_id,
        kind="problem",
        text="题目",
        request_id=request_id,
        attachments=[
            {"id": first["id"], "role": "statement"},
            {"id": second["id"], "role": "statement"},
        ],
    )
    assert conflict.status_code == 409


def test_ambiguous_dispatch_returns_fixed_options(env: Env) -> None:
    conversation_id = create_conversation(env)
    env.router.push(IntentDecision(intent="ambiguous", reason="两种都像"))
    response = turn(env, conversation_id, kind="auto", text="这是啥？")
    event = result_event(response)
    assert event["type"] == "result"
    assert event["tutoring"]["kind"] == "intent_clarification"
    assert event["tutoring"]["options"] == ["query", "tutoring"]
    detail = env.client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["active_problem"] is None
    assert detail["turns"][0]["kind"] == "problem"


def test_auto_dispatch_record_and_feedback(env: Env) -> None:
    conversation_id = create_conversation(env)
    env.tutor.push_understanding(clear_understanding())
    env.tutor.push_explanation(sample_explanation())
    turn(env, conversation_id, kind="problem", text="题目")

    env.router.push(IntentDecision(intent="record", reason="学生要记录"))
    record_event = result_event(
        turn(env, conversation_id, kind="auto", text="记录这题")
    )
    assert record_event["tutoring"]["kind"] == "record_created"

    env.router.push(IntentDecision(intent="feedback", reason="学生说懂了"))
    feedback_event = result_event(
        turn(env, conversation_id, kind="auto", text="我懂了")
    )
    assert feedback_event["tutoring"]["kind"] == "feedback_recorded"


def test_legacy_f004_query_still_works(env: Env) -> None:
    conversation_id = create_conversation(env)
    # 该服务未配置查询；旧客户端请求应在建立流前得到 503，而不是被意图模型兜底。
    response = turn(env, conversation_id, kind=None, text="我要复习高数")
    assert response.status_code == 503
