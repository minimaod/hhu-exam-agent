"""错题记录创建、列表、修改、纠正幂等与来源校验。"""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.images as images
from app.api.conversations import (
    ConversationServiceState,
    get_conversation_service_state,
)
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService
from app.main import app
from app.mistakes.service import MistakeRecordService
from app.papers.repository import PaperRepository
from app.papers.service import PaperService
from tests.fakes import parse_sse
from tests.tutoring_fakes import (
    FakeRouter,
    FakeTutor,
    clear_understanding,
    png_bytes,
    sample_explanation,
    unclear_understanding,
)


@dataclass
class Env:
    repo: ConversationRepository
    tutor: FakeTutor
    records: MistakeRecordService
    client: TestClient


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sample_root: Path):
    monkeypatch.setattr(images, "ATTACHMENTS_ROOT", tmp_path / "attachments")
    repository = ConversationRepository(tmp_path / "study.sqlite3")
    repository.initialize()
    papers = PaperService(PaperRepository(sample_root))
    tutor = FakeTutor()
    records = MistakeRecordService(
        repository,
        papers,
        make_tutor=lambda: tutor,
        tutoring_error=None,
        attachments_root=images.ATTACHMENTS_ROOT,
    )
    service = ConversationService(
        repository=repository,
        make_query=None,
        query_error="测试未配置查询",
        make_tutor=lambda: tutor,
        tutoring_error=None,
        make_router=lambda: FakeRouter([]),
        papers=papers,
        records=records,
        attachments_root=images.ATTACHMENTS_ROOT,
    )
    app.dependency_overrides[get_conversation_service_state] = lambda: (
        ConversationServiceState(service=service, error=None)
    )
    with TestClient(app) as client:
        yield Env(repo=repository, tutor=tutor, records=records, client=client)
    app.dependency_overrides.pop(get_conversation_service_state, None)


def _last_event(response) -> dict:
    events = parse_sse(response.text)
    return events[-1]


def _problem(env: Env, conversation_id: str, text: str = "题目") -> dict:
    env.tutor.push_understanding(clear_understanding(text))
    env.tutor.push_explanation(sample_explanation())
    response = env.client.post(
        f"/api/conversations/{conversation_id}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "problem",
            "query": text,
        },
    )
    return _last_event(response)


def _record(env: Env, conversation_id: str) -> str:
    response = env.client.post(
        f"/api/conversations/{conversation_id}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "record",
            "query": "记录这题",
        },
    )
    return _last_event(response)["tutoring"]["record"]["id"]


