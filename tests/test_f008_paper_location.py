"""F008 查卷定位：原话里的学年、学期、卷别必须真正参与筛选。

截图对应的真实调用只有 ``{"course": ..., "exam_type": "final"}`` 两个参数，
结果却返回了全部 10 份期末卷。修复口径：

1. 学年/学期/卷别由 ``app.papers.locator`` 从学生原话确定性提取；
2. 唯一命中只展示那一份，并给出下载入口、把试卷身份写进上下文；
3. 多候选只询问仍缺的区分条件；无匹配返回空结果，不扩大范围；
4. 没有学年/学期时保持原来的宽泛查询行为。
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput
from app.papers.locator import (
    RANK_AMBIGUOUS,
    RANK_NONE,
    RANK_NULL,
    RANK_UNIQUE,
    describe_location,
    extract_location,
    locate_papers,
    paper_term,
)
from app.papers.models import PaperItem, PaperListResponse
from app.tutoring.models import Source
from tests.question_fakes import FakeQuestionRuntime, sample_card
from tests.tutoring_fakes import FakeScheduler

COURSE = "Python语言程序设计"
TITLE = "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）"
PAPER = "py-2024-2025-s1-final"


def paper(
    academic_year: str,
    *,
    term: str = "s1",
    exam_type: str = "final",
    variant: str = "A",
    course: str = COURSE,
) -> PaperItem:
    """按真实目录的命名规则造一份试卷，便于构造 A/B 两卷等场景。"""

    year, semester = academic_year.split("-", 1)
    kind = {"final": "期末", "midterm": "期中", "other": "其他"}[exam_type]
    title = (
        f"{academic_year}学年{'第一学期' if term == 's1' else '第二学期'}"
        f"《{course}》{kind}试卷（{variant}卷）"
    )
    paper_id = f"py-{academic_year}-{term}-{exam_type}".lower()
    return PaperItem(
        id=paper_id,
        title=title,
        course=course,
        academic_year=academic_year,
        exam_type=exam_type,
        exam_date=None,
        download_url=f"/api/papers/{paper_id}/download",
    )


PYTHON_PAPERS = [
    paper("2020-2021", term="s1", exam_type="midterm"),
    paper("2020-2021", term="s1"),
    paper("2020-2021", term="s2", exam_type="midterm"),
    paper("2020-2021", term="s2"),
    paper("2024-2025", term="s1"),
    paper("2024-2025", term="s2"),
]


def candidate(text: str, papers=None, *, exam_type: str | None = "final"):
    return locate_papers(text, papers if papers is not None else PYTHON_PAPERS, exam_type=exam_type)


PAPER_ITEM = paper("2024-2025", term="s1")


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


def paper_card(number: int = 1):
    return sample_card(f"{PAPER}-choice-{number}", number=str(number))


def card_of(paper_id: str, number: int):
    """另一份试卷的题目；用来验证“跨卷找类似题”不会被当前卷锁死。"""

    card = sample_card(f"{paper_id}-choice-{number}", number=str(number))
    return card.model_copy(
        update={
            "source": card.source.model_copy(
                update={"paper_id": paper_id, "paper_title": "另一份期末试卷"}
            )
        }
    )


def build_service(
    tmp_path: Path,
    *,
    items: list[PaperItem] | None = None,
    scheduler=None,
    cards=None,
) -> tuple[ConversationService, ConversationRepository, FakeQuestionRuntime]:
    repository = ConversationRepository(tmp_path / "study.sqlite3")
    repository.initialize()
    runtime = FakeQuestionRuntime(cards=cards or [paper_card()])
    service = ConversationService(
        repository=repository,
        make_query=None,
        query_error="模型未配置",
        make_tutor=None,
        tutoring_error="模型未配置",
        make_scheduler=(lambda: scheduler) if scheduler is not None else None,
        make_reply=None,
        papers=FakePapers(items if items is not None else [PAPER_ITEM]),
        question_runtime=runtime,
        attachments_root=tmp_path / "attachments",
    )
    return service, repository, runtime


def turn_input(text: str, request_id: str) -> TurnInput:
    return TurnInput(
        request_id=request_id,
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


class NeverDisconnected:
    async def is_disconnected(self) -> bool:
        return False


async def run(service: ConversationService, conversation_id: str, text: str):
    # 同一用例里多次请求不能复用 request_id，否则会被当成重复请求。
    data = turn_input(text, f"r-{uuid4().hex}")
    prepared = service.prepare_turn(conversation_id, data)
    return [event async for event in service.stream_prepared(prepared, NeverDisconnected())]


def result_of(events):
    return next(event for event in events if event.type == "result")


def query_decision(**parameters):
    return ScheduleDecision(actions=[ActionPlan(action="query_papers", parameters=parameters)])


# ----------------------------------------------------------------------
# 定位规则本身
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "给出2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）及答案",
        "给出 2024-2025 学年第一学期 Python 期末试卷 A 卷",
        "2024–2025 第一学期 python 期末试卷（A卷）",
        "帮我下载2024-2025学年第一学期Python语言程序设计期末试卷A卷",
    ],
)
def test_screenshot_sentence_hits_one_paper(text: str) -> None:
    match = candidate(text)
    assert match.rank == RANK_UNIQUE
    assert match.paper.id == PAPER_ITEM.id


def test_plain_query_keeps_broad_scope() -> None:
    match = candidate("给我 Python 期末试卷")
    assert match.rank == RANK_NULL
    assert match.paper is None


def test_missing_academic_year_returns_empty_instead_of_widening() -> None:
    match = candidate("2019-2020学年第一学期 Python 期末试卷")
    assert match.rank == RANK_NONE
    assert match.paper is None and match.candidates == ()


def test_same_year_and_term_with_a_and_b_asks_variant() -> None:
    two = [paper("2024-2025", term="s1", variant="A"), paper("2024-2025", term="s1", variant="B")]
    match = candidate("2024-2025学年第一学期 Python 期末试卷", two)
    assert match.rank == RANK_AMBIGUOUS
    assert match.paper is None and len(match.candidates) == 2
    exact = candidate("2024-2025学年第一学期 Python 期末试卷（B卷）", two)
    assert exact.rank == RANK_UNIQUE and exact.paper.title.endswith("（B卷）")


def test_second_term_is_not_confused_with_first() -> None:
    match = candidate("2024-2025学年第二学期 Python 期末试卷", exam_type=None)
    assert match.paper is not None and match.paper.id.endswith("-s2-final")


def test_term_falls_back_to_registered_id() -> None:
    item = PAPER_ITEM.model_copy(update={"title": "Python 期末试卷（A卷）"})
    assert paper_term(item) == "s1"


def test_location_summary_keeps_real_conditions() -> None:
    location = extract_location("2024-2025学年第二学期 Python 期中 B卷")
    assert location.year == "2024-2025" and location.term == "s2"
    assert location.variant == "B" and location.exam_type == "midterm"
    assert describe_location(location) == "2024-2025学年第二学期期中B卷"


# ----------------------------------------------------------------------
# 会话业务：筛选结果、上下文与空结果
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_query_finds_one_paper_and_saves_its_identity(tmp_path: Path) -> None:
    scheduler = FakeScheduler(decisions=[query_decision(course=COURSE, exam_type="final")])
    service, repository, _ = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    result = result_of(await run(
        service, conversation.id,
        "给出2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）及答案",
    ))

    assert result.result.total == 1
    assert result.result.items[0].id == PAPER_ITEM.id
    outcome = result.composite_results[0]
    assert outcome.status == "success"
    assert outcome.details["paper_id"] == PAPER_ITEM.id
    assert outcome.details["paper_title"] == TITLE
    assert outcome.details["download_url"].endswith(f"/{PAPER_ITEM.id}/download")
    assert "A卷" in outcome.message and "第一学期" in outcome.message
    # 唯一命中即“当前试卷”：“这张卷的选择题”不必再问年份。
    stored = repository.find_question_context(conversation.id, require_paper=True)
    assert stored["paper_id"] == PAPER_ITEM.id
    assert stored["paper_title"] == TITLE


@pytest.mark.anyio
async def test_query_without_academic_year_still_lists_all_final_papers(tmp_path: Path) -> None:
    scheduler = FakeScheduler(decisions=[query_decision(course=COURSE, exam_type="final")])
    service, _, _ = build_service(tmp_path, items=PYTHON_PAPERS, scheduler=scheduler)
    conversation = service.create_conversation()
    result = result_of(await run(service, conversation.id, "给我 Python 期末试卷"))
    # 没有学年/学期条件时不进入精确定位：各学年、各学期的期末卷都要列出。
    assert result.result.total == 4
    assert result.result.kind == "found"
    assert {item.exam_type for item in result.result.items} == {"final"}


@pytest.mark.anyio
async def test_missing_year_returns_empty_result_without_fallback(tmp_path: Path) -> None:
    scheduler = FakeScheduler(decisions=[query_decision(course=COURSE, exam_type="final")])
    service, repository, _ = build_service(tmp_path, items=PYTHON_PAPERS, scheduler=scheduler)
    conversation = service.create_conversation()
    result = result_of(await run(
        service, conversation.id, "给我 2019-2020 学年第一学期 Python 期末试卷"
    ))

    assert result.result.kind == "empty" and result.result.total == 0
    outcome = result.composite_results[0]
    assert outcome.details["status"] == "empty" and outcome.details["total"] == 0
    assert "2019-2020" in result.result.message
    # 空结果不留下“当前试卷”，避免下一轮误以为学生指的是旧卷。
    assert repository.find_question_context(conversation.id, require_paper=True) is None


@pytest.mark.anyio
async def test_ambiguous_paper_asks_only_for_missing_variant(tmp_path: Path) -> None:
    two = [paper("2024-2025", term="s1", variant="A"), paper("2024-2025", term="s1", variant="B")]
    scheduler = FakeScheduler(decisions=[query_decision(course=COURSE, exam_type="final")])
    service, _, _ = build_service(tmp_path, items=two, scheduler=scheduler)
    conversation = service.create_conversation()
    result = result_of(await run(
        service, conversation.id, "2024-2025学年第一学期 Python 期末试卷"
    ))

    outcome = result.composite_results[0]
    assert outcome.status == "clarification"
    assert "请说明要哪一份" in outcome.message
    assert "（A卷）" in outcome.message and "（B卷）" in outcome.message
    assert "第一学期" in outcome.message


@pytest.mark.anyio
async def test_real_catalog_screenshot_sentence_hits_one_paper(tmp_path: Path) -> None:
    """真实 162 条目录：截图原句必须只命中 py-2024-2025-s1-final。"""

    from app.papers.repository import PaperRepository
    from app.papers.service import DEFAULT_PAPERS_ROOT, PaperService

    if not (DEFAULT_PAPERS_ROOT / "catalog.json").is_file():
        pytest.skip("本机无真实试卷目录")
    papers = PaperService(PaperRepository(DEFAULT_PAPERS_ROOT))
    scheduler = FakeScheduler(decisions=[query_decision(course=COURSE, exam_type="final")])
    repository = ConversationRepository(tmp_path / "study.sqlite3")
    repository.initialize()
    service = ConversationService(
        repository=repository,
        make_query=None,
        query_error="模型未配置",
        make_tutor=None,
        tutoring_error="模型未配置",
        make_scheduler=(lambda: scheduler),
        make_reply=None,
        papers=papers,
        question_runtime=FakeQuestionRuntime(cards=[paper_card()]),
        attachments_root=tmp_path / "attachments",
    )
    conversation = service.create_conversation()
    result = result_of(await run(
        service,
        conversation.id,
        "给出2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）及答案",
    ))

    assert result.result.total == 1
    item = result.result.items[0]
    assert item.id == "py-2024-2025-s1-final"
    assert item.academic_year == "2024-2025"
    assert item.download_url == "/api/papers/py-2024-2025-s1-final/download"
    outcome = result.composite_results[0]
    assert outcome.details["paper_id"] == "py-2024-2025-s1-final"
    assert outcome.details["term"] == "s1"
    stored = repository.find_question_context(conversation.id, require_paper=True)
    assert stored["paper_id"] == "py-2024-2025-s1-final"


@pytest.mark.anyio
async def test_naming_a_paper_does_not_list_its_questions(tmp_path: Path) -> None:
    """只发卷名时模型若顺手列题，程序必须拦下，不能把整卷题目倒出来。"""

    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
        ActionPlan(action="query_papers",
                   parameters={"course": COURSE, "exam_type": "final"}),
        # 真实记录里模型就是这样追加的：不带题型，等于整卷 59 题。
        ActionPlan(action="list_paper_questions", parameters={}),
    ])])
    service, repository, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    result = result_of(await run(
        service, conversation.id,
        "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）",
    ))

    actions = {item.action: item for item in result.composite_results}
    assert "query_papers" in actions
    blocked = actions["list_paper_questions"]
    assert blocked.status == "clarification"
    assert "要列这份卷的题目" in blocked.message
    # 没有真的去题库展开题目。
    assert runtime.search_calls == []
    assert result.question_results == []
    assert result.result.total == 1
    assert "下载入口已备好" in result.assistant_message
    assert "要列这份卷的题目" in result.assistant_message


@pytest.mark.anyio
async def test_asking_for_a_type_still_lists_questions(tmp_path: Path) -> None:
    """同一轮里学生明确要题型时，拦截面不能误伤。"""

    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
        ActionPlan(action="query_papers",
                   parameters={"course": COURSE, "exam_type": "final"}),
        ActionPlan(action="list_paper_questions", parameters={"question_type": "choice"}),
    ])])
    service, _, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    result = result_of(await run(
        service, conversation.id,
        "2024-2025学年第一学期 Python 期末试卷（A卷）的选择题",
    ))

    assert "list_paper_questions" in [item.action for item in result.composite_results]
    assert runtime.search_calls[-1]["request"].paper_id == PAPER_ITEM.id


@pytest.mark.anyio
async def test_paper_query_does_not_replay_previous_question_cards(tmp_path: Path) -> None:
    """上一轮列过题，下一轮只问试卷时，旧题卡不能冒充本轮结果。"""

    scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
        ActionPlan(action="query_papers",
                   parameters={"course": COURSE, "exam_type": "final"}),
        ActionPlan(action="list_paper_questions", parameters={"question_type": "choice"}),
    ])])
    service, repository, _ = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    first = result_of(await run(service, conversation.id, "2024-2025学年第一学期 Python 期末试卷（A卷）的选择题"))
    assert first.question_results, "第一轮应该真的列出题目"

    # 第二轮只发卷名：模型仍只调 query_papers。
    scheduler.push(query_decision(course=COURSE, exam_type="final"))
    events = await run(service, conversation.id, "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）")
    second = result_of(events)

    assert second.result.total == 1
    assert second.question_results == [], "历史题卡不能被当成这一轮的结果"
    turn = repository.find_turn(conversation.id, second.request_id)
    assert turn is not None and turn.question_results == []
    # 正文材料里不能出现上一轮的题目清单或分页进度，否则模型会照着编。
    material = json.dumps(turn.recovery.get("reply_input") or {}, ensure_ascii=False)
    assert "候选题目" not in material
    assert "整卷浏览" not in material
    assert "第 1 到第 5 题" not in second.assistant_message
    assert "继续" not in second.assistant_message
    assert "下载入口已备好" in second.assistant_message


@pytest.mark.anyio
async def test_similar_question_searches_whole_bank_and_skips_source(
    tmp_path: Path,
) -> None:
    """学生做完一题后要“去真实试卷库找类似的题”：不能锁在当前卷，也不能返回原题。"""

    own = card_of(PAPER, 12)
    other = card_of("py-2019-2020-s1-final", 3)
    scheduler = FakeScheduler(decisions=[
        query_decision(course=COURSE, exam_type="final"),
    ])
    service, _, runtime = build_service(tmp_path, scheduler=scheduler, cards=[own, other])
    conversation = service.create_conversation()
    await run(service, conversation.id, "2024-2025学年第一学期《Python语言程序设计》期末试卷（A卷）")
    # 决策要在上一轮跑完后逐个补入：调度器会在同一轮的后续步骤里继续取队列。
    scheduler.push(ScheduleDecision(actions=[ActionPlan(
        action="search_questions",
        parameters={"course": COURSE, "paper_id": PAPER, "question_type": "choice"},
    )]))
    done = result_of(await run(service, conversation.id, "这张卷的选择题"))
    assert [card.question_id for card in done.question_results] == [own.question_id]

    # 模型按真实记录那样只给课程、题型和内容关键词，不带 paper_id。
    scheduler.push(ScheduleDecision(actions=[ActionPlan(
        action="search_questions",
        parameters={
            "course": COURSE,
            "paper_id": PAPER,
            "query": "字符串大小写转换 capitalize title upper",
            "question_type": "choice",
        },
    )]))
    similar = result_of(await run(service, conversation.id, "去真实试卷库里面找一道类似的题"))

    request = runtime.search_calls[-1]["request"]
    assert request.paper_id is None, "跨卷检索不应该继承当前试卷"
    returned = [card.question_id for card in similar.question_results]
    assert other.question_id in returned
    assert own.question_id not in returned, "不能把刚做过的原题再发一遍"


@pytest.mark.anyio
async def test_following_question_inherits_located_paper(tmp_path: Path) -> None:
    scheduler = FakeScheduler(decisions=[query_decision(course=COURSE, exam_type="final")])
    service, _, runtime = build_service(tmp_path, scheduler=scheduler)
    conversation = service.create_conversation()
    await run(service, conversation.id,
              "2024-2025学年第一学期 Python 语言程序设计期末试卷（A卷）")
    # 替身按顺序消费脚本；第二轮的动作要在第一轮跑完后再放进去。
    scheduler.push(ScheduleDecision(actions=[ActionPlan(
        action="list_paper_questions", parameters={"question_type": "choice"},
    )]))
    result = result_of(await run(service, conversation.id, "这张卷的选择题"))

    outcome = result.composite_results[0]
    assert outcome.status == "success"
    assert outcome.details["paper_id"] == PAPER_ITEM.id
    # 真实检索条件确实继承到上一轮定位出的试卷，而不是重新问年份或查全部。
    assert runtime.search_calls[-1]["request"].paper_id == PAPER_ITEM.id
    assert result.question is None or not result.question.answer_revealed
