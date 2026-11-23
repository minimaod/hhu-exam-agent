import { useState } from "react";
import type { QuizQuestionPublicView } from "../lib/materialsApi.ts";
import MathText from "./MathText.tsx";

const TYPE_LABELS: Record<QuizQuestionPublicView["q_type"], string> = {
  choice: "单选题",
  multi: "多选题",
  tf: "判断题",
  fill: "填空题",
  calc: "计算题",
  code: "编程题",
  short: "简答题",
};

function formatRevealedAnswer(q: QuizQuestionPublicView): string {
  const ans = q.revealed_answer;
  if (ans === null || ans === undefined) return "";
  if (q.q_type === "tf") {
    return ans === true || ans === 0 || ans === "true" ? "正确" : "错误";
  }
  if (q.q_type === "choice" && typeof ans === "number" && q.options?.[ans]) {
    return `${String.fromCharCode(65 + ans)}. ${q.options[ans]}`;
  }
  if (q.q_type === "multi" && Array.isArray(ans) && q.options) {
    return ans
      .map((idx) =>
        typeof idx === "number" && q.options?.[idx]
          ? `${String.fromCharCode(65 + idx)}. ${q.options[idx]}`
          : String(idx),
      )
      .join("；");
  }
  if (Array.isArray(ans)) {
    return ans.map((x) => (Array.isArray(x) ? x.join(" / ") : String(x))).join("；");
  }
  return String(ans);
}

