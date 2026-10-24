import type { FormEvent } from "react";
import type { ExamTypeFilter } from "../lib/papersApi";

type PaperFiltersProps = {
  course: string;
  examType: ExamTypeFilter;
  loading: boolean;
  onCourseChange: (value: string) => void;
  onExamTypeChange: (value: ExamTypeFilter) => void;
  onSubmit: () => void;
};

const EXAM_TYPE_OPTIONS: { value: ExamTypeFilter; label: string }[] = [
  { value: "all", label: "全部类型" },
  { value: "midterm", label: "期中" },
  { value: "final", label: "期末" },
];

/**
 * 筛选区只维护输入值并把“查询”动作上报，不发请求。
 * 真正的请求由 App 统一发起，便于处理加载、空结果和过期响应。
 */
export default function PaperFilters({
  course,
  examType,
  loading,
  onCourseChange,
  onExamTypeChange,
  onSubmit,
}: PaperFiltersProps) {
  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit();
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-3 rounded-2xl border border-slate-200/90 bg-white/90 p-4 shadow-card backdrop-blur sm:flex-row sm:items-end"
    >
      <label className="flex-1">
        <span className="block text-[11px] font-medium tracking-[0.18em] text-slate-500">
          课程
        </span>
        <input
          type="search"
          value={course}
          onChange={(event) => onCourseChange(event.target.value)}
          placeholder="例如 高数、高等数学BⅠ"
          className="mt-2 w-full rounded-xl border border-slate-300 bg-white px-3.5 py-2.5 text-sm text-ink outline-none transition placeholder:text-slate-400 focus:border-river focus:ring-2 focus:ring-river/20"
        />
      </label>

      <label className="sm:w-40">
        <span className="block text-[11px] font-medium tracking-[0.18em] text-slate-500">
          考试类型
        </span>
        <select
          value={examType}
          onChange={(event) =>
            onExamTypeChange(event.target.value as ExamTypeFilter)
          }
          className="mt-2 w-full rounded-xl border border-slate-300 bg-white px-3.5 py-2.5 text-sm text-ink outline-none transition focus:border-river focus:ring-2 focus:ring-river/20"
        >
          {EXAM_TYPE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>

      <button
        type="submit"
        disabled={loading}
        className="inline-flex items-center justify-center gap-2 rounded-xl bg-ink px-5 py-2.5 text-sm font-medium text-white transition hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-river focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:bg-slate-400"
      >
        {loading ? "查询中…" : "查询试卷"}
      </button>
    </form>
  );
}
