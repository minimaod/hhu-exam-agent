import { useEffect, useRef, useState } from "react";
import type {
  ExerciseAttemptView,
  ExerciseEvaluation,
  ExerciseView,
} from "../lib/conversationsApi";

type AttemptKind = "supplement" | "correction";

type ExerciseCardProps = {
  exercise: ExerciseView;
  busy?: boolean;
  draft?: { answer: string; solution: string };
  /** 已在别处（对应作答轮次）展示的尝试，不在本题主卡里重复。 */
  hiddenAttemptIds?: string[];
  /** 这些作答回执所在的轮次，用于告诉学生评价去哪里看。 */
  receiptSeqs?: number[];
  /** 主卡锚点，供“查看完整题目”定位。 */
  anchorId?: string;
  /** 递增令牌：请求聚焦作答框（来自建议按钮）。 */
  focusToken?: number;
  /** 递增令牌：请求展开订正表单（来自作答回执）。 */
  correctionToken?: number;
  onDraftChange?: (draft: { answer: string; solution: string }) => void;
  onSubmitAnswer?: (
    exerciseId: string,
    answerText: string,
    solutionText?: string,
    attemptKind?: AttemptKind,
  ) => void;
  onRevealSolution?: (exerciseId: string) => void;
};

const KIND_LABELS: Record<string, string> = {
  initial: "首次作答",
  supplement: "补充过程",
  correction: "订正",
};

export { KIND_LABELS };

function IndependenceNote({
  evaluation,
  answeredAfterRevealed,
}: {
  evaluation: ExerciseEvaluation;
  answeredAfterRevealed: boolean;
}) {
  const afterReveal =
    answeredAfterRevealed ||
    evaluation.independence_assessment === "after_reveal";
  if (!afterReveal && !evaluation.independence_note) {
    return null;
  }
  return (
    <div
      className={`rounded-lg border p-2 text-xs leading-6 ${
        afterReveal
          ? "border-amber-300 bg-amber-50 text-amber-900"
          : "border-slate-200 bg-slate-50 text-slate-700"
      }`}
    >
      <span className="font-semibold">
        {afterReveal ? "对照作答：" : "独立性说明："}
      </span>
      {evaluation.independence_note ||
        "这次作答是在参考解答展示之后完成的；答案正确不代表独立掌握，建议遮住解答后完整重做一遍。"}
    </div>
  );
}

