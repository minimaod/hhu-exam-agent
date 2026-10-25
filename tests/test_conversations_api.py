"""会话管理、轮次提交与恢复的 HTTP / SSE 契约。

模型一律使用 FakeUnderstander；样例试卷目录与临时 SQLite 由夹具提供，
不读写项目真实的 data/local 或 data/papers 数据。
"""

import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.conversations.repository as conversation_repository
from app.api.conversations import (
    ConversationServiceState,
    get_conversation_service_state,
)
from app.conversations.errors import ConversationStorageError
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.tutoring.models import Source
from app.main import app
from app.papers.repository import PaperRepository
from app.papers.service import PaperService
from app.querying.errors import QueryError
from app.querying.service import QueryService
from tests.fakes import FakeUnderstander, parse_sse, understanding


REQUEST_ONE = "11111111-1111-4111-8111-111111111111"
REQUEST_TWO = "22222222-2222-4222-8222-222222222222"
REQUEST_THREE = "33333333-3333-4333-8333-333333333333"
REQUEST_FOUR = "44444444-4444-4444-8444-444444444444"
REQUEST_FIVE = "55555555-5555-4555-8555-555555555555"


class NeverDisconnected:
    async def is_disconnected(self) -> bool:
        return False


@pytest.fixture
def install_service(
    sample_root: Path,
    study_db_path: Path,
) -> object:
    """把会话依赖替换成“临时数据库 + 模型替身”的服务。"""

    def _install(
        understander: FakeUnderstander,
        papers_root: Path | None = None,
    ) -> ConversationService:
        repository = ConversationRepository(study_db_path)
        repository.initialize()
        papers = PaperService(PaperRepository(papers_root or sample_root))

        def make_query() -> QueryService:
            return QueryService(understand=understander, papers=papers)

        service = ConversationService(
            repository=repository,
            make_query=make_query,
            query_error=None,
        )
        app.dependency_overrides[get_conversation_service_state] = lambda: (
            ConversationServiceState(service=service, error=None)
        )
        return service

    yield _install
    app.dependency_overrides.pop(get_conversation_service_state, None)


def create_conversation(client: TestClient) -> dict:
    response = client.post("/api/conversations", json={})
    assert response.status_code == 201, response.text
    return response.json()


def post_turn(
    client: TestClient,
    conversation_id: str,
    request_id: str,
    query: str = "我要复习高数",
):
    return client.post(
        f"/api/conversations/{conversation_id}/turns",
        json={"request_id": request_id, "query": query},
    )


def query_turn_input(request_id: str, query: str) -> TurnInput:
    """F004 语义的查询输入：不指定 kind，不触发意图模型。"""

    return TurnInput(
        request_id=request_id,
        kind=None,
        text=query,
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(type="none"),
        problem_id=None,
        start_new=False,
    )


def test_conversation_management_contract() -> None:
    with TestClient(app) as client:
        created = create_conversation(client)
        assert created["title"] == "新会话"
        assert created["created_at"]
        assert created["updated_at"]

        listing = client.get("/api/conversations")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["items"][0]["id"] == created["id"]

        detail = client.get(f"/api/conversations/{created['id']}")
        assert detail.status_code == 200
        assert detail.json()["conversation"]["id"] == created["id"]
        assert detail.json()["turns"] == []

        assert client.get(f"/api/conversations/{uuid4()}").status_code == 404
        assert client.get("/api/conversations/not-a-uuid").status_code == 422

        assert client.post("/api/conversations", json={"extra": 1}).status_code == 422
        assert client.post("/api/conversations").status_code == 422

        assert (
            post_turn(client, created["id"], "not-a-uuid").status_code == 422
        )
        assert (
            post_turn(client, created["id"], REQUEST_ONE, "   ").status_code == 422
        )
        assert (
            post_turn(client, created["id"], REQUEST_ONE, "高" * 1001).status_code
            == 422
        )
        assert (
            post_turn(client, str(uuid4()), REQUEST_ONE).status_code == 404
        )


