"""F007 P4 测试替身：内存真题运行时与可复用的公开卡片。"""

from __future__ import annotations

from app.question_bank.models import AnswerDetail
from app.questions.cards import attach_answer, card_from_snapshot
from app.questions.errors import QuestionNotRevealedError
from app.questions.models import (
    AnswerAvailability,
    EvidenceBundle,
    QuestionAssetView,
    QuestionCapabilityView,
    QuestionCard,
    QuestionOption,
    QuestionPages,
    QuestionReview,
    QuestionSearchResult,
    QuestionSelection,
    QuestionSourceView,
)
from app.questions.runtime import QuestionMaterial

BUILD_ID = "iv-0123456789abcdef"
SOURCE_VERSION = "sv-0123456789abcdef"


def sample_card(
    question_id: str = "py-2024-2025-s1-final-choice-1",
    *,
    number: str = "1",
    statement: str = "下列 Python 表达式的值是？",
    answer_text: str = "B",
) -> QuestionCard:
    return QuestionCard(
        question_id=question_id,
        build_id=BUILD_ID,
        question_type="choice",
        number_label=number,
        statement=statement,
        content_sha256="a" * 64,
        options=[
            QuestionOption(label="A", text="1"),
            QuestionOption(label="B", text="2"),
        ],
        assets=[
            QuestionAssetView(asset_id="asset-1", page=1, url="/assets/asset-1")
        ],
        pages=QuestionPages(statement=[1], answer=[2]),
        answer=AnswerAvailability(state="original", available=True),
        review=QuestionReview(status="approved", approved=True, stale=False),
        source=QuestionSourceView(
            paper_id="py-2024-2025-s1-final",
            paper_title="2024-2025 学年第一学期 Python 期末试卷",
            exam_type="final",
            academic_year="2024-2025",
            question_number=number,
            download_url="/api/papers/py-2024-2025-s1-final/download",
            may_show_answer=True,
        ),
    )


class FakeQuestionRuntime:
    """只覆盖服务层真正调用的方法；不读磁盘、不连 Qdrant。"""

    ready = True
    build_id = BUILD_ID
    source_version = SOURCE_VERSION

    def __init__(
        self,
        cards: list[QuestionCard] | None = None,
        *,
        search_status: str = "ok",
        search_message: str = "",
        evidence_status: str = "ok",
        answer_text: str = "B（测试答案）",
    ) -> None:
        self.cards = cards or [sample_card()]
        self.search_status = search_status
        self.search_message = search_message
        self.evidence_status = evidence_status
        self.answer_text = answer_text
        self.search_calls: list[dict] = []
        self.asset_calls: list[tuple[str, str]] = []
        self.material_calls: list[str] = []
        self.closed = False

    def capability(self, course: str) -> QuestionCapabilityView:
        return QuestionCapabilityView(
            course=course,
            status="ready",
            searchable=len(self.cards),
            approved=len(self.cards),
            message="Python 题库可用：测试数据。",
        )

    def search(
        self, request, *, conversation_id: str, request_id: str = ""
    ) -> QuestionSearchResult:
        self.search_calls.append(
            {
                "request": request,
                "conversation_id": conversation_id,
                "request_id": request_id,
            }
        )
        if self.search_status != "ok":
            return QuestionSearchResult(
                status=self.search_status, message=self.search_message
            )
        pool = list(self.cards)
        if request.paper_id:
            pool = [card for card in pool if card.source.paper_id == request.paper_id]
        number_label = getattr(request, "number_label", None)
        if number_label:
            # 与真实题库一致：原卷题号是精确过滤条件，不是列表位置。
            pool = [card for card in pool if card.number_label == str(number_label)]
        question_type = getattr(request, "question_type", None)
        if question_type:
            # 同卷不同题型可以有相同题号，题型也是精确过滤条件。
            pool = [card for card in pool if card.question_type == question_type]
        offset = min(max(int(getattr(request, "offset", 0) or 0), 0), len(pool))
        limit = int(request.limit or 0)
        window = pool[offset : offset + limit] if limit else pool[offset:]
        cards = [
            card.model_copy(
                update={
                    "source_request_id": request_id or None,
                    "answer_url": (
                        f"/api/conversations/{conversation_id}/questions/"
                        f"{card.question_id}/answer?build={card.build_id}"
                    ),
                }
            )
            for card in window
        ]
        total = len(pool)
        end = offset + len(cards)
        first = cards[0] if cards else None
        return QuestionSearchResult(
            status="ok",
            cards=cards,
            mode="bm25",
            message=self.search_message,
            total=total,
            offset=offset,
            returned=len(cards),
            next_offset=end if end < total else None,
            paper_id=first.source.paper_id if first is not None else None,
            paper_title=first.source.paper_title if first is not None else None,
        )

    def is_approved(self, question_id: str) -> bool:
        return any(card.question_id == question_id for card in self.cards)

    def verify_card(self, card: QuestionCard) -> bool:
        return any(
            item.question_id == card.question_id
            and item.build_id == card.build_id
            and item.content_sha256 == card.content_sha256
            for item in self.cards
        )

    def card_for_selection(
        self, selection: QuestionSelection, *, selected: bool = True
    ) -> QuestionCard:
        card = card_from_snapshot(
            selection.snapshot,
            selection_id=selection.id,
            selected=selected,
            answer_revealed=bool(selection.answer_revealed_at),
            answer_revealed_at=selection.answer_revealed_at,
        )
        return card.model_copy(
            update={"source_request_id": selection.source_request_id}
        )

    def evidence(self, selection: QuestionSelection) -> EvidenceBundle:
        if self.evidence_status == "missing_answer":
            return EvidenceBundle(status="missing_answer", message="原资料未提供参考答案。")
        if self.evidence_status == "source_changed":
            return EvidenceBundle(
                status="source_changed", message="题目内容已变化，请重新查题。"
            )
        if self.evidence_status == "source_pruned":
            return EvidenceBundle(
                status="source_pruned", message="原来源版本已不在本机。"
            )
        return EvidenceBundle(
            status="ok",
            answer_detail=AnswerDetail(state="original", text=self.answer_text),
        )

    def answer_view(
        self, selection: QuestionSelection
    ) -> tuple[QuestionCard, EvidenceBundle]:
        if not selection.answer_revealed_at:
            raise QuestionNotRevealedError("还没有查看这道题的答案。")
        evidence = self.evidence(selection)
        card = self.card_for_selection(selection)
        return attach_answer(card, evidence), evidence

    def material(self, selection: QuestionSelection) -> QuestionMaterial:
        self.material_calls.append(selection.question_id)
        card = self.card_for_selection(selection)
        return QuestionMaterial(
            text=f"【真题材料】{card.source.paper_title} 第 {card.number_label} 题\n题干：{card.statement}",
            images=[],
            errors=[],
            evidence=self.evidence(selection),
            card=card,
        )

    def asset(self, selection: QuestionSelection, asset_id: str):
        from app.questions.models import AssetPayload
        from pathlib import Path

        self.asset_calls.append((selection.question_id, asset_id))
        return AssetPayload(
            path=Path(__file__), mime="image/png", page=1, description="测试题图"
        )

    def asset_for_card(self, card: QuestionCard, asset_id: str):
        from app.questions.models import AssetPayload
        from pathlib import Path

        self.asset_calls.append((card.question_id, asset_id))
        return AssetPayload(
            path=Path(__file__), mime="image/png", page=1, description="测试题图"
        )

    def close(self) -> None:
        self.closed = True
