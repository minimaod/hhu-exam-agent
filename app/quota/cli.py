"""F013 管理员用量命令行。

用法：
    uv run python -m app.quota.cli report --date 2026-11-10
    uv run python -m app.quota.cli report --username 同学 --json
    uv run python -m app.quota.cli set-limit --username 同学 --requests 2 --until 2026-11-12
    uv run python -m app.quota.cli clear-limit --username 同学
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

from app.config import load_usage_config_state
from app.conversations.repository import DEFAULT_DB_PATH
from app.quota.errors import QuotaStorageError
from app.quota.gate import UsageGate, UsageLimits, beijing_day


def _default_db() -> Path:
    raw = os.environ.get("HHU_STUDY_DB")
    return Path(raw).expanduser() if raw else DEFAULT_DB_PATH


def _gate(database: Path) -> UsageGate:
    config = load_usage_config_state()
    if config.error:
        raise SystemExit(config.error)
    return UsageGate(
        database,
        limits=UsageLimits(
            model_requests=config.model_requests,
            site_requests=config.site_requests,
            images=config.images,
        ),
    )


def _user_id(database: Path, username: str) -> str:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT id FROM users WHERE username_normalized = ?",
            (username.strip().casefold(),),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise SystemExit(f"未找到用户：{username}")
    return str(row[0])


def _cmd_report(args: argparse.Namespace) -> int:
    gate = _gate(Path(args.database))
    report = gate.report(args.date)
    if args.username:
        owner_id = _user_id(Path(args.database), args.username)
        report["users"] = [
            item for item in report["users"] if item["owner_id"] == owner_id
        ]
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    site = report["site"]
    print(
        f"{report['date']} 全站模型请求：{site['model_requests']['used']}/"
        f"{site['model_requests']['limit']}，图片：{site['images']}；"
        f"重置：{report['reset_at']}"
    )
    for item in report["users"]:
        print(
            f"{item['owner_id']}\t模型 {item['model_requests']}，"
            f"图片 {item['images']}\t个人额度 {item['limit']}/{item['image_limit']}"
        )
    if report["overrides"]:
        print("临时覆盖：")
        for item in report["overrides"]:
            print(f"  {item}")
    return 0


def _cmd_set_limit(args: argparse.Namespace) -> int:
    if args.requests is None and args.images is None:
        raise SystemExit("至少提供 --requests 或 --images。")
    try:
        until = date.fromisoformat(args.until)
    except ValueError as exc:
        raise SystemExit("--until 必须是 YYYY-MM-DD。") from exc
    if until < beijing_day():
        raise SystemExit("--until 不能早于今天。")
    gate = _gate(Path(args.database))
    owner_id = _user_id(Path(args.database), args.username)
    gate.set_override(
        owner_id,
        daily_requests=args.requests,
        daily_images=args.images,
        expires_on=until.isoformat(),
    )
    print(f"已设置 {args.username} 的临时额度，有效期至 {until}。")
    return 0


def _cmd_clear_limit(args: argparse.Namespace) -> int:
    gate = _gate(Path(args.database))
    owner_id = _user_id(Path(args.database), args.username)
    gate.clear_override(owner_id)
    print(f"已取消 {args.username} 的临时额度。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="F013 模型用量与费用限制")
    parser.add_argument("--database", default=str(_default_db()))
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="查看某天用量")
    report.add_argument("--date")
    report.add_argument("--username")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=_cmd_report)

    set_limit = sub.add_parser("set-limit", help="设置个人临时额度")
    set_limit.add_argument("--username", required=True)
    set_limit.add_argument("--requests", type=int)
    set_limit.add_argument("--images", type=int)
    set_limit.add_argument("--until", required=True)
    set_limit.set_defaults(func=_cmd_set_limit)

    clear = sub.add_parser("clear-limit", help="取消个人临时额度")
    clear.add_argument("--username", required=True)
    clear.set_defaults(func=_cmd_clear_limit)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except QuotaStorageError as exc:
        print(f"用量库不可用：{exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())