export default function MaterialQuizView({
  questions,
  submittingId,
  onSubmitAttempt,
  onRevealSolution,
  onOpenPage,
  onStudyQuestion,
  onEvaluateQuestion,
  oneAtATime = false,
  hideSummary = false,
  disableActions = false,
  studyLabel,
  onReviewNote,
}: {
  questions: QuizQuestionPublicView[];
  submittingId: string | null;
  onSubmitAttempt: (questionId: string, userAnswer: unknown) => Promise<void>;
  onRevealSolution: (questionId: string) => Promise<void>;
  onOpenPage: (pageNo: number) => void;
  onStudyQuestion?: (questionId: string, mode: "explain" | "evaluate") => void;
  onEvaluateQuestion?: (questionId: string) => Promise<void>;
  oneAtATime?: boolean;
  hideSummary?: boolean;
  disableActions?: boolean;
  studyLabel?: string;
  onReviewNote?: () => void;
}) {
  const [activeIndex, setActiveIndex] = useState(() => Math.max(0, questions.findIndex((q) =>
    !q.attempt || q.attempt.grading_status === "unanswered",
  )));
  const questionIndex = Math.min(activeIndex, questions.length - 1);
  const [drafts, setDrafts] = useState<Record<string, unknown>>({});

  const objectiveQuestions = questions.filter((q) =>
    ["choice", "multi", "tf", "fill"].includes(q.q_type),
  );
  const subjectiveQuestions = questions.filter(
    (q) => !["choice", "multi", "tf", "fill"].includes(q.q_type),
  );
  const gradedObj = objectiveQuestions.filter(
    (q) => q.attempt?.grading_status === "auto_graded",
  );
  const objEarned = gradedObj.reduce((acc, q) => acc + (q.attempt?.score ?? 0), 0);
  const objMax = objectiveQuestions.reduce((acc, q) => acc + q.points, 0);

  const getBlankCount = (stem: string): number => {
    const m = stem.match(/(_{2,}|＿+)/g);
    return m && m.length > 0 ? m.length : 1;
  };

  const getDraft = (q: QuizQuestionPublicView) => {
    if (q.id in drafts) return drafts[q.id];
    if (q.attempt && q.attempt.user_answer !== null && q.attempt.user_answer !== undefined) {
      return q.attempt.user_answer;
    }
    if (q.q_type === "multi") return [];
    if (q.q_type === "fill") return Array.from({ length: getBlankCount(q.stem) }, () => "");
    return "";
  };

  const setDraft = (qid: string, val: unknown) => {
    setDrafts((prev) => ({ ...prev, [qid]: val }));
  };

  return (
    <div className="epa-quiz-shell">
      {!hideSummary && <div className="epa-quiz-summary">
        <div>
          <strong>章节测试（共 {questions.length} 题）</strong>
          <span className="muted-note" style={{ marginLeft: 12 }}>
            客观题已答 {gradedObj.length}/{objectiveQuestions.length} · 得分 {objEarned} / {objMax}
          </span>
          {subjectiveQuestions.length > 0 && (
            <span className="muted-note" style={{ marginLeft: 12 }}>
              另含 {subjectiveQuestions.length} 道主观题（不计入自动客观分）
            </span>
          )}
        </div>
      </div>}

      {oneAtATime && questions.length > 0 && (
        <nav aria-label="章节测试题目导航" className="mb-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="quiet-button"
            disabled={questionIndex === 0 || submittingId !== null}
            onClick={() => setActiveIndex(questionIndex - 1)}
          >
            上一题
          </button>
          <label className="flex items-center gap-2 text-sm">
            跳到题目
            <select
              className="quiet-button"
              value={questionIndex}
              disabled={submittingId !== null}
              onChange={(event) => setActiveIndex(Number(event.target.value))}
            >
              {questions.map((question, index) => (
                <option key={question.id} value={index}>
                  第 {index + 1} / {questions.length} 题
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="quiet-button"
            disabled={questionIndex === questions.length - 1 || submittingId !== null}
            onClick={() => setActiveIndex(questionIndex + 1)}
          >
            下一题
          </button>
        </nav>
      )}

      {questions.map((q, idx) => {
        if (oneAtATime && idx !== questionIndex) return null;
        const draft = getDraft(q);
        const attempt = q.attempt;
        const busy = submittingId === q.id;
        const blocked = busy || disableActions;
        const canShowSolution = q.revealed_answer !== null && q.revealed_answer !== undefined;

        return (
          <article key={q.id} className="epa-quiz-card">
            <div className="epa-quiz-card-head">
              <div className="epa-quiz-meta">
                <span className="epa-quiz-index">第 {idx + 1} 题</span>
                <span className="guide-category-badge">{TYPE_LABELS[q.q_type]}</span>
                <span className="muted-note">{q.points} 分</span>
                {q.difficulty && <span className="muted-note">· {q.difficulty}</span>}
                {q.source_pages.length > 0 ? (
                  <button
                    type="button"
                    className="citation-chip"
                    onClick={() => onOpenPage(q.source_pages[0])}
                  >
                    原件第 {q.source_pages[0]} 页
                  </button>
                ) : (
                  <span className="muted-note">来源待核对</span>
                )}
              </div>

              {attempt && (
                <span
                  className={`epa-verdict-badge ${
                    attempt.grading_status === "auto_graded" ||
                    attempt.grading_status === "evaluated"
                      ? attempt.is_correct
                        ? "is-ok"
                        : attempt.is_correct === false
                          ? "is-no"
                          : "is-pending"
                      : "is-pending"
                  }`}
                >
                  {attempt.grading_status === "auto_graded" ||
                  attempt.grading_status === "evaluated"
                    ? attempt.is_correct
                      ? `当前：正确 (+${attempt.score ?? 0})`
                      : attempt.is_correct === false
                        ? `当前：有误 (${attempt.score ?? 0}/${attempt.max_points})`
                        : "AI 已评价（待补充）"
                    : attempt.grading_status === "pending_review"
                      ? q.q_type === "fill"
                        ? "数学填空待核对"
                        : "主观题待评价"
                      : attempt.revealed_solution_at
                        ? "已查看解析"
                        : "已记录"}
                  {attempt.answered_after_revealed ? " · 看答案后订正" : ""}
                  {attempt.answered_after_revealed &&
                    attempt.first_is_correct !== null &&
                    attempt.first_is_correct !== undefined &&
                    `（初次：${attempt.first_is_correct ? "对" : "错"} ${attempt.first_score ?? 0}分）`}
                </span>
              )}
            </div>

            <div className="epa-quiz-stem">
              <MathText text={q.stem} />
            </div>

            {q.q_type === "choice" && q.options && (
              <div className="epa-options-list">
                {q.options.map((opt, optIdx) => (
                  <label
                    key={optIdx}
                    className={`epa-option-row ${draft === optIdx ? "is-selected" : ""}`}
                  >
                    <input
                      type="radio"
                      name={`quiz-${q.id}`}
                      checked={draft === optIdx}
                      onChange={() => setDraft(q.id, optIdx)}
                    />
                    <span>
                      <strong>{String.fromCharCode(65 + optIdx)}. </strong>
                      <MathText text={opt} />
                    </span>
                  </label>
                ))}
              </div>
            )}

            {q.q_type === "tf" && (
              <div className="epa-options-list">
                {[
                  { label: "正确", val: true },
                  { label: "错误", val: false },
                ].map((opt) => (
                  <label
                    key={opt.label}
                    className={`epa-option-row ${
                      draft === opt.val || (opt.val ? draft === 0 : draft === 1)
                        ? "is-selected"
                        : ""
                    }`}
                  >
                    <input
                      type="radio"
                      name={`quiz-${q.id}`}
                      checked={draft === opt.val || (opt.val ? draft === 0 : draft === 1)}
                      onChange={() => setDraft(q.id, opt.val)}
                    />
                    <span>{opt.label}</span>
                  </label>
                ))}
              </div>
            )}

            {q.q_type === "multi" && q.options && (
              <div className="epa-options-list">
                {q.options.map((opt, optIdx) => {
                  const arr = Array.isArray(draft) ? (draft as number[]) : [];
                  const checked = arr.includes(optIdx);
                  return (
                    <label
                      key={optIdx}
                      className={`epa-option-row ${checked ? "is-selected" : ""}`}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(e) => {
                          const next = e.target.checked
                            ? [...arr, optIdx].sort((a, b) => a - b)
                            : arr.filter((x) => x !== optIdx);
                          setDraft(q.id, next);
                        }}
                      />
                      <span>
                        <strong>{String.fromCharCode(65 + optIdx)}. </strong>
                        <MathText text={opt} />
                      </span>
                    </label>
                  );
                })}
              </div>
            )}

            {q.q_type === "fill" && (
              <div className="epa-text-answer-wrap">
                {Array.from({ length: getBlankCount(q.stem) }, (_, blankIdx) => {
                  const arr = Array.isArray(draft)
                    ? (draft as string[])
                    : [String(draft ?? "")];
                  return (
                    <input
                      key={blankIdx}
                      type="text"
                      className="epa-text-input"
                      style={blankIdx > 0 ? { marginTop: 6 } : undefined}
                      placeholder={`第 ${blankIdx + 1} 空答案`}
                      value={String(arr[blankIdx] ?? "")}
                      onChange={(e) => {
                        const next = Array.from(
                          { length: getBlankCount(q.stem) },
                          (__, i) => String(arr[i] ?? ""),
                        );
                        next[blankIdx] = e.target.value;
                        setDraft(q.id, next);
                      }}
                    />
                  );
                })}
              </div>
            )}

            {(q.q_type === "calc" || q.q_type === "code" || q.q_type === "short") && (
              <div className="epa-text-answer-wrap">
                <textarea
                  rows={3}
                  className="epa-textarea"
                  placeholder="写下你的推导过程与最终结果…"
                  value={typeof draft === "string" ? draft : ""}
                  onChange={(e) => setDraft(q.id, e.target.value)}
                />
              </div>
            )}

            <div className="epa-quiz-actions">
              <button
                type="button"
                className="primary-btn compact-btn"
                disabled={blocked}
                onClick={() => void onSubmitAttempt(q.id, draft)}
              >
                {busy ? "提交中…" : "提交本题"}
              </button>
              {!canShowSolution && (
                <button
                  type="button"
                  className="ghost-btn compact-btn"
                  disabled={blocked}
                  onClick={() => void onRevealSolution(q.id)}
                >
                  查看参考答案
                </button>
              )}
              {onEvaluateQuestion &&
                attempt?.grading_status === "pending_review" && (
                  <button
                    type="button"
                    className="quiet-button compact-btn"
                    disabled={blocked}
                    onClick={() => void onEvaluateQuestion(q.id)}
                  >
                    {busy ? "评价中…" : "AI 评价本题"}
                  </button>
                )}
              {onStudyQuestion && (
                <button
                  type="button"
                  className="ghost-btn compact-btn"
                  disabled={blocked}
                  onClick={() =>
                    onStudyQuestion(
                      q.id,
                      ["calc", "code", "short"].includes(q.q_type) && attempt
                        ? "evaluate"
                        : "explain",
                    )
                  }
                >
                  {studyLabel ?? (["calc", "code", "short"].includes(q.q_type) && attempt
                    ? "去对话 AI 评价"
                    : "去聊天追问")}
                </button>
              )}
            </div>

            {attempt?.is_correct === false && onReviewNote && (
              <button type="button" className="review-link" onClick={onReviewNote}>回到该节笔记</button>
            )}

            {attempt?.feedback && (
              <p className="muted-note" style={{ marginTop: 8 }}>
                {attempt.feedback}
              </p>
            )}

            {canShowSolution && (
              <div className="epa-solution-box">
                <div>
                  <strong>参考答案：</strong>
                  <MathText text={formatRevealedAnswer(q)} />
                </div>
                {q.revealed_explanation && (
                  <div style={{ marginTop: 6 }}>
                    <strong>解析：</strong>
                    <MathText text={q.revealed_explanation} />
                  </div>
                )}
                {q.pitfall && (
                  <div className="epa-pitfall-line" style={{ marginTop: 6 }}>
                    <strong>易错提醒：</strong>
                    <MathText text={q.pitfall} />
                  </div>
                )}
              </div>
            )}
          </article>
        );
      })}
    </div>
  );
}
