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
  { value: "other", label: "其他" },
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
      className="archive-filters"
    >
      <label className="archive-search">
        <span className="field-label">
          课程
        </span>
        <input
          type="search"
          value={course}
          onChange={(event) => onCourseChange(event.target.value)}
          placeholder="例如 高数、高等数学BⅠ"
          className="field-control"
        />
      </label>

      <label className="archive-type">
        <span className="field-label">
          考试类型
        </span>
        <select
          value={examType}
          onChange={(event) =>
            onExamTypeChange(event.target.value as ExamTypeFilter)
          }
          className="field-control"
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
        className="primary-button"
      >
        {loading ? "查询中…" : "查询试卷"}
      </button>
    </form>
  );
}
