import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import ExerciseCard from "./ExerciseCard";
import type {
  ExerciseAttemptView,
  ExerciseEvaluation,
  ExerciseView,
} from "../lib/conversationsApi";

const date = "2026-10-25T08:00:00+00:00";
const id = "22222222-2222-4222-8222-222222222222";

function evaluation(
  overrides: Partial<ExerciseEvaluation> = {},
): ExerciseEvaluation {
  return {
    is_correct: true,
    final_answer_assessment: "答案正确。",
    step_by_step_analysis: "步骤完整。",
    guidance: "继续保持。",
    needs_supplement: false,
    ...overrides,
  };
}

function attempt(
  overrides: Partial<ExerciseAttemptView> = {},
): ExerciseAttemptView {
  return {
    id: `${id}-attempt-1`,
    attempt_no: 1,
    kind: "initial",
    student_answer_text: "1",
    student_solution_text: "等价无穷小代换。",
    answered_after_revealed: false,
    evaluation: evaluation(),
    request_id: null,
    attachments: [],
    created_at: date,
    updated_at: date,
    ...overrides,
  };
}

function exercise(overrides: Partial<ExerciseView> = {}): ExerciseView {
  return {
    id,
    conversation_id: "33333333-3333-4333-8333-333333333333",
    problem_id: "44444444-4444-4444-8444-444444444444",
    root_problem_id: "44444444-4444-4444-8444-444444444444",
    statement_text: "计算极限 lim x->0 tan(x)/x",
    knowledge_point: "等价无穷小",
    status: "submitted",
    reference_solution_text: "tan x ~ x，答案为 1。",
    student_answer_text: "1",
    student_solution_text: "等价无穷小代换。",
    revealed_solution_at: date,
    answered_after_revealed: false,
    evaluation: evaluation(),
    attachments: [],
    attempts: [attempt()],
    created_at: date,
    updated_at: date,
    ...overrides,
  };
}

describe("练习卡片：尝试历史与订正/补充", () => {
  test("查看解答后的作答标注对照，不出现“已掌握”", () => {
    const afterReveal = evaluation({
      independence_assessment: "after_reveal",
      independence_note: "这次作答是在参考解答展示之后完成的，答案正确不代表独立掌握。",
    });
    render(
      <ExerciseCard
        exercise={exercise({
          answered_after_revealed: true,
          attempts: [
            attempt({
              answered_after_revealed: true,
              evaluation: afterReveal,
            }),
          ],
          evaluation: afterReveal,
        })}
      />,
    );
    expect(screen.getAllByText("已看解答后作答").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/不代表独立掌握/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/已掌握/)).toBeNull();
  });

  test("已完成评价后可以再订正，提交时带 correction，历史保留", () => {
    const onSubmit = vi.fn();
    render(
      <ExerciseCard
        exercise={exercise({
          attempts: [
            attempt(),
            attempt({
              id: `${id}-attempt-2`,
              attempt_no: 2,
              kind: "correction",
              student_answer_text: "2",
              student_solution_text: "重新计算。",
              answered_after_revealed: true,
              evaluation: evaluation({
                final_answer_assessment: "订正后答案正确。",
                independence_assessment: "after_reveal",
                independence_note: "这是查看参考解答后的对照作答。",
              }),
            }),
          ],
          student_answer_text: "2",
          student_solution_text: "重新计算。",
          answered_after_revealed: true,
        })}
        onSubmitAnswer={onSubmit}
      />,
    );
    expect(screen.getByText(/更早的作答与评价（1 次）/)).toBeTruthy();
    expect(screen.getByText(/已看解答后作答/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "再订正一次" }));
    fireEvent.change(screen.getByLabelText("最终答案"), {
      target: { value: "3" },
    });
    fireEvent.change(screen.getByLabelText("解题步骤 / 推导思路（选填）"), {
      target: { value: "换一种方法再算。" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "提交订正并重新评价" }),
    );
    expect(onSubmit).toHaveBeenCalledWith(id, "3", "换一种方法再算。", "correction");
  });

  test("待补充时隐藏参考答案，提交带 supplement 并保留原答案", () => {
    const onSubmit = vi.fn();
    const supplement = evaluation({
      is_correct: null,
      final_answer_assessment: "只看到答案，未看到推导。",
      needs_supplement: true,
    });
    render(
      <ExerciseCard
        exercise={exercise({
          status: "pending_answer",
          reference_solution_text: null,
          revealed_solution_at: null,
          evaluation: supplement,
          attempts: [
            attempt({
              evaluation: supplement,
              student_solution_text: null,
            }),
          ],
        })}
        onSubmitAnswer={onSubmit}
      />,
    );
    expect(
      screen.getByRole("button", { name: "补充并重新评价" }),
    ).toBeTruthy();
    expect(screen.queryByText(/参考解答与推导/)).toBeNull();
    fireEvent.change(screen.getByLabelText("最终答案"), {
      target: { value: "1" },
    });
    fireEvent.change(screen.getByLabelText("解题步骤 / 推导思路（选填）"), {
      target: { value: "补充：先做等价无穷小代换。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "补充并重新评价" }));
    expect(onSubmit).toHaveBeenCalledWith(
      id,
      "1",
      "补充：先做等价无穷小代换。",
      "supplement",
    );
  });

  test("评价失败保留已存作答，可重试且不丢旧答案", () => {
    render(
      <ExerciseCard
        exercise={exercise({
          evaluation: null,
          attempts: [
            attempt({
              evaluation: null,
              student_answer_text: "1",
              student_solution_text: "只写了代换。",
            }),
          ],
          student_answer_text: "1",
        })}
      />,
    );
    expect(screen.getByText(/评价尚未完成/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "提交作答并评价" })).toBeTruthy();
    expect(screen.getByText("1")).toBeTruthy();
    expect(screen.queryByText(/已掌握/)).toBeNull();
  });

  test("回执接管后主卡不预填、不重复隐藏的作答", () => {
    const hidden = attempt({
      student_answer_text: "3",
      evaluation: null,
    });
    render(
      <ExerciseCard
        exercise={exercise({
          attempts: [hidden],
          evaluation: null,
          status: "pending_answer",
          student_answer_text: "3",
          student_solution_text: "",
        })}
        hiddenAttemptIds={[hidden.id]}
      />,
    );

    expect(screen.getByText(/本题共 1 次作答/)).toBeTruthy();
    expect(screen.queryByDisplayValue("3")).toBeNull();
    expect(screen.queryByText("最终答案：")).toBeNull();
  });
});