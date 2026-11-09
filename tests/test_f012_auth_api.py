"""F012：注册、登录、退出、邀请码与 CSRF。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth.errors import InviteInvalidError
from app.auth.passwords import hash_password
from app.auth.repository import AuthRepository
from app.conversations import schema
from tests.f012_support import (
    TEST_CSRF_HEADERS,
    TEST_PASSWORD,
    create_invite,
    login,
    register,
)


def _db_for(client: TestClient) -> Path:
    from app.main import app

    path = app.state.conversation_store.db_path
    assert path is not None
    return path


def test_register_sets_cookie_and_logs_in(anon_client: TestClient) -> None:
    db = _db_for(anon_client)
    code = create_invite(db)
    response = anon_client.post(
        "/api/auth/register",
        json={"invite_code": code, "username": "alice", "password": TEST_PASSWORD},
        headers=TEST_CSRF_HEADERS,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["username"] == "alice"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie or "SameSite=Lax" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=" in cookie
    assert anon_client.get("/api/me").status_code == 200

    connection = sqlite3.connect(db)
    try:
        stored = connection.execute(
            "SELECT password_hash FROM users WHERE username = 'alice'"
        ).fetchone()[0]
        token = connection.execute(
            "SELECT token_sha256 FROM login_sessions"
        ).fetchone()[0]
    finally:
        connection.close()
    assert stored != TEST_PASSWORD
    assert stored.startswith("$argon2")
    assert cookie.split("hhu_session=", 1)[1].split(";", 1)[0] != token


def test_login_failure_is_generic_and_disable_revokes_sessions(
    anon_client: TestClient,
) -> None:
    db = _db_for(anon_client)
    register(anon_client, "alice", invite_code=create_invite(db))
    assert anon_client.get("/api/me").status_code == 200

    bad = anon_client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "wrong-password"},
        headers=TEST_CSRF_HEADERS,
    )
    missing = anon_client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "wrong-password"},
        headers=TEST_CSRF_HEADERS,
    )
    assert bad.status_code == missing.status_code == 401
    assert bad.json()["detail"] == missing.json()["detail"]

    AuthRepository(db).disable_user("alice")
    assert anon_client.get("/api/me").status_code == 401
    disabled_login = anon_client.post(
        "/api/auth/login",
        json={"username": "alice", "password": TEST_PASSWORD},
        headers=TEST_CSRF_HEADERS,
    )
    assert disabled_login.status_code == 401


def test_logout_revokes_only_current_session(anon_client: TestClient) -> None:
    db = _db_for(anon_client)
    register(anon_client, "alice", invite_code=create_invite(db))
    other = TestClient(anon_client.app)
    with other:
        login(other, "alice")
        assert other.get("/api/me").status_code == 200
        response = anon_client.post(
            "/api/auth/logout", headers=TEST_CSRF_HEADERS
        )
        assert response.status_code == 204
        assert anon_client.get("/api/me").status_code == 401
        assert other.get("/api/me").status_code == 200


def test_invite_is_single_use(anon_client: TestClient) -> None:
    db = _db_for(anon_client)
    code = create_invite(db)
    register(anon_client, "alice", invite_code=code)
    second = anon_client.post(
        "/api/auth/register",
        json={"invite_code": code, "username": "bob", "password": TEST_PASSWORD},
        headers=TEST_CSRF_HEADERS,
    )
    assert second.status_code == 400


def test_username_conflict_leaves_invite_usable(anon_client: TestClient) -> None:
    db = _db_for(anon_client)
    register(anon_client, "taken", invite_code=create_invite(db))
    code = create_invite(db)
    conflict = anon_client.post(
        "/api/auth/register",
        json={"invite_code": code, "username": "taken", "password": TEST_PASSWORD},
        headers=TEST_CSRF_HEADERS,
    )
    assert conflict.status_code == 409
    connection = sqlite3.connect(db)
    try:
        used = connection.execute(
            "SELECT used_at FROM invite_codes WHERE code_sha256 = ?",
            (
                __import__("hashlib").sha256(code.encode()).hexdigest(),
            ),
        ).fetchone()[0]
    finally:
        connection.close()
    assert used is None
    register(anon_client, "fresh", invite_code=code)


def test_expired_invite_is_rejected(anon_client: TestClient) -> None:
    db = _db_for(anon_client)
    code = create_invite(db, expires_in_days=0)
    response = anon_client.post(
        "/api/auth/register",
        json={"invite_code": code, "username": "late", "password": TEST_PASSWORD},
        headers=TEST_CSRF_HEADERS,
    )
    assert response.status_code == 400


def test_concurrent_same_invite_only_one_succeeds(tmp_path: Path) -> None:
    db = tmp_path / "study.sqlite3"
    schema.initialize_database(db)
    code = create_invite(db)
    password_hash = hash_password(TEST_PASSWORD)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def attempt(username: str) -> None:
        repository = AuthRepository(db)
        barrier.wait()
        try:
            repository.register_with_invite(
                invite_code=code,
                username=username,
                password_hash=password_hash,
            )
            results.append("ok")
        except InviteInvalidError:
            results.append("invalid")

    threads = [
        threading.Thread(target=attempt, args=("alpha",)),
        threading.Thread(target=attempt, args=("beta",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["invalid", "ok"]
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM invite_codes WHERE used_at IS NOT NULL"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_csrf_rejects_missing_header_and_bad_origin(anon_client: TestClient) -> None:
    db = _db_for(anon_client)
    code = create_invite(db)
    missing = anon_client.post(
        "/api/auth/register",
        json={"invite_code": code, "username": "alice", "password": TEST_PASSWORD},
    )
    assert missing.status_code == 403

    register(anon_client, "alice", invite_code=create_invite(db))
    no_header = anon_client.post("/api/conversations", json={})
    assert no_header.status_code == 403

    bad_origin = anon_client.post(
        "/api/conversations",
        json={},
        headers={**TEST_CSRF_HEADERS, "Origin": "https://evil.example"},
    )
    assert bad_origin.status_code == 403

    ok = anon_client.post(
        "/api/conversations", json={}, headers=TEST_CSRF_HEADERS
    )
    assert ok.status_code == 201


def test_cookie_secure_flag_follows_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.conversations import schema
    from app.main import app

    db = tmp_path / "study.sqlite3"
    schema.initialize_database(db)
    code = create_invite(db)
    monkeypatch.setenv("HHU_STUDY_DB", str(db))
    monkeypatch.setenv("HHU_COOKIE_SECURE", "true")
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/register",
            json={"invite_code": code, "username": "secure", "password": TEST_PASSWORD},
            headers=TEST_CSRF_HEADERS,
        )
    assert response.status_code == 201
    assert "Secure" in response.headers["set-cookie"]