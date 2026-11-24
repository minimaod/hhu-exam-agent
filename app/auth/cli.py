"""F012/F013 管理员命令行。

用法：
    uv run python -m app.auth.cli migrate --admin-username admin
    uv run python -m app.auth.cli create-invite --label "给同学" --expires-in-days 7
    uv run python -m app.auth.cli list-users
    uv run python -m app.auth.cli disable-user --username 同学
    uv run python -m app.auth.cli restore --backup <path> --yes
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.auth.migrate import (
    MigrationCommittedCheckError,
    MigrationRolledBackError,
    migrate_database,
    restore_backup,
)
from app.auth.passwords import (
    AuthPasswordError,
    hash_password,
    normalize_username,
    validate_password,
    validate_username,
)
from app.auth.repository import AuthRepository
from app.conversations import schema
from app.conversations.errors import ConversationStorageError
from app.conversations.repository import DEFAULT_DB_PATH


def _default_db() -> Path:
    raw = os.environ.get("HHU_STUDY_DB")
    return Path(raw).expanduser() if raw else DEFAULT_DB_PATH


def _read_password(args: argparse.Namespace) -> str:
    if getattr(args, "admin_password_stdin", False):
        first = sys.stdin.readline().rstrip("\r\n")
        second = sys.stdin.readline().rstrip("\r\n")
    else:
        first = getpass.getpass("管理员密码：")
        second = getpass.getpass("再次输入：")
    if first != second:
        raise SystemExit("两次密码不一致。")
    return first


def _db_version(db_path: Path) -> int:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _cmd_migrate(args: argparse.Namespace) -> int:
    db_path = Path(args.database)
    if not db_path.is_file():
        raise SystemExit(f"数据库不存在：{db_path}")
    try:
        version = _db_version(db_path)
        owner = None
        if version == 6:
            if not args.admin_username:
                raise SystemExit("从 v6 升级需要 --admin-username。")
            username = validate_username(args.admin_username)
            password = _read_password(args)
            validate_password(password)
            owner = schema.SchemaOwner(
                id=str(uuid4()),
                username=username,
                username_normalized=normalize_username(username),
                password_hash=hash_password(password),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        report = migrate_database(
            db_path,
            owner=owner,
            backup_dir=Path(args.backup_dir)
            if args.backup_dir
            else db_path.parent / "backups",
        )
    except MigrationCommittedCheckError as exc:
        print(f"迁移已提交为 v{schema.SCHEMA_VERSION}，但提交后检查失败：{exc}", file=sys.stderr)
        return 1
    except MigrationRolledBackError as exc:
        print(f"迁移失败，事务已回滚：{exc}", file=sys.stderr)
        return 1
    except (AuthPasswordError, ConversationStorageError) as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1
    if report.already_migrated:
        print(f"数据库已是 v{report.version}：会话 {report.conversations} 条，无需修改。")
    else:
        print(f"迁移完成：v{report.version}，会话 {report.conversations} 条。")
        print(f"备份：{report.backup_path}")
    return 0


def _repo(args: argparse.Namespace) -> AuthRepository:
    db_path = Path(args.database)
    if not db_path.is_file():
        raise SystemExit(f"数据库不存在：{db_path}")
    return AuthRepository(db_path)


def _cmd_create_invite(args: argparse.Namespace) -> int:
    repo = _repo(args)
    invite_id, raw = repo.create_invite(
        label=args.label,
        expires_in_days=args.expires_in_days,
    )
    print(f"邀请码（只显示一次）：{raw}")
    print(f"邀请码 id：{invite_id}")
    return 0


def _cmd_list_users(args: argparse.Namespace) -> int:
    repo = _repo(args)
    for user in repo.list_users():
        status = "已禁用" if user.disabled else "正常"
        print(f"{user.id}\t{user.username}\t{status}\t{user.created_at}")
    return 0


def _cmd_disable_user(args: argparse.Namespace) -> int:
    repo = _repo(args)
    user = repo.disable_user(args.username)
    if user is None:
        print("未找到该用户。", file=sys.stderr)
        return 1
    print(f"已禁用 {user.username}，并撤销其全部登录会话。")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    if not args.yes:
        print("恢复会覆盖目标数据库；请加 --yes 明确确认。", file=sys.stderr)
        return 1
    try:
        restore_backup(Path(args.backup), Path(args.database))
    except ConversationStorageError as exc:
        print(f"恢复失败：{exc}", file=sys.stderr)
        return 1
    print("恢复完成。请确认后端已停止后重新启动。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="F012 账号与迁移管理")
    parser.add_argument("--database", default=str(_default_db()))
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate", help="v6/v7/v8→v9 迁移（自动备份）")
    migrate.add_argument("--admin-username")
    migrate.add_argument("--backup-dir")
    migrate.add_argument(
        "--admin-password-stdin",
        action="store_true",
        help="从标准输入读两行密码（自动化用；默认交互输入）",
    )
    migrate.set_defaults(func=_cmd_migrate)

    invite = sub.add_parser("create-invite", help="生成一次性邀请码")
    invite.add_argument("--label")
    invite.add_argument("--expires-in-days", type=int)
    invite.set_defaults(func=_cmd_create_invite)

    users = sub.add_parser("list-users", help="列出用户")
    users.set_defaults(func=_cmd_list_users)

    disable = sub.add_parser("disable-user", help="禁用用户并撤销会话")
    disable.add_argument("--username", required=True)
    disable.set_defaults(func=_cmd_disable_user)

    restore = sub.add_parser("restore", help="从备份恢复")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--yes", action="store_true")
    restore.set_defaults(func=_cmd_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())