"""本地学习数据库的结构与迁移（当前 v5）。

只有两种数据库允许写入：真正的空库，或结构与目标版本完全兼容的库。旧版
数据库在单个事务中逐级迁移到 v5；任何未知结构、损坏或迁移失败都只返回
ConversationStorageError，不删除、不清空、不覆盖原文件。

v4 在 v3 基础上增加练习的“作答尝试”历史：首次作答、补充和订正分别保存为
不可变尝试行，评价只写回对应尝试，不覆盖旧答案与旧评价。

v5（F007 P4）只追加三张表，不改旧列：
- question_selections：会话当前选中的真题、公开快照与答案开放事实；
- exercise_question_sources：练习到真题的稳定关联；
- record_question_sources：正式错题到真题的稳定关联。
答案正文不进入本库；读取时按 build 从只读题库取。

迁移步骤：
1. 校验来源版本的表、列、主键、唯一约束和运行中唯一索引；
2. BEGIN IMMEDIATE，建新表/索引并回填旧练习的首次作答；
3. 写 PRAGMA user_version，提交；
4. 提交成功后才启用 WAL；失败回滚，不切换 WAL。
"""

import logging
import sqlite3
from contextlib import suppress
from pathlib import Path

from app.conversations.errors import ConversationStorageError
from app.conversations.inputs import query_input

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 5

_STORAGE_MESSAGE = "本地学习数据暂时不可用，请稍后再试。"
_UPGRADE_MESSAGE = "本地学习数据的结构版本不兼容，请保留原文件并联系维护者。"
_UNKNOWN_MESSAGE = (
    "检测到已有数据库的结构与当前版本不一致；为避免修改原文件，"
    "已停止初始化，请保留原文件并联系维护者。"
)
_MIGRATION_MESSAGE = (
    "本地学习数据的结构升级失败，已回滚并保留原数据；请停止使用并联系维护者。"
)

# ---------------------------------------------------------------------------
# v1（F004）结构
# ---------------------------------------------------------------------------

_V1_COLUMNS: dict[str, tuple[str, ...]] = {
    "conversations": ("id", "title", "created_at", "updated_at"),
    "conversation_turns": (
        "id",
        "conversation_id",
        "request_id",
        "seq",
        "query",
        "status",
        "result_json",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
    ),
}

_V1_TABLES = frozenset(_V1_COLUMNS)
_V1_INDEXES = frozenset({"turns_one_running"})

# ---------------------------------------------------------------------------
# v2 结构
# ---------------------------------------------------------------------------

_V2_COLUMNS: dict[str, tuple[str, ...]] = {
    "conversations": ("id", "title", "created_at", "updated_at"),
    "conversation_turns": (
        "id",
        "conversation_id",
        "request_id",
        "seq",
        "query",
        "status",
        "result_json",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
        "kind",
        "input_json",
        "output_json",
        "problem_id",
        "record_id",
    ),
    "problems": (
        "id",
        "conversation_id",
        "root_problem_id",
        "supersedes_problem_id",
        "version",
        "statement_text",
        "status",
        "clarity",
        "unclear_points_json",
        "uncertainties_json",
        "source_type",
        "source_text",
        "paper_id",
        "source_title",
        "student_answer_text",
        "solution_text",
        "explanation_json",
        "self_reported_understood",
        "active",
        "created_at",
        "updated_at",
    ),
    "attachments": (
        "id",
        "conversation_id",
        "stored_name",
        "mime",
        "width",
        "height",
        "size_bytes",
        "sha256",
        "pending",
        "created_at",
    ),
    "turn_attachments": ("turn_id", "attachment_id", "role"),
    "problem_attachments": ("problem_id", "attachment_id", "role"),
    "mistake_records": (
        "id",
        "conversation_id",
        "problem_id",
        "root_problem_id",
        "problem_version",
        "request_id",
        "title",
        "statement_text",
        "student_answer_text",
        "solution_text",
        "source_json",
        "explanation_json",
        "followups_json",
        "feedback_json",
        "self_reported_understood",
        "created_at",
        "updated_at",
    ),
    "mistake_record_attachments": ("record_id", "attachment_id", "role"),
    "mistake_record_revisions": (
        "id",
        "record_id",
        "request_id",
        "reason",
        "snapshot_json",
        "created_at",
    ),
}

_V2_TABLES = frozenset(_V2_COLUMNS)
_V2_INDEXES = frozenset(
    {
        "turns_one_running",
        "turns_problem",
        "problems_one_active",
        "problems_root",
        "attachments_conversation",
        "attachments_pending",
        "turn_attachments_attachment",
        "problem_attachments_attachment",
        "mistake_records_created",
        "mistake_record_attachments_attachment",
    }
)

_V1_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "conversations": ("id",),
    "conversation_turns": ("id",),
}

_V2_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "conversations": ("id",),
    "conversation_turns": ("id",),
    "problems": ("id",),
    "attachments": ("id",),
    "turn_attachments": ("turn_id", "attachment_id", "role"),
    "problem_attachments": ("problem_id", "attachment_id", "role"),
    "mistake_records": ("id",),
    "mistake_record_attachments": ("record_id", "attachment_id", "role"),
    "mistake_record_revisions": ("id",),
}

_V1_UNIQUE_SETS = {
    ("conversation_id", "request_id"),
    ("conversation_id", "seq"),
}

_V2_UNIQUE_SETS = {
    ("conversation_id", "request_id"),
    ("conversation_id", "seq"),
    ("conversation_id", "root_problem_id", "version"),
    ("conversation_id", "root_problem_id"),
    ("record_id", "request_id"),
    ("stored_name",),
}

_V1_FOREIGN_KEYS = {
    ("conversation_id", "conversations", "id"),
}

_V2_FOREIGN_KEYS = {
    ("conversation_id", "conversations", "id"),
    ("turn_id", "conversation_turns", "id"),
    ("attachment_id", "attachments", "id"),
    ("problem_id", "problems", "id"),
    ("record_id", "mistake_records", "id"),
}

