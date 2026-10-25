import type { PaperItem } from "../lib/papersApi";
import type {
  ConversationTurn as StoredTurn,
  ExerciseAttemptView,
  ProblemView,
  SuggestionItem,
} from "../lib/conversationsApi";
import ExerciseCard from "./ExerciseCard";
import ExerciseAttemptReceipt from "./ExerciseAttemptReceipt";
import ProblemCard from "./ProblemCard";
import QueryResult from "./QueryResult";
import SourceBadge from "./SourceBadge";
import { turnBusinessStatus } from "../lib/conversationsApi";
import {
  actionLabel,
  actionStatusLabel,
  classifySuggestion,
  dedupeTexts,
  formatDateTime,
  isCoveredText,
  type DownloadStates,
} from "../lib/display";

export type ExerciseRole = "full" | "attempt" | "reference";

type ConversationTurnProps = {
  turn: StoredTurn;
  downloads?: DownloadStates;
  onDownload: (paper: PaperItem) => void;
  onRetry: (turn: StoredTurn) => void;
  exerciseDrafts?: Record<string, { answer: string; solution: string }>;
  onExerciseDraftChange?: (exerciseId: string, draft: { answer: string; solution: string }) => void;
  onFeedback: (problem: ProblemView) => void;
  onRecord: (problem: ProblemView) => void;
  onCorrect: (problem: ProblemView) => void;
  onSupplement: (problem: ProblemView) => void;
  onNew: () => void;
  onIntentChoice: (turn: StoredTurn, option: string) => void;
  onOpenRecord: (recordId: string) => void;
  onSubmitExerciseAnswer?: (
    exerciseId: string,
    answerText: string,
    solutionText?: string,
    attemptKind?: "supplement" | "correction",
  ) => void;
  onRevealExerciseSolution?: (exerciseId: string) => void;
  onSuggestionClick?: (turn: StoredTurn, suggestion: SuggestionItem) => void;
  onJumpToExercise?: (exerciseId: string) => void;
  onCorrectExercise?: (exerciseId: string) => void;
  /** 这一轮是否为当前最新一轮；旧轮次的建议已经失效，不再展示。 */
  isLatestTurn?: boolean;
  hasActiveProblem?: boolean;
  exerciseRole?: ExerciseRole;
  exerciseAttempt?: ExerciseAttemptView | null;
  hiddenAttemptIds?: string[];
  receiptSeqs?: number[];
  focusToken?: number;
  correctionToken?: number;
  busy: boolean;
};

const STATUS_LABELS: Record<StoredTurn["status"], string> = {
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
};

function statusClass(status: StoredTurn["status"]): string {
  if (status === "completed") {
    return "border-emerald-200 bg-emerald-50 text-emerald-700";
  }
  if (status === "failed") {
    return "border-rose-200 bg-rose-50 text-rose-700";
  }
  if (status === "interrupted") {
    return "border-amber-200 bg-amber-50 text-amber-700";
  }
  return "border-slate-200 bg-slate-50 text-slate-600";
}

const OPTION_LABELS: Record<string, string> = {
  query: "查试卷",
  tutoring: "辅导这道题",
  new_problem: "开始新题",
  supplement: "补充当前题",
  correction: "纠正题干",
};

function bubbleFallback(turn: StoredTurn): string {
  if (turn.kind === "exercise") {
    return "（提交练习作答）";
  }
  if (turn.kind === "reveal_solution") {
    return "（查看参考解答）";
  }
  return "（图片题目）";
}

/**
 * 建议只在“最新一轮 + 当前状态仍有效”时出现：
 * 已评价练习不再提示提交答案，已展开解答不再提示看答案，没有当前题不再提示练习。
 */
function usableSuggestions(
  turn: StoredTurn,
  isLatestTurn: boolean,
  hasActiveProblem: boolean,
): SuggestionItem[] {
  if (!isLatestTurn) {
    return [];
  }
  const exercise = turn.exercise ?? null;
  return (turn.suggestions ?? []).filter((suggestion) => {
    const intent = classifySuggestion(suggestion.label);
    if (intent === "answer") {
      return Boolean(exercise) && !exercise!.evaluation && exercise!.status !== "submitted";
    }
    if (intent === "reveal") {
      return (
        Boolean(exercise) &&
        !exercise!.evaluation &&
        !exercise!.revealed_solution_at &&
        !exercise!.reference_solution_text
      );
    }
    if (intent === "save") {
      return turn.tutoring?.kind === "explanation";
    }
    if (intent === "practice") {
      return hasActiveProblem;
    }
    return true;
  });
}

