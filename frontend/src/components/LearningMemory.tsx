import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
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
import MathText from "./MathText";

type Props = {
  /** 从记忆条目跳回原会话：只带会话 id，不复制任何学习内容。 */
  onOpenConversation?: (conversationId: string) => void;
  onCountChange?: (count: number) => void;
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

/** 课程归属来源；未知时显示“课程待确认”，不能默认算进某门课。 */
const COURSE_SOURCE_LABELS: Record<string, string> = {
  user: "你手动指定",
  paper: "按试卷归属",
  problem: "按题目归属",
  conversation: "按会话科目",
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
      <label className="block text-xs text-slate-600">
        课程（留空表示改为待确认）
        <input
          className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-1 text-xs"
          value={course}
          onChange={(event) => setCourse(event.target.value)}
          placeholder="例如：Python语言程序设计"
        />
      </label>
      <label className="block text-xs text-slate-600">
        知识点
        <input
          className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-1 text-xs"
          value={knowledgePoint}
          onChange={(event) => setKnowledgePoint(event.target.value)}
          placeholder="例如：循环的条件与终止条件"
        />
      </label>
      <label className="block text-xs text-slate-600">
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
          className="rounded-lg bg-river px-3 py-1 text-xs font-medium text-white disabled:opacity-50"
        >
          保存更正
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-lg border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600"
        >
          取消
        </button>
      </div>
    </form>
  );
}

export default function LearningMemory({ onOpenConversation, onCountChange }: Props) {
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
        if (controller.signal.aborted) return;
        setCourses(payload.items);
        setPending(payload.pending);
        onCountChange?.(payload.items.reduce((total, item) => total + item.evidence, 0) + payload.pending);
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
  }, [onCountChange, reloadKey]);

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
    <div className="page-frame memory-page">
      <header className="page-heading">
        <p className="eyebrow">个人学习记录</p>
        <h1>知识点掌握账本与复习建议</h1>
        <p>按作答和更正记录查看知识点。一次做对也不会被当成永久掌握。</p>
      </header>

      <div className="memory-controls">
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
              <span className="ml-1 text-xs opacity-75">{item.evidence}</span>
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
                  ? "border-river bg-river text-white"
                  : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
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
          <section className="memory-ledger">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="ledger-heading">
                {view.course ?? "课程待确认"} · 近 {days} 天
              </h2>
              <p className="text-xs text-slate-500">
                记录 {view.counts.evidence} 条 · 练习 {view.counts.practices} 道 ·
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
                    <li key={item} className="break-words">
                      <MathText text={item} />
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {view.groups.length > 0 && (
              <div className="ledger-grid">
                {view.groups.map((group) => (
                  <article key={group.label} className="ledger-entry">
                    <header>
                      <h3>
                        <MathText text={group.label} />
                      </h3>
                    </header>
                    <p className="ledger-date">最近一次 · {formatDateTime(group.latest_at, "无记录")}</p>
                    <dl className="ledger-metrics">
                      <div><dt>独立做错</dt><dd className="ledger-wrong">{group.wrong_count}</dd></div>
                      <div><dt>独立做对</dt><dd>{group.correct_count}</dd></div>
                      <div><dt>看答案后作答</dt><dd>{group.after_reveal_count}</dd></div>
                      {group.unclear_count > 0 && <div><dt>是否独立待确认</dt><dd>{group.unclear_count}</dd></div>}
                      {group.earlier_wrong_count > 0 && <div><dt>更早做错</dt><dd>{group.earlier_wrong_count}</dd></div>}
                      {group.excluded_evaluation_count > 0 && <div><dt>评价已排除</dt><dd>{group.excluded_evaluation_count}</dd></div>}
                      {group.no_attempt_count > 0 && <div><dt>尚未作答</dt><dd>{group.no_attempt_count}</dd></div>}
                    </dl>
                  </article>
                ))}
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
                  className="evidence-entry"
                >
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="rounded-md bg-slate-100 px-1.5 py-0.5 text-slate-700">
                      {memoryVerdictLabel(entry.verdict)}
                    </span>
                    <span className="text-slate-500">{entryCourseText(entry)}</span>
                    {entry.course && (
                      <span className="text-slate-400">
                        {COURSE_SOURCE_LABELS[entry.course_source] ?? "课程待确认"}
                      </span>
                    )}
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
                      <MathText text={entry.statement_excerpt} />
                    </p>
                  )}

                  {entry.knowledge_points.length > 0 && (
                    <p className="mt-1 break-words text-xs text-slate-500">
                      知识点：
                      {entry.knowledge_points.map((kp, index) => (
                        <Fragment key={`${kp}-${index}`}>
                          {index > 0 ? "、" : ""}
                          <MathText text={kp} />
                        </Fragment>
                      ))}
                    </p>
                  )}

                  {entry.note && (
                    <p className="mt-1 text-xs text-slate-600">
                      附注：{entry.note}
                    </p>
                  )}

                  {entry.attempts.length > 0 && (
                    <ul className="mt-1.5 space-y-0.5 text-xs text-slate-500">
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
                      className="rounded-lg border border-slate-300 bg-white px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-50"
                    >
                      {editingId === entry.entry_id ? "收起更正" : "更正这条"}
                    </button>
                    <button
                      type="button"
                      onClick={() => void removeEntry(entry)}
                      disabled={busyId === entry.entry_id}
                      className="rounded-lg border border-rose-200 bg-white px-2.5 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                    >
                      从学习记忆移除
                    </button>
                    {onOpenConversation && entry.source.conversation_id && (
                      <button
                        type="button"
                        onClick={() =>
                          onOpenConversation(entry.source.conversation_id)
                        }
                        className="rounded-lg border border-river/40 bg-white px-2.5 py-1 text-xs text-river hover:bg-slate-50"
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

        </>
      )}
    </div>
  );
}