_V2_REQUIRED_CHECKS: dict[str, tuple[str, ...]] = {
    "conversation_turns": (
        "statusin('running','completed','failed','interrupted')",
    ),
    "problems": (
        "statusin('awaiting_supplement','explained')",
        "clarityin('clear','unclear')",
        "source_typein('none','student_text','registered_paper')",
        "self_reported_understoodin(0,1)",
        "activein(0,1)",
    ),
    "attachments": (
        "mimein('image/jpeg','image/png','image/gif','image/webp')",
        "pendingin(0,1)",
    ),
    "turn_attachments": ("rolein('statement','solution','supplement')",),
    "problem_attachments": ("rolein('statement','solution','supplement')",),
    "mistake_records": ("self_reported_understoodin(0,1)",),
    "mistake_record_attachments": ("rolein('statement','solution','supplement')",),
    "mistake_record_revisions": (
        "reasonin('correction','retutor','adopt_problem')",
    ),
}

# ---------------------------------------------------------------------------
# v3 结构（F006：动态对话上下文、练习与评价）
# ---------------------------------------------------------------------------

_V3_COLUMNS: dict[str, tuple[str, ...]] = {
    **_V2_COLUMNS,
    "conversation_turns": (
        *_V2_COLUMNS["conversation_turns"],
        "exercise_id",
    ),
    "conversation_contexts": (
        "conversation_id",
        "confirmed_course",
        "active_problem_id",
        "active_exercise_id",
        "last_suggestions_json",
        "last_action_summary",
        "updated_at",
    ),
    "exercises": (
        "id",
        "conversation_id",
        "problem_id",
        "root_problem_id",
        "statement_text",
        "reference_solution_text",
        "knowledge_point",
        "status",
        "student_answer_text",
        "student_solution_text",
        "revealed_solution_at",
        "answered_after_revealed",
        "evaluation_json",
        "created_at",
        "updated_at",
    ),
    "exercise_attachments": ("exercise_id", "attachment_id", "role"),
}

_V3_TABLES = frozenset(_V3_COLUMNS)
_V3_INDEXES = frozenset(
    _V2_INDEXES
    | {
        "exercises_conversation",
        "exercises_problem",
        "exercise_attachments_attachment",
    }
)

_V3_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    **_V2_PRIMARY_KEYS,
    "conversation_contexts": ("conversation_id",),
    "exercises": ("id",),
    "exercise_attachments": ("exercise_id", "attachment_id", "role"),
}

_V3_UNIQUE_SETS = {
    *_V2_UNIQUE_SETS,
}

_V3_FOREIGN_KEYS = {
    *_V2_FOREIGN_KEYS,
    ("exercise_id", "exercises", "id"),
    ("active_problem_id", "problems", "id"),
    ("active_exercise_id", "exercises", "id"),
}

_V3_REQUIRED_CHECKS: dict[str, tuple[str, ...]] = {
    **_V2_REQUIRED_CHECKS,
    "exercises": (
        "statusin('pending_answer','submitted','revealed')",
        "answered_after_revealedin(0,1)",
    ),
    "exercise_attachments": (
        "rolein('student_solution','student_answer')",
    ),
}

# ---------------------------------------------------------------------------
# v4 结构（F006 B 批：不可变作答尝试、补充与订正历史）
# ---------------------------------------------------------------------------

_V4_COLUMNS: dict[str, tuple[str, ...]] = {
    **_V3_COLUMNS,
    "exercise_attempts": (
        "id",
        "exercise_id",
        "attempt_no",
        "kind",
        "student_answer_text",
        "student_solution_text",
        "answered_after_revealed",
        "evaluation_json",
        "request_id",
        "created_at",
        "updated_at",
    ),
    "exercise_attempt_attachments": ("attempt_id", "attachment_id", "role"),
}

_V4_TABLES = frozenset(_V4_COLUMNS)
_V4_INDEXES = frozenset(
    _V3_INDEXES
    | {
        "exercise_attempts_exercise",
        "exercise_attempt_attachments_attachment",
    }
)

_V4_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    **_V3_PRIMARY_KEYS,
    "exercise_attempts": ("id",),
    "exercise_attempt_attachments": ("attempt_id", "attachment_id", "role"),
}

_V4_UNIQUE_SETS = {
    *_V3_UNIQUE_SETS,
    ("exercise_id", "attempt_no"),
}

_V4_FOREIGN_KEYS = {
    *_V3_FOREIGN_KEYS,
    ("exercise_id", "exercises", "id"),
    ("attempt_id", "exercise_attempts", "id"),
    ("attachment_id", "attachments", "id"),
}

_V4_REQUIRED_CHECKS: dict[str, tuple[str, ...]] = {
    **_V3_REQUIRED_CHECKS,
    "exercise_attempts": (
        "attempt_no>=1",
        "kindin('initial','supplement','correction')",
        "answered_after_revealedin(0,1)",
    ),
    "exercise_attempt_attachments": (
        "rolein('student_solution','student_answer')",
    ),
}

# ---------------------------------------------------------------------------
# v5 结构（F007 P4：真题选中、练习来源、错题来源）
# ---------------------------------------------------------------------------

_V5_COLUMNS: dict[str, tuple[str, ...]] = {
    **_V4_COLUMNS,
    "question_selections": (
        "id",
        "conversation_id",
        "request_id",
        "source_request_id",
        "question_id",
        "paper_id",
        "build_id",
        "source_version",
        "content_sha256",
        "question_type",
        "number_label",
        "snapshot_json",
        "problem_id",
        "answer_revealed_at",
        "answer_reveal_source",
        "active",
        "created_at",
        "updated_at",
    ),
    "exercise_question_sources": (
        "exercise_id",
        "question_id",
        "paper_id",
        "build_id",
        "source_version",
        "content_sha256",
        "snapshot_json",
        "created_at",
    ),
    "record_question_sources": (
        "record_id",
        "question_id",
        "paper_id",
        "build_id",
        "source_version",
        "content_sha256",
        "created_at",
    ),
}

