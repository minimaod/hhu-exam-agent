"""F008 第三轮复核：真题讲解接入正文、讲解后提问、拒绝纠正、评价判定。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.conversations.graph import ActionPlan, ScheduleDecision
from app.conversations.repository import ConversationRepository
from app.conversations.service import ConversationService, TurnInput, _DynamicContext
from app.tutoring.models import Source
from tests.question_fakes import FakeQuestionRuntime, sample_card
from tests.test_question_turns import (
    NeverDisconnected,
    add_search_turn,
    build_service as build_question_service,
    turn_input,
)
from tests.tool_fakes import ReplyFactory, FakeReplyModel
from tests.tutoring_fakes import (
    FakeScheduler,
    FakeTutor,
    clear_understanding,
    sample_explanation,
)


def _rejected(tool: str, message: str, code: str = "MISSING_ARGUMENTS") -> dict:
    return {
        "action": f"tool:{tool}",
        "status": "failed",
        "message": message,
        "details": {
            "tool": tool,
            "error_code": code,
            "retryable": True,
            "rejected": True,
        },
        "needs_user": False,
        "executed": False,
    }


def _turn_recovery(db_path: Path, conversation_id: str, request_id: str) -> dict:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT output_json FROM conversation_turns WHERE conversation_id=? AND request_id=?",
            (conversation_id, request_id),
        ).fetchone()
    assert row is not None and row[0]
    return json.loads(row[0]).get("_recovery") or {}


# ----------------------------------------------------------------------
# P1-1：真题卡“讲解/提交并讲解”要走统一正文阶段
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_question_explanation_streams_body_and_reports_status(tmp_path: Path) -> None:
    """真题卡讲解也要有正文片段、回复状态，并能只重做正文。"""

    factory = ReplyFactory(
        [
            "这道题考的是列表推导式。",
            "第一步先把循环范围写对，再套进方括号。",
        ]
    )
    service, repository, _ = build_question_service(
        tmp_path,
        tutor=FakeTutor(explanations=[sample_explanation("B")]),
        runtime=FakeQuestionRuntime(cards=[sample_card()]),
    )
    service._make_reply = factory
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])
    data = replace(
        turn_input(kind="explain_question", question_id=card.question_id, source_turn=source),
        student_answer_text="B",
    )

    prepared = service.prepare_turn(conversation.id, data)
    events = [event async for event in service.stream_prepared(prepared, NeverDisconnected())]

    kinds = [event.type for event in events]
    assert "message_delta" in kinds
    assert kinds.index("message_delta") < kinds.index("result")
    deltas = [event for event in events if event.type == "message_delta"]
    assert [event.seq for event in deltas] == list(range(1, len(deltas) + 1))

    result = next(event for event in events if event.type == "result")
    assert result.assistant_message == "".join(event.delta for event in deltas)
    assert "已结合原卷材料完成题目讲解。" != result.assistant_message
    assert result.reply_status == "complete"
    assert result.reply_retryable is False
    # 讲解卡仍然随结果返回（目标绑定没有丢）。
    assert result.tutoring is not None and result.question is not None

    # 正文材料已存盘，后续可以只重做正文。
    recovery = _turn_recovery(tmp_path / "study.sqlite3", conversation.id, prepared.request_id)
    assert recovery["reply"]["mode"] == "model"
    assert recovery["reply_input"]["teaching"]

    service._make_reply = ReplyFactory(["重写后的讲解正文。"])
    regenerated = await service.regenerate_reply(conversation.id, prepared.request_id)
    assert regenerated["assistant_message"] == "重写后的讲解正文。"
    assert regenerated["reply_status"] == "complete"


@pytest.mark.anyio
async def test_question_explanation_without_model_keeps_deterministic_text(
    tmp_path: Path,
) -> None:
    """没有回复模型时仍是原来的确定性说明，且不会被标成“生成失败”。"""

    service, repository, _ = build_question_service(
        tmp_path, tutor=FakeTutor(explanations=[sample_explanation()])
    )
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])

    events = await _run(
        service,
        conversation.id,
        turn_input(kind="explain_question", question_id=card.question_id, source_turn=source),
    )
    assert all(event.type != "message_delta" for event in events)
    result = next(event for event in events if event.type == "result")
    assert result.assistant_message == "已结合原卷材料完成题目讲解。"
    assert result.reply_status is None
    assert result.reply_retryable is False


# ----------------------------------------------------------------------
# P1-2：讲解成功后提问，不能吞掉讲解
# ----------------------------------------------------------------------


def _dynamic_service(tmp_path: Path, factory):
    from app.papers.repository import PaperRepository
    from app.papers.service import DEFAULT_PAPERS_ROOT, PaperService
    from app.mistakes.service import MistakeRecordService

    repository = ConversationRepository(tmp_path / "study.sqlite3")
    repository.initialize()
    papers = PaperService(PaperRepository(DEFAULT_PAPERS_ROOT))
    tutor = FakeTutor(
        understandings=[clear_understanding()],
        explanations=[sample_explanation()],
    )
    records = MistakeRecordService(
        repository=repository,
        papers=papers,
        make_tutor=lambda: tutor,
        tutoring_error=None,
        attachments_root=tmp_path / "attachments",
    )
    scheduler = FakeScheduler()
    service = ConversationService(
        repository=repository,
        make_query=None,
        query_error=None,
        make_tutor=lambda: tutor,
        make_router=None,
        papers=papers,
        records=records,
        attachments_root=tmp_path / "attachments",
        make_scheduler=lambda: scheduler,
        make_reply=factory,
    )
    return service, repository, scheduler


def _input(text: str = "讲讲这道题") -> TurnInput:
    return TurnInput(
        request_id=str(uuid4()),
        kind=None,
        dispatch=True,
        text=text,
        attachments=(),
        student_answer_text=None,
        solution_text=None,
        source=Source(),
        problem_id=None,
        start_new=False,
    )


async def _run(service, conversation_id: str, data: TurnInput):
    prepared = service.prepare_turn(conversation_id, data)
    return [event async for event in service.stream_prepared(prepared, NeverDisconnected())]


@pytest.mark.anyio
async def test_explanation_then_question_keeps_body_and_asks(tmp_path: Path) -> None:
    """“讲解成功 → 问要不要练类似的”必须既有讲解正文，也有那句问题。"""

    factory = ReplyFactory(["先看单调性，再求导判断极值。"])
    service, repository, scheduler = _dynamic_service(tmp_path, factory)
    conversation = repository.create_conversation()
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="introduce", reason="确认科目")],
            confirmed_course="高等数学",
        )
    )
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="explain_problem", reason="讲解当前题")],
        )
    )
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="clarify_or_reply", reason="问要不要练类似的")],
            assistant_reply="要不要来一道类似题练手？",
        )
    )

    prepared = service.prepare_turn(conversation.id, _input())
    events = [event async for event in service.stream_prepared(prepared, NeverDisconnected())]
    result = next(event for event in events if event.type == "result")

    deltas = [event for event in events if event.type == "message_delta"]
    assert deltas, "讲解之后提问时仍然要有正文片段"
    assert len(factory.models) == 1, "这一轮必须真的调用回复模型"
    text = result.assistant_message or ""
    assert "先看单调性，再求导判断极值。" in text
    assert "要不要来一道类似题练手？" in text
    assert text.index("先看单调性") < text.index("要不要来一道类似题练手？")
    # 问题是用工具原文追加的，不会被模型改写。
    assert result.reply_status == "complete"


@pytest.mark.anyio
async def test_pure_clarification_still_skips_reply_model(tmp_path: Path) -> None:
    """没有讲解的纯澄清轮仍然不调用回复模型。"""

    factory = ReplyFactory(["不该被用到"])
    service, repository, scheduler = _dynamic_service(tmp_path, factory)
    conversation = repository.create_conversation()
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="clarify_or_reply", reason="信息不足")],
            assistant_reply="你想复习哪一门课？",
        )
    )

    events = await _run(service, conversation.id, _input("复习一下"))
    assert all(event.type != "message_delta" for event in events)
    assert not factory.models
    result = next(event for event in events if event.type == "result")
    assert result.assistant_message == "你想复习哪一门课？"


# ----------------------------------------------------------------------
# P2-1：参数纠正成功后不能算“部分失败”
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_rejection_fixed_by_later_success_is_not_a_failure(tmp_path: Path) -> None:
    """先被拒、后用正确参数成功执行：业务状态按最终结果算成功。"""

    service, repository, scheduler = _dynamic_service(tmp_path, None)
    conversation = repository.create_conversation()
    scheduler.push(
        ScheduleDecision(
            actions=[],
            rejected=[_rejected("confirm_course", "缺少 course 参数，本次调用没有执行。")],
        )
    )
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="introduce", reason="补上参数后重做")],
            confirmed_course="Python",
        )
    )

    prepared = service.prepare_turn(conversation.id, _input())
    events = [event async for event in service.stream_prepared(prepared, NeverDisconnected())]
    result = next(event for event in events if event.type == "result")

    assert result.business_status == "succeeded"
    assert result.retryable is False
    assert [item.action for item in result.composite_results] == ["introduce"]
    assert all(item.status == "success" for item in result.composite_results)
    assert not any(item.details.get("rejected") for item in result.composite_results)


@pytest.mark.anyio
async def test_unfixed_rejection_still_reports_failure(tmp_path: Path) -> None:
    """拒绝之后没有重做成功，仍然如实报失败。"""

    service, repository, scheduler = _dynamic_service(tmp_path, None)
    conversation = repository.create_conversation()
    scheduler.push(
        ScheduleDecision(
            actions=[],
            rejected=[_rejected("confirm_course", "缺少 course 参数，本次调用没有执行。")],
        )
    )
    scheduler.push(
        # 换了一个和拒绝无关的动作：拒绝没有被同一工具的成功调用纠正。
        ScheduleDecision(actions=[ActionPlan(action="query_papers", reason="先找卷")])
    )

    prepared = service.prepare_turn(conversation.id, _input())
    events = [event async for event in service.stream_prepared(prepared, NeverDisconnected())]
    result = next(event for event in events if event.type == "result")

    assert result.business_status != "succeeded"
    assert result.retryable is True
    rejected = [item for item in result.composite_results if item.details.get("rejected")]
    assert len(rejected) == 1 and rejected[0].status == "failed"


# ----------------------------------------------------------------------
# P2-2：评价判定的措辞
# ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_dynamic_interruption_keeps_actions_and_redoes_only_body(
    tmp_path: Path,
) -> None:
    """普通动态轮次同样：正文中断后不需要重跑工具，只重做正文。"""

    factory = ReplyFactory(["先确认科目。", "再看要练哪部分。"])
    service, repository, scheduler = _dynamic_service(tmp_path, factory)
    conversation = repository.create_conversation()
    scheduler.push(
        ScheduleDecision(
            actions=[ActionPlan(action="introduce", reason="确认科目")],
            confirmed_course="Python",
        )
    )

    prepared = service.prepare_turn(conversation.id, _input())
    stream = service.stream_prepared(prepared, NeverDisconnected())
    async for event in stream:
        if event.type == "message_delta":
            break
    await stream.aclose()

    detail = service.get_detail(conversation.id)
    turn = detail.turns[-1]
    # 无论生产协程是被取消还是已经跑完，轮次都不能退回“中断、需要重来”。
    assert turn.status == "completed"
    assert [item.action for item in turn.composite_results] == ["introduce"]

    recovery = _turn_recovery(tmp_path / "study.sqlite3", conversation.id, prepared.request_id)
    assert "user_text" in recovery["reply_input"], "正文材料必须在流式输出前就落盘"

    calls_before = len(scheduler.calls)
    service._make_reply = ReplyFactory(["重写后的正文。"])
    replay = await service.regenerate_reply(conversation.id, prepared.request_id)

    assert replay["assistant_message"] == "重写后的正文。"
    assert len(scheduler.calls) == calls_before, "重做正文不能重新调度"
    restored = service.get_detail(conversation.id)
    assert [item.action for item in restored.turns[-1].composite_results] == ["introduce"]


def _evaluation(**overrides):
    from app.conversations.models import ExerciseEvaluation as EvaluationResult

    payload = {
        "is_correct": False,
        "needs_supplement": False,
        "final_answer_assessment": "最终答案错误。",
        "step_by_step_analysis": "第二步符号写反了。",
        "guidance": "注意移项变号。",
    }
    payload.update(overrides)
    return EvaluationResult(**payload)


def _exercise_view(evaluation) -> object:
    class _View:
        pass

    view = _View()
    view.evaluation = evaluation
    return view


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("evaluation", "expected", "forbidden"),
    [
        (_evaluation(is_correct=False, needs_supplement=False), "判定：错误", "需要补充"),
        (_evaluation(is_correct=True, needs_supplement=False), "判定：正确", "需要补充"),
        (
            _evaluation(is_correct=None, needs_supplement=False),
            "判定：依据不足",
            "正确",
        ),
        (
            _evaluation(is_correct=False, needs_supplement=True),
            "需要补充解题过程",
            None,
        ),
    ],
)
def test_evaluation_verdict_distinguishes_error_from_supplement(
    evaluation, expected: str, forbidden: str | None
) -> None:
    """判错 ≠ 需要补充；依据不足也要单独表达。"""

    ctx = _DynamicContext(
        conversation_id="c", exercise_view=_exercise_view(evaluation)
    )
    teaching = ConversationService._teaching_material(ctx)
    assert teaching is not None
    assert expected in teaching
    if forbidden:
        assert forbidden not in teaching


# ----------------------------------------------------------------------
# P1-3：正文中断后只恢复正文，不重复讲题、不重复建题
# ----------------------------------------------------------------------


def _question_explain_setup(tmp_path: Path, factory, tutor=None):
    tutor = tutor or FakeTutor(explanations=[sample_explanation("B")])
    service, repository, _ = build_question_service(
        tmp_path,
        tutor=tutor,
        runtime=FakeQuestionRuntime(cards=[sample_card()]),
    )
    service._make_reply = factory
    conversation = service.create_conversation()
    card = sample_card()
    source = add_search_turn(repository, conversation.id, [card])
    data = replace(
        turn_input(kind="explain_question", question_id=card.question_id, source_turn=source),
        student_answer_text="B",
    )
    return service, repository, conversation, data, tutor


@pytest.mark.anyio
async def test_interruption_after_first_delta_keeps_explanation_saved(
    tmp_path: Path,
) -> None:
    """首个正文片段后断开：讲解、目标绑定、正文材料都已落盘，轮次不是中断态。"""

    factory = ReplyFactory(["第一句讲解。", "第二句讲解。"])
    service, repository, conversation, data, tutor = _question_explain_setup(
        tmp_path, factory
    )
    prepared = service.prepare_turn(conversation.id, data)

    seen: list[str] = []
    stream = service.stream_prepared(prepared, NeverDisconnected())
    async for event in stream:
        seen.append(event.type)
        if event.type == "message_delta":
            break  # 模拟学生在第一段正文之后关掉页面
    await stream.aclose()

    assert "message_delta" in seen
    assert "result" not in seen, "中断时不会有最终结果事件"

    detail = service.get_detail(conversation.id)
    turn = detail.turns[-1]
    # 讲解已经完成并保存，所以不是“中断，需要重来”。
    assert turn.status == "completed"
    assert turn.tutoring is not None
    assert turn.business_status == "succeeded"
    assert detail.active_problem is not None
    saved_problem_id = detail.active_problem.id
    assert turn.assistant_message  # 兜底正文先落盘，不留下空白

    recovery = _turn_recovery(tmp_path / "study.sqlite3", conversation.id, prepared.request_id)
    assert recovery["reply"]["reason"] == "reply_pending"
    assert recovery["reply_input"]["teaching"], "正文材料必须已经存在，才能只重做正文"

    # 恢复只需要重做正文：讲解模型不再调用，题目也不会再建一份。
    explain_calls_before = len(tutor.explain_calls)
    service._make_reply = ReplyFactory(["重写后的讲解正文。"])
    replay = await service.regenerate_reply(conversation.id, prepared.request_id)

    assert replay["assistant_message"] == "重写后的讲解正文。"
    assert replay["reply_status"] == "complete"
    restored = service.get_detail(conversation.id)
    assert restored.active_problem.id == saved_problem_id
    assert len(restored.turns) == 2  # 检索 + 讲解，没有新增“重新讲一遍”的轮次
    assert len(tutor.explain_calls) == explain_calls_before
    assert restored.turns[-1].assistant_message == "重写后的讲解正文。"


@pytest.mark.anyio
async def test_restarted_service_redoes_body_without_rexplaining(tmp_path: Path) -> None:
    """进程重启后仍能只重做正文：材料在库里，讲解模型不会被再次调用。"""

    factory = ReplyFactory(["第一句讲解。", "第二句讲解。"])
    service, _repository, conversation, data, _tutor = _question_explain_setup(
        tmp_path, factory
    )
    prepared = service.prepare_turn(conversation.id, data)
    stream = service.stream_prepared(prepared, NeverDisconnected())
    async for event in stream:
        if event.type == "message_delta":
            break
    await stream.aclose()

    # 全新的服务实例 + 全新的题目替身：只要被调用就会断言失败。
    fresh_tutor = FakeTutor()
    restarted, _repo2, _ = build_question_service(
        tmp_path,
        tutor=fresh_tutor,
        runtime=FakeQuestionRuntime(cards=[sample_card()]),
    )
    restarted._make_reply = ReplyFactory(["重启后补写的讲解。"])

    replay = await restarted.regenerate_reply(conversation.id, prepared.request_id)

    assert replay["assistant_message"] == "重启后补写的讲解。"
    assert restarted.get_detail(conversation.id).turns[-1].assistant_message == (
        "重启后补写的讲解。"
    )
    assert fresh_tutor.explain_calls == []
    assert fresh_tutor.understand_calls == []