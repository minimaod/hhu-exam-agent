"""F009：记忆覆盖表的读写（v6）。

只存两件事：用户对某条证据的纠正（课程/知识点/补充说明）和删除排除。
学习证据本身不在这里——它是 exercises/attempts/problems 的实时投影，所以
这张表很小、也不需要在新增练习时同步维护。

两条写路径都是幂等的：``entry_key`` 是主键，重复纠正覆盖同一行，重复删除
不会产生第二行。因此模型重复调用同一个工具不会叠加副作用。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.conversations.errors import ConversationStorageError
from app.papers.courses import normalize_course

_STORAGE_MESSAGE = "本地学习数据暂时不可用，请稍后再试。"

# 表示“本次没有提供这个字段”，与“提供了空串（改为待确认）”区分开。
UNSET: object = object()

Correction = tuple[str | None, str | None, str | None]


class MemoryRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        # 读取不存在的数据库文件时不能“顺手建一个空库”：那会把“还没初始化”
        # 伪装成“没有学习记录”，学生就分不清是空还是坏了。
        if not self._db_path.is_file():
            raise ConversationStorageError(_STORAGE_MESSAGE)
        try:
            connection = sqlite3.connect(self._db_path, timeout=5.0)
        except (sqlite3.Error, OSError) as exc:
            raise ConversationStorageError(_STORAGE_MESSAGE) from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        except sqlite3.Error as exc:
            raise ConversationStorageError(_STORAGE_MESSAGE) from exc
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """供上层投影证据用的只读连接。"""

        with self._open() as connection:
            yield connection

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def corrections(self) -> dict[str, Correction]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT entry_key, course, knowledge_point, note FROM memory_corrections"
            ).fetchall()
        return {
            str(row["entry_key"]): (
                row["course"],
                row["knowledge_point"],
                row["note"],
            )
            for row in rows
        }

    def exclusions(self) -> set[str]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT entry_key FROM memory_exclusions"
            ).fetchall()
        return {str(row["entry_key"]) for row in rows}

    def is_excluded(self, entry_key: str) -> bool:
        with self._open() as connection:
            row = connection.execute(
                "SELECT 1 FROM memory_exclusions WHERE entry_key = ?",
                (entry_key,),
            ).fetchone()
        return row is not None

    def entry_exists(self, kind: str, source_id: str) -> bool:
        """确认 entry_key 指向的真实记录还在（删除后仍可用：见 is_excluded）。"""

        table, column = {
            "exercise": ("exercises", "id"),
            "attempt": ("exercise_attempts", "id"),
            "selfreport": ("problems", "id"),
        }.get(kind, (None, None))
        if table is None:
            return False
        with self._open() as connection:
            row = connection.execute(
                f"SELECT 1 FROM {table} WHERE {column} = ?", (source_id,)
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def upsert_correction(
        self,
        entry_key: str,
        *,
        course: object = UNSET,
        knowledge_point: object = UNSET,
        note: object = UNSET,
    ) -> Correction:
        now = datetime.now(timezone.utc).isoformat()
        if course is not UNSET and course is not None and str(course) != "":
            # 写入和读取使用同一套课程归一；否则存 “Python”、查规范名会查不到。
            # 空串保留为“改为待确认”的哨兵，不能被归一成 None 而丢掉意图。
            course = normalize_course(str(course)) or str(course)
        with self._open() as connection:
            row = connection.execute(
                "SELECT course, knowledge_point, note FROM memory_corrections "
                "WHERE entry_key = ?",
                (entry_key,),
            ).fetchone()
            current: Correction = (
                (row["course"], row["knowledge_point"], row["note"])
                if row is not None
                else (None, None, None)
            )
            merged = (
                current[0] if course is UNSET else (str(course) if course is not None else None),
                current[1]
                if knowledge_point is UNSET
                else (str(knowledge_point) if knowledge_point is not None else None),
                current[2] if note is UNSET else (str(note) if note is not None else None),
            )
            connection.execute(
                "INSERT INTO memory_corrections "
                "(entry_key, course, knowledge_point, note, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entry_key) DO UPDATE SET "
                "course = excluded.course, "
                "knowledge_point = excluded.knowledge_point, "
                "note = excluded.note, "
                "updated_at = excluded.updated_at",
                (entry_key, *merged, now, now),
            )
            connection.commit()
        return merged

    def add_exclusion(self, entry_key: str, reason: str | None = None) -> bool:
        """写入持续排除；返回 True 表示本次新写入，False 表示之前已排除。"""

        now = datetime.now(timezone.utc).isoformat()
        with self._open() as connection:
            cursor = connection.execute(
                "INSERT INTO memory_exclusions (entry_key, reason, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(entry_key) DO NOTHING",
                (entry_key, reason, now),
            )
            connection.commit()
            return cursor.rowcount > 0