_V5_TABLES = frozenset(_V5_COLUMNS)
_V5_INDEXES = frozenset(
    _V4_INDEXES
    | {
        "question_selections_one_active",
        "question_selections_conversation",
    }
)

_V5_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    **_V4_PRIMARY_KEYS,
    "question_selections": ("id",),
    "exercise_question_sources": ("exercise_id",),
    "record_question_sources": ("record_id",),
}

_V5_UNIQUE_SETS = {
    *_V4_UNIQUE_SETS,
    ("conversation_id", "request_id", "question_id"),
}

_V5_FOREIGN_KEYS = set(_V4_FOREIGN_KEYS)

_V5_REQUIRED_CHECKS: dict[str, tuple[str, ...]] = {
    **_V4_REQUIRED_CHECKS,
    "question_selections": (
        "question_typein('choice','fill','code')",
        "answer_reveal_sourcein('view','explain')",
        "activein(0,1)",
    ),
}

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CONVERSATIONS = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CONVERSATION_TURNS = """
CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    query TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('running', 'completed', 'failed', 'interrupted')),
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    kind TEXT NOT NULL DEFAULT 'query',
    input_json TEXT,
    output_json TEXT,
    problem_id TEXT,
    record_id TEXT,
    UNIQUE (conversation_id, request_id),
    UNIQUE (conversation_id, seq)
)
"""

_TURNS_ONE_RUNNING = """
CREATE UNIQUE INDEX IF NOT EXISTS turns_one_running
    ON conversation_turns(conversation_id)
    WHERE status = 'running'
"""

_TURNS_PROBLEM = """
CREATE INDEX IF NOT EXISTS turns_problem
    ON conversation_turns(problem_id, seq)
"""

_PROBLEMS = """
CREATE TABLE IF NOT EXISTS problems (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    root_problem_id TEXT NOT NULL,
    supersedes_problem_id TEXT,
    version INTEGER NOT NULL CHECK (version >= 1),
    statement_text TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('awaiting_supplement', 'explained')),
    clarity TEXT NOT NULL CHECK (clarity IN ('clear', 'unclear')),
    unclear_points_json TEXT,
    uncertainties_json TEXT,
    source_type TEXT NOT NULL
        CHECK (source_type IN ('none', 'student_text', 'registered_paper')),
    source_text TEXT,
    paper_id TEXT,
    source_title TEXT,
    student_answer_text TEXT,
    solution_text TEXT,
    explanation_json TEXT,
    self_reported_understood INTEGER NOT NULL DEFAULT 0
        CHECK (self_reported_understood IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (conversation_id, root_problem_id, version)
)
"""

_PROBLEMS_ONE_ACTIVE = """
CREATE UNIQUE INDEX IF NOT EXISTS problems_one_active
    ON problems(conversation_id)
    WHERE active = 1
"""

_PROBLEMS_ROOT = """
CREATE INDEX IF NOT EXISTS problems_root
    ON problems(root_problem_id, version)
"""

_ATTACHMENTS = """
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    stored_name TEXT NOT NULL UNIQUE,
    mime TEXT NOT NULL
        CHECK (mime IN ('image/jpeg', 'image/png', 'image/gif', 'image/webp')),
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    sha256 TEXT NOT NULL,
    pending INTEGER NOT NULL DEFAULT 1 CHECK (pending IN (0, 1)),
    created_at TEXT NOT NULL
)
"""

_ATTACHMENTS_CONVERSATION = """
CREATE INDEX IF NOT EXISTS attachments_conversation
    ON attachments(conversation_id, created_at)
"""

_ATTACHMENTS_PENDING = """
CREATE INDEX IF NOT EXISTS attachments_pending
    ON attachments(pending, created_at)
"""

_TURN_ATTACHMENTS = """
CREATE TABLE IF NOT EXISTS turn_attachments (
    turn_id INTEGER NOT NULL
        REFERENCES conversation_turns(id) ON DELETE CASCADE,
    attachment_id TEXT NOT NULL
        REFERENCES attachments(id) ON DELETE CASCADE,
    role TEXT NOT NULL
        CHECK (role IN ('statement', 'solution', 'supplement')),
    PRIMARY KEY (turn_id, attachment_id, role)
)
"""

_TURN_ATTACHMENTS_ATTACHMENT = """
CREATE INDEX IF NOT EXISTS turn_attachments_attachment
    ON turn_attachments(attachment_id)
"""

_PROBLEM_ATTACHMENTS = """
CREATE TABLE IF NOT EXISTS problem_attachments (
    problem_id TEXT NOT NULL
        REFERENCES problems(id) ON DELETE CASCADE,
    attachment_id TEXT NOT NULL
        REFERENCES attachments(id) ON DELETE CASCADE,
    role TEXT NOT NULL
        CHECK (role IN ('statement', 'solution', 'supplement')),
    PRIMARY KEY (problem_id, attachment_id, role)
)
"""

_PROBLEM_ATTACHMENTS_ATTACHMENT = """
CREATE INDEX IF NOT EXISTS problem_attachments_attachment
    ON problem_attachments(attachment_id)
"""

_MISTAKE_RECORDS = """
CREATE TABLE IF NOT EXISTS mistake_records (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    problem_id TEXT NOT NULL
        REFERENCES problems(id) ON DELETE CASCADE,
    root_problem_id TEXT NOT NULL,
    problem_version INTEGER NOT NULL CHECK (problem_version >= 1),
    request_id TEXT NOT NULL,
    title TEXT NOT NULL,
    statement_text TEXT NOT NULL,
    student_answer_text TEXT,
    solution_text TEXT,
    source_json TEXT NOT NULL,
    explanation_json TEXT,
    followups_json TEXT,
    feedback_json TEXT,
    self_reported_understood INTEGER NOT NULL DEFAULT 0
        CHECK (self_reported_understood IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (conversation_id, root_problem_id)
)
"""