def test_turn_is_streamed_saved_and_restored(install_service) -> None:
    understander = FakeUnderstander(
        [
            understanding(exam_type="final"),
            understanding(
                action="clarify",
                course=None,
                reason="missing_course",
            ),
        ]
    )
    service = install_service(understander)

    with TestClient(app) as client:
        conversation = create_conversation(client)

        response = post_turn(client, conversation["id"], REQUEST_ONE)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(response.text)
        assert [event["type"] for event in events] == [
            "progress",
            "progress",
            "result",
        ]
        assert all(event["conversation_id"] == conversation["id"] for event in events)
        assert all(event["request_id"] == REQUEST_ONE for event in events)
        assert events[-1]["result"]["kind"] == "found"
        assert events[-1]["result"]["total"] == 1

        second = parse_sse(
            post_turn(client, conversation["id"], REQUEST_TWO, "我要复习").text
        )
        assert second[-1]["type"] == "result"
        assert second[-1]["result"]["kind"] == "clarification"

        detail = client.get(f"/api/conversations/{conversation['id']}").json()
        assert detail["conversation"]["title"] == "我要复习高数"
        assert [turn["request_id"] for turn in detail["turns"]] == [
            REQUEST_ONE,
            REQUEST_TWO,
        ]
        assert [turn["status"] for turn in detail["turns"]] == [
            "completed",
            "completed",
        ]
        assert [turn["result"]["kind"] for turn in detail["turns"]] == [
            "found",
            "clarification",
        ]
        assert [turn["seq"] for turn in detail["turns"]] == [1, 2]
        assert service is not None


@pytest.mark.parametrize(
    ("results", "expected_kind"),
    [
        ([understanding()], "found"),
        ([understanding(course="大学化学")], "empty"),
        (
            [understanding(action="clarify", course=None, reason="missing_course")],
            "clarification",
        ),
        (
            [
                understanding(
                    action="unsupported",
                    course="高数",
                    reason="unsupported_constraint",
                )
            ],
            "unsupported",
        ),
    ],
)
def test_four_business_results_are_saved(
    install_service,
    results: list,
    expected_kind: str,
) -> None:
    install_service(FakeUnderstander(results))

    with TestClient(app) as client:
        conversation = create_conversation(client)
        events = parse_sse(post_turn(client, conversation["id"], REQUEST_ONE).text)
        assert events[-1]["result"]["kind"] == expected_kind

        stored = client.get(f"/api/conversations/{conversation['id']}").json()[
            "turns"
        ][0]
        assert stored["status"] == "completed"
        assert stored["result"]["kind"] == expected_kind
        assert stored["error"] is None


def test_model_error_is_saved(install_service) -> None:
    install_service(
        FakeUnderstander(
            [QueryError("MODEL_TIMEOUT", "理解需求超时，请重试。")]
        )
    )

    with TestClient(app) as client:
        conversation = create_conversation(client)
        events = parse_sse(post_turn(client, conversation["id"], REQUEST_ONE).text)

        assert [event["type"] for event in events] == ["progress", "error"]
        assert events[-1]["code"] == "MODEL_TIMEOUT"

        stored = client.get(f"/api/conversations/{conversation['id']}").json()[
            "turns"
        ][0]
        assert stored["status"] == "failed"
        assert stored["error"] == {
            "code": "MODEL_TIMEOUT",
            "message": "理解需求超时，请重试。",
        }
        assert stored["result"] is None


def test_duplicate_request_replays_without_model(install_service) -> None:
    understander = FakeUnderstander([understanding(), understanding()])
    install_service(understander)

    with TestClient(app) as client:
        conversation = create_conversation(client)

        first = parse_sse(post_turn(client, conversation["id"], REQUEST_ONE).text)
        assert first[-1]["type"] == "result"

        replay = post_turn(
            client,
            conversation["id"],
            REQUEST_ONE,
            "我要复习高数",
        )
        assert replay.status_code == 200
        replay_events = parse_sse(replay.text)
        assert [event["type"] for event in replay_events] == ["result"]
        assert replay_events[0]["result"] == first[-1]["result"]

        # 新的 request_id 才是新的执行轮次。
        fresh = parse_sse(
            post_turn(client, conversation["id"], REQUEST_TWO).text
        )
        assert fresh[-1]["type"] == "result"
        assert len(understander.queries) == 2


def test_replay_of_failed_turn_returns_saved_error(install_service) -> None:
    install_service(
        FakeUnderstander(
            [
                QueryError("MODEL_RATE_LIMITED", "模型服务当前限流，请稍后重试。"),
            ]
        )
    )

    with TestClient(app) as client:
        conversation = create_conversation(client)
        first = parse_sse(post_turn(client, conversation["id"], REQUEST_ONE).text)
        assert first[-1]["type"] == "error"

        replay = parse_sse(
            post_turn(client, conversation["id"], REQUEST_ONE).text
        )
        assert [event["type"] for event in replay] == ["error"]
        assert replay[0]["code"] == "MODEL_RATE_LIMITED"


