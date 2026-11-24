"""F013：HTTP 429、用量接口与图片额度。"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth.models import User
from app.main import app
from tests.conftest import TEST_USER_ID
from tests.tutoring_fakes import png_bytes


def _set_limits(*, requests: int, images: int) -> None:
    gate = app.state.usage_gate
    assert gate is not None
    gate.set_override(
        TEST_USER_ID,
        daily_requests=requests,
        daily_images=images,
        expires_on="2099-01-01",
    )


def _conversation(client: TestClient) -> str:
    response = client.post("/api/conversations", json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_turn_is_rejected_before_sse_when_quota_is_zero(
    client: TestClient,
) -> None:
    _set_limits(requests=0, images=1)
    conversation = _conversation(client)
    response = client.post(
        f"/api/conversations/{conversation}/turns",
        json={"request_id": str(uuid4()), "query": "我要复习高数"},
        headers={"X-HHU-CSRF": "1"},
    )
    assert response.status_code == 429, response.text
    body = response.json()
    assert body["code"] == "MODEL_QUOTA_EXCEEDED"
    assert body["quota"]["scope"] == "user"
    assert body["quota"]["used"] == 0


def test_usage_endpoint_hides_site_totals(client: TestClient) -> None:
    _set_limits(requests=2, images=3)
    response = client.get("/api/usage/today")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requests"] == {"limit": 2, "used": 0, "remaining": 2}
    assert body["images"] == {"limit": 3, "used": 0, "remaining": 3}
    assert body["site_exhausted"] is False
    assert "site" not in body


def test_image_quota_rejects_second_upload(client: TestClient) -> None:
    _set_limits(requests=10, images=1)
    conversation = _conversation(client)
    files = {"file": ("a.png", png_bytes(), "image/png")}
    first = client.post(
        f"/api/conversations/{conversation}/attachments",
        files=files,
        headers={"X-HHU-CSRF": "1"},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/conversations/{conversation}/attachments",
        files={"file": ("b.png", png_bytes(), "image/png")},
        headers={"X-HHU-CSRF": "1"},
    )
    assert second.status_code == 429, second.text
    assert second.json()["code"] == "IMAGE_QUOTA_EXCEEDED"


def test_non_model_routes_remain_available_when_quota_is_zero(
    client: TestClient,
) -> None:
    _set_limits(requests=0, images=0)
    assert client.get("/api/conversations").status_code == 200
    assert client.get("/api/papers").status_code == 200
    assert client.get("/api/learning-memory").status_code == 200
    assert client.get("/api/mistake-records").status_code == 200

def test_second_conversation_turn_returns_409_while_first_active(
    client: TestClient,
) -> None:
    from app.conversations.repository import ConversationRepository

    _set_limits(requests=10, images=10)
    first = _conversation(client)
    second = _conversation(client)
    db_path = app.state.conversation_store.db_path
    repository = ConversationRepository(db_path).scoped(TEST_USER_ID)
    repository.start_turn(
        first, "active-request", "第一轮", lease_token="active-lease"
    )
    try:
        response = client.post(
            f"/api/conversations/{second}/turns",
            json={"request_id": str(uuid4()), "query": "第二轮"},
            headers={"X-HHU-CSRF": "1"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "TURN_IN_PROGRESS"
    finally:
        repository.release_turn_slot("active-lease")


def test_quota_storage_failure_returns_503_before_model(
    client: TestClient, monkeypatch
) -> None:
    from app.quota.errors import QuotaStorageError

    _set_limits(requests=10, images=10)
    conversation = _conversation(client)
    gate = app.state.usage_gate
    assert gate is not None

    def broken(owner_id: str) -> None:
        raise QuotaStorageError()

    monkeypatch.setattr(gate, "ensure_available", broken)
    response = client.post(
        f"/api/conversations/{conversation}/turns",
        json={"request_id": str(uuid4()), "query": "我要复习高数"},
        headers={"X-HHU-CSRF": "1"},
    )
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "QUOTA_STORAGE_UNAVAILABLE"
    assert "暂时无法核对用量" in response.json()["detail"]