def test_direct_record_create_is_idempotent(env: Env) -> None:
    conversation_id = env.client.post("/api/conversations", json={}).json()["id"]
    _problem(env, conversation_id, "直接记录")
    first = env.client.post(
        "/api/mistake-records",
        json={"request_id": str(uuid4()), "conversation_id": conversation_id},
    )
    assert first.status_code == 201
    second = env.client.post(
        "/api/mistake-records",
        json={"request_id": str(uuid4()), "conversation_id": conversation_id},
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert env.client.get("/api/mistake-records").json()["total"] == 1


def test_record_list_order_and_patch(env: Env) -> None:
    conversation_id = env.client.post("/api/conversations", json={}).json()["id"]
    _problem(env, conversation_id, "第一题")
    first = _record(env, conversation_id)
    detail = env.client.get(f"/api/mistake-records/{first}").json()
    assert detail["title"].startswith("第一题")
    assert detail["source"]["type"] == "none"

    patched = env.client.patch(
        f"/api/mistake-records/{first}",
        json={
            "title": "手写标题",
            "source": {"type": "student_text", "text": "同学说来自 2023 期中"},
            "feedback_text": "这题当时没思路",
            "self_reported_understood": True,
        },
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["title"] == "手写标题"
    assert body["source"]["type"] == "student_text"
    assert body["feedback"]["text"] == "这题当时没思路"
    assert body["self_reported_understood"] is True

    listing = env.client.get("/api/mistake-records", params={"limit": 10, "offset": 0})
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["id"] == first

    assert env.client.get("/api/mistake-records", params={"limit": 0}).status_code == 422


def test_registered_paper_source_is_validated_and_snapshotted(env: Env) -> None:
    conversation_id = env.client.post("/api/conversations", json={}).json()["id"]
    env.tutor.push_understanding(clear_understanding("来源题"))
    env.tutor.push_explanation(sample_explanation())
    response = env.client.post(
        f"/api/conversations/{conversation_id}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "problem",
            "query": "来源题",
            "source": {"type": "registered_paper", "paper_id": "sample-final"},
        },
    )
    assert _last_event(response)["type"] == "result"
    record_id = _record(env, conversation_id)
    detail = env.client.get(f"/api/mistake-records/{record_id}").json()
    assert detail["source"]["paper_id"] == "sample-final"
    assert detail["source"]["paper_title"] == "样例试卷 sample-final"


def test_record_correction_unclear_does_not_change_record(env: Env) -> None:
    conversation_id = env.client.post("/api/conversations", json={}).json()["id"]
    _problem(env, conversation_id, "原题")
    record_id = _record(env, conversation_id)
    env.tutor.push_understanding(unclear_understanding("模糊的新题干"))

    response = env.client.post(
        f"/api/mistake-records/{record_id}/corrections",
        json={
            "request_id": str(uuid4()),
            "statement_text": "模糊的新题干",
        },
    )
    event = _last_event(response)
    assert event["type"] == "result"
    assert event["result"]["kind"] == "record_needs_clarification"
    detail = env.client.get(f"/api/mistake-records/{record_id}").json()
    assert detail["statement_text"] == "原题"
    assert detail["revisions"] == []


def test_record_correction_replay_does_not_call_model_again(env: Env) -> None:
    conversation_id = env.client.post("/api/conversations", json={}).json()["id"]
    _problem(env, conversation_id, "原题")
    record_id = _record(env, conversation_id)
    request_id = str(uuid4())
    env.tutor.push_understanding(clear_understanding("新题干"))
    env.tutor.push_explanation(sample_explanation("2"))
    first = env.client.post(
        f"/api/mistake-records/{record_id}/corrections",
        json={"request_id": request_id, "statement_text": "新题干"},
    )
    assert _last_event(first)["result"]["kind"] == "record_updated"
    calls = len(env.tutor.understand_calls)

    replay = env.client.post(
        f"/api/mistake-records/{record_id}/corrections",
        json={"request_id": request_id, "statement_text": "新题干"},
    )
    assert _last_event(replay)["result"]["kind"] == "record_updated"
    assert len(env.tutor.understand_calls) == calls


def test_record_correction_uses_old_attachments_when_omitted(env: Env) -> None:
    conversation_id = env.client.post("/api/conversations", json={}).json()["id"]
    image = env.client.post(
        f"/api/conversations/{conversation_id}/attachments",
        files={"file": ("p.png", b"\x89PNG\r\n\x1a\n" + b"x", "image/png")},
    )
    assert image.status_code == 422  # 伪造图片应先被拒绝

    from tests.tutoring_fakes import png_bytes

    uploaded = env.client.post(
        f"/api/conversations/{conversation_id}/attachments",
        files={"file": ("p.png", png_bytes(), "image/png")},
    ).json()
    env.tutor.push_understanding(clear_understanding("原图题"))
    env.tutor.push_explanation(sample_explanation())
    env.client.post(
        f"/api/conversations/{conversation_id}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "problem",
            "query": "原图题",
            "attachments": [{"id": uploaded["id"], "role": "statement"}],
        },
    )
    record_id = _record(env, conversation_id)
    env.tutor.push_understanding(clear_understanding("纠正文字"))
    env.tutor.push_explanation(sample_explanation("3"))
    response = env.client.post(
        f"/api/mistake-records/{record_id}/corrections",
        json={"request_id": str(uuid4()), "statement_text": "纠正文字"},
    )
    assert _last_event(response)["result"]["kind"] == "record_updated"
    detail = env.client.get(f"/api/mistake-records/{record_id}").json()
    assert detail["attachments"][0]["id"] == uploaded["id"]


