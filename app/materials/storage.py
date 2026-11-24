"""个人 PDF 原件的本地安全存储与回收。

安全边界：
- 文件名由服务端生成，数据库只保存相对路径；
- 读取时解析并校验位于 MATERIALS_ROOT 内，拒绝越界；
- 原子写入（写临时文件 + fsync + os.replace）。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from app.materials.errors import MaterialNotFoundError, MaterialStorageError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MATERIALS_ROOT = PROJECT_ROOT / "data" / "local" / "materials"


def sanitize_display_name(raw: str | None) -> str:
    name = Path(raw or "document.pdf").name.strip()
    if not name:
        name = "document.pdf"
    if len(name) > 120:
        stem = Path(name).stem[:100]
        suffix = Path(name).suffix[:16] or ".pdf"
        name = f"{stem}{suffix}"
    return name


def save_material_pdf(
    root: Path,
    owner_id: str,
    material_id: str,
    data: bytes,
) -> tuple[str, str]:
    """原子保存 PDF 原件，返回 (stored_name, file_sha256)。"""

    directory = root / owner_id
    filename = f"{material_id}.pdf"
    target = directory / filename
    temp = directory / f".{material_id}.tmp"
    stored_name = f"{owner_id}/{filename}"
    sha256 = hashlib.sha256(data).hexdigest()

    try:
        directory.mkdir(parents=True, exist_ok=True)
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise MaterialStorageError("个人资料存储暂时不可用，请稍后再试。") from exc

    return stored_name, sha256


def resolve_material_pdf(root: Path, stored_name: str) -> Path:
    """解析并校验 PDF 路径严格位于 root 内；缺失抛 MaterialNotFoundError。"""

    try:
        root_resolved = root.resolve()
        candidate = (root / stored_name).resolve()
        candidate.relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise MaterialNotFoundError("个人资料文件不存在。") from exc

    if not candidate.is_file():
        raise MaterialNotFoundError("个人资料文件不存在。")
    return candidate


def delete_material_pdf(root: Path, stored_name: str) -> None:
    try:
        root_resolved = root.resolve()
        candidate = (root / stored_name).resolve()
        candidate.relative_to(root_resolved)
        candidate.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def try_delete_file(root: Path, stored_name: str) -> str | None:
    """尝试删除 root 内的文件；成功或已不存在返回 None，失败返回错误原因。"""

    try:
        root_resolved = root.resolve()
        candidate = (root / stored_name).resolve()
        candidate.relative_to(root_resolved)
        candidate.unlink(missing_ok=True)
        return None
    except (OSError, ValueError) as exc:
        return str(exc) or exc.__class__.__name__
