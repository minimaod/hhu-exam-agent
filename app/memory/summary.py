"""F009：学习摘要的分组、排序与措辞。

事实全部由程序算，模型只负责组织语言。这样做的原因：一旦让模型“总结”
证据，就很容易写出“你已经掌握循环”这类结论；而本阶段明确不输出掌握率，
也不把一次答对当成永久掌握。

排序规则（确定性）：
1. 有“独立作答被判错”的知识点最优先（按判错自身时间判断是否为近期）；
2. 其次只有“看过解答后作答”的；
3. 再次是“独立性未确认”的；
4. 再次是“过程不足”的；
5. 再往后是“尚未作答”“自述理解”“只有更早判错”的；
6. 组内按最近时间降序、次数降序、标签字典序。

旧版本/无法核对题库版本的依据不进入统计与建议，只在材料里提示“只作历史回看”。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from app.memory.evidence import EntryEvidence, count_questions
from app.memory.models import (
    VERDICT_LABELS,
    LearningMemoryView,
    MemoryCounts,
    MemoryEntryView,
    MemoryGroupView,
    MemoryRange,
)

MAX_GROUPS = 8
MAX_SUGGESTIONS = 3
MATERIAL_LIMIT = 1200
MATERIAL_DETAIL_BUDGET = 560
FOCUS_DETAIL_BUDGET = 420
FOCUS_LINE_LIMIT = 220
MATERIAL_ATTEMPT_SEGMENTS = 4
_UNKNOWN_LABEL = "未标注知识点"


def build_range(
    *, days: int, limit: int, now: datetime, entries: list[EntryEvidence], fallback: bool
) -> MemoryRange:
    times = [item.recorded_at for item in entries]
    return MemoryRange(
        days=days,
        from_at=min(times) if times else None,
        to_at=max(times) if times else None,
        fallback=fallback,
        limit=limit,
    )


def describe_range(
    course: str | None, window: MemoryRange, counts: MemoryCounts
) -> str:
    """范围说明必须和实际统计一致：说“近期”就只能是窗口内的记录。"""

    subject = course or "全部课程"
    if counts.evidence == 0:
        if window.fallback:
            return f"{subject}近 {window.days} 天没有学习记录。"
        return f"{subject}近 {window.days} 天没有学习记录。"
    span = _span(window)
    if window.fallback:
        return (
            f"{subject}近 {window.days} 天没有记录，以下是最近的 "
            f"{counts.evidence} 条（{span}）"
        )
    return (
        f"{subject}近 {window.days} 天的学习记录：{counts.evidence} 条证据、"
        f"{counts.questions} 道题（{span}）"
    )


def _span(window: MemoryRange) -> str:
    if window.from_at is None or window.to_at is None:
        return "时间未知"
    start = window.from_at.astimezone().strftime("%Y-%m-%d")
    end = window.to_at.astimezone().strftime("%Y-%m-%d")
    return f"{start} 至 {end}" if start != end else start


def group_entries(
    entries: list[EntryEvidence], *, cutoff: datetime
) -> list[MemoryGroupView]:
    """按知识点分组；归一失败的分组保留原标签，不同概念不强行合并。

    旧版本/旧题库版本的依据不进入分组统计：它们可以回看，但不能继续累计
    “判错”或生成建议。判错是否算“近期”，按判错自身的时间判断。
    """

    buckets: dict[str, MemoryGroupView] = {}
    for entry in entries:
        if entry.validity != "ok":
            continue
        pairs = list(
            zip(
                entry.knowledge_points,
                entry.knowledge_point_slugs,
                entry.knowledge_points_normalized,
            )
        ) or [(_UNKNOWN_LABEL, None, False)]
        for label, slug, normalized in pairs:
            key = slug or f"raw:{label.casefold()}"
            group = buckets.get(key)
            if group is None:
                group = MemoryGroupView(label=label, slug=slug, normalized=normalized)
                buckets[key] = group
            _bump(group, entry, cutoff)
    groups = sorted(buckets.values(), key=_group_sort_key)
    return groups[:MAX_GROUPS]


def _bump(group: MemoryGroupView, entry: EntryEvidence, cutoff: datetime) -> None:
    # 归一后同一个知识点可能由多个标签命中（如 list、tuple 归到同一 slug）；
    # 计数单位是“练习＋归一后知识点”，同一练习在同一组里只能计一次。
    if entry.entry_id in group.entry_ids:
        return
    group.entry_ids.append(entry.entry_id)
    if group.latest_at is None or entry.recorded_at > group.latest_at:
        group.latest_at = entry.recorded_at
    if entry.kind == "selfreport":
        group.self_report_count += 1
        return

    attempts = [item for item in entry.attempts if not item.excluded]
    if not attempts:
        if entry.attempts:
            # 有作答，但评价被学生排除：不能写成“还没有作答”。
            group.excluded_evaluation_count += 1
        else:
            group.no_attempt_count += 1
        return

    # 判错与“看过解答后作答”是发生过的事实，不因后续作答被抹掉；
    # 过程不足、独立性未确认、独立做对则按最近一次状态判断。
    wrong_at = entry.wrong_at
    if wrong_at is not None:
        if wrong_at >= cutoff:
            group.wrong_count += 1
            if group.latest_wrong_at is None or wrong_at > group.latest_wrong_at:
                group.latest_wrong_at = wrong_at
        else:
            group.earlier_wrong_count += 1
    if any(item.verdict == "after_reveal" for item in attempts):
        group.after_reveal_count += 1

    verdict = entry.verdict
    if verdict == "correct_independent":
        group.correct_count += 1
    elif verdict == "unclear":
        group.unclear_count += 1
    elif verdict == "insufficient":
        group.insufficient_count += 1


def _group_sort_key(group: MemoryGroupView) -> tuple:
    priority = 8
    if group.wrong_count:
        priority = 0
    elif group.after_reveal_count:
        priority = 1
    elif group.unclear_count:
        priority = 2
    elif group.insufficient_count:
        priority = 3
    elif group.no_attempt_count:
        priority = 4
    elif group.self_report_count:
        priority = 5
    elif group.earlier_wrong_count:
        priority = 6
    elif group.excluded_evaluation_count:
        priority = 7
    latest = group.latest_at or datetime.min.replace(tzinfo=timezone.utc)
    return (priority, -latest.timestamp(), -(group.entry_ids.__len__()), group.label)


def _short_date(value: datetime | None) -> str:
    if value is None:
        return "时间未知"
    return value.astimezone().strftime("%m-%d")


def suggest(group: MemoryGroupView) -> str | None:
    """一组知识点对应一句建议；只描述证据，不下“会不会”的结论。"""

    label = group.label
    if group.wrong_count:
        return (
            f"{label}：独立作答被判错 {group.wrong_count} 次"
            f"（最近一次判错 {_short_date(group.latest_wrong_at)}），"
            "建议先补这一块的判定条件和常见错误"
        )
    if group.after_reveal_count:
        return (
            f"{label}：有 {group.after_reveal_count} 次是看过解答后作答，"
            "建议遮住解答独立重做一遍"
        )
    if group.unclear_count:
        return (
            f"{label}：有 {group.unclear_count} 次作答的独立性没有确认，"
            "建议遮住解答独立重做一次，再按结果判断"
        )
    if group.insufficient_count:
        return (
            f"{label}：有 {group.insufficient_count} 次过程不足或评价未完成，"
            "下次把每一步写完整再提交"
        )
    if group.excluded_evaluation_count:
        return (
            f"{label}：有 {group.excluded_evaluation_count} 道练习的作答评价已被你排除，"
            "不参与判断；如果需要重新评估，可以再独立做一遍"
        )
    if group.no_attempt_count:
        return (
            f"{label}：有 {group.no_attempt_count} 道练习还没有作答，"
            "建议先独立做一遍再对照"
        )
    if group.self_report_count:
        return (
            f"{label}：你自述听懂了，但还没有作答证据，建议做一道题确认一下"
        )
    if group.correct_count:
        return (
            f"{label}：独立作答正确 {group.correct_count} 次，"
            "一次正确不代表已经掌握，可以再做一道变式题确认"
        )
    return None


def build_view(
    *,
    status: str,
    course: str | None,
    window: MemoryRange,
    entries: list[EntryEvidence],
    pending: list[EntryEvidence],
    excluded_entries: int,
    cutoff: datetime,
    pending_total: int | None = None,
    message: str = "",
) -> LearningMemoryView:
    active = [item for item in entries if item.validity == "ok"]
    counts = MemoryCounts(
        evidence=len(entries),
        # 练习数按“练习”去重；作答次数单独统计，不能拿作答次数当练习数。
        practices=sum(1 for item in entries if item.kind == "exercise"),
        questions=count_questions(entries),
        attempts=sum(len(item.attempts) for item in entries),
        excluded=excluded_entries,
        pending_course=len(pending) if pending_total is None else pending_total,
        wrong_independent=sum(
            1 for item in active if item.wrong_at is not None and item.wrong_at >= cutoff
        ),
        unclear=sum(1 for item in active if item.verdict == "unclear"),
        excluded_evaluation=sum(
            1
            for item in active
            if item.attempts
            and not [attempt for attempt in item.attempts if not attempt.excluded]
        ),
        earlier_wrong=sum(
            1 for item in active if item.wrong_at is not None and item.wrong_at < cutoff
        ),
        superseded=sum(1 for item in entries if item.validity == "superseded"),
        unverified=sum(1 for item in entries if item.validity == "unverified"),
    )
    groups = group_entries(entries, cutoff=cutoff)
    suggestions: list[str] = []
    for group in groups:
        text = suggest(group)
        if text is None:
            continue
        group.suggestion = text
        if len(suggestions) < MAX_SUGGESTIONS:
            suggestions.append(text)
    return LearningMemoryView(
        status=status,  # type: ignore[arg-type]
        course=course,
        range=window,
        range_text=describe_range(course, window, counts),
        counts=counts,
        groups=groups,
        entries=[item.to_view() for item in entries],
        suggestions=suggestions,
        pending_course_entries=[item.to_view() for item in pending],
        message=message,
    )


def empty_view(course: str | None, *, days: int, limit: int, now: datetime) -> LearningMemoryView:
    window = MemoryRange(days=days, limit=limit)
    counts = MemoryCounts()
    return LearningMemoryView(
        status="empty",
        course=course,
        range=window,
        range_text=describe_range(course, window, counts),
        counts=counts,
        message=f"{course or '全部课程'}近 {days} 天没有学习记录。",
    )


def unavailable_view(course: str | None, *, days: int, limit: int) -> LearningMemoryView:
    return LearningMemoryView(
        status="unavailable",
        course=course,
        range=MemoryRange(days=days, limit=limit),
        range_text="暂时无法读取学习记忆",
        message="暂时无法读取学习记忆，请稍后再试。",
    )


def _block_len(lines: list[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def _group_line(group) -> str:
    parts: list[str] = []
    if group.wrong_count:
        parts.append(
            f"独立作答被判错 {group.wrong_count} 次"
            f"（最近一次判错 {_short_date(group.latest_wrong_at)}）"
        )
    if group.earlier_wrong_count:
        parts.append(
            f"另有 {group.earlier_wrong_count} 次判错早于本窗口，"
            "只作历史回看，不计入本窗口统计"
        )
    if group.correct_count:
        parts.append(f"独立作答正确 {group.correct_count} 次（一次正确不代表掌握）")
    if group.after_reveal_count:
        parts.append(f"看过解答后对照作答 {group.after_reveal_count} 次（不算独立掌握）")
    if group.unclear_count:
        parts.append(f"独立性未确认 {group.unclear_count} 次（不按独立对错统计）")
    if group.insufficient_count:
        parts.append(f"过程不足或评价未完成 {group.insufficient_count} 次")
    if group.excluded_evaluation_count:
        parts.append(
            f"{group.excluded_evaluation_count} 道练习的作答评价已被排除"
            "（已有作答，不按未作答计）"
        )
    if group.no_attempt_count:
        parts.append(f"还没有作答 {group.no_attempt_count} 次")
    if group.self_report_count:
        parts.append("学生自述理解（与作答证据分开）")
    suffix = "" if group.normalized else "（原标签，未归一词表）"
    return (
        f"- {group.label}{suffix}：{'；'.join(parts)}；最近 {_short_date(group.latest_at)}"
    )


def _compact_group_line(group) -> str:
    """预算紧张时的分组行：保留全部计数，去掉解释性长句。"""

    counts = (
        ("判错", group.wrong_count),
        ("早前判错", group.earlier_wrong_count),
        ("做对", group.correct_count),
        ("看答案", group.after_reveal_count),
        ("未确认", group.unclear_count),
        ("过程不足", group.insufficient_count),
        ("评价排除", group.excluded_evaluation_count),
        ("未作答", group.no_attempt_count),
        ("自述", group.self_report_count),
    )
    shown = " ".join(f"{name}{value}" for name, value in counts if value)
    return f"- {group.label[:18]}：{shown or '无计数'}"


def material_block(
    view: LearningMemoryView,
    limit: int = MATERIAL_LIMIT,
    *,
    focus_entry_ids: Iterable[str] = (),
) -> str:
    """正文模型能看到的记忆材料：公开事实、真实范围、不含答案。

    ``focus_entry_ids`` 是学生本轮明确提到的条目。装配顺序是
    “头部 → 目标明细 → 分组统计 → 其他明细 → 待确认 → 建议 → 结尾”，
    目标明细先占位、不参与截断；统计放不下时先换成紧凑分组行，再减少组数，
    最后才压缩其他明细。这样记录很多、标签很长时目标附注也不会被整体截掉。
    """

    if view.status != "ok":
        return view.message or "暂时无法读取学习记忆"
    focus = set(focus_entry_ids)
    detail_entries = list(view.entries) + [
        entry for entry in view.pending_course_entries if entry.entry_id in focus
    ]
    focus_entries = [entry for entry in detail_entries if entry.entry_id in focus]
    other_entries = [entry for entry in detail_entries if entry.entry_id not in focus]

    focus_lines = _entry_lines(
        focus_entries,
        focus_ids=focus,
        budget=FOCUS_DETAIL_BUDGET,
        line_limit=FOCUS_LINE_LIMIT,
    )
    header = f"【学习记忆】{view.range_text}"
    full_groups = [_group_line(group) for group in view.groups[:5]]
    compact_groups = [_compact_group_line(group) for group in view.groups[:5]]
    stale_lines: list[str] = []
    stale: list[str] = []
    if view.counts.superseded:
        stale.append(f"{view.counts.superseded} 条依据来自旧版本")
    if view.counts.unverified:
        stale.append(f"{view.counts.unverified} 条依据无法核对题库版本")
    if stale:
        stale_lines.append(f"- 另有 {'、'.join(stale)}，未计入以上统计，只作历史回看")
    pending_lines = (
        [
            f"- 另有 {view.counts.pending_course} 条记录归属待确认，"
            "没有计入本课程摘要"
        ]
        if view.counts.pending_course
        else []
    )
    suggestion_lines: list[str] = []
    if view.suggestions:
        suggestion_lines = ["【可给的复习建议（依据以上记录）】"] + [
            f"{index}. {text}" for index, text in enumerate(view.suggestions, start=1)
        ]
    closing = [
        "以上只描述已有记录：不要下“已掌握”的结论，不要计算比例，"
        "一次答对不等于永久掌握。"
    ]

    # 先扣除目标明细与固定尾部，剩下的才是统计和其他明细的预算。
    available = limit - _block_len([header] + focus_lines + stale_lines + pending_lines)
    available -= _block_len(suggestion_lines + closing)

    groups: list[str] = []
    for candidate in (full_groups, compact_groups):
        if candidate and _block_len(candidate) <= available:
            groups = candidate
            break
    else:
        if compact_groups:
            keep = len(compact_groups)
            while keep > 0:
                candidate = compact_groups[:keep] + [
                    f"- 另有 {len(compact_groups) - keep} 个知识点未列出"
                ]
                if _block_len(candidate) <= available:
                    groups = candidate
                    break
                keep -= 1
    available -= _block_len(groups)

    other_lines: list[str] = []
    if other_entries:
        detail_head = "【逐条记录（同一道练习按作答顺序保留关键变化）】"
        room = available - len(detail_head) - 1
        raw = _entry_lines(other_entries, budget=max(0, room))
        if raw:
            candidate = [detail_head] + raw
            if _block_len(candidate) > available:
                kept: list[str] = []
                used = len(detail_head) + 1
                shown = 0
                for text in raw:
                    if text.startswith("- 还有"):
                        break
                    if used + len(text) + 1 > available:
                        break
                    kept.append(text)
                    used += len(text) + 1
                    shown += 1
                # 必须留出一行告诉模型还有多少条没有展开，不能静默丢掉。
                remaining = len(other_entries) - shown
                while True:
                    note = f"- 还有 {remaining} 条带附注或多次作答的记录未展开"
                    if _block_len([detail_head] + kept + [note]) <= available:
                        break
                    if not kept:
                        break
                    kept.pop()
                    remaining += 1
                candidate = [detail_head] + kept
                if _block_len(candidate + [note]) <= available:
                    candidate.append(note)
            other_lines = candidate

    def assemble() -> list[str]:
        return (
            [header]
            + focus_lines
            + groups
            + stale_lines
            + other_lines
            + pending_lines
            + suggestion_lines
            + closing
        )

    lines = assemble()
    # 兜底：只有非目标内容会被继续压缩，目标明细始终保留。
    while _block_len(lines) > limit:
        if other_lines:
            other_lines.pop()
        elif len(suggestion_lines) > 1:
            suggestion_lines.pop()
        elif suggestion_lines:
            suggestion_lines.clear()
        elif groups:
            groups.pop()
        elif stale_lines:
            stale_lines.pop()
        elif pending_lines:
            pending_lines.pop()
        else:
            break
        lines = assemble()
    return "\n".join(lines)[:limit]


def _attempt_sequence(entry: MemoryEntryView) -> str:
    """按作答顺序列出关键变化；相邻同等级合并，避免同一结论重复占篇幅。"""

    segments: list[tuple[datetime, str]] = []
    for attempt in entry.attempts:
        if attempt.excluded:
            continue
        label = VERDICT_LABELS.get(attempt.verdict, attempt.verdict)
        if segments and segments[-1][1] == label:
            segments[-1] = (attempt.recorded_at, label)
        else:
            segments.append((attempt.recorded_at, label))
    if not segments:
        return ""
    if len(segments) == 1:
        moment, label = segments[0]
        return f"{_short_date(moment)} {label}"
    parts = [f"首次 {_short_date(segments[0][0])} {segments[0][1]}"]
    middle = segments[1:-1]
    for moment, label in middle[:2]:
        parts.append(f"{_short_date(moment)} {label}")
    if len(middle) > 2:
        parts.append(f"中间还有 {len(middle) - 2} 次")
    parts.append(f"最近 {_short_date(segments[-1][0])} {segments[-1][1]}")
    if len(segments) > MATERIAL_ATTEMPT_SEGMENTS:
        parts.append(f"共 {len(segments)} 次作答")
    return " → ".join(parts)


def _entry_line(entry: MemoryEntryView) -> str:
    label = (entry.statement_excerpt or "未提供题干").strip()[:36]
    sequence = _attempt_sequence(entry)
    if sequence:
        text = f"- {label}：{sequence}"
    else:
        text = f"- {label}：{_short_date(entry.recorded_at)} {VERDICT_LABELS.get(entry.verdict, entry.verdict)}"
    if entry.note:
        text += f"；附注：{entry.note[:60]}"
    if entry.validity != "ok" and entry.validity_note:
        text += f"；{entry.validity_note[:60]}"
    return text


def _entry_lines(
    entries: list[MemoryEntryView],
    *,
    focus_ids: set[str] | None = None,
    budget: int = MATERIAL_DETAIL_BUDGET,
    line_limit: int | None = None,
) -> list[str]:
    """逐条记录：优先展开学生本轮问到的条目，再按预算补充其余条目。

    不固定条数：材料只有 415 字时，第五条的附注也必须能进入正文材料；预算
    用完才截断，并明确告诉模型“还有多少条没有展开”。
    """

    picked = [
        entry
        for entry in entries
        if entry.validity != "ok" or entry.note or len(entry.attempts) > 1
    ]
    focus = set(focus_ids or ())
    ordered = [entry for entry in picked if entry.entry_id in focus]
    ordered.extend(entry for entry in picked if entry.entry_id not in focus)

    lines: list[str] = []
    used = 0
    for index, entry in enumerate(ordered):
        text = _entry_line(entry)
        if line_limit is not None:
            text = text[:line_limit]
        if index and used + len(text) + 1 > budget:
            omitted = len(ordered) - index
            lines.append(f"- 还有 {omitted} 条带附注或多次作答的记录未展开")
            break
        lines.append(text)
        used += len(text) + 1
    return lines


def context_line(view: LearningMemoryView, limit: int = 400) -> str:
    """决策阶段的一行上下文；比正文材料短，只保留最要紧的事实。"""

    if view.status != "ok":
        return f"【学习记忆】{view.message or '暂时无法读取学习记忆'}"
    head = f"【学习记忆】{view.range_text}"
    if view.suggestions:
        head += "；建议顺序：" + "；".join(view.suggestions[:2])
    elif view.counts.evidence:
        head += "；已有记录但没有需要优先补的知识点"
    if view.counts.pending_course:
        head += f"；另有 {view.counts.pending_course} 条归属待确认"
    return head[:limit]


def window_bounds(days: int, now: datetime) -> datetime:
    return now - timedelta(days=days)