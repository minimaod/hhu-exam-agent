"""F013：额度检查、事务、跨日、覆盖与占用。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.conversations import schema
from app.quota.errors import (
    ImageQuotaExceeded,
    ModelQuotaExceeded,
    TurnInProgressError,
)
from app.quota.gate import UsageGate, UsageLimits
from app.quota.lease import LeaseRegistry


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "study.sqlite3"
    schema.initialize_database(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO users (id, username, username_normalized, password_hash, created_at) "
            "VALUES ('u1', 'u1', 'u1', 'x', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO users (id, username, username_normalized, password_hash, created_at) "
            "VALUES ('u2', 'u2', 'u2', 'x', '2026-01-01')"
        )
        connection.commit()
    finally:
        connection.close()
    return db


def _gate(db: Path, *, user: int = 2, site: int = 10, images: int = 1, clock=None):
    return UsageGate(
        db,
        limits=UsageLimits(user, site, images),
        now_fn=clock,
    )


def test_two_requests_allowed_third_blocked(tmp_path: Path) -> None:
    db = _db(tmp_path)
    gate = _gate(db)
    gate.acquire_model_request("u1")
    gate.acquire_model_request("u1")
    with pytest.raises(ModelQuotaExceeded) as caught:
        gate.acquire_model_request("u1")
    assert caught.value.scope == "user"
    assert caught.value.used == 2


def test_site_limit_blocks_other_user(tmp_path: Path) -> None:
    db = _db(tmp_path)
    gate = _gate(db, user=10, site=2)
    gate.acquire_model_request("u1")
    gate.acquire_model_request("u1")
    with pytest.raises(ModelQuotaExceeded) as caught:
        gate.acquire_model_request("u2")
    assert caught.value.scope == "site"


def test_override_and_expiry(tmp_path: Path) -> None:
    db = _db(tmp_path)
    gate = _gate(db, user=2)
    gate.acquire_model_request("u1")
    gate.acquire_model_request("u1")
    gate.set_override(
        "u1", daily_requests=3, daily_images=None, expires_on="2099-01-01"
    )
    gate.acquire_model_request("u1")
    with pytest.raises(ModelQuotaExceeded):
        gate.acquire_model_request("u1")
    gate.set_override(
        "u1", daily_requests=99, daily_images=None, expires_on="2000-01-01"
    )
    with pytest.raises(ModelQuotaExceeded):
        gate.acquire_model_request("u1")


def test_cross_day_resets(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = [datetime(2026, 11, 10, 15, 59, tzinfo=timezone.utc)]
    gate = _gate(db, user=1, clock=lambda: now[0])
    gate.acquire_model_request("u1")
    with pytest.raises(ModelQuotaExceeded):
        gate.acquire_model_request("u1")
    now[0] = datetime(2026, 11, 10, 16, 1, tzinfo=timezone.utc)
    gate.acquire_model_request("u1")


def test_image_quota_inside_transaction(tmp_path: Path) -> None:
    db = _db(tmp_path)
    gate = _gate(db, images=1)
    connection = sqlite3.connect(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        gate.check_and_insert_image(connection, "u1")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ImageQuotaExceeded):
            gate.check_and_insert_image(connection, "u1")
        connection.rollback()
    finally:
        connection.close()


def test_usage_rows_are_not_cascade_deleted(tmp_path: Path) -> None:
    db = _db(tmp_path)
    gate = _gate(db)
    gate.acquire_model_request("u1")
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id = 'u1'")
        connection.rollback()
    finally:
        connection.close()


def test_lease_registry_does_not_reclaim_live_task(tmp_path: Path) -> None:
    registry = LeaseRegistry(reclaim_seconds=15 * 60, clock=lambda: 0.0)
    registry.prepare("lease-1")
    assert registry.orphans() == []
    registry.clock = lambda: 20 * 60
    assert registry.orphans() == ["lease-1"]


def test_repository_turn_slot_conflicts(tmp_path: Path) -> None:
    from tests.f012_support import scoped_repository

    db = _db(tmp_path)
    repo = scoped_repository(db, owner_id="u1", initialize=False)
    conversation = repo.create_conversation().id
    other = repo.create_conversation().id
    repo.start_turn(
        conversation, "req-1", "第一轮", lease_token="lease-1"
    )
    with pytest.raises(TurnInProgressError):
        repo.start_turn(
            other, "req-2", "第二轮", lease_token="lease-2"
        )
    assert repo.release_turn_slot("lease-1") is True
    repo.start_turn(other, "req-2", "第二轮", lease_token="lease-2")
    assert repo.release_turn_slot("lease-2") is True

@pytest.mark.anyio
async def test_live_task_is_not_reclaimed_even_after_threshold() -> None:
    import asyncio

    registry = LeaseRegistry(reclaim_seconds=0, clock=lambda: 0.0)
    registry.prepare("lease-live")
    task = asyncio.create_task(asyncio.sleep(10))
    registry.mark_started("lease-live", task)
    registry.clock = lambda: 10**6
    assert registry.orphans() == []
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert registry.orphans() == ["lease-live"]


def test_business_marker_merges_recovery_actions(tmp_path: Path) -> None:
    from tests.f012_support import scoped_repository

    db = _db(tmp_path)
    repo = scoped_repository(db, owner_id="u1", initialize=False)
    conversation = repo.create_conversation().id
    repo.start_turn(conversation, "req-1", "第一轮", lease_token="lease-1")
    repo.save_recovery(
        conversation,
        "req-1",
        {"version": 2, "actions": [{"id": "a1", "outcome": {"status": "success"}}]},
    )
    repo.mark_business_saved(conversation, "req-1")
    turn = repo.find_turn(conversation, "req-1")
    assert turn is not None
    assert turn.recovery["business_saved"] is True
    assert turn.recovery["actions"][0]["id"] == "a1"


def test_acquire_rereads_day_after_lock_wait(tmp_path: Path) -> None:
    db = _db(tmp_path)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO model_usage_events (owner_id, usage_date, created_at, kind) "
            "VALUES ('u1', '2026-11-10', '2026-11-10T15:59:00+00:00', 'model_request')"
        )
        connection.commit()
    finally:
        connection.close()
    state = {"locked": False}

    def now() -> datetime:
        if state["locked"]:
            return datetime(2026, 11, 10, 16, 1, tzinfo=timezone.utc)
        return datetime(2026, 11, 10, 15, 59, tzinfo=timezone.utc)

    gate = UsageGate(
        db,
        limits=UsageLimits(1, 100, 1),
        now_fn=now,
    )
    real_open = gate._open

    class LockAwareConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args):
            if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                state["locked"] = True
            return self._inner.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    gate._open = lambda: LockAwareConnection(real_open())  # type: ignore[method-assign]
    # 等锁期间跨过北京时间零点：取得写锁后应按新的一天统计。
    gate.acquire_model_request("u1")
    connection = sqlite3.connect(db)
    try:
        days = [
            row[0]
            for row in connection.execute(
                "SELECT usage_date FROM model_usage_events WHERE owner_id='u1' "
                "ORDER BY id"
            )
        ]
    finally:
        connection.close()
    assert days == ["2026-11-10", "2026-11-11"]
