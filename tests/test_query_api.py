"""POST /api/query 的 HTTP 与 SSE 契约。

替身模型保证分支可重复；样例目录只用于“高数命中样例两份”的端到端形状检查。
所有异常用例都在临时目录构造，不触碰用户的原始 PDF。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.query import get_query_service
from app.main import app
import app.config as query_config
from app.papers.repository import PaperRepository
from app.papers.service import PaperService
from app.querying.errors import QueryError
from app.querying.service import QueryService
from tests.fakes import FakeUnderstander, parse_sse, understanding


@pytest.fixture
def override_service(monkeypatch: pytest.MonkeyPatch) -> Iterator:
    """按用例注入 QueryService；结束后只移除本依赖替换。

    配置依赖仍会执行（但不会构造真实模型），所以补一个假密钥让配置有效。
    """

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    clients: list[TestClient] = []

    def _make(service: QueryService) -> TestClient:
        app.dependency_overrides[get_query_service] = lambda: service
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    try:
        yield _make
    finally:
        for client in clients:
            client.__exit__(None, None, None)
        app.dependency_overrides.pop(get_query_service, None)


def build_service(
    understander: FakeUnderstander,
    papers_root: Path,
) -> QueryService:
    return QueryService(
        understand=understander,
        papers=PaperService(PaperRepository(papers_root)),
    )


def post_query(client: TestClient, query: str = "我要复习高数"):
    return client.post("/api/query", json={"query": query})


def test_missing_key_returns_503_before_stream() -> None:
    with TestClient(app) as client:
        response = post_query(client)

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert "DEEPSEEK_API_KEY" in response.json()["detail"]
    assert "sk-" not in response.text


@pytest.mark.parametrize(
    "base_url",
    [
        "http://[broken",
        "http://example.test:bad",
        "ftp://example.test",
    ],
)
def test_invalid_base_url_returns_503(
    base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", base_url)

    with TestClient(app) as client:
        response = post_query(client)

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert "DEEPSEEK_BASE_URL" in response.json()["detail"]


def test_invalid_env_file_returns_503_and_keeps_features(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"DEEPSEEK_API_KEY=\xff\xfe\n")
    monkeypatch.setattr(query_config, "ENV_FILE", env_file)

    with TestClient(app) as client:
        response = post_query(client)
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/json")
        assert "配置" in response.json()["detail"]
        assert "sk-" not in response.text
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/papers").status_code == 200


def test_startup_uses_environment_over_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setattr(query_config, "ENV_FILE", env_file)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")

    with TestClient(app):
        assert app.state.query_config.settings is not None
        assert app.state.query_config.settings.api_key == "env-key"


def test_config_snapshot_changes_only_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=first\n", encoding="utf-8")
    monkeypatch.setattr(query_config, "ENV_FILE", env_file)

    with TestClient(app):
        assert app.state.query_config.settings is not None
        assert app.state.query_config.settings.api_key == "first"

        env_file.write_text("DEEPSEEK_API_KEY=second\n", encoding="utf-8")
        # 同一个启动快照不随文件变化。
        assert app.state.query_config.settings.api_key == "first"

    # 新的 lifespan 相当于重启后端，才会读到新值。
    with TestClient(app):
        assert app.state.query_config.settings is not None
        assert app.state.query_config.settings.api_key == "second"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "高" * 1001},
        {"query": 123},
    ],
)
def test_invalid_request_returns_422(payload: dict) -> None:
    with TestClient(app) as client:
        response = client.post("/api/query", json=payload)

    assert response.status_code == 422


def test_found_result_streams_progress_and_terminal(
    sample_root: Path,
    override_service,
) -> None:
    service = build_service(FakeUnderstander([understanding()]), sample_root)
    client = override_service(service)

    response = post_query(client)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"

    events = parse_sse(response.text)
    assert [event["type"] for event in events] == ["progress", "progress", "result"]

    result = events[-1]
    assert result["kind"] == "found"
    assert result["total"] == 2
    assert result["filters"] == {"course": "高等数学", "exam_type": None}
    assert [item["id"] for item in result["items"]] == [
        "sample-final",
        "sample-midterm",
    ]
    assert all("file_path" not in item for item in result["items"])
    assert all(item["download_url"].startswith("/api/papers/") for item in result["items"])
    assert "2 份" in result["message"]


def test_clarification_skips_retrieving_progress(
    sample_root: Path,
    override_service,
) -> None:
    service = build_service(
        FakeUnderstander(
            [understanding(action="clarify", course=None, reason="missing_course")]
        ),
        sample_root,
    )
    client = override_service(service)

    events = parse_sse(post_query(client, "我要复习").text)

    assert [event["type"] for event in events] == ["progress", "result"]
    assert events[-1]["kind"] == "clarification"
    assert events[-1]["filters"] is None
    assert events[-1]["items"] == []
    assert events[-1]["total"] == 0


def test_unknown_course_returns_empty(
    sample_root: Path,
    override_service,
) -> None:
    service = build_service(
        FakeUnderstander([understanding(course="大学化学")]),
        sample_root,
    )
    client = override_service(service)

    events = parse_sse(post_query(client, "找大学化学试卷").text)

    assert events[-1]["kind"] == "empty"
    assert events[-1]["total"] == 0
    assert events[-1]["filters"] == {"course": "大学化学", "exam_type": None}


def test_unsupported_returns_result_without_filters(
    sample_root: Path,
    override_service,
) -> None:
    service = build_service(
        FakeUnderstander(
            [
                understanding(
                    action="unsupported",
                    course="高数",
                    reason="unsupported_constraint",
                )
            ]
        ),
        sample_root,
    )
    client = override_service(service)

    events = parse_sse(post_query(client, "只要 2024 年的高数试卷").text)

    assert events[-1]["kind"] == "unsupported"
    assert events[-1]["filters"] is None
    assert "年份" in events[-1]["message"]


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (QueryError("MODEL_TIMEOUT", "理解需求超时，请重试。"), "MODEL_TIMEOUT"),
        (QueryError("MODEL_AUTH_FAILED", "认证失败"), "MODEL_AUTH_FAILED"),
        ("not json", "MODEL_RESPONSE_INVALID"),
    ],
)
def test_model_failures_become_error_event(
    error,
    expected_code: str,
    sample_root: Path,
    override_service,
) -> None:
    service = build_service(FakeUnderstander([error]), sample_root)
    client = override_service(service)

    events = parse_sse(post_query(client).text)

    assert [event["type"] for event in events] == ["progress", "error"]
    assert events[-1]["code"] == expected_code
    assert "result" not in [event["type"] for event in events]


def test_catalog_failure_is_error_not_empty(
    tmp_path: Path,
    override_service,
) -> None:
    broken_root = tmp_path / "broken"
    broken_root.mkdir()
    service = build_service(FakeUnderstander([understanding()]), broken_root)
    client = override_service(service)

    events = parse_sse(post_query(client).text)

    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "CATALOG_UNAVAILABLE"
    assert "目录" in events[-1]["message"]


def test_only_one_terminal_event(
    sample_root: Path,
    override_service,
) -> None:
    service = build_service(FakeUnderstander([understanding()]), sample_root)
    client = override_service(service)

    events = parse_sse(post_query(client).text)
    terminals = [event for event in events if event["type"] in ("result", "error")]

    assert len(terminals) == 1
    assert terminals[0] is events[-1]


def test_response_has_no_key_or_disk_path(
    sample_root: Path,
    override_service,
) -> None:
    service = build_service(FakeUnderstander([understanding()]), sample_root)
    client = override_service(service)

    text = post_query(client).text

    assert "sk-" not in text
    assert str(sample_root) not in text
    assert "file_path" not in text


def test_without_key_existing_features_still_work(real_client: TestClient) -> None:
    assert post_query(real_client).status_code == 503
    assert real_client.get("/api/health").status_code == 200
    assert real_client.get("/api/papers").status_code == 200
