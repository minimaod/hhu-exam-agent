"""F013：一个账号清理孤儿时不能删掉另一个账号的占用登记。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.conversations.repository import ConversationRepository
from app.conversations.schema import initialize_database
from app.conversations.service import ConversationService
from app.quota.lease import LeaseRegistry


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "study.sqlite3"
    initialize_database(db)
    connection = sqlite3.connect(db)
    try:
        for owner in ("a", "b"):
            connection.execute(
                "INSERT INTO users (id, username, username_normalized, password_hash, created_at) "
                "VALUES (?, ?, ?, 'x', '2026-01-01')",
                (owner, owner, owner),
            )
        connection.commit()
    finally:
        connection.close()
    return db


def _service(repo: ConversationRepository, registry: LeaseRegistry) -> ConversationService:
    return ConversationService(
        repository=repo,
        make_query=None,
        query_error="测试未配置",
        lease_registry=registry,
    )


def test_other_user_sweep_keeps_original_lease(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo_a = ConversationRepository(db).scoped("a")
    repo_b = ConversationRepository(db).scoped("b")
    conversation_a = repo_a.create_conversation().id
    repo_a.start_turn(
        conversation_a, "req-a", "A 的轮次", lease_token="lease-a"
    )

    registry = LeaseRegistry(reclaim_seconds=0, clock=lambda: 0.0)
    registry.prepare("lease-a")
    registry.clock = lambda: 10**6
    assert registry.orphans() == ["lease-a"]

    service_b = _service(repo_b, registry)
    service_b._sweep_orphans()
    # B 的 scoped 仓库删不到 A 的数据库占用，因此也不能删 A 的内存登记。
    assert repo_a.has_active_lease() is True
    assert registry.is_active("lease-a") is True

    service_a = _service(repo_a, registry)
    service_a._sweep_orphans()
    assert repo_a.has_active_lease() is False
    assert registry.is_active("lease-a") is False

    repo_a.start_turn(
        conversation_a, "req-a-2", "A 重试", lease_token="lease-a-2"
    )
    assert repo_a.release_turn_slot("lease-a-2") is True