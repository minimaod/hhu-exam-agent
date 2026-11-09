"""F012：实际路由清单、隐藏路由检查、公开接口与 A/B 隔离。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import app
from tests.f012_support import (
    TEST_CSRF_HEADERS,
    create_conversation,
    create_invite,
    owner_snapshot,
    register,
)

PUBLIC_ROUTES = {
    ("GET", "/api/health"),
    ("GET", "/api/papers"),
    ("GET", "/api/papers/{paper_id}/download"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
}

PRIVATE_ROUTES = {
    ("GET", "/api/me"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/query"),
    ("GET", "/api/conversations"),
    ("POST", "/api/conversations"),
    ("GET", "/api/conversations/{conversation_id}"),
    ("POST", "/api/conversations/{conversation_id}/turns"),
    ("POST", "/api/conversations/{conversation_id}/turns/{request_id}/reply"),
    ("POST", "/api/conversations/{conversation_id}/exercises/{exercise_id}/reveal"),
    ("GET", "/api/conversations/{conversation_id}/questions/{question_id}/source"),
    (
        "GET",
        "/api/conversations/{conversation_id}/questions/{question_id}/assets/{asset_id}",
    ),
    ("GET", "/api/conversations/{conversation_id}/questions/{question_id}/answer"),
    ("POST", "/api/conversations/{conversation_id}/attachments"),
    ("GET", "/api/conversations/{conversation_id}/attachments/{attachment_id}"),
    ("GET", "/api/mistake-records"),
    ("POST", "/api/mistake-records"),
    ("GET", "/api/mistake-records/{record_id}"),
    ("PATCH", "/api/mistake-records/{record_id}"),
    ("DELETE", "/api/mistake-records/{record_id}"),
    ("POST", "/api/mistake-records/{record_id}/corrections"),
    ("POST", "/api/mistake-records/{record_id}/adopt-problem"),
    ("GET", "/api/learning-memory"),
    ("GET", "/api/learning-memory/courses"),
    ("PATCH", "/api/learning-memory/{kind}/{source_id}"),
    ("DELETE", "/api/learning-memory/{kind}/{source_id}"),
}

ALL_ROUTES = PUBLIC_ROUTES | PRIVATE_ROUTES


def collect_api_operations(application: FastAPI) -> set[tuple[str, str]]:
    def walk(routes, prefix: str = ""):
        for route in routes:
            if isinstance(route, APIRoute):
                for method in route.methods:
                    yield prefix + route.path, method.upper()
                continue
            router = getattr(route, "original_router", None)
            if router is not None:
                context = getattr(route, "include_context", None)
                yield from walk(
                    router.routes,
                    prefix + (getattr(context, "prefix", "") if context else ""),
                )

    return {
        (method, path)
        for path, method in walk(application.routes)
        if path.startswith("/api/")
    }


def test_actual_route_matrix_matches_plan() -> None:
    actual = collect_api_operations(app)
    assert len(actual) == 30
    assert actual == ALL_ROUTES


def test_hidden_route_is_seen_and_reported_missing() -> None:
    probe = FastAPI()
    router = APIRouter(prefix="/api")

    @router.get("/_hidden_probe", include_in_schema=False)
    def hidden_probe() -> dict[str, bool]:
        return {"ok": True}

    probe.include_router(router)
    found = collect_api_operations(probe)
    assert ("GET", "/api/_hidden_probe") in found
    assert found - (PUBLIC_ROUTES | PRIVATE_ROUTES)


def test_private_routes_require_login(anon_client: TestClient) -> None:
    for method, path in sorted(PRIVATE_ROUTES):
        if "/{request_id}" in path:
            path = path.replace("{request_id}", "11111111-1111-4111-8111-111111111111")
        path = path.replace("{conversation_id}", "11111111-1111-4111-8111-111111111111")
        path = path.replace("{exercise_id}", "11111111-1111-4111-8111-111111111111")
        path = path.replace("{attachment_id}", "11111111-1111-4111-8111-111111111111")
        path = path.replace("{record_id}", "11111111-1111-4111-8111-111111111111")
        path = path.replace("{source_id}", "11111111-1111-4111-8111-111111111111")
        path = path.replace("{question_id}", "q-1")
        path = path.replace("{asset_id}", "a-1")
        path = path.replace("{kind}", "exercise")
        body = {} if method in {"POST", "PATCH"} else None
        response = anon_client.request(method, path, json=body)
        assert response.status_code == 401, (method, path, response.text)


def test_public_papers_are_downloadable_without_login(
    sample_root: Path,
) -> None:
    from app.papers.repository import PaperRepository
    from app.papers.service import PaperService, get_paper_service

    app.dependency_overrides[get_paper_service] = lambda: PaperService(
        PaperRepository(sample_root)
    )
    try:
        with TestClient(app) as public:
            listing = public.get("/api/papers")
            assert listing.status_code == 200
            items = listing.json()["items"]
            assert items
            for item in items:
                download = public.get(f"/api/papers/{item['id']}/download")
                if download.status_code == 200:
                    assert download.headers["content-type"].startswith(
                        "application/pdf"
                    )
                    assert download.content.startswith(b"%PDF")
                    return
            pytest.fail("样例目录里的试卷都无法下载")
    finally:
        app.dependency_overrides.pop(get_paper_service, None)


def _seed_a_world(db: Path, owner_id: str) -> dict[str, str]:
    now = "2026-11-01T00:00:00+00:00"
    ids = {
        "conversation": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "problem": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "exercise": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "attempt": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "record": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    }
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO conversations (id,title,created_at,updated_at,owner_id) VALUES (?,?,?,?,?)",
            (ids["conversation"], "A 的会话", now, now, owner_id),
        )
        connection.execute(
            """
            INSERT INTO problems (
                id, conversation_id, root_problem_id, version, statement_text,
                status, clarity, source_type, active, created_at, updated_at
            ) VALUES (?,?,?,?,?, 'explained','clear','student_text',1,?,?)
            """,
            (
                ids["problem"],
                ids["conversation"],
                ids["problem"],
                1,
                "A 的题目",
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO exercises (
                id, conversation_id, problem_id, root_problem_id, statement_text,
                reference_solution_text, knowledge_point, status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?, 'submitted',?,?)
            """,
            (
                ids["exercise"],
                ids["conversation"],
                ids["problem"],
                ids["problem"],
                "A 的练习",
                "参考解答",
                "循环",
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO exercise_attempts (
                id, exercise_id, attempt_no, kind, student_answer_text,
                answered_after_revealed, created_at, updated_at
            ) VALUES (?,?,1,'initial','A 的答案',0,?,?)
            """,
            (ids["attempt"], ids["exercise"], now, now),
        )
        connection.execute(
            """
            INSERT INTO mistake_records (
                id, conversation_id, problem_id, root_problem_id, problem_version,
                request_id, title, statement_text, source_json, created_at, updated_at
            ) VALUES (?,?,?,?,1,?,?,?, '{}',?,?)
            """,
            (
                ids["record"],
                ids["conversation"],
                ids["problem"],
                ids["problem"],
                "req-a",
                "A 的错题",
                "A 的题目",
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return ids


def test_a_b_isolation_on_owned_routes(anon_client: TestClient, sample_root: Path) -> None:
    db = anon_client.app.state.conversation_store.db_path
    assert db is not None
    with TestClient(app) as a, TestClient(app) as b:
        a_user = register(a, "alice", invite_code=create_invite(db))
        b_user = register(b, "bob", invite_code=create_invite(db))
        a_conversation = create_conversation(a)
        b_conversation = create_conversation(b)
        world = _seed_a_world(db, a_user["id"])

        # A 上传一张真实 PNG，供跨用户读取测试。
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
            b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        upload = a.post(
            f"/api/conversations/{a_conversation}/attachments",
            files={"file": ("a.png", png, "image/png")},
            headers=TEST_CSRF_HEADERS,
        )
        assert upload.status_code == 201, upload.text
        attachment = upload.json()["id"]

        snapshot = owner_snapshot(db, a_user["id"])
        a_req = "11111111-1111-4111-8111-111111111111"
        probes = [
            ("GET", f"/api/conversations/{a_conversation}", None),
            ("POST", f"/api/conversations/{a_conversation}/turns", {"request_id": a_req, "query": "hi"}),
            ("POST", f"/api/conversations/{a_conversation}/turns/{a_req}/reply", None),
            ("POST", f"/api/conversations/{a_conversation}/exercises/{world['exercise']}/reveal", None),
            ("GET", f"/api/conversations/{a_conversation}/questions/q-1/source?build=b&kind=statement&page=1", None),
            ("GET", f"/api/conversations/{a_conversation}/questions/q-1/assets/a-1?build=b", None),
            ("GET", f"/api/conversations/{a_conversation}/questions/q-1/answer?build=b", None),
            ("GET", f"/api/conversations/{a_conversation}/attachments/{attachment}", None),
            ("GET", f"/api/mistake-records/{world['record']}", None),
            ("PATCH", f"/api/mistake-records/{world['record']}", {}),
            ("DELETE", f"/api/mistake-records/{world['record']}", None),
            ("POST", f"/api/mistake-records/{world['record']}/corrections", {"request_id": a_req, "statement_text": "x"}),
            ("POST", f"/api/mistake-records/{world['record']}/adopt-problem", {"request_id": a_req, "problem_id": world["problem"]}),
            ("PATCH", f"/api/learning-memory/exercise/{world['exercise']}", {"note": "x"}),
            ("DELETE", f"/api/learning-memory/exercise/{world['exercise']}", None),
        ]
        for method, path, body in probes:
            response = b.request(
                method, path, json=body, headers=TEST_CSRF_HEADERS
            )
            assert response.status_code == 404, (method, path, response.status_code, response.text)
        # B 自己的会话里引用 A 的附件/练习/题目
        assert b.post(
            f"/api/conversations/{b_conversation}/turns",
            json={
                "request_id": a_req,
                "query": "带图",
                "attachments": [{"id": attachment, "role": "statement"}],
            },
            headers=TEST_CSRF_HEADERS,
        ).status_code == 404
        assert b.post(
            f"/api/conversations/{b_conversation}/turns",
            json={"request_id": "22222222-2222-4222-8222-222222222222", "kind": "exercise_answer", "exercise_id": world["exercise"], "student_answer": "B 的答案"},
            headers=TEST_CSRF_HEADERS,
        ).status_code == 404
        assert b.get("/api/mistake-records").json()["total"] == 0
        assert b.get("/api/conversations").json()["total"] == 1
        assert owner_snapshot(db, a_user["id"])["digest"] == snapshot["digest"]
        # A 仍然可读自己的数据
        assert a.get(f"/api/conversations/{a_conversation}").status_code == 200


def test_same_public_question_selection_is_independent(anon_client: TestClient) -> None:
    db = anon_client.app.state.conversation_store.db_path
    assert db is not None
    with TestClient(app) as a, TestClient(app) as b:
        register(a, "alice", invite_code=create_invite(db))
        register(b, "bob", invite_code=create_invite(db))
        a_conv = create_conversation(a)
        b_conv = create_conversation(b)
        assert a_conv != b_conv
        # 公开 question_id 本身不是用户数据；两个会话的选中记录互不可见。
        assert a.get(f"/api/conversations/{a_conv}").json()["active_question"] is None
        assert b.get(f"/api/conversations/{b_conv}").json()["active_question"] is None