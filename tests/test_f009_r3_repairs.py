"""F009-R3 返修回归：记录很多时，目标附注仍要进入正文材料与实际提示。"""

from __future__ import annotations

from pathlib import Path

from app.conversations.reply import ReplyMaterial, _describe_details
from app.memory.models import MemoryCorrectionRequest
from app.memory.summary import material_block
from tests.test_f009_agent_memory import build

PY = "Python语言程序设计"
LONG_LABELS = [
    "循环结构与终止条件的边界判断及常见错误分析",
    "列表与元组的可变性差异及适用场景选择",
    "字符串切片与索引越界的边界处理",
    "函数参数传递与默认参数的求值时机",
    "文件读写中的编码与异常处理顺序",
]
TARGET_NOTE = "目标附注：这就是学生本轮问到的第25条记录"


def _seed_one(seed, memory, *, index: int, knowledge_point: str, note: str):
    conversation = seed.conversation()
    problem = seed.problem(
        conversation,
        paper_id="py-2023-2024-s2-final",
        source_type="registered_paper",
        days_ago=index,
    )
    exercise = seed.exercise(
        conversation,
        problem,
        knowledge_point=knowledge_point,
        days_ago=index,
        statement=f"第{index}道练习：{knowledge_point}",
    )
    kind = index % 5
    if kind == 1:
        seed.attempt(exercise, correct=False, days_ago=index)
    elif kind == 2:
        seed.attempt(exercise, correct=True, after_reveal=True, days_ago=index)
    elif kind == 3:
        seed.attempt(
            exercise,
            correct=None,
            needs_supplement=True,
            days_ago=index,
        )
    elif kind == 4:
        seed.attempt(
            exercise, correct=False, independence="unclear", days_ago=index
        )
    memory.correct("exercise", exercise, MemoryCorrectionRequest(note=note))
    return exercise


def test_target_note_survives_full_material_budget(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    target = None
    for index in range(1, 26):
        note = TARGET_NOTE if index == 25 else f"第{index}条附注：当时错误原因"
        exercise = _seed_one(
            seed,
            memory,
            index=index,
            knowledge_point=LONG_LABELS[(index - 1) % len(LONG_LABELS)],
            note=note,
        )
        if index == 25:
            target = f"exercise:{exercise}"

    view = memory.summary(PY, days=30, limit=50)
    assert len(view.entries) == 25
    material = material_block(view, focus_entry_ids={target})
    assert len(material) <= 1200
    assert TARGET_NOTE in material
    # 目标明细在结尾说明之前，没有被整体截掉；分组统计也没有消失。
    assert material.index(TARGET_NOTE) < material.index("以上只描述已有记录")
    assert "判错" in material


def test_reply_describe_keeps_target_note(tmp_path: Path) -> None:
    service, seed, memory = build(tmp_path)
    target = None
    for index in range(1, 26):
        note = TARGET_NOTE if index == 25 else f"第{index}条附注：当时错误原因"
        exercise = _seed_one(
            seed,
            memory,
            index=index,
            knowledge_point=LONG_LABELS[(index - 1) % len(LONG_LABELS)],
            note=note,
        )
        if index == 25:
            target = f"exercise:{exercise}"

    view = memory.summary(PY, days=30, limit=50)
    material = material_block(view, focus_entry_ids={target})
    prompt = ReplyMaterial(
        user_text="第25条记录那个附注是什么？",
        learning_memory=material,
    ).describe()
    assert "【学习记忆" in prompt
    assert TARGET_NOTE in prompt
    assert prompt.index(TARGET_NOTE) < prompt.index("【学习记忆") + 1200


def test_tool_result_notes_are_not_capped_at_eight() -> None:
    entries = [
        {
            "entry_id": f"exercise:{index:04d}",
            "note": f"第{index}条附注内容",
        }
        for index in range(1, 26)
    ]
    text = _describe_details({"status": "ok", "memory_entries": entries})
    assert "第1条附注内容" in text
    assert "第25条附注内容" in text
    assert "未列出" not in text