def test_same_request_id_with_different_query_conflicts(install_service) -> None:
    install_service(FakeUnderstander([understanding()]))

    with TestClient(app) as client:
        conversation = create_conversation(client)
        assert post_turn(client, conversation["id"], REQUEST_ONE).status_code == 200

        response = post_turn(
            client,
            conversation["id"],
            REQUEST_ONE,
            "找大学物理期末试卷",
        )
        assert response.status_code == 409
        assert "request_id" in response.json()["detail"]


def test_running_turn_conflicts(install_service) -> None:
    install_service(FakeUnderstander([understanding()]))

    with TestClient(app) as client:
        conversation = create_conversation(client)
        repository = ConversationRepository(_db_path_for(client))
        repository.start_turn(
            conversation["id"],
            REQUEST_TWO,
            "已经在执行的请求",
        )

        response = post_turn(client, conversation["id"], REQUEST_ONE)
        assert response.status_code == 409
        assert "正在执行" in response.json()["detail"]


def test_interrupted_duplicate_conflicts(install_service) -> None:
    install_service(FakeUnderstander([understanding()]))

    with TestClient(app) as client:
        conversation = create_conversation(client)
        repository = ConversationRepository(_db_path_for(client))
        repository.start_turn(conversation["id"], REQUEST_ONE, "我要复习高数")
        repository.interrupt_turn(conversation["id"], REQUEST_ONE)

        response = post_turn(client, conversation["id"], REQUEST_ONE)
        assert response.status_code == 409
        assert "已中断" in response.json()["detail"]


def test_management_works_without_model_key_but_new_turn_returns_503(
    study_db_path: Path,
) -> None:
    with TestClient(app) as client:
        conversation = create_conversation(client)
        assert client.get("/api/conversations").status_code == 200
        assert (
            client.get(f"/api/conversations/{conversation['id']}").status_code
            == 200
        )

        response = post_turn(client, conversation["id"], REQUEST_ONE)
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/json")
        assert "DEEPSEEK_API_KEY" in response.json()["detail"]
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/papers").status_code == 200

    assert study_db_path.is_file()


