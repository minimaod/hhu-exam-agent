"""个人资料、物理页缓存与复习清单的 SQLite 仓库。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.materials.errors import (
    GuideNotFoundError,
    MaterialConflictError,
    MaterialNotFoundError,
    MaterialStorageError,
)
from app.materials.models import (
    ArtifactCoverage,
    ArtifactDetailView,
    ArtifactSummary,
    ConversationDeleteImpactView,
    ConversationMaterialItem,
    DeleteImpactView,
    GraphNoteView,
    GuideItemView,
    GuideSummary,
    MaterialLibraryItem,
    MaterialPageView,
    MaterialSummary,
    OutputKind,
    OutputStatus,
    QuizAttemptView,
    QuizQuestionPublicView,
    ReadabilityOverview,
    ReadabilityReport,
    ReviewGuideView,
    VerifiedCitation,
)
from app.materials.coverage import describe_outputs
from app.materials.workbench import NOTE_WARNING, build_kc_index, ensure_note_anchors, graph_nodes
from app.materials.ocr import (
    OCR_EXCLUDED_PREFIX, OCR_FAILED_VERSION, OCR_PAGE_VERSION,
    ExcludedOcrPage, FailedOcrPage, OcrPage, page_parser_version, page_status,
)
from app.materials.pdf_parser import PARSER_VERSION, PDF_TEXT_CACHE_VERSION, ParsedPage

CONNECT_TIMEOUT_SECONDS = 5.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


@dataclass(frozen=True)
class MaterialRecord:
    id: str
    owner_id: str
    display_name: str
    stored_name: str
    file_sha256: str
    size_bytes: int
    page_count: int
    status: str
    dedicated_conversation_id: str | None
    created_at: datetime
    updated_at: datetime

    def to_summary(self) -> MaterialSummary:
        return MaterialSummary(
            id=self.id,
            display_name=self.display_name,
            file_sha256=self.file_sha256,
            size_bytes=self.size_bytes,
            page_count=self.page_count,
            dedicated_conversation_id=self.dedicated_conversation_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


@dataclass(frozen=True)
class GuideAcquireResult:
    guide_id: str
    status: str
    reused: bool
    generation_token: str | None
    conversation_id: str | None


@dataclass(frozen=True)
class ArtifactAcquireResult:
    artifact_id: str
    status: str
    reused: bool
    generation_token: str | None
    conversation_id: str | None
    current_step: str
    requested_outputs: tuple[OutputKind, ...]
    notes_status: OutputStatus
    quiz_status: OutputStatus
    graph_status: OutputStatus
    existing_skeleton: dict[str, object] | None
    existing_graph_tree: dict[str, object] | None
    existing_notes_html: str | None
    existing_questions: list[dict[str, object]]
    existing_coverage_warnings: list[str]


_VALID_OUTPUTS: tuple[OutputKind, ...] = ("notes", "quiz", "graph")


def _normalize_outputs(outputs: list[OutputKind] | tuple[OutputKind, ...] | None) -> tuple[OutputKind, ...]:
    if not outputs:
        return _VALID_OUTPUTS
    seen: list[OutputKind] = []
    for item in outputs:
        if item in _VALID_OUTPUTS and item not in seen:
            seen.append(item)
    return tuple(seen) if seen else _VALID_OUTPUTS


def _parse_requested_outputs(raw: object) -> list[OutputKind]:
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return list(_normalize_outputs([x for x in data if x in _VALID_OUTPUTS]))  # type: ignore[arg-type]
        except json.JSONDecodeError:
            pass
    return list(_VALID_OUTPUTS)


def _read_artifact_coverage(raw: object) -> tuple[list[str], ArtifactCoverage | None]:
    data = json.loads(str(raw)) if raw else []
    if isinstance(data, list):
        return data, None
    return data["warnings"], ArtifactCoverage.model_validate(data["input"])


def _write_artifact_coverage(warnings: list[str], coverage: ArtifactCoverage | None) -> str:
    data = {"warnings": warnings, "input": coverage.model_dump()} if coverage else warnings
    return json.dumps(data, ensure_ascii=False)


class MaterialRepository:
    def __init__(self, db_path: Path, *, owner_id: str) -> None:
        self._db_path = Path(db_path)
        self._owner_id = owner_id

    @property
    def owner_id(self) -> str:
        return self._owner_id

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self._db_path, timeout=CONNECT_TIMEOUT_SECONDS)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            return conn
        except sqlite3.Error as exc:
            raise MaterialStorageError("个人资料数据库暂时不可用，请稍后再试。") from exc

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        except sqlite3.Error as exc:
            raise MaterialStorageError("个人资料读取失败，请稍后再试。") from exc
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise MaterialStorageError("个人资料保存失败，请稍后再试。") from exc
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def _row_to_material(row: sqlite3.Row) -> MaterialRecord:
        return MaterialRecord(
            id=str(row["id"]),
            owner_id=str(row["owner_id"]),
            display_name=str(row["display_name"]),
            stored_name=str(row["stored_name"]),
            file_sha256=str(row["file_sha256"]),
            size_bytes=int(row["size_bytes"]),
            page_count=int(row["page_count"]),
            status=str(row["status"]),
            dedicated_conversation_id=row["dedicated_conversation_id"],
            created_at=_parse_time(str(row["created_at"])),
            updated_at=_parse_time(str(row["updated_at"])),
        )

    def count_active_materials(self) -> int:
        with self._read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM personal_materials WHERE owner_id = ? AND status = 'active'",
                (self._owner_id,),
            ).fetchone()
            return int(row[0]) if row is not None else 0

    def create_material(
        self,
        *,
        material_id: str,
        display_name: str,
        stored_name: str,
        file_sha256: str,
        size_bytes: int,
        page_count: int,
        max_per_user: int,
    ) -> MaterialRecord:
        now = _now()
        with self._write() as conn:
            count_row = conn.execute(
                "SELECT COUNT(*) FROM personal_materials WHERE owner_id = ? AND status = 'active'",
                (self._owner_id,),
            ).fetchone()
            if count_row is not None and int(count_row[0]) >= max_per_user:
                raise MaterialConflictError(
                    f"每位账号最多保存 {max_per_user} 份个人资料，请先删除不再使用的资料。"
                )
            conn.execute(
                """
                INSERT INTO personal_materials (
                    id, owner_id, display_name, stored_name, file_sha256,
                    size_bytes, page_count, status, dedicated_conversation_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?)
                """,
                (
                    material_id,
                    self._owner_id,
                    display_name,
                    stored_name,
                    file_sha256,
                    size_bytes,
                    page_count,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM personal_materials WHERE id = ?",
                (material_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_material(row)

    def list_materials(self) -> list[MaterialSummary]:
        with self._read() as conn:
            rows = conn.execute(
                """
                SELECT * FROM personal_materials
                WHERE owner_id = ? AND status = 'active'
                ORDER BY created_at DESC, id ASC
                """,
                (self._owner_id,),
            ).fetchall()
            return [self._row_to_material(r).to_summary() for r in rows]

    def get_material(self, material_id: str) -> MaterialRecord:
        with self._read() as conn:
            row = conn.execute(
                """
                SELECT * FROM personal_materials
                WHERE id = ? AND owner_id = ? AND status = 'active'
                """,
                (material_id, self._owner_id),
            ).fetchone()
            if row is None:
                raise MaterialNotFoundError("个人资料不存在。")
            return self._row_to_material(row)

    def save_parsed_pages(self, material_id: str, pages: list[ParsedPage]) -> None:
        if not pages:
            return
        now = _now()
        with self._write() as conn:
            row = conn.execute(
                "SELECT 1 FROM personal_materials WHERE id = ? AND owner_id = ? AND status = 'active'",
                (material_id, self._owner_id),
            ).fetchone()
            if row is None:
                raise MaterialNotFoundError("个人资料不存在或正在删除。")
            for page in pages:
                version = page_parser_version(page)
                conn.execute(
                    """
                    INSERT INTO personal_material_pages (
                        material_id, page_no, extracted_text, char_count,
                        scanned_suspect, garbled_ratio, formula_warning,
                        parser_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(material_id, page_no) DO UPDATE SET
                        extracted_text = excluded.extracted_text,
                        char_count = excluded.char_count,
                        scanned_suspect = excluded.scanned_suspect,
                        garbled_ratio = excluded.garbled_ratio,
                        formula_warning = excluded.formula_warning,
                        parser_version = excluded.parser_version
                    WHERE excluded.parser_version != ? OR personal_material_pages.parser_version IN (?, ?)
                    """,
                    (
                        material_id,
                        page.page_no,
                        page.extracted_text,
                        page.char_count,
                        1 if page.scanned_suspect else 0,
                        page.garbled_ratio,
                        1 if page.formula_warning else 0,
                        PDF_TEXT_CACHE_VERSION if version == PARSER_VERSION else version,
                        now,
                        PDF_TEXT_CACHE_VERSION,
                        PARSER_VERSION,
                        PDF_TEXT_CACHE_VERSION,
                    ),
                )

    def reuse_visual_pages(self, material_id: str, start_page: int, end_page: int) -> int:
        """Copy completed visual checks from identical files owned by the same account."""
        with self._write() as conn:
            conn.execute(
                """
                WITH matching_pages AS (
                    SELECT p.*, ROW_NUMBER() OVER (
                        PARTITION BY p.page_no
                        ORDER BY p.created_at DESC, original.created_at DESC, original.id DESC
                    ) AS preference
                    FROM personal_materials target
                    JOIN personal_materials original
                      ON original.owner_id = target.owner_id
                     AND original.file_sha256 = target.file_sha256
                     AND original.id != target.id
                     AND original.status = 'active'
                    JOIN personal_material_pages p ON p.material_id = original.id
                    WHERE target.id = ? AND target.owner_id = ? AND target.status = 'active'
                      AND p.page_no BETWEEN ? AND ?
                      AND (p.parser_version = ? OR p.parser_version LIKE ?)
                )
                UPDATE personal_material_pages AS current
                SET extracted_text = previous.extracted_text,
                    char_count = previous.char_count,
                    scanned_suspect = previous.scanned_suspect,
                    garbled_ratio = previous.garbled_ratio,
                    formula_warning = previous.formula_warning,
                    parser_version = previous.parser_version,
                    created_at = previous.created_at
                FROM matching_pages AS previous
                WHERE current.material_id = ?
                  AND current.page_no = previous.page_no AND previous.preference = 1
                  AND (current.parser_version = ? OR (
                      current.parser_version IN (?, ?)
                      AND (current.scanned_suspect OR current.garbled_ratio > 0.15)
                  ))
                """,
                (
                    material_id, self._owner_id, start_page, end_page,
                    OCR_PAGE_VERSION, OCR_EXCLUDED_PREFIX + "%", material_id,
                    OCR_FAILED_VERSION, PARSER_VERSION, PDF_TEXT_CACHE_VERSION,
                ),
            )
            return int(conn.execute("SELECT changes()").fetchone()[0])

    def get_cached_pages(
        self, material_id: str, start_page: int, end_page: int, *, fresh_pdf_text: bool = False
    ) -> dict[int, ParsedPage]:
        with self._read() as conn:
            rows = conn.execute(
                """
                SELECT p.*
                FROM personal_material_pages p
                JOIN personal_materials m ON m.id = p.material_id
                WHERE p.material_id = ? AND m.owner_id = ? AND m.status = 'active'
                  AND p.page_no BETWEEN ? AND ?
                  AND (NOT ? OR p.parser_version != ?)
                ORDER BY p.page_no ASC
                """,
                (material_id, self._owner_id, start_page, end_page, fresh_pdf_text, PARSER_VERSION),
            ).fetchall()
            pages: dict[int, ParsedPage] = {}
            for r in rows:
                version = str(r["parser_version"])
                fields = dict(
                    page_no=int(r["page_no"]), extracted_text=str(r["extracted_text"]),
                    char_count=int(r["char_count"]), scanned_suspect=bool(r["scanned_suspect"]),
                    garbled_ratio=float(r["garbled_ratio"]), formula_warning=bool(r["formula_warning"]),
                )
                if version == OCR_PAGE_VERSION:
                    page = OcrPage(**fields)
                elif version == OCR_FAILED_VERSION:
                    page = FailedOcrPage(**fields)
                elif version.startswith(OCR_EXCLUDED_PREFIX):
                    page = ExcludedOcrPage(**fields, kind=version.removeprefix(OCR_EXCLUDED_PREFIX))
                else:
                    page = ParsedPage(**fields)
                pages[page.page_no] = page
            return pages

    def get_page_view(self, material_id: str, page_no: int) -> MaterialPageView | None:
        with self._read() as conn:
            row = conn.execute(
                """
                SELECT p.*
                FROM personal_material_pages p
                JOIN personal_materials m ON m.id = p.material_id
                WHERE p.material_id = ? AND m.owner_id = ? AND m.status = 'active'
                  AND p.page_no = ?
                """,
                (material_id, self._owner_id, page_no),
            ).fetchone()
            if row is None:
                return None
            warnings: list[str] = []
            is_ocr = row["parser_version"] == OCR_PAGE_VERSION
            status = page_status(
                page_no=page_no, version=str(row["parser_version"]), scanned=bool(row["scanned_suspect"]),
                garbled=float(row["garbled_ratio"]), formula=bool(row["formula_warning"]),
            )
            if status.status in ("ocr_failed", "excluded") and status.reason:
                warnings.append(status.reason)
            if is_ocr:
                warnings.append("本页文字由 OCR 转写，题干和公式请对照原页。")
            if row["scanned_suspect"] and status.status != "excluded":
                warnings.append("疑似扫描页或空白页（可提取文字过少）")
            if float(row["garbled_ratio"]) > 0.15:
                warnings.append("乱码字符比例较高")
            if row["formula_warning"]:
                warnings.append("部分公式或特殊符号可能提取不完整")
            return MaterialPageView(
                material_id=material_id,
                page_no=int(row["page_no"]),
                source=status.source or "pdf_text",
                char_count=int(row["char_count"]),
                scanned_suspect=bool(row["scanned_suspect"]),
                garbled_ratio=float(row["garbled_ratio"]),
                formula_warning=bool(row["formula_warning"]),
                warnings=warnings,
                extracted_text=str(row["extracted_text"]),
            )

    def list_guides_for_material(self, material_id: str) -> list[GuideSummary]:
        with self._read() as conn:
            rows = conn.execute(
                """
                SELECT g.*
                FROM material_review_guides g
                JOIN personal_materials m ON m.id = g.material_id
                WHERE g.material_id = ? AND g.owner_id = ? AND m.status = 'active'
                ORDER BY g.created_at DESC, g.id ASC
                """,
                (material_id, self._owner_id),
            ).fetchall()
            return [
                GuideSummary(
                    id=str(r["id"]),
                    material_id=str(r["material_id"]),
                    start_page=int(r["start_page"]),
                    end_page=int(r["end_page"]),
                    rule_version=str(r["rule_version"]),
                    conversation_id=r["conversation_id"],
                    status=r["status"],
                    error_code=r["error_code"],
                    error_message=r["error_message"],
                    created_at=_parse_time(str(r["created_at"])),
                    updated_at=_parse_time(str(r["updated_at"])),
                )
                for r in rows
            ]

    def acquire_guide_generation(
        self,
        *,
        material_id: str,
        file_sha256: str,
        start_page: int,
        end_page: int,
        rule_version: str,
        course_hint: str | None = None,
        readability: ReadabilityReport,
    ) -> GuideAcquireResult:
        """并发抢占生成权；ready 直接复用，generating 去重，failed 允许重试。"""

        now = _now()
        token = str(uuid.uuid4())
        norm_hint = (course_hint or "").strip()
        readability_json = readability.model_dump_json()

        with self._write() as conn:
            mat = conn.execute(
                "SELECT status, dedicated_conversation_id FROM personal_materials WHERE id = ? AND owner_id = ?",
                (material_id, self._owner_id),
            ).fetchone()
            if mat is None or mat["status"] != "active":
                raise MaterialNotFoundError("个人资料不存在或正在删除。")

            existing = conn.execute(
                """
                SELECT id, status, conversation_id
                FROM material_review_guides
                WHERE owner_id = ? AND material_id = ? AND file_sha256 = ?
                  AND start_page = ? AND end_page = ? AND rule_version = ?
                  AND course_hint = ?
                """,
                (
                    self._owner_id,
                    material_id,
                    file_sha256,
                    start_page,
                    end_page,
                    rule_version,
                    norm_hint,
                ),
            ).fetchone()

            if existing is not None:
                status = str(existing["status"])
                guide_id = str(existing["id"])
                conv_id = existing["conversation_id"]
                if status == "ready":
                    return GuideAcquireResult(
                        guide_id=guide_id,
                        status="ready",
                        reused=True,
                        generation_token=None,
                        conversation_id=conv_id,
                    )
                if status == "generating":
                    return GuideAcquireResult(
                        guide_id=guide_id,
                        status="generating",
                        reused=True,
                        generation_token=None,
                        conversation_id=conv_id,
                    )
                # status == "failed": 原地原子恢复为 generating
                conn.execute(
                    """
                    UPDATE material_review_guides
                    SET status = 'generating',
                        generation_token = ?,
                        readability_json = ?,
                        error_code = NULL,
                        error_message = NULL,
                        updated_at = ?
                    WHERE id = ? AND status = 'failed'
                    """,
                    (token, readability_json, now, guide_id),
                )
                return GuideAcquireResult(
                    guide_id=guide_id,
                    status="generating",
                    reused=False,
                    generation_token=token,
                    conversation_id=conv_id,
                )

            guide_id = str(uuid.uuid4())
            conv_id = mat["dedicated_conversation_id"]
            conn.execute(
                """
                INSERT INTO material_review_guides (
                    id, owner_id, material_id, file_sha256, start_page, end_page,
                    rule_version, course_hint, conversation_id, status, generation_token,
                    readability_json, error_code, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'generating', ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    guide_id,
                    self._owner_id,
                    material_id,
                    file_sha256,
                    start_page,
                    end_page,
                    rule_version,
                    norm_hint,
                    conv_id,
                    token,
                    readability_json,
                    now,
                    now,
                ),
            )
            return GuideAcquireResult(
                guide_id=guide_id,
                status="generating",
                reused=False,
                generation_token=token,
                conversation_id=conv_id,
            )

    def complete_guide_generation(
        self,
        *,
        guide_id: str,
        material_id: str,
        generation_token: str,
        conversation_id: str | None,
        items: list[dict[str, object]],
    ) -> bool:
        """写回生成结果；若资料已删/deleting 或 token 不匹配，拒绝迟到写回返回 False。"""

        now = _now()
        with self._write() as conn:
            mat = conn.execute(
                "SELECT status, dedicated_conversation_id FROM personal_materials WHERE id = ? AND owner_id = ?",
                (material_id, self._owner_id),
            ).fetchone()
            if mat is None or mat["status"] != "active":
                return False

            guide = conn.execute(
                "SELECT status, generation_token, conversation_id FROM material_review_guides WHERE id = ? AND owner_id = ?",
                (guide_id, self._owner_id),
            ).fetchone()
            if (
                guide is None
                or guide["status"] != "generating"
                or guide["generation_token"] != generation_token
            ):
                return False

            effective_conv = (
                guide["conversation_id"]
                or mat["dedicated_conversation_id"]
                or conversation_id
            )
            if effective_conv is None:
                effective_conv = str(uuid.uuid4())
                display_row = conn.execute(
                    "SELECT display_name FROM personal_materials WHERE id = ?",
                    (material_id,),
                ).fetchone()
                title = (
                    f"资料复习：{display_row['display_name']}"[:30]
                    if display_row is not None
                    else "个人资料复习"
                )
                conn.execute(
                    """
                    INSERT INTO conversations (id, title, created_at, updated_at, owner_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (effective_conv, title, now, now, self._owner_id),
                )
            if mat["dedicated_conversation_id"] is None:
                conn.execute(
                    "UPDATE personal_materials SET dedicated_conversation_id = ?, updated_at = ? WHERE id = ?",
                    (effective_conv, now, material_id),
                )

            conn.execute("DELETE FROM material_guide_items WHERE guide_id = ?", (guide_id,))
            for idx, item in enumerate(items):
                conn.execute(
                    """
                    INSERT INTO material_guide_items (
                        id, guide_id, owner_id, sort_order, category, title,
                        source_excerpt, ai_explanation, practice_prompt,
                        verified_citations_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        guide_id,
                        self._owner_id,
                        idx,
                        str(item["category"]),
                        str(item["title"]),
                        str(item["source_excerpt"]),
                        str(item["ai_explanation"]),
                        str(item["practice_prompt"]),
                        json.dumps(item["verified_citations"], ensure_ascii=False),
                        now,
                    ),
                )

            conn.execute(
                """
                UPDATE material_review_guides
                SET status = 'ready',
                    conversation_id = ?,
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?
                WHERE id = ? AND generation_token = ?
                """,
                (effective_conv, now, guide_id, generation_token),
            )
            return True

    def fail_guide_generation(
        self,
        *,
        guide_id: str,
        generation_token: str,
        error_code: str,
        error_message: str,
    ) -> None:
        now = _now()
        with self._write() as conn:
            conn.execute(
                """
                UPDATE material_review_guides
                SET status = 'failed',
                    error_code = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ? AND owner_id = ? AND status = 'generating' AND generation_token = ?
                """,
                (error_code, error_message, now, guide_id, self._owner_id, generation_token),
            )

    def get_guide(self, guide_id: str, *, reused: bool = False) -> ReviewGuideView:
        with self._read() as conn:
            row = conn.execute(
                """
                SELECT g.*
                FROM material_review_guides g
                JOIN personal_materials m ON m.id = g.material_id
                WHERE g.id = ? AND g.owner_id = ? AND m.status = 'active'
                """,
                (guide_id, self._owner_id),
            ).fetchone()
            if row is None:
                raise GuideNotFoundError("复习清单不存在。")
            item_rows = conn.execute(
                """
                SELECT * FROM material_guide_items
                WHERE guide_id = ? AND owner_id = ?
                ORDER BY sort_order ASC
                """,
                (guide_id, self._owner_id),
            ).fetchall()

        readability = (
            ReadabilityReport.model_validate_json(str(row["readability_json"]))
            if row["readability_json"]
            else None
        )
        items: list[GuideItemView] = []
        for ir in item_rows:
            raw_cits = json.loads(str(ir["verified_citations_json"] or "[]"))
            citations = [VerifiedCitation.model_validate(c) for c in raw_cits]
            items.append(
                GuideItemView(
                    id=str(ir["id"]),
                    sort_order=int(ir["sort_order"]),
                    category=ir["category"],
                    title=str(ir["title"]),
                    source_excerpt=str(ir["source_excerpt"]),
                    ai_explanation=str(ir["ai_explanation"]),
                    practice_prompt=str(ir["practice_prompt"]),
                    verified_citations=citations,
                )
            )
        return ReviewGuideView(
            id=str(row["id"]),
            material_id=str(row["material_id"]),
            file_sha256=str(row["file_sha256"]),
            start_page=int(row["start_page"]),
            end_page=int(row["end_page"]),
            rule_version=str(row["rule_version"]),
            conversation_id=row["conversation_id"],
            status=row["status"],
            reused=reused,
            readability=readability,
            items=items,
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=_parse_time(str(row["created_at"])),
            updated_at=_parse_time(str(row["updated_at"])),
        )

    def get_guide_item_context(
        self, guide_id: str, item_id: str
    ) -> tuple[ReviewGuideView, GuideItemView, str]:
        guide = self.get_guide(guide_id)
        for item in guide.items:
            if item.id == item_id:
                mat = self.get_material(guide.material_id)
                return guide, item, mat.display_name
        raise GuideNotFoundError("复习清单条目不存在。")

    def get_delete_impact(self, material_id: str) -> DeleteImpactView:
        with self._read() as conn:
            mat = conn.execute(
                """
                SELECT id, display_name, dedicated_conversation_id
                FROM personal_materials
                WHERE id = ? AND owner_id = ? AND status IN ('active', 'deleting')
                """,
                (material_id, self._owner_id),
            ).fetchone()
            if mat is None:
                raise MaterialNotFoundError("个人资料不存在。")

            guide_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM material_review_guides WHERE material_id = ? AND owner_id = ?",
                    (material_id, self._owner_id),
                ).fetchone()[0]
            )
            artifact_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM material_artifacts WHERE material_id = ? AND owner_id = ?",
                    (material_id, self._owner_id),
                ).fetchone()[0]
            )
            quiz_attempt_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM material_quiz_attempts a
                    JOIN material_artifacts art ON art.id = a.artifact_id
                    WHERE art.material_id = ? AND a.owner_id = ?
                    """,
                    (material_id, self._owner_id),
                ).fetchone()[0]
            )
            graph_note_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM material_graph_notes WHERE material_id = ? AND owner_id = ?",
                    (material_id, self._owner_id),
                ).fetchone()[0]
            )
            conv_id = mat["dedicated_conversation_id"]
            linked_rows = conn.execute(
                """
                SELECT conversation_id FROM conversation_materials
                WHERE material_id = ? AND owner_id = ?
                """,
                (material_id, self._owner_id),
            ).fetchall()
            regular_conv_ids = [
                str(r["conversation_id"])
                for r in linked_rows
                if str(r["conversation_id"]) != conv_id
            ]
            exercise_count = 0
            mistake_count = 0
            attachment_count = 0
            if conv_id:
                exercise_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM exercises WHERE conversation_id = ?",
                        (conv_id,),
                    ).fetchone()[0]
                )
                mistake_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM mistake_records WHERE conversation_id = ?",
                        (conv_id,),
                    ).fetchone()[0]
                )
                attachment_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM attachments WHERE conversation_id = ?",
                        (conv_id,),
                    ).fetchone()[0]
                )
            return DeleteImpactView(
                material_id=material_id,
                display_name=str(mat["display_name"]),
                guide_count=guide_count,
                artifact_count=artifact_count,
                quiz_attempt_count=quiz_attempt_count,
                graph_note_count=graph_note_count,
                conversation_id=conv_id,
                linked_conversation_count=len(regular_conv_ids),
                preserves_regular_conversations=True,
                exercise_count=exercise_count,
                mistake_count=mistake_count,
                attachment_count=attachment_count,
            )

    def mark_deleting_and_delete(
        self, material_id: str, *, active_guide_ids: set[str] | None = None
    ) -> tuple[str, str | None, list[str]]:
        """原子校验无活跃任务、标删、登记待清理文件并删除资料与专用会话。"""

        now = _now()
        with self._write() as conn:
            mat = conn.execute(
                """
                SELECT stored_name, status, dedicated_conversation_id
                FROM personal_materials
                WHERE id = ? AND owner_id = ?
                """,
                (material_id, self._owner_id),
            ).fetchone()
            if mat is None:
                raise MaterialNotFoundError("个人资料不存在。")

            conv_id = mat["dedicated_conversation_id"]
            generating_rows = conn.execute(
                "SELECT id FROM material_review_guides WHERE material_id = ? AND status = 'generating'",
                (material_id,),
            ).fetchall()
            generating_art_rows = conn.execute(
                "SELECT id FROM material_artifacts WHERE material_id = ? AND status = 'generating'",
                (material_id,),
            ).fetchall()
            if active_guide_ids is not None:
                for gr in [*generating_rows, *generating_art_rows]:
                    if str(gr["id"]) in active_guide_ids:
                        raise MaterialConflictError("当前资料正在生成复习资料，请稍候完成后再删除。")

            if conv_id:
                running_turn = conn.execute(
                    "SELECT 1 FROM conversation_turns WHERE conversation_id = ? AND status = 'running' LIMIT 1",
                    (conv_id,),
                ).fetchone()
                active_lease = conn.execute(
                    "SELECT 1 FROM user_active_turns WHERE owner_id = ? AND conversation_id = ? LIMIT 1",
                    (self._owner_id, conv_id),
                ).fetchone()
                if running_turn is not None or active_lease is not None:
                    raise MaterialConflictError("关联学习会话正在处理中，请稍候完成后再删除。")

            # 阶段 A：先标为 deleting，阻断任何并发读取或迟到写回
            conn.execute(
                "UPDATE personal_materials SET status = 'deleting', updated_at = ? WHERE id = ?",
                (now, material_id),
            )

            attachment_names: list[str] = []
            if conv_id:
                att_rows = conn.execute(
                    "SELECT stored_name FROM attachments WHERE conversation_id = ?",
                    (conv_id,),
                ).fetchall()
                attachment_names = [str(r["stored_name"]) for r in att_rows]

            pdf_stored_name = str(mat["stored_name"])

            # 在同一事务中记录待清理文件，避免磁盘文件被锁导致孤儿文件无法重试回收
            conn.execute(
                """
                INSERT INTO pending_file_cleanups (
                    id, root_kind, stored_name, attempts, last_error, created_at
                ) VALUES (?, 'material', ?, 0, NULL, ?)
                """,
                (str(uuid.uuid4()), pdf_stored_name, now),
            )
            for att_name in attachment_names:
                conn.execute(
                    """
                    INSERT INTO pending_file_cleanups (
                        id, root_kind, stored_name, attempts, last_error, created_at
                    ) VALUES (?, 'attachment', ?, 0, NULL, ?)
                    """,
                    (str(uuid.uuid4()), att_name, now),
                )

            # 阶段 B：删除资料（级联页缓存、清单、清单项、conversation_materials 关联）；
            # 仅清理纯粹用于该资料的专用会话（如果专用会话内挂载过其它资料，则也予以保留）。
            delete_dedicated = False
            if conv_id:
                other_linked = conn.execute(
                    """
                    SELECT 1 FROM conversation_materials
                    WHERE conversation_id = ? AND owner_id = ? AND material_id != ?
                    LIMIT 1
                    """,
                    (conv_id, self._owner_id, material_id),
                ).fetchone()
                delete_dedicated = other_linked is None
            if not delete_dedicated:
                attachment_names = []
                # 重新清除刚才可能多登记的附件清理记录，保留该会话的附件
                if conv_id:
                    conn.execute(
                        "DELETE FROM pending_file_cleanups WHERE root_kind = 'attachment' AND stored_name IN (SELECT stored_name FROM attachments WHERE conversation_id = ?)",
                        (conv_id,),
                    )

            conn.execute(
                "DELETE FROM personal_materials WHERE id = ? AND owner_id = ?",
                (material_id, self._owner_id),
            )
            if conv_id and delete_dedicated:
                conn.execute(
                    "DELETE FROM conversations WHERE id = ? AND owner_id = ?",
                    (conv_id, self._owner_id),
                )
            return pdf_stored_name, (conv_id if delete_dedicated else None), attachment_names

    def list_pending_cleanups(self) -> list[tuple[str, str, str]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT id, root_kind, stored_name FROM pending_file_cleanups ORDER BY created_at ASC"
            ).fetchall()
            return [
                (str(r["id"]), str(r["root_kind"]), str(r["stored_name"]))
                for r in rows
            ]

    def mark_cleanup_succeeded(self, cleanup_id: str) -> None:
        with self._write() as conn:
            conn.execute(
                "DELETE FROM pending_file_cleanups WHERE id = ?",
                (cleanup_id,),
            )

    def record_cleanup_failure(self, cleanup_id: str, error: str) -> None:
        with self._write() as conn:
            conn.execute(
                """
                UPDATE pending_file_cleanups
                SET attempts = attempts + 1,
                    last_error = ?
                WHERE id = ?
                """,
                (error[:300], cleanup_id),
            )

    # -----------------------------------------------------------------------
    # F019 第二阶段新版：ExamPass 复习产物、测试题、作答与图谱笔记
    # -----------------------------------------------------------------------

    @staticmethod
    def _row_to_artifact_summary(r: sqlite3.Row) -> ArtifactSummary:
        keys = set(r.keys())
        requested = (
            _parse_requested_outputs(r["requested_outputs_json"])
            if "requested_outputs_json" in keys
            else list(_VALID_OUTPUTS)
        )
        return ArtifactSummary(
            id=str(r["id"]),
            material_id=str(r["material_id"]),
            file_sha256=str(r["file_sha256"]),
            start_page=int(r["start_page"]),
            end_page=int(r["end_page"]),
            rule_version=str(r["rule_version"]),
            coverage=_read_artifact_coverage(r["coverage_warnings_json"])[1],
            course_hint=str(r["course_hint"]),
            title=str(r["title"]),
            is_sample=bool(r["is_sample"]),
            status=str(r["status"]),  # type: ignore[arg-type]
            current_step=str(r["current_step"]),
            requested_outputs=requested,
            notes_status=str(r["notes_status"]) if "notes_status" in keys else "none",  # type: ignore[arg-type]
            notes_error_code=str(r["notes_error_code"]) if "notes_error_code" in keys and r["notes_error_code"] else None,
            notes_error_message=str(r["notes_error_message"]) if "notes_error_message" in keys and r["notes_error_message"] else None,
            quiz_status=str(r["quiz_status"]) if "quiz_status" in keys else "none",  # type: ignore[arg-type]
            quiz_error_code=str(r["quiz_error_code"]) if "quiz_error_code" in keys and r["quiz_error_code"] else None,
            quiz_error_message=str(r["quiz_error_message"]) if "quiz_error_message" in keys and r["quiz_error_message"] else None,
            graph_status=str(r["graph_status"]) if "graph_status" in keys else "none",  # type: ignore[arg-type]
            graph_error_code=str(r["graph_error_code"]) if "graph_error_code" in keys and r["graph_error_code"] else None,
            graph_error_message=str(r["graph_error_message"]) if "graph_error_message" in keys and r["graph_error_message"] else None,
            conversation_id=str(r["conversation_id"]) if r["conversation_id"] else None,
            question_count=int(r["question_count"]),
            error_code=str(r["error_code"]) if r["error_code"] else None,
            error_message=str(r["error_message"]) if r["error_message"] else None,
            created_at=_parse_time(str(r["created_at"])),
            updated_at=_parse_time(str(r["updated_at"])),
        )

    def list_artifacts_for_material(self, material_id: str) -> list[ArtifactSummary]:
        with self._read() as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.material_id, a.file_sha256, a.start_page, a.end_page,
                       a.rule_version, a.course_hint, a.title, a.is_sample, a.status,
                       a.current_step, a.requested_outputs_json,
                       a.notes_status, a.notes_error_code, a.notes_error_message,
                       a.quiz_status, a.quiz_error_code, a.quiz_error_message,
                       a.graph_status, a.graph_error_code, a.graph_error_message,
                       a.conversation_id, a.error_code, a.error_message,
                       a.created_at, a.updated_at, a.coverage_warnings_json,
                       (SELECT COUNT(*) FROM material_quiz_questions q WHERE q.artifact_id = a.id) AS question_count
                FROM material_artifacts a
                JOIN personal_materials m ON m.id = a.material_id
                WHERE a.material_id = ? AND a.owner_id = ? AND m.status = 'active'
                ORDER BY a.updated_at DESC
                """,
                (material_id, self._owner_id),
            ).fetchall()
            return [self._row_to_artifact_summary(r) for r in rows]

    def _link_conversation_material(
        self,
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        material_id: str,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO conversation_materials (
                id, owner_id, conversation_id, material_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), self._owner_id, conversation_id, material_id, now),
        )

    def _ensure_dedicated_conversation(
        self,
        conn: sqlite3.Connection,
        *,
        material_id: str,
        display_name: str,
        existing_conv_id: str | None,
        now: str,
    ) -> str:
        if existing_conv_id:
            return existing_conv_id
        conv_id = str(uuid.uuid4())
        conv_title = f"资料复习：{display_name}"[:30]
        conn.execute(
            """
            INSERT INTO conversations (id, title, created_at, updated_at, owner_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (conv_id, conv_title, now, now, self._owner_id),
        )
        conn.execute(
            "UPDATE personal_materials SET dedicated_conversation_id = ?, updated_at = ? WHERE id = ?",
            (conv_id, now, material_id),
        )
        return conv_id

    def upsert_ready_artifact(
        self,
        *,
        material_id: str,
        file_sha256: str,
        start_page: int,
        end_page: int,
        rule_version: str,
        course_hint: str,
        title: str,
        is_sample: bool,
        skeleton: dict[str, object],
        notes_html: str,
        graph_tree: dict[str, object],
        coverage_warnings: list[str],
        questions: list[dict[str, object]],
    ) -> tuple[str, bool]:
        """写入或复用已完成的复习产物，返回 (artifact_id, reused)。"""

        notes_html, anchor_warnings = ensure_note_anchors(notes_html, skeleton)
        coverage_warnings = list(dict.fromkeys([*coverage_warnings, *anchor_warnings]))
        now = _now()
        with self._write() as conn:
            mat = conn.execute(
                """
                SELECT id, dedicated_conversation_id
                FROM personal_materials
                WHERE id = ? AND owner_id = ? AND status = 'active'
                """,
                (material_id, self._owner_id),
            ).fetchone()
            if mat is None:
                raise MaterialNotFoundError("个人资料不存在。")

            existing = conn.execute(
                """
                SELECT id, status FROM material_artifacts
                WHERE owner_id = ? AND material_id = ? AND file_sha256 = ?
                  AND start_page = ? AND end_page = ? AND rule_version = ? AND course_hint = ?
                """,
                (
                    self._owner_id,
                    material_id,
                    file_sha256,
                    start_page,
                    end_page,
                    rule_version,
                    course_hint,
                ),
            ).fetchone()
            if existing is not None and str(existing["status"]) == "ready":
                return str(existing["id"]), True

            conv_id = self._ensure_dedicated_conversation(
                conn,
                material_id=material_id,
                display_name=title or "个人资料学习",
                existing_conv_id=str(mat["dedicated_conversation_id"]) if mat["dedicated_conversation_id"] else None,
                now=now,
            )

            artifact_id = str(existing["id"]) if existing is not None else str(uuid.uuid4())
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO material_artifacts (
                        id, owner_id, material_id, file_sha256, start_page, end_page,
                        rule_version, course_hint, title, is_sample, status, current_step,
                        generation_token, conversation_id, skeleton_json, notes_html,
                        graph_tree_json, coverage_warnings_json, error_code, error_message,
                        created_at, updated_at,
                        requested_outputs_json, notes_status, quiz_status, graph_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', 'completed', NULL, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, '["notes","quiz","graph"]', 'ready', 'ready', 'ready')
                    """,
                    (
                        artifact_id,
                        self._owner_id,
                        material_id,
                        file_sha256,
                        start_page,
                        end_page,
                        rule_version,
                        course_hint,
                        title,
                        1 if is_sample else 0,
                        conv_id,
                        json.dumps(skeleton, ensure_ascii=False),
                        notes_html,
                        json.dumps(graph_tree, ensure_ascii=False),
                        json.dumps(coverage_warnings, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM material_quiz_questions WHERE artifact_id = ?",
                    (artifact_id,),
                )
                conn.execute(
                    """
                    UPDATE material_artifacts
                    SET title = ?, is_sample = ?, status = 'ready', current_step = 'completed',
                        generation_token = NULL, conversation_id = ?, skeleton_json = ?,
                        notes_html = ?, graph_tree_json = ?, coverage_warnings_json = ?,
                        error_code = NULL, error_message = NULL,
                        requested_outputs_json = '["notes","quiz","graph"]',
                        notes_status = 'ready', notes_error_code = NULL, notes_error_message = NULL,
                        quiz_status = 'ready', quiz_error_code = NULL, quiz_error_message = NULL,
                        graph_status = 'ready', graph_error_code = NULL, graph_error_message = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        title,
                        1 if is_sample else 0,
                        conv_id,
                        json.dumps(skeleton, ensure_ascii=False),
                        notes_html,
                        json.dumps(graph_tree, ensure_ascii=False),
                        json.dumps(coverage_warnings, ensure_ascii=False),
                        now,
                        artifact_id,
                    ),
                )
            self._link_conversation_material(
                conn, conversation_id=conv_id, material_id=material_id, now=now
            )

            for q in questions:
                conn.execute(
                    """
                    INSERT INTO material_quiz_questions (
                        id, artifact_id, owner_id, sort_order, kc_id, q_type, stem,
                        options_json, answer_json, explanation, pitfall, cognitive,
                        difficulty, points, source_pages_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        artifact_id,
                        self._owner_id,
                        int(q["sort_order"]),
                        str(q.get("kc_id") or ""),
                        str(q["q_type"]),
                        str(q["stem"]),
                        json.dumps(q["options"], ensure_ascii=False) if q.get("options") is not None else None,
                        json.dumps(q["answer"], ensure_ascii=False),
                        str(q.get("explanation") or ""),
                        str(q.get("pitfall") or ""),
                        str(q.get("cognitive") or ""),
                        str(q.get("difficulty") or ""),
                        float(q.get("points") or 5),
                        json.dumps(q.get("source_pages") or [], ensure_ascii=False),
                        now,
                    ),
                )
            return artifact_id, False

    def get_artifact_detail(self, artifact_id: str, *, reused: bool = False) -> ArtifactDetailView:
        with self._read() as conn:
            art = conn.execute(
                """
                SELECT a.*
                FROM material_artifacts a
                JOIN personal_materials m ON m.id = a.material_id
                WHERE a.id = ? AND a.owner_id = ? AND m.status = 'active'
                """,
                (artifact_id, self._owner_id),
            ).fetchone()
            if art is None:
                raise GuideNotFoundError("复习产物不存在。")

            q_rows = conn.execute(
                """
                SELECT * FROM material_quiz_questions
                WHERE artifact_id = ? AND owner_id = ?
                ORDER BY sort_order ASC
                """,
                (artifact_id, self._owner_id),
            ).fetchall()

            att_rows = conn.execute(
                """
                SELECT * FROM material_quiz_attempts
                WHERE artifact_id = ? AND owner_id = ?
                """,
                (artifact_id, self._owner_id),
            ).fetchall()
            attempts_by_qid = {str(r["question_id"]): r for r in att_rows}

            questions: list[QuizQuestionPublicView] = []
            for qr in q_rows:
                qid = str(qr["id"])
                ar = attempts_by_qid.get(qid)
                attempt_view: QuizAttemptView | None = None
                can_reveal = False
                if ar is not None:
                    grading_status = str(ar["grading_status"])
                    revealed_at = _parse_time(str(ar["revealed_solution_at"])) if ar["revealed_solution_at"] else None
                    eval_obj = json.loads(str(ar["evaluation_json"])) if ar["evaluation_json"] else {}
                    first_attempt = eval_obj.get("first_attempt") if isinstance(eval_obj.get("first_attempt"), dict) else None
                    attempt_view = QuizAttemptView(
                        question_id=qid,
                        user_answer=json.loads(str(ar["user_answer_json"])) if ar["user_answer_json"] is not None else None,
                        is_correct=(bool(ar["is_correct"]) if ar["is_correct"] is not None else None),
                        score=(float(ar["score"]) if ar["score"] is not None else None),
                        max_points=float(ar["max_points"]),
                        grading_status=grading_status,  # type: ignore[arg-type]
                        feedback=str(eval_obj.get("feedback")) if eval_obj.get("feedback") else None,
                        revealed_solution_at=revealed_at,
                        answered_after_revealed=bool(ar["answered_after_revealed"]),
                        first_is_correct=(
                            bool(first_attempt["is_correct"])
                            if first_attempt and first_attempt.get("is_correct") is not None
                            else None
                        ),
                        first_score=(
                            float(first_attempt["score"])
                            if first_attempt and first_attempt.get("score") is not None
                            else None
                        ),
                        first_grading_status=(
                            str(first_attempt["grading_status"])  # type: ignore[arg-type]
                            if first_attempt and first_attempt.get("grading_status")
                            else None
                        ),
                        updated_at=_parse_time(str(ar["updated_at"])),
                    )
                    can_reveal = (
                        revealed_at is not None
                        or grading_status in ("auto_graded", "evaluated")
                    )

                questions.append(
                    QuizQuestionPublicView(
                        id=qid,
                        sort_order=int(qr["sort_order"]),
                        kc_id=str(qr["kc_id"]),
                        q_type=str(qr["q_type"]),  # type: ignore[arg-type]
                        stem=str(qr["stem"]),
                        options=json.loads(str(qr["options_json"])) if qr["options_json"] else None,
                        pitfall=str(qr["pitfall"]),
                        cognitive=str(qr["cognitive"]),
                        difficulty=str(qr["difficulty"]),
                        points=float(qr["points"]),
                        source_pages=json.loads(str(qr["source_pages_json"])) if qr["source_pages_json"] else [],
                        attempt=attempt_view,
                        revealed_answer=json.loads(str(qr["answer_json"])) if can_reveal else None,
                        revealed_explanation=str(qr["explanation"]) if can_reveal else None,
                    )
                )

            note_rows = conn.execute(
                """
                SELECT node_id, note_text FROM material_graph_notes
                WHERE artifact_id = ? AND owner_id = ?
                ORDER BY updated_at, node_id
                """,
                (artifact_id, self._owner_id),
            ).fetchall()
            saved_notes = {str(r["node_id"]): str(r["note_text"]) for r in note_rows}
            warnings, coverage = _read_artifact_coverage(art["coverage_warnings_json"])
            skeleton = json.loads(str(art["skeleton_json"])) if art["skeleton_json"] else None
            notes_html = str(art["notes_html"] or "")
            tree = json.loads(str(art["graph_tree_json"])) if art["graph_tree_json"] else None
            kc_index, graph_warnings = build_kc_index(
                skeleton, notes_html, tree, questions, int(art["start_page"]), int(art["end_page"])
            )
            if art["graph_status"] != "ready":
                for point in kc_index.values():
                    point.graph_node_id = None
                graph_warnings = []
            node_to_kc = {
                str(node["id"]): node["kc_id"] for node in graph_nodes(tree)
                if node.get("id") and node.get("kc_id") in kc_index
            }
            kc_notes = {node_to_kc[node]: text for node, text in saved_notes.items() if node in node_to_kc}
            kc_notes.update({kc: saved_notes[f"kc:{kc}"] for kc in kc_index if f"kc:{kc}" in saved_notes})
            graph_notes = {node: text for node, text in saved_notes.items() if not node.startswith("kc:") and text}
            for node, kc in node_to_kc.items():
                if kc in kc_notes:
                    if kc_notes[kc]:
                        graph_notes[node] = kc_notes[kc]
                    else:
                        graph_notes.pop(node, None)
            return ArtifactDetailView(
                coverage=coverage,
                output_coverage=describe_outputs(
                    coverage, skeleton, notes_html, questions,
                    str(art["notes_status"]), str(art["quiz_status"]), str(art["graph_status"]),
                ),
                id=str(art["id"]),
                material_id=str(art["material_id"]),
                file_sha256=str(art["file_sha256"]),
                start_page=int(art["start_page"]),
                end_page=int(art["end_page"]),
                rule_version=str(art["rule_version"]),
                course_hint=str(art["course_hint"]),
                title=str(art["title"]),
                is_sample=bool(art["is_sample"]),
                status=str(art["status"]),  # type: ignore[arg-type]
                current_step=str(art["current_step"]),
                requested_outputs=_parse_requested_outputs(art["requested_outputs_json"]),
                notes_status=str(art["notes_status"]),  # type: ignore[arg-type]
                notes_error_code=str(art["notes_error_code"]) if art["notes_error_code"] else None,
                notes_error_message=str(art["notes_error_message"]) if art["notes_error_message"] else None,
                quiz_status=str(art["quiz_status"]),  # type: ignore[arg-type]
                quiz_error_code=str(art["quiz_error_code"]) if art["quiz_error_code"] else None,
                quiz_error_message=str(art["quiz_error_message"]) if art["quiz_error_message"] else None,
                graph_status=str(art["graph_status"]),  # type: ignore[arg-type]
                graph_error_code=str(art["graph_error_code"]) if art["graph_error_code"] else None,
                graph_error_message=str(art["graph_error_message"]) if art["graph_error_message"] else None,
                reused=reused,
                conversation_id=str(art["conversation_id"]) if art["conversation_id"] else None,
                skeleton=skeleton,
                notes_html=notes_html,
                graph_tree=tree,
                coverage_warnings=list(dict.fromkeys([*warnings, *graph_warnings])),
                questions=questions,
                graph_notes=graph_notes,
                kc_index=kc_index,
                kc_notes=kc_notes,
                error_code=str(art["error_code"]) if art["error_code"] else None,
                error_message=str(art["error_message"]) if art["error_message"] else None,
                created_at=_parse_time(str(art["created_at"])),
                updated_at=_parse_time(str(art["updated_at"])),
            )

    def get_quiz_question_row(self, artifact_id: str, question_id: str) -> dict[str, object]:
        with self._read() as conn:
            row = conn.execute(
                """
                SELECT q.*, a.status AS artifact_status, a.quiz_status AS quiz_status
                FROM material_quiz_questions q
                JOIN material_artifacts a ON a.id = q.artifact_id
                JOIN personal_materials m ON m.id = a.material_id
                WHERE q.id = ? AND q.artifact_id = ? AND q.owner_id = ? AND m.status = 'active'
                """,
                (question_id, artifact_id, self._owner_id),
            ).fetchone()
            if row is None:
                raise GuideNotFoundError("测试题目不存在。")
            if str(row["quiz_status"]) != "ready":
                raise MaterialConflictError("复习资料尚未生成完成，请稍候完成后再作答或查看解析。")
            return dict(row)

    def get_quiz_attempt_updated_at(self, question_id: str) -> str | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT updated_at FROM material_quiz_attempts WHERE owner_id = ? AND question_id = ?",
                (self._owner_id, question_id),
            ).fetchone()
            return str(row["updated_at"]) if row is not None else None

    def save_quiz_attempt(
        self,
        *,
        artifact_id: str,
        question_id: str,
        user_answer: object,
        is_correct: bool | None,
        score: float | None,
        max_points: float,
        grading_status: str,
        feedback: str | None,
        expected_updated_at: str | None = None,
    ) -> None:
        now = _now()
        with self._write() as conn:
            existing = conn.execute(
                """
                SELECT id, revealed_solution_at, answered_after_revealed, evaluation_json,
                       grading_status, is_correct, score, created_at, updated_at
                FROM material_quiz_attempts
                WHERE owner_id = ? AND question_id = ?
                """,
                (self._owner_id, question_id),
            ).fetchone()
            if expected_updated_at is not None:
                if existing is None or str(existing["updated_at"]) != expected_updated_at:
                    raise MaterialConflictError(
                        "作答已更新，本次旧评价结果已丢弃，请针对最新作答重新评价。"
                    )
            user_ans_json = json.dumps(user_answer, ensure_ascii=False)
            is_corr_val = (1 if is_correct else 0) if is_correct is not None else None
            # 客观题自动判分或主观题评价完成后即向前端展示答案，因此同步记录首次答案展示时间
            reveals_now = grading_status in ("auto_graded", "evaluated")

            if existing is None:
                first_attempt = {
                    "is_correct": is_correct,
                    "score": score,
                    "grading_status": grading_status,
                    "answered_after_revealed": False,
                    "submitted_at": now,
                }
                eval_obj: dict[str, object] = {"first_attempt": first_attempt}
                if feedback:
                    eval_obj["feedback"] = feedback
                eval_json = json.dumps(eval_obj, ensure_ascii=False)
                revealed_at = now if reveals_now else None
                conn.execute(
                    """
                    INSERT INTO material_quiz_attempts (
                        id, question_id, artifact_id, owner_id, user_answer_json,
                        is_correct, score, max_points, grading_status, evaluation_json,
                        revealed_solution_at, answered_after_revealed, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        question_id,
                        artifact_id,
                        self._owner_id,
                        user_ans_json,
                        is_corr_val,
                        score,
                        max_points,
                        grading_status,
                        eval_json,
                        revealed_at,
                        now,
                        now,
                    ),
                )
            else:
                already_revealed = existing["revealed_solution_at"] is not None
                after_revealed = 1 if (already_revealed or existing["answered_after_revealed"]) else 0
                prev_eval = json.loads(str(existing["evaluation_json"])) if existing["evaluation_json"] else {}
                first_attempt = prev_eval.get("first_attempt")
                if not isinstance(first_attempt, dict) and str(existing["grading_status"]) != "unanswered":
                    # 兼容历史旧记录：用旧行自身的原始状态回填 first_attempt，绝不覆盖为当前新重答结果
                    first_attempt = {
                        "is_correct": (bool(existing["is_correct"]) if existing["is_correct"] is not None else None),
                        "score": (float(existing["score"]) if existing["score"] is not None else None),
                        "grading_status": str(existing["grading_status"]),
                        "answered_after_revealed": bool(existing["answered_after_revealed"]),
                        "submitted_at": str(existing["created_at"]),
                    }
                eval_obj = {}
                if isinstance(first_attempt, dict):
                    eval_obj["first_attempt"] = first_attempt
                elif str(existing["grading_status"]) == "unanswered":
                    eval_obj["first_attempt"] = {
                        "is_correct": is_correct,
                        "score": score,
                        "grading_status": grading_status,
                        "answered_after_revealed": bool(after_revealed),
                        "submitted_at": now,
                    }
                if feedback:
                    eval_obj["feedback"] = feedback
                eval_json = json.dumps(eval_obj, ensure_ascii=False)

                next_revealed_at = (
                    str(existing["revealed_solution_at"])
                    if already_revealed
                    else (now if reveals_now else None)
                )
                conn.execute(
                    """
                    UPDATE material_quiz_attempts
                    SET user_answer_json = ?, is_correct = ?, score = ?,
                        max_points = ?, grading_status = ?, evaluation_json = ?,
                        revealed_solution_at = ?, answered_after_revealed = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        user_ans_json,
                        is_corr_val,
                        score,
                        max_points,
                        grading_status,
                        eval_json,
                        next_revealed_at,
                        after_revealed,
                        now,
                        str(existing["id"]),
                    ),
                )

    def reveal_quiz_solution(self, *, artifact_id: str, question_id: str, max_points: float) -> None:
        now = _now()
        with self._write() as conn:
            existing = conn.execute(
                "SELECT id, revealed_solution_at FROM material_quiz_attempts WHERE owner_id = ? AND question_id = ?",
                (self._owner_id, question_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO material_quiz_attempts (
                        id, question_id, artifact_id, owner_id, user_answer_json,
                        is_correct, score, max_points, grading_status, evaluation_json,
                        revealed_solution_at, answered_after_revealed, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, 'unanswered', NULL, ?, 0, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        question_id,
                        artifact_id,
                        self._owner_id,
                        max_points,
                        now,
                        now,
                        now,
                    ),
                )
            elif existing["revealed_solution_at"] is None:
                conn.execute(
                    "UPDATE material_quiz_attempts SET revealed_solution_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, str(existing["id"])),
                )

    def upsert_graph_note(
        self, *, artifact_id: str, node_id: str, note_text: str, is_knowledge_point: bool = False
    ) -> GraphNoteView:
        now = _now()
        with self._write() as conn:
            art = conn.execute(
                """
                SELECT a.material_id, a.graph_tree_json, a.skeleton_json
                FROM material_artifacts a
                JOIN personal_materials m ON m.id = a.material_id
                WHERE a.id = ? AND a.owner_id = ? AND m.status = 'active'
                """,
                (artifact_id, self._owner_id),
            ).fetchone()
            if art is None:
                raise GuideNotFoundError("复习产物不存在。")
            material_id = str(art["material_id"])
            tree = json.loads(str(art["graph_tree_json"])) if art["graph_tree_json"] else None
            saved_key = node_id
            if is_knowledge_point:
                skeleton = json.loads(str(art["skeleton_json"])) if art["skeleton_json"] else None
                index, _ = build_kc_index(skeleton, "", tree, [], 1, 1)
                if node_id not in index:
                    raise GuideNotFoundError("指定的知识点不存在。")
                saved_key = f"kc:{node_id}"
            else:
                node = next((item for item in graph_nodes(tree) if item.get("id") == node_id), None)
                if node is None:
                    raise GuideNotFoundError("指定的图谱节点不存在。")
                if node.get("kc_id"):
                    saved_key = f"kc:{node['kc_id']}"
            existing = conn.execute(
                "SELECT id FROM material_graph_notes WHERE owner_id = ? AND artifact_id = ? AND node_id = ?",
                (self._owner_id, artifact_id, saved_key),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO material_graph_notes (
                        id, owner_id, material_id, artifact_id, node_id, note_text, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), self._owner_id, material_id, artifact_id, saved_key, note_text, now),
                )
            else:
                conn.execute(
                    "UPDATE material_graph_notes SET note_text = ?, updated_at = ? WHERE id = ?",
                    (note_text, now, str(existing["id"])),
                )
            return GraphNoteView(
                artifact_id=artifact_id,
                node_id=node_id,
                note_text=note_text,
                updated_at=_parse_time(now),
            )

    def acquire_artifact_generation(
        self,
        *,
        material_id: str,
        file_sha256: str,
        start_page: int,
        end_page: int,
        rule_version: str,
        course_hint: str | None,
        default_title: str,
        coverage: ArtifactCoverage | None = None,
        outputs: list[OutputKind] | tuple[OutputKind, ...] | None = None,
        conversation_id: str | None = None,
    ) -> ArtifactAcquireResult:
        now = _now()
        normalized_hint = (course_hint or "").strip()
        target_outputs = _normalize_outputs(outputs)
        token = str(uuid.uuid4())
        with self._write() as conn:
            mat = conn.execute(
                """
                SELECT status, dedicated_conversation_id
                FROM personal_materials
                WHERE id = ? AND owner_id = ?
                """,
                (material_id, self._owner_id),
            ).fetchone()
            if mat is None or mat["status"] != "active":
                raise MaterialNotFoundError("个人资料不存在。")

            if conversation_id is not None:
                conv_row = conn.execute(
                    "SELECT id FROM conversations WHERE id = ? AND owner_id = ?",
                    (conversation_id, self._owner_id),
                ).fetchone()
                if conv_row is None:
                    raise MaterialNotFoundError("学习会话不存在。")
                self._link_conversation_material(
                    conn, conversation_id=conversation_id, material_id=material_id, now=now
                )

            active_same_material = conn.execute(
                """
                SELECT id FROM material_artifacts
                WHERE owner_id = ? AND material_id = ? AND status = 'generating'
                LIMIT 1
                """,
                (self._owner_id, material_id),
            ).fetchone()
            if active_same_material is not None:
                raise MaterialConflictError("该资料已有正在生成的复习任务，请稍候完成后再试。")

            existing = conn.execute(
                """
                SELECT id, status, current_step, conversation_id,
                       skeleton_json, notes_html, graph_tree_json, coverage_warnings_json,
                       requested_outputs_json, notes_status, quiz_status, graph_status
                FROM material_artifacts
                WHERE owner_id = ? AND material_id = ? AND file_sha256 = ?
                  AND start_page = ? AND end_page = ? AND rule_version = ? AND course_hint = ?
                """,
                (
                    self._owner_id,
                    material_id,
                    file_sha256,
                    start_page,
                    end_page,
                    rule_version,
                    normalized_hint,
                ),
            ).fetchone()

            if existing is not None:
                st = str(existing["status"])
                art_id = str(existing["id"])
                if st == "generating":
                    raise MaterialConflictError("该资料正在生成复习资料，请稍候完成后查看。")

                cur_status: dict[OutputKind, OutputStatus] = {
                    "notes": str(existing["notes_status"]),  # type: ignore[dict-item]
                    "quiz": str(existing["quiz_status"]),  # type: ignore[dict-item]
                    "graph": str(existing["graph_status"]),  # type: ignore[dict-item]
                }
                if all(cur_status[o] == "ready" for o in target_outputs):
                    return ArtifactAcquireResult(
                        artifact_id=art_id,
                        status="ready",
                        reused=True,
                        generation_token=None,
                        conversation_id=conversation_id or (str(existing["conversation_id"]) if existing["conversation_id"] else None),
                        current_step="completed",
                        requested_outputs=tuple(_parse_requested_outputs(existing["requested_outputs_json"])),
                        notes_status=cur_status["notes"],
                        quiz_status=cur_status["quiz"],
                        graph_status=cur_status["graph"],
                        existing_skeleton=None,
                        existing_graph_tree=None,
                        existing_notes_html=None,
                        existing_questions=[],
                        existing_coverage_warnings=[],
                    )

                skeleton = json.loads(str(existing["skeleton_json"])) if existing["skeleton_json"] else None
                graph_tree = json.loads(str(existing["graph_tree_json"])) if existing["graph_tree_json"] else None
                notes_html = str(existing["notes_html"]) if existing["notes_html"] else None
                existing_warnings, _ = _read_artifact_coverage(existing["coverage_warnings_json"])
                q_rows = conn.execute(
                    "SELECT * FROM material_quiz_questions WHERE artifact_id = ? ORDER BY sort_order ASC",
                    (art_id,),
                ).fetchall()
                existing_questions: list[dict[str, object]] = [
                    {
                        "sort_order": int(r["sort_order"]),
                        "kc_id": str(r["kc_id"]),
                        "q_type": str(r["q_type"]),
                        "stem": str(r["stem"]),
                        "options": json.loads(str(r["options_json"])) if r["options_json"] else None,
                        "answer": json.loads(str(r["answer_json"])),
                        "explanation": str(r["explanation"]),
                        "pitfall": str(r["pitfall"]),
                        "cognitive": str(r["cognitive"]),
                        "difficulty": str(r["difficulty"]),
                        "points": float(r["points"]),
                        "source_pages": json.loads(str(r["source_pages_json"])) if r["source_pages_json"] else [],
                    }
                    for r in q_rows
                ]

                prev_req = _parse_requested_outputs(existing["requested_outputs_json"])
                merged_req = _normalize_outputs([*prev_req, *target_outputs])
                next_notes_status: OutputStatus = (
                    "ready"
                    if cur_status["notes"] == "ready"
                    else ("generating" if "notes" in target_outputs else cur_status["notes"])
                )
                next_quiz_status: OutputStatus = (
                    "ready"
                    if cur_status["quiz"] == "ready"
                    else ("generating" if "quiz" in target_outputs else cur_status["quiz"])
                )
                next_graph_status: OutputStatus = (
                    "ready"
                    if cur_status["graph"] == "ready"
                    else ("generating" if "graph" in target_outputs else cur_status["graph"])
                )

                next_step = "skeleton"
                if skeleton is not None:
                    if "notes" in target_outputs and next_notes_status != "ready":
                        next_step = "notes"
                    elif "quiz" in target_outputs and next_quiz_status != "ready":
                        next_step = "review" if existing_questions else "quiz"
                    else:
                        next_step = "graph"

                eff_conv_id = str(existing["conversation_id"]) if existing["conversation_id"] else conversation_id
                if eff_conv_id is None:
                    eff_conv_id = self._ensure_dedicated_conversation(
                        conn,
                        material_id=material_id,
                        display_name=default_title,
                        existing_conv_id=str(mat["dedicated_conversation_id"]) if mat["dedicated_conversation_id"] else None,
                        now=now,
                    )
                self._link_conversation_material(
                    conn, conversation_id=eff_conv_id, material_id=material_id, now=now
                )

                conn.execute(
                    """
                    UPDATE material_artifacts
                    SET status = 'generating', current_step = ?, generation_token = ?,
                        conversation_id = COALESCE(conversation_id, ?),
                        requested_outputs_json = ?,
                        notes_status = ?,
                        notes_error_code = CASE WHEN ? = 'generating' THEN NULL ELSE notes_error_code END,
                        notes_error_message = CASE WHEN ? = 'generating' THEN NULL ELSE notes_error_message END,
                        quiz_status = ?,
                        quiz_error_code = CASE WHEN ? = 'generating' THEN NULL ELSE quiz_error_code END,
                        quiz_error_message = CASE WHEN ? = 'generating' THEN NULL ELSE quiz_error_message END,
                        graph_status = ?,
                        graph_error_code = CASE WHEN ? = 'generating' THEN NULL ELSE graph_error_code END,
                        graph_error_message = CASE WHEN ? = 'generating' THEN NULL ELSE graph_error_message END,
                        error_code = NULL, error_message = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_step,
                        token,
                        eff_conv_id,
                        json.dumps(list(merged_req), ensure_ascii=False),
                        next_notes_status,
                        next_notes_status,
                        next_notes_status,
                        next_quiz_status,
                        next_quiz_status,
                        next_quiz_status,
                        next_graph_status,
                        next_graph_status,
                        next_graph_status,
                        now,
                        art_id,
                    ),
                )
                return ArtifactAcquireResult(
                    artifact_id=art_id,
                    status="generating",
                    reused=False,
                    generation_token=token,
                    conversation_id=eff_conv_id,
                    current_step=next_step,
                    requested_outputs=merged_req,
                    notes_status=next_notes_status,
                    quiz_status=next_quiz_status,
                    graph_status=next_graph_status,
                    existing_skeleton=skeleton,
                    existing_graph_tree=graph_tree,
                    existing_notes_html=notes_html,
                    existing_questions=existing_questions,
                    existing_coverage_warnings=existing_warnings,
                )

            if conversation_id is not None:
                conv_id = conversation_id
            else:
                conv_id = self._ensure_dedicated_conversation(
                    conn,
                    material_id=material_id,
                    display_name=default_title,
                    existing_conv_id=str(mat["dedicated_conversation_id"]) if mat["dedicated_conversation_id"] else None,
                    now=now,
                )
            self._link_conversation_material(
                conn, conversation_id=conv_id, material_id=material_id, now=now
            )

            init_notes: OutputStatus = "generating" if "notes" in target_outputs else "none"
            init_quiz: OutputStatus = "generating" if "quiz" in target_outputs else "none"
            init_graph: OutputStatus = "generating" if "graph" in target_outputs else "none"
            art_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO material_artifacts (
                    id, owner_id, material_id, file_sha256, start_page, end_page,
                    rule_version, course_hint, title, is_sample, status, current_step,
                    generation_token, conversation_id,
                    requested_outputs_json, notes_status, quiz_status, graph_status,
                    created_at, updated_at, coverage_warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'generating', 'skeleton', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    art_id,
                    self._owner_id,
                    material_id,
                    file_sha256,
                    start_page,
                    end_page,
                    rule_version,
                    normalized_hint,
                    default_title,
                    token,
                    conv_id,
                    json.dumps(list(target_outputs), ensure_ascii=False),
                    init_notes,
                    init_quiz,
                    init_graph,
                    now,
                    now,
                    _write_artifact_coverage([], coverage),
                ),
            )
            return ArtifactAcquireResult(
                artifact_id=art_id,
                status="generating",
                reused=False,
                generation_token=token,
                conversation_id=conv_id,
                current_step="skeleton",
                requested_outputs=target_outputs,
                notes_status=init_notes,
                quiz_status=init_quiz,
                graph_status=init_graph,
                existing_skeleton=None,
                existing_graph_tree=None,
                existing_notes_html=None,
                existing_questions=[],
                existing_coverage_warnings=[],
            )

    def _is_active_artifact_token(
        self, conn: sqlite3.Connection, *, artifact_id: str, generation_token: str
    ) -> bool:
        row = conn.execute(
            """
            SELECT a.status, a.generation_token, m.status AS material_status
            FROM material_artifacts a
            JOIN personal_materials m ON m.id = a.material_id
            WHERE a.id = ? AND a.owner_id = ?
            """,
            (artifact_id, self._owner_id),
        ).fetchone()
        return (
            row is not None
            and row["material_status"] == "active"
            and row["status"] == "generating"
            and row["generation_token"] == generation_token
        )

    def save_artifact_step_skeleton(
        self,
        *,
        artifact_id: str,
        generation_token: str,
        title: str,
        skeleton: dict[str, object],
        graph_tree: dict[str, object],
        mark_graph_ready: bool = False,
        next_step: str = "notes",
    ) -> bool:
        now = _now()
        with self._write() as conn:
            if not self._is_active_artifact_token(
                conn, artifact_id=artifact_id, generation_token=generation_token
            ):
                return False
            if mark_graph_ready:
                conn.execute(
                    """
                    UPDATE material_artifacts
                    SET title = ?, skeleton_json = ?, graph_tree_json = ?,
                        current_step = ?, graph_status = 'ready',
                        graph_error_code = NULL, graph_error_message = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        title,
                        json.dumps(skeleton, ensure_ascii=False),
                        json.dumps(graph_tree, ensure_ascii=False),
                        next_step,
                        now,
                        artifact_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE material_artifacts
                    SET title = ?, skeleton_json = ?, graph_tree_json = ?,
                        current_step = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        title,
                        json.dumps(skeleton, ensure_ascii=False),
                        json.dumps(graph_tree, ensure_ascii=False),
                        next_step,
                        now,
                        artifact_id,
                    ),
                )
            return True

    def save_artifact_step_notes(
        self,
        *,
        artifact_id: str,
        generation_token: str,
        notes_html: str,
        next_step: str = "quiz",
    ) -> bool:
        now = _now()
        with self._write() as conn:
            if not self._is_active_artifact_token(
                conn, artifact_id=artifact_id, generation_token=generation_token
            ):
                return False
            row = conn.execute(
                "SELECT skeleton_json, coverage_warnings_json FROM material_artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
            skeleton = json.loads(str(row["skeleton_json"])) if row["skeleton_json"] else None
            warnings, coverage = _read_artifact_coverage(row["coverage_warnings_json"])
            notes_html, anchor_warnings = ensure_note_anchors(notes_html, skeleton)
            warnings = list(dict.fromkeys([*(warning for warning in warnings if not warning.startswith(NOTE_WARNING)), *anchor_warnings]))
            conn.execute(
                """
                UPDATE material_artifacts
                SET notes_html = ?, current_step = ?, coverage_warnings_json = ?,
                    notes_status = 'ready', notes_error_code = NULL, notes_error_message = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (notes_html, next_step, _write_artifact_coverage(warnings, coverage), now, artifact_id),
            )
            return True

    def save_artifact_step_quiz(
        self,
        *,
        artifact_id: str,
        generation_token: str,
        questions: list[dict[str, object]],
    ) -> bool:
        now = _now()
        with self._write() as conn:
            if not self._is_active_artifact_token(
                conn, artifact_id=artifact_id, generation_token=generation_token
            ):
                return False
            conn.execute(
                "DELETE FROM material_quiz_questions WHERE artifact_id = ?",
                (artifact_id,),
            )
            for q in questions:
                conn.execute(
                    """
                    INSERT INTO material_quiz_questions (
                        id, artifact_id, owner_id, sort_order, kc_id, q_type, stem,
                        options_json, answer_json, explanation, pitfall, cognitive,
                        difficulty, points, source_pages_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        artifact_id,
                        self._owner_id,
                        int(q["sort_order"]),
                        str(q.get("kc_id") or ""),
                        str(q["q_type"]),
                        str(q["stem"]),
                        json.dumps(q["options"], ensure_ascii=False) if q.get("options") is not None else None,
                        json.dumps(q["answer"], ensure_ascii=False),
                        str(q.get("explanation") or ""),
                        str(q.get("pitfall") or ""),
                        str(q.get("cognitive") or ""),
                        str(q.get("difficulty") or ""),
                        float(q.get("points") or 5),
                        json.dumps(q.get("source_pages") or [], ensure_ascii=False),
                        now,
                    ),
                )
            conn.execute(
                "UPDATE material_artifacts SET current_step = 'review', updated_at = ? WHERE id = ?",
                (now, artifact_id),
            )
            return True

    def complete_artifact_generation(
        self,
        *,
        artifact_id: str,
        generation_token: str,
        coverage_warnings: list[str],
        questions_override: list[dict[str, object]] | None = None,
        mark_outputs_ready: tuple[OutputKind, ...] = ("notes", "quiz", "graph"),
    ) -> bool:
        now = _now()
        with self._write() as conn:
            if not self._is_active_artifact_token(
                conn, artifact_id=artifact_id, generation_token=generation_token
            ):
                return False
            if questions_override is not None:
                existing_rows = conn.execute(
                    "SELECT id, sort_order FROM material_quiz_questions WHERE artifact_id = ? ORDER BY sort_order ASC",
                    (artifact_id,),
                ).fetchall()
                by_order = {int(r["sort_order"]): str(r["id"]) for r in existing_rows}
                if len(existing_rows) == len(questions_override) and all(
                    int(q["sort_order"]) in by_order for q in questions_override
                ):
                    # 就地更新已有题目，保留原 question_id 与已关联引用不变
                    for q in questions_override:
                        qid = by_order[int(q["sort_order"])]
                        conn.execute(
                            """
                            UPDATE material_quiz_questions
                            SET answer_json = ?, explanation = ?, pitfall = ?
                            WHERE id = ? AND artifact_id = ?
                            """,
                            (
                                json.dumps(q["answer"], ensure_ascii=False),
                                str(q.get("explanation") or ""),
                                str(q.get("pitfall") or ""),
                                qid,
                                artifact_id,
                            ),
                        )
                else:
                    conn.execute(
                        "DELETE FROM material_quiz_questions WHERE artifact_id = ?",
                        (artifact_id,),
                    )
                    for q in questions_override:
                        conn.execute(
                            """
                            INSERT INTO material_quiz_questions (
                                id, artifact_id, owner_id, sort_order, kc_id, q_type, stem,
                                options_json, answer_json, explanation, pitfall, cognitive,
                                difficulty, points, source_pages_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(uuid.uuid4()),
                                artifact_id,
                                self._owner_id,
                                int(q["sort_order"]),
                                str(q.get("kc_id") or ""),
                                str(q["q_type"]),
                                str(q["stem"]),
                                json.dumps(q["options"], ensure_ascii=False) if q.get("options") is not None else None,
                                json.dumps(q["answer"], ensure_ascii=False),
                                str(q.get("explanation") or ""),
                                str(q.get("pitfall") or ""),
                                str(q.get("cognitive") or ""),
                                str(q.get("difficulty") or ""),
                                float(q.get("points") or 5),
                                json.dumps(q.get("source_pages") or [], ensure_ascii=False),
                                now,
                            ),
                        )
            cur_row = conn.execute(
                "SELECT notes_status, quiz_status, graph_status, coverage_warnings_json FROM material_artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
            assert cur_row is not None
            previous_warnings, coverage = _read_artifact_coverage(cur_row["coverage_warnings_json"])
            coverage_warnings = list(dict.fromkeys([
                *coverage_warnings,
                *(warning for warning in previous_warnings if warning.startswith(NOTE_WARNING)),
            ]))
            notes_st = "ready" if "notes" in mark_outputs_ready else str(cur_row["notes_status"])
            quiz_st = "ready" if "quiz" in mark_outputs_ready else str(cur_row["quiz_status"])
            graph_st = "ready" if "graph" in mark_outputs_ready else str(cur_row["graph_status"])
            # 若存在历史未重试的 failed 项，顶层 status 保持 failed；否则全部所选就绪则为 ready
            final_status = (
                "failed"
                if "failed" in (notes_st, quiz_st, graph_st)
                else "ready"
            )
            conn.execute(
                """
                UPDATE material_artifacts
                SET status = ?, current_step = 'completed', generation_token = NULL,
                    notes_status = ?,
                    notes_error_code = CASE WHEN ? = 'ready' THEN NULL ELSE notes_error_code END,
                    notes_error_message = CASE WHEN ? = 'ready' THEN NULL ELSE notes_error_message END,
                    quiz_status = ?,
                    quiz_error_code = CASE WHEN ? = 'ready' THEN NULL ELSE quiz_error_code END,
                    quiz_error_message = CASE WHEN ? = 'ready' THEN NULL ELSE quiz_error_message END,
                    graph_status = ?,
                    graph_error_code = CASE WHEN ? = 'ready' THEN NULL ELSE graph_error_code END,
                    graph_error_message = CASE WHEN ? = 'ready' THEN NULL ELSE graph_error_message END,
                    coverage_warnings_json = ?, error_code = NULL, error_message = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    final_status,
                    notes_st,
                    notes_st,
                    notes_st,
                    quiz_st,
                    quiz_st,
                    quiz_st,
                    graph_st,
                    graph_st,
                    graph_st,
                    _write_artifact_coverage(coverage_warnings, coverage),
                    now,
                    artifact_id,
                ),
            )
            return True

    def fail_artifact_generation(
        self,
        *,
        artifact_id: str,
        generation_token: str,
        failed_step: str,
        error_code: str,
        error_message: str,
    ) -> None:
        now = _now()
        code = error_code[:80]
        msg = error_message[:300]
        with self._write() as conn:
            conn.execute(
                """
                UPDATE material_artifacts
                SET status = 'failed', current_step = ?, generation_token = NULL,
                    notes_status = CASE WHEN notes_status = 'generating' THEN 'failed' ELSE notes_status END,
                    notes_error_code = CASE WHEN notes_status = 'generating' THEN ? ELSE notes_error_code END,
                    notes_error_message = CASE WHEN notes_status = 'generating' THEN ? ELSE notes_error_message END,
                    quiz_status = CASE WHEN quiz_status = 'generating' THEN 'failed' ELSE quiz_status END,
                    quiz_error_code = CASE WHEN quiz_status = 'generating' THEN ? ELSE quiz_error_code END,
                    quiz_error_message = CASE WHEN quiz_status = 'generating' THEN ? ELSE quiz_error_message END,
                    graph_status = CASE WHEN graph_status = 'generating' THEN 'failed' ELSE graph_status END,
                    graph_error_code = CASE WHEN graph_status = 'generating' THEN ? ELSE graph_error_code END,
                    graph_error_message = CASE WHEN graph_status = 'generating' THEN ? ELSE graph_error_message END,
                    error_code = ?, error_message = ?, updated_at = ?
                WHERE id = ? AND owner_id = ? AND generation_token = ?
                """,
                (
                    failed_step,
                    code,
                    msg,
                    code,
                    msg,
                    code,
                    msg,
                    code,
                    msg,
                    now,
                    artifact_id,
                    self._owner_id,
                    generation_token,
                ),
            )

    # -----------------------------------------------------------------------
    # F019 第三阶段：会话-资料关联、资料库检索与会话删除影响
    # -----------------------------------------------------------------------

    def _readability_overview_conn(
        self, conn: sqlite3.Connection, *, material_id: str, page_count: int
    ) -> ReadabilityOverview | None:
        rows = conn.execute(
            """
            SELECT page_no, char_count, scanned_suspect, garbled_ratio, formula_warning, parser_version
            FROM personal_material_pages
            WHERE material_id = ? 
            ORDER BY page_no ASC
            """,
            (material_id,),
        ).fetchall()
        if not rows:
            return None
        pages = [
            page_status(
                page_no=int(r["page_no"]), version=str(r["parser_version"]), scanned=bool(r["scanned_suspect"]),
                garbled=float(r["garbled_ratio"]), formula=bool(r["formula_warning"]),
            )
            for r in rows
        ]
        scanned = [p.page_no for p in pages if p.status in ("pending_ocr", "ocr_failed")]
        ocr_pages = [p.page_no for p in pages if p.status == "usable" and p.source == "ocr"]
        total_chars = sum(int(r["char_count"]) for p, r in zip(pages, rows) if p.status == "usable")
        formula_warn = [p.page_no for p in pages if p.needs_review]
        readable = sum(p.status == "usable" for p in pages)
        excluded = sum(p.status == "excluded" for p in pages)
        sufficient = readable > 0 and total_chars >= 120
        if len(rows) < page_count:
            summary = f"已检查 {len(rows)}/{page_count} 页，其中 {readable} 页可读取文字。"
            available = ["notes", "quiz", "graph"] if sufficient else []
        elif not sufficient:
            summary = f"全书 {page_count} 页可用正文不足，可预览原件或识别扫描页。"
            available: list[OutputKind] = []
        elif scanned:
            summary = (
                f"共 {page_count} 页，其中 {readable} 页可读取文字；"
                f"第 {', '.join(str(p) for p in scanned[:6])}{' 等' if len(scanned) > 6 else ''} 页疑似扫描页。"
            )
            available = ["notes", "quiz", "graph"]
        elif ocr_pages or excluded:
            summary = f"共 {page_count} 页，{len(ocr_pages)} 页由 OCR 转写，{excluded} 页无需复习。"
            if formula_warn:
                summary += "公式及图示请对照原页。"
            available = ["notes", "quiz", "graph"]
        else:
            summary = f"共 {page_count} 页，文字提取完整，可生成章节笔记、测试与知识图谱。"
            available = ["notes", "quiz", "graph"]
        return ReadabilityOverview(
            material_id=material_id,
            page_count=page_count,
            readable_pages=readable,
            pages=pages,
            total_chars=total_chars,
            scanned_pages=scanned,
            ocr_pages=ocr_pages,
            formula_warning_pages=formula_warn,
            checked_pages=[int(r["page_no"]) for r in rows],
            sufficient=sufficient,
            summary=summary,
            available_outputs=available,
        )

    def get_readability_overview(self, material_id: str) -> ReadabilityOverview | None:
        with self._read() as conn:
            mat = conn.execute(
                "SELECT page_count FROM personal_materials WHERE id = ? AND owner_id = ? AND status = 'active'",
                (material_id, self._owner_id),
            ).fetchone()
            if mat is None:
                raise MaterialNotFoundError("个人资料不存在。")
            return self._readability_overview_conn(
                conn, material_id=material_id, page_count=int(mat["page_count"])
            )

    def attach_material_to_conversation(
        self, *, conversation_id: str, material_id: str
    ) -> ConversationMaterialItem:
        now = _now()
        with self._write() as conn:
            conv = conn.execute(
                "SELECT id FROM conversations WHERE id = ? AND owner_id = ?",
                (conversation_id, self._owner_id),
            ).fetchone()
            if conv is None:
                raise MaterialNotFoundError("学习会话不存在。")
            mat = conn.execute(
                "SELECT * FROM personal_materials WHERE id = ? AND owner_id = ? AND status = 'active'",
                (material_id, self._owner_id),
            ).fetchone()
            if mat is None:
                raise MaterialNotFoundError("个人资料不存在。")
            self._link_conversation_material(
                conn, conversation_id=conversation_id, material_id=material_id, now=now
            )
        items = self.list_conversation_materials(conversation_id)
        found = next((item for item in items if item.material.id == material_id), None)
        assert found is not None
        return found

    def detach_material_from_conversation(
        self, *, conversation_id: str, material_id: str
    ) -> bool:
        with self._write() as conn:
            conv = conn.execute(
                "SELECT id FROM conversations WHERE id = ? AND owner_id = ?",
                (conversation_id, self._owner_id),
            ).fetchone()
            if conv is None:
                raise MaterialNotFoundError("学习会话不存在。")
            cur = conn.execute(
                """
                DELETE FROM conversation_materials
                WHERE owner_id = ? AND conversation_id = ? AND material_id = ?
                """,
                (self._owner_id, conversation_id, material_id),
            )
            return cur.rowcount > 0

    def list_conversation_materials(
        self, conversation_id: str
    ) -> list[ConversationMaterialItem]:
        with self._read() as conn:
            conv = conn.execute(
                "SELECT id FROM conversations WHERE id = ? AND owner_id = ?",
                (conversation_id, self._owner_id),
            ).fetchone()
            if conv is None:
                raise MaterialNotFoundError("学习会话不存在。")
            rows = conn.execute(
                """
                SELECT m.*, cm.created_at AS attached_at
                FROM conversation_materials cm
                JOIN personal_materials m ON m.id = cm.material_id
                WHERE cm.owner_id = ? AND cm.conversation_id = ? AND m.status = 'active'
                ORDER BY cm.created_at DESC, m.created_at DESC
                """,
                (self._owner_id, conversation_id),
            ).fetchall()
            items: list[ConversationMaterialItem] = []
            for r in rows:
                mid = str(r["id"])
                mat = self._row_to_material(r)
                art_rows = conn.execute(
                    """
                    SELECT a.id, a.material_id, a.file_sha256, a.start_page, a.end_page,
                           a.rule_version, a.course_hint, a.title, a.is_sample, a.status,
                           a.current_step, a.requested_outputs_json,
                           a.notes_status, a.notes_error_code, a.notes_error_message,
                           a.quiz_status, a.quiz_error_code, a.quiz_error_message,
                           a.graph_status, a.graph_error_code, a.graph_error_message,
                           a.conversation_id, a.error_code, a.error_message,
                           a.created_at, a.updated_at, a.coverage_warnings_json,
                           (SELECT COUNT(*) FROM material_quiz_questions q WHERE q.artifact_id = a.id) AS question_count
                    FROM material_artifacts a
                    WHERE a.material_id = ? AND a.owner_id = ?
                    ORDER BY a.updated_at DESC
                    """,
                    (mid, self._owner_id),
                ).fetchall()
                overview = self._readability_overview_conn(
                    conn, material_id=mid, page_count=mat.page_count
                )
                items.append(
                    ConversationMaterialItem(
                        material=mat.to_summary(),
                        readability=overview,
                        artifacts=[self._row_to_artifact_summary(ar) for ar in art_rows],
                        attached_at=_parse_time(str(r["attached_at"])),
                    )
                )
            return items

    def is_artifact_accessible_in_conversation(
        self, *, conversation_id: str, artifact_id: str
    ) -> bool:
        with self._read() as conn:
            row = conn.execute(
                """
                SELECT a.id, a.conversation_id, a.material_id
                FROM material_artifacts a
                JOIN personal_materials m ON m.id = a.material_id
                WHERE a.id = ? AND a.owner_id = ? AND m.status = 'active'
                """,
                (artifact_id, self._owner_id),
            ).fetchone()
            if row is None:
                return False
            if row["conversation_id"] == conversation_id:
                return True
            linked = conn.execute(
                """
                SELECT 1 FROM conversation_materials
                WHERE owner_id = ? AND conversation_id = ? AND material_id = ?
                LIMIT 1
                """,
                (self._owner_id, conversation_id, str(row["material_id"])),
            ).fetchone()
            return linked is not None

    def list_material_library(self) -> list[MaterialLibraryItem]:
        with self._read() as conn:
            mat_rows = conn.execute(
                """
                SELECT * FROM personal_materials
                WHERE owner_id = ? AND status = 'active'
                ORDER BY created_at DESC, id ASC
                """,
                (self._owner_id,),
            ).fetchall()
            out: list[MaterialLibraryItem] = []
            for mr in mat_rows:
                mat = self._row_to_material(mr)
                mid = mat.id
                overview = self._readability_overview_conn(
                    conn, material_id=mid, page_count=mat.page_count
                )
                guide_rows = conn.execute(
                    """
                    SELECT id, material_id, start_page, end_page, rule_version,
                           conversation_id, status, error_code, error_message,
                           created_at, updated_at
                    FROM material_review_guides
                    WHERE material_id = ? AND owner_id = ?
                    ORDER BY created_at DESC
                    """,
                    (mid, self._owner_id),
                ).fetchall()
                guides = [
                    GuideSummary(
                        id=str(gr["id"]),
                        material_id=str(gr["material_id"]),
                        start_page=int(gr["start_page"]),
                        end_page=int(gr["end_page"]),
                        rule_version=str(gr["rule_version"]),
                        conversation_id=str(gr["conversation_id"]) if gr["conversation_id"] else None,
                        status=str(gr["status"]),  # type: ignore[arg-type]
                        error_code=str(gr["error_code"]) if gr["error_code"] else None,
                        error_message=str(gr["error_message"]) if gr["error_message"] else None,
                        created_at=_parse_time(str(gr["created_at"])),
                        updated_at=_parse_time(str(gr["updated_at"])),
                    )
                    for gr in guide_rows
                ]
                art_rows = conn.execute(
                    """
                    SELECT a.id, a.material_id, a.file_sha256, a.start_page, a.end_page,
                           a.rule_version, a.course_hint, a.title, a.is_sample, a.status,
                           a.current_step, a.requested_outputs_json,
                           a.notes_status, a.notes_error_code, a.notes_error_message,
                           a.quiz_status, a.quiz_error_code, a.quiz_error_message,
                           a.graph_status, a.graph_error_code, a.graph_error_message,
                           a.conversation_id, a.error_code, a.error_message,
                           a.created_at, a.updated_at, a.coverage_warnings_json,
                           (SELECT COUNT(*) FROM material_quiz_questions q WHERE q.artifact_id = a.id) AS question_count
                    FROM material_artifacts a
                    WHERE a.material_id = ? AND a.owner_id = ?
                    ORDER BY a.updated_at DESC
                    """,
                    (mid, self._owner_id),
                ).fetchall()
                conv_rows = conn.execute(
                    """
                    SELECT conversation_id FROM conversation_materials
                    WHERE owner_id = ? AND material_id = ?
                    ORDER BY created_at DESC
                    """,
                    (self._owner_id, mid),
                ).fetchall()
                out.append(
                    MaterialLibraryItem(
                        material=mat.to_summary(),
                        readability=overview,
                        guides=guides,
                        artifacts=[self._row_to_artifact_summary(ar) for ar in art_rows],
                        conversation_ids=[str(cr["conversation_id"]) for cr in conv_rows],
                    )
                )
            return out

    def get_conversation_delete_impact(
        self, conversation_id: str
    ) -> ConversationDeleteImpactView:
        with self._read() as conn:
            conv = conn.execute(
                "SELECT id, title FROM conversations WHERE id = ? AND owner_id = ?",
                (conversation_id, self._owner_id),
            ).fetchone()
            if conv is None:
                raise MaterialNotFoundError("学习会话不存在。")
            mat_rows = conn.execute(
                """
                SELECT cm.material_id
                FROM conversation_materials cm
                JOIN personal_materials m ON m.id = cm.material_id
                WHERE cm.owner_id = ? AND cm.conversation_id = ? AND m.status = 'active'
                """,
                (self._owner_id, conversation_id),
            ).fetchall()
            mat_ids = [str(r["material_id"]) for r in mat_rows]
            artifact_count = 0
            if mat_ids:
                placeholders = ",".join("?" for _ in mat_ids)
                artifact_count = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM material_artifacts WHERE owner_id = ? AND material_id IN ({placeholders})",
                        (self._owner_id, *mat_ids),
                    ).fetchone()[0]
                )
            if mat_ids:
                summary = (
                    f"该会话关联了 {len(mat_ids)} 份资料与 {artifact_count} 份复习成果。"
                    "删除会话仅移除对话记录和会话关联，原 PDF 与已生成成果仍保留在「资料与成果」中，可随时在其他会话继续打开。"
                )
            else:
                summary = "删除该会话将永久移除本会话内的对话记录与临时图片附件。"
            return ConversationDeleteImpactView(
                conversation_id=conversation_id,
                title=str(conv["title"]),
                linked_material_count=len(mat_ids),
                artifact_count=artifact_count,
                materials_still_accessible=True,
                summary=summary,
            )
