import type {
  ExerciseAttemptView,
  ExerciseView,
} from "../lib/conversationsApi";
import { KIND_LABELS, EvaluationBlock } from "./ExerciseCard";

type ExerciseAttemptReceiptProps = {
  exercise: ExerciseView;
  attempt: ExerciseAttemptView;
  onJump: () => void;
  onCorrect?: () => void;
  busy?: boolean;
};

/**
 * 一次作答的紧凑回执：只展示这一次提交、这一次评价，题目本体留在一张主卡上。
 * 这样同一道练习不会在生成轮和作答轮各展开一张相同的大卡片，历史也不被删除。
 */
export default function ExerciseAttemptReceipt({
  exercise,
  attempt,
  onJump,
  onCorrect,
  busy = false,
}: ExerciseAttemptReceiptProps) {
  const kindLabel = KIND_LABELS[attempt.kind] ?? "本次作答";
  const statement = exercise.statement_text.trim();
  const shortStatement =
    statement.length > 36 ? `${statement.slice(0, 36)}…` : statement;

  return (
    <div className="rounded-2xl border border-indigo-200/80 bg-white/90 p-4 shadow-card">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full border border-indigo-300 bg-indigo-50 px-2.5 py-0.5 text-[11px] font-medium text-indigo-700">
            第 {attempt.attempt_no} 次作答 · {kindLabel}
          </span>
          {attempt.answered_after_revealed && (
            <span className="rounded-full border border-amber-300 bg-amber-50 px-2.5 py-0.5 text-[11px] font-medium text-amber-800">
              已看解答后作答
            </span>
          )}
        </div>
        <span className="text-[10px] text-slate-400">本次提交</span>
      </div>

      <p className="mt-2 text-xs leading-6 text-slate-500" title={statement}>
        题目：{shortStatement}
      </p>

      {(attempt.student_answer_text || attempt.student_solution_text) && (
        <div className="mt-2 rounded-xl border border-slate-200 bg-slate-50/80 p-3">
          {attempt.student_answer_text && (
            <p className="whitespace-pre-wrap text-xs leading-6 text-slate-900">
              <span className="font-semibold text-slate-600">最终答案：</span>
              {attempt.student_answer_text}
            </p>
          )}
          {attempt.student_solution_text && (
            <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-900">
              <span className="font-semibold text-slate-600">解题步骤：</span>
              {attempt.student_solution_text}
            </p>
          )}
        </div>
      )}

      {attempt.attachments.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {attempt.attachments.map((item) => (
            <a
              key={item.id}
              href={item.url}
              target="_blank"
              rel="noreferrer"
              className="text-[11px] text-river underline decoration-river/30"
            >
              {item.role === "student_answer" ? "作答图片" : "过程图片"}
            </a>
          ))}
        </div>
      )}

      {attempt.evaluation ? (
        <div className="mt-3 rounded-xl border border-slate-200 bg-white p-3">
          <EvaluationBlock
            evaluation={attempt.evaluation}
            answeredAfterRevealed={attempt.answered_after_revealed}
          />
        </div>
      ) : (
        <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs leading-6 text-amber-900">
          这次作答已经保存，但评价没有完成。可在本轮重试评价；原作答不会丢失。
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={onJump}
          className="text-xs font-medium text-river underline decoration-river/30 hover:text-river/80"
        >
          查看题目主卡
        </button>
        {onCorrect && attempt.evaluation && (
          <button
            type="button"
            onClick={onCorrect}
            disabled={busy}
            className="rounded-lg border border-indigo-300 bg-indigo-50 px-2.5 py-1 text-[11px] font-medium text-indigo-700 hover:bg-indigo-100 disabled:opacity-40"
          >
            回到题目并订正
          </button>
        )}
      </div>
    </div>
  );
}