import type { TutoringResult, ProblemView } from "../lib/conversationsApi";
import SourceBadge from "./SourceBadge";

type Explanation = Extract<TutoringResult, { kind: "explanation" }>;

type ProblemCardProps = {
  result: Explanation;
  busy: boolean;
  onFeedback: (problem: ProblemView) => void;
  onRecord: (problem: ProblemView) => void;
  onCorrect: (problem: ProblemView) => void;
  onSupplement: (problem: ProblemView) => void;
  onNew: () => void;
};

export default function ProblemCard({
  result,
  busy,
  onFeedback,
  onRecord,
  onCorrect,
  onSupplement,
  onNew,
}: ProblemCardProps) {
  const problem = result.problem;
  const recorded = Boolean(result.record_id);

  return (
    <div className="rounded-2xl border border-slate-200/90 bg-white/95 p-5 shadow-card">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full border border-river/30 bg-river/10 px-2.5 py-0.5 text-[11px] font-medium text-river">
            第 {problem.version} 版题目
          </span>
          <SourceBadge source={problem.source} />
          {recorded && (
            <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-[11px] font-medium text-emerald-700">
              已记录到错题本
            </span>
          )}
        </div>
        <span className="text-[11px] text-slate-400">
          {problem.status === "explained" ? "已讲解" : "待补充"}
        </span>
      </div>

      <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7 text-ink">
        {problem.statement_text}
      </p>

      {problem.attachments.length > 0 && (
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
          {problem.attachments.map((image) => (
            <a
              key={image.id}
              href={image.url}
              target="_blank"
              rel="noreferrer"
              className="block overflow-hidden rounded-xl border border-slate-200 bg-slate-50"
            >
              <img
                src={image.url}
                alt={image.role === "solution" ? "解法图片" : "题目图片"}
                className="h-28 w-full object-cover"
              />
            </a>
          ))}
        </div>
      )}

      {(problem.student_answer_text || problem.solution_text) && (
        <div className="mt-3 rounded-xl border border-amber-200/80 bg-amber-50/50 p-3">
          <p className="text-[11px] font-medium text-amber-900">你提交的解法</p>
          {problem.student_answer_text && (
            <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-amber-950">
              答案：{problem.student_answer_text}
            </p>
          )}
          {problem.solution_text && (
            <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-amber-950">
              过程：{problem.solution_text}
            </p>
          )}
        </div>
      )}

      <div className="mt-4">
        <h4 className="text-xs font-semibold tracking-wide text-slate-500">
          知识点
        </h4>
        <ul className="mt-2 space-y-1.5">
          {result.knowledge_points.map((point) => (
            <li key={point.name} className="text-sm leading-6 text-slate-700">
              <span className="font-medium text-ink">{point.name}：</span>
              {point.explanation}
            </li>
          ))}
        </ul>
      </div>

      <div className="mt-4">
        <h4 className="text-xs font-semibold tracking-wide text-slate-500">
          解题步骤
        </h4>
        <ol className="mt-2 space-y-2">
          {result.steps.map((step) => (
            <li key={step.index} className="rounded-xl bg-slate-50/80 p-3">
              <p className="text-sm font-medium text-ink">
                {step.index}. {step.title}
              </p>
              <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-6 text-slate-700">
                {step.content}
              </p>
            </li>
          ))}
        </ol>
      </div>

      <div className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50/70 p-3">
        <p className="text-[11px] font-medium text-emerald-800">最终答案</p>
        <p className="mt-1 whitespace-pre-wrap break-words text-sm font-medium text-emerald-950">
          {result.final_answer}
        </p>
      </div>

      {result.error_analysis && (
        <div className="mt-3 rounded-xl border border-rose-200 bg-rose-50/80 p-3">
          <p className="text-[11px] font-medium text-rose-800">你解法中的具体问题</p>
          <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-6 text-rose-950">
            {result.error_analysis}
          </p>
        </div>
      )}

      {result.uncertainties.length > 0 && (
        <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50/70 p-3">
          <p className="text-[11px] font-medium text-amber-800">需要你确认</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-xs leading-6 text-amber-950">
            {result.uncertainties.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-4 flex flex-wrap gap-2 border-t border-slate-100 pt-3">
        <button
          type="button"
          onClick={() => onFeedback(problem)}
          disabled={busy || problem.self_reported_understood}
          className="rounded-lg border border-emerald-300 bg-white px-3 py-1.5 text-xs font-medium text-emerald-800 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {problem.self_reported_understood ? "已记录“我懂了”" : "我懂了"}
        </button>
        <button
          type="button"
          onClick={() => onRecord(problem)}
          disabled={busy || recorded}
          className="rounded-lg border border-river/40 bg-white px-3 py-1.5 text-xs font-medium text-river hover:bg-river/5 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {recorded ? "已记录" : "记录这题"}
        </button>
        <button
          type="button"
          onClick={() => onCorrect(problem)}
          disabled={busy}
          className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
        >
          纠正题干
        </button>
        {problem.status === "awaiting_supplement" && (
          <button
            type="button"
            onClick={() => onSupplement(problem)}
            disabled={busy}
            className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
          >
            补充题目
          </button>
        )}
        <button
          type="button"
          onClick={onNew}
          disabled={busy}
          className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
        >
          开始新题
        </button>
      </div>
    </div>
  );
}
