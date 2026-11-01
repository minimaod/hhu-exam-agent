"""F009：学习记忆接口契约（200 / 404 / 422 / 503）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.papers.service import get_paper_service
from tests.f009_support import Seed
from tests.test_f009_evidence import FakePapers

DELETE_MESSAGE = "从学习记忆和记忆建议中移除"
DELETE_COPY = "从学习记忆和复习建议中移除，原会话记录保留。"


@pytest.fixture
def memory_client(client: TestClient) -> TestClient:
    """把试卷目录换成两张 Python 卷，让“课程来自登记试卷”可被验证。"""

    app.dependency_overrides[get_paper_service] = lambda: FakePapers()
    return client


def seed_python(db: Path) -> tuple[str, str]:
    seed = Seed(db)
    conversation = seed.conversation()
    problem = seed.problem(
        conversation,
        paper_id="py-2023-2024-s2-final",
        source_type="registered_paper",
    )
    exercise = seed.exercise(
        conversation,
        problem,
        knowledge_point="循环的条件与终止条件",
        reference="参考解答：绝不能出现在接口返回里的答案正文",
    )
    seed.attempt(exercise, correct=False)
    return conversation, exercise


def test_empty_course_returns_guidance(memory_client: TestClient) -> None:
    response = memory_client.get("/api/learning-memory")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["entries"] == []
    assert "先选一门课" in body["message"]


def test_invalid_query_parameters_are_422(memory_client: TestClient) -> None:
    assert memory_client.get("/api/learning-memory?days=0").status_code == 422
    assert memory_client.get("/api/learning-memory?limit=999").status_code == 422
    assert memory_client.get("/api/learning-memory?course=" + "x" * 80).status_code == 422


def test_summary_returns_entries_without_answers(
    memory_client: TestClient, study_db_path: Path
) -> None:
    conversation, exercise = seed_python(study_db_path)

    response = memory_client.get(
        "/api/learning-memory", params={"course": "Python语言程序设计"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["counts"]["evidence"] == 1
    assert body["counts"]["questions"] == 1
    assert body["entries"][0]["entry_id"] == f"exercise:{exercise}"
    assert body["entries"][0]["course"] == "Python语言程序设计"
    assert body["entries"][0]["verdict"] == "wrong_independent"
    assert body["entries"][0]["source"]["conversation_id"] == conversation
    assert body["groups"][0]["label"] == "循环的条件与终止条件"
    assert "参考解答" not in response.text
    assert "绝不能出现" not in response.text


def test_courses_endpoint_lists_known_and_pending(
    memory_client: TestClient, study_db_path: Path
) -> None:
    seed_python(study_db_path)
    seed = Seed(study_db_path)
    unknown = seed.conversation()
    seed.exercise(unknown, seed.problem(unknown), knowledge_point="杂项")

    response = memory_client.get("/api/learning-memory/courses")
    assert response.status_code == 200
    body = response.json()
    labels = {item["label"]: item["evidence"] for item in body["items"]}
    assert labels == {"Python语言程序设计": 1}
    assert body["pending"] == 1


def test_patch_corrects_course_and_reports_user_source(
    memory_client: TestClient, study_db_path: Path
) -> None:
    seed = Seed(study_db_path)
    conversation = seed.conversation()
    problem = seed.problem(conversation)
    exercise = seed.exercise(conversation, problem, knowledge_point="杂项")

    response = memory_client.patch(
        f"/api/learning-memory/exercise/{exercise}",
        json={"course": "Python语言程序设计", "note": "这是我当时记错的题"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["course"] == "Python语言程序设计"
    assert body["course_source"] == "user"
    assert body["note"] == "这是我当时记错的题"

    summary = memory_client.get(
        "/api/learning-memory", params={"course": "Python语言程序设计"}
    ).json()
    assert summary["counts"]["evidence"] == 1
    assert summary["counts"]["pending_course"] == 0


def test_patch_normalizes_alias_and_allows_returning_to_pending(
    memory_client: TestClient, study_db_path: Path
) -> None:
    seed = Seed(study_db_path)
    conversation = seed.conversation()
    problem = seed.problem(conversation)
    exercise = seed.exercise(conversation, problem, knowledge_point="循环")

    # 写入别名 “Python”，摘要按规范课程名也能查到。
    first = memory_client.patch(
        f"/api/learning-memory/exercise/{exercise}", json={"course": "Python"}
    )
    assert first.status_code == 200
    assert first.json()["course"] == "Python语言程序设计"

    summary = memory_client.get(
        "/api/learning-memory", params={"course": "Python语言程序设计"}
    ).json()
    assert summary["counts"]["evidence"] == 1

    # 留空课程 = 改回待确认，不能继续挂在原来的课程下。
    cleared = memory_client.patch(
        f"/api/learning-memory/exercise/{exercise}", json={"course": ""}
    )
    assert cleared.status_code == 200
    assert cleared.json()["course"] is None
    assert cleared.json()["course_source"] == "user"

    after = memory_client.get(
        "/api/learning-memory", params={"course": "Python语言程序设计"}
    ).json()
    assert after["counts"]["evidence"] == 0
    courses = memory_client.get("/api/learning-memory/courses").json()
    assert courses["items"] == []
    assert courses["pending"] == 1


def test_patch_rejects_attempt_and_unknown_entries(
    memory_client: TestClient, study_db_path: Path
) -> None:
    seed = Seed(study_db_path)
    conversation = seed.conversation()
    problem = seed.problem(conversation)
    exercise = seed.exercise(conversation, problem, knowledge_point="循环")
    attempt = seed.attempt(exercise, correct=False)

    unsupported = memory_client.patch(
        f"/api/learning-memory/attempt/{attempt}", json={"course": "高等数学"}
    )
    assert unsupported.status_code == 422
    assert "只能整体移除" in unsupported.json()["detail"]

    missing = memory_client.patch(
        "/api/learning-memory/exercise/00000000-0000-4000-8000-000000000000",
        json={"course": "高等数学"},
    )
    assert missing.status_code == 404
    assert "没有这条学习记忆" in missing.json()["detail"]

    empty = memory_client.patch(
        f"/api/learning-memory/exercise/{exercise}", json={}
    )
    assert empty.status_code == 422

    bad_kind = memory_client.patch(
        f"/api/learning-memory/session/{exercise}", json={"note": "x"}
    )
    assert bad_kind.status_code == 422


def test_delete_is_persistent_and_idempotent(
    memory_client: TestClient, study_db_path: Path
) -> None:
    _, exercise = seed_python(study_db_path)

    first = memory_client.delete(f"/api/learning-memory/exercise/{exercise}")
    assert first.status_code == 200
    body = first.json()
    assert body["removed"] is True
    assert body["message"] == DELETE_COPY
    assert body["entry_id"] == f"exercise:{exercise}"

    summary = memory_client.get(
        "/api/learning-memory", params={"course": "Python语言程序设计"}
    ).json()
    assert summary["counts"]["evidence"] == 0
    assert summary["counts"]["excluded"] == 1

    second = memory_client.delete(f"/api/learning-memory/exercise/{exercise}")
    assert second.status_code == 200
    assert second.json()["removed"] is False
    assert "之前已经移除" in second.json()["message"]

    missing = memory_client.delete(
        "/api/learning-memory/exercise/00000000-0000-4000-8000-000000000000"
    )
    assert missing.status_code == 404


def test_delete_does_not_touch_conversation_records(
    memory_client: TestClient, study_db_path: Path
) -> None:
    conversation, exercise = seed_python(study_db_path)
    assert memory_client.get(f"/api/conversations/{conversation}").status_code == 200

    memory_client.delete(f"/api/learning-memory/exercise/{exercise}")

    detail = memory_client.get(f"/api/conversations/{conversation}")
    assert detail.status_code == 200
    assert detail.json()["conversation"]["id"] == conversation
    with sqlite3.connect(study_db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM exercises WHERE id = ?", (exercise,)
        ).fetchone()[0] == 1


def test_storage_failure_maps_to_503(
    memory_client: TestClient, study_db_path: Path
) -> None:
    seed_python(study_db_path)
    with sqlite3.connect(study_db_path) as connection:
        connection.execute("DROP TABLE memory_exclusions")
        connection.commit()

    response = memory_client.get(
        "/api/learning-memory", params={"course": "Python语言程序设计"}
    )
    assert response.status_code == 503
    assert "暂时不可用" in response.json()["detail"]