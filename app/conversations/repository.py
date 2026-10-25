"""本地 SQLite 学习数据仓库。

目前只有这一处知道学习数据存在哪个文件、表结构长什么样。服务层和接口层只
调用这里的方法；以后更换数据库时，新增实现相同方法的仓库即可。

关键约定：
- 每次操作新开连接，使用短事务（BEGIN IMMEDIATE），模型等待期间不持有事务。
- 参数化 SQL，不拼接用户输入。
- 结构版本记录在 PRAGMA user_version；版本不兼容或文件损坏时只报错，
  绝不删除、清空或覆盖原文件。
- 启动时把上次遗留的 running 轮次标记为 interrupted，不自动重跑模型。
- 题目、讲解、记录和轮次在同一个事务内保存；保存成功后才由服务层发事件。
"""

import json
from contextvars import ContextVar
import logging
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from app.conversations import schema
from app.conversations.errors import (
    AttachmentInvalidError,
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationStorageError,
    MistakeRecordConflictError,
    MistakeRecordNotFoundError,
    ProblemNotFoundError,
)
from app.images import MAX_IMAGE_BYTES, MAX_IMAGES_PER_TURN, MAX_TOTAL_BYTES
from app.conversations.inputs import query_input
from app.conversations.models import (
    ActionResultItem,
    ConversationContextView,
    ConversationSummary,
    ExerciseAttemptView,
    ExerciseEvaluation,
    ExerciseView,
    SuggestionItem,
    Turn,
    TurnError,
)
from app.querying.models import ResultEvent
from app.tutoring.models import AttachmentView, Explanation, Source, TutoringResult

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "local" / "study.sqlite3"

SCHEMA_VERSION = schema.SCHEMA_VERSION
DEFAULT_TITLE = "新会话"
TITLE_MAX_LENGTH = 30
CONNECT_TIMEOUT_SECONDS = 5.0
_VALID_ROLES = frozenset({"statement", "solution", "supplement"})

_STORAGE_MESSAGE = "本地学习数据暂时不可用，请稍后再试。"
_CORRUPT_MESSAGE = "已保存的会话记录不完整，暂时无法恢复。"

_TUTORING_ADAPTER: TypeAdapter[TutoringResult] = TypeAdapter(TutoringResult)


def _now() -> str:
    """统一保存带时区的时间；字符串按 ISO 8601 可直接比较先后。"""

    return datetime.now(timezone.utc).isoformat()


def _title_from_text(text: str) -> str:
    """从首轮输入文字生成简短标题，不调用模型。"""

    collapsed = " ".join(text.split())
    if not collapsed:
        return DEFAULT_TITLE
    if len(collapsed) <= TITLE_MAX_LENGTH:
        return collapsed
    return collapsed[: TITLE_MAX_LENGTH - 1] + "…"


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _needs_supplement(evaluation_json: str | None) -> bool:
    """评价要求补充过程时，允许学生继续提交并重新评价。"""

    if not evaluation_json:
        return False
    try:
        return bool(
            ExerciseEvaluation.model_validate_json(evaluation_json).needs_supplement
        )
    except (ValidationError, ValueError):
        return False


def _json_load(text: str | None, default: object) -> object:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ConversationStorageError(_CORRUPT_MESSAGE) from exc