_MISTAKE_RECORDS_CREATED = """
CREATE INDEX IF NOT EXISTS mistake_records_created
    ON mistake_records(created_at DESC, id ASC)
"""

_MISTAKE_RECORD_ATTACHMENTS = """
CREATE TABLE IF NOT EXISTS mistake_record_attachments (
    record_id TEXT NOT NULL
        REFERENCES mistake_records(id) ON DELETE CASCADE,
    attachment_id TEXT NOT NULL
        REFERENCES attachments(id) ON DELETE CASCADE,
    role TEXT NOT NULL
        CHECK (role IN ('statement', 'solution', 'supplement')),
    PRIMARY KEY (record_id, attachment_id, role)
)
"""

_MISTAKE_RECORD_ATTACHMENTS_ATTACHMENT = """
CREATE INDEX IF NOT EXISTS mistake_record_attachments_attachment
    ON mistake_record_attachments(attachment_id)
"""

_MISTAKE_RECORD_REVISIONS = """
CREATE TABLE IF NOT EXISTS mistake_record_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL
        REFERENCES mistake_records(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    reason TEXT NOT NULL
        CHECK (reason IN ('correction', 'retutor', 'adopt_problem')),
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (record_id, request_id)
)
"""

_CREATE_V2_STATEMENTS = (
    _CONVERSATIONS,
    _CONVERSATION_TURNS,
    _TURNS_ONE_RUNNING,
    _TURNS_PROBLEM,
    _PROBLEMS,
    _PROBLEMS_ONE_ACTIVE,
    _PROBLEMS_ROOT,
    _ATTACHMENTS,
    _ATTACHMENTS_CONVERSATION,
    _ATTACHMENTS_PENDING,
    _TURN_ATTACHMENTS,
    _TURN_ATTACHMENTS_ATTACHMENT,
    _PROBLEM_ATTACHMENTS,
    _PROBLEM_ATTACHMENTS_ATTACHMENT,
    _MISTAKE_RECORDS,
    _MISTAKE_RECORDS_CREATED,
    _MISTAKE_RECORD_ATTACHMENTS,
    _MISTAKE_RECORD_ATTACHMENTS_ATTACHMENT,
    _MISTAKE_RECORD_REVISIONS,
)

_ALTER_TURN_COLUMNS = (
    "ALTER TABLE conversation_turns ADD COLUMN kind TEXT NOT NULL DEFAULT 'query'",
    "ALTER TABLE conversation_turns ADD COLUMN input_json TEXT",
    "ALTER TABLE conversation_turns ADD COLUMN output_json TEXT",
    "ALTER TABLE conversation_turns ADD COLUMN problem_id TEXT",
    "ALTER TABLE conversation_turns ADD COLUMN record_id TEXT",
)

_MIGRATION_STATEMENTS = (
    *_ALTER_TURN_COLUMNS,
    _TURNS_PROBLEM,
    _PROBLEMS,
    _PROBLEMS_ONE_ACTIVE,
    _PROBLEMS_ROOT,
    _ATTACHMENTS,
    _ATTACHMENTS_CONVERSATION,
    _ATTACHMENTS_PENDING,
    _TURN_ATTACHMENTS,
    _TURN_ATTACHMENTS_ATTACHMENT,
    _PROBLEM_ATTACHMENTS,
    _PROBLEM_ATTACHMENTS_ATTACHMENT,
    _MISTAKE_RECORDS,
    _MISTAKE_RECORDS_CREATED,
    _MISTAKE_RECORD_ATTACHMENTS,
    _MISTAKE_RECORD_ATTACHMENTS_ATTACHMENT,
    _MISTAKE_RECORD_REVISIONS,
)


def _migration_statements() -> tuple[str, ...]:
    """单独成函数，测试可以替换成含失败语句的序列。"""

    return _MIGRATION_STATEMENTS


_CONVERSATION_TURNS_V3 = """
CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    query TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('running', 'completed', 'failed', 'interrupted')),
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    kind TEXT NOT NULL DEFAULT 'query',
    input_json TEXT,
    output_json TEXT,
    problem_id TEXT,
    record_id TEXT,
    exercise_id TEXT,
    UNIQUE (conversation_id, request_id),
    UNIQUE (conversation_id, seq)
)
"""

_CONVERSATION_CONTEXTS = """
CREATE TABLE IF NOT EXISTS conversation_contexts (
    conversation_id TEXT PRIMARY KEY
        REFERENCES conversations(id) ON DELETE CASCADE,
    confirmed_course TEXT,
    active_problem_id TEXT
        REFERENCES problems(id),
    active_exercise_id TEXT
        REFERENCES exercises(id),
    last_suggestions_json TEXT,
    last_action_summary TEXT,
    updated_at TEXT NOT NULL
)
"""

_EXERCISES = """
CREATE TABLE IF NOT EXISTS exercises (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    problem_id TEXT NOT NULL
        REFERENCES problems(id) ON DELETE CASCADE,
    root_problem_id TEXT NOT NULL,
    statement_text TEXT NOT NULL,
    reference_solution_text TEXT NOT NULL,
    knowledge_point TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('pending_answer', 'submitted', 'revealed')),
    student_answer_text TEXT,
    student_solution_text TEXT,
    revealed_solution_at TEXT,
    answered_after_revealed INTEGER NOT NULL DEFAULT 0
        CHECK (answered_after_revealed IN (0, 1)),
    evaluation_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_EXERCISES_CONVERSATION = """
CREATE INDEX IF NOT EXISTS exercises_conversation
    ON exercises(conversation_id, created_at)
"""

_EXERCISES_PROBLEM = """
CREATE INDEX IF NOT EXISTS exercises_problem
    ON exercises(problem_id)
