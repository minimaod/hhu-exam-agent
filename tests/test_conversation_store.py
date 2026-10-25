"""SQLite 会话仓库的行为与故障边界。"""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.conversations.errors import (
    ConversationConflictError,
    ConversationStorageError,
)
from app.conversations.repository import (
    ConversationRepository,
    DEFAULT_TITLE,
    SCHEMA_VERSION,
)
from app.querying.models import QueryFilters, ResultEvent
from app.papers.models import PaperItem


def build_result(kind: str = "found", course: str = "高等数学") -> ResultEvent:
    if kind == "found":
        item = PaperItem(
            id="sample-final",
            title="样例试卷 sample-final",
            course="高等数学BⅠ",
            academic_year="2022-2023",
            exam_type="final",
            exam_date="2025-01-16",
            download_url="/api/papers/sample-final/download",
        )
        return ResultEvent(
            kind="found",
            message="找到 1 份试卷",
            filters=QueryFilters(course=course, exam_type="final"),
            items=[item],
            total=1,
        )
    return ResultEvent(
        kind="empty",
        message="没有匹配的试卷",
        filters=QueryFilters(course=course, exam_type=None),
        items=[],
        total=0,
    )


def open_repository(path: Path) -> ConversationRepository:
    repository = ConversationRepository(path)
    repository.initialize()
    return repository


def checkpoint_hash(path: Path) -> str:
    """先把 WAL 合并回主文件，再比较数据库文件字节。"""

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_initialize_creates_schema_and_version(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "study.sqlite3"

    repository = open_repository(db_path)

    assert repository.db_path == db_path
    assert db_path.is_file()
    with sqlite3.connect(db_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert version == SCHEMA_VERSION
    assert {"conversations", "conversation_turns"} <= tables

    # 重复初始化是安全的。
    repository.initialize()


def test_create_and_stable_list_order(tmp_path: Path) -> None:
    repository = open_repository(tmp_path / "study.sqlite3")
    first = repository.create_conversation()
    second = repository.create_conversation()

    assert first.title == DEFAULT_TITLE
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", first.id),
        )
        connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            ("2026-02-01T00:00:00+00:00", second.id),
        )
        connection.commit()

    ordered = repository.list_conversations()
    assert [item.id for item in ordered] == [second.id, first.id]

    # 更新时间相同按 id 排序，重复读取顺序一致。
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            "UPDATE conversations SET updated_at = ?",
            ("2026-03-01T00:00:00+00:00",),
        )
        connection.commit()
    first_read = [item.id for item in repository.list_conversations()]
    second_read = [item.id for item in repository.list_conversations()]
    assert first_read == second_read

    assert repository.get_conversation("missing") is None


def test_turn_lifecycle_and_title(tmp_path: Path) -> None:
    repository = open_repository(tmp_path / "study.sqlite3")
    conversation = repository.create_conversation()

    turn = repository.start_turn(
        conversation.id,
        "11111111-1111-4111-8111-111111111111",
        "  我要复习\n高数  ",
    )

    assert turn.status == "running"
    assert turn.seq == 1
    assert repository.get_conversation(conversation.id).title == "我要复习 高数"

    repository.complete_turn(conversation.id, turn.request_id, build_result())
    saved = repository.find_turn(conversation.id, turn.request_id)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.result is not None
    assert saved.result.kind == "found"
    assert saved.finished_at is not None

    second = repository.start_turn(
        conversation.id,
        "22222222-2222-4222-8222-222222222222",
        "找大学物理期末试卷",
    )
    assert second.seq == 2
    repository.fail_turn(conversation.id, second.request_id, "MODEL_TIMEOUT", "超时")

    turns = repository.list_turns(conversation.id)
    assert [item.request_id for item in turns] == [turn.request_id, second.request_id]
    assert turns[1].status == "failed"
    assert turns[1].error is not None
    assert turns[1].error.code == "MODEL_TIMEOUT"

    # 已结束的轮次不能被再次标记为 running 或完成。
    with pytest.raises(ConversationConflictError):
        repository.complete_turn(conversation.id, turn.request_id, build_result())


