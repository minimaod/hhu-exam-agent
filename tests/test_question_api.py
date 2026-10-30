"""F007 P4：真题接口、答案门禁与来源资源边界。"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.conversations.repository import ConversationRepository
from tests.fakes import parse_sse
from tests.question_fakes import FakeQuestionRuntime, sample_card

SENTINEL = "SENTINEL-ANSWER-TEXT-DO-NOT-PERSIST"


def test_only_explain_question_accepts_choice_answer() -> None:
    import pytest
    from app.api.conversations import TurnRequest

    fields = {"request_id": str(uuid4()), "question_id": "paper-choice-1",
              "question_source_turn": str(uuid4()), "student_answer": " B "}
    request = TurnRequest.model_validate({**fields, "kind": "explain_question"})
    assert request.student_answer == "B"
    for kind in ("select_question", "view_question_answer"):
        with pytest.raises(ValueError):
            TurnRequest.model_validate({**fields, "kind": kind})


def create_conversation(client: TestClient) -> dict:
    response = client.post("/api/conversations", json={})
    assert response.status_code == 201, response.text
    return response.json()


def add_search_turn(
    study_db_path: Path, conversation_id: str, card
) -> str:
    repository = ConversationRepository(study_db_path)
    request_id = str(uuid4())
    repository.start_turn(conversation_id, request_id, "找一道真题", kind="dynamic")
    repository.complete_dynamic_turn(
        conversation_id,
        request_id,
        kind="dynamic",
        assistant_message="找到题目",
        question_results=[card],
        question_result_status="ok",
    )
    return request_id


def install_runtime(client: TestClient) -> FakeQuestionRuntime:
    runtime = FakeQuestionRuntime(answer_text=SENTINEL)
    client.app.state.question_runtime = runtime
    return runtime


def test_question_fields_are_validated_by_kind(client: TestClient) -> None:
    conversation = create_conversation(client)
    response = client.post(
        f"/api/conversations/{conversation['id']}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "query",
            "query": "查试卷",
            "question_id": "py-2024-2025-s1-final-choice-1",
        },
    )
    assert response.status_code == 422

    response = client.post(
        f"/api/conversations/{conversation['id']}/turns",
        json={"request_id": str(uuid4()), "kind": "select_question"},
    )
    assert response.status_code == 422


def test_select_turn_then_detail_and_answer_gate(
    client: TestClient, study_db_path: Path
) -> None:
    runtime = install_runtime(client)
    conversation = create_conversation(client)
    card = sample_card()
    source = add_search_turn(study_db_path, conversation["id"], card)

    response = client.post(
        f"/api/conversations/{conversation['id']}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "select_question",
            "question_id": card.question_id,
            "question_source_turn": source,
        },
    )
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    event = events[-1]
    assert event["type"] == "result"
    assert event["kind"] == "select_question"
    assert event["question"]["question_id"] == card.question_id
    assert event["question"]["answer_detail"] is None
    assert SENTINEL not in response.text

    detail = client.get(f"/api/conversations/{conversation['id']}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["active_question"]["question_id"] == card.question_id
    assert payload["active_question"]["answer_detail"] is None
    assert SENTINEL not in detail.text

    build = card.build_id
    answer = client.get(
        f"/api/conversations/{conversation['id']}/questions/{card.question_id}"
        f"/answer?build={build}"
    )
    assert answer.status_code == 409
    assert SENTINEL not in answer.text

    reveal = client.post(
        f"/api/conversations/{conversation['id']}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "view_question_answer",
            "question_id": card.question_id,
            "question_source_turn": source,
        },
    )
    assert reveal.status_code == 200, reveal.text
    assert SENTINEL in reveal.text

    answer = client.get(
        f"/api/conversations/{conversation['id']}/questions/{card.question_id}"
        f"/answer?build={build}"
    )
    assert answer.status_code == 200
    assert answer.headers["cache-control"] == "private, no-store"
    assert answer.headers["x-content-type-options"] == "nosniff"
    assert answer.json()["answer_detail"]["text"] == SENTINEL
    # 学习库仍不含答案正文。
    assert SENTINEL.encode() not in study_db_path.read_bytes()


def test_answer_endpoint_maps_changed_and_pruned_sources(
    client: TestClient, study_db_path: Path
) -> None:
    runtime = install_runtime(client)
    conversation = create_conversation(client)
    card = sample_card()
    source = add_search_turn(study_db_path, conversation["id"], card)
    client.post(
        f"/api/conversations/{conversation['id']}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "select_question",
            "question_id": card.question_id,
            "question_source_turn": source,
        },
    )
    reveal = client.post(
        f"/api/conversations/{conversation['id']}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "view_question_answer",
            "question_id": card.question_id,
            "question_source_turn": source,
        },
    )
    assert reveal.status_code == 200, reveal.text

    url = (
        f"/api/conversations/{conversation['id']}/questions/{card.question_id}"
        f"/answer?build={card.build_id}"
    )
    runtime.evidence_status = "source_changed"
    changed = client.get(url)
    assert changed.status_code == 409
    assert SENTINEL not in changed.text

    runtime.evidence_status = "source_pruned"
    pruned = client.get(url)
    assert pruned.status_code == 410
    assert SENTINEL not in pruned.text


def test_missing_answer_view_is_explicit_and_has_no_detail(
    client: TestClient, study_db_path: Path
) -> None:
    runtime = install_runtime(client)
    runtime.evidence_status = "missing_answer"
    conversation = create_conversation(client)
    card = sample_card()
    source = add_search_turn(study_db_path, conversation["id"], card)
    client.post(
        f"/api/conversations/{conversation['id']}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "select_question",
            "question_id": card.question_id,
            "question_source_turn": source,
        },
    )
    reveal = client.post(
        f"/api/conversations/{conversation['id']}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "view_question_answer",
            "question_id": card.question_id,
            "question_source_turn": source,
        },
    )
    assert reveal.status_code == 200, reveal.text
    event = parse_sse(reveal.text)[-1]
    assert event["question_result_status"] == "missing_answer"
    assert event["question"]["answer_detail"] is None
    assert "未提供" in event["question_result_message"]
    assert SENTINEL not in reveal.text


def test_question_action_requires_auto_kind_and_target() -> None:
    import pytest

    from app.api.conversations import TurnRequest

    with pytest.raises(ValueError):
        TurnRequest(
            request_id=str(uuid4()),
            kind="query",
            query="查题",
            question_id="py-2024-2025-s1-final-choice-1",
            question_source_turn=uuid4(),
            question_action="record",
        )
    with pytest.raises(ValueError):
        TurnRequest(
            request_id=str(uuid4()),
            kind="auto",
            query="",
            question_action="record",
        )


def test_candidate_asset_is_readable_before_selection_but_answer_is_not(
    client: TestClient, study_db_path: Path
) -> None:
    runtime = install_runtime(client)
    conversation = create_conversation(client)
    card = sample_card()
    add_search_turn(study_db_path, conversation["id"], card)

    asset = client.get(
        f"/api/conversations/{conversation['id']}/questions/{card.question_id}"
        f"/assets/asset-1?build={card.build_id}"
    )
    assert asset.status_code == 200
    assert runtime.asset_calls

    answer = client.get(
        f"/api/conversations/{conversation['id']}/questions/{card.question_id}"
        f"/answer?build={card.build_id}"
    )
    assert answer.status_code in (404, 409)
    assert SENTINEL not in answer.text


def test_asset_and_answer_require_membership_and_build(
    client: TestClient, study_db_path: Path
) -> None:
    install_runtime(client)
    conversation = create_conversation(client)
    card = sample_card()
    source = add_search_turn(study_db_path, conversation["id"], card)
    client.post(
        f"/api/conversations/{conversation['id']}/turns",
        json={
            "request_id": str(uuid4()),
            "kind": "select_question",
            "question_id": card.question_id,
            "question_source_turn": source,
        },
    )

    asset_url = (
        f"/api/conversations/{conversation['id']}/questions/{card.question_id}"
        f"/assets/asset-1?build={card.build_id}"
    )
    asset = client.get(asset_url)
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "private, no-store"
    assert asset.headers["x-content-type-options"] == "nosniff"

    wrong_build = client.get(asset_url.replace(card.build_id, "iv-ffffffffffffffff"))
    assert wrong_build.status_code == 409
    assert "版本" in wrong_build.json()["detail"]

    other = create_conversation(client)
    foreign = client.get(asset_url.replace(conversation["id"], other["id"]))
    assert foreign.status_code == 404

    answer = client.get(
        f"/api/conversations/{conversation['id']}/questions/{card.question_id}"
        f"/answer?build={card.build_id}"
    )
    assert answer.status_code == 409

    # 未选中的题目不能借资源接口探测存在性。
    missing = client.get(
        f"/api/conversations/{conversation['id']}/questions/"
        f"py-2024-2025-s1-final-choice-9/assets/asset-1?build={card.build_id}"
    )
    assert missing.status_code == 404