"""

_EXERCISE_ATTACHMENTS = """
CREATE TABLE IF NOT EXISTS exercise_attachments (
    exercise_id TEXT NOT NULL
        REFERENCES exercises(id) ON DELETE CASCADE,
    attachment_id TEXT NOT NULL
        REFERENCES attachments(id) ON DELETE CASCADE,
    role TEXT NOT NULL
        CHECK (role IN ('student_solution', 'student_answer')),
    PRIMARY KEY (exercise_id, attachment_id, role)
)
"""

_EXERCISE_ATTACHMENTS_ATTACHMENT = """
CREATE INDEX IF NOT EXISTS exercise_attachments_attachment
    ON exercise_attachments(attachment_id)
"""

_EXERCISE_ATTEMPTS = """
CREATE TABLE IF NOT EXISTS exercise_attempts (
    id TEXT PRIMARY KEY,
    exercise_id TEXT NOT NULL
        REFERENCES exercises(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    kind TEXT NOT NULL
        CHECK (kind IN ('initial', 'supplement', 'correction')),
    student_answer_text TEXT,
    student_solution_text TEXT,
    answered_after_revealed INTEGER NOT NULL DEFAULT 0
        CHECK (answered_after_revealed IN (0, 1)),
    evaluation_json TEXT,
    request_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (exercise_id, attempt_no)
)
"""

_EXERCISE_ATTEMPTS_EXERCISE = """
CREATE INDEX IF NOT EXISTS exercise_attempts_exercise
    ON exercise_attempts(exercise_id, attempt_no)
"""

_EXERCISE_ATTEMPT_ATTACHMENTS = """
CREATE TABLE IF NOT EXISTS exercise_attempt_attachments (
    attempt_id TEXT NOT NULL
        REFERENCES exercise_attempts(id) ON DELETE CASCADE,
    attachment_id TEXT NOT NULL
        REFERENCES attachments(id) ON DELETE CASCADE,
    role TEXT NOT NULL
        CHECK (role IN ('student_solution', 'student_answer')),
    PRIMARY KEY (attempt_id, attachment_id, role)
)
"""

_EXERCISE_ATTEMPT_ATTACHMENTS_ATTACHMENT = """
CREATE INDEX IF NOT EXISTS exercise_attempt_attachments_attachment
    ON exercise_attempt_attachments(attachment_id)
"""

_CREATE_V3_STATEMENTS = (
    _CONVERSATIONS,
    _CONVERSATION_TURNS_V3,
    _TURNS_ONE_RUNNING,
    _TURNS_PROBLEM,
    _PROBLEMS,
    _PROBLEMS_ONE_ACTIVE,
    _PROBLEMS_ROOT,
    _ATTACHMENTS,
    _ATTACHMENTS_CONVERSATION,
    _ATTACHMENTS_PENDING,
    _TURN_ATTACHMENTS,
    _TURN_ATTACHMENTS_ATTACHMENT,
    _PROBLEM_ATTACHMENTS,
    _PROBLEM_ATTACHMENTS_ATTACHMENT,
    _MISTAKE_RECORDS,
    _MISTAKE_RECORDS_CREATED,
    _MISTAKE_RECORD_ATTACHMENTS,
    _MISTAKE_RECORD_ATTACHMENTS_ATTACHMENT,
    _MISTAKE_RECORD_REVISIONS,
    _CONVERSATION_CONTEXTS,
    _EXERCISES,
    _EXERCISES_CONVERSATION,
    _EXERCISES_PROBLEM,
    _EXERCISE_ATTACHMENTS,
    _EXERCISE_ATTACHMENTS_ATTACHMENT,
)

_V3_MIGRATION_STATEMENTS = (
    "ALTER TABLE conversation_turns ADD COLUMN exercise_id TEXT",
    _CONVERSATION_CONTEXTS,
    _EXERCISES,
    _EXERCISES_CONVERSATION,
    _EXERCISES_PROBLEM,
    _EXERCISE_ATTACHMENTS,
    _EXERCISE_ATTACHMENTS_ATTACHMENT,
)


def _v3_migration_statements() -> tuple[str, ...]:
    return _V3_MIGRATION_STATEMENTS


_CREATE_V4_STATEMENTS = (
    *_CREATE_V3_STATEMENTS,
    _EXERCISE_ATTEMPTS,
    _EXERCISE_ATTEMPTS_EXERCISE,
    _EXERCISE_ATTEMPT_ATTACHMENTS,
    _EXERCISE_ATTEMPT_ATTACHMENTS_ATTACHMENT,
)

_V4_MIGRATION_STATEMENTS = (
    _EXERCISE_ATTEMPTS,
    _EXERCISE_ATTEMPTS_EXERCISE,
    _EXERCISE_ATTEMPT_ATTACHMENTS,
    _EXERCISE_ATTEMPT_ATTACHMENTS_ATTACHMENT,
)

# 把 v3 的单份作答快照回填为第 1 次尝试；旧列保留不删，便于只读兼容。
_V4_BACKFILL_ATTEMPTS = """
INSERT OR IGNORE INTO exercise_attempts (
    id, exercise_id, attempt_no, kind,
    student_answer_text, student_solution_text, answered_after_revealed,
    evaluation_json, request_id, created_at, updated_at
)
SELECT
    e.id || '-attempt-1', e.id, 1, 'initial',
    e.student_answer_text, e.student_solution_text, e.answered_after_revealed,
    e.evaluation_json, NULL, e.created_at, e.updated_at
FROM exercises e
WHERE e.student_answer_text IS NOT NULL
   OR e.student_solution_text IS NOT NULL
   OR e.evaluation_json IS NOT NULL
   OR e.answered_after_revealed = 1
   OR EXISTS (
       SELECT 1 FROM exercise_attachments ea WHERE ea.exercise_id = e.id
   )
"""

_V4_BACKFILL_ATTEMPT_ATTACHMENTS = """
INSERT OR IGNORE INTO exercise_attempt_attachments (
    attempt_id, attachment_id, role
)
SELECT e.id || '-attempt-1', ea.attachment_id, ea.role
FROM exercise_attachments ea
JOIN exercises e ON e.id = ea.exercise_id
JOIN exercise_attempts at ON at.id = e.id || '-attempt-1'
"""


def _v4_migration_statements() -> tuple[str, ...]:
    return _V4_MIGRATION_STATEMENTS


# ---------------------------------------------------------------------------
# v5 DDL（F007 P4）
# ---------------------------------------------------------------------------

_QUESTION_SELECTIONS = """
CREATE TABLE IF NOT EXISTS question_selections (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    source_request_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    paper_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    question_type TEXT NOT NULL CHECK (question_type IN ('choice', 'fill', 'code')),
    number_label TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    problem_id TEXT REFERENCES problems(id) ON DELETE SET NULL,
    answer_revealed_at TEXT,
    answer_reveal_source TEXT CHECK (answer_reveal_source IN ('view', 'explain')),
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (conversation_id, request_id, question_id)
)
"""

_QUESTION_SELECTIONS_ONE_ACTIVE = """
CREATE UNIQUE INDEX IF NOT EXISTS question_selections_one_active
    ON question_selections(conversation_id) WHERE active = 1
"""

_QUESTION_SELECTIONS_CONVERSATION = """
CREATE INDEX IF NOT EXISTS question_selections_conversation
    ON question_selections(conversation_id, created_at)
"""

_EXERCISE_QUESTION_SOURCES = """
CREATE TABLE IF NOT EXISTS exercise_question_sources (
    exercise_id TEXT PRIMARY KEY REFERENCES exercises(id) ON DELETE CASCADE,
    question_id TEXT NOT NULL,
    paper_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_RECORD_QUESTION_SOURCES = """
CREATE TABLE IF NOT EXISTS record_question_sources (
    record_id TEXT PRIMARY KEY REFERENCES mistake_records(id) ON DELETE CASCADE,
    question_id TEXT NOT NULL,
    paper_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_V5_STATEMENTS = (
    *_CREATE_V4_STATEMENTS,
    _QUESTION_SELECTIONS,
    _QUESTION_SELECTIONS_ONE_ACTIVE,
    _QUESTION_SELECTIONS_CONVERSATION,
    _EXERCISE_QUESTION_SOURCES,
    _RECORD_QUESTION_SOURCES,
)

_V5_MIGRATION_STATEMENTS = (
    _QUESTION_SELECTIONS,
    _QUESTION_SELECTIONS_ONE_ACTIVE,
    _QUESTION_SELECTIONS_CONVERSATION,
    _EXERCISE_QUESTION_SOURCES,
    _RECORD_QUESTION_SOURCES,
)


def _v5_migration_statements() -> tuple[str, ...]:
    return _V5_MIGRATION_STATEMENTS


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def initialize_database(db_path: Path) -> None:
    """建库、校验或迁移到 v5，并恢复遗留运行中轮次。"""

    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc

    connection = _open(db_path)
    try:
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise ConversationStorageError(_STORAGE_MESSAGE) from exc

        if version == 0:
            if not _user_objects(connection):
                _create_v5(connection)
                _enable_wal(connection)
            elif _is_compatible_v5(connection):
                _adopt_version(connection, SCHEMA_VERSION)
                _enable_wal(connection)
            elif _is_compatible_v4(connection):
                _migrate_v4_to_v5(connection)
                _enable_wal(connection)
            elif _is_compatible_v3(connection):
                _migrate_v3_to_v4(connection)
                _migrate_v4_to_v5(connection)
                _enable_wal(connection)
            elif _is_compatible_v2(connection):
                _migrate_v2_to_v3(connection)
                _migrate_v3_to_v4(connection)
                _migrate_v4_to_v5(connection)
                _enable_wal(connection)
            elif _is_compatible_v1(connection):
                _migrate_v1_to_v2(connection)
                _migrate_v2_to_v3(connection)
                _migrate_v3_to_v4(connection)
                _migrate_v4_to_v5(connection)
                _enable_wal(connection)
            else:
                raise ConversationStorageError(_UNKNOWN_MESSAGE)
        elif version == 1:
            if not _is_compatible_v1(connection):
                raise ConversationStorageError(_UPGRADE_MESSAGE)
            _migrate_v1_to_v2(connection)
            _migrate_v2_to_v3(connection)
            _migrate_v3_to_v4(connection)
            _migrate_v4_to_v5(connection)
            _enable_wal(connection)
        elif version == 2:
            if not _is_compatible_v2(connection):
                raise ConversationStorageError(_UPGRADE_MESSAGE)
            _migrate_v2_to_v3(connection)
            _migrate_v3_to_v4(connection)
            _migrate_v4_to_v5(connection)
            _enable_wal(connection)
        elif version == 3:
            if not _is_compatible_v3(connection):
                raise ConversationStorageError(_UPGRADE_MESSAGE)
            _migrate_v3_to_v4(connection)
            _migrate_v4_to_v5(connection)
            _enable_wal(connection)
        elif version == 4:
            if not _is_compatible_v4(connection):
                raise ConversationStorageError(_UPGRADE_MESSAGE)
            _migrate_v4_to_v5(connection)
            _enable_wal(connection)
        elif version == SCHEMA_VERSION:
            if not _is_compatible_v5(connection):
                raise ConversationStorageError(_UPGRADE_MESSAGE)
            _enable_wal(connection)
        else:
            raise ConversationStorageError(_UPGRADE_MESSAGE)

        _recover_running(connection)
    finally:
        connection.close()


def _open(db_path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(db_path, timeout=5.0)
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


def _user_objects(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute("SELECT name, type FROM sqlite_master").fetchall()
    except sqlite3.DatabaseError as exc:
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc
    return {
        row["name"]: row["type"]
        for row in rows
        if not row["name"].startswith("sqlite_")
    }


def _is_compatible_v1(connection: sqlite3.Connection) -> bool:
    objects = _user_objects(connection)
    if set(objects) != set(_V1_TABLES) | set(_V1_INDEXES):
        return False
    if objects.get("turns_one_running") != "index":
        return False
    return _check_common(
        connection,
        columns=_V1_COLUMNS,
        primary_keys=_V1_PRIMARY_KEYS,
        unique_sets=_V1_UNIQUE_SETS,
        foreign_keys=_V1_FOREIGN_KEYS,
        required_checks={},
    ) and _has_partial_unique_index(
        connection,
        table="conversation_turns",
        name="turns_one_running",
        column="conversation_id",
        where="wherestatus='running'",
    )


def _is_compatible_v2(connection: sqlite3.Connection) -> bool:
    objects = _user_objects(connection)
    if set(objects) != set(_V2_TABLES) | set(_V2_INDEXES):
        return False
    if objects.get("turns_one_running") != "index":
        return False
    if objects.get("problems_one_active") != "index":
        return False
    return (
        _check_common(
            connection,
            columns=_V2_COLUMNS,
            primary_keys=_V2_PRIMARY_KEYS,
            unique_sets=_V2_UNIQUE_SETS,
            foreign_keys=_V2_FOREIGN_KEYS,
            required_checks=_V2_REQUIRED_CHECKS,
        )
        and _has_partial_unique_index(
            connection,
            table="conversation_turns",
            name="turns_one_running",
            column="conversation_id",
            where="wherestatus='running'",
        )
        and _has_partial_unique_index(
            connection,
            table="problems",
            name="problems_one_active",
            column="conversation_id",
            where="whereactive=1",
        )
    )


def _is_compatible_v3(connection: sqlite3.Connection) -> bool:
    objects = _user_objects(connection)
    if set(objects) != set(_V3_TABLES) | set(_V3_INDEXES):
        return False
    if objects.get("turns_one_running") != "index":
        return False
    if objects.get("problems_one_active") != "index":
        return False
    return (
        _check_common(
            connection,
            columns=_V3_COLUMNS,
            primary_keys=_V3_PRIMARY_KEYS,
            unique_sets=_V3_UNIQUE_SETS,
            foreign_keys=_V3_FOREIGN_KEYS,
            required_checks=_V3_REQUIRED_CHECKS,
        )
        and _has_partial_unique_index(
            connection,
            table="conversation_turns",
            name="turns_one_running",
            column="conversation_id",
            where="wherestatus='running'",
        )
        and _has_partial_unique_index(
            connection,
            table="problems",
            name="problems_one_active",
            column="conversation_id",
            where="whereactive=1",
        )
    )


def _is_compatible_v4(connection: sqlite3.Connection) -> bool:
    objects = _user_objects(connection)
    if set(objects) != set(_V4_TABLES) | set(_V4_INDEXES):
        return False
    if objects.get("turns_one_running") != "index":
        return False
    if objects.get("problems_one_active") != "index":
        return False
    return (
        _check_common(
            connection,
            columns=_V4_COLUMNS,
            primary_keys=_V4_PRIMARY_KEYS,
            unique_sets=_V4_UNIQUE_SETS,
            foreign_keys=_V4_FOREIGN_KEYS,
            required_checks=_V4_REQUIRED_CHECKS,
        )
        and _has_partial_unique_index(
            connection,
            table="conversation_turns",
            name="turns_one_running",
            column="conversation_id",
            where="wherestatus='running'",
        )
        and _has_partial_unique_index(
            connection,
            table="problems",
            name="problems_one_active",
            column="conversation_id",
            where="whereactive=1",
        )
    )


def _is_compatible_v5(connection: sqlite3.Connection) -> bool:
    objects = _user_objects(connection)
    if set(objects) != set(_V5_TABLES) | set(_V5_INDEXES):
        return False
    if objects.get("turns_one_running") != "index":
        return False
    if objects.get("problems_one_active") != "index":
        return False
    if objects.get("question_selections_one_active") != "index":
        return False
    return (
        _check_common(
            connection,
            columns=_V5_COLUMNS,
            primary_keys=_V5_PRIMARY_KEYS,
            unique_sets=_V5_UNIQUE_SETS,
            foreign_keys=_V5_FOREIGN_KEYS,
            required_checks=_V5_REQUIRED_CHECKS,
        )
        and _has_partial_unique_index(
            connection,
            table="conversation_turns",
            name="turns_one_running",
            column="conversation_id",
            where="wherestatus='running'",
        )
        and _has_partial_unique_index(
            connection,
            table="problems",
            name="problems_one_active",
            column="conversation_id",
            where="whereactive=1",
        )
        and _has_partial_unique_index(
            connection,
            table="question_selections",
            name="question_selections_one_active",
            column="conversation_id",
            where="whereactive=1",
        )
    )


def _check_common(
    connection: sqlite3.Connection,
    *,
    columns: dict[str, tuple[str, ...]],
    primary_keys: dict[str, tuple[str, ...]],
    unique_sets: set[tuple[str, ...]],
    foreign_keys: set[tuple[str, str, str]],
    required_checks: dict[str, tuple[str, ...]],
) -> bool:
    for table, expected in columns.items():
        try:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.DatabaseError as exc:
            raise ConversationStorageError(_STORAGE_MESSAGE) from exc
        if tuple(row["name"] for row in rows) != expected:
            return False

    for table, expected_pk in primary_keys.items():
        if _primary_key_columns(connection, table) != expected_pk:
            return False

    found_unique = _unique_index_columns(connection, tuple(columns))
    if not unique_sets.issubset(found_unique):
        return False

    if _foreign_key_set(connection, tuple(columns)) != foreign_keys:
        return False

    for table, needles in required_checks.items():
        sql = _normalized_table_sql(connection, table)
        if sql is None or any(needle not in sql for needle in needles):
            return False

    return True


def _primary_key_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[str, ...]:
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.DatabaseError as exc:
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc
    ordered = sorted(
        ((row["pk"], row["name"]) for row in rows if row["pk"]),
        key=lambda item: item[0],
    )
    return tuple(name for _, name in ordered)


def _unique_index_columns(
    connection: sqlite3.Connection, tables: tuple[str, ...]
) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    try:
        for table in tables:
            indexes = connection.execute(f"PRAGMA index_list({table})").fetchall()
            for index in indexes:
                if not index["unique"]:
                    continue
                columns = tuple(
                    row["name"]
                    for row in connection.execute(
                        f"PRAGMA index_info({index['name']})"
                    ).fetchall()
                )
                result.add(columns)
    except sqlite3.DatabaseError as exc:
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc
    return result


def _foreign_key_set(
    connection: sqlite3.Connection, tables: tuple[str, ...]
) -> set[tuple[str, str, str]]:
    result: set[tuple[str, str, str]] = set()
    try:
        for table in tables:
            for row in connection.execute(
                f"PRAGMA foreign_key_list({table})"
            ).fetchall():
                result.add((row["from"], row["table"], row["to"]))
    except sqlite3.DatabaseError as exc:
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc
    return result


def _normalized_table_sql(
    connection: sqlite3.Connection, table: str
) -> str | None:
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc
    if row is None or not row["sql"]:
        return None
    return "".join(row["sql"].lower().split())


def _has_partial_unique_index(
    connection: sqlite3.Connection,
    *,
    table: str,
    name: str,
    column: str,
    where: str,
) -> bool:
    """确认同名索引唯一、只索引指定列、带指定 WHERE 条件。"""

    try:
        indexes = connection.execute(f"PRAGMA index_list({table})").fetchall()
        target = next((index for index in indexes if index["name"] == name), None)
        if target is None or not target["unique"]:
            return False
        if "partial" in target.keys() and not target["partial"]:
            return False

        columns = tuple(
            row["name"]
            for row in connection.execute(f"PRAGMA index_info({name})").fetchall()
        )
        if columns != (column,):
            return False

        row = connection.execute(
            "SELECT tbl_name, sql FROM sqlite_master "
            "WHERE type = 'index' AND name = ?",
            (name,),
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc

    if row is None or not row["sql"] or row["tbl_name"] != table:
        return False
    normalized = "".join(row["sql"].lower().split())
    return where in normalized


# ---------------------------------------------------------------------------
# 创建、迁移、恢复
# ---------------------------------------------------------------------------


def _create_v2(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in _CREATE_V2_STATEMENTS:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            connection.rollback()
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc


def _create_v3(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in _CREATE_V3_STATEMENTS:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            connection.rollback()
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc


def _create_v4(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in _CREATE_V4_STATEMENTS:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            connection.rollback()
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc


def _create_v5(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in _CREATE_V5_STATEMENTS:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            connection.rollback()
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """单事务升级；失败回滚，不切换 WAL，原数据保持不变。"""

    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in _migration_statements():
            connection.execute(statement)

        rows = connection.execute(
            "SELECT id, query FROM conversation_turns"
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE conversation_turns SET input_json = ? WHERE id = ?",
                (query_input(row["query"]), row["id"]),
            )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            connection.rollback()
        logger.warning("本地学习数据库 v1→v2 迁移失败：%s", exc)
        raise ConversationStorageError(_MIGRATION_MESSAGE) from exc


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """单事务升级：从 v2 升级到 v3，增加练习、评价及上下文。"""

    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in _v3_migration_statements():
            connection.execute(statement)

        # 为已有会话初始化默认上下文
        rows = connection.execute("SELECT id, updated_at FROM conversations").fetchall()
        for row in rows:
            cid = row["id"]
            up_at = row["updated_at"]
            act_p = connection.execute(
                "SELECT id FROM problems WHERE conversation_id = ? AND active = 1",
                (cid,),
            ).fetchone()
            act_pid = act_p["id"] if act_p else None
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_contexts
                (conversation_id, confirmed_course, active_problem_id, active_exercise_id, last_suggestions_json, last_action_summary, updated_at)
                VALUES (?, NULL, ?, NULL, NULL, NULL, ?)
                """,
                (cid, act_pid, up_at),
            )
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            connection.rollback()
        logger.warning("本地学习数据库 v2→v3 迁移失败：%s", exc)
        raise ConversationStorageError(_MIGRATION_MESSAGE) from exc


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    """单事务升级：增加不可变作答尝试，并把旧作答回填为第 1 次尝试。"""

    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in _v4_migration_statements():
            connection.execute(statement)
        connection.execute(_V4_BACKFILL_ATTEMPTS)
        connection.execute(_V4_BACKFILL_ATTEMPT_ATTACHMENTS)
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            connection.rollback()
        logger.warning("本地学习数据库 v3→v4 迁移失败：%s", exc)
        raise ConversationStorageError(_MIGRATION_MESSAGE) from exc


def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
    """单事务升级：只追加真题选中、练习来源和错题来源三张表。"""

    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in _v5_migration_statements():
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            connection.rollback()
        logger.warning("本地学习数据库 v4→v5 迁移失败：%s", exc)
        raise ConversationStorageError(_MIGRATION_MESSAGE) from exc


def _adopt_version(connection: sqlite3.Connection, version: int) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            connection.rollback()
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc


def _enable_wal(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.DatabaseError as exc:
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc


def _recover_running(connection: sqlite3.Connection) -> None:
    """启动恢复：遗留 running 轮次变成 interrupted，不重跑模型。"""

    from datetime import datetime, timezone

    try:
        running = connection.execute(
            "SELECT COUNT(*) FROM conversation_turns WHERE status = 'running'"
        ).fetchone()[0]
        if not running:
            return

        now = datetime.now(timezone.utc).isoformat()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE conversations SET updated_at = ?
            WHERE id IN (
                SELECT conversation_id FROM conversation_turns
                WHERE status = 'running'
            )
            """,
            (now,),
        )
        connection.execute(
            """
            UPDATE conversation_turns
            SET status = 'interrupted', finished_at = ?
            WHERE status = 'running'
            """,
            (now,),
        )
        connection.commit()
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            connection.rollback()
        raise ConversationStorageError(_STORAGE_MESSAGE) from exc