def test_record_creation_old_version_conflicts(env: Env) -> None:
    conversation_id = env.client.post("/api/conversations", json={}).json()["id"]
    _problem(env, conversation_id, "第一版")
    record_id = _record(env, conversation_id)

    env.tutor.push_understanding(clear_understanding("第二版"))
    env.tutor.push_explanation(sample_explanation("2"))
    env.client.post(
        f"/api/conversations/{conversation_id}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "correction",
            "query": "第二版",
        },
    )

    response = env.client.post(
        "/api/mistake-records",
        json={
            "request_id": str(uuid4()),
            "conversation_id": conversation_id,
        },
    )
    assert response.status_code == 409
    assert response.json()["record_id"] == record_id

    detail = env.client.get(f"/api/mistake-records/{record_id}").json()
    assert detail["conversation_has_newer_version"] is True


def test_self_reported_understood_can_be_cleared(env: Env) -> None:
    conversation_id = env.client.post("/api/conversations", json={}).json()["id"]
    _problem(env, conversation_id, "自述题")
    record_id = _record(env, conversation_id)

    enabled = env.client.patch(
        f"/api/mistake-records/{record_id}",
        json={"self_reported_understood": True},
    )
    assert enabled.status_code == 200
    assert enabled.json()["self_reported_understood"] is True
    assert enabled.json()["feedback"]["self_reported_at"] is not None

    disabled = env.client.patch(
        f"/api/mistake-records/{record_id}",
        json={"self_reported_understood": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["self_reported_understood"] is False
    assert disabled.json()["feedback"]["self_reported_at"] is None


def test_record_correction_same_request_id_different_content_conflicts(
    env: Env,
) -> None:
    conversation_id = env.client.post("/api/conversations", json={}).json()["id"]
    _problem(env, conversation_id, "原题")
    record_id = _record(env, conversation_id)
    request_id = str(uuid4())

    env.tutor.push_understanding(clear_understanding("计算 2+2"))
    env.tutor.push_explanation(sample_explanation("4"))
    first = env.client.post(
        f"/api/mistake-records/{record_id}/corrections",
        json={"request_id": request_id, "statement_text": "计算 2+2"},
    )
    assert _last_event(first)["result"]["kind"] == "record_updated"
    calls = len(env.tutor.understand_calls)

    conflict = env.client.post(
        f"/api/mistake-records/{record_id}/corrections",
        json={"request_id": request_id, "statement_text": "计算 9+9"},
    )
    assert conflict.status_code == 409
    assert len(env.tutor.understand_calls) == calls
    detail = env.client.get(f"/api/mistake-records/{record_id}").json()
    assert detail["statement_text"] == "计算 2+2"


def test_record_correction_rejects_total_and_duplicate_attachments(
    env: Env,
) -> None:
    conversation_id = env.client.post("/api/conversations", json={}).json()["id"]
    uploaded = env.client.post(
        f"/api/conversations/{conversation_id}/attachments",
        files={"file": ("p.png", png_bytes(), "image/png")},
    ).json()
    env.tutor.push_understanding(clear_understanding("原图题"))
    env.tutor.push_explanation(sample_explanation())
    env.client.post(
        f"/api/conversations/{conversation_id}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "problem",
            "query": "原图题",
            "attachments": [{"id": uploaded["id"], "role": "statement"}],
        },
    )
    record_id = _record(env, conversation_id)

    large = []
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
        large.append(attachment_id)

    total = env.client.post(
        f"/api/mistake-records/{record_id}/corrections",
        json={
            "request_id": str(uuid4()),
            "statement_text": "新题干",
            "attachments": [
                {
                    "id": attachment_id,
                    "role": "statement" if index < 3 else "solution",
                }
                for index, attachment_id in enumerate(large)
            ],
        },
    )
    assert total.status_code == 413

    duplicate = env.client.post(
        f"/api/mistake-records/{record_id}/corrections",
        json={
            "request_id": str(uuid4()),
            "statement_text": "新题干",
            "attachments": [
                {"id": large[0], "role": "statement"},
                {"id": large[0], "role": "solution"},
            ],
        },
    )
    assert duplicate.status_code == 422