def test_save_failure_emits_storage_error_without_result(
    install_service,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = install_service(FakeUnderstander([understanding()]))

    def fail_save(*args: object, **kwargs: object) -> None:
        raise ConversationStorageError("写不进去")

    monkeypatch.setattr(service._repository, "complete_turn", fail_save)

    with TestClient(app) as client:
        conversation = create_conversation(client)
        events = parse_sse(post_turn(client, conversation["id"], REQUEST_ONE).text)

        assert "result" not in [event["type"] for event in events]
        assert events[-1]["type"] == "error"
        assert events[-1]["code"] == "STORAGE_UNAVAILABLE"

        stored = client.get(f"/api/conversations/{conversation['id']}").json()[
            "turns"
        ][0]
        assert stored["status"] == "interrupted"


def test_final_result_is_committed_before_it_is_sent(
    install_service,
    study_db_path: Path,
) -> None:
    service = install_service(FakeUnderstander([understanding()]))
    conversation = service.create_conversation()

    async def scenario() -> None:
        prepared = service.prepare_turn(
            conversation.id,
            query_turn_input(REQUEST_ONE, "我要复习高数"),
        )
        repository = ConversationRepository(study_db_path)
        async for event in service.stream_prepared(prepared, NeverDisconnected()):
            if event.type == "result":
                stored = repository.find_turn(conversation.id, REQUEST_ONE)
                assert stored is not None
                assert stored.status == "completed"
                assert stored.result is not None
                assert stored.result.kind == "found"

    asyncio.run(scenario())


def test_stopping_marks_undone_turn_interrupted(
    install_service,
    study_db_path: Path,
) -> None:
    service = install_service(FakeUnderstander([understanding()]))
    conversation = service.create_conversation()

    async def scenario() -> None:
        prepared = service.prepare_turn(
            conversation.id,
            query_turn_input(REQUEST_ONE, "我要复习高数"),
        )
        stream = service.stream_prepared(prepared, NeverDisconnected())
        first = await anext(stream)
        assert first.type == "progress"
        await stream.aclose()

    asyncio.run(scenario())

    stored = ConversationRepository(study_db_path).find_turn(
        conversation.id,
        REQUEST_ONE,
    )
    assert stored is not None
    assert stored.status == "interrupted"
    assert stored.result is None


def test_startup_marks_stale_running_turn_interrupted(study_db_path: Path) -> None:
    repository = ConversationRepository(study_db_path)
    repository.initialize()
    conversation = repository.create_conversation()
    repository.start_turn(conversation.id, REQUEST_ONE, "我要复习高数")

    with TestClient(app) as client:
        detail = client.get(f"/api/conversations/{conversation.id}").json()

    assert detail["turns"][0]["status"] == "interrupted"
    assert detail["turns"][0]["finished_at"] is not None
    assert detail["turns"][0]["result"] is None


def assert_invalid_conversation_requests_are_422(client: TestClient) -> None:
    """存储不可用时，参数/请求体校验仍必须先返回 422。"""

    assert client.post("/api/conversations", json={"extra": 1}).status_code == 422
    assert client.get("/api/conversations/not-a-uuid").status_code == 422
    assert (
        client.post(
            f"/api/conversations/{uuid4()}/turns",
            json={"request_id": "bad", "query": ""},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/conversations/{uuid4()}/turns",
            json={"request_id": REQUEST_ONE, "query": "   "},
        ).status_code
        == 422
    )


def test_corrupt_database_returns_503_and_keeps_file(study_db_path: Path) -> None:
    payload = b"this is not a sqlite database"
    study_db_path.write_bytes(payload)

    with TestClient(app) as client:
        assert client.get("/api/conversations").status_code == 503
        assert client.post("/api/conversations", json={}).status_code == 503
        assert_invalid_conversation_requests_are_422(client)
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/papers").status_code == 200

    assert study_db_path.read_bytes() == payload


def test_unopenable_database_returns_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "db_is_a_directory"
    directory.mkdir()
    monkeypatch.setattr(
        conversation_repository,
        "DEFAULT_DB_PATH",
        directory,
    )

    with TestClient(app) as client:
        assert client.get("/api/conversations").status_code == 503
        assert_invalid_conversation_requests_are_422(client)
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/papers").status_code == 200


def test_list_order_and_turn_order_are_stable(study_db_path: Path) -> None:
    with TestClient(app) as client:
        first = create_conversation(client)
        second = create_conversation(client)

        with sqlite3.connect(study_db_path) as connection:
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00+00:00", first["id"]),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                ("2026-02-01T00:00:00+00:00", second["id"]),
            )
            connection.commit()

        listing = client.get("/api/conversations").json()
        assert [item["id"] for item in listing["items"]] == [
            second["id"],
            first["id"],
        ]

        with sqlite3.connect(study_db_path) as connection:
            connection.execute(
                "UPDATE conversations SET updated_at = ?",
                ("2026-03-01T00:00:00+00:00",),
            )
            connection.commit()

        first_order = [
            item["id"] for item in client.get("/api/conversations").json()["items"]
        ]
        second_order = [
            item["id"] for item in client.get("/api/conversations").json()["items"]
        ]
        assert first_order == second_order


def test_turn_response_has_no_key_or_disk_path(
    install_service,
    sample_root: Path,
) -> None:
    install_service(FakeUnderstander([understanding()]))

    with TestClient(app) as client:
        conversation = create_conversation(client)
        response = post_turn(client, conversation["id"], REQUEST_ONE)

    assert "sk-" not in response.text
    assert str(sample_root) not in response.text
    assert "file_path" not in response.text


def _db_path_for(client: TestClient) -> Path:
    """读取当前测试实际使用的隔离数据库路径。"""

    repository = app.state.conversation_store.repository
    assert repository is not None
    return repository.db_path


def test_corrupt_stored_snapshot_returns_503(install_service, study_db_path: Path) -> None:
    install_service(FakeUnderstander([understanding()]))

    with TestClient(app) as client:
        conversation = create_conversation(client)
        assert post_turn(client, conversation["id"], REQUEST_ONE).status_code == 200

        with sqlite3.connect(study_db_path) as connection:
            connection.execute(
                "UPDATE conversation_turns SET result_json = ?",
                ("{not json",),
            )
            connection.commit()

        detail = client.get(f"/api/conversations/{conversation['id']}")
        assert detail.status_code == 503
        assert "记录" in detail.json()["detail"]

        with sqlite3.connect(study_db_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM conversation_turns"
            ).fetchone()[0]
        assert count == 1
