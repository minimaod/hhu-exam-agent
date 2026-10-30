"""F008 返修：按上下文继续处理，而不是“先选题→作答→才讲解”。

覆盖四类真实体验问题：
1. 指定整卷/题型时一次给出该范围全部题目，并说明总数与本次范围；
2. 学生说“继续/下一批”时从持久位置往后翻，刷新或重启后仍然接着来；
3. “第 5 题”按原卷题号定位，不是本次列表的第 5 个；
4. 查题结果只是中间事实，学生问考点或要讲解时直接处理；只有学生要求
   自己先做时才等待作答。
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.reply import material_from_payload, material_to_payload
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.papers.models import PaperItem, PaperListResponse
from app.tutoring.models import Source
from tests.question_fakes import FakeQuestionRuntime, sample_card
from tests.tool_fakes import ReplyFactory
from tests.tutoring_fakes import FakeScheduler, FakeTutor, sample_explanation

TITLE = "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）"
PAPER = "py-2024-2025-s1-final"
COURSE = "Python语言程序设计"


def paper_cards(count: int = 40):
    return [
        sample_card(
            f"{PAPER}-choice-{number}",
            number=str(number),
            statement=f"第 {number} 题题干：下列表达式的值是？",
        )
        for number in range(1, count + 1)
    ]


class FakePapers:
    """只实现服务层用到的目录接口；不读磁盘。"""

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


def paper_item() -> PaperItem:
    return PaperItem(
        id=PAPER,
        title=TITLE,
        course=COURSE,
        academic_year="2024-2025",
        exam_type="final",
        exam_date=None,
        download_url=f"/api/papers/{PAPER}/download",
    )


def build_service(
    tmp_path: Path,
    *,
    cards=None,
    scheduler=None,
    tutor=None,
    reply_factory=None,
    db_name: str = "study.sqlite3",
) -> tuple[ConversationService, ConversationRepository, FakeQuestionRuntime]:
    repository = ConversationRepository(tmp_path / db_name)
    repository.initialize()
    runtime = FakeQuestionRuntime(cards=cards if cards is not None else paper_cards())
    service = ConversationService(
        repository=repository,
        make_query=None,
        query_error="模型未配置",
        make_tutor=(lambda: tutor) if tutor is not None else None,
        tutoring_error=None if tutor is not None else "模型未配置",
        make_scheduler=(lambda: scheduler) if scheduler is not None else None,
        make_reply=reply_factory,
        papers=FakePapers([paper_item()]),
        question_runtime=runtime,
        attachments_root=tmp_path / "attachments",
    )
    return service, repository, runtime


def seed_page(
    repository: ConversationRepository,
    conversation_id: str,
    *,
    offset: int = 0,
    returned: int = 40,
    next_offset: int | None = None,
    question_type: str = "choice",
    cards: list | None = None,
) -> str:
    """写入上一轮的试卷上下文，模拟“刚刚已经列过一页”。"""

    request_id = str(uuid4())
    repository.start_turn(conversation_id, request_id, "这张卷的选择题", kind="dynamic")
    repository.complete_dynamic_turn(
        conversation_id,
        request_id,
        kind="dynamic",
        assistant_message="已列出题目",
        question_results=list(cards or []),
        question_result_status="ok",
        question_context={
            "paper_id": PAPER,
            "paper_title": TITLE,
            "question_type": question_type,
            "total": 40,
            "offset": offset,
            "returned": returned,
            "next_offset": next_offset,
            "limit": returned,
            "request_id": request_id,
        },
    )
    return request_id


def turn_input(*, text: str, kind: str | None = None) -> TurnInput:
    return TurnInput(
        request_id=str(uuid4()),
        kind=kind,
        text=text,
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(type="none"),
        problem_id=None,
        start_new=False,
        dispatch=True,
    )


async def run(service: ConversationService, conversation_id: str, data: TurnInput):
    prepared = service.prepare_turn(conversation_id, data)
    return [event async for event in service.stream_prepared(prepared, NeverDisconnected())]


class NeverDisconnected:
    async def is_disconnected(self) -> bool:
        return False


def search_decision(**parameters):
    return ScheduleDecision(
        actions=[ActionPlan(action="search_questions", parameters={"course": "Python", **parameters})]
    )


def result_of(events) -> object:
    return next(event for event in events if event.type == "result")


def test_public_material_preserves_verified_label_status() -> None:
    from types import SimpleNamespace
    from app.questions.models import QuestionKnowledgePoint

    card = sample_card().model_copy(update={"knowledge_points": [
        QuestionKnowledgePoint(slug="loops", name="分支与循环", status="verified"),
        QuestionKnowledgePoint(slug="candidate", name="候选知识点", status="candidate"),
    ]})
    material = "\n".join(ConversationService._question_materials(SimpleNamespace(
        question_card=card, question_results=[],
    )))
    assert "已审核知识点：分支与循环" in material
    assert "题库候选知识点（未审核，仅供参考）：候选知识点" in material


@pytest.mark.parametrize("text", ["我选这张卷的选择题第5题", "找一道Python真题", "先让我自己做，别给答案"])
@pytest.mark.anyio
async def test_preview_and_old_reply_material_cannot_supply_a_solution(tmp_path: Path, text: str) -> None:
    from app.conversations.reply import ReplyMaterial

    service, _, _ = build_service(tmp_path)
    factory = ReplyFactory(["已定位到指定题目。"])
    # 模拟旧版本已落盘、包含过多内容的回复材料；正文再生成同样要做投影。
    material = ReplyMaterial(
        user_text=text, question_materials=("OLD-SOLVABLE-STATEMENT",),
        context_summary="OLD-SOLUTION-IN-HISTORY",
    )
    reply, _ = await service._stream_reply(factory(), material)
    prompt = "\n".join(str(m) for call in factory.models[0].calls for m in call)
    assert "OLD-SOLVABLE-STATEMENT" not in prompt
    assert "OLD-SOLUTION-IN-HISTORY" not in prompt
    assert text in prompt and reply == "已定位到指定题目。"


@pytest.mark.anyio
async def test_selection_ack_confirms_only_real_target_without_reply_model(tmp_path: Path) -> None:
    factory = ReplyFactory(["不得生成这段答案或猜测总题数。"])
    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
        ActionPlan(action="select_question", parameters={"number_label": "5"}),
    ])])
    service, repository, _ = build_service(tmp_path, scheduler=scheduler, reply_factory=factory)
    cid = service.create_conversation().id
    seed_page(repository, cid, cards=[paper_cards()[4]], returned=1)
    result = result_of(await run(service, cid, turn_input(text="我选这张卷的选择题第5题")))
    assert "已选中" in result.assistant_message and "第 5 题" in result.assistant_message
    assert "总" not in result.assistant_message and "答案" not in result.assistant_message
    assert all(not model.calls for model in factory.models)
    assert result.business_status == "succeeded"
    assert result.reply_status is None


@pytest.mark.parametrize("text", [
    "这段代码考什么知识点",
    "解释一下这段代码考查哪些知识点",
    "这段解题过程主要考什么知识点",
])
@pytest.mark.anyio
async def test_topic_referent_is_not_permission_to_solve(tmp_path: Path, text: str) -> None:
    """代码/过程是提问对象，不是要求公开代码答案或解题过程。"""
    card = sample_card(statement="循环输出有哪些特点？").model_copy(update={
        "question_type": "code", "options": [],
        "code_lines": ["for i in range(3):", "    print(i)"],
    })
    scheduler = FakeScheduler(decisions=[
        ScheduleDecision(actions=[ActionPlan(action="select_question", parameters={"index": 1})]),
        ScheduleDecision(actions=[ActionPlan(action="explain_problem", parameters={})]),
    ])
    tutor = FakeTutor(explanations=[sample_explanation("DO-NOT-REVEAL")])
    factory = ReplyFactory(["这段代码考查循环、range 的边界以及缩进控制的循环体。"])
    service, repository, _ = build_service(
        tmp_path, cards=[card], scheduler=scheduler, tutor=tutor, reply_factory=factory,
    )
    cid = service.create_conversation().id
    seed_page(repository, cid, cards=[card], question_type="code", returned=1)
    result = result_of(await run(service, cid, turn_input(text=text)))
    assert tutor.explain_calls == []
    assert repository.get_active_problem(cid) is None
    selection = repository.get_active_question(cid)
    assert selection is None or selection.answer_revealed_at is None
    assert result.tutoring is None
    assert result.business_status == "succeeded"
    prompt = "\n".join(str(m) for call in factory.models[0].calls for m in call)
    assert "只回答考点" in prompt and "for i in range(3)" in prompt
    assert "DO-NOT-REVEAL" not in prompt
    assert "循环" in result.assistant_message


def two_papers(service, runtime):
    old = paper_cards()[4]
    other_id = "py-2023-2024-s1-final"
    title = "2023-2024学年第一学期《Python语言程序设计》期末试卷（A卷）"
    new = old.model_copy(update={
        "question_id": other_id + "-choice-5", "statement": "另一份卷的第5题，禁止串到旧卷。",
        "source": old.source.model_copy(update={
            "paper_id": other_id, "paper_title": title, "academic_year": "2023-2024",
            "download_url": f"/api/papers/{other_id}/download",
        }),
    })
    service._papers = FakePapers([paper_item(), paper_item().model_copy(update={
        "id": other_id, "title": title, "academic_year": "2023-2024",
        "download_url": f"/api/papers/{other_id}/download",
    })])
    runtime.cards = [old, new]
    return old, new


@pytest.mark.parametrize("action", ["select_question", "explain_problem"])
@pytest.mark.anyio
async def test_all_target_entries_honour_explicit_new_paper(tmp_path: Path, action: str) -> None:
    """选题与讲解共用目标约束；新卷相同题号不能命中旧卷。"""
    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[ActionPlan(
        action=action, parameters={"number_label": "5"} if action == "select_question" else {},
    )])])
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler, tutor=tutor)
    old, new = two_papers(service, runtime)
    cid = service.create_conversation().id
    seed_page(repository, cid, cards=[old], returned=1)
    text = ("我选" if action == "select_question" else "讲讲") + new.source.paper_title + "的选择题第5题"
    data = turn_input(text=text)
    result = result_of(await run(service, cid, data))
    selected = repository.get_active_question(cid)
    assert selected.question_id == new.question_id
    assert result.question.source.paper_id == new.source.paper_id
    assert repository.find_question_context(cid)["paper_id"] == new.source.paper_id
    assert runtime.search_calls[-1]["request"].paper_id == new.source.paper_id
    assert new.source.paper_id in scheduler.calls[-1]["question_context"]
    if action == "explain_problem":
        assert new.statement in tutor.explain_calls[0]["statement"]
        assert old.statement not in tutor.explain_calls[0]["statement"]
    count = len(tutor.explain_calls)
    await run(service, cid, data)
    assert len(tutor.explain_calls) == count


@pytest.mark.parametrize("action", ["select_question", "explain_problem"])
@pytest.mark.anyio
async def test_all_target_entries_require_a_unique_match(tmp_path: Path, action: str) -> None:
    """同页存在同卷不同题型第5题，两个入口都必须澄清，不能取第一道。"""
    cards = [card for card in mixed_type_cards() if card.number_label == "5"]
    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[ActionPlan(
        action=action, parameters={"number_label": "5"} if action == "select_question" else {},
    )])])
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, repository, _ = build_service(tmp_path, cards=cards, scheduler=scheduler, tutor=tutor)
    cid = service.create_conversation().id
    seed_page(repository, cid, cards=cards, question_type=None, returned=2)
    result = result_of(await run(service, cid, turn_input(text=(
        "我选这张卷的第5题" if action == "select_question" else "讲讲这张卷的第5题"
    ))))
    assert "题型" in result.assistant_message
    assert result.business_status == "needs_input"
    assert result.question_results == []  # 旧题卡保留在原轮次，澄清时不复制成新结果
    assert repository.get_active_question(cid) is None
    assert repository.get_active_problem(cid) is None
    assert tutor.explain_calls == []


@pytest.mark.parametrize("action", ["select_question", "explain_problem"])
@pytest.mark.anyio
async def test_unresolved_new_paper_cannot_fall_back_to_old_card(tmp_path: Path, action: str) -> None:
    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[ActionPlan(
        action=action, parameters={"number_label": "5"} if action == "select_question" else {},
    )])])
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler, tutor=tutor)
    cid = service.create_conversation().id
    seed_page(repository, cid, cards=[paper_cards()[4]], returned=1)
    text = ("我选" if action == "select_question" else "讲讲") + "2030-2031学年第一学期期末卷的选择题第5题"
    result = result_of(await run(service, cid, turn_input(text=text)))
    assert result.business_status == "needs_input"
    assert repository.get_active_question(cid) is None
    assert not runtime.search_calls and not tutor.explain_calls


@pytest.mark.anyio
async def test_whole_paper_choices_listed_in_one_page(tmp_path: Path) -> None:
    """指定“这张卷的选择题”就给全 40 道，并说明总数与范围。"""

    scheduler = FakeScheduler(decisions=[search_decision()])
    service, _, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()

    events = await run(service, conversation.id, turn_input(text=f"{TITLE}的选择题"))
    result = result_of(events)

    request = runtime.search_calls[0]["request"]
    assert request.paper_id == PAPER
    assert request.limit == 64  # 整卷该题型一次给全，不是 3 道预览
    assert len(result.question_results) == 40
    message = result.composite_results[0].message
    assert "共 40 道" in message and "全部列出" in message
    assert result.business_status == "succeeded"


@pytest.mark.anyio
async def test_explicit_count_pages_and_reports_remaining(tmp_path: Path) -> None:
    """“前五题”只显示 5 道，但必须告诉学生还有多少没显示。"""

    scheduler = FakeScheduler(decisions=[search_decision()])
    service, repository, _ = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, offset=0, returned=40, next_offset=None)

    events = await run(service, conversation.id, turn_input(text="刚刚那张试卷的选择题前五题"))
    result = result_of(events)

    assert [card.number_label for card in result.question_results] == ["1", "2", "3", "4", "5"]
    message = result.composite_results[0].message
    assert "前五题" not in message or "共 40 道" in message
    assert "本次显示第 1–5 道" in message
    assert "还有 35 道" in message


@pytest.mark.anyio
async def test_all_choices_request_after_context_returns_everything(tmp_path: Path) -> None:
    """“给出全部选择题”：沿用刚刚那张卷，一次给全 40 道，不再回“没有找到”。"""

    scheduler = FakeScheduler(decisions=[search_decision()])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    seed_page(repository, conversation.id)

    events = await run(service, conversation.id, turn_input(text="给出全部选择题"))
    result = result_of(events)

    request = runtime.search_calls[0]["request"]
    assert request.paper_id == PAPER
    assert request.query == ""
    assert request.limit == 64
    assert len(result.question_results) == 40
    assert "共 40 道" in result.composite_results[0].message


@pytest.mark.anyio
async def test_continue_resumes_from_saved_offset(tmp_path: Path) -> None:
    """“继续”从上次留下的位置往后翻，不重放已经看过的题。"""

    scheduler = FakeScheduler(decisions=[search_decision()])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, offset=0, returned=5, next_offset=5)

    events = await run(service, conversation.id, turn_input(text="继续看下一批选择题"))
    result = result_of(events)

    request = runtime.search_calls[0]["request"]
    assert request.offset == 5
    assert [card.number_label for card in result.question_results] == ["6", "7", "8", "9", "10"]
    assert result.composite_results[0].details["next_offset"] == 10


@pytest.mark.anyio
async def test_continue_after_restart_uses_persisted_offset(tmp_path: Path) -> None:
    """刷新、重启后仍从库里恢复位置，而不是从头再来。"""

    scheduler = FakeScheduler(decisions=[search_decision()])
    service, repository, _ = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, offset=5, returned=5, next_offset=10)
    service.close()

    # 新建服务实例 = 新的进程：上下文只能来自持久化记录。
    other, _, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation_id = conversation.id
    events = await run(other, conversation_id, turn_input(text="接着看这张卷的选择题"))
    result = result_of(events)

    assert runtime.search_calls[0]["request"].offset == 10
    assert [card.number_label for card in result.question_results[:2]] == ["11", "12"]


@pytest.mark.anyio
async def test_original_number_is_not_page_position(tmp_path: Path) -> None:
    """“第 25 题”按原卷题号定位，与本次列表位置无关。"""

    scheduler = FakeScheduler(decisions=[search_decision()])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, offset=20, returned=5, next_offset=25)

    events = await run(service, conversation.id, turn_input(text="这份卷子第 25 题是什么？"))
    result = result_of(events)

    request = runtime.search_calls[0]["request"]
    assert request.number_label == "25"
    assert [card.number_label for card in result.question_results] == ["25"]


@pytest.mark.anyio
async def test_empty_search_keeps_previous_paper_context(tmp_path: Path) -> None:
    """查无结果不能把“刚刚那张卷”的指向清掉。"""

    scheduler = FakeScheduler(decisions=[search_decision(), search_decision()])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    runtime.search_status = "no_match"
    runtime.search_message = "没有找到相关真题"
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, cards=[sample_card()])

    first = result_of(await run(service, conversation.id, turn_input(text="找找和量子力学有关的题")))
    assert first.question_results == []
    events = await run(service, conversation.id, turn_input(text="这张卷的填空题呢"))
    result = result_of(events)

    assert runtime.search_calls[0]["request"].paper_id == PAPER
    assert runtime.search_calls[1]["request"].paper_id == PAPER
    assert result.question_results == []
    # 查无结果不覆盖已记录的试卷指向。
    assert repository.find_question_context(conversation.id)["paper_id"] == PAPER


@pytest.mark.anyio
async def test_list_paper_questions_tool_reuses_current_paper(tmp_path: Path) -> None:
    """浏览工具不要求模型重复试卷 ID，也不替学生决定看哪道。"""

    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
        ActionPlan(action="list_paper_questions", parameters={"question_type": "choice"})
    ])])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, offset=0, returned=5, next_offset=5)

    events = await run(service, conversation.id, turn_input(text="继续看这张卷的选择题"))
    result = result_of(events)

    request = runtime.search_calls[0]["request"]
    assert request.paper_id == PAPER
    assert request.offset == 5
    assert request.question_type == "choice"
    assert result.question is None  # 没有替学生选题
    assert result.composite_results[0].status == "success"


@pytest.mark.anyio
async def test_select_question_prefers_original_number(tmp_path: Path) -> None:
    """列表里第 5 个不是“第 5 题”，选题也要按原卷题号。"""

    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
        ActionPlan(action="select_question", parameters={"number_label": "25"})
    ])])
    service, repository, _ = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, offset=20, returned=5, next_offset=25, cards=paper_cards())

    events = await run(service, conversation.id, turn_input(text="我选第 25 题"))
    result = result_of(events)

    assert result.question is not None
    assert result.question.number_label == "25"


@pytest.mark.anyio
async def test_try_first_still_waits_for_student(tmp_path: Path) -> None:
    """学生说“先自己做”时，讲解与答案仍然要等。"""

    scheduler = FakeScheduler(decisions=[
        search_decision(number_label="1", question_type="choice"),
        ScheduleDecision(actions=[ActionPlan(action="explain_problem", parameters={})]),
    ])
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, _, _ = build_service(tmp_path, scheduler=scheduler, tutor=tutor)
    conversation = service.create_conversation()

    events = await run(
        service, conversation.id, turn_input(text="这份卷的第一道选择题先自己做，别给答案")
    )
    result = result_of(events)

    assert result.tutoring is None
    assert tutor.explain_calls == []
    assert result.business_status in {"needs_input", "partial"}


@pytest.mark.anyio
async def test_numbered_topic_question_answers_from_context_without_waiting(tmp_path: Path) -> None:
    """“给出刚刚那张试卷的选择题第五题，主要查哪个知识点”直接答，不要求先作答。"""

    scheduler = FakeScheduler(decisions=[search_decision()])
    factory = ReplyFactory(["第 5 题主要考列表索引与切片。"])
    service, repository, runtime = build_service(
        tmp_path, scheduler=scheduler, reply_factory=factory
    )
    conversation = service.create_conversation()
    seed_page(repository, conversation.id)

    events = await run(
        service,
        conversation.id,
        turn_input(text="给出刚刚那张试卷的选择题第五题，主要查哪个知识点"),
    )
    result = result_of(events)

    request = runtime.search_calls[0]["request"]
    assert request.paper_id == PAPER
    assert request.number_label == "5"
    assert request.question_type == "choice"
    assert [card.number_label for card in result.question_results] == ["5"]
    assert not any(item.action == "ask_user" for item in result.composite_results)
    assert result.business_status == "succeeded"

    prompt = "\n".join(str(message) for call in factory.models[0].calls for message in call)
    assert "第 5 题题干" in prompt
    assert "B（测试答案）" not in prompt


@pytest.mark.anyio
async def test_hint_only_request_does_not_reveal_answer(tmp_path: Path) -> None:
    """“只给提示，先别公布答案”：不讲解、不公开答案，材料里也没有答案。"""

    scheduler = FakeScheduler(decisions=[
        search_decision(number_label="1", question_type="choice"),
        ScheduleDecision(actions=[ActionPlan(action="explain_problem", parameters={})]),
    ])
    factory = ReplyFactory(["先看下标越界的边界条件。"])
    tutor = FakeTutor(explanations=[sample_explanation("B")])
    service, _, _ = build_service(
        tmp_path, scheduler=scheduler, tutor=tutor, reply_factory=factory
    )
    conversation = service.create_conversation()

    events = await run(
        service,
        conversation.id,
        turn_input(text="这份卷第一道选择题只给提示，先别公布答案"),
    )
    result = result_of(events)

    assert tutor.explain_calls == []
    assert result.tutoring is None
    turn = service._repository.list_turns(conversation.id)[-1]
    payload = (turn.recovery or {}).get("reply_input")
    text = material_from_payload(payload).describe() if payload else (turn.assistant_message or "")
    assert "B（测试答案）" not in text
    assert result.business_status == "succeeded"


@pytest.mark.anyio
async def test_public_material_lets_reply_answer_without_student_answer(tmp_path: Path) -> None:
    """只问考点时，正文材料里有题干与知识点，但不含答案。"""

    scheduler = FakeScheduler(decisions=[search_decision(number_label="1", question_type="choice")])
    factory = ReplyFactory(["这道题主要考 Python 表达式求值。"])
    service, _, _ = build_service(tmp_path, scheduler=scheduler, reply_factory=factory)
    conversation = service.create_conversation()

    await run(service, conversation.id, turn_input(text="这份卷第一道选择题考什么知识点"))
    prompt = "\n".join(str(message) for call in factory.models[0].calls for message in call)

    assert "第 1 题题干" in prompt
    assert "【本轮题目材料" in prompt
    assert "B（测试答案）" not in prompt


@pytest.mark.anyio
async def test_reply_material_round_trip_keeps_page_and_materials(tmp_path: Path) -> None:
    """只重做浏览正文时，保留分页事实，题面留在卡片中。"""

    scheduler = FakeScheduler(decisions=[search_decision()])
    factory = ReplyFactory(["这次列出第 1 到 40 道。"])
    service, _, _ = build_service(tmp_path, scheduler=scheduler, reply_factory=factory)
    conversation = service.create_conversation()

    await run(service, conversation.id, turn_input(text=f"{TITLE}的选择题"))
    turn = service._repository.list_turns(conversation.id)[-1]
    stored = (turn.recovery or {}).get("reply_input")
    assert stored is not None
    material = material_from_payload(stored)
    assert not material.question_materials  # 纯浏览只交给正文数量与范围，题面保留在卡片
    assert material.question_page is not None
    assert material.question_page["total"] == 40
    assert "共 40 道" in material.describe()
    assert material_to_payload(material)["question_page"] == material.question_page


def test_page_description_states_remaining_and_exhaustion() -> None:
    """分页事实的措辞：还有多少、是否已经列完，都要说清楚。"""

    from app.conversations.reply import ReplyMaterial

    partial = ReplyMaterial(
        user_text="这张卷的选择题",
        question_page={
            "paper_title": TITLE,
            "question_type": "choice",
            "total": 40,
            "offset": 10,
            "returned": 10,
            "next_offset": 20,
        },
    ).describe()
    assert "共 40 道" in partial and "第 11–20 道" in partial and "还有 20 道" in partial

    done = ReplyMaterial(
        user_text="这张卷的选择题",
        question_page={
            "paper_title": TITLE,
            "question_type": "choice",
            "total": 40,
            "offset": 30,
            "returned": 10,
            "next_offset": None,
        },
    ).describe()
    assert "已经全部显示" in done
    assert json.dumps(ReplyMaterial(user_text="x").describe(), ensure_ascii=False)

# --- 第二轮复核（2026-10-30）：提示/考点/题号/分页/上下文/换一道 ---


def topic_then_explain(*, number: str | None = None) -> FakeScheduler:
    """调度先查题、再（错误地）要求讲解。"""

    parameters: dict = {"course": "Python"}
    if number is not None:
        parameters["number_label"] = number
    return FakeScheduler(decisions=[
        ScheduleDecision(actions=[ActionPlan(action="search_questions", parameters=parameters)]),
        ScheduleDecision(actions=[ActionPlan(action="explain_problem", parameters={})]),
    ])


@pytest.mark.anyio
async def test_hint_request_really_returns_a_hint(tmp_path: Path) -> None:
    """“只给提示”必须真的给出与题目相关的提示，而不只是拒绝讲解。"""

    scheduler = topic_then_explain(number="1")
    factory = ReplyFactory(["先看选项里的类型：A 是字符串，B 是整数，判断表达式里哪个参与运算。"])
    service, repository, _ = build_service(
        tmp_path, scheduler=scheduler, tutor=FakeTutor(), reply_factory=factory
    )
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, offset=0, returned=1, next_offset=1)

    events = await run(
        service, conversation.id, turn_input(text="第一题只给提示，先别公布答案")
    )
    result = result_of(events)

    assert factory.models, "只给提示也要生成正文，不能只回一句固定话术"
    prompt = "\n".join(str(m) for call in factory.models[0].calls for m in call)
    assert "只给提示" in prompt
    assert "第 1 题题干：下列表达式的值是？" in prompt
    assert "B（测试答案）" not in prompt
    assert "先看选项里的类型" in (result.assistant_message or "")


@pytest.mark.anyio
async def test_topic_question_answers_without_opening_answer(tmp_path: Path) -> None:
    """“第一题考啥”只回答考点：不建讲解记录，也不把答案标成已公开。"""

    scheduler = topic_then_explain(number="1")
    factory = ReplyFactory(["这道题考的是 Python 表达式求值，以及字符串和整数的区别。"])
    tutor = FakeTutor()
    service, repository, _ = build_service(
        tmp_path, scheduler=scheduler, tutor=tutor, reply_factory=factory
    )
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, offset=0, returned=1, next_offset=1)

    events = await run(service, conversation.id, turn_input(text="第一题考啥"))
    result = result_of(events)

    assert tutor.explain_calls == []
    assert repository.get_active_problem(conversation.id) is None
    selection = repository.get_active_question(conversation.id)
    assert selection is None or selection.answer_revealed_at is None
    assert result.tutoring is None
    prompt = "\n".join(str(m) for call in factory.models[0].calls for m in call)
    assert "只回答考点" in prompt
    assert "B（测试答案）" not in prompt


@pytest.mark.anyio
async def test_off_page_original_number_is_located_not_misclicked(tmp_path: Path) -> None:
    """展示第 21–25 题时说“我选第 5 题”：按卷内题号定位第 5 题，不能选第 25 题。"""

    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
        ActionPlan(action="select_question", parameters={"number_label": "5"})
    ])])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    seed_page(
        repository,
        conversation.id,
        offset=20,
        returned=5,
        next_offset=25,
        cards=paper_cards()[20:25],
    )

    events = await run(service, conversation.id, turn_input(text="我选第 5 题"))
    result = result_of(events)

    selection = repository.get_active_question(conversation.id)
    assert selection is not None
    assert selection.number_label == "5"
    assert selection.number_label != "25"
    assert runtime.search_calls[-1]["request"].number_label == "5"
    assert "第 5 题" in result.composite_results[0].message


@pytest.mark.anyio
async def test_continue_after_everything_listed_does_not_restart(tmp_path: Path) -> None:
    """40 道已全部列出后说“继续”：说明已经列完，不从第 1 题重放。"""

    scheduler = FakeScheduler(decisions=[search_decision()])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, offset=0, returned=40, next_offset=None)

    events = await run(service, conversation.id, turn_input(text="继续"))
    result = result_of(events)

    assert runtime.search_calls == []
    message = result.composite_results[0].message
    assert "已经全部列完" in message and "40" in message


@pytest.mark.anyio
async def test_continue_with_new_type_starts_from_first_question(tmp_path: Path) -> None:
    """看完五道选择题后说“继续看填空题”：新题型从第 1 道开始，不沿用旧位置。"""

    scheduler = FakeScheduler(decisions=[search_decision(question_type="fill")])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, offset=0, returned=5, next_offset=5)

    await run(service, conversation.id, turn_input(text="继续看填空题"))

    request = runtime.search_calls[0]["request"]
    assert request.question_type == "fill"
    assert request.offset == 0


@pytest.mark.anyio
async def test_legacy_session_without_context_still_knows_the_paper(tmp_path: Path) -> None:
    """返修前的旧会话只有题卡：也能据此定位第 5 题，不要求学生补年份。"""

    scheduler = FakeScheduler(decisions=[
        ScheduleDecision(actions=[ActionPlan(action="explain_problem", parameters={})])
    ])
    service, repository, runtime = build_service(
        tmp_path, scheduler=scheduler, tutor=FakeTutor(explanations=[sample_explanation()])
    )
    conversation = service.create_conversation()
    request_id = str(uuid4())
    repository.start_turn(conversation.id, request_id, "这张卷的选择题", kind="dynamic")
    repository.complete_dynamic_turn(
        conversation.id,
        request_id,
        kind="dynamic",
        assistant_message="已列出题目",
        question_results=paper_cards()[20:25],
        question_result_status="ok",
    )

    await run(service, conversation.id, turn_input(text="讲讲第 5 题"))

    located = [
        call["request"] for call in runtime.search_calls
        if getattr(call["request"], "number_label", None) == "5"
    ]
    assert located, "应当按卷内题号定位，而不是要求补充年份/学期"
    assert located[0].paper_id == PAPER


@pytest.mark.anyio
async def test_switching_course_drops_previous_paper(tmp_path: Path) -> None:
    """明确改问高数时不带旧 Python 试卷 ID。"""

    scheduler = FakeScheduler(decisions=[
        ScheduleDecision(actions=[
            ActionPlan(action="search_questions", parameters={"course": "高数"})
        ])
    ])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, offset=0, returned=5, next_offset=5)

    await run(service, conversation.id, turn_input(text="我要复习高数，找一道期末真题"))

    request = runtime.search_calls[0]["request"]
    assert request.paper_id is None
    assert request.course != COURSE


@pytest.mark.anyio
async def test_swap_question_returns_an_unseen_one(tmp_path: Path) -> None:
    """“换一道”要排除已经展示过的题，不能原样返回同一道。"""

    scheduler = FakeScheduler(decisions=[search_decision()])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()

    first = result_of(await run(service, conversation.id, turn_input(text="找一道选择题")))
    assert first.question_results[0].number_label == "1"

    scheduler.push(search_decision())
    second = result_of(await run(service, conversation.id, turn_input(text="换一道")))
    assert [card.number_label for card in second.question_results] == ["2"]


# --- 第三轮复核（2026-10-30）：考点范围/题型校验/旧会话总量/换题继承 ---


@pytest.mark.parametrize(
    ("text", "scope"),
    [
        ("只解释第一题考什么知识点", "topic"),
        ("第一题考啥", "topic"),
        ("第一题考的是什么", "topic"),
        ("第一题考查的知识点", "topic"),
        ("这道题主要考核哪些内容", "topic"),
        ("只解释第一题考什么知识点，不用给答案", "topic"),
        ("这道题讲讲，顺便说考点", None),
        ("讲讲这道题的考点和解题步骤", None),
        ("这道题考什么，答案是多少", None),
        ("解释一下这题为什么选 B", None),
        ("这道题考什么？怎么做", None),
        ("帮我找一道考循环的选择题", None),
    ],
)
def test_answer_scope_judges_object_and_range(text: str, scope: str | None) -> None:
    """“解释”本身不等于完整讲解：要看解释的对象和范围。"""

    assert ConversationService._answer_scope(text) == scope


@pytest.mark.anyio
async def test_explain_wording_for_topic_does_not_open_answer(tmp_path: Path) -> None:
    """“只解释第一题考什么知识点”：不讲解、不建记录、不公开答案。"""

    scheduler = topic_then_explain(number="1")
    factory = ReplyFactory(["这道题考的是表达式求值和字符串、整数的区别。"])
    tutor = FakeTutor()
    service, repository, _ = build_service(
        tmp_path, scheduler=scheduler, tutor=tutor, reply_factory=factory
    )
    conversation = service.create_conversation()
    seed_page(repository, conversation.id, offset=0, returned=1, next_offset=1)

    events = await run(
        service, conversation.id, turn_input(text="只解释第一题考什么知识点")
    )
    result = result_of(events)

    assert tutor.explain_calls == []
    assert repository.get_active_problem(conversation.id) is None
    selection = repository.get_active_question(conversation.id)
    assert selection is None or selection.answer_revealed_at is None
    prompt = "\n".join(str(m) for call in factory.models[0].calls for m in call)
    assert "只回答考点" in prompt


def mixed_type_cards():
    """同卷的选择题 1–5 与填空题 1–5：题号相同、题型不同。"""

    fill = [
        sample_card(
            f"{PAPER}-fill-{number}",
            number=str(number),
            statement=f"第 {number} 道填空题：请写出结果。",
        ).model_copy(update={"question_type": "fill"})
        for number in range(1, 6)
    ]
    choice = [
        sample_card(
            f"{PAPER}-choice-{number}",
            number=str(number),
            statement=f"第 {number} 道选择题：下列表达式的值是？",
        )
        for number in range(1, 6)
    ]
    return fill + choice


@pytest.mark.anyio
async def test_selection_honours_explicit_question_type(tmp_path: Path) -> None:
    """展示填空题第 5 题时说“选择题第 5 题”：要选中选择题，不是填空题。"""

    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
        ActionPlan(action="select_question", parameters={})
    ])])
    service, repository, runtime = build_service(
        tmp_path, cards=mixed_type_cards(), scheduler=scheduler
    )
    conversation = service.create_conversation()
    seed_page(
        repository,
        conversation.id,
        offset=0,
        returned=5,
        next_offset=5,
        question_type="fill",
        cards=mixed_type_cards()[:5],
    )

    events = await run(
        service, conversation.id, turn_input(text="我选这张卷的选择题第 5 题")
    )

    selection = repository.get_active_question(conversation.id)
    assert selection is not None
    assert selection.question_type == "choice"
    assert selection.question_id == f"{PAPER}-choice-5"
    request = runtime.search_calls[-1]["request"]
    assert request.number_label == "5"
    assert request.question_type == "choice"


@pytest.mark.anyio
async def test_legacy_cards_do_not_prove_total_or_exhaustion(tmp_path: Path) -> None:
    """返修前只展示过 3 张卡的会话：继续时从第 4 道查，不谎称“共 3 道已列完”。"""

    scheduler = FakeScheduler(decisions=[search_decision()])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    request_id = str(uuid4())
    repository.start_turn(conversation.id, request_id, "这张卷的选择题", kind="dynamic")
    repository.complete_dynamic_turn(
        conversation.id,
        request_id,
        kind="dynamic",
        assistant_message="已列出题目",
        question_results=paper_cards()[:3],
        question_result_status="ok",
    )

    events = await run(
        service, conversation.id, turn_input(text="继续给出剩下的选择题")
    )
    result = result_of(events)

    request = runtime.search_calls[-1]["request"]
    assert request.paper_id == PAPER
    assert request.offset == 3
    message = result.composite_results[0].message
    assert "共 40 道" in message and "本次显示第 4–" in message
    assert "已经全部列完" not in message


@pytest.mark.anyio
async def test_swap_keeps_previous_query_and_type(tmp_path: Path) -> None:
    """“换一道”继承原来的关键词与题型，只排除已展示的题。"""

    scheduler = FakeScheduler(decisions=[search_decision()])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()

    first = result_of(
        await run(service, conversation.id, turn_input(text="找一道Python循环选择真题"))
    )
    original = runtime.search_calls[-1]["request"]
    assert original.question_type == "choice"

    scheduler.push(search_decision())
    second = result_of(await run(service, conversation.id, turn_input(text="换一道")))
    follow_up = runtime.search_calls[-1]["request"]

    assert follow_up.query == original.query and follow_up.query
    assert follow_up.question_type == original.question_type
    assert [card.number_label for card in second.question_results] == ["2"]
    assert first.question_results[0].number_label == "1"
