import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import ExerciseAttemptReceipt from "./ExerciseAttemptReceipt";
import type {
  ExerciseAttemptView,
  ExerciseEvaluation,
  ExerciseView,
} from "../lib/conversationsApi";

const date = "2026-10-25T08:00:00+00:00";
const exerciseId = "22222222-2222-4222-8222-222222222222";

function evaluation(overrides: Partial<ExerciseEvaluation> = {}): ExerciseEvaluation {
  return {
    is_correct: true,
    final_answer_assessment: "答案正确。",
    step_by_step_analysis: "步骤完整。",
    guidance: "继续保持。",
    needs_supplement: false,
    ...overrides,
  };
}

function attempt(overrides: Partial<ExerciseAttemptView> = {}): ExerciseAttemptView {
  return {
    id: `${exerciseId}-attempt-1`,
    attempt_no: 1,
    kind: "initial",
    student_answer_text: "1",
    student_solution_text: "等价无穷小代换。",
    answered_after_revealed: false,
    evaluation: evaluation(),
    request_id: "turn-answer",
    attachments: [],
    created_at: date,
    updated_at: date,
    ...overrides,
  };
}

function exercise(overrides: Partial<ExerciseView> = {}): ExerciseView {
  return {
    id: exerciseId,
    conversation_id: "33333333-3333-4333-8333-333333333333",
    problem_id: "44444444-4444-4444-8444-444444444444",
    root_problem_id: "44444444-4444-4444-8444-444444444444",
    statement_text: "计算极限 lim x->0 tan(x)/x 并说明每一步依据。",
    knowledge_point: "等价无穷小",
    status: "submitted",
    reference_solution_text: null,
    student_answer_text: "1",
    student_solution_text: "等价无穷小代换。",
    revealed_solution_at: date,
    answered_after_revealed: false,
    evaluation: evaluation(),
    attachments: [],
    attempts: [],
    created_at: date,
    updated_at: date,
    ...overrides,
  };
}

describe("作答回执", () => {
  test("已评价：展示这次作答与评价，可回主卡订正", () => {
    const onJump = vi.fn();
    const onCorrect = vi.fn();
    render(
      <ExerciseAttemptReceipt
        exercise={exercise()}
        attempt={attempt()}
        onJump={onJump}
        onCorrect={onCorrect}
      />,
    );

    expect(screen.getByText("第 1 次作答 · 首次作答")).toBeTruthy();
    expect(screen.getByText(/最终答案：/)).toBeTruthy();
    expect(screen.getByText("答案正确。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看题目主卡" }));
    expect(onJump).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "回到题目并订正" }));
    expect(onCorrect).toHaveBeenCalledTimes(1);
  });

  test("评价未完成：不提供无动作的“订正”按钮，提示本轮重试", () => {
    render(
      <ExerciseAttemptReceipt
        exercise={exercise({ evaluation: null, status: "pending_answer" })}
        attempt={attempt({ evaluation: null })}
        onJump={vi.fn()}
        onCorrect={vi.fn()}
      />,
    );

    expect(screen.getByText(/评价没有完成/)).toBeTruthy();
    expect(screen.getByText(/可在本轮重试评价/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "回到题目并订正" })).toBeNull();
    expect(screen.getByRole("button", { name: "查看题目主卡" })).toBeTruthy();
  });
});