/** 一轮历史：学生输入、状态和查询或辅导结果。 */
export default function ConversationTurn({
  turn,
  downloads,
  onDownload,
  onRetry,
  onFeedback,
  onRecord,
  onCorrect,
  onSupplement,
  onNew,
  onIntentChoice,
  onOpenRecord,
  onSubmitExerciseAnswer,
  onRevealExerciseSolution,
  onSuggestionClick,
  onJumpToExercise,
  onCorrectExercise,
  isLatestTurn = true,
  hasActiveProblem = true,
  exerciseRole = "full",
  exerciseAttempt = null,
  hiddenAttemptIds,
  receiptSeqs,
  focusToken = 0,
  correctionToken = 0,
  busy,
  exerciseDrafts,
  onExerciseDraftChange,
}: ConversationTurnProps) {
  const tutoring = turn.tutoring;
  const business = turnBusinessStatus(turn);
  const hasSavedResults = turn.status === "completed" || business === "partial" || business === "succeeded";
  const labels = { succeeded: "成功", partial: "部分完成", failed: "未完成", needs_input: "待补充" };
  const badgeClass = business === "succeeded" ? statusClass("completed") :
    business === "failed" ? statusClass("failed") : business ? statusClass("interrupted") : statusClass(turn.status);

  const resultMessage =
    turn.result && "message" in turn.result ? turn.result.message : null;
  const compositeItems = turn.composite_results ?? [];
  const { shown: narrativeParts } = dedupeTexts([turn.assistant_message]);
  const assistantText = narrativeParts[0] ?? "";
  // 优先级：自然语言叙事 → 查询结果消息 → 动作结果消息。
  // 后面的文字只要已被前面的叙事整段包含，就不再单独重复。
  const shownTexts: string[] = [assistantText];
  const showResultMessage =
    Boolean(resultMessage) && !isCoveredText(resultMessage, shownTexts);
  if (showResultMessage && resultMessage) {
    shownTexts.push(resultMessage);
  }
  const compositeRows = compositeItems.map((item) => {
    const showMessage = !isCoveredText(item.message, shownTexts);
    if (showMessage) {
      shownTexts.push(item.message);
    }
    return { item, showMessage };
  });

  const suggestions = usableSuggestions(turn, isLatestTurn, hasActiveProblem);
  const exercise = turn.exercise ?? null;
  // 只有真正提交/重试作答的轮次才需要“去题目卡片查看”的指引；
  // 追问、保存等轮次只是带着练习上下文，不用重复同一块提示。
  const showsExerciseReference =
    Boolean(exercise) &&
    exerciseRole === "reference" &&
    (turn.kind === "exercise" || turn.kind === "exercise_answer");
  const userText = turn.query.trim() || bubbleFallback(turn);

  return (
    <li className="space-y-3">
      <div className="flex justify-end">
        <p className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl bg-river px-4 py-2.5 text-sm leading-6 text-white">
          {userText}
        </p>
      </div>

      <div className="rounded-2xl border border-slate-200/90 bg-white/90 p-5 shadow-card backdrop-blur">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span
              className={`rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${badgeClass}`}
            >
              {business ? labels[business] : STATUS_LABELS[turn.status]}
            </span>
            <span className="text-[11px] text-slate-400">
              第 {turn.seq} 轮 · {formatDateTime(turn.started_at)}
            </span>
          </div>
          {turn.retryable === true && (
            <button
              type="button"
              disabled={busy}
              onClick={() => onRetry(turn)}
              className="rounded-lg border border-slate-300 bg-white px-2.5 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-100"
            >
              {turn.kind === "exercise" ? "重试评价" : "重试未完成步骤"}
            </button>
          )}
        </div>

        <div className="mt-3 space-y-3">
          {business && business !== "succeeded" && turn.retry_hint && (
            <p className="text-xs leading-6 text-amber-900">{turn.retry_hint}</p>
          )}
          {/* 自然语言助手回复；与结果/动作消息完全相同的段落不重复渲染 */}
          {assistantText && (
            <p className="whitespace-pre-wrap break-words text-sm leading-7 text-slate-800">
              {assistantText}
            </p>
          )}

          {/* 复合操作执行结果反馈：内部动作名转成中文，重复消息折叠为状态 */}
          {compositeRows.length > 0 && (
            <div className="space-y-1.5">
              {compositeRows.map(({ item, showMessage }, idx) => (
                <div
                  key={idx}
                  className={`flex items-center justify-between rounded-xl px-3 py-2 text-xs ${
                    item.status === "success"
                      ? "border border-slate-200 bg-slate-50/80 text-slate-700"
                      : item.status === "clarification"
                        ? "border border-amber-200 bg-amber-50 text-amber-800"
                        : "border border-rose-200 bg-rose-50 text-rose-700"
                  }`}
                >
                  <span className="break-words">
                    {showMessage ? item.message : actionStatusLabel(item.status)}
                  </span>
                  <span className="ml-2 shrink-0 text-[10px] text-slate-400">
                    {actionLabel(item.action)}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* 试卷检索结果（普通查询轮次或动态查卷动作产出） */}
          {hasSavedResults && turn.result && (
            <QueryResult
              result={turn.result}
              downloads={downloads}
              showMessage={showResultMessage}
              onDownload={onDownload}
            />
          )}

          {/* 练习：一张题目主卡 + 每次作答的紧凑回执，避免两张相同大卡片 */}
          {exercise && exerciseRole === "attempt" && exerciseAttempt && (
            <ExerciseAttemptReceipt
              exercise={exercise}
              attempt={exerciseAttempt}
              busy={busy}
              onJump={() => onJumpToExercise?.(exercise.id)}
              onCorrect={
                onCorrectExercise ? () => onCorrectExercise(exercise.id) : undefined
              }
            />
          )}

          {showsExerciseReference && exercise && (
            <div className="rounded-2xl border border-slate-200 bg-slate-50/70 p-4">
              <p className="text-xs leading-6 text-slate-600">
                这道练习的题目在上方主卡；本次作答与评价保留在卡片的最新记录中。
              </p>
              <button
                type="button"
                onClick={() => onJumpToExercise?.(exercise.id)}
                className="mt-2 text-xs font-medium text-river underline decoration-river/30 hover:text-river/80"
              >
                查看题目主卡
              </button>
            </div>
          )}

          {exercise && exerciseRole === "full" && (
            <ExerciseCard
              exercise={exercise}
              busy={busy}
              anchorId={`exercise-card-${exercise.id}`}
              hiddenAttemptIds={hiddenAttemptIds}
              receiptSeqs={receiptSeqs}
              focusToken={focusToken}
              correctionToken={correctionToken}
              onSubmitAnswer={onSubmitExerciseAnswer}
              onRevealSolution={onRevealExerciseSolution}
              draft={exerciseDrafts?.[exercise.id]}
              onDraftChange={(draft) => onExerciseDraftChange?.(exercise.id, draft)}
            />
          )}

          {hasSavedResults &&
            tutoring?.kind === "explanation" && (
              <ProblemCard
                result={tutoring}
                busy={busy}
                onFeedback={onFeedback}
                onRecord={onRecord}
                onCorrect={onCorrect}
                onSupplement={onSupplement}
                onNew={onNew}
              />
            )}

          {hasSavedResults &&
            tutoring?.kind === "needs_clarification" && (
              <div className="rounded-2xl border border-amber-200 bg-amber-50/80 p-4">
                <p className="text-sm font-semibold text-amber-900">
                  题目还不清楚，先补充再讲解
                </p>
                <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-amber-950">
                  {tutoring.problem.statement_text}
                </p>
                <ul className="mt-2 list-disc space-y-1 pl-5 text-xs leading-6 text-amber-900">
                  {tutoring.questions.map((question) => (
                    <li key={question}>{question}</li>
                  ))}
                </ul>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => onSupplement(tutoring.problem)}
                    className="rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs font-medium text-amber-900 hover:bg-amber-100"
                  >
                    补充当前题
                  </button>
                  <button
                    type="button"
                    onClick={() => onCorrect(tutoring.problem)}
                    className="rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs font-medium text-amber-900 hover:bg-amber-100"
                  >
                    直接纠正题干
                  </button>
                </div>
              </div>
            )}

          {hasSavedResults &&
            tutoring?.kind === "intent_clarification" && (
              <div className="rounded-2xl border border-slate-200 bg-white p-4">
                <p className="text-sm leading-6 text-slate-700">
                  {tutoring.message}
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {tutoring.options.map((option) => (
                    <button
                      key={option}
                      type="button"
                      onClick={() => onIntentChoice(turn, option)}
                      className="rounded-lg border border-river/30 bg-white px-3 py-1.5 text-xs font-medium text-river hover:bg-river/5"
                    >
                      {OPTION_LABELS[option] ?? option}
                    </button>
                  ))}
                </div>
              </div>
            )}

          {hasSavedResults &&
            tutoring?.kind === "followup_answer" && (
              <div className="rounded-2xl border border-slate-200 bg-slate-50/70 p-4">
                <p className="text-[11px] font-medium text-slate-500">追问回答</p>
                <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-7 text-slate-800">
                  {tutoring.answer}
                </p>
                {tutoring.related_steps.length > 0 && (
                  <p className="mt-2 text-[11px] text-slate-500">
                    相关步骤：{tutoring.related_steps.join("、")}
                  </p>
                )}
              </div>
            )}

          {hasSavedResults &&
            (tutoring?.kind === "record_created" ||
              tutoring?.kind === "record_exists") && (
              <div className="rounded-2xl border border-emerald-200 bg-emerald-50/80 p-4">
                <p className="text-sm font-semibold text-emerald-900">
                  {tutoring.kind === "record_created"
                    ? "已记录到错题本"
                    : tutoring.needs_update
                      ? "已有旧版本记录"
                      : "这道题已经记录过了"}
                </p>
                <p className="mt-1 text-sm leading-6 text-emerald-900">
                  {tutoring.kind === "record_exists" && tutoring.needs_update
                    ? "原记录对应旧题干，可在错题本中更新或重新讲解。"
                    : "记录包含题目、图片、来源、讲解和反馈；删除记录不会删除原会话。"}
                </p>
                <button
                  type="button"
                  onClick={() => onOpenRecord(tutoring.record.id)}
                  className="mt-2 rounded-lg border border-emerald-300 bg-white px-3 py-1.5 text-xs font-medium text-emerald-800 hover:bg-emerald-100"
                >
                  打开错题本记录
                </button>
              </div>
            )}

          {hasSavedResults &&
            tutoring?.kind === "feedback_recorded" && (
              <div className="rounded-2xl border border-emerald-200 bg-emerald-50/70 p-3">
                <p className="text-sm leading-6 text-emerald-900">
                  {tutoring.message}
                </p>
              </div>
            )}

          {turn.status === "failed" && turn.error && (
            <div>
              <p className="break-words text-sm leading-6 text-rose-800">
                {turn.error.message}
              </p>
              <details className="mt-1 text-[11px] text-rose-500">
                <summary className="cursor-pointer">诊断信息</summary>
                <p className="mt-1">错误码：{turn.error.code}</p>
              </details>
            </div>
          )}

          {turn.status === "interrupted" && (
            <p className="text-sm leading-6 text-amber-800">
              {business === "succeeded"
                ? "连接未完整结束，已确认的业务结果已经保存。"
                : business === "partial"
                  ? "连接中断，已成功的操作会保留，可按提示恢复未完成步骤。"
                  : "这一轮因连接停止或中断而未完成，可按提示继续操作。"}
            </p>
          )}

          {turn.status === "running" && (
            <p className="text-sm leading-6 text-slate-600">
              这一轮仍在进行中。若长时间没有变化，请刷新会话记录确认状态。
            </p>
          )}

          {hasSavedResults &&
            tutoring?.kind === "explanation" &&
            tutoring.problem.source.type !== "none" && (
              <div className="mt-2">
                <SourceBadge source={tutoring.problem.source} />
              </div>
            )}

          {/* 快捷建议操作：只在最新一轮展示仍然有效的建议 */}
          {suggestions.length > 0 && (
            <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
              <span className="text-xs text-slate-400">建议下一步：</span>
              {suggestions.map((sug) => (
                <button
                  key={`${sug.action}-${sug.label}`}
                  type="button"
                  disabled={busy}
                  onClick={() => onSuggestionClick?.(turn, sug)}
                  className="rounded-full border border-indigo-200 bg-indigo-50/70 px-3 py-1 text-xs font-medium text-indigo-700 hover:bg-indigo-100 disabled:opacity-40"
                >
                  {sug.label}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </li>
  );
}