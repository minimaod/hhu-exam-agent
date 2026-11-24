"""F012 测试辅助：默认测试用户、邀请码注册、A/B 数据快照。"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth.repository import AuthRepository
from app.conversations import schema
from app.conversations.repository import ConversationRepository

TEST_USER_ID = "00000000-0000-4000-8000-000000000001"
TEST_USERNAME = "test-user"
TEST_PASSWORD = "test-password-123"
TEST_CSRF_HEADERS = {"X-HHU-CSRF": "1"}
TEST_MIGRATION_OWNER = schema.SchemaOwner(
    id="00000000-0000-4000-8000-000000000099",
    username="migration-admin",
    username_normalized="migration-admin",
    password_hash="test-only-not-a-real-hash",
    created_at="2026-01-01T00:00:00+00:00",
)


def ensure_test_user(db_path: Path) -> None:
    """给旧测试的隔离库补一条测试用户，避免 owner 外键失败。"""

    path = Path(db_path)
    try:
        schema.initialize_database(path)
    except Exception:
        return
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO users (
                id, username, username_normalized, password_hash,
                created_at, disabled_at
            ) VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                TEST_USER_ID,
                TEST_USERNAME,
                TEST_USERNAME,
                "test-only-not-a-real-hash",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def ensure_owner(db_path: Path, owner_id: str) -> None:
    path = Path(db_path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO users (
                id, username, username_normalized, password_hash,
                created_at, disabled_at
            ) VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                owner_id,
                owner_id,
                owner_id.casefold(),
                "test-only-not-a-real-hash",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def scoped_repository(
    db_path: Path, owner_id: str = TEST_USER_ID, *, initialize: bool = True
) -> ConversationRepository:
    if initialize:
        schema.initialize_database(Path(db_path))
    ensure_owner(db_path, owner_id)
    return ConversationRepository(Path(db_path), owner_id=owner_id)


def create_invite(db_path: Path, *, expires_in_days: int | None = 7) -> str:
    return AuthRepository(Path(db_path)).create_invite(
        label="test", expires_in_days=expires_in_days
    )[1]


def register(
    client: TestClient, username: str, *, invite_code: str, password: str = TEST_PASSWORD
) -> dict[str, Any]:
    response = client.post(
        "/api/auth/register",
        json={
            "invite_code": invite_code,
            "username": username,
            "password": password,
        },
        headers=TEST_CSRF_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


def login(client: TestClient, username: str, password: str = TEST_PASSWORD) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers=TEST_CSRF_HEADERS,
    )
    assert response.status_code == 200, response.text


def create_conversation(client: TestClient) -> str:
    response = client.post(
        "/api/conversations", json={}, headers=TEST_CSRF_HEADERS
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def owner_snapshot(db_path: Path, owner_id: str) -> dict[str, Any]:
    """A 数据快照：行数 + 内容哈希 + 附件文件名；用于断言失败请求不写入。"""

    connection = sqlite3.connect(Path(db_path))
    connection.row_factory = sqlite3.Row
    try:
        conversation_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM conversations WHERE owner_id = ?", (owner_id,)
            ).fetchall()
        ]
        payload: dict[str, Any] = {
            "conversations": conversation_ids,
        }
        for table, column in (
            ("conversation_turns", "conversation_id"),
            ("problems", "conversation_id"),
            ("attachments", "conversation_id"),
            ("attendance", "conversation_id"),
        ):
            if not conversation_ids:
                payload[table] = []
                continue
            if table == "attendance":
                continue
            marks = ",".join("?" for _ in conversation_ids)
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE {column} IN ({marks})",
                conversation_ids,
            ).fetchall()
            payload[table] = [dict(row) for row in rows]
        records = []
        if conversation_ids:
            marks = ",".join("?" for _ in conversation_ids)
            records = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM mistake_records WHERE conversation_id IN ({marks})",
                    conversation_ids,
                ).fetchall()
            ]
        payload["mistake_records"] = records
        payload["memory_corrections"] = [
            dict(row)
            for row in connection.execute(
                "SELECT owner_id, entry_key, note FROM memory_corrections "
                "WHERE owner_id = ?",
                (owner_id,),
            ).fetchall()
        ]
        payload["memory_exclusions"] = [
            dict(row)
            for row in connection.execute(
                "SELECT owner_id, entry_key, reason FROM memory_exclusions "
                "WHERE owner_id = ?",
                (owner_id,),
            ).fetchall()
        ]
        text = repr(payload).encode("utf-8")
        payload["digest"] = hashlib.sha256(text).hexdigest()
        return payload
    finally:
        connection.close()


def new_id() -> str:
    return str(uuid4())