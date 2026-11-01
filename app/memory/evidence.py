"""F009：从既有学习记录投影出“可追溯证据”。

这一层是只读的：只查表、只整理，不写库、不调用模型、不改写任何原记录。
投影而不是物化，是 F009 的关键取舍——删掉的证据不会因为“重新汇总”又长
回来，因为排除标记每次读取时都会重新生效。

能进入摘要的字段是白名单：题干摘录、知识点标签、判断标签、真实时间、来源
标识。练习参考解答、真题答案、独立解答、参考代码从不读取。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.memory.models import (
    VALIDITY_NOTES,
    VERDICT_LABELS,
    MemoryAttemptView,
    MemoryEntryView,
    build_entry_id,
)
from app.papers.courses import normalize_course

# (课程, 试卷标题)；目录不可用或试卷未登记时返回 (None, None)。
PaperLookup = Callable[[str | None], tuple[str | None, str | None]]

_SPLIT = re.compile(r"[,，、;；/]")
_MAX_KNOWLEDGE_POINTS = 6
_STATEMENT_EXCERPT = 80


@dataclass
class AttemptEvidence:
    attempt_id: str
    attempt_no: int
    kind: str
    verdict: str
    is_correct: bool | None
    needs_supplement: bool
    answered_after_revealed: bool
    independence: str | None
    recorded_at: datetime
    excluded: bool = False

    def to_view(self) -> MemoryAttemptView:
        return MemoryAttemptView(
            attempt_id=self.attempt_id,
            attempt_no=self.attempt_no,
            kind=self.kind,
            verdict=self.verdict,  # type: ignore[arg-type]
            label=VERDICT_LABELS.get(self.verdict, self.verdict),
            is_correct=self.is_correct,
            needs_supplement=self.needs_supplement,
            answered_after_revealed=self.answered_after_revealed,
            independence=self.independence,
            recorded_at=self.recorded_at,
            excluded=self.excluded,
        )


@dataclass
class EntryEvidence:
    entry_id: str
    kind: str
    course: str | None
    course_source: str
    knowledge_points: list[str] = field(default_factory=list)
    knowledge_point_slugs: list[str | None] = field(default_factory=list)
    knowledge_points_normalized: list[bool] = field(default_factory=list)
    knowledge_point_status: list[str] = field(default_factory=list)
    knowledge_points_truncated: bool = False
    statement_excerpt: str | None = None
    verdict: str = "no_attempt"
    first_verdict: str = "no_attempt"
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    first_at: datetime | None = None
    wrong_at: datetime | None = None
    after_reveal_at: datetime | None = None
    unclear_at: datetime | None = None
    validity: str = "ok"
    validity_note: str | None = None
    note: str | None = None
    source: dict[str, Any] = field(default_factory=dict)
    attempts: list[AttemptEvidence] = field(default_factory=list)
    record_id: str | None = None
    excluded: bool = False
    problem_id: str | None = None
    root_problem_id: str | None = None

    def to_view(self) -> MemoryEntryView:
        return MemoryEntryView(
            entry_id=self.entry_id,
            kind=self.kind,  # type: ignore[arg-type]
            course=self.course,
            course_source=self.course_source,  # type: ignore[arg-type]
            knowledge_points=list(self.knowledge_points),
            knowledge_point_slugs=list(self.knowledge_point_slugs),
            knowledge_points_normalized=list(self.knowledge_points_normalized),
            knowledge_points_truncated=self.knowledge_points_truncated,
            statement_excerpt=self.statement_excerpt,
            verdict=self.verdict,  # type: ignore[arg-type]
            first_verdict=self.first_verdict,  # type: ignore[arg-type]
            recorded_at=self.recorded_at,
            first_at=self.first_at,
            wrong_at=self.wrong_at,
            validity=self.validity,  # type: ignore[arg-type]
            validity_note=self.validity_note,
            note=self.note,
            source=dict(self.source),
            attempts=[item.to_view() for item in self.attempts],
            record_id=self.record_id,
            excluded=self.excluded,
        )


@dataclass
class EvidenceBundle:
    entries: list[EntryEvidence] = field(default_factory=list)
    excluded_entries: int = 0
    fallback_used: bool = False
    pending: list[EntryEvidence] = field(default_factory=list)
    pending_total: int = 0


def parse_time(raw: Any) -> datetime | None:
    """把库里存的 ISO 字符串解析成带时区的 datetime；失败返回 None。"""

    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def split_knowledge_points(raw: str | None) -> tuple[list[str], bool]:
    """把模型生成的多标签文本拆成去重列表；返回 (标签, 是否被截断)。"""

    text = (raw or "").strip()
    if not text:
        return [], False
    seen: set[str] = set()
    items: list[str] = []
    for piece in _SPLIT.split(text):
        label = piece.strip().strip("。.;；")
        if not label:
            continue
        key = re.sub(r"\s+", "", label).casefold()
        if key in seen:
            continue
        seen.add(key)
        items.append(label[:60])
    if len(items) > _MAX_KNOWLEDGE_POINTS:
        return items[:_MAX_KNOWLEDGE_POINTS], True
    return items, False


class KnowledgeNormalizer:
    """知识点归一：命中词表就用官方名，不命中保留原标签。

    当前真实数据里模型生成的标签只有约一成能对上词表，所以这里刻意不做
    模糊匹配：宁可显示“未归一，原标签”，也不把两个概念强行合并。
    """

    def __init__(self, taxonomy: Any | None = None) -> None:
        self._taxonomy = taxonomy

    @property
    def available(self) -> bool:
        return self._taxonomy is not None

    def normalize(self, raw: str) -> tuple[str, str | None]:
        if self._taxonomy is None:
            return raw, None
        try:
            slug = self._taxonomy.normalize(raw)
        except Exception:  # 词表异常不影响记忆读取
            return raw, None
        if not slug:
            return raw, None
        try:
            return self._taxonomy.name(slug), slug
        except Exception:
            return raw, slug


def _attempt_verdict(
    evaluation: dict[str, Any] | None,
    *,
    answered_after_revealed: bool,
) -> str:
    """一次作答的判断标签；顺序即优先级。

    过程不足（评价缺失或要求补充）优先于对错：这类证据不累计为确认答错。
    看过解答后的对照作答单独成类，永远不算独立作答。
    独立性未确认（``unclear``，或旧记录里根本没有该字段却判错）也不能算作
    “独立作答被判错”：宁可标注“未确认”，也不把模糊证据说成确定的独立表现。
    """

    if not isinstance(evaluation, dict):
        return "insufficient"
    if evaluation.get("needs_supplement"):
        return "insufficient"
    independence = evaluation.get("independence_assessment")
    if answered_after_revealed or independence == "after_reveal":
        return "after_reveal"
    if independence == "unclear":
        return "unclear"
    if evaluation.get("is_correct") is False:
        if independence is None:
            # 旧记录没有独立性字段：不能断言是独立作答。
            return "unclear"
        return "wrong_independent"
    if evaluation.get("is_correct") is True:
        return "correct_independent"
    return "insufficient"


def _load_evaluation(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _attempt_rows(
    connection: sqlite3.Connection, exercise_ids: list[str]
) -> dict[str, list[sqlite3.Row]]:
    if not exercise_ids:
        return {}
    marks = ",".join("?" for _ in exercise_ids)
    rows = connection.execute(
        "SELECT id, exercise_id, attempt_no, kind, evaluation_json, "
        "answered_after_revealed, created_at "
        f"FROM exercise_attempts WHERE exercise_id IN ({marks}) "
        "ORDER BY exercise_id, attempt_no",
        exercise_ids,
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["exercise_id"]), []).append(row)
    return grouped


def _question_snapshot(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _knowledge_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    items = snapshot.get("knowledge_points")
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        cleaned.append(
            {
                "name": name[:60],
                "slug": str(item.get("slug") or "").strip() or None,
                "status": str(item.get("status") or "candidate"),
            }
        )
    return cleaned[: _MAX_KNOWLEDGE_POINTS]


def _apply_correction(
    entry: EntryEvidence, correction: tuple[Any, Any, Any] | None
) -> None:
    """用户的纠正优先于自动推导；空串课程表示“改为待确认”。"""

    if correction is None:
        return
    course, knowledge_point, note = correction
    if course is not None:
        # 写入时已归一；这里再归一一次，兼容历史行里保存的别名（如 “Python”）。
        entry.course = normalize_course(course) if course else None
        entry.course_source = "user"
    if knowledge_point:
        entry.knowledge_points = [knowledge_point]
        entry.knowledge_point_slugs = [None]
        entry.knowledge_points_normalized = [False]
        entry.knowledge_point_status = ["user"]
        entry.knowledge_points_truncated = False
    if note:
        entry.note = note


def _course_from_paper(
    paper_lookup: PaperLookup, paper_id: str | None
) -> tuple[str | None, str | None]:
    if not paper_id:
        return None, None
    try:
        course, title = paper_lookup(paper_id)
    except Exception:
        return None, None
    return (normalize_course(course) if course else None), title


def collect_evidence(
    connection: sqlite3.Connection,
    *,
    course: str | None,
    paper_lookup: PaperLookup,
    normalizer: KnowledgeNormalizer,
    corrections: dict[str, tuple[Any, Any, Any]],
    exclusions: set[str],
    current_build_id: str | None,
    now: datetime,
    days: int = 30,
    limit: int = 50,
) -> EvidenceBundle:
    """投影指定课程在时间窗内的证据；未知归属的条目单独返回。

    ``course`` 为空表示“还没有选定课程”：此时不返回任何课程内容，只返回
    待确认归属的条目，避免把多门课混成一份摘要。
    """

    all_entries = collect_all(
        connection,
        paper_lookup=paper_lookup,
        normalizer=normalizer,
        corrections=corrections,
        exclusions=exclusions,
        current_build_id=current_build_id,
    )
    return select_window(
        all_entries.entries,
        course=course,
        days=days,
        limit=limit,
        now=now,
        excluded_entries=all_entries.excluded_entries,
    )


def collect_all(
    connection: sqlite3.Connection,
    *,
    paper_lookup: PaperLookup,
    normalizer: KnowledgeNormalizer,
    corrections: dict[str, tuple[Any, Any, Any]],
    exclusions: set[str],
    current_build_id: str | None,
) -> EvidenceBundle:
    """投影全部证据（不截断），供课程列表与按课程汇总共同使用。"""

    newest_version = {
        str(row["root_problem_id"]): int(row["max_version"])
        for row in connection.execute(
            "SELECT root_problem_id, MAX(version) AS max_version "
            "FROM problems GROUP BY root_problem_id"
        ).fetchall()
    }
    record_rows = connection.execute(
        "SELECT id, problem_id FROM mistake_records WHERE problem_id IS NOT NULL"
    ).fetchall()
    record_by_problem = {str(row["problem_id"]): str(row["id"]) for row in record_rows}

    exercise_rows = connection.execute(
        "SELECT e.id, e.conversation_id, e.problem_id, e.root_problem_id, "
        "e.knowledge_point, e.statement_text, e.created_at, "
        "p.version AS problem_version, p.source_type, "
        "p.paper_id AS problem_paper_id, p.source_title, "
        "p.self_reported_understood, "
        "q.question_id, q.paper_id AS question_paper_id, "
        "q.build_id, q.snapshot_json "
        "FROM exercises e "
        "LEFT JOIN problems p ON p.id = e.problem_id "
        "LEFT JOIN exercise_question_sources q ON q.exercise_id = e.id "
        "ORDER BY e.created_at DESC"
    ).fetchall()
    attempts = _attempt_rows(connection, [str(row["id"]) for row in exercise_rows])

    bundle = EvidenceBundle()
    for row in exercise_rows:
        entry = _exercise_entry(
            row,
            attempts.get(str(row["id"]), []),
            newest_version=newest_version,
            record_by_problem=record_by_problem,
            paper_lookup=paper_lookup,
            normalizer=normalizer,
            current_build_id=current_build_id,
        )
        _apply_correction(entry, corrections.get(entry.entry_id))
        if entry.entry_id in exclusions:
            entry.excluded = True
            bundle.excluded_entries += 1
            continue
        if any(
            build_entry_id("attempt", item.attempt_id) in exclusions
            for item in entry.attempts
        ):
            for item in entry.attempts:
                if build_entry_id("attempt", item.attempt_id) in exclusions:
                    item.excluded = True
            _refresh_entry_verdict(entry)
        bundle.entries.append(entry)

    selfreport_rows = connection.execute(
        "SELECT p.id, p.conversation_id, p.root_problem_id, p.version, "
        "p.source_type, p.paper_id, p.source_title, p.statement_text, "
        "p.explanation_json, p.updated_at "
        "FROM problems p WHERE p.self_reported_understood = 1 "
        "AND NOT EXISTS (SELECT 1 FROM exercises e WHERE e.problem_id = p.id) "
        "ORDER BY p.updated_at DESC"
    ).fetchall()
    for row in selfreport_rows:
        entry = _selfreport_entry(
            row,
            record_by_problem=record_by_problem,
            paper_lookup=paper_lookup,
            normalizer=normalizer,
            newest_version=newest_version,
        )
        _apply_correction(entry, corrections.get(entry.entry_id))
        if entry.entry_id in exclusions:
            entry.excluded = True
            bundle.excluded_entries += 1
            continue
        bundle.entries.append(entry)

    bundle.entries.sort(key=lambda item: (item.recorded_at, item.entry_id), reverse=True)
    return bundle


def select_window(
    entries: list[EntryEvidence],
    *,
    course: str | None,
    days: int,
    limit: int,
    now: datetime,
    excluded_entries: int = 0,
) -> EvidenceBundle:
    """按课程 + 时间窗 + 条数截取，并单独挑出归属待确认的条目。"""

    wanted = normalize_course(course) if course else None
    pending_all = [item for item in entries if item.course is None]
    bundle = EvidenceBundle(
        pending=pending_all[:5],
        pending_total=len(pending_all),
        excluded_entries=excluded_entries,
    )
    if wanted is None:
        return bundle
    pool = [
        item
        for item in entries
        if item.course is not None and normalize_course(item.course) == wanted
    ]
    cutoff = now - timedelta(days=days)
    recent = [item for item in pool if item.recorded_at >= cutoff]
    if not recent and pool:
        # 近 N 天没有记录时回退到最近 5 条，并明确标记“不是近期”，
        # 不能让页面或回复一边统计全部历史一边说“近期表现”。
        bundle.fallback_used = True
        bundle.entries = pool[: min(limit, 5)]
        return bundle
    bundle.entries = recent[:limit]
    return bundle


def _refresh_entry_verdict(entry: EntryEvidence) -> None:
    valid = [item for item in entry.attempts if not item.excluded]
    entry.verdict = _entry_verdict(valid)
    entry.first_verdict = _first_verdict(valid)
    if valid:
        entry.first_at = min(valid, key=lambda item: item.attempt_no).recorded_at
    entry.wrong_at = _latest_time(valid, "wrong_independent")
    entry.after_reveal_at = _latest_time(valid, "after_reveal")
    entry.unclear_at = _latest_time(valid, "unclear")
    if not valid and entry.attempts:
        entry.verdict = "insufficient"
        entry.first_verdict = "insufficient"
        entry.note = (entry.note + "；" if entry.note else "") + "你已排除这条评价，未再用于判断"


def _latest_time(attempts: list[AttemptEvidence], verdict: str) -> datetime | None:
    times = [item.recorded_at for item in attempts if item.verdict == verdict]
    return max(times) if times else None


def _entry_verdict(attempts: list[AttemptEvidence]) -> str:
    """条目的“当前状态”取最近一次有效作答，而不是首次。

    首次表现另由 ``first_verdict`` 保存，材料里同时展示首末变化，避免“补充后
    已经做对”仍被写成“过程不完整”。
    """

    if not attempts:
        return "no_attempt"
    latest = max(attempts, key=lambda item: (item.attempt_no, item.recorded_at))
    return latest.verdict


def _first_verdict(attempts: list[AttemptEvidence]) -> str:
    if not attempts:
        return "no_attempt"
    first = min(attempts, key=lambda item: (item.attempt_no, item.recorded_at))
    return first.verdict


def _exercise_entry(
    row: sqlite3.Row,
    attempt_rows: list[sqlite3.Row],
    *,
    newest_version: dict[str, int],
    record_by_problem: dict[str, str],
    paper_lookup: PaperLookup,
    normalizer: KnowledgeNormalizer,
    current_build_id: str | None,
) -> EntryEvidence:
    attempt_items: list[AttemptEvidence] = []
    for attempt in attempt_rows:
        evaluation = _load_evaluation(attempt["evaluation_json"])
        verdict = _attempt_verdict(
            evaluation,
            answered_after_revealed=bool(attempt["answered_after_revealed"]),
        )
        attempt_items.append(
            AttemptEvidence(
                attempt_id=str(attempt["id"]),
                attempt_no=int(attempt["attempt_no"]),
                kind=str(attempt["kind"]),
                verdict=verdict,
                is_correct=(
                    None if evaluation is None else evaluation.get("is_correct")
                ),
                needs_supplement=bool(
                    evaluation is not None and evaluation.get("needs_supplement")
                ),
                answered_after_revealed=bool(attempt["answered_after_revealed"]),
                independence=(
                    None if evaluation is None else evaluation.get("independence_assessment")
                ),
                recorded_at=parse_time(attempt["created_at"])
                or datetime.now(timezone.utc),
            )
        )

    snapshot = _question_snapshot(row["snapshot_json"])
    knowledge = _knowledge_from_snapshot(snapshot)
    if knowledge:
        names = [item["name"] for item in knowledge]
        slugs = [item["slug"] for item in knowledge]
        normalized_flags = [item["slug"] is not None for item in knowledge]
        statuses = [item["status"] for item in knowledge]
        truncated = False
    else:
        raw_items, truncated = split_knowledge_points(row["knowledge_point"])
        names, slugs, normalized_flags = [], [], []
        for item in raw_items:
            display, slug = normalizer.normalize(item)
            names.append(display)
            slugs.append(slug)
            normalized_flags.append(slug is not None)
        statuses = ["verified" if flag else "raw" for flag in normalized_flags]

    paper_id = row["question_paper_id"] or row["problem_paper_id"]
    course, paper_title = _course_from_paper(paper_lookup, paper_id)
    course_source = (
        "paper"
        if row["question_paper_id"]
        else ("problem" if row["problem_paper_id"] else "unknown")
    )

    problem_version = row["problem_version"]
    root = str(row["root_problem_id"])
    validity, validity_note = "ok", None
    if problem_version is not None and newest_version.get(root, int(problem_version)) > int(
        problem_version
    ):
        validity = "superseded"
        validity_note = (
            f"{VALIDITY_NOTES['superseded']}（第 {problem_version} 版，"
            f"当前第 {newest_version[root]} 版）"
        )
    elif row["build_id"]:
        if current_build_id and row["build_id"] != current_build_id:
            validity = "unverified"
            validity_note = f"依据为旧题库版本（build {str(row['build_id'])[:11]}）"

    recorded_at = (
        max(attempt_items, key=lambda item: item.attempt_no).recorded_at
        if attempt_items
        else parse_time(row["created_at"]) or datetime.now(timezone.utc)
    )
    first_attempt = (
        min(attempt_items, key=lambda item: item.attempt_no) if attempt_items else None
    )
    entry = EntryEvidence(
        entry_id=build_entry_id("exercise", str(row["id"])),
        kind="exercise",
        course=course,
        course_source=course_source,
        knowledge_points=names,
        knowledge_point_slugs=slugs,
        knowledge_points_normalized=normalized_flags,
        knowledge_point_status=statuses,
        knowledge_points_truncated=truncated,
        statement_excerpt=_excerpt(row["statement_text"]),
        verdict=_entry_verdict(attempt_items),
        first_verdict=_first_verdict(attempt_items),
        recorded_at=recorded_at,
        first_at=(
            first_attempt.recorded_at
            if first_attempt is not None
            else recorded_at
        ),
        wrong_at=_latest_time(attempt_items, "wrong_independent"),
        after_reveal_at=_latest_time(attempt_items, "after_reveal"),
        unclear_at=_latest_time(attempt_items, "unclear"),
        validity=validity,  # type: ignore[arg-type]
        validity_note=validity_note,
        source={
            "conversation_id": str(row["conversation_id"]),
            "exercise_id": str(row["id"]),
            "problem_id": (str(row["problem_id"]) if row["problem_id"] else None),
            "problem_version": int(problem_version) if problem_version is not None else None,
            "question_id": row["question_id"],
            "paper_id": paper_id,
            "paper_title": paper_title or row["source_title"],
            "number_label": snapshot.get("number_label"),
            "build_id": row["build_id"],
        },
        attempts=attempt_items,
        record_id=record_by_problem.get(str(row["problem_id"]))
        if row["problem_id"]
        else None,
        problem_id=str(row["problem_id"]) if row["problem_id"] else None,
        root_problem_id=root,
    )
    if row["self_reported_understood"]:
        entry.note = "学生当时自述理解（与作答证据分开）"
    return entry


def _selfreport_entry(
    row: sqlite3.Row,
    *,
    record_by_problem: dict[str, str],
    paper_lookup: PaperLookup,
    normalizer: KnowledgeNormalizer,
    newest_version: dict[str, int],
) -> EntryEvidence:
    explanation = _question_snapshot(row["explanation_json"])
    raw_points: list[str] = []
    for item in explanation.get("knowledge_points") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                raw_points.append(name[:60])
        elif isinstance(item, str) and item.strip():
            raw_points.append(item.strip()[:60])
    raw_points = raw_points[:_MAX_KNOWLEDGE_POINTS]
    names, slugs, flags = [], [], []
    for item in raw_points:
        display, slug = normalizer.normalize(item)
        names.append(display)
        slugs.append(slug)
        flags.append(slug is not None)

    paper_id = row["paper_id"]
    course, paper_title = _course_from_paper(paper_lookup, paper_id)
    root = str(row["root_problem_id"])
    version = row["version"]
    validity, validity_note = "ok", None
    if version is not None and newest_version.get(root, int(version)) > int(version):
        validity = "superseded"
        validity_note = f"{VALIDITY_NOTES['superseded']}（第 {version} 版）"
    return EntryEvidence(
        entry_id=build_entry_id("selfreport", str(row["id"])),
        kind="selfreport",
        course=course,
        course_source="problem" if row["paper_id"] else "unknown",
        knowledge_points=names,
        knowledge_point_slugs=slugs,
        knowledge_points_normalized=flags,
        knowledge_point_status=[
            "verified" if flag else "raw" for flag in flags
        ],
        statement_excerpt=_excerpt(row["statement_text"]),
        verdict="self_report",
        first_verdict="self_report",
        recorded_at=parse_time(row["updated_at"]) or datetime.now(timezone.utc),
        first_at=parse_time(row["updated_at"]) or datetime.now(timezone.utc),
        validity=validity,  # type: ignore[arg-type]
        validity_note=validity_note,
        source={
            "conversation_id": str(row["conversation_id"]),
            "problem_id": str(row["id"]),
            "problem_version": int(version) if version is not None else None,
            "paper_id": paper_id,
            "paper_title": paper_title or row["source_title"],
            "build_id": None,
            "question_id": None,
            "number_label": None,
            "exercise_id": None,
        },
        record_id=record_by_problem.get(str(row["id"])),
        problem_id=str(row["id"]),
        root_problem_id=root,
    )


def _excerpt(text: Any) -> str | None:
    value = " ".join(str(text or "").split())
    if not value:
        return None
    return value[:_STATEMENT_EXCERPT]


def count_questions(entries: list[EntryEvidence]) -> int:
    """不同原题数量：同一道题的多道练习只算一道题。"""

    roots = {item.root_problem_id for item in entries if item.root_problem_id}
    return len(roots) if roots else 0