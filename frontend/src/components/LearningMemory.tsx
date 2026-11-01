import { useCallback, useEffect, useMemo, useState } from "react";
import {
  correctMemory,
  deleteMemory,
  fetchLearningMemory,
  fetchMemoryCourses,
  MemoryApiError,
  type LearningMemoryView,
  type MemoryCourseCount,
  type MemoryEntry,
} from "../lib/memoryApi";
import { formatDateTime, memoryValidityLabel, memoryVerdictLabel } from "../lib/display";

type Props = {
  /** 从记忆条目跳回原会话：只带会话 id，不复制任何学习内容。 */
  onOpenConversation?: (conversationId: string) => void;
};

type Phase =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; view: LearningMemoryView }
  | { kind: "error"; message: string };

const RANGES = [
  { days: 30, label: "近 30 天" },
  { days: 90, label: "近 90 天" },
];

/** 课程归属只有这四种来源；未知时要显示“待确认”，不能默认算进某门课。 */
const COURSE_SOURCE_LABELS: Record<string, string> = {
  user: "你手动指定",
  paper: "按试卷归属",
  problem: "按题目归属",
  unknown: "课程待确认",
};

function entryCourseText(entry: MemoryEntry): string {
  if (entry.course) {
    return entry.course;
  }
  return "课程待确认";
}

function EntryForm({
  entry,
  onCancel,
  onSubmit,
  busy,
}: {
  entry: MemoryEntry;
  onCancel: () => void;
  onSubmit: (change: { course?: string; knowledge_point?: string; note?: string }) => void;
  busy: boolean;
}) {
  const [course, setCourse] = useState(entry.course ?? "");
  const [knowledgePoint, setKnowledgePoint] = useState(
    entry.knowledge_points[0] ?? "",
  );
  const [note, setNote] = useState(entry.note ?? "");

  return (
    <form
      className="mt-2 space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-2.5"
      onSubmit={(event) => {
        event.preventDefault();
        const change: { course?: string; knowledge_point?: string; note?: string } = {};
        // 留空也要提交：空串表示“改为待确认”，否则课程归错后改不回来。
        if (course.trim() !== (entry.course ?? "")) {
          change.course = course.trim();
        }
        if (
          knowledgePoint.trim() &&
          knowledgePoint.trim() !== (entry.knowledge_points[0] ?? "")
        ) {
          change.knowledge_point = knowledgePoint.trim();
        }
        if (note.trim() !== (entry.note ?? "")) {
          change.note = note.trim();
        }
        onSubmit(change);
      }}
    >
      <label className="block text-[11px] text-slate-600">
        课程（留空表示改为待确认）
        <input
          className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-1 text-xs"
          value={course}
          onChange={(event) => setCourse(event.target.value)}
          placeholder="例如：Python语言程序设计"
        />
      </label>
      <label className="block text-[11px] text-slate-600">
        知识点
        <input
          className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-1 text-xs"
          value={knowledgePoint}
          onChange={(event) => setKnowledgePoint(event.target.value)}
          placeholder="例如：循环的条件与终止条件"
        />
      </label>
      <label className="block text-[11px] text-slate-600">
        附注（只写你的说明，不改原记录）
        <textarea
          className="mt-1 h-16 w-full rounded-md border border-slate-300 bg-white px-2 py-1 text-xs"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="例如：这是我当时看错条件"
        />
      </label>
      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={busy}
          className="rounded-lg bg-river px-3 py-1 text-[11px] font-medium text-white disabled:opacity-50"
        >
          保存更正
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-lg border border-slate-300 bg-white px-3 py-1 text-[11px] text-slate-600"
        >
          取消
        </button>
      </div>
    </form>
  );
}