def test_title_is_truncated_to_one_line(tmp_path: Path) -> None:
    repository = open_repository(tmp_path / "study.sqlite3")
    conversation = repository.create_conversation()

    repository.start_turn(
        conversation.id,
        "33333333-3333-4333-8333-333333333333",
        "第一行\n" + "很长的需求" * 20,
    )

    title = repository.get_conversation(conversation.id).title
    assert "\n" not in title
    assert len(title) <= 30
    assert title.endswith("…")


def test_one_running_turn_and_unique_request(tmp_path: Path) -> None:
    repository = open_repository(tmp_path / "study.sqlite3")
    conversation = repository.create_conversation()
    request_id = "44444444-4444-4444-8444-444444444444"

    repository.start_turn(conversation.id, request_id, "我要复习高数")

    with pytest.raises(ConversationConflictError):
        repository.start_turn(
            conversation.id,
            "55555555-5555-4555-8555-555555555555",
            "找线代试卷",
        )

    repository.interrupt_turn(conversation.id, request_id)
    with pytest.raises(ConversationConflictError):
        repository.start_turn(conversation.id, request_id, "我要复习高数")

    assert repository.has_running_turn(conversation.id) is False


def test_restart_keeps_history_and_recovers_running(tmp_path: Path) -> None:
    db_path = tmp_path / "study.sqlite3"
    repository = open_repository(db_path)
    conversation = repository.create_conversation()
    request_id = "66666666-6666-4666-8666-666666666666"
    repository.start_turn(conversation.id, request_id, "我要复习高数")
    repository.complete_turn(conversation.id, request_id, build_result())

    second_id = "77777777-7777-4777-8777-777777777777"
    repository.start_turn(conversation.id, second_id, "找大学物理期末试卷")

    reopened = open_repository(db_path)

    turns = reopened.list_turns(conversation.id)
    assert [item.request_id for item in turns] == [request_id, second_id]
    assert turns[0].status == "completed"
    assert turns[0].result is not None
    assert turns[1].status == "interrupted"
    assert turns[1].finished_at is not None
    assert reopened.has_running_turn(conversation.id) is False


def test_corrupt_turn_snapshot_is_not_deleted(tmp_path: Path) -> None:
    repository = open_repository(tmp_path / "study.sqlite3")
    conversation = repository.create_conversation()
    request_id = "88888888-8888-4888-8888-888888888888"
    repository.start_turn(conversation.id, request_id, "我要复习高数")
    repository.complete_turn(conversation.id, request_id, build_result())

    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            "UPDATE conversation_turns SET result_json = ? WHERE request_id = ?",
            ("{not json", request_id),
        )
        connection.commit()

    with pytest.raises(ConversationStorageError):
        repository.list_turns(conversation.id)

    with sqlite3.connect(repository.db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM conversation_turns WHERE request_id = ?",
            (request_id,),
        ).fetchone()[0]
    assert count == 1


def test_incompatible_schema_version_keeps_file(tmp_path: Path) -> None:
    db_path = tmp_path / "study.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT)")
        connection.execute("PRAGMA user_version = 99")
        connection.commit()

    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    repository = ConversationRepository(db_path)

    with pytest.raises(ConversationStorageError):
        repository.initialize()

    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_unknown_nonempty_version_zero_database_is_not_modified(
    tmp_path: Path,
) -> None:
    """版本号为 0 但有其他业务表：不能当成空库建表。"""

    db_path = tmp_path / "study.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE notes (body TEXT)")
        connection.execute("INSERT INTO notes (body) VALUES ('keep me')")
        connection.commit()

    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    repository = ConversationRepository(db_path)

    with pytest.raises(ConversationStorageError):
        repository.initialize()

    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    with sqlite3.connect(db_path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        body = connection.execute("SELECT body FROM notes").fetchone()[0]
    assert names == {"notes"}
    assert version == 0
    assert body == "keep me"


def test_version_zero_with_incompatible_same_name_tables_is_not_adopted(
    tmp_path: Path,
) -> None:
    """同名表结构不兼容时，不能补索引、改版本号或加列。"""

    db_path = tmp_path / "study.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE conversations (id TEXT, title TEXT)")
        connection.execute(
            "CREATE TABLE conversation_turns (id INTEGER, conversation_id TEXT)"
        )
        connection.execute("PRAGMA user_version = 0")
        connection.commit()

    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    repository = ConversationRepository(db_path)

    with pytest.raises(ConversationStorageError):
        repository.initialize()

    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        conversations = [
            row[1]
            for row in connection.execute("PRAGMA table_info(conversations)")
        ]
    assert conversations == ["id", "title"]


