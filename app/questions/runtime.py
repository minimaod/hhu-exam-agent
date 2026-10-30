"""进程级题目运行时：单例、串行访问、状态映射、材料与题图。

- ``QuestionBankService`` 只在应用启动时构造一次，关闭时释放 Qdrant；
- 所有题库/归档读取经同一把可重入锁串行，调用方用 ``asyncio.to_thread``；
- 检索失败、未审核、未建库、降级都映射成学生能理解的状态，不暴露内部诊断。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from app.question_bank.config import DEFAULT_BANK_ROOT
from app.question_bank.errors import QuestionBankRequestError
from app.question_bank.models import SearchRequest, SearchResponse
from app.question_bank.service import QuestionBankService
from app.questions.archive import BuildArchive
from app.questions.cards import (
    answer_url,
    attach_answer,
    card_from_snapshot,
    card_from_summary,
    compose_material,
)
from app.questions.errors import QuestionError, QuestionNotRevealedError
from app.questions.models import (
    AssetPayload,
    EvidenceBundle,
    QuestionCapabilityView,
    QuestionCard,
    QuestionSearchResult,
    QuestionSelection,
)
from app.tutoring.models import ImagePayload

logger = logging.getLogger(__name__)

COURSE = "Python语言程序设计"


@dataclass(frozen=True)
class QuestionMaterial:
    """一次讲解/追问用的私有材料；images 只在内存中传给模型。"""

    text: str
    images: list[ImagePayload] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    evidence: EvidenceBundle | None = None
    card: QuestionCard | None = None


def bank_root_from_env() -> Path:
    raw = os.environ.get("HHU_QB_ROOT")
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_BANK_ROOT


class QuestionRuntime:
    def __init__(
        self,
        service: QuestionBankService | None,
        *,
        error: str | None = None,
        bank_root: Path | None = None,
        papers_root: Path | None = None,
    ) -> None:
        self._service = service
        self._error = error
        self._root = Path(bank_root) if bank_root is not None else DEFAULT_BANK_ROOT
        self._archive = BuildArchive(self._root, papers_root)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._service is not None

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def build_id(self) -> str | None:
        if self._service is None:
            return None
        return self._service.build_id

    @property
    def source_version(self) -> str | None:
        if self._service is None:
            return None
        return self._service.source_version

    def close(self) -> None:
        with self._lock:
            if self._service is not None:
                try:
                    self._service.close()
                except Exception:  # pragma: no cover - 关闭失败不阻塞退出
                    logger.warning("关闭题目库服务失败", exc_info=True)
                self._service = None

    # ------------------------------------------------------------------
    # 能力与检索
    # ------------------------------------------------------------------

    def capability(self, course: str) -> QuestionCapabilityView:
        label = course or COURSE
        if self._service is None:
            return QuestionCapabilityView(
                course=label,
                status="error",
                message="题目库暂时不可用，请稍后重试；仍可查看或下载原卷。",
            )
        with self._lock:
            try:
                capability = self._service.capability(label)
            except QuestionBankRequestError:
                return QuestionCapabilityView(
                    course=label,
                    status="unknown",
                    message="未登记该课程，暂不能按题检索。",
                )
            except Exception:
                logger.warning("读取题库能力失败", exc_info=True)
                return QuestionCapabilityView(
                    course=label,
                    status="error",
                    message="题目库暂时不可用，请稍后重试；仍可查看或下载原卷。",
                )
        approved = int(capability.approved_count)
        searchable = int(capability.searchable_count)
        if capability.status == "ready":
            message = f"Python 题库可用：{approved} 道已审核题可检索。"
        elif capability.status == "unreviewed":
            message = (
                f"题库已建（{searchable} 道可检索），题目正在逐题审核，"
                "暂不能按题检索；可先查看或下载原卷。"
            )
        elif capability.status == "catalog_only":
            message = "该课程目前只有试卷目录，没有真题题目库；可查看或下载原卷。"
        elif capability.status == "unknown":
            message = "未登记该课程，暂不能按题检索。"
        else:
            message = "题目库暂时不可用，请稍后重试；仍可查看或下载原卷。"
        return QuestionCapabilityView(
            course=capability.course,
            status=capability.status,  # type: ignore[arg-type]
            searchable=searchable,
            approved=approved,
            message=message,
        )

    def search(
        self,
        request: SearchRequest,
        *,
        conversation_id: str,
        request_id: str = "",
    ) -> QuestionSearchResult:
        if self._service is None:
            return QuestionSearchResult(
                status="bank_unavailable",
                message="题目库暂时不可用，请稍后重试；仍可查看或下载原卷。",
            )
        if request.preview_candidates:
            # 产品路径绝不预览未审核题目；即使上层误传也在这里收口。
            request = request.model_copy(update={"preview_candidates": False})
        with self._lock:
            try:
                response = self._service.search(request)
            except QuestionBankRequestError as exc:
                # 模型可能建议不存在的知识点；程序不据此扩大筛选，但也不让
                # 一个无效建议把整次查题变成“条件不完整”。去掉建议后重试一次。
                logger.info("题目检索条件被拒绝，将忽略知识点建议重试：%s", exc)
                if not request.knowledge_points:
                    return QuestionSearchResult(
                        status="no_match",
                        message="查题条件不完整，请补充课程或关键词。",
                    )
                request = request.model_copy(update={"knowledge_points": []})
                try:
                    response = self._service.search(request)
                except QuestionBankRequestError:
                    return QuestionSearchResult(
                        status="no_match",
                        message="查题条件不完整，请补充课程或关键词。",
                    )
                except Exception:
                    logger.warning("题目检索失败", exc_info=True)
                    return QuestionSearchResult(
                        status="bank_unavailable",
                        message="题目库暂时不可用，请稍后重试；仍可查看或下载原卷。",
                    )
            except Exception:
                logger.warning("题目检索失败", exc_info=True)
                return QuestionSearchResult(
                    status="bank_unavailable",
                    message="题目库暂时不可用，请稍后重试；仍可查看或下载原卷。",
                )
            build_id = (response.build.build_id if response.build else None) or self.build_id or ""
            paper_id, paper_title = _paper_of(response)
            paging = {
                "total": response.matched_total,
                "offset": response.offset,
                "returned": response.returned,
                "next_offset": response.next_offset,
                "paper_id": paper_id or request.paper_id,
                "paper_title": paper_title,
                "question_type": request.question_type,
            }
            if response.status == "ok" and response.items:
                cards = [
                    card_from_summary(
                        item,
                        conversation_id=conversation_id,
                        build_id=build_id,
                        source_request_id=request_id or None,
                    )
                    for item in response.items
                ]
                message = ""
                if response.degraded:
                    message = "向量检索暂不可用，本次使用关键词检索；结果仍来自已审核题目。"
                return QuestionSearchResult(
                    status="ok",
                    message=message,
                    cards=cards,
                    mode=response.mode,
                    degraded=bool(response.degraded),
                    **paging,
                )
            capability = self._capability_locked(request.course)
            if response.status == "bank_not_built":
                return QuestionSearchResult(
                    status="bank_not_built",
                    message=capability.message
                    if capability.status != "error"
                    else (response.message or "该课程暂无真题题目库。"),
                    mode=response.mode,
                )
            if response.status == "bank_unavailable":
                return QuestionSearchResult(
                    status="bank_unavailable",
                    message="题目库暂时不可用，请稍后重试；仍可查看或下载原卷。",
                )
            if capability.status == "unreviewed":
                return QuestionSearchResult(
                    status="unreviewed",
                    message=(
                        "题目资料正在逐题审核，暂不能按题检索；"
                        "可先查看或下载原卷，审核通过后会开放查题。"
                    ),
                    mode=response.mode,
                )
            if request.exam_type == "other":
                return QuestionSearchResult(
                    status="no_match",
                    message="Python 题库目前只有期中、期末真题；其他类型请查看试卷目录。",
                    mode=response.mode,
                )
            return QuestionSearchResult(
                status="no_match",
                message="没有找到符合这些条件的已审核题目。可以放宽题型、学期或关键词再试。",
                mode=response.mode,
            )

    def _capability_locked(self, course: str) -> QuestionCapabilityView:
        # 调用方已持有锁；capability() 会再次获取可重入锁。
        return self.capability(course)

    # ------------------------------------------------------------------
    # 卡片与答案
    # ------------------------------------------------------------------

    def card_for_selection(
        self,
        selection: QuestionSelection,
        *,
        selected: bool = True,
    ) -> QuestionCard:
        card = card_from_snapshot(
            selection.snapshot,
            selection_id=selection.id,
            selected=selected,
            answer_revealed=bool(selection.answer_revealed_at),
            answer_revealed_at=selection.answer_revealed_at,
        )
        # 快照里的资产 URL 已经绑定会话与 build；answer_url 由当前会话重建。
        return card.model_copy(
            update={
                "source_request_id": selection.source_request_id,
                "source_url": answer_url(selection.conversation_id, selection.question_id, selection.build_id).replace("/answer?", "/source?"),
                "answer_url": (
                    answer_url(
                        selection.conversation_id,
                        selection.question_id,
                        selection.build_id,
                    )
                    if card.answer.available
                    else None
                )
            }
        )

    def evidence(self, selection: QuestionSelection) -> EvidenceBundle:
        with self._lock:
            try:
                return self._archive.evidence(selection)
            except QuestionError as exc:
                status = (
                    "source_pruned"
                    if getattr(exc, "status_code", 409) == 410
                    else "source_changed"
                )
                return EvidenceBundle(status=status, message=exc.message)  # type: ignore[arg-type]
            except Exception:
                logger.warning("读取题目答案失败", exc_info=True)
                return EvidenceBundle(
                    status="unavailable", message="原答案暂时无法读取，请下载原卷核对。"
                )

    def answer_view(
        self, selection: QuestionSelection
    ) -> tuple[QuestionCard, EvidenceBundle]:
        if not selection.answer_revealed_at:
            raise QuestionNotRevealedError("还没有查看这道题的答案。")
        evidence = self.evidence(selection)
        card = self.card_for_selection(selection)
        if evidence.status in ("source_changed", "source_pruned"):
            return card, evidence
        return attach_answer(card, evidence), evidence

    def asset(self, selection: QuestionSelection, asset_id: str) -> AssetPayload:
        with self._lock:
            return self._archive.asset(selection, asset_id)

    def asset_for_card(self, card: QuestionCard, asset_id: str) -> AssetPayload:
        """候选卡片尚未选中时也能读题面图；仍校验 build、题号和内容哈希。"""

        with self._lock:
            reference = SimpleNamespace(
                build_id=card.build_id,
                question_id=card.question_id,
                content_sha256=card.content_sha256,
            )
            return self._archive.asset(reference, asset_id)

    def asset_images(
        self, selection: QuestionSelection
    ) -> tuple[list[ImagePayload], list[str]]:
        images: list[ImagePayload] = []
        errors: list[str] = []
        assets = selection.snapshot.get("assets") or []
        for item in assets:
            asset_id = str(item.get("asset_id") or "")
            if not asset_id:
                continue
            try:
                payload = self.asset(selection, asset_id)
                images.append(
                    ImagePayload(data=payload.path.read_bytes(), mime=payload.mime)
                )
            except QuestionError as exc:
                errors.append(exc.message)
            except Exception:
                logger.warning("读取题图失败：%s", asset_id, exc_info=True)
                errors.append("题图读取失败，可下载原卷核对。")
        return images, errors

    def source_for_card(self, card: QuestionCard, kind: str, page: int) -> AssetPayload:
        with self._lock:
            return self._archive.source(card, kind, page)

    def material(self, selection: QuestionSelection) -> QuestionMaterial:
        card = self.card_for_selection(selection)
        evidence = self.evidence(selection)
        images, errors = self.asset_images(selection)
        text = compose_material(card, evidence)
        if errors:
            text += "\n\n规则：以下题图未能加载，不要假装看到了它们：" + "；".join(errors)
        return QuestionMaterial(
            text=text,
            images=images,
            errors=errors,
            evidence=evidence,
            card=card,
        )

    def has_build(self, build_id: str) -> bool:
        return self._archive.exists(build_id)

    def verify_card(self, card: QuestionCard) -> bool:
        """卡片仍属于当前 build 且内容哈希一致。"""

        if self._service is None:
            return False
        if self._service.build_id and card.build_id and card.build_id != self._service.build_id:
            return False
        if not card.content_sha256:
            return False
        return self._archive.verify(card.build_id, card.question_id, card.content_sha256)

    def is_approved(self, question_id: str) -> bool:
        if self._service is None:
            return False
        return question_id in self._service.approved_ids


def _paper_of(response: SearchResponse) -> tuple[str | None, str | None]:
    """从本次结果里取出试卷身份；空结果时返回 ``(None, None)``。"""

    for item in response.items:
        paper = getattr(item, "paper", None)
        if paper is not None:
            return paper.paper_id, paper.title
    return None, None


def build_question_runtime() -> QuestionRuntime:
    """应用启动时构造一次；题库损坏不阻止应用启动。"""

    root = bank_root_from_env()
    try:
        service = QuestionBankService(root)
    except Exception as exc:
        logger.warning("题目库初始化失败：%s", exc)
        return QuestionRuntime(None, error=str(exc), bank_root=root)
    return QuestionRuntime(service, bank_root=root)
