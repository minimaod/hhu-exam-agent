"""当前/历史 build 的只读回读：答案、参考代码、代码块与题图。

只打开 ``questions.sqlite3``（``mode=ro``），不碰 Qdrant，也不写学习库。
每次读取都校验题目内容哈希；资源再校验 manifest 中的文件哈希与物理页。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.question_bank.models import (
    AnswerDetail,
    AnswerSource,
    CodeAnnotation,
    CodeBlock,
    IndependentSolution,
)
from app.question_bank.paths import BUILD_ID_RE, BankPaths
from app.question_bank.config import DEFAULT_PAPERS_ROOT
from app.question_bank.repository import QuestionRepository
from app.questions.errors import (
    QuestionAssetChangedError,
    QuestionAssetMissingError,
    QuestionBankUnavailableError,
    QuestionError,
    QuestionNotFoundError,
    QuestionSourceChangedError,
    QuestionSourcePrunedError,
)
from app.questions.models import AssetPayload, EvidenceBundle


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BuildArchive:
    def __init__(self, bank_root: Path, papers_root: Path | None = None) -> None:
        self._paths = BankPaths(Path(bank_root))
        self._papers_root = Path(papers_root or DEFAULT_PAPERS_ROOT).resolve()

    def build_dir(self, build_id: str) -> Path:
        if not BUILD_ID_RE.match(build_id or ""):
            raise QuestionSourcePrunedError("题目来源版本标识无效。")
        path = self._paths.build_dir(build_id)
        if not path.is_dir():
            raise QuestionSourcePrunedError(
                "这道题的原来源版本已不在本机，无法读取原答案或题图；可下载原卷核对。"
            )
        return path

    def _open(self, build_id: str) -> QuestionRepository:
        build_dir = self.build_dir(build_id)
        try:
            return QuestionRepository(build_dir)
        except QuestionBankUnavailableError as exc:
            raise QuestionSourcePrunedError(
                "这道题的原来源版本无法打开，可下载原卷核对。"
            ) from exc

    def _detail(self, build_id: str, question_id: str, expected_sha: str) -> dict:
        repository = self._open(build_id)
        try:
            record = repository.fetch_detail(question_id)
            if record is None:
                raise QuestionSourceChangedError(
                    "这道题已不在原来源版本中，请重新查题。"
                )
            if str(record.get("content_sha256") or "") != expected_sha:
                raise QuestionSourceChangedError(
                    "题目内容与选中时不一致，原答案不再适用；请重新查题。"
                )
            record["_build"] = repository.build
            record["_paper"] = repository.get_paper(record["paper_id"])
            return record
        finally:
            repository.close()

    @staticmethod
    def _issues(record: dict) -> list[str]:
        issue = str(record.get("content_issue") or "").strip()
        return [issue] if issue and issue != "ok" else []

    def evidence(self, selection) -> EvidenceBundle:
        record = self._detail(
            selection.build_id, selection.question_id, selection.content_sha256
        )
        issues = self._issues(record)
        payload = record.get("answer_json") or {}
        available = bool(record.get("answer_available"))
        state = str(record.get("answer_state") or payload.get("state") or "pending_review")
        detail: AnswerDetail | None = None
        if payload:
            detail = AnswerDetail(
                state=state,  # type: ignore[arg-type]
                text=payload.get("text"),
                note=payload.get("note"),
                origin=payload.get("origin"),
                label=payload.get("label"),
                sources=[
                    AnswerSource(
                        kind=str(item.get("kind") or ""),
                        text=item.get("text"),
                        detail=item.get("detail"),
                        pairing_verified=item.get("pairing_verified"),
                        pages=[int(page) for page in item.get("pages") or []],
                    )
                    for item in record.get("answer_sources") or []
                ],
            )
        blocks = [
            CodeBlock(
                block_id=item.get("block_id"),
                method=item.get("method"),
                method_label=item.get("method_label"),
                column=item.get("column"),
                kind=item.get("kind"),
                name=item.get("name"),
                text=str(item.get("text") or ""),
                page=item.get("page"),
                pages=[int(page) for page in item.get("pages") or []],
                depends_on=list(item.get("depends_on") or []),
            )
            for item in record.get("code_blocks") or []
        ]
        annotations = [
            CodeAnnotation(
                role=item.get("role"),
                text=str(item.get("text") or ""),
                page=item.get("page"),
            )
            for item in record.get("code_annotations") or []
        ]
        solutions = [
            IndependentSolution(
                author=item.get("author"),
                text=str(item.get("text") or ""),
                note=item.get("note"),
            )
            for item in record.get("independent_solutions") or []
        ]
        text = (detail.text or "").strip() if detail is not None else ""
        if not available or state == "missing":
            status = "missing_answer"
            message = "原资料未提供参考答案。"
        elif state == "conflict":
            status = "ok"
            message = "原卷答案存在冲突，需要按原卷核对。"
        else:
            status = "ok"
            message = None
        return EvidenceBundle(
            status=status,  # type: ignore[arg-type]
            answer_detail=detail,
            reference_code=record.get("reference_code"),
            reference_code_pages=[
                int(page) for page in record.get("reference_code_pages") or []
            ],
            code_blocks=blocks,
            code_annotations=annotations,
            code_examples=[
                str(item.get("text") or "") for item in record.get("code_examples") or []
            ],
            independent_solutions=solutions,
            content_issues=issues,
            message=message,
        )

    def asset(self, selection, asset_id: str) -> AssetPayload:
        record = self._detail(
            selection.build_id, selection.question_id, selection.content_sha256
        )
        asset = next(
            (
                item
                for item in record.get("assets") or []
                if str(item.get("asset_id")) == asset_id
            ),
            None,
        )
        if asset is None:
            raise QuestionAssetMissingError("这道题没有该题图。")
        page = asset.get("page")
        statement_pages = {int(item) for item in record.get("pages_statement") or []}
        if page is not None and statement_pages and int(page) not in statement_pages:
            raise QuestionAssetMissingError("该资源不属于这道题的题面页。")

        manifest = {
            str(item.get("asset_id")): item
            for item in record["_build"].get("assets") or []
        }
        entry = manifest.get(asset_id)
        if entry is None:
            raise QuestionAssetMissingError("题图清单中没有该资源。")
        build_dir = self.build_dir(selection.build_id)
        try:
            path = (build_dir / str(entry["path"])).resolve()
        except (KeyError, OSError) as exc:
            raise QuestionAssetMissingError("题图路径无效。") from exc
        if not path.is_relative_to(build_dir.resolve()):
            raise QuestionAssetMissingError("题图路径越界，已拒绝。")
        if not path.is_file():
            raise QuestionAssetMissingError("题图文件已丢失，可下载原卷核对。")
        if _sha256(path) != str(entry.get("sha256") or ""):
            raise QuestionAssetChangedError("题图文件与清单哈希不一致，已拒绝展示。")
        suffix = path.suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        return AssetPayload(
            path=path,
            mime=mime,
            page=int(page) if page is not None else None,
            description=asset.get("description"),
        )

    def source(self, reference, kind: str, page: int) -> AssetPayload:
        """只允许该版本题目引用的物理页；原卷必须仍与建库哈希一致。"""
        record = self._detail(
            reference.build_id, reference.question_id, reference.content_sha256
        )
        allowed = record.get("pages_statement" if kind == "statement" else "pages_answer") or []
        if kind not in ("statement", "answer") or page not in allowed:
            raise QuestionAssetMissingError("该页不是这道题的题面或答案来源页。")
        paper = record.get("_paper") or {}
        relative = paper.get("pdf_rel_path")
        if not relative:
            raise QuestionAssetMissingError("原卷来源路径缺失，暂时无法预览。")
        path = (self._papers_root / relative).resolve()
        if not path.is_relative_to(self._papers_root):
            raise QuestionAssetMissingError("原卷来源路径无效。")
        if not path.is_file():
            raise QuestionAssetMissingError("原卷文件不在本机，暂时无法预览。")
        if not paper.get("pdf_sha256") or _sha256(path) != paper["pdf_sha256"]:
            raise QuestionSourceChangedError("原卷文件已变化，已停止展示旧来源；请重新核对资料。")
        if not 1 <= page <= int(paper.get("physical_pages") or 0):
            raise QuestionAssetMissingError("来源物理页码无效。")
        return AssetPayload(path=path, mime="application/pdf", page=page, description="原卷来源",
                            source_version=record["_build"].get("source_version"))

    def asset_bytes(self, selection, asset_id: str) -> tuple[bytes, str]:
        payload = self.asset(selection, asset_id)
        return payload.path.read_bytes(), payload.mime

    def exists(self, build_id: str) -> bool:
        try:
            self.build_dir(build_id)
        except QuestionSourcePrunedError:
            return False
        return True

    def verify(self, build_id: str, question_id: str, content_sha256: str) -> bool:
        """选中/讲解前确认该 build 仍有同一内容的这道题。"""

        try:
            self._detail(build_id, question_id, content_sha256)
        except QuestionError:
            return False
        return True
