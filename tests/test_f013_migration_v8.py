"""F013：v7→v8 主路径与 v6→v8 链式迁移。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.auth.migrate import migrate_database
from app.conversations import schema
from tests.test_f012_migration_v7 import build_v6, owner


def _version(db: Path) -> int:
    connection = sqlite3.connect(db)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _usage_tables(db: Path) -> set[str]:
    connection = sqlite3.connect(db)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if row[0] in {"model_usage_events", "model_quota_overrides", "user_active_turns"}
        }
    finally:
        connection.close()


def _make_v7(db: Path) -> None:
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for stmt in schema._CREATE_V7_STATEMENTS:
            connection.execute(stmt)
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
    finally:
        connection.close()


def test_v7_to_v8_without_admin(tmp_path: Path) -> None:
    db = tmp_path / "study.sqlite3"
    _make_v7(db)
    report = migrate_database(db, backup_dir=tmp_path / "backups")
    assert report.already_migrated is False
    assert report.version == schema.SCHEMA_VERSION
    assert _version(db) == schema.SCHEMA_VERSION
    assert _usage_tables(db) == {
        "model_usage_events",
        "model_quota_overrides",
        "user_active_turns",
    }
    second = migrate_database(db, backup_dir=tmp_path / "backups")
    assert second.already_migrated is True


def test_v6_to_v8_chain_keeps_old_data(tmp_path: Path) -> None:
    db = tmp_path / "study.sqlite3"
    build_v6(db)
    report = migrate_database(
        db, owner=owner(), backup_dir=tmp_path / "backups"
    )
    assert report.version == schema.SCHEMA_VERSION
    assert _version(db) == schema.SCHEMA_VERSION
    connection = sqlite3.connect(db)
    try:
        assert connection.execute(
            "SELECT owner_id FROM conversations"
        ).fetchone()[0] == "admin-1"
        assert connection.execute(
            "SELECT COUNT(*) FROM model_usage_events"
        ).fetchone()[0] == 0
    finally:
        connection.close()