def test_version_zero_but_complete_schema_is_adopted(tmp_path: Path) -> None:
    """结构完整、仅版本号丢失的库可以补写版本号并继续使用。"""

    db_path = tmp_path / "study.sqlite3"
    repository = open_repository(db_path)
    conversation = repository.create_conversation()
    request_id = "99999999-9999-4999-8999-999999999999"
    repository.start_turn(conversation.id, request_id, "我要复习高数")
    repository.complete_turn(conversation.id, request_id, build_result())

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA user_version = 0")
        connection.commit()

    reopened = ConversationRepository(db_path)
    reopened.initialize()

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    turns = reopened.list_turns(conversation.id)
    assert [turn.status for turn in turns] == ["completed"]


def test_version_one_with_broken_structure_is_not_modified(tmp_path: Path) -> None:
    db_path = tmp_path / "study.sqlite3"
    repository = open_repository(db_path)
    conversation = repository.create_conversation()

    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX turns_one_running")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()

    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    with pytest.raises(ConversationStorageError):
        ConversationRepository(db_path).initialize()

    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE id = ?",
                (conversation.id,),
            ).fetchone()[0]
            == 1
        )


def test_non_unique_same_name_running_index_is_rejected(tmp_path: Path) -> None:
    """同名索引存在但非唯一时，不能收养该库，否则可写入多个 running 轮次。"""

    db_path = tmp_path / "study.sqlite3"
    repository = open_repository(db_path)
    repository.create_conversation()

    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX turns_one_running")
        connection.execute(
            """
            CREATE INDEX turns_one_running
                ON conversation_turns(conversation_id)
                WHERE status = 'running'
            """
        )
        connection.execute("PRAGMA user_version = 0")
        connection.commit()

    before = checkpoint_hash(db_path)
    with pytest.raises(ConversationStorageError):
        ConversationRepository(db_path).initialize()

    assert checkpoint_hash(db_path) == before
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'turns_one_running'"
        ).fetchone()[0]
    assert "UNIQUE" not in sql.upper()


def test_unique_same_name_running_index_on_wrong_column_is_rejected(
    tmp_path: Path,
) -> None:
    """同名索引唯一但索引 request_id 时，不能收养该库。"""

    db_path = tmp_path / "study.sqlite3"
    repository = open_repository(db_path)
    repository.create_conversation()

    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX turns_one_running")
        connection.execute(
            """
            CREATE UNIQUE INDEX turns_one_running
                ON conversation_turns(request_id)
                WHERE status = 'running'
            """
        )
        connection.execute("PRAGMA user_version = 0")
        connection.commit()

    before = checkpoint_hash(db_path)
    with pytest.raises(ConversationStorageError):
        ConversationRepository(db_path).initialize()

    assert checkpoint_hash(db_path) == before
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        columns = tuple(
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(turns_one_running)"
            ).fetchall()
        )
    assert columns == ("request_id",)


def test_corrupt_database_file_keeps_bytes(tmp_path: Path) -> None:
    db_path = tmp_path / "study.sqlite3"
    payload = b"this is not a sqlite database"
    db_path.write_bytes(payload)

    repository = ConversationRepository(db_path)

    with pytest.raises(ConversationStorageError):
        repository.initialize()

    assert db_path.read_bytes() == payload


def test_initialize_does_not_touch_other_files(tmp_path: Path) -> None:
    db_path = tmp_path / "study.sqlite3"
    neighbour = tmp_path / "keep.txt"
    neighbour.write_text("keep", encoding="utf-8")

    open_repository(db_path)

    assert neighbour.read_text(encoding="utf-8") == "keep"
