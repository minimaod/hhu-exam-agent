import { useState, type FormEvent } from "react";
import { submitContribution } from "../lib/contributionsApi.ts";

const COURSE_SUGGESTIONS = [
  "高等数学",
  "概率论与数理统计",
  "几何与线性代数",
  "复变函数与积分变换",
  "离散数学",
  "大学物理",
  "C语言程序设计",
  "Python语言程序设计",
  "中国近现代史纲要",
  "大学英语",
  "思想道德与法治",
  "马克思主义基本原理",
  "转专业",
];

type ContributeModalProps = {
  onClose: () => void;
};

export default function ContributeModal({ onClose }: ContributeModalProps) {
  const [file, setFile] = useState<File | null>(null);
  const [course, setCourse] = useState("");
  const [year, setYear] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;

    const trimmedCourse = course.trim();
    const trimmedYear = year.trim();

    if (!file) {
      setError("请选择一份 PDF 试卷文件。");
      return;
    }
    if (!trimmedCourse) {
      setError("请填写学科名称。");
      return;
    }
    if (!trimmedYear) {
      setError("请填写试卷年份。");
      return;
    }

    setSubmitting(true);
    setError(null);
    setSuccessMessage(null);
    try {
      const result = await submitContribution({
        file,
        course: trimmedCourse,
        year: trimmedYear,
      });
      setSuccessMessage(result.message || "已提交，等待审核");
      setFile(null);
      setCourse("");
      setYear("");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "提交失败，请稍后再试。");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="material-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="contribute-modal-heading"
    >
      <div className="material-modal-card w-[min(520px,95vw)]">
        <header className="material-modal-header">
          <div>
            <p className="eyebrow">公共题库共建</p>
            <h2 id="contribute-modal-heading">自愿贡献资料</h2>
          </div>
          <div className="material-modal-actions">
            <button type="button" className="quiet-button" onClick={onClose}>
              关闭
            </button>
          </div>
        </header>

        <p className="mb-4 text-sm text-slate-600">
          提交后，项目维护者会审核这份试卷。审核通过的题目可在站内公共题库检索。
        </p>

        {successMessage && (
          <div
            role="status"
            className="mb-4 rounded-xl border border-emerald-200 bg-emerald-50 px-3.5 py-2.5 text-sm text-emerald-900"
          >
            {successMessage}
          </div>
        )}

        {error && (
          <div role="alert" className="inline-alert mb-4">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4" noValidate>
          <div>
            <label htmlFor="contribute-pdf" className="mb-1 block text-sm font-medium">
              试卷 PDF（必填）
            </label>
            <input
              id="contribute-pdf"
              type="file"
              accept=".pdf,application/pdf"
              disabled={submitting}
              onChange={(event) => {
                const picked = event.target.files?.[0] ?? null;
                setFile(picked);
                setError(null);
                setSuccessMessage(null);
              }}
              className="block w-full text-sm file:mr-3 file:rounded-lg file:border file:border-slate-300 file:bg-white file:px-3 file:py-1.5 file:text-xs file:font-medium hover:file:bg-slate-50"
            />
            {file && (
              <p className="mt-1 text-xs text-slate-500">已选择：{file.name}</p>
            )}
          </div>

          <div>
            <label htmlFor="contribute-course" className="mb-1 block text-sm font-medium">
              学科（必填）
            </label>
            <input
              id="contribute-course"
              type="text"
              list="contribute-course-list"
              placeholder="例如：高等数学、Python语言程序设计"
              value={course}
              disabled={submitting}
              onChange={(event) => {
                setCourse(event.target.value);
                setError(null);
              }}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
            />
            <datalist id="contribute-course-list">
              {COURSE_SUGGESTIONS.map((item) => (
                <option key={item} value={item} />
              ))}
            </datalist>
          </div>

          <div>
            <label htmlFor="contribute-year" className="mb-1 block text-sm font-medium">
              年份（必填）
            </label>
            <input
              id="contribute-year"
              type="text"
              placeholder="例如：2024 或 2023-2024"
              value={year}
              disabled={submitting}
              onChange={(event) => {
                setYear(event.target.value);
                setError(null);
              }}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
            />
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="quiet-button" onClick={onClose}>
              取消
            </button>
            <button type="submit" className="primary-button" disabled={submitting}>
              {submitting ? "提交中…" : "确认提交"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
