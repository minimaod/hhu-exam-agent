"""F008 收尾返修 R1–R4 的回归：先复现，再验证修复。

四项都来自 §7.7 独立复核：
R1 换到不存在的卷之后，下一轮指代不能静默回到旧卷；
R2 A/B 卷澄清后只回答“B卷”，必须沿用上一轮的学年与学期；
R3 带“这道题”的跨卷请求不能被旧卷过滤条件覆盖；
R4 真实下载链接必须出现在传给正文模型的提示里。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.conversations.final_reply import material_from_context
from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.reply import material_from_payload
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.papers.models import PaperItem, PaperListResponse
from app.tutoring.models import Source
from tests.question_fakes import FakeQuestionRuntime, sample_card
from tests.tool_fakes import ReplyFactory
from tests.tutoring_fakes import FakeScheduler, FakeTutor, sample_explanation

COURSE = "Python语言程序设计"


def paper(paper_id: str, year: str, term: str = "s1", variant: str = "A") -> PaperItem:
    label = "第一学期" if term == "s1" else "第二学期"
    return PaperItem(
        id=paper_id,
        title=f"{year}学年{label}《Python语言程序设计》期末试卷（{variant}卷）",
        course=COURSE,
        academic_year=year,
        exam_type="final",
        exam_date=None,
        download_url=f"/api/papers/{paper_id}/download",
    )


class FakePapers:
    def __init__(self, items: list[PaperItem]) -> None:
        self._items = items

    def list_papers(self, course=None, exam_type=None) -> PaperListResponse:
        items = [
            item
            for item in self._items
            if (course is None or item.course == course)
            and (exam_type is None or item.exam_type == exam_type)
        ]
        return PaperListResponse(items=items, total=len(items))

    def get_paper(self, paper_id: str) -> PaperItem | None:
        return next((item for item in self._items if item.id == paper_id), None)


def build_service(
    tmp_path: Path,
    *,
    items: list[PaperItem],
    scheduler: FakeScheduler | None = None,
    cards=None,
    reply_factory=None,
    tutor=None,
) -> tuple[ConversationService, ConversationRepository, FakeQuestionRuntime]:
    """同一目录新建服务 = 模拟刷新/重启后的进程。"""

    repository = ConversationRepository(tmp_path / "study.sqlite3")
    repository.initialize()
    runtime = FakeQuestionRuntime(cards=cards or [])
    service = ConversationService(
        repository=repository,
        make_query=None,
        query_error="模型未配置",
        make_tutor=(lambda: tutor) if tutor is not None else None,
        tutoring_error="模型未配置",
        make_scheduler=(lambda: scheduler) if scheduler is not None else None,
        make_reply=reply_factory,
        papers=FakePapers(items),
        question_runtime=runtime,
        attachments_root=tmp_path / "attachments",
    )
    return service, repository, runtime


class NeverDisconnected:
    async def is_disconnected(self) -> bool:
        return False


async def run(service: ConversationService, conversation_id: str, text: str):
    data = TurnInput(
        request_id=f"r-{uuid4().hex}",
        kind=None,
        text=text,
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(type="none"),
        problem_id=None,
        start_new=False,
        dispatch=True,
    )
    prepared = service.prepare_turn(conversation_id, data)
    return [event async for event in service.stream_prepared(prepared, NeverDisconnected())]


def result_of(events):
    return next(event for event in events if event.type == "result")


def query_decision(**parameters) -> ScheduleDecision:
    return ScheduleDecision(
        actions=[ActionPlan(action="query_papers", parameters=parameters)]
    )


# ---------------------------------------------------------------------------
# R1：换到不存在的卷 → 刷新/重启后指代不能回到旧卷
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_missing_paper_switch_invalidates_current_paper(tmp_path: Path) -> None:
    items = [paper("py-2024-2025-s1-final", "2024-2025")]
    scheduler = FakeScheduler(decisions=[query_decision(
        course=COURSE, exam_type="final",
        academic_year="2024-2025", term="s1", variant="A",
    )])
    service, repository, _ = build_service(tmp_path, items=items, scheduler=scheduler)
    conversation = service.create_conversation()

    found = result_of(
        await run(
            service,
            conversation.id,
            "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）",
        )
    )
    assert found.result.total == 1

    scheduler.push(query_decision(
        course=COURSE, exam_type="final",
        academic_year="2019-2020", term="s1",
    ))
    missing = result_of(
        await run(service, conversation.id, "换成 2019-2020 学年第一学期 Python 期末试卷")
    )
    assert missing.result.kind == "empty"

    # 刷新/重启：同一学习库上新建服务进程。
    scheduler2 = FakeScheduler()
    restarted, _, runtime = build_service(tmp_path, items=items, scheduler=scheduler2)
    scheduler2.push(
        ScheduleDecision(
            actions=[ActionPlan(action="list_paper_questions", parameters={})]
        )
    )
    followup = result_of(await run(restarted, conversation.id, "这张卷的选择题"))

    assert runtime.search_calls == [], "旧卷不应被静默恢复成当前目标"
    assert followup.question_results == []
    message = followup.assistant_message
    assert "2019-2020" not in message or "没有" in message
    assert any(word in message for word in ("哪一份", "哪份", "试卷", "说明"))

    # 旧卷历史仍然保留：学生明确点名回旧卷时照常命中。
    scheduler2.push(query_decision(
        course=COURSE, exam_type="final",
        academic_year="2024-2025", term="s1", variant="A",
    ))
    back = result_of(
        await run(
            restarted,
            conversation.id,
            "回到 2024-2025 学年第一学期《Python语言程序设计》期末试卷（A卷）",
        )
    )
    assert back.result.total == 1
    assert back.result.items[0].id == "py-2024-2025-s1-final"

    # 新的成功定位要覆盖更早的“未定位”标记，否则之后的指代会一直失效。
    scheduler2.push(query_decision(
        course=COURSE, exam_type="final",
        academic_year="2024-2025", term="s1", variant="A",
    ))
    follow = result_of(
        await run(
            restarted,
            conversation.id,
            "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）",
        )
    )
    assert follow.result.total == 1
    assert repository.find_paper_state(conversation.id) is None


# ---------------------------------------------------------------------------
# R2：A/B 澄清后只回答“B卷”，沿用上一轮学年学期
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_clarification_reuses_year_and_term(tmp_path: Path) -> None:
    items = [
        paper("py-2024-2025-s1-final", "2024-2025", variant="A"),
        paper("py-2024-2025-s1-final-b", "2024-2025", variant="B"),
        paper("py-2023-2024-s1-final", "2023-2024", variant="A"),
        paper("py-2023-2024-s1-final-b", "2023-2024", variant="B"),
    ]
    scheduler = FakeScheduler(decisions=[query_decision(
        course=COURSE, exam_type="final",
        academic_year="2024-2025", term="s1",
    )])
    service, _, _ = build_service(tmp_path, items=items, scheduler=scheduler)
    conversation = service.create_conversation()

    ambiguous = result_of(
        await run(service, conversation.id, "2024-2025学年第一学期 Python 期末试卷")
    )
    assert "请说明要哪一份" in ambiguous.assistant_message
    assert "A卷" in ambiguous.assistant_message and "B卷" in ambiguous.assistant_message

    scheduler.push(query_decision(
        course=COURSE, exam_type="final", variant="B",
    ))
    resolved = result_of(await run(service, conversation.id, "B卷"))
    assert resolved.result.total == 1
    assert resolved.result.items[0].id == "py-2024-2025-s1-final-b"

    # 学生明确换学年时，旧候选范围不能强加到新请求上。
    scheduler.push(query_decision(
        course=COURSE, exam_type="final",
        academic_year="2023-2024", term="s1", variant="A",
    ))
    switched = result_of(
        await run(service, conversation.id, "2023-2024学年第一学期 Python 期末试卷 A 卷")
    )
    assert switched.result.total == 1
    assert switched.result.items[0].id == "py-2023-2024-s1-final"


@pytest.mark.anyio
async def test_clarification_reuses_year_and_term_after_restart(tmp_path: Path) -> None:
    items = [
        paper("py-2024-2025-s1-final", "2024-2025", variant="A"),
        paper("py-2024-2025-s1-final-b", "2024-2025", variant="B"),
        paper("py-2023-2024-s1-final-b", "2023-2024", variant="B"),
    ]
    scheduler = FakeScheduler(decisions=[query_decision(
        course=COURSE, exam_type="final",
        academic_year="2024-2025", term="s1",
    )])
    service, _, _ = build_service(tmp_path, items=items, scheduler=scheduler)
    conversation = service.create_conversation()
    await run(service, conversation.id, "2024-2025学年第一学期 Python 期末试卷")

    scheduler2 = FakeScheduler()
    restarted, _, _ = build_service(tmp_path, items=items, scheduler=scheduler2)
    scheduler2.push(query_decision(
        course=COURSE, exam_type="final", variant="B",
    ))
    resolved = result_of(await run(restarted, conversation.id, "B卷"))

    assert resolved.result.total == 1
    assert resolved.result.items[0].id == "py-2024-2025-s1-final-b"


# ---------------------------------------------------------------------------
# R3：带“这道题”的跨卷请求不能被旧卷限制
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cross_paper_request_with_pronoun_keeps_no_paper(tmp_path: Path) -> None:
    own = sample_card("py-2024-2025-s1-final-choice-12", number="12")
    other = sample_card("py-2019-2020-s1-final-choice-3", number="3").model_copy(
        update={
            "source": sample_card("py-2019-2020-s1-final-choice-3").source.model_copy(
                update={"paper_id": "py-2019-2020-s1-final", "paper_title": "另一份期末试卷"}
            )
        }
    )
    items = [paper("py-2024-2025-s1-final", "2024-2025")]
    scheduler = FakeScheduler(decisions=[query_decision(
        course=COURSE, exam_type="final",
        academic_year="2024-2025", term="s1", variant="A",
    )])
    service, _, runtime = build_service(
        tmp_path, items=items, scheduler=scheduler, cards=[own, other]
    )
    conversation = service.create_conversation()
    await run(
        service,
        conversation.id,
        "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）",
    )

    scheduler.push(
        ScheduleDecision(
            actions=[
                ActionPlan(
                    action="search_questions",
                    parameters={
                        "course": COURSE,
                        "paper_id": "py-2024-2025-s1-final",
                        "number_label": "12",
                        "question_type": "choice",
                    },
                )
            ]
        )
    )
    await run(service, conversation.id, "这张卷的第 12 题")

    scheduler.push(
        ScheduleDecision(
            actions=[
                ActionPlan(
                    action="search_questions",
                    parameters={
                        "course": COURSE,
                        "paper_id": "py-2024-2025-s1-final",
                        "query": "字符串大小写转换 capitalize title upper",
                        "question_type": "choice",
                        "scope": "bank",
                        "exclude_previous": True,
                    },
                )
            ]
        )
    )
    similar = result_of(
        await run(service, conversation.id, "去题库找一道和这道题类似的选择真题")
    )

    request = runtime.search_calls[-1]["request"]
    assert request.paper_id is None, "“这道题”只是指代，不能把旧卷重新锁上"
    returned = [card.question_id for card in similar.question_results]
    assert other.question_id in returned
    assert own.question_id not in returned


# ---------------------------------------------------------------------------
# R4：下载链接必须进入正文模型的实际提示
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_download_url_reaches_reply_prompt(tmp_path: Path) -> None:
    items = [paper("py-2024-2025-s1-final", "2024-2025")]
    scheduler = FakeScheduler(decisions=[query_decision(
        course=COURSE, exam_type="final",
        academic_year="2024-2025", term="s1", variant="A",
    )])
    replies = ReplyFactory(pieces=["已找到这份试卷。"])
    service, repository, _ = build_service(
        tmp_path, items=items, scheduler=scheduler, reply_factory=replies
    )
    conversation = service.create_conversation()

    events = await run(
        service,
        conversation.id,
        "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）",
    )
    result = result_of(events)

    prompt = "\n".join(
        str(getattr(message, "content", message))
        for message in replies.models[-1].calls[-1]
    )
    url = "/api/papers/py-2024-2025-s1-final/download"
    assert url in prompt, "下载链接必须真的交给正文模型"
    assert url in result.composite_results[0].details["download_url"]

    # 恢复材料（只重做正文时读的就是它）必须与首次生成一致。
    turn = repository.find_turn(conversation.id, result.request_id)
    assert turn is not None
    recovered, _notes = material_from_context(
        service, conversation.id, turn.recovery["reply_context"]
    )
    assert url in recovered.describe()


@pytest.mark.anyio
async def test_material_without_download_url_does_not_invent_one(tmp_path: Path) -> None:
    from app.conversations.models import ActionResultItem
    from app.conversations.reply import build_material

    material = build_material(
        user_text="有没有 Python 试卷",
        course=COURSE,
        composite_results=[
            ActionResultItem(action="query_papers", status="success", message="没有找到。")
        ],
        waiting_for=None,
    )
    assert "下载入口" not in material.describe()
    assert "/api/papers/" not in material.describe()

# ---------------------------------------------------------------------------
# §7.9 追加：R1 未定位状态约束旧已选题；R2 换课程/换考试类型释放旧候选
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_missing_paper_switch_does_not_explain_old_selection(tmp_path: Path) -> None:
    """换卷失败后“讲讲这道题”不能拿旧已选题顶上。"""

    items = [paper("py-2024-2025-s1-final", "2024-2025")]
    own = sample_card("py-2024-2025-s1-final-choice-12", number="12")
    tutor = FakeTutor(explanations=[sample_explanation()])
    scheduler = FakeScheduler(decisions=[query_decision(
        course=COURSE, exam_type="final",
        academic_year="2024-2025", term="s1", variant="A",
    )])
    service, repository, _ = build_service(
        tmp_path, items=items, scheduler=scheduler, cards=[own], tutor=tutor
    )
    conversation = service.create_conversation()
    await run(
        service,
        conversation.id,
        "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）",
    )

    scheduler.push(
        ScheduleDecision(
            actions=[
                ActionPlan(
                    action="search_questions",
                    parameters={"course": COURSE, "paper_id": "py-2024-2025-s1-final"},
                )
            ]
        )
    )
    await run(service, conversation.id, "这张卷的选择题")
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="select_question", parameters={"index": 1})]
        )
    )
    selected = result_of(await run(service, conversation.id, "就讲这道题"))
    assert selected.question is not None, "先要选中一道题才能复现"
    assert repository.get_active_question(conversation.id) is not None

    scheduler.push(query_decision(
        course=COURSE, exam_type="final",
        academic_year="2019-2020", term="s1",
    ))
    missing = result_of(
        await run(service, conversation.id, "换成 2019-2020 学年第一学期 Python 期末试卷")
    )
    assert missing.result.kind == "empty"

    # 刷新/重启后同一路径仍不能讲解旧题。
    tutor2 = FakeTutor(explanations=[sample_explanation()])
    scheduler2 = FakeScheduler()
    restarted, _, _ = build_service(
        tmp_path, items=items, scheduler=scheduler2, cards=[own], tutor=tutor2
    )
    scheduler2.push(
        ScheduleDecision(actions=[ActionPlan(action="explain_problem", parameters={})])
    )
    explained = result_of(await run(restarted, conversation.id, "讲讲这道题"))

    assert tutor2.explain_calls == [], "没有确定试卷时不能直接讲解旧题"
    assert explained.tutoring is None
    assert any(
        word in explained.assistant_message
        for word in ("哪份试卷", "哪一份", "先说", "没有登记")
    )


@pytest.mark.anyio
async def test_clarification_drops_candidates_when_course_changes(tmp_path: Path) -> None:
    """A/B 澄清期间换课程：不能套用旧课程的学年、学期和候选。"""

    items = [
        paper("py-2024-2025-s1-final", "2024-2025", variant="A"),
        paper("py-2024-2025-s1-final-b", "2024-2025", variant="B"),
        PaperItem(
            id="math-2023-2024-s2-final",
            title="2023-2024学年第二学期《高等数学》期末试卷（A卷）",
            course="高等数学",
            academic_year="2023-2024",
            exam_type="final",
            exam_date=None,
            download_url="/api/papers/math-2023-2024-s2-final/download",
        ),
    ]
    scheduler = FakeScheduler(decisions=[query_decision(
        course=COURSE, exam_type="final",
        academic_year="2024-2025", term="s1",
    )])
    service, _, _ = build_service(tmp_path, items=items, scheduler=scheduler)
    conversation = service.create_conversation()
    await run(service, conversation.id, "2024-2025学年第一学期 Python 期末试卷")

    scheduler.push(query_decision(course="高等数学", exam_type="final"))
    switched = result_of(
        await run(service, conversation.id, "不看 Python 了，给我高等数学期末试卷")
    )
    assert switched.result.kind == "found"
    assert switched.result.items[0].id == "math-2023-2024-s2-final"


@pytest.mark.anyio
async def test_clarification_drops_candidates_when_exam_type_changes(
    tmp_path: Path,
) -> None:
    """期末 A/B 澄清期间改看期中：旧期末候选不能排除合法的期中卷。"""

    items = [
        paper("py-2024-2025-s1-final", "2024-2025", variant="A"),
        paper("py-2024-2025-s1-final-b", "2024-2025", variant="B"),
        PaperItem(
            id="py-2024-2025-s1-midterm-b",
            title="2024-2025学年第一学期《Python语言程序设计》期中试卷（B卷）",
            course=COURSE,
            academic_year="2024-2025",
            exam_type="midterm",
            exam_date=None,
            download_url="/api/papers/py-2024-2025-s1-midterm-b/download",
        ),
    ]
    scheduler = FakeScheduler(decisions=[query_decision(
        course=COURSE, exam_type="final",
        academic_year="2024-2025", term="s1",
    )])
    service, _, _ = build_service(tmp_path, items=items, scheduler=scheduler)
    conversation = service.create_conversation()
    await run(service, conversation.id, "2024-2025学年第一学期 Python 期末试卷")

    scheduler.push(query_decision(course=COURSE, exam_type="midterm"))
    switched = result_of(await run(service, conversation.id, "改看期中 B 卷"))
    assert switched.result.kind == "found"
    assert switched.result.items[0].id == "py-2024-2025-s1-midterm-b"


# ---------------------------------------------------------------------------
# §7.10 追加：R2a 合并后的学年/学期必须真正参与定位；R2b 显式取消类型
# ---------------------------------------------------------------------------


def midterm(paper_id: str, year: str, variant: str = "B", term: str = "s1") -> PaperItem:
    label = "第一学期" if term == "s1" else "第二学期"
    return PaperItem(
        id=paper_id,
        title=f"{year}学年{label}《Python语言程序设计》期中试卷（{variant}卷）",
        course=COURSE,
        academic_year=year,
        exam_type="midterm",
        exam_date=None,
        download_url=f"/api/papers/{paper_id}/download",
    )


@pytest.mark.anyio
async def test_exam_type_change_keeps_inherited_year_and_term(tmp_path: Path) -> None:
    """“改看期中B卷”必须用合并后的学年学期定位，不能重新只解析原话。"""

    items = [
        paper("py-2024-2025-s1-final", "2024-2025", variant="A"),
        paper("py-2024-2025-s1-final-b", "2024-2025", variant="B"),
        midterm("py-2024-2025-s1-midterm-b", "2024-2025"),
        midterm("py-2023-2024-s1-midterm-b", "2023-2024"),
    ]
    scheduler = FakeScheduler(decisions=[query_decision(
        course=COURSE, exam_type="final",
        academic_year="2024-2025", term="s1",
    )])
    service, _, _ = build_service(tmp_path, items=items, scheduler=scheduler)
    conversation = service.create_conversation()
    await run(service, conversation.id, "2024-2025学年第一学期 Python 期末试卷")

    scheduler.push(query_decision(course=COURSE, exam_type="midterm"))
    switched = result_of(await run(service, conversation.id, "改看期中B卷"))

    assert switched.result.kind == "found", "继承的学年学期必须真正参与匹配"
    assert switched.result.items[0].id == "py-2024-2025-s1-midterm-b"


@pytest.mark.anyio
async def test_explicit_unlimited_exam_type_clears_old_type(tmp_path: Path) -> None:
    """“不限期中期末”要清掉旧期末范围，B 卷候选应同时包含期中与期末。"""

    items = [
        paper("py-2024-2025-s1-final", "2024-2025", variant="A"),
        paper("py-2024-2025-s1-final-b", "2024-2025", variant="B"),
        midterm("py-2024-2025-s1-midterm-b", "2024-2025"),
    ]
    scheduler = FakeScheduler(decisions=[query_decision(
        course=COURSE, exam_type="final",
        academic_year="2024-2025", term="s1",
    )])
    service, repository, _ = build_service(tmp_path, items=items, scheduler=scheduler)
    conversation = service.create_conversation()
    await run(service, conversation.id, "2024-2025学年第一学期 Python 期末试卷")

    scheduler.push(query_decision(course=COURSE, all_exam_types=True, variant="B"))
    unlimited = result_of(
        await run(service, conversation.id, "不限期中期末，B卷都列出来")
    )

    state = repository.find_paper_state(conversation.id)
    assert state is not None and state.get("status") == "clarify_paper"
    candidates = set(state.get("candidates") or [])
    assert candidates == {"py-2024-2025-s1-final-b", "py-2024-2025-s1-midterm-b"}
    assert "2 份" in unlimited.assistant_message or "哪一份" in unlimited.assistant_message
