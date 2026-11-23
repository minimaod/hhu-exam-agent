"""自愿贡献试卷 PDF 的私有待审目录存储。

安全边界：
- 待审文件存放在 data/local/contributions/<contribution_id>/ 下，
  位于公开静态目录和 data/papers/ 下载目录之外；
- 不提供任何学生侧或公开下载接口，仅项目维护者通过本地或服务器权限查看；
- 保存前复用 F019 隔离子进程限时解析 PDF，确认至少有一页可读，拒绝损坏文件；
- 先在临时目录写齐 source.pdf 与 metadata.json，再整体移入待审目录；
  任意步骤失败时清理临时目录，不残留缺少元数据的 PDF。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from app.contributions.errors import (
    ContributionInvalidError,
    ContributionParseTimeoutError,
    ContributionStorageError,
)
from app.contributions.models import ContributionSubmitResponse
from app.materials.errors import (
    MaterialInvalidError,
    MaterialPageLimitError,
    MaterialParseTimeoutError,
)
from app.materials.pdf_parser import run_isolated_pdf_parse
from app.materials.storage import sanitize_display_name

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRIBUTIONS_ROOT = PROJECT_ROOT / "data" / "local" / "contributions"

_PDF_MAGIC = b"%PDF-"
_MAX_COURSE_LENGTH = 80
_MAX_YEAR_LENGTH = 20


def validate_contribution_fields(
    *,
    course: str,
    year: str,
    data: bytes,
) -> tuple[str, str]:
    """校验学科、年份和 PDF 内容，返回规范化后的 (course, year)。"""

    cleaned_course = course.strip()
    if not cleaned_course:
        raise ContributionInvalidError("请填写学科名称。")
    if len(cleaned_course) > _MAX_COURSE_LENGTH:
        raise ContributionInvalidError(f"学科名称不能超过 {_MAX_COURSE_LENGTH} 个字符。")

    cleaned_year = year.strip()
    if not cleaned_year:
        raise ContributionInvalidError("请填写试卷年份。")
    if len(cleaned_year) > _MAX_YEAR_LENGTH:
        raise ContributionInvalidError(f"年份不能超过 {_MAX_YEAR_LENGTH} 个字符。")

    if not data:
        raise ContributionInvalidError("请选择非空的 PDF 文件。")
    if not data.lstrip()[:10].startswith(_PDF_MAGIC):
        raise ContributionInvalidError("仅支持有效的 PDF 文件。")

    return cleaned_course, cleaned_year


def _atomic_write_bytes(target: Path, content: bytes) -> None:
    temp = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temp.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ContributionStorageError("待审资料存储暂时不可用，请稍后再试。") from exc


def save_contribution(
    *,
    root: Path,
    owner_id: str,
    username: str,
    course: str,
    year: str,
    filename: str | None,
    data: bytes,
    max_pages: int = 200,
    parse_timeout_seconds: float = 20.0,
) -> ContributionSubmitResponse:
    """校验并把学生提交的试卷 PDF 与元数据整体原子写入私有待审目录。"""

    cleaned_course, cleaned_year = validate_contribution_fields(
        course=course,
        year=year,
        data=data,
    )
    contribution_id = str(uuid4())
    temp_dir = root / f".tmp-{contribution_id}"
    entry_dir = root / contribution_id

    try:
        try:
            temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ContributionStorageError("待审资料存储暂时不可用，请稍后再试。") from exc

        temp_pdf = temp_dir / "source.pdf"
        _atomic_write_bytes(temp_pdf, data)

        try:
            parsed = run_isolated_pdf_parse(
                temp_pdf,
                max_pages=max_pages,
                start_page=1,
                end_page=1,
                timeout_seconds=parse_timeout_seconds,
            )
        except MaterialParseTimeoutError as exc:
            raise ContributionParseTimeoutError(str(exc)) from exc
        except (MaterialInvalidError, MaterialPageLimitError) as exc:
            raise ContributionInvalidError(str(exc)) from exc

        if parsed.page_count < 1 or not parsed.pages:
            raise ContributionInvalidError("PDF 文件缺少可读页面，请检查后重新上传。")

        sha256 = hashlib.sha256(data).hexdigest()
        submitted_at = datetime.now(timezone.utc).isoformat()
        safe_filename = sanitize_display_name(filename)

        metadata = {
            "contribution_id": contribution_id,
            "owner_id": owner_id,
            "username": username,
            "course": cleaned_course,
            "year": cleaned_year,
            "original_filename": safe_filename,
            "stored_file": "source.pdf",
            "file_sha256": sha256,
            "size_bytes": len(data),
            "page_count": parsed.page_count,
            "status": "pending_review",
            "submitted_at": submitted_at,
        }
        metadata_bytes = (
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

        _atomic_write_bytes(temp_dir / "metadata.json", metadata_bytes)

        try:
            os.replace(temp_dir, entry_dir)
        except OSError as exc:
            raise ContributionStorageError("待审资料存储暂时不可用，请稍后再试。") from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return ContributionSubmitResponse(
        contribution_id=contribution_id,
        course=cleaned_course,
        year=cleaned_year,
    )