def _parse_time(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ConversationStorageError(_CORRUPT_MESSAGE) from exc


# ---------------------------------------------------------------------------
# 内部记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttachmentRecord:
    id: str
    conversation_id: str
    stored_name: str
    mime: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    pending: bool
    created_at: datetime
    role: str | None = None


@dataclass(frozen=True)
class ProblemRecord:
    id: str
    conversation_id: str
    root_problem_id: str
    supersedes_problem_id: str | None
    version: int
    statement_text: str
    status: str
    clarity: str
    unclear_points: list[str]
    uncertainties: list[str]
    source: Source
    student_answer_text: str | None
    solution_text: str | None
    explanation: Explanation | None
    self_reported_understood: bool
    active: bool
    created_at: datetime
    updated_at: datetime
    attachments: list[AttachmentRecord]


@dataclass(frozen=True)
class ExerciseAttemptRecord:
    id: str
    exercise_id: str
    attempt_no: int
    kind: str
    student_answer_text: str | None
    student_solution_text: str | None
    answered_after_revealed: bool
    evaluation_json: str | None
    request_id: str | None
    created_at: datetime
    updated_at: datetime
    attachments: list[AttachmentRecord] = ()  # type: ignore[assignment]


@dataclass(frozen=True)
class ExerciseRecord:
    id: str
    conversation_id: str
    problem_id: str
    root_problem_id: str
    statement_text: str
    reference_solution_text: str
    knowledge_point: str
    status: str
    student_answer_text: str | None
    student_solution_text: str | None
    revealed_solution_at: datetime | None
    answered_after_revealed: bool
    evaluation_json: str | None
    created_at: datetime
    updated_at: datetime
    attachments: list[AttachmentRecord] = ()  # type: ignore[assignment]
    attempts: list[ExerciseAttemptRecord] = ()  # type: ignore[assignment]

    @property
    def current_attempt(self) -> ExerciseAttemptRecord | None:
        return self.attempts[-1] if self.attempts else None


@dataclass(frozen=True)
class ContextRecord:
    conversation_id: str
    confirmed_course: str | None
    active_problem_id: str | None
    active_exercise_id: str | None
    last_suggestions_json: str | None
    last_action_summary: str | None
    updated_at: datetime


@dataclass(frozen=True)
class NewProblem:
    """保存新题目或纠正版本时由服务层提供的字段。"""

    statement_text: str
    status: str
    clarity: str
    unclear_points: list[str]
    uncertainties: list[str]
    source: Source
    student_answer_text: str | None
    solution_text: str | None
    explanation: Explanation | None
    root_problem_id: str | None = None
    supersedes_problem_id: str | None = None


@dataclass(frozen=True)
class UpdatedProblem:
    statement_text: str
    status: str
    clarity: str
    unclear_points: list[str]
    uncertainties: list[str]
    source: Source
    student_answer_text: str | None
    solution_text: str | None
    explanation: Explanation | None


@dataclass(frozen=True)
class RecordSnapshot:
    """写入或替换错题记录时的完整快照。"""

    conversation_id: str
    problem_id: str
    root_problem_id: str
    problem_version: int
    request_id: str
    title: str
    statement_text: str
    student_answer_text: str | None
    solution_text: str | None
    source: Source
    explanation: Explanation | None
    followups: list[dict[str, str]]
    feedback: dict[str, object]
    self_reported_understood: bool
    attachments: list[tuple[str, str]]


@dataclass(frozen=True)
class RecordRow:
    id: str
    conversation_id: str
    problem_id: str
    root_problem_id: str
    problem_version: int
    request_id: str
    title: str
    statement_text: str
    student_answer_text: str | None
    solution_text: str | None
    source: Source
    explanation: Explanation | None
    followups: list[dict[str, str]]
    feedback: dict[str, object]
    self_reported_understood: bool
    created_at: datetime
    updated_at: datetime
    attachments: list[AttachmentRecord]


@dataclass(frozen=True)
class RevisionRow:
    id: int
    record_id: str
    request_id: str
    reason: str
    snapshot: dict[str, object]
    created_at: datetime


# ---------------------------------------------------------------------------
# 仓库
# ---------------------------------------------------------------------------


class ConversationRepository:
    """按数据库路径读写会话、题目、附件与错题记录；测试可注入临时路径。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        # 仅在不含 await 的短同步区间使用，允许业务写入与动作检查点原子提交。
        self._action_connection: ContextVar[sqlite3.Connection | None] = ContextVar("action_connection", default=None)

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ------------------------------------------------------------------
    # 连接与事务
    # ------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self._db_path,
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except (sqlite3.Error, OSError) as exc:
            raise ConversationStorageError(_STORAGE_MESSAGE) from exc

        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
        except sqlite3.Error as exc:
            connection.close()
            raise ConversationStorageError(_STORAGE_MESSAGE) from exc
        return connection

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        if self._action_connection.get() is not None:
            yield self._action_connection.get()
            return
        connection = self._open()
        try:
            yield connection
        except sqlite3.Error as exc:
            raise ConversationStorageError(_STORAGE_MESSAGE) from exc
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        if self._action_connection.get() is not None:
            yield self._action_connection.get()
            return
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            connection.close()
            raise ConversationStorageError(_STORAGE_MESSAGE) from exc

        try:
            yield connection
        except (ConversationConflictError, AttachmentInvalidError):
            with suppress(sqlite3.Error):
                connection.rollback()
            raise
        except sqlite3.IntegrityError:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise
        except sqlite3.Error as exc:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise ConversationStorageError(_STORAGE_MESSAGE) from exc
        except BaseException:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise
        else:
            try:
                connection.commit()
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    connection.rollback()
                raise ConversationStorageError(_STORAGE_MESSAGE) from exc
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        schema.initialize_database(self._db_path)

    @contextmanager
    def action_write(self) -> Iterator[None]:
        """不得跨模型等待：内部仓库方法与动作检查点共享同一事务。"""
        with self._write() as connection:
            token = self._action_connection.set(connection)
            try:
                yield
            finally:
                self._action_connection.reset(token)

    def save_recovery(self, conversation_id: str, request_id: str, recovery: dict) -> None:
        with self._write() as connection:
            row = connection.execute(
                "SELECT output_json FROM conversation_turns WHERE conversation_id=? AND request_id=? AND status='running'",
                (conversation_id, request_id),
            ).fetchone()
            if row is None:
                raise ConversationConflictError("轮次已结束，不能再写入动作恢复记录。")
            payload = _json_load(row["output_json"], {}) if row["output_json"] else {}
            payload["_recovery"] = recovery
            connection.execute(
                "UPDATE conversation_turns SET output_json=?, kind=CASE WHEN ?=1 THEN 'dynamic' ELSE kind END WHERE conversation_id=? AND request_id=?",
                (json.dumps(payload, ensure_ascii=False), recovery.get("version", 0), conversation_id, request_id),
            )

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    def create_conversation(self) -> ConversationSummary:
        now = _now()
        conversation_id = str(uuid.uuid4())
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO conversations (id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (conversation_id, DEFAULT_TITLE, now, now),
            )
        return ConversationSummary(
            id=conversation_id,
            title=DEFAULT_TITLE,
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )

    def list_conversations(self) -> list[ConversationSummary]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT id, title, created_at, updated_at FROM conversations
                ORDER BY updated_at DESC, id ASC
                """
            ).fetchall()
        return [self._row_to_summary(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> ConversationSummary | None:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT id, title, created_at, updated_at FROM conversations
                WHERE id = ?
                """,
                (conversation_id,),
            ).fetchone()
        return None if row is None else self._row_to_summary(row)

    # ------------------------------------------------------------------
    # 轮次
    # ------------------------------------------------------------------

    _TURN_COLUMNS = """
        conversation_id, request_id, seq, kind, query, status, result_json, output_json,
        error_code, error_message, started_at, finished_at, input_json
    """

    def list_turns(self, conversation_id: str) -> list[Turn]:
        with self._read() as connection:
            rows = connection.execute(
                f"""
                SELECT {self._TURN_COLUMNS}
                FROM conversation_turns
                WHERE conversation_id = ?
                ORDER BY seq ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [self._row_to_turn(row) for row in rows]

    def find_turn(self, conversation_id: str, request_id: str) -> Turn | None:
        with self._read() as connection:
            row = connection.execute(
                f"""
                SELECT {self._TURN_COLUMNS}
                FROM conversation_turns
                WHERE conversation_id = ? AND request_id = ?
                """,
                (conversation_id, request_id),
            ).fetchone()
        return None if row is None else self._row_to_turn(row)

    def has_running_turn(self, conversation_id: str) -> bool:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM conversation_turns
                WHERE conversation_id = ? AND status = 'running'
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return row is not None

    def start_turn(
        self,
        conversation_id: str,
        request_id: str,
        query: str,
        *,
        kind: str = "query",
        input_json: str | None = None,
        attachments: Sequence[tuple[str, str]] = (),
    ) -> Turn:
        """原子接受一轮输入：分配序号、绑定附件、更新标题/时间、写入 running。"""

        now = _now()
        canonical = input_json or query_input(query)

        with self._write() as connection:
            conversation = connection.execute(
                "SELECT title FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise ConversationNotFoundError("会话不存在。")

            next_seq = connection.execute(
                """
                SELECT COALESCE(MAX(seq), 0) + 1 FROM conversation_turns
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()[0]

            title = conversation["title"]
            if title == DEFAULT_TITLE:
                title = _title_from_text(query)

            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, conversation_id),
            )

            try:
                cursor = connection.execute(
                    """
                    INSERT INTO conversation_turns (
                        conversation_id, request_id, seq, query, status,
                        kind, input_json, started_at
                    ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        request_id,
                        next_seq,
                        query,
                        kind,
                        canonical,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConversationConflictError(
                    "该会话已有一轮正在执行，或该请求已经提交。"
                ) from exc

            self._link_turn_attachments(
                connection,
                cursor.lastrowid,
                conversation_id,
                attachments,
            )

        return Turn(
            request_id=request_id,
            seq=next_seq,
            kind=kind,  # type: ignore[arg-type]
            query=query,
            status="running",
            started_at=datetime.fromisoformat(now),
        )

    def complete_turn(
        self,
        conversation_id: str,
        request_id: str,
        result: ResultEvent,
    ) -> None:
        payload = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
        now = _now()

        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE conversation_turns
                SET status = 'completed', result_json = ?, error_code = NULL,
                    error_message = NULL, finished_at = ?
                WHERE conversation_id = ? AND request_id = ? AND status = 'running'
                """,
                (payload, now, conversation_id, request_id),
            )
            if cursor.rowcount != 1:
                raise ConversationConflictError("该轮次已经结束，不能重复保存结果。")
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )

    def fail_turn(
        self,
        conversation_id: str,
        request_id: str,
        code: str,
        message: str,
    ) -> None:
        now = _now()

        with self._write() as connection:
            self._finish_failed_turn(
                connection, conversation_id, request_id, code, message, now
            )

    def interrupt_turn(self, conversation_id: str, request_id: str) -> bool:
        """把尚未完成的轮次标记为 interrupted；已经结束则保持原状。"""

        now = _now()

        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE conversation_turns
                SET status = 'interrupted', result_json = NULL,
                    error_code = NULL, error_message = NULL, finished_at = ?
                WHERE conversation_id = ? AND request_id = ? AND status = 'running'
                """,
                (now, conversation_id, request_id),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
        return cursor.rowcount == 1

    def _maybe_set_title_from_problem(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        statement: str,
        now: str,
    ) -> None:
        row = connection.execute(
            "SELECT title FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row is not None and row["title"] == DEFAULT_TITLE:
            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (_title_from_text(statement), now, conversation_id),
            )

    def _finish_failed_turn(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        request_id: str,
        code: str,
        message: str,
        now: str,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE conversation_turns
            SET status = 'failed', result_json = NULL,
                error_code = ?, error_message = ?, finished_at = ?
            WHERE conversation_id = ? AND request_id = ? AND status = 'running'
            """,
            (code, message, now, conversation_id, request_id),
        )
        if cursor.rowcount != 1:
            raise ConversationConflictError("该轮次已经结束，不能重复保存错误。")
        connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )

    def complete_output_turn(
        self,
        conversation_id: str,
        request_id: str,
        *,
        kind: str,
        output_json: str,
    ) -> None:
        """完成一个不新建题目的辅导轮次（澄清、记录、反馈等）。"""

        now = _now()
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE conversation_turns
                SET status = 'completed', kind = ?, output_json = ?,
                    result_json = NULL, error_code = NULL, error_message = NULL,
                    finished_at = ?
                WHERE conversation_id = ? AND request_id = ? AND status = 'running'
                """,
                (kind, output_json, now, conversation_id, request_id),
            )
            if cursor.rowcount != 1:
                raise ConversationConflictError("该轮次已经结束，不能重复保存结果。")
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )

    def _complete_turn(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        request_id: str,
        *,
        output_json: str,
        problem_id: str | None = None,
        record_id: str | None = None,
        kind: str | None = None,
        now: str,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE conversation_turns
            SET status = 'completed', kind = COALESCE(?, kind),
                result_json = NULL, error_code = NULL,
                error_message = NULL, output_json = ?, problem_id = ?,
                record_id = ?, finished_at = ?
            WHERE conversation_id = ? AND request_id = ? AND status = 'running'
            """,
            (
                kind,
                output_json,
                problem_id,
                record_id,
                now,
                conversation_id,
                request_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ConversationConflictError("该轮次已经结束，不能重复保存结果。")
        connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )

    # ------------------------------------------------------------------
    # 附件
    # ------------------------------------------------------------------

    def create_attachment(
        self,
        *,
        conversation_id: str,
        attachment_id: str,
        stored_name: str,
        mime: str,
        width: int,
        height: int,
        size_bytes: int,
        sha256: str,
    ) -> AttachmentRecord:
        now = _now()
        with self._write() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError("会话不存在。")

            pending = connection.execute(
                "SELECT COUNT(*) FROM attachments "
                "WHERE conversation_id = ? AND pending = 1",
                (conversation_id,),
            ).fetchone()[0]
            if pending >= 20:
                raise AttachmentInvalidError("待提交图片过多，请先发送或稍后再试。")

            connection.execute(
                """
                INSERT INTO attachments (
                    id, conversation_id, stored_name, mime, width, height,
                    size_bytes, sha256, pending, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    attachment_id,
                    conversation_id,
                    stored_name,
                    mime,
                    width,
                    height,
                    size_bytes,
                    sha256,
                    now,
                ),
            )
        return AttachmentRecord(
            id=attachment_id,
            conversation_id=conversation_id,
            stored_name=stored_name,
            mime=mime,
            width=width,
            height=height,
            size_bytes=size_bytes,
            sha256=sha256,
            pending=True,
            created_at=datetime.fromisoformat(now),
        )

    def get_attachment(
        self, conversation_id: str, attachment_id: str
    ) -> AttachmentRecord | None:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT * FROM attachments
                WHERE id = ? AND conversation_id = ?
                """,
                (attachment_id, conversation_id),
            ).fetchone()
        return None if row is None else self._row_to_attachment(row)

    def validate_attachment_selection(
        self,
        conversation_id: str,
        entries: Sequence[tuple[str, str]],
    ) -> list[AttachmentRecord]:
        """统一校验一轮附件：数量、角色、重复、归属和总大小。

        普通会话轮次和错题纠正都必须经过这里；图片实际上传时已经校验单张
        6 MiB，这里再防止多张累计超过 20 MiB。
        """

        selected = list(entries)
        if len(selected) > MAX_IMAGES_PER_TURN:
            raise AttachmentInvalidError("一轮最多 4 张图片。")

        ids = [attachment_id for attachment_id, _ in selected]
        if len(set(ids)) != len(ids):
            raise AttachmentInvalidError("同一张图片不能在一轮中重复使用。")

        roles = [role for _, role in selected]
        if any(role not in _VALID_ROLES for role in roles):
            raise AttachmentInvalidError("附件角色不合法。")
        if roles.count("statement") > 3:
            raise AttachmentInvalidError("题干图片最多 3 张。")
        if roles.count("solution") > 2:
            raise AttachmentInvalidError("解法图片最多 2 张。")

        records: list[AttachmentRecord] = []
        total = 0
        for attachment_id, role in selected:
            attachment = self.get_attachment(conversation_id, attachment_id)
            if attachment is None:
                raise AttachmentInvalidError("图片不存在或不属于当前会话。")
            if attachment.size_bytes > MAX_IMAGE_BYTES:
                raise AttachmentTooLargeError("单张图片不能超过 6 MiB。")
            total += attachment.size_bytes
            records.append(
                AttachmentRecord(
                    id=attachment.id,
                    conversation_id=attachment.conversation_id,
                    stored_name=attachment.stored_name,
                    mime=attachment.mime,
                    width=attachment.width,
                    height=attachment.height,
                    size_bytes=attachment.size_bytes,
                    sha256=attachment.sha256,
                    pending=attachment.pending,
                    created_at=attachment.created_at,
                    role=role,
                )
            )

        if total > MAX_TOTAL_BYTES:
            raise AttachmentTooLargeError("本轮图片合计不能超过 20 MiB。")
        return records

    def list_attachments_for_problem(
        self, problem_id: str
    ) -> list[AttachmentRecord]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT a.*, pa.role AS role
                FROM problem_attachments pa
                JOIN attachments a ON a.id = pa.attachment_id
                WHERE pa.problem_id = ?
                ORDER BY pa.role ASC, a.id ASC
                """,
                (problem_id,),
            ).fetchall()
        return [self._row_to_attachment(row, row["role"]) for row in rows]

    def list_attachments_for_record(
        self, record_id: str
    ) -> list[AttachmentRecord]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT a.*, ma.role AS role
                FROM mistake_record_attachments ma
                JOIN attachments a ON a.id = ma.attachment_id
                WHERE ma.record_id = ?
                ORDER BY ma.role ASC, a.id ASC
                """,
                (record_id,),
            ).fetchall()
        return [self._row_to_attachment(row, row["role"]) for row in rows]

    def cleanup_pending_attachments(
        self, older_than: str
    ) -> list[str]:
        """返回待清理附件的 stored_name；由调用方删除文件后再删行。"""

        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT a.id, a.stored_name FROM attachments a
                WHERE a.pending = 1 AND a.created_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM turn_attachments t WHERE t.attachment_id = a.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM problem_attachments p WHERE p.attachment_id = a.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM mistake_record_attachments m
                      WHERE m.attachment_id = a.id
                  )
                """,
                (older_than,),
            ).fetchall()

        removed: list[str] = []
        for row in rows:
            with self._write() as connection:
                connection.execute(
                    "DELETE FROM attachments WHERE id = ? AND pending = 1",
                    (row["id"],),
                )
            removed.append(row["stored_name"])
        return removed

    def _link_turn_attachments(
        self,
        connection: sqlite3.Connection,
        turn_id: int,
        conversation_id: str,
        attachments: Sequence[tuple[str, str]],
    ) -> None:
        for attachment_id, role in attachments:
            if role not in _VALID_ROLES:
                raise AttachmentInvalidError("附件角色不合法。")
            row = connection.execute(
                "SELECT 1 FROM attachments WHERE id = ? AND conversation_id = ?",
                (attachment_id, conversation_id),
            ).fetchone()
            if row is None:
                raise AttachmentInvalidError("附件不存在或不属于当前会话。")
            connection.execute(
                """
                INSERT OR IGNORE INTO turn_attachments
                    (turn_id, attachment_id, role)
                VALUES (?, ?, ?)
                """,
                (turn_id, attachment_id, role),
            )
            connection.execute(
                "UPDATE attachments SET pending = 0 WHERE id = ?",
                (attachment_id,),
            )

    def _replace_problem_attachments(
        self,
        connection: sqlite3.Connection,
        problem_id: str,
        conversation_id: str,
        attachments: Sequence[tuple[str, str]],
    ) -> None:
        for attachment_id, role in attachments:
            if role not in _VALID_ROLES:
                raise AttachmentInvalidError("附件角色不合法。")
            row = connection.execute(
                "SELECT 1 FROM attachments WHERE id = ? AND conversation_id = ?",
                (attachment_id, conversation_id),
            ).fetchone()
            if row is None:
                raise AttachmentInvalidError("附件不存在或不属于当前会话。")
        connection.execute(
            "DELETE FROM problem_attachments WHERE problem_id = ?",
            (problem_id,),
        )
        for attachment_id, role in attachments:
            connection.execute(
                """
                INSERT INTO problem_attachments
                    (problem_id, attachment_id, role)
                VALUES (?, ?, ?)
                """,
                (problem_id, attachment_id, role),
            )
            connection.execute(
                "UPDATE attachments SET pending = 0 WHERE id = ?",
                (attachment_id,),
            )

    def _replace_record_attachments(
        self,
        connection: sqlite3.Connection,
        record_id: str,
        conversation_id: str,
        attachments: Sequence[tuple[str, str]],
    ) -> None:
        for attachment_id, role in attachments:
            if role not in _VALID_ROLES:
                raise AttachmentInvalidError("附件角色不合法。")
            row = connection.execute(
                "SELECT 1 FROM attachments WHERE id = ? AND conversation_id = ?",
                (attachment_id, conversation_id),
            ).fetchone()
            if row is None:
                raise AttachmentInvalidError("附件不存在或不属于当前会话。")
        connection.execute(
            "DELETE FROM mistake_record_attachments WHERE record_id = ?",
            (record_id,),
        )
        for attachment_id, role in attachments:
            connection.execute(
                """
                INSERT INTO mistake_record_attachments
                    (record_id, attachment_id, role)
                VALUES (?, ?, ?)
                """,
                (record_id, attachment_id, role),
            )
            connection.execute(
                "UPDATE attachments SET pending = 0 WHERE id = ?",
                (attachment_id,),
            )

    # ------------------------------------------------------------------
    # 题目
    # ------------------------------------------------------------------

    _PROBLEM_SELECT = "SELECT * FROM problems"

    def get_active_problem(self, conversation_id: str) -> ProblemRecord | None:
        with self._read() as connection:
            row = connection.execute(
                f"{self._PROBLEM_SELECT} WHERE conversation_id = ? AND active = 1",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_problem(connection, row)

    def get_problem(self, problem_id: str) -> ProblemRecord | None:
        with self._read() as connection:
            row = connection.execute(
                f"{self._PROBLEM_SELECT} WHERE id = ?",
                (problem_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_problem(connection, row)

    def get_problem_by_root_version(
        self, root_problem_id: str, version: int
    ) -> ProblemRecord | None:
        with self._read() as connection:
            row = connection.execute(
                f"{self._PROBLEM_SELECT} WHERE root_problem_id = ? AND version = ?",
                (root_problem_id, version),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_problem(connection, row)

    def max_problem_version(self, root_problem_id: str) -> int:
        with self._read() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM problems "
                "WHERE root_problem_id = ?",
                (root_problem_id,),
            ).fetchone()
        return int(row[0])

    def latest_explained_problem(self, root_problem_id: str) -> ProblemRecord | None:
        with self._read() as connection:
            row = connection.execute(
                f"""
                {self._PROBLEM_SELECT}
                WHERE root_problem_id = ? AND status = 'explained'
                ORDER BY version DESC LIMIT 1
                """,
                (root_problem_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_problem(connection, row)

    def get_problem_followups(self, problem_id: str) -> list[dict[str, str]]:
        """读取同一题目的历史追问。

        兼容两类保存方式：F005 的独立 ``followup`` 轮次直接把辅导结果写入
        output_json；F006 动态轮次把它放在 ``tutoring`` 字段中。
        """

        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT query, output_json, started_at
                FROM conversation_turns
                WHERE problem_id = ? AND kind IN ('followup', 'dynamic')
                  AND status = 'completed'
                ORDER BY seq ASC
                """,
                (problem_id,),
            ).fetchall()
        followups: list[dict[str, str]] = []
        for row in rows:
            try:
                raw = _json_load(row["output_json"], {})
                if not isinstance(raw, dict):
                    continue
                candidate = raw
                if isinstance(raw.get("tutoring"), dict):
                    candidate = raw["tutoring"]
                payload = _TUTORING_ADAPTER.validate_python(candidate)
            except (ValidationError, ConversationStorageError, TypeError):
                continue
            if getattr(payload, "kind", None) == "followup_answer":
                followups.append(
                    {
                        "question": row["query"],
                        "answer": payload.answer,
                        "created_at": row["started_at"],
                    }
                )
        return followups

    def _insert_problem_version(
        self,
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        problem: NewProblem,
        attachments: Sequence[tuple[str, str]],
        now: str,
    ) -> ProblemRecord:
        """插入新题或纠正版本，并让旧活动题目让位。不结束轮次。"""

        problem_id = str(uuid.uuid4())
        if problem.root_problem_id is None:
            root_id = problem_id
            version = 1
            connection.execute(
                "UPDATE problems SET active = 0 "
                "WHERE conversation_id = ? AND active = 1",
                (conversation_id,),
            )
        else:
            root_id = problem.root_problem_id
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM problems "
                "WHERE root_problem_id = ? AND conversation_id = ?",
                (root_id, conversation_id),
            ).fetchone()
            version = int(row[0]) + 1
            if problem.supersedes_problem_id:
                connection.execute(
                    "UPDATE problems SET active = 0 "
                    "WHERE id = ? AND conversation_id = ?",
                    (problem.supersedes_problem_id, conversation_id),
                )
            # 纠正历史题时，当前活动题可能是另一道题；新版本要成为唯一
            # 活动题目，必须先让位，避免部分唯一索引冲突。
            connection.execute(
                "UPDATE problems SET active = 0 "
                "WHERE conversation_id = ? AND active = 1",
                (conversation_id,),
            )

        connection.execute(
            """
            INSERT INTO problems (
                id, conversation_id, root_problem_id, supersedes_problem_id,
                version, statement_text, status, clarity,
                unclear_points_json, uncertainties_json,
                source_type, source_text, paper_id, source_title,
                student_answer_text, solution_text, explanation_json,
                self_reported_understood, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
            """,
            (
                problem_id,
                conversation_id,
                root_id,
                problem.supersedes_problem_id,
                version,
                problem.statement_text,
                problem.status,
                problem.clarity,
                _json_dump(problem.unclear_points),
                _json_dump(problem.uncertainties),
                problem.source.type,
                problem.source.text,
                problem.source.paper_id,
                problem.source.paper_title,
                problem.student_answer_text,
                problem.solution_text,
                (
                    _json_dump(problem.explanation.model_dump(mode="json"))
                    if problem.explanation is not None
                    else None
                ),
                now,
                now,
            ),
        )
        self._replace_problem_attachments(
            connection, problem_id, conversation_id, attachments
        )
        row = connection.execute(
            f"{self._PROBLEM_SELECT} WHERE id = ?", (problem_id,)
        ).fetchone()
        assert row is not None
        return self._row_to_problem(connection, row)

    def _update_problem_row(
        self,
        connection: sqlite3.Connection,
        *,
        problem: ProblemRecord,
        updated: UpdatedProblem,
        attachments: Sequence[tuple[str, str]],
        now: str,
    ) -> ProblemRecord:
        """更新同一逻辑版本的题干/讲解/附件。不结束轮次。"""

        row = connection.execute(
            f"{self._PROBLEM_SELECT} WHERE id = ?", (problem.id,)
        ).fetchone()
        if row is None:
            raise ProblemNotFoundError("题目不存在。")

        connection.execute(
            """
            UPDATE problems SET
                statement_text = ?, status = ?, clarity = ?,
                unclear_points_json = ?, uncertainties_json = ?,
                source_type = ?, source_text = ?, paper_id = ?, source_title = ?,
                student_answer_text = ?, solution_text = ?, explanation_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                updated.statement_text,
                updated.status,
                updated.clarity,
                _json_dump(updated.unclear_points),
                _json_dump(updated.uncertainties),
                updated.source.type,
                updated.source.text,
                updated.source.paper_id,
                updated.source.paper_title,
                updated.student_answer_text,
                updated.solution_text,
                (
                    _json_dump(updated.explanation.model_dump(mode="json"))
                    if updated.explanation is not None
                    else None
                ),
                now,
                problem.id,
            ),
        )
        self._replace_problem_attachments(
            connection, problem.id, problem.conversation_id, attachments
        )
        updated_row = connection.execute(
            f"{self._PROBLEM_SELECT} WHERE id = ?", (problem.id,)
        ).fetchone()
        assert updated_row is not None
        return self._row_to_problem(connection, updated_row)

    def save_problem_turn(
        self,
        *,
        conversation_id: str,
        request_id: str,
        kind: str,
        problem: NewProblem,
        attachments: Sequence[tuple[str, str]],
        output_builder: Callable[[ProblemRecord], str],
    ) -> ProblemRecord:
        """保存新题/纠正版本并完成轮次；旧活动题目在同一事务内让位。

        output_builder 在题目行、附件关联都写入后调用，确保事件里的题目 ID
        和附件 URL 来自真实保存结果。
        """

        now = _now()
        with self._write() as connection:
            self._check_conversation_exists(connection, conversation_id)
            record = self._insert_problem_version(
                connection,
                conversation_id=conversation_id,
                problem=problem,
                attachments=attachments,
                now=now,
            )
            self._complete_turn(
                connection,
                conversation_id,
                request_id,
                output_json=output_builder(record),
                problem_id=record.id,
                kind=kind,
                now=now,
            )
            self._maybe_set_title_from_problem(
                connection, conversation_id, record.statement_text, now
            )

        return record

    def create_problem_version(
        self,
        *,
        conversation_id: str,
        problem: NewProblem,
        attachments: Sequence[tuple[str, str]] = (),
    ) -> ProblemRecord:
        """动态调度中保存新题或纠正版本，但由调用方统一结束本轮。"""

        now = _now()
        with self._write() as connection:
            self._check_conversation_exists(connection, conversation_id)
            return self._insert_problem_version(
                connection,
                conversation_id=conversation_id,
                problem=problem,
                attachments=attachments,
                now=now,
            )

    def update_problem_content(
        self,
        problem: ProblemRecord,
        *,
        updated: UpdatedProblem,
        attachments: Sequence[tuple[str, str]] = (),
    ) -> ProblemRecord:
        """动态调度中补充尚未讲清的题目，但由调用方统一结束本轮。"""

        now = _now()
        with self._write() as connection:
            return self._update_problem_row(
                connection,
                problem=problem,
                updated=updated,
                attachments=attachments,
                now=now,
            )

    def save_supplement_turn(
        self,
        *,
        problem: ProblemRecord,
        request_id: str,
        updated: UpdatedProblem,
        attachments: Sequence[tuple[str, str]],
        output_builder: Callable[[ProblemRecord], str],
    ) -> ProblemRecord:
        now = _now()
        with self._write() as connection:
            result = self._update_problem_row(
                connection,
                problem=problem,
                updated=updated,
                attachments=attachments,
                now=now,
            )
            self._complete_turn(
                connection,
                problem.conversation_id,
                request_id,
                output_json=output_builder(result),
                problem_id=problem.id,
                kind="supplement",
                now=now,
            )
            self._maybe_set_title_from_problem(
                connection, problem.conversation_id, result.statement_text, now
            )

        return result

    def create_problem(
        self,
        *,
        conversation_id: str,
        statement_text: str,
        id: str | None = None,
        root_problem_id: str | None = None,
        supersedes_problem_id: str | None = None,
        status: str = "explained",
        clarity: str = "clear",
        explanation: Explanation | None = None,
        source: Source | None = None,
    ) -> ProblemRecord:
        now = _now()
        created_id = id or str(uuid.uuid4())
        root_id = root_problem_id or created_id
        src = source or Source(type="none")

        with self._write() as connection:
            self._check_conversation_exists(connection, conversation_id)
            connection.execute(
                "UPDATE problems SET active = 0 WHERE conversation_id = ? AND active = 1",
                (conversation_id,),
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM problems WHERE root_problem_id = ? AND conversation_id = ?",
                (root_id, conversation_id),
            ).fetchone()
            version = int(row[0]) + 1

            connection.execute(
                """
                INSERT INTO problems (
                    id, conversation_id, root_problem_id, supersedes_problem_id,
                    version, statement_text, status, clarity,
                    unclear_points_json, uncertainties_json,
                    source_type, source_text, paper_id, source_title,
                    student_answer_text, solution_text, explanation_json,
                    self_reported_understood, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
                """,
                (
                    created_id,
                    conversation_id,
                    root_id,
                    supersedes_problem_id,
                    version,
                    statement_text,
                    status,
                    clarity,
                    None,
                    None,
                    src.type,
                    src.text,
                    src.paper_id,
                    src.paper_title,
                    None,
                    None,
                    _json_dump(explanation.model_dump(mode="json")) if explanation else None,
                    now,
                    now,
                ),
            )
            created_row = connection.execute(
                f"{self._PROBLEM_SELECT} WHERE id = ?",
                (created_id,),
            ).fetchone()
            assert created_row is not None
            return self._row_to_problem(connection, created_row)

    def update_problem(
        self,
        problem_id: str,
        *,
        statement_text: str | None = None,
        status: str | None = None,
        clarity: str | None = None,
        explanation: Explanation | None = None,
    ) -> ProblemRecord:
        now = _now()
        with self._write() as connection:
            row = connection.execute("SELECT * FROM problems WHERE id = ?", (problem_id,)).fetchone()
            if row is None:
                raise ProblemNotFoundError("题目不存在。")
            new_statement = statement_text if statement_text is not None else row["statement_text"]
            new_status = status if status is not None else row["status"]
            new_clarity = clarity if clarity is not None else row["clarity"]
            new_expl = _json_dump(explanation.model_dump(mode="json")) if explanation else row["explanation_json"]
            connection.execute(
                """
                UPDATE problems
                SET statement_text = ?, status = ?, clarity = ?, explanation_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_statement, new_status, new_clarity, new_expl, now, problem_id),
            )
            updated_row = connection.execute(
                f"{self._PROBLEM_SELECT} WHERE id = ?",
                (problem_id,),
            ).fetchone()
            assert updated_row is not None
            return self._row_to_problem(connection, updated_row)

    def save_followup_turn(
        self,
        *,
        problem: ProblemRecord,
        request_id: str,
        question: str,
        answer: str,
        output_json: str,
    ) -> None:
        now = _now()
        with self._write() as connection:
            connection.execute(
                "UPDATE problems SET updated_at = ? WHERE id = ?",
                (now, problem.id),
            )
            self._complete_turn(
                connection,
                problem.conversation_id,
                request_id,
                output_json=output_json,
                problem_id=problem.id,
                kind="followup",
                now=now,
            )
            self._append_record_followup(
                connection,
                conversation_id=problem.conversation_id,
                root_problem_id=problem.root_problem_id,
                question=question,
                answer=answer,
                created_at=now,
            )

    def save_feedback_turn(
        self,
        *,
        problem: ProblemRecord,
        request_id: str,
        output_json: str,
    ) -> None:
        now = _now()
        with self._write() as connection:
            connection.execute(
                "UPDATE problems SET self_reported_understood = 1, updated_at = ? "
                "WHERE id = ?",
                (now, problem.id),
            )
            self._complete_turn(
                connection,
                problem.conversation_id,
                request_id,
                output_json=output_json,
                problem_id=problem.id,
                kind="feedback",
                now=now,
            )
            record = self._find_record_by_root(
                connection, problem.conversation_id, problem.root_problem_id
            )
            if record is not None:
                feedback = dict(record.feedback)
                feedback["self_reported_at"] = now
                connection.execute(
                    "UPDATE mistake_records SET self_reported_understood = 1, "
                    "feedback_json = ?, updated_at = ? WHERE id = ?",
                    (_json_dump(feedback), now, record.id),
                )

    def _append_record_followup(
        self,
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        root_problem_id: str,
        question: str,
        answer: str,
        created_at: str,
    ) -> None:
        record = self._find_record_by_root(
            connection, conversation_id, root_problem_id
        )
        if record is None:
            return
        followups = list(record.followups)
        followups.append(
            {"question": question, "answer": answer, "created_at": created_at}
        )
        connection.execute(
            "UPDATE mistake_records SET followups_json = ?, updated_at = ? "
            "WHERE id = ?",
            (_json_dump(followups), created_at, record.id),
        )

    # ------------------------------------------------------------------
    # 错题记录
    # ------------------------------------------------------------------

    _RECORD_SELECT = "SELECT * FROM mistake_records"

    def create_record(
        self,
        snapshot: RecordSnapshot,
        *,
        turn: tuple[str, str] | None = None,
        output_builder: Callable[[RecordRow, bool], str] | None = None,
    ) -> tuple[RecordRow, bool]:
        """创建记录；同一会话+根题目已存在时返回已有记录。

        传入 turn=(conversation_id, request_id) 时，记录插入与轮次完成在同一
        事务内；output_builder 在事务内根据“新建/已存在”生成最终事件 JSON。
        """

        now = _now()
        with self._write() as connection:
            existing = self._find_record_by_root(
                connection, snapshot.conversation_id, snapshot.root_problem_id
            )
            if existing is not None:
                record = existing
                created = False
            else:
                record_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO mistake_records (
                        id, conversation_id, problem_id, root_problem_id,
                        problem_version, request_id, title, statement_text,
                        student_answer_text, solution_text, source_json,
                        explanation_json, followups_json, feedback_json,
                        self_reported_understood, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        snapshot.conversation_id,
                        snapshot.problem_id,
                        snapshot.root_problem_id,
                        snapshot.problem_version,
                        snapshot.request_id,
                        snapshot.title,
                        snapshot.statement_text,
                        snapshot.student_answer_text,
                        snapshot.solution_text,
                        _json_dump(snapshot.source.model_dump(mode="json")),
                        (
                            _json_dump(
                                snapshot.explanation.model_dump(mode="json")
                            )
                            if snapshot.explanation is not None
                            else None
                        ),
                        _json_dump(snapshot.followups),
                        _json_dump(snapshot.feedback),
                        int(snapshot.self_reported_understood),
                        now,
                        now,
                    ),
                )
                self._replace_record_attachments(
                    connection,
                    record_id,
                    snapshot.conversation_id,
                    snapshot.attachments,
                )
                record = self._load_record(connection, record_id)
                created = True

            if turn is not None:
                conversation_id, request_id = turn
                if output_builder is None:
                    raise ValueError("完成轮次时必须提供 output_builder。")
                output_json = output_builder(record, created)
                self._complete_turn(
                    connection,
                    conversation_id,
                    request_id,
                    output_json=output_json,
                    problem_id=record.problem_id,
                    record_id=record.id,
                    kind="record",
                    now=now,
                )

        return record, created

    def get_record(self, record_id: str) -> RecordRow | None:
        with self._read() as connection:
            row = connection.execute(
                f"{self._RECORD_SELECT} WHERE id = ?", (record_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_record(connection, row)

    def get_record_by_root(
        self, conversation_id: str, root_problem_id: str
    ) -> RecordRow | None:
        with self._read() as connection:
            return self._find_record_by_root(
                connection, conversation_id, root_problem_id
            )

    def list_records(self, *, limit: int, offset: int) -> tuple[list[RecordRow], int]:
        with self._read() as connection:
            total = int(
                connection.execute("SELECT COUNT(*) FROM mistake_records").fetchone()[0]
            )
            rows = connection.execute(
                f"""
                {self._RECORD_SELECT}
                ORDER BY created_at DESC, id ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            records = [self._row_to_record(connection, row) for row in rows]
        return records, total

    def update_record_metadata(
        self,
        record_id: str,
        *,
        title: str | None = None,
        source: Source | None = None,
        feedback_text: str | None = None,
        self_reported_understood: bool | None = None,
    ) -> RecordRow:
        now = _now()
        with self._write() as connection:
            row = connection.execute(
                f"{self._RECORD_SELECT} WHERE id = ?", (record_id,)
            ).fetchone()
            if row is None:
                raise MistakeRecordNotFoundError("错题记录不存在。")

            current = self._row_to_record(connection, row)
            feedback = dict(current.feedback)
            if feedback_text is not None:
                feedback["text"] = feedback_text
                feedback["updated_at"] = now
            if self_reported_understood is True:
                feedback["self_reported_at"] = now
            elif self_reported_understood is False:
                feedback.pop("self_reported_at", None)
            if self_reported_understood is not None:
                feedback["updated_at"] = now

            connection.execute(
                """
                UPDATE mistake_records SET
                    title = ?, source_json = ?, feedback_json = ?,
                    self_reported_understood = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    title if title is not None else current.title,
                    _json_dump(
                        (source or current.source).model_dump(mode="json")
                    ),
                    _json_dump(feedback),
                    (
                        int(self_reported_understood)
                        if self_reported_understood is not None
                        else int(current.self_reported_understood)
                    ),
                    now,
                    record_id,
                ),
            )
            updated = self._load_record(connection, record_id)
        return updated

    def replace_record(
        self,
        record_id: str,
        *,
        request_id: str,
        reason: str,
        new_snapshot: RecordSnapshot,
        request_input: str,
    ) -> tuple[RecordRow, bool]:
        """用新快照替换当前记录，并把旧快照写入修订；request_id 幂等。

        同一个 request_id 重试时比较完整纠正输入；内容不同必须冲突，不能
        回放旧结果。
        """

        now = _now()
        with self._write() as connection:
            row = connection.execute(
                f"{self._RECORD_SELECT} WHERE id = ?", (record_id,)
            ).fetchone()
            if row is None:
                raise MistakeRecordNotFoundError("错题记录不存在。")

            duplicate = connection.execute(
                "SELECT snapshot_json FROM mistake_record_revisions "
                "WHERE record_id = ? AND request_id = ?",
                (record_id, request_id),
            ).fetchone()
            if duplicate is not None:
                previous = _json_load(duplicate["snapshot_json"], {})
                previous_input = (
                    previous.get("_request_input")
                    if isinstance(previous, dict)
                    else None
                )
                if previous_input is not None and previous_input != request_input:
                    raise MistakeRecordConflictError(
                        "同一个 request_id 不能提交不同的纠正内容。"
                    )
                return self._load_record(connection, record_id), False

            current = self._row_to_record(connection, row)
            snapshot = self._record_snapshot(current)
            snapshot["_request_input"] = request_input
            connection.execute(
                """
                INSERT INTO mistake_record_revisions
                    (record_id, request_id, reason, snapshot_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    request_id,
                    reason,
                    _json_dump(snapshot),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE mistake_records SET
                    problem_id = ?, problem_version = ?, title = ?,
                    statement_text = ?, student_answer_text = ?, solution_text = ?,
                    source_json = ?, explanation_json = ?, followups_json = ?,
                    feedback_json = ?, self_reported_understood = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_snapshot.problem_id,
                    new_snapshot.problem_version,
                    new_snapshot.title,
                    new_snapshot.statement_text,
                    new_snapshot.student_answer_text,
                    new_snapshot.solution_text,
                    _json_dump(new_snapshot.source.model_dump(mode="json")),
                    (
                        _json_dump(new_snapshot.explanation.model_dump(mode="json"))
                        if new_snapshot.explanation is not None
                        else None
                    ),
                    _json_dump(new_snapshot.followups),
                    _json_dump(new_snapshot.feedback),
                    int(new_snapshot.self_reported_understood),
                    now,
                    record_id,
                ),
            )
            self._replace_record_attachments(
                connection,
                record_id,
                new_snapshot.conversation_id,
                new_snapshot.attachments,
            )
            updated = self._load_record(connection, record_id)
        return updated, True

    def find_record_revision(
        self, record_id: str, request_id: str
    ) -> RevisionRow | None:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT * FROM mistake_record_revisions
                WHERE record_id = ? AND request_id = ?
                """,
                (record_id, request_id),
            ).fetchone()
        return None if row is None else self._row_to_revision(row)

    def list_record_revisions(self, record_id: str) -> list[RevisionRow]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM mistake_record_revisions
                WHERE record_id = ? ORDER BY id ASC
                """,
                (record_id,),
            ).fetchall()
        return [self._row_to_revision(row) for row in rows]

    def delete_record(self, record_id: str) -> None:
        with self._write() as connection:
            cursor = connection.execute(
                "DELETE FROM mistake_records WHERE id = ?", (record_id,)
            )
            if cursor.rowcount != 1:
                raise MistakeRecordNotFoundError("错题记录不存在。")

    def _find_record_by_root(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        root_problem_id: str,
    ) -> RecordRow | None:
        row = connection.execute(
            f"""
            {self._RECORD_SELECT}
            WHERE conversation_id = ? AND root_problem_id = ?
            """,
            (conversation_id, root_problem_id),
        ).fetchone()
        return None if row is None else self._row_to_record(connection, row)

    def _load_record(
        self, connection: sqlite3.Connection, record_id: str
    ) -> RecordRow:
        row = connection.execute(
            f"{self._RECORD_SELECT} WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise MistakeRecordNotFoundError("错题记录不存在。")
        return self._row_to_record(connection, row)

    def _record_snapshot(self, record: RecordRow) -> dict[str, object]:
        return {
            "problem_id": record.problem_id,
            "problem_version": record.problem_version,
            "statement_text": record.statement_text,
            "student_answer_text": record.student_answer_text,
            "solution_text": record.solution_text,
            "source": record.source.model_dump(mode="json"),
            "explanation": (
                record.explanation.model_dump(mode="json")
                if record.explanation is not None
                else None
            ),
            "followups": record.followups,
            "feedback": record.feedback,
            "self_reported_understood": record.self_reported_understood,
            "attachments": [
                {"id": item.id, "role": item.role}
                for item in record.attachments
            ],
        }

    # ------------------------------------------------------------------
    # 行转换
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_summary(row: sqlite3.Row) -> ConversationSummary:
        try:
            return ConversationSummary(
                id=row["id"],
                title=row["title"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        except ValueError as exc:
            raise ConversationStorageError(_CORRUPT_MESSAGE) from exc

    def _row_to_turn(self, row: sqlite3.Row) -> Turn:
        result: ResultEvent | None = None
        if row["result_json"]:
            try:
                payload = _json_load(row["result_json"], {})
                result = ResultEvent.model_validate(payload)
            except (TypeError, ValueError, ValidationError) as exc:
                raise ConversationStorageError(_CORRUPT_MESSAGE) from exc

        tutoring = None
        exercise = None
        evaluation = None
        composite_results = []
        assistant_message = None
        suggestions = []
        recovery = {}

        if row["output_json"]:
            try:
                raw_payload = _json_load(row["output_json"], {})
                if isinstance(raw_payload, dict):
                    recovery = raw_payload.get("_recovery") or {}
                    if raw_payload.get("kind") in {
                        "explanation",
                        "needs_clarification",
                        "followup_answer",
                        "feedback_recorded",
                        "record_created",
                        "record_exists",
                        "intent_clarification",
                    }:
                        tutoring = _TUTORING_ADAPTER.validate_python(raw_payload)
                    else:
                        if "exercise" in raw_payload and raw_payload["exercise"]:
                            with suppress(Exception):
                                exercise = ExerciseView.model_validate(raw_payload["exercise"])
                        if "evaluation" in raw_payload and raw_payload["evaluation"]:
                            with suppress(Exception):
                                evaluation = ExerciseEvaluation.model_validate(raw_payload["evaluation"])
                        if "composite_results" in raw_payload and raw_payload["composite_results"]:
                            for it in raw_payload["composite_results"]:
                                with suppress(Exception):
                                    composite_results.append(ActionResultItem.model_validate(it))
                        assistant_message = raw_payload.get("assistant_message")
                        if "suggestions" in raw_payload and raw_payload["suggestions"]:
                            for it in raw_payload["suggestions"]:
                                with suppress(Exception):
                                    suggestions.append(SuggestionItem.model_validate(it))
                        if "tutoring" in raw_payload and raw_payload["tutoring"]:
                            with suppress(Exception):
                                tutoring = _TUTORING_ADAPTER.validate_python(raw_payload["tutoring"])
            except (TypeError, ValueError, ValidationError) as exc:
                raise ConversationStorageError(_CORRUPT_MESSAGE) from exc

        if row["status"] in ("failed", "interrupted") and recovery:
            composite_results = [ActionResultItem.model_validate(item["outcome"])
                                 for item in recovery.get("actions", []) if item.get("outcome")]
            snapshot = recovery.get("context") or {}
            result = ResultEvent.model_validate(snapshot["result"]) if snapshot.get("result") else result
            tutoring = _TUTORING_ADAPTER.validate_python(snapshot["tutoring"]) if snapshot.get("tutoring") else tutoring
            exercise = ExerciseView.model_validate(snapshot["exercise_view"]) if snapshot.get("exercise_view") else exercise
            evaluation = ExerciseEvaluation.model_validate(snapshot["evaluation_view"]) if snapshot.get("evaluation_view") else evaluation
        error: TurnError | None = None
        if row["error_code"] is not None:
            try:
                error = TurnError(
                    code=row["error_code"],
                    message=row["error_message"] or "查询过程中发生错误，请重试。",
                )
            except ValueError as exc:
                raise ConversationStorageError(_CORRUPT_MESSAGE) from exc

        try:
            finished_raw = row["finished_at"]
            ex_id = row["exercise_id"] if "exercise_id" in row.keys() else None
            if not ex_id and row["status"] != "running":
                ex_id = recovery.get("exercise_id")
            if not ex_id and exercise:
                ex_id = exercise.id
            if ex_id:
                with suppress(Exception):
                    latest_ex = self.get_exercise(ex_id)
                    if latest_ex:
                        request_ids = [row["request_id"]]
                        retry_of = (recovery.get("input") or {}).get("retry_of")
                        if not retry_of:
                            with suppress(Exception):
                                retry_of = json.loads(
                                    row["input_json"] or "{}"
                                ).get("retry_of")
                        if retry_of:
                            request_ids.append(retry_of)
                        matched = next(
                            (
                                item
                                for item in latest_ex.attempts
                                if item.request_id in request_ids
                            ),
                            None,
                        )
                        exercise = self.view_from_exercise(
                            latest_ex, attempt=matched
                        )
                        if exercise.evaluation:
                            evaluation = exercise.evaluation

            raw_kind = row["kind"] or "query"
            # F006 修复前把作答轮次保存为 exercise_answer；读取时归一为
            # 当前契约的 exercise，旧数据仍可刷新和回放。
            if raw_kind == "exercise_answer":
                raw_kind = "exercise"
            turn = Turn(
                request_id=row["request_id"],
                seq=row["seq"],
                kind=raw_kind,
                query=row["query"],
                status=row["status"],
                result=result,
                tutoring=tutoring,
                exercise=exercise,
                evaluation=evaluation,
                composite_results=composite_results,
                assistant_message=assistant_message,
                suggestions=suggestions,
                exercise_id=ex_id,
                error=error,
                started_at=datetime.fromisoformat(row["started_at"]),
                finished_at=(
                    None if finished_raw is None else datetime.fromisoformat(finished_raw)
                ),
                input_json=row["input_json"],
                recovery=recovery,
            )
            if turn.retryable:
                with self._read() as connection:
                    retried = connection.execute(
                        """SELECT seq FROM conversation_turns WHERE conversation_id=?
                           AND json_valid(input_json) AND json_extract(input_json, '$.retry_of')=?
                           ORDER BY seq DESC LIMIT 1""", (row["conversation_id"], turn.request_id),
                    ).fetchone()
                if retried:
                    turn.retried_by_seq = retried["seq"]
                    turn.retryable = False
                    turn.retry_hint = f"该轮次已重试，请查看第 {retried['seq']} 轮的结果。"
            return turn
        except ValueError as exc:
            raise ConversationStorageError(_CORRUPT_MESSAGE) from exc

    def _row_to_attachment(
        self, row: sqlite3.Row, role: str | None = None
    ) -> AttachmentRecord:
        try:
            return AttachmentRecord(
                id=row["id"],
                conversation_id=row["conversation_id"],
                stored_name=row["stored_name"],
                mime=row["mime"],
                width=row["width"],
                height=row["height"],
                size_bytes=row["size_bytes"],
                sha256=row["sha256"],
                pending=bool(row["pending"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                role=role,
            )
        except ValueError as exc:
            raise ConversationStorageError(_CORRUPT_MESSAGE) from exc

    def _row_to_problem(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> ProblemRecord:
        try:
            attachments = self._load_problem_attachments(connection, row["id"])
            explanation = None
            if row["explanation_json"]:
                explanation = Explanation.model_validate(
                    _json_load(row["explanation_json"], {})
                )
            source = Source(
                type=row["source_type"],
                text=row["source_text"],
                paper_id=row["paper_id"],
                paper_title=row["source_title"],
            )
            return ProblemRecord(
                id=row["id"],
                conversation_id=row["conversation_id"],
                root_problem_id=row["root_problem_id"],
                supersedes_problem_id=row["supersedes_problem_id"],
                version=row["version"],
                statement_text=row["statement_text"],
                status=row["status"],
                clarity=row["clarity"],
                unclear_points=list(_json_load(row["unclear_points_json"], [])),
                uncertainties=list(_json_load(row["uncertainties_json"], [])),
                source=source,
                student_answer_text=row["student_answer_text"],
                solution_text=row["solution_text"],
                explanation=explanation,
                self_reported_understood=bool(row["self_reported_understood"]),
                active=bool(row["active"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                attachments=attachments,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ConversationStorageError(_CORRUPT_MESSAGE) from exc

    def _load_problem_attachments(
        self, connection: sqlite3.Connection, problem_id: str
    ) -> list[AttachmentRecord]:
        rows = connection.execute(
            """
            SELECT a.*, pa.role AS role
            FROM problem_attachments pa
            JOIN attachments a ON a.id = pa.attachment_id
            WHERE pa.problem_id = ?
            ORDER BY pa.role ASC, a.id ASC
            """,
            (problem_id,),
        ).fetchall()
        return [self._row_to_attachment(row, row["role"]) for row in rows]

    def _row_to_record(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> RecordRow:
        try:
            source = Source.model_validate(_json_load(row["source_json"], {}))
            explanation = None
            if row["explanation_json"]:
                explanation = Explanation.model_validate(
                    _json_load(row["explanation_json"], {})
                )
            followups_raw = _json_load(row["followups_json"], [])
            if not isinstance(followups_raw, list):
                raise ValueError("followups 不是数组")
            followups = [
                {
                    "question": str(item.get("question", "")),
                    "answer": str(item.get("answer", "")),
                    "created_at": str(item.get("created_at", "")),
                }
                for item in followups_raw
                if isinstance(item, dict)
            ]
            feedback_raw = _json_load(row["feedback_json"], {})
            feedback = feedback_raw if isinstance(feedback_raw, dict) else {}
            attachments = self._load_record_attachments(connection, row["id"])
            return RecordRow(
                id=row["id"],
                conversation_id=row["conversation_id"],
                problem_id=row["problem_id"],
                root_problem_id=row["root_problem_id"],
                problem_version=row["problem_version"],
                request_id=row["request_id"],
                title=row["title"],
                statement_text=row["statement_text"],
                student_answer_text=row["student_answer_text"],
                solution_text=row["solution_text"],
                source=source,
                explanation=explanation,
                followups=followups,
                feedback=feedback,
                self_reported_understood=bool(row["self_reported_understood"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                attachments=attachments,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ConversationStorageError(_CORRUPT_MESSAGE) from exc

    def _load_record_attachments(
        self, connection: sqlite3.Connection, record_id: str
    ) -> list[AttachmentRecord]:
        rows = connection.execute(
            """
            SELECT a.*, ma.role AS role
            FROM mistake_record_attachments ma
            JOIN attachments a ON a.id = ma.attachment_id
            WHERE ma.record_id = ?
            ORDER BY ma.role ASC, a.id ASC
            """,
            (record_id,),
        ).fetchall()
        return [self._row_to_attachment(row, row["role"]) for row in rows]

    def _row_to_revision(self, row: sqlite3.Row) -> RevisionRow:
        try:
            snapshot = _json_load(row["snapshot_json"], {})
            if not isinstance(snapshot, dict):
                raise ValueError("修订快照不是对象")
            return RevisionRow(
                id=row["id"],
                record_id=row["record_id"],
                request_id=row["request_id"],
                reason=row["reason"],
                snapshot=snapshot,
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        except (TypeError, ValueError) as exc:
            raise ConversationStorageError(_CORRUPT_MESSAGE) from exc

    # ------------------------------------------------------------------
    # F006：完成动态轮次、练习管理与上下文
    # ------------------------------------------------------------------

    @staticmethod
    def _check_conversation_exists(
        connection: sqlite3.Connection, conversation_id: str
    ) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            is None
        ):
            raise ConversationNotFoundError("会话不存在。")

    def complete_dynamic_turn(
        self,
        conversation_id: str,
        request_id: str,
        *,
        kind: str,
        result: ResultEvent | None = None,
        tutoring: TutoringResult | None = None,
        exercise: ExerciseView | None = None,
        evaluation: ExerciseEvaluation | None = None,
        composite_results: list[ActionResultItem] | None = None,
        assistant_message: str | None = None,
        suggestions: list[SuggestionItem] | None = None,
        problem_id: str | None = None,
        exercise_id: str | None = None,
    ) -> None:
        result_json = None
        if result is not None:
            result_json = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)

        output_payload: dict[str, Any] = {}
        if tutoring is not None:
            output_payload["tutoring"] = tutoring.model_dump(mode="json")
        if exercise is not None:
            output_payload["exercise"] = exercise.model_dump(mode="json")
        if evaluation is not None:
            output_payload["evaluation"] = evaluation.model_dump(mode="json")
        if composite_results:
            output_payload["composite_results"] = [
                it.model_dump(mode="json") for it in composite_results
            ]
        if assistant_message is not None:
            output_payload["assistant_message"] = assistant_message
        if suggestions:
            output_payload["suggestions"] = [
                it.model_dump(mode="json") for it in suggestions
            ]

        output_json = (
            json.dumps(output_payload, ensure_ascii=False) if output_payload else None
        )
        now = _now()

        with self._write() as connection:
            previous = connection.execute(
                "SELECT output_json FROM conversation_turns WHERE conversation_id=? AND request_id=?",
                (conversation_id, request_id),
            ).fetchone()
            if previous and previous["output_json"]:
                recovery = _json_load(previous["output_json"], {}).get("_recovery")
                if recovery:
                    output_payload["_recovery"] = recovery
                    output_json = json.dumps(output_payload, ensure_ascii=False)
            cursor = connection.execute(
                """
                UPDATE conversation_turns
                SET status = 'completed', kind = ?, result_json = ?, output_json = ?,
                    problem_id = ?, exercise_id = ?, error_code = NULL, error_message = NULL,
                    finished_at = ?
                WHERE conversation_id = ? AND request_id = ? AND status = 'running'
                """,
                (
                    kind,
                    result_json,
                    output_json,
                    problem_id,
                    exercise_id,
                    now,
                    conversation_id,
                    request_id,
                ),
            )
            if cursor.rowcount == 0:
                self._check_conversation_exists(connection, conversation_id)
                raise ConversationConflictError("该轮次不是运行中状态，无法完成。")

            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )

    def create_exercise(
        self,
        *,
        id: str,
        conversation_id: str,
        problem_id: str,
        root_problem_id: str,
        statement_text: str,
        reference_solution_text: str,
        knowledge_point: str,
    ) -> ExerciseRecord:
        now = _now()
        with self._write() as connection:
            self._check_conversation_exists(connection, conversation_id)
            connection.execute(
                """
                INSERT INTO exercises (
                    id, conversation_id, problem_id, root_problem_id,
                    statement_text, reference_solution_text, knowledge_point,
                    status, answered_after_revealed, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_answer', 0, ?, ?)
                """,
                (
                    id,
                    conversation_id,
                    problem_id,
                    root_problem_id,
                    statement_text,
                    reference_solution_text,
                    knowledge_point,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO conversation_contexts (
                    conversation_id, active_exercise_id, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    active_exercise_id = excluded.active_exercise_id,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, id, now),
            )
            return self._get_exercise(connection, id)

    def get_exercise(self, exercise_id: str) -> ExerciseRecord | None:
        with self._read() as connection:
            return self._find_exercise(connection, exercise_id)

    def get_active_exercise(self, conversation_id: str) -> ExerciseRecord | None:
        with self._read() as connection:
            ctx = connection.execute(
                "SELECT active_exercise_id FROM conversation_contexts WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if not ctx or not ctx["active_exercise_id"]:
                return None
            return self._find_exercise(connection, ctx["active_exercise_id"])

    def clear_active_exercise(self, conversation_id: str) -> None:
        """题目进入新版本时清除练习指针；旧练习仍可按 ID 查看。"""

        now = _now()
        with self._write() as connection:
            connection.execute(
                "UPDATE conversation_contexts SET active_exercise_id = NULL, "
                "updated_at = ? WHERE conversation_id = ?",
                (now, conversation_id),
            )

    def reveal_exercise_solution(
        self, exercise_id: str, conversation_id: str | None = None
    ) -> ExerciseRecord:
        now = _now()
        with self._write() as connection:
            ex = self._find_exercise(connection, exercise_id)
            if ex is None:
                raise ProblemNotFoundError("未找到指定的练习题。")
            if conversation_id is not None and ex.conversation_id != conversation_id:
                raise ProblemNotFoundError("练习题不属于当前会话。")
            if ex.status == "pending_answer":
                new_status = "revealed"
            else:
                new_status = ex.status

            revealed_at = ex.revealed_solution_at.isoformat() if ex.revealed_solution_at else now
            connection.execute(
                """
                UPDATE exercises
                SET status = ?, revealed_solution_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_status, revealed_at, now, exercise_id),
            )
            return self._get_exercise(connection, exercise_id)

    # ------------------------------------------------------------------
    # 作答尝试：首次、补充、订正与评价
    # ------------------------------------------------------------------

    def _attempt_rows(
        self, connection: sqlite3.Connection, exercise_id: str
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT * FROM exercise_attempts
            WHERE exercise_id = ?
            ORDER BY attempt_no ASC
            """,
            (exercise_id,),
        ).fetchall()

    def _insert_attempt(
        self,
        connection: sqlite3.Connection,
        exercise_id: str,
        *,
        kind: str,
        student_answer_text: str | None,
        student_solution_text: str | None,
        attachments: Sequence[tuple[str, str]],
        answered_after_revealed: bool,
        inherit_attachments: bool = False,
        request_id: str | None = None,
    ) -> str:
        rows = self._attempt_rows(connection, exercise_id)
        next_no = (rows[-1]["attempt_no"] + 1) if rows else 1
        if next_no == 1 and kind != "initial":
            raise ConversationConflictError("请先保存首次作答。")
        if next_no > 1 and kind == "initial":
            raise ConversationConflictError("首次作答已经保存。")
        attempt_id = str(uuid.uuid4())
        now = _now()
        connection.execute(
            """
            INSERT INTO exercise_attempts (
                id, exercise_id, attempt_no, kind,
                student_answer_text, student_solution_text,
                answered_after_revealed, evaluation_json, request_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                attempt_id,
                exercise_id,
                next_no,
                kind,
                student_answer_text,
                student_solution_text,
                1 if answered_after_revealed else 0,
                request_id,
                now,
                now,
            ),
        )
        for attachment_id, role in attachments:
            connection.execute(
                """
                INSERT OR IGNORE INTO exercise_attempt_attachments (
                    attempt_id, attachment_id, role
                ) VALUES (?, ?, ?)
                """,
                (attempt_id, attachment_id, role),
            )
        if next_no == 1:
            # 兼容旧入口：作答图片可能先挂在 exercises 上。
            connection.execute(
                """
                INSERT OR IGNORE INTO exercise_attempt_attachments (
                    attempt_id, attachment_id, role
                )
                SELECT ?, attachment_id, role
                FROM exercise_attachments
                WHERE exercise_id = ?
                """,
                (attempt_id, exercise_id),
            )
        elif inherit_attachments:
            previous_id = rows[-1]["id"]
            connection.execute(
                """
                INSERT OR IGNORE INTO exercise_attempt_attachments (
                    attempt_id, attachment_id, role
                )
                SELECT ?, attachment_id, role
                FROM exercise_attempt_attachments
                WHERE attempt_id = ?
                """,
                (attempt_id, previous_id),
            )
        connection.execute(
            """
            UPDATE exercises
            SET status = 'pending_answer',
                student_answer_text = ?,
                student_solution_text = ?,
                answered_after_revealed = ?,
                evaluation_json = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (
                student_answer_text,
                student_solution_text,
                1 if answered_after_revealed else 0,
                now,
                exercise_id,
            ),
        )
        return attempt_id

    def _update_pending_attempt(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        *,
        student_answer_text: str | None,
        student_solution_text: str | None,
        attachments: Sequence[tuple[str, str]],
    ) -> str:
        row = connection.execute(
            "SELECT * FROM exercise_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise ProblemNotFoundError("未找到这次作答记录。")
        if row["evaluation_json"] is not None:
            raise ConversationConflictError("这次作答已经完成评价，不能覆盖原结果。")
        now = _now()
        connection.execute(
            """
            UPDATE exercise_attempts
            SET student_answer_text = ?, student_solution_text = ?, updated_at = ?
            WHERE id = ? AND evaluation_json IS NULL
            """,
            (
                student_answer_text,
                student_solution_text,
                now,
                attempt_id,
            ),
        )
        for attachment_id, role in attachments:
            connection.execute(
                """
                INSERT OR IGNORE INTO exercise_attempt_attachments (
                    attempt_id, attachment_id, role
                ) VALUES (?, ?, ?)
                """,
                (attempt_id, attachment_id, role),
            )
        connection.execute(
            """
            UPDATE exercises
            SET student_answer_text = ?, student_solution_text = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                student_answer_text,
                student_solution_text,
                now,
                row["exercise_id"],
            ),
        )
        return row["exercise_id"]

    def create_exercise_attempt(
        self,
        exercise_id: str,
        *,
        kind: str,
        student_answer_text: str | None,
        student_solution_text: str | None,
        attachments: Sequence[tuple[str, str]] = (),
        answered_after_revealed: bool = False,
        request_id: str | None = None,
    ) -> ExerciseRecord:
        """新增一次不可变尝试；只有评价会写回该尝试。"""

        if kind not in ("initial", "supplement", "correction"):
            raise ConversationConflictError("未知的作答类型。")
        with self._write() as connection:
            ex = self._find_exercise(connection, exercise_id)
            if ex is None:
                raise ProblemNotFoundError("未找到指定的练习题。")
            self._insert_attempt(
                connection,
                exercise_id,
                kind=kind,
                student_answer_text=student_answer_text,
                student_solution_text=student_solution_text,
                attachments=attachments,
                answered_after_revealed=answered_after_revealed,
                inherit_attachments=(kind == "supplement"),
                request_id=request_id,
            )
            return self._get_exercise(connection, exercise_id)

    def update_pending_exercise_attempt(
        self,
        attempt_id: str,
        *,
        student_answer_text: str | None,
        student_solution_text: str | None,
        attachments: Sequence[tuple[str, str]] = (),
    ) -> ExerciseRecord:
        """在评价前补充/修正本次作答；不会改写已完成的评价。"""

        with self._write() as connection:
            exercise_id = self._update_pending_attempt(
                connection,
                attempt_id,
                student_answer_text=student_answer_text,
                student_solution_text=student_solution_text,
                attachments=attachments,
            )
            return self._get_exercise(connection, exercise_id)

    def get_exercise_attempt(self, attempt_id: str) -> ExerciseAttemptRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM exercise_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                return None
            attachments = self._load_attempt_attachments(
                connection, [attempt_id]
            ).get(attempt_id, [])
            return ExerciseAttemptRecord(
                id=row["id"],
                exercise_id=row["exercise_id"],
                attempt_no=row["attempt_no"],
                kind=row["kind"],
                student_answer_text=row["student_answer_text"],
                student_solution_text=row["student_solution_text"],
                answered_after_revealed=bool(row["answered_after_revealed"]),
                evaluation_json=row["evaluation_json"],
                request_id=row["request_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                attachments=attachments,
            )

    def get_attempt_by_request(
        self, exercise_id: str, request_id: str
    ) -> ExerciseAttemptRecord | None:
        """按创建它的轮次定位尝试，避免故障重试误评价后续订正。"""

        with self._read() as connection:
            row = connection.execute(
                """
                SELECT * FROM exercise_attempts
                WHERE exercise_id = ? AND request_id = ?
                ORDER BY attempt_no DESC LIMIT 1
                """,
                (exercise_id, request_id),
            ).fetchone()
            if row is None:
                return None
            attachments = self._load_attempt_attachments(
                connection, [row["id"]]
            ).get(row["id"], [])
            return ExerciseAttemptRecord(
                id=row["id"],
                exercise_id=row["exercise_id"],
                attempt_no=row["attempt_no"],
                kind=row["kind"],
                student_answer_text=row["student_answer_text"],
                student_solution_text=row["student_solution_text"],
                answered_after_revealed=bool(row["answered_after_revealed"]),
                evaluation_json=row["evaluation_json"],
                request_id=row["request_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                attachments=attachments,
            )

    def save_attempt_evaluation(
        self, attempt_id: str, *, evaluation_json: str
    ) -> ExerciseRecord:
        """把评价写回指定尝试，条件更新避免并发或迟到响应覆盖。"""

        with self._write() as connection:
            return self.save_attempt_evaluation_record(
                connection, attempt_id, evaluation_json
            )

    def list_recent_exercise_statements(
        self, conversation_id: str, *, limit: int = 5
    ) -> list[str]:
        """取本会话最近生成的题干，供去重提示；不含当前这道。"""

        safe_limit = max(1, min(int(limit), 10))
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT statement_text FROM exercises
                WHERE conversation_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (conversation_id, safe_limit),
            ).fetchall()
        return [row["statement_text"] for row in rows]

    # ------------------------------------------------------------------
    # 兼容旧单份作答接口（测试与旧调用方）
    # ------------------------------------------------------------------

    def save_exercise_answer(
        self,
        exercise_id: str,
        *,
        student_answer_text: str | None,
        student_solution_text: str | None,
        attachments: Sequence[tuple[str, str]] = (),
        answered_after_revealed: bool = False,
    ) -> ExerciseRecord:
        """兼容入口：未评价时更新当前尝试；已要求补充时新增尝试。"""

        with self._write() as connection:
            ex = self._find_exercise(connection, exercise_id)
            if ex is None:
                raise ProblemNotFoundError("未找到指定的练习题。")
            current = ex.current_attempt
            revealed = ex.status == "revealed" or ex.revealed_solution_at is not None
            if current is None:
                self._insert_attempt(
                    connection,
                    exercise_id,
                    kind="initial",
                    student_answer_text=student_answer_text,
                    student_solution_text=student_solution_text,
                    attachments=attachments,
                    answered_after_revealed=answered_after_revealed or revealed,
                )
            elif current.evaluation_json is None:
                self._update_pending_attempt(
                    connection,
                    current.id,
                    student_answer_text=student_answer_text,
                    student_solution_text=student_solution_text,
                    attachments=attachments,
                )
            elif _needs_supplement(current.evaluation_json):
                if not (student_answer_text or "").strip():
                    student_answer_text = current.student_answer_text
                if not (student_solution_text or "").strip():
                    student_solution_text = current.student_solution_text
                self._insert_attempt(
                    connection,
                    exercise_id,
                    kind="supplement",
                    student_answer_text=student_answer_text,
                    student_solution_text=student_solution_text,
                    attachments=attachments,
                    answered_after_revealed=answered_after_revealed or revealed,
                    inherit_attachments=True,
                )
            else:
                raise ConversationConflictError(
                    "这道练习题已经完成评价；如需继续练习，请生成新的类似题。"
                )
            return self._get_exercise(connection, exercise_id)

    def save_exercise_evaluation(
        self,
        exercise_id: str,
        *,
        evaluation_json: str,
    ) -> ExerciseRecord:
        """兼容入口：评价写入最新一次尚未评价的尝试。"""

        with self._write() as connection:
            ex = self._find_exercise(connection, exercise_id)
            if ex is None:
                raise ProblemNotFoundError("未找到指定的练习题。")
            current = ex.current_attempt
            if current is None:
                attempt_id = self._insert_attempt(
                    connection,
                    exercise_id,
                    kind="initial",
                    student_answer_text=None,
                    student_solution_text=None,
                    attachments=(),
                    answered_after_revealed=False,
                )
            else:
                attempt_id = current.id
            return self.save_attempt_evaluation_record(
                connection, attempt_id, evaluation_json
            )

    def save_attempt_evaluation_record(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        evaluation_json: str,
    ) -> ExerciseRecord:
        row = connection.execute(
            "SELECT * FROM exercise_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise ProblemNotFoundError("未找到这次作答记录。")
        now = _now()
        cursor = connection.execute(
            """
            UPDATE exercise_attempts
            SET evaluation_json = ?, updated_at = ?
            WHERE id = ? AND evaluation_json IS NULL
            """,
            (evaluation_json, now, attempt_id),
        )
        if cursor.rowcount == 0:
            raise ConversationConflictError(
                "这次作答已经完成评价，本次评价结果没有覆盖原结果。"
            )
        # 顶层练习列镜像最新一次尝试；补充未完成时不视为已展示参考解答。
        if _needs_supplement(evaluation_json):
            connection.execute(
                """
                UPDATE exercises
                SET status = 'pending_answer',
                    evaluation_json = ?,
                    student_answer_text = ?,
                    student_solution_text = ?,
                    answered_after_revealed = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    evaluation_json,
                    row["student_answer_text"],
                    row["student_solution_text"],
                    1 if row["answered_after_revealed"] else 0,
                    now,
                    row["exercise_id"],
                ),
            )
        else:
            connection.execute(
                """
                UPDATE exercises
                SET status = 'submitted',
                    evaluation_json = ?,
                    student_answer_text = ?,
                    student_solution_text = ?,
                    answered_after_revealed = ?,
                    revealed_solution_at = COALESCE(revealed_solution_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    evaluation_json,
                    row["student_answer_text"],
                    row["student_solution_text"],
                    1 if row["answered_after_revealed"] else 0,
                    now,
                    now,
                    row["exercise_id"],
                ),
            )
        return self._get_exercise(connection, row["exercise_id"])

    def record_exercise_answer_and_evaluation(
        self,
        exercise_id: str,
        *,
        student_answer_text: str | None,
        student_solution_text: str | None,
        evaluation_json: str,
        answered_after_revealed: bool = False,
    ) -> ExerciseRecord:
        """兼容旧的单事务接口：先保存作答，再写入评价。"""

        self.save_exercise_answer(
            exercise_id,
            student_answer_text=student_answer_text,
            student_solution_text=student_solution_text,
            answered_after_revealed=answered_after_revealed,
        )
        return self.save_exercise_evaluation(
            exercise_id, evaluation_json=evaluation_json
        )

    def link_exercise_attachments(
        self,
        exercise_id: str,
        attachments: Sequence[tuple[str, str]],
    ) -> None:
        with self._write() as connection:
            for aid, role in attachments:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO exercise_attachments (exercise_id, attachment_id, role)
                    VALUES (?, ?, ?)
                    """,
                    (exercise_id, aid, role),
                )

    def list_exercises_by_problem(self, problem_id: str) -> list[ExerciseRecord]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM exercises WHERE problem_id = ? ORDER BY created_at ASC",
                (problem_id,),
            ).fetchall()
            return [self._row_to_exercise(connection, row) for row in rows]

    def list_exercises_by_root_problem(self, root_problem_id: str) -> list[ExerciseRecord]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM exercises WHERE root_problem_id = ? ORDER BY created_at ASC",
                (root_problem_id,),
            ).fetchall()
            return [self._row_to_exercise(connection, row) for row in rows]

    def _find_exercise(
        self, connection: sqlite3.Connection, exercise_id: str
    ) -> ExerciseRecord | None:
        row = connection.execute(
            "SELECT * FROM exercises WHERE id = ?",
            (exercise_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_exercise(connection, row)

    def _get_exercise(
        self, connection: sqlite3.Connection, exercise_id: str
    ) -> ExerciseRecord:
        ex = self._find_exercise(connection, exercise_id)
        if ex is None:
            raise ProblemNotFoundError("未找到指定的练习题。")
        return ex

    def _row_to_exercise(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> ExerciseRecord:
        try:
            attempts = self._load_exercise_attempts(connection, row["id"])
            current = attempts[-1] if attempts else None
            attachments = (
                list(current.attachments)
                if current is not None
                else self._load_exercise_attachments(connection, row["id"])
            )
            revealed_at = (
                datetime.fromisoformat(row["revealed_solution_at"])
                if row["revealed_solution_at"]
                else None
            )
            return ExerciseRecord(
                id=row["id"],
                conversation_id=row["conversation_id"],
                problem_id=row["problem_id"],
                root_problem_id=row["root_problem_id"],
                statement_text=row["statement_text"],
                reference_solution_text=row["reference_solution_text"],
                knowledge_point=row["knowledge_point"],
                status=row["status"],
                # 顶层字段镜像最新尝试，历史尝试保留在 attempts 中。
                student_answer_text=(
                    current.student_answer_text
                    if current is not None
                    else row["student_answer_text"]
                ),
                student_solution_text=(
                    current.student_solution_text
                    if current is not None
                    else row["student_solution_text"]
                ),
                revealed_solution_at=revealed_at,
                answered_after_revealed=(
                    current.answered_after_revealed
                    if current is not None
                    else bool(row["answered_after_revealed"])
                ),
                evaluation_json=(
                    current.evaluation_json
                    if current is not None
                    else row["evaluation_json"]
                ),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                attachments=attachments,
                attempts=attempts,
            )
        except (TypeError, ValueError) as exc:
            raise ConversationStorageError(_CORRUPT_MESSAGE) from exc

    def _load_exercise_attempts(
        self, connection: sqlite3.Connection, exercise_id: str
    ) -> list[ExerciseAttemptRecord]:
        rows = connection.execute(
            """
            SELECT * FROM exercise_attempts
            WHERE exercise_id = ?
            ORDER BY attempt_no ASC
            """,
            (exercise_id,),
        ).fetchall()
        if not rows:
            return []
        attachments = self._load_attempt_attachments(
            connection, [row["id"] for row in rows]
        )
        result: list[ExerciseAttemptRecord] = []
        for row in rows:
            result.append(
                ExerciseAttemptRecord(
                    id=row["id"],
                    exercise_id=row["exercise_id"],
                    attempt_no=row["attempt_no"],
                    kind=row["kind"],
                    student_answer_text=row["student_answer_text"],
                    student_solution_text=row["student_solution_text"],
                    answered_after_revealed=bool(row["answered_after_revealed"]),
                    evaluation_json=row["evaluation_json"],
                    request_id=row["request_id"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                    attachments=attachments.get(row["id"], []),
                )
            )
        return result

    def _load_attempt_attachments(
        self, connection: sqlite3.Connection, attempt_ids: Sequence[str]
    ) -> dict[str, list[AttachmentRecord]]:
        if not attempt_ids:
            return {}
        placeholders = ",".join("?" for _ in attempt_ids)
        rows = connection.execute(
            f"""
            SELECT ea.attempt_id AS attempt_id, a.*, ea.role AS role
            FROM exercise_attempt_attachments ea
            JOIN attachments a ON a.id = ea.attachment_id
            WHERE ea.attempt_id IN ({placeholders})
            ORDER BY ea.role ASC, a.id ASC
            """,
            tuple(attempt_ids),
        ).fetchall()
        grouped: dict[str, list[AttachmentRecord]] = {}
        for row in rows:
            grouped.setdefault(row["attempt_id"], []).append(
                self._row_to_attachment(row, row["role"])
            )
        return grouped

    def view_from_exercise(
        self,
        exercise: ExerciseRecord,
        *,
        allow_solution: bool = False,
        attempt: ExerciseAttemptRecord | None = None,
    ) -> ExerciseView:
        """练习卡片构造口径。

        默认展示最新一次尝试（当前练习/生成题轮次）；传入 ``attempt`` 时
        展示该次尝试并截断历史，供历史轮次绑定自己的作答，避免后续订正
        覆盖旧轮次卡片。参考解答只在完整评价后（或显式允许时）返回。
        """

        def attachments_view(items: Sequence[AttachmentRecord]) -> list[AttachmentView]:
            return [
                AttachmentView(
                    id=att.id,
                    role=(
                        att.role
                        if att.role in ("student_answer", "student_solution")
                        else "student_solution"
                    ),
                    mime=att.mime,
                    width=att.width,
                    height=att.height,
                    url=(
                        f"/api/conversations/{exercise.conversation_id}"
                        f"/attachments/{att.id}"
                    ),
                )
                for att in items
            ]

        def evaluation(raw: str | None) -> ExerciseEvaluation | None:
            if not raw:
                return None
            with suppress(Exception):
                return ExerciseEvaluation.model_validate_json(raw)
            return None

        current = attempt or exercise.current_attempt
        if attempt is None:
            status = exercise.status
            answer = exercise.student_answer_text
            solution = exercise.student_solution_text
            flag = exercise.answered_after_revealed
            revealed_at = exercise.revealed_solution_at
            parsed_eval = evaluation(exercise.evaluation_json)
            attachments_source = (
                exercise.attachments if current is None else current.attachments
            )
            attempts_source = exercise.attempts
            created_at = exercise.created_at
            updated_at = exercise.updated_at
        else:
            parsed_eval = evaluation(attempt.evaluation_json)
            complete = parsed_eval is not None and not parsed_eval.needs_supplement
            status = "pending_answer" if not complete else "submitted"
            answer = attempt.student_answer_text
            solution = attempt.student_solution_text
            flag = attempt.answered_after_revealed
            revealed_at = (
                exercise.revealed_solution_at if attempt.answered_after_revealed else None
            )
            attachments_source = attempt.attachments
            attempts_source = [
                item
                for item in exercise.attempts
                if item.attempt_no <= attempt.attempt_no
            ]
            created_at = attempt.created_at
            updated_at = attempt.updated_at

        ref_sol = None
        if allow_solution or (
            parsed_eval is not None and not parsed_eval.needs_supplement
        ):
            ref_sol = exercise.reference_solution_text
        elif attempt is None and (
            exercise.status in ("submitted", "revealed")
            or exercise.revealed_solution_at is not None
        ):
            # 兼容未回填尝试的旧数据/查看解答动作：沿用原可见性。
            ref_sol = exercise.reference_solution_text

        attempts: list[ExerciseAttemptView] = []
        for item in attempts_source:
            attempts.append(
                ExerciseAttemptView(
                    id=item.id,
                    attempt_no=item.attempt_no,
                    kind=item.kind,  # type: ignore[arg-type]
                    student_answer_text=item.student_answer_text,
                    student_solution_text=item.student_solution_text,
                    answered_after_revealed=item.answered_after_revealed,
                    evaluation=evaluation(item.evaluation_json),
                    request_id=item.request_id,
                    attachments=attachments_view(item.attachments),
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
            )

        return ExerciseView(
            id=exercise.id,
            conversation_id=exercise.conversation_id,
            problem_id=exercise.problem_id,
            root_problem_id=exercise.root_problem_id,
            statement_text=exercise.statement_text,
            knowledge_point=exercise.knowledge_point,
            status=status,  # type: ignore[arg-type]
            student_answer_text=answer,
            student_solution_text=solution,
            revealed_solution_at=revealed_at,
            answered_after_revealed=flag,
            evaluation=parsed_eval,
            reference_solution_text=ref_sol,
            attachments=attachments_view(attachments_source),
            attempts=attempts,
            created_at=created_at,
            updated_at=updated_at,
        )

    def _load_exercise_attachments(
        self, connection: sqlite3.Connection, exercise_id: str
    ) -> list[AttachmentRecord]:
        rows = connection.execute(
            """
            SELECT a.*, ea.role AS role
            FROM exercise_attachments ea
            JOIN attachments a ON a.id = ea.attachment_id
            WHERE ea.exercise_id = ?
            ORDER BY ea.role ASC, a.id ASC
            """,
            (exercise_id,),
        ).fetchall()
        return [self._row_to_attachment(row, row["role"]) for row in rows]

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------

    def get_context(self, conversation_id: str) -> ContextRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_contexts WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_context(row)

    def upsert_context(
        self,
        conversation_id: str,
        *,
        confirmed_course: str | None = None,
        active_problem_id: str | None = None,
        active_exercise_id: str | None = None,
        last_suggestions_json: str | None = None,
        last_action_summary: str | None = None,
    ) -> ContextRecord:
        now = _now()
        with self._write() as connection:
            self._check_conversation_exists(connection, conversation_id)
            existing = connection.execute(
                "SELECT * FROM conversation_contexts WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()

            if existing is None:
                connection.execute(
                    """
                    INSERT INTO conversation_contexts (
                        conversation_id, confirmed_course, active_problem_id,
                        active_exercise_id, last_suggestions_json, last_action_summary,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        confirmed_course,
                        active_problem_id,
                        active_exercise_id,
                        last_suggestions_json,
                        last_action_summary,
                        now,
                    ),
                )
            else:
                new_course = confirmed_course if confirmed_course is not None else existing["confirmed_course"]
                new_prob = active_problem_id if active_problem_id is not None else existing["active_problem_id"]
                new_ex = active_exercise_id if active_exercise_id is not None else existing["active_exercise_id"]
                new_sug = last_suggestions_json if last_suggestions_json is not None else existing["last_suggestions_json"]
                new_act = last_action_summary if last_action_summary is not None else existing["last_action_summary"]

                connection.execute(
                    """
                    UPDATE conversation_contexts
                    SET confirmed_course = ?, active_problem_id = ?, active_exercise_id = ?,
                        last_suggestions_json = ?, last_action_summary = ?, updated_at = ?
                    WHERE conversation_id = ?
                    """,
                    (
                        new_course,
                        new_prob,
                        new_ex,
                        new_sug,
                        new_act,
                        now,
                        conversation_id,
                    ),
                )
            return self._row_to_context(
                connection.execute(
                    "SELECT * FROM conversation_contexts WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
            )

    def update_problem_understood(self, problem_id: str, understood: bool) -> None:
        """更新原题的学生自述理解状态。"""
        now = _now()
        with self._write() as connection:
            connection.execute(
                "UPDATE problems SET self_reported_understood = ?, updated_at = ? WHERE id = ?",
                (1 if understood else 0, now, problem_id),
            )

    @staticmethod
    def _row_to_context(row: sqlite3.Row) -> ContextRecord:
        try:
            return ContextRecord(
                conversation_id=row["conversation_id"],
                confirmed_course=row["confirmed_course"],
                active_problem_id=row["active_problem_id"],
                active_exercise_id=row["active_exercise_id"],
                last_suggestions_json=row["last_suggestions_json"],
                last_action_summary=row["last_action_summary"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        except (TypeError, ValueError) as exc:
            raise ConversationStorageError(_CORRUPT_MESSAGE) from exc