export function EvaluationBlock({
  evaluation,
  answeredAfterRevealed,
}: {
  evaluation: ExerciseEvaluation;
  answeredAfterRevealed: boolean;
}) {
  return (
    <div className="mt-2 space-y-2 text-xs leading-6 text-slate-800">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
            evaluation.is_correct === true
              ? "border border-emerald-300 bg-emerald-100 text-emerald-800"
              : evaluation.is_correct === false
                ? "border border-rose-300 bg-rose-100 text-rose-800"
                : "border border-amber-300 bg-amber-100 text-amber-800"
          }`}
        >
          {evaluation.is_correct === true
            ? "答案正确"
            : evaluation.is_correct === false
              ? "答案有误"
              : "待补充依据"}
        </span>
        {evaluation.needs_supplement && (
          <span className="text-[11px] font-medium text-amber-800">
            过程依据不足，未直接判错
          </span>
        )}
      </div>
      <p>
        <span className="font-semibold text-slate-600">答案判定：</span>
        {evaluation.final_answer_assessment}
      </p>
      <p>
        <span className="font-semibold text-slate-600">步骤分析：</span>
        {evaluation.step_by_step_analysis}
      </p>
      {evaluation.guidance && (
        <p className="rounded-lg bg-indigo-50/60 p-2 text-indigo-900">
          <span className="font-semibold">点拨指导：</span>
          {evaluation.guidance}
        </p>
      )}
      <IndependenceNote
        evaluation={evaluation}
        answeredAfterRevealed={answeredAfterRevealed}
      />
    </div>
  );
}

export default function ExerciseCard({
  exercise,
  busy = false,
  draft,
  hiddenAttemptIds,
  receiptSeqs,
  anchorId,
  focusToken = 0,
  correctionToken = 0,
  onDraftChange,
  onSubmitAnswer,
  onRevealSolution,
}: ExerciseCardProps) {
  const attempts = exercise.attempts ?? [];
  const latest = attempts.length ? attempts[attempts.length - 1] : null;
  const hiddenIds = new Set(hiddenAttemptIds ?? []);
  const visibleAttempts = attempts.filter((attempt) => !hiddenIds.has(attempt.id));
  const displayAttempt: ExerciseAttemptView | null = visibleAttempts.length
    ? visibleAttempts[visibleAttempts.length - 1]
    : null;
  const displayAnswer =
    displayAttempt?.student_answer_text ??
    (attempts.length === 0 ? exercise.student_answer_text : null);
  const displaySolution =
    displayAttempt?.student_solution_text ??
    (attempts.length === 0 ? exercise.student_solution_text : null);
  const displayEvaluation =
    displayAttempt?.evaluation ??
    (attempts.length === 0 ? exercise.evaluation : null);
  const displayAfterReveal =
    displayAttempt?.answered_after_revealed ??
    (attempts.length === 0 ? exercise.answered_after_revealed : false);
  const historyAttempts = displayAttempt
    ? visibleAttempts.filter((attempt) => attempt.id !== displayAttempt.id)
    : visibleAttempts;
  const latestAnswer = latest?.student_answer_text ?? exercise.student_answer_text;
  const latestSolution =
    latest?.student_solution_text ?? exercise.student_solution_text;
  const latestEvaluation = latest?.evaluation ?? exercise.evaluation;
  const latestAfterReveal =
    latest?.answered_after_revealed ?? exercise.answered_after_revealed;
  // 被回执接管的尝试不在主卡回填，避免把隐藏的作答又显示一次。
  const baseAnswer =
    displayAttempt?.student_answer_text ??
    (attempts.length === 0 ? exercise.student_answer_text : "");
  const baseSolution =
    displayAttempt?.student_solution_text ??
    (attempts.length === 0 ? exercise.student_solution_text : "");

  const [localAnswer, setAnswerInput] = useState(baseAnswer || "");
  const [localSolution, setSolutionInput] = useState(baseSolution || "");
  const [revealing, setRevealing] = useState(false);
  const [correcting, setCorrecting] = useState(false);
  const answerRef = useRef<HTMLInputElement>(null);
  // 记录已处理的令牌，避免切换会话后重新挂载卡片时自动聚焦/展开订正。
  const handledFocusTokenRef = useRef(focusToken);
  const handledCorrectionTokenRef = useRef(correctionToken);

  useEffect(() => {
    setAnswerInput(baseAnswer || "");
    setSolutionInput(baseSolution || "");
    setCorrecting(false);
    // 只在新尝试落库后同步；用户正在输入时 attempts.length 不会变化。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempts.length, hiddenAttemptIds?.join(",")]);

  const hasRevealed =
    exercise.status === "revealed" || Boolean(exercise.revealed_solution_at);
  const needsSupplement = latestEvaluation?.needs_supplement === true;
  const isSubmitted = Boolean(latestEvaluation) && !needsSupplement;
  const isEvaluationPending =
    !latestEvaluation && Boolean(latestAnswer || latestSolution);

  const answerInput = correcting ? localAnswer : draft?.answer ?? localAnswer;
  const solutionInput = correcting
    ? localSolution
    : draft?.solution ?? localSolution;
  const showForm = !isSubmitted || correcting;
  const attemptKind: AttemptKind | undefined = needsSupplement
    ? "supplement"
    : correcting
      ? "correction"
      : undefined;

  const handleReveal = async () => {
    if (!onRevealSolution || revealing || busy) return;
    setRevealing(true);
    try {
      await onRevealSolution(exercise.id);
    } finally {
      setRevealing(false);
    }
  };

  const startCorrection = () => {
    setCorrecting(true);
    setAnswerInput("");
    setSolutionInput("");
    onDraftChange?.({ answer: "", solution: "" });
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!onSubmitAnswer || busy) return;
    const trimmedAns = answerInput.trim();
    const trimmedSol = solutionInput.trim();
    if (!trimmedAns && !trimmedSol) return;
    onSubmitAnswer(exercise.id, trimmedAns, trimmedSol, attemptKind);
  };

  useEffect(() => {
    if (focusToken > handledFocusTokenRef.current && showForm) {
      answerRef.current?.focus();
    }
    handledFocusTokenRef.current = focusToken;
    // 只在点击“提交我的答案”时聚焦一次，不随普通输入变化。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusToken]);

  useEffect(() => {
    if (correctionToken > handledCorrectionTokenRef.current && isSubmitted) {
      startCorrection();
      window.setTimeout(() => answerRef.current?.focus(), 0);
    }
    handledCorrectionTokenRef.current = correctionToken;
    // 回执里的“回到题目并订正”每次点击都会递增令牌。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [correctionToken]);

  const statusText = needsSupplement
    ? "待补充过程"
    : isSubmitted
      ? attempts.length > 1
        ? `已评价（${attempts.length} 次作答）`
        : "已提交评价"
      : isEvaluationPending
        ? "评价未完成"
        : hasRevealed
          ? "已看解答"
          : "等待作答";

  const submitLabel = busy
    ? "评价中..."
    : needsSupplement
      ? "补充并重新评价"
      : correcting
        ? "提交订正并重新评价"
        : "提交作答并评价";

  return (
    <div
      id={anchorId}
      className="rounded-2xl border border-indigo-200/90 bg-white/95 p-5 shadow-card transition-all"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full border border-indigo-300 bg-indigo-50 px-2.5 py-0.5 text-[11px] font-medium text-indigo-700">
            AI 生成类似题
          </span>
          {exercise.source_question && (
            <span
              title={exercise.source_question.paper_title ?? exercise.source_question.paper_id}
              className="max-w-full truncate rounded-full border border-river/30 bg-river/10 px-2.5 py-0.5 text-[11px] font-medium text-river"
            >
              根据真题生成：第 {exercise.source_question.number_label ?? "?"} 题
            </span>
          )}
          {exercise.knowledge_point && (
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-[11px] font-medium text-slate-600">
              考点：{exercise.knowledge_point}
            </span>
          )}
          {latestAfterReveal && (
            <span className="rounded-full border border-amber-300 bg-amber-50 px-2.5 py-0.5 text-[11px] font-medium text-amber-800">
              已看解答后作答
            </span>
          )}
        </div>
        <span className="text-[11px] font-medium text-slate-400">
          {statusText}
        </span>
      </div>

      <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7 text-ink">
        {exercise.statement_text}
      </p>

      {hiddenIds.size > 0 && (
        <p className="mt-2 rounded-lg border border-indigo-100 bg-indigo-50/60 px-2.5 py-1.5 text-[11px] leading-5 text-indigo-900">
          本题共 {attempts.length} 次作答；每次作答与评价按提交顺序显示在各自轮次
          {receiptSeqs && receiptSeqs.length > 0
            ? `（第 ${receiptSeqs.join("、")} 轮）`
            : ""}
          ，题目主卡不重复展开。
        </p>
      )}

      {(displayAnswer || displaySolution) && (
        <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50/80 p-3">
          <p className="text-[11px] font-medium text-slate-700">
            {displayAttempt
              ? `第 ${displayAttempt.attempt_no} 次 · ${KIND_LABELS[displayAttempt.kind] ?? "本次作答"}：`
              : "你的作答"}
          </p>
          {displayAnswer && (
            <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-900">
              <span className="font-semibold text-slate-600">最终答案：</span>
              {displayAnswer}
            </p>
          )}
          {displaySolution && (
            <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-900">
              <span className="font-semibold text-slate-600">解题步骤：</span>
              {displaySolution}
            </p>
          )}
        </div>
      )}

      {isEvaluationPending && (
        <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs leading-6 text-amber-900">
          作答已经保存，但评价尚未完成。可以再次提交同一份作答重试评价，也可以修改后作为订正提交；旧记录都会保留。
        </p>
      )}

      {needsSupplement && (
        <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs leading-6 text-amber-900">
          评价认为解题过程还需要补充。参考答案暂未展示；可在下方继续补充步骤后重新提交，原作答会保留。
        </p>
      )}

      {isSubmitted && !correcting && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={startCorrection}
            disabled={busy}
            className="rounded-xl border border-indigo-300 bg-indigo-50 px-3 py-1.5 text-xs font-medium text-indigo-700 hover:bg-indigo-100 disabled:opacity-40"
          >
            再订正一次
          </button>
          <span className="text-[11px] text-slate-500">
            订正会作为新记录保存，原答案和评价不会被覆盖。
          </span>
        </div>
      )}

      {showForm && (
        <form onSubmit={handleSubmit} className="mt-4 space-y-3">
          {hasRevealed && (
            <p className="rounded-lg bg-amber-50 p-2 text-xs text-amber-800 border border-amber-200">
              你已查看参考解答，仍可对照思路写出你的答案并提交评价（提交后将标注为“已看解答后作答”）。
            </p>
          )}
          {correcting && (
            <p className="rounded-lg bg-indigo-50 p-2 text-xs text-indigo-900 border border-indigo-200">
              这次提交会作为新的订正记录；请写出订正后的答案或完整过程。
            </p>
          )}
          <div>
            <label
              htmlFor={`ans-${exercise.id}`}
              className="block text-xs font-medium text-slate-600"
            >
              最终答案
            </label>
            <input
              id={`ans-${exercise.id}`}
              ref={answerRef}
              type="text"
              value={answerInput}
              onChange={(e) => {
                setAnswerInput(e.target.value);
                onDraftChange?.({ answer: e.target.value, solution: solutionInput });
              }}
              placeholder="请输入最终答案，例如：x = 2 或 1/2"
              disabled={busy}
              className="mt-1 block w-full rounded-xl border border-slate-200 px-3 py-2 text-sm text-ink placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-50"
            />
          </div>

          <div>
            <label
              htmlFor={`sol-${exercise.id}`}
              className="block text-xs font-medium text-slate-600"
            >
              解题步骤 / 推导思路（选填）
            </label>
            <textarea
              id={`sol-${exercise.id}`}
              rows={3}
              value={solutionInput}
              onChange={(e) => {
                setSolutionInput(e.target.value);
                onDraftChange?.({ answer: answerInput, solution: e.target.value });
              }}
              placeholder="简要写出步骤，AI 将逐条指出思路正误与点拨"
              disabled={busy}
              className="mt-1 block w-full rounded-xl border border-slate-200 px-3 py-2 text-sm text-ink placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-50"
            />
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
            <button
              type="submit"
              disabled={busy || (!answerInput.trim() && !solutionInput.trim())}
              className="inline-flex items-center rounded-xl bg-indigo-600 px-4 py-2 text-xs font-medium text-white shadow-sm hover:bg-indigo-700 disabled:opacity-40"
            >
              {submitLabel}
            </button>

            <div className="flex items-center gap-3">
              {correcting && (
                <button
                  type="button"
                  onClick={() => {
                    setCorrecting(false);
                    setAnswerInput(baseAnswer || "");
                    setSolutionInput(baseSolution || "");
                    onDraftChange?.({
                      answer: baseAnswer || "",
                      solution: baseSolution || "",
                    });
                  }}
                  disabled={busy}
                  className="text-xs text-slate-500 underline decoration-slate-300 hover:text-indigo-600 disabled:opacity-40"
                >
                  取消订正
                </button>
              )}
              {!hasRevealed && onRevealSolution && (
                <button
                  type="button"
                  onClick={handleReveal}
                  disabled={busy || revealing}
                  className="text-xs text-slate-500 underline decoration-slate-300 hover:text-indigo-600 disabled:opacity-40"
                  title="查看后作答将明确标记为已看解答"
                >
                  {revealing ? "获取中..." : "不会做？查看参考解答"}
                </button>
              )}
            </div>
          </div>
        </form>
      )}

      {exercise.reference_solution_text && (
        <div className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50/50 p-4">
          <div className="flex items-center justify-between">
            <h4 className="text-xs font-semibold text-emerald-900">
              参考解答与推导
            </h4>
            {latestAfterReveal && (
              <span className="text-[10px] text-amber-700">
                （参考解答已在本次作答前展示）
              </span>
            )}
          </div>
          <p className="mt-2 whitespace-pre-wrap break-words text-xs leading-6 text-emerald-950">
            {exercise.reference_solution_text}
          </p>
        </div>
      )}

      {displayEvaluation && (
        <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50/90 p-4">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-slate-700">
              作答评价
            </span>
            {displayAttempt && (
              <span className="text-[10px] text-slate-500">
                第 {displayAttempt.attempt_no} 次 ·{" "}
                {KIND_LABELS[displayAttempt.kind] ?? displayAttempt.kind}
              </span>
            )}
          </div>
          <EvaluationBlock
            evaluation={displayEvaluation}
            answeredAfterRevealed={displayAfterReveal}
          />
        </div>
      )}

      {historyAttempts.length > 0 && (
        <details className="mt-4 rounded-xl border border-slate-200 bg-white p-3">
          <summary className="cursor-pointer text-xs font-medium text-slate-600">
            更早的作答与评价（{historyAttempts.length} 次）
          </summary>
          <ol className="mt-3 space-y-3">
            {historyAttempts.map((attempt) => (
              <li
                key={attempt.id}
                className="rounded-lg border border-slate-200 bg-slate-50/70 p-3"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-[11px] font-medium text-slate-700">
                    第 {attempt.attempt_no} 次 ·{" "}
                    {KIND_LABELS[attempt.kind] ?? attempt.kind}
                  </span>
                  <span className="text-[10px] text-slate-400">
                    {attempt.answered_after_revealed
                      ? "参考解答已展示后作答"
                      : "参考解答展示前作答"}
                  </span>
                </div>
                {attempt.student_answer_text && (
                  <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-900">
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
                {attempt.evaluation ? (
                  <EvaluationBlock
                    evaluation={attempt.evaluation}
                    answeredAfterRevealed={attempt.answered_after_revealed}
                  />
                ) : (
                  <p className="mt-2 text-[11px] text-amber-800">
                    这次评价没有完成；可重试评价，旧作答不会丢失。
                  </p>
                )}
              </li>
            ))}
          </ol>
        </details>
      )}
    </div>
  );
}