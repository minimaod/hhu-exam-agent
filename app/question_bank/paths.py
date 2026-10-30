"""题目库目录布局与 current.json 指针。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.question_bank.config import DEFAULT_BANK_ROOT, SCHEMA_VERSION

BUILD_ID_RE = re.compile(r"^iv-[0-9a-f]{16}$")
CURRENT_FIELDS = ("schema_version", "build_id", "source_version", "index_version")


class BankLayoutError(ValueError):
    """目录或版本指针不合法。"""


@dataclass(frozen=True)
class BankPaths:
    """一个题目库根目录下的固定布局。"""

    root: Path = DEFAULT_BANK_ROOT

    @property
    def db_dir(self) -> Path:
        return self.root / "db"

    @property
    def builds_dir(self) -> Path:
        return self.db_dir / "builds"

    @property
    def current_file(self) -> Path:
        return self.db_dir / "current.json"

    @property
    def reviews_file(self) -> Path:
        return self.db_dir / "reviews.json"

    @property
    def taxonomy_dir(self) -> Path:
        return self.root / "taxonomy"

    @property
    def taxonomy_file(self) -> Path:
        return self.taxonomy_dir / "knowledge-points.json"

    @property
    def stopwords_file(self) -> Path:
        return self.taxonomy_dir / "stopwords-zh.txt"

    @property
    def eval_dir(self) -> Path:
        return self.root / "eval"

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence" / "p3"

    @property
    def model_dir(self) -> Path:
        return self.root / "models"

    def build_dir(self, build_id: str) -> Path:
        if not BUILD_ID_RE.match(build_id):
            raise BankLayoutError(f"非法 build_id：{build_id!r}")
        return self.builds_dir / build_id


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """同目录临时文件 + os.replace，避免读到半个指针。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def read_current(paths: BankPaths) -> dict[str, Any] | None:
    """读取当前版本指针；不存在返回 None，存在但不合法则报错。"""

    path = paths.current_file
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BankLayoutError("current.json 无法读取或不是合法 JSON") from exc
    if not isinstance(payload, dict) or any(field not in payload for field in CURRENT_FIELDS):
        raise BankLayoutError("current.json 缺少必要字段")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise BankLayoutError("current.json 的 schema_version 与当前代码不一致")
    build_id = str(payload["build_id"])
    if not BUILD_ID_RE.match(build_id):
        raise BankLayoutError("current.json 的 build_id 不合法")
    if payload["index_version"] != build_id:
        raise BankLayoutError("current.json 的 index_version 与 build_id 不一致")
    return payload


def write_current(
    paths: BankPaths,
    *,
    build_id: str,
    source_version: str,
    index_version: str,
    previous_build_id: str | None,
    activated_at: str,
) -> None:
    atomic_write_json(
        paths.current_file,
        {
            "schema_version": SCHEMA_VERSION,
            "build_id": build_id,
            "source_version": source_version,
            "index_version": index_version,
            "previous_build_id": previous_build_id,
            "activated_at": activated_at,
        },
    )


def relative(path: Path) -> str:
    """项目内路径用相对写法，便于报告引用。"""

    from app.question_bank.config import PROJECT_ROOT

    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)