export default function LearningMemory({ onOpenConversation }: Props) {
  const [courses, setCourses] = useState<MemoryCourseCount[]>([]);
  const [pending, setPending] = useState(0);
  const [coursesLoaded, setCoursesLoaded] = useState(false);
  const [course, setCourse] = useState<string | null>(null);
  const [days, setDays] = useState(30);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [message, setMessage] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    fetchMemoryCourses(controller.signal)
      .then((payload) => {
        setCourses(payload.items);
        setPending(payload.pending);
        setCoursesLoaded(true);
        setCourse((current) => current ?? payload.items[0]?.course ?? null);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setCoursesLoaded(true);
        setPhase({
          kind: "error",
          message:
            error instanceof MemoryApiError
              ? error.message
              : "读取课程列表失败，请稍后再试。",
        });
      });
    return () => controller.abort();
  }, [reloadKey]);

  useEffect(() => {
    if (!coursesLoaded) {
      return;
    }
    const controller = new AbortController();
    setPhase({ kind: "loading" });
    fetchLearningMemory({ course, days, limit: 50 }, controller.signal)
      .then((view) => setPhase({ kind: "ready", view }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setPhase({
          kind: "error",
          message:
            error instanceof MemoryApiError
              ? error.message
              : "读取学习记忆失败，请稍后再试。",
        });
      });
    return () => controller.abort();
  }, [course, days, reloadKey, coursesLoaded]);

  const refresh = useCallback(() => {
    setReloadKey((value) => value + 1);
  }, []);

  const view = phase.kind === "ready" ? phase.view : null;
  const entries = useMemo(
    () => (view ? [...view.entries, ...view.pending_course_entries] : []),
    [view],
  );

  const submitCorrection = useCallback(
    async (
      entry: MemoryEntry,
      change: { course?: string; knowledge_point?: string; note?: string },
    ) => {
      if (
        change.course === undefined &&
        change.knowledge_point === undefined &&
        change.note === undefined
      ) {
        setMessage("没有改动需要保存。");
        return;
      }
      setBusyId(entry.entry_id);
      try {
        await correctMemory(entry.entry_id, change);
        setEditingId(null);
        setMessage("已更新这条学习记忆，原会话记录不变。");
        refresh();
      } catch (error) {
        setMessage(
          error instanceof MemoryApiError ? error.message : "更新失败，请稍后再试。",
        );
      } finally {
        setBusyId(null);
      }
    },
    [refresh],
  );

  const removeEntry = useCallback(
    async (entry: MemoryEntry) => {
      const confirmed = window.confirm(
        "从学习记忆和复习建议中移除这条？原会话记录会保留。",
      );
      if (!confirmed) {
        return;
      }
      setBusyId(entry.entry_id);
      try {
        const result = await deleteMemory(entry.entry_id, "学生主动移除");
        setMessage(result.message);
        refresh();
      } catch (error) {
        setMessage(
          error instanceof MemoryApiError ? error.message : "移除失败，请稍后再试。",
        );
      } finally {
        setBusyId(null);
      }
    },
    [refresh],
  );

  return (
    <div className="mx-auto max-w-4xl pb-10">
      <section className="mt-2">
        <p className="text-xs font-medium tracking-[0.2em] text-river">
          学习记忆 · 复习建议
        </p>
        <h1 className="mt-3 text-2xl font-semibold leading-tight tracking-tight text-ink sm:text-3xl">
          从你已经做过的题，找回还没稳的那一块。
        </h1>
        <p className="mt-3 text-sm leading-7 text-slate-600">
          这里只统计程序里真实存在的练习、作答和你的更正；不做百分制估算，
          一次做对也不会被当成永久掌握。需要改哪一条，直接在这里更正或移除。
        </p>
      </section>

      <div className="mt-5 flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1.5">
          {courses.map((item) => (
            <button
              key={item.label}
              type="button"
              onClick={() => setCourse(item.course)}
              aria-pressed={course === item.course}
              className={`rounded-lg border px-3 py-1 text-xs ${
                course === item.course
                  ? "border-river bg-river text-white"
                  : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
              }`}
            >
              {item.label}
              <span className="ml-1 text-[10px] opacity-75">{item.evidence}</span>
            </button>
          ))}
          {courses.length === 0 && pending > 0 && (
            <span className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-1 text-xs text-amber-800">
              {pending} 条记录的课程还没确认
            </span>
          )}
        </div>
        <div className="ml-auto flex gap-1.5">
          {RANGES.map((item) => (
            <button
              key={item.days}
              type="button"
              onClick={() => setDays(item.days)}
              aria-pressed={days === item.days}
              className={`rounded-lg border px-3 py-1 text-xs ${
                days === item.days
                  ? "border-ink bg-ink text-white"
                  : "border-slate-200 bg-white text-slate-600"
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {message && (
        <p
          role="status"
          className="mt-3 rounded-lg border border-river/30 bg-river/5 px-3 py-2 text-xs text-slate-700"
        >
          {message}
        </p>
      )}

      {phase.kind === "loading" && (
        <p className="mt-6 text-sm text-slate-500">正在读取学习记忆…</p>
      )}

      {phase.kind === "error" && (
        <div className="mt-6 rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">
          <p>{phase.message}</p>
          <button
            type="button"
            onClick={refresh}
            className="mt-2 rounded-lg border border-rose-300 bg-white px-3 py-1 text-xs"
          >
            重试
          </button>
        </div>
      )}

      {view && (
        <>
          <section className="mt-5 rounded-xl border border-slate-200 bg-white/80 p-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-sm font-semibold text-ink">
                {view.course ?? "课程待确认"} · {view.range_text}
              </h2>
              <p className="text-[11px] text-slate-500">
                证据 {view.counts.evidence} 条 · 练习 {view.counts.practices} 道 ·
                作答 {view.counts.attempts} 次
                {view.counts.pending_course > 0 &&
                  ` · 课程待确认 ${view.counts.pending_course} 条`}
                {view.counts.excluded > 0 && ` · 已移除 ${view.counts.excluded} 条`}
                {view.counts.excluded_evaluation > 0 &&
                  ` · 评价已排除 ${view.counts.excluded_evaluation} 条`}
              </p>
            </div>

            {view.status === "empty" && (
              <p className="mt-3 text-sm leading-7 text-slate-600">{view.message}</p>
            )}

            {view.suggestions.length > 0 && (
              <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2">
                <p className="text-xs font-medium text-amber-900">建议先补</p>
                <ul className="mt-1 list-inside list-disc space-y-0.5 text-xs leading-6 text-amber-900">
                  {view.suggestions.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            )}

            {view.groups.length > 0 && (
              <div className="mt-4 overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="text-[11px] text-slate-500">
                    <tr>
                      <th className="py-1 pr-3 font-medium">知识点</th>
                      <th className="py-1 pr-3 font-medium">独立做错</th>
                      <th className="py-1 pr-3 font-medium">独立做对</th>
                      <th className="py-1 pr-3 font-medium">看答案后作答</th>
                      <th className="py-1 pr-3 font-medium">独立性未确认</th>
                      <th className="py-1 pr-3 font-medium">更早判错</th>
                      <th className="py-1 pr-3 font-medium">评价已排除</th>
                      <th className="py-1 pr-3 font-medium">尚未作答</th>
                      <th className="py-1 font-medium">最近一次</th>
                    </tr>
                  </thead>
                  <tbody>
                    {view.groups.map((group) => (
                      <tr key={group.label} className="border-t border-slate-100">
                        <td className="py-1.5 pr-3 text-slate-800">
                          {group.label}
                          {!group.normalized && (
                            <span className="ml-1 text-[10px] text-amber-700">
                              未归一
                            </span>
                          )}
                        </td>
                        <td className="py-1.5 pr-3">{group.wrong_count}</td>
                        <td className="py-1.5 pr-3">{group.correct_count}</td>
                        <td className="py-1.5 pr-3">{group.after_reveal_count}</td>
                        <td className="py-1.5 pr-3">{group.unclear_count}</td>
                        <td className="py-1.5 pr-3">
                          {group.earlier_wrong_count > 0
                            ? `${group.earlier_wrong_count}（窗口前）`
                            : 0}
                        </td>
                        <td className="py-1.5 pr-3">
                          {group.excluded_evaluation_count}
                        </td>
                        <td className="py-1.5 pr-3">{group.no_attempt_count}</td>
                        <td className="py-1.5 text-slate-500">
                          {formatDateTime(group.latest_at, "无记录")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {entries.length > 0 && (
            <section className="mt-4 space-y-2">
              <h2 className="text-xs font-medium tracking-[0.16em] text-slate-500">
                逐条记录（{entries.length}）
              </h2>
              {entries.map((entry) => (
                <article
                  key={entry.entry_id}
                  className="rounded-xl border border-slate-200 bg-white/80 p-3.5"
                >
                  <div className="flex flex-wrap items-center gap-2 text-[11px]">
                    <span className="rounded-md bg-slate-100 px-1.5 py-0.5 text-slate-700">
                      {memoryVerdictLabel(entry.verdict)}
                    </span>
                    <span className="text-slate-500">{entryCourseText(entry)}</span>
                    <span className="text-slate-400">
                      {COURSE_SOURCE_LABELS[entry.course_source] ?? "课程待确认"}
                    </span>
                    <span className="text-slate-400">
                      {formatDateTime(entry.recorded_at)}
                    </span>
                    {entry.validity !== "ok" && (
                      <span className="rounded-md bg-amber-50 px-1.5 py-0.5 text-amber-800">
                        {memoryValidityLabel(entry.validity)}
                      </span>
                    )}
                  </div>

                  {entry.statement_excerpt && (
                    <p className="mt-2 text-sm leading-6 text-slate-700">
                      {entry.statement_excerpt}
                    </p>
                  )}

                  {entry.knowledge_points.length > 0 && (
                    <p className="mt-1 text-[11px] text-slate-500">
                      知识点：{entry.knowledge_points.join("、")}
                      {entry.knowledge_points_normalized.some((item) => !item) &&
                        "（有一条未归一）"}
                    </p>
                  )}

                  {entry.note && (
                    <p className="mt-1 text-[11px] text-slate-600">
                      附注：{entry.note}
                    </p>
                  )}

                  {entry.attempts.length > 0 && (
                    <ul className="mt-1.5 space-y-0.5 text-[11px] text-slate-500">
                      {entry.attempts.map((attempt) => (
                        <li key={attempt.attempt_id}>
                          第 {attempt.attempt_no} 次 · {attempt.label}
                          {attempt.excluded && "（已从统计移除）"}
                        </li>
                      ))}
                    </ul>
                  )}

                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={() =>
                        setEditingId((current) =>
                          current === entry.entry_id ? null : entry.entry_id,
                        )
                      }
                      className="rounded-lg border border-slate-300 bg-white px-2.5 py-1 text-[11px] text-slate-700 hover:bg-slate-50"
                    >
                      {editingId === entry.entry_id ? "收起更正" : "更正这条"}
                    </button>
                    <button
                      type="button"
                      onClick={() => void removeEntry(entry)}
                      disabled={busyId === entry.entry_id}
                      className="rounded-lg border border-rose-200 bg-white px-2.5 py-1 text-[11px] text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                    >
                      从学习记忆移除
                    </button>
                    {onOpenConversation && entry.source.conversation_id && (
                      <button
                        type="button"
                        onClick={() =>
                          onOpenConversation(entry.source.conversation_id)
                        }
                        className="text-[11px] text-river underline-offset-2 hover:underline"
                      >
                        回到原会话
                      </button>
                    )}
                  </div>

                  {editingId === entry.entry_id && (
                    <EntryForm
                      entry={entry}
                      busy={busyId === entry.entry_id}
                      onCancel={() => setEditingId(null)}
                      onSubmit={(change) => void submitCorrection(entry, change)}
                    />
                  )}
                </article>
              ))}
            </section>
          )}

          <p className="mt-6 text-[11px] leading-6 text-slate-500">
            记忆只描述已有记录：一次答对不代表已经掌握，也不会计算比例。
            移除只影响学习记忆与复习建议，原会话里的题目、讲解和作答都保留。
          </p>
        </>
      )}
    </div>
  );
}