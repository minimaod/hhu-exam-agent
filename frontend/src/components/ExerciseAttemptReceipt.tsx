import type {
  ExerciseAttemptView,
  ExerciseView,
  RecordRef,
} from "../lib/conversationsApi";
import {
  AttachmentThumbnails,
  EvaluationBlock,
  KIND_LABELS,
} from "./ExerciseCard";
import MathText from "./MathText";

type ExerciseAttemptReceiptProps = {
  exercise: ExerciseView;
  attempt: ExerciseAttemptView;
  onJump: () => void;
  onCorrect?: () => void;
  mistakeRecord?: RecordRef | null;
  savingMistake?: boolean;
  onSaveMistake?: (exercise: ExerciseView) => void;
  onOpenRecord?: (recordId: string) => void;
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
  mistakeRecord = null,
  savingMistake = false,
  onSaveMistake,
  onOpenRecord,
  busy = false,
}: ExerciseAttemptReceiptProps) {
  const kindLabel = KIND_LABELS[attempt.kind] ?? "本次作答";

  return (
    <div className="rounded-2xl attempt-receipt">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="sheet-badge">
            <span>本题</span>
            <span aria-hidden="true"> · </span>
            <span>{`第 ${attempt.attempt_no} 次作答 · ${kindLabel}`}</span>
          </span>
          {attempt.answered_after_revealed && !attempt.evaluation && (
            <span className="rounded-full border border-amber-300 bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-800">
              已看解答后作答
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={onJump}
          className="text-xs font-medium text-river underline decoration-river/30 hover:text-river/80"
        >
          回到题目
        </button>
      </div>

      {(attempt.student_answer_text || attempt.student_solution_text) && (
        <div className="mt-2">
          {attempt.student_answer_text && (
            <p className="whitespace-pre-wrap text-xs leading-6 text-slate-900">
              <span className="font-semibold text-slate-600">最终答案：</span>
              <MathText text={attempt.student_answer_text} />
            </p>
          )}
          {attempt.student_solution_text &&
            (attempt.student_solution_text.trim().length > 120 ? (
              <details className="lesson-disclosure mt-1">
                <summary className="cursor-pointer text-xs font-medium text-slate-600">
                  提交的解题步骤（点击展开）
                </summary>
                <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-900">
                  <span className="font-semibold text-slate-600">解题步骤：</span>
                  <MathText text={attempt.student_solution_text} />
                </p>
              </details>
            ) : (
              <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-900">
                <span className="font-semibold text-slate-600">解题步骤：</span>
                <MathText text={attempt.student_solution_text} />
              </p>
            ))}
        </div>
      )}

      <AttachmentThumbnails
        attachments={attempt.attachments}
        conversationId={exercise.conversation_id}
      />

      {attempt.evaluation ? (
        <div className="lesson-disclosure">
          <EvaluationBlock
            evaluation={attempt.evaluation}
            answeredAfterRevealed={attempt.answered_after_revealed}
            referenceSolutionText={exercise.reference_solution_text}
            hasProcess={Boolean(
              attempt.student_solution_text?.trim() ||
                attempt.attachments.length > 0,
            )}
          />
        </div>
      ) : (
        <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs leading-6 text-amber-900">
          这次作答已经保存，但评价没有完成。可在本轮重试评价；原作答不会丢失。
        </p>
      )}

      {(onCorrect || onSaveMistake || (mistakeRecord && onOpenRecord)) && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          {onCorrect && attempt.evaluation && (
            <button
              type="button"
              onClick={onCorrect}
              disabled={busy}
              className="quiet-button"
            >
              回到题目并订正
            </button>
          )}
          {attempt.evaluation && (onSaveMistake || mistakeRecord) && (
            mistakeRecord ? (
              <button
                type="button"
                onClick={() => onOpenRecord?.(mistakeRecord.id)}
                className="text-xs font-medium text-emerald-700 underline decoration-emerald-300 hover:text-emerald-900"
              >
                已加入 · 查看
              </button>
            ) : (
              <button
                type="button"
                disabled={busy || savingMistake}
                onClick={() => onSaveMistake?.(exercise)}
                className="quiet-button"
              >
                {savingMistake ? "保存中..." : "加入错题本"}
              </button>
            )
          )}
        </div>
      )}
    </div>
  );
}