import type { TutoringResult, ProblemView } from "../lib/conversationsApi";
import MathText from "./MathText";
import SourceBadge from "./SourceBadge";

type Explanation = Extract<TutoringResult, { kind: "explanation" }>;

type ProblemCardProps = {
  result: Explanation;
  showStatement?: boolean;
  canCorrect?: boolean;
  busy: boolean;
  onFeedback: (problem: ProblemView) => void;
  onRecord: (problem: ProblemView) => void;
  onCorrect: (problem: ProblemView) => void;
  onSupplement: (problem: ProblemView) => void;
};

export default function ProblemCard({
  result,
  showStatement = true,
  canCorrect = true,
  busy,
  onFeedback,
  onRecord,
  onCorrect,
  onSupplement,
}: ProblemCardProps) {
  const problem = result.problem;
  const recorded = Boolean(result.record_id);

  return (
    <article className="lesson-sheet">
      <header className="sheet-meta">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <SourceBadge source={problem.source} />
          {recorded && <span className="sheet-badge">已记录到错题本</span>}
        </div>
        <span>{problem.status === "explained" ? "已讲解" : "待补充"}</span>
      </header>

      {showStatement && <p className="lesson-statement">
        <MathText text={problem.statement_text} />
      </p>}

      {problem.attachments.some((image) => showStatement || image.role === "solution") && (
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
          {problem.attachments.filter((image) => showStatement || image.role === "solution").map((image) => (
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
        <div className="student-work">
          <p className="text-xs font-medium text-amber-900">你提交的解法</p>
          {problem.student_answer_text && (
            <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-amber-950">
              答案：<MathText text={problem.student_answer_text} />
            </p>
          )}
          {problem.solution_text && (
            <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-amber-950">
              过程：<MathText text={problem.solution_text} />
            </p>
          )}
        </div>
      )}

      {result.uncertainties.length > 0 && (
        <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50/70 p-3">
          <p className="text-xs font-medium text-amber-800">需要你确认</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-xs leading-6 text-amber-950">
            {result.uncertainties.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="lesson-actions">
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
          {recorded ? "已加入错题本" : "加入错题本"}
        </button>
        {canCorrect && (
          <button
            type="button"
            onClick={() => onCorrect(problem)}
            disabled={busy}
            className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
          >
            纠正题干
          </button>
        )}
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
      </div>
    </article>
  );
}
