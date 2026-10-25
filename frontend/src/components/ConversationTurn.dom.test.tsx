import type { ComponentProps } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import ConversationTurn from "./ConversationTurn";
import ExerciseAttemptReceipt from "./ExerciseAttemptReceipt";
import type {
  ConversationTurn as StoredTurn,
  ExerciseAttemptView,
  ExerciseEvaluation,
  ExerciseView,
} from "../lib/conversationsApi";

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

function exercise(overrides: Partial<ExerciseView> = {}): ExerciseView {
  return {
    id: "exercise-1",
    conversation_id: "conversation-1",
    problem_id: "problem-1",
    root_problem_id: "problem-1",
    statement_text: "计算极限 lim x→0 sin(3x)/x",
    knowledge_point: "重要极限",
    status: "pending_answer",
    answered_after_revealed: false,
    attachments: [],
    created_at: "2026-10-25T12:00:00+00:00",
    updated_at: "2026-10-25T12:00:00+00:00",
    ...overrides,
  };
}

function attempt(overrides: Partial<ExerciseAttemptView> = {}): ExerciseAttemptView {
  return {
    id: "attempt-1",
    attempt_no: 1,
    kind: "initial",
    student_answer_text: "3",
    student_solution_text: "使用重要极限。",
    answered_after_revealed: false,
    evaluation: evaluation(),
    request_id: "turn-answer",
    attachments: [],
    created_at: "2026-10-25T12:01:00+00:00",
    updated_at: "2026-10-25T12:01:00+00:00",
    ...overrides,
  };
}

function turn(overrides: Partial<StoredTurn> = {}): StoredTurn {
  return {
    request_id: "turn-1",
    seq: 1,
    kind: "dynamic",
    query: "我要做一道类似题",
    status: "completed",
    result: null,
    tutoring: null,
    exercise: null,
    error: null,
    started_at: "2026-10-25T12:00:00+00:00",
    finished_at: "2026-10-25T12:00:01+00:00",
    ...overrides,
  };
}

const noop = () => undefined;

function renderTurn(props: Partial<ComponentProps<typeof ConversationTurn>> = {}) {
  return render(
    <ConversationTurn
      turn={turn()}
      onDownload={noop}
      onRetry={noop}
      onFeedback={noop}
      onRecord={noop}
      onCorrect={noop}
      onSupplement={noop}
      onNew={noop}
      onIntentChoice={noop}
      onOpenRecord={noop}
      busy={false}
      {...props}
    />,
  );
}

describe("会话轮次的展示修正", () => {
  test("内部动作名转成中文，重复的动作消息只出现一次", () => {
    const { container } = renderTurn({
      turn: turn({
        assistant_message: "已生成针对性练习题（待作答）",
        composite_results: [
          {
            action: "generate_exercise",
            status: "success",
            message: "已生成针对性练习题（待作答）",
          },
          {
            action: "record_mistake",
            status: "success",
            message: "已保存到错题本。",
          },
        ],
      }),
    });

    expect(screen.getAllByText("已生成针对性练习题（待作答）")).toHaveLength(1);
    expect(screen.getByText("生成练习")).toBeTruthy();
    expect(screen.getByText("已保存到错题本。")).toBeTruthy();
    expect(screen.getByText("保存到错题本")).toBeTruthy();
    expect(container.textContent).not.toContain("generate_exercise");
    expect(container.textContent).not.toContain("record_mistake");
  });

  test("查询结果与自然语言回复完全相同时不重复渲染，且不暴露试卷 ID", () => {
    const { container } = renderTurn({
      turn: turn({
        kind: "query",
        query: "找高数期末试卷",
        assistant_message: "找到 1 份匹配的高数试卷。",
        result: {
          type: "result",
          kind: "found",
          message: "找到 1 份匹配的高数试卷。",
          filters: null,
          total: 1,
          items: [
            {
              id: "paper-internal-id",
              title: "高等数学（上）期末试卷",
              course: "高等数学",
              academic_year: "2024-2025",
              exam_type: "final",
              exam_date: null,
              download_url: "/api/papers/paper-internal-id/download",
            },
          ],
        },
      }),
    });

    expect(screen.getAllByText("找到 1 份匹配的高数试卷。")).toHaveLength(1);
    expect(screen.getByText("查询结果")).toBeTruthy();
    expect(screen.getByText("高等数学（上）期末试卷")).toBeTruthy();
    // 目录没有登记日期时如实说明
    expect(screen.getByText("日期未登记")).toBeTruthy();
    expect(container.textContent).not.toContain("paper-internal-id");
  });

  test("失败原因直接可见，错误码收进诊断信息", () => {
    renderTurn({
      turn: turn({
        status: "failed",
        assistant_message: null,
        error: { code: "MODEL_TIMEOUT", message: "模型响应超时，请重试。" },
      }),
    });

    expect(screen.getByText("模型响应超时，请重试。")).toBeTruthy();
    expect(screen.getByText("诊断信息")).toBeTruthy();
    const code = screen.getByText("错误码：MODEL_TIMEOUT");
    expect(code.closest("details")).not.toBeNull();
  });

  test("动作结果整段被叙事包含时只保留叙事，仍标明未完成", () => {
    renderTurn({
      turn: turn({
        assistant_message:
          "抱歉，以下操作没有完成：这道练习题已经完成评价；如需继续练习，请生成新的类似题。。依赖未成功步骤的后续动作已跳过。",
        composite_results: [
          {
            action: "evaluate_exercise",
            status: "failed",
            message: "这道练习题已经完成评价；如需继续练习，请生成新的类似题。",
          },
        ],
      }),
    });

    expect(
      screen.getAllByText(/这道练习题已经完成评价/).length,
    ).toBe(1);
    expect(screen.getAllByText("未完成").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("评价作答")).toBeTruthy();
  });

  test("只带练习上下文的追问轮次不重复展示题目卡片提示", () => {
    renderTurn({
      turn: turn({
        kind: "followup",
        exercise: exercise({ status: "submitted", evaluation: evaluation() }),
      }),
      exerciseRole: "reference",
    });

    expect(screen.queryByText(/都在上方题目卡片中/)).toBeNull();
    expect(screen.queryByText("AI 生成类似题")).toBeNull();
  });

  test("已评价的练习不再展示提交答案/看答案建议，旧轮次也没有建议", () => {
    const submitted = exercise({
      status: "submitted",
      evaluation: evaluation(),
      attempts: [attempt()],
    });
    const suggestions = [
      { label: "提交我的答案", action: "提交我的答案", target_id: "exercise-1" },
      { label: "直接看答案", action: "直接看答案", target_id: "exercise-1" },
      { label: "保存到错题本", action: "保存到错题本", target_id: "problem-1" },
    ];

    const { rerender } = renderTurn({
      turn: turn({ exercise: submitted, suggestions }),
    });
    expect(screen.queryByRole("button", { name: "提交我的答案" })).toBeNull();
    expect(screen.queryByRole("button", { name: "直接看答案" })).toBeNull();

    rerender(
      <ConversationTurn
        turn={turn({ exercise: exercise(), suggestions })}
        onDownload={noop}
        onRetry={noop}
        onFeedback={noop}
        onRecord={noop}
        onCorrect={noop}
        onSupplement={noop}
        onNew={noop}
        onIntentChoice={noop}
        onOpenRecord={noop}
        isLatestTurn={false}
        busy={false}
      />,
    );
    expect(screen.queryByRole("button", { name: "提交我的答案" })).toBeNull();

    rerender(
      <ConversationTurn
        turn={turn({ exercise: exercise(), suggestions })}
        onDownload={noop}
        onRetry={noop}
        onFeedback={noop}
        onRecord={noop}
        onCorrect={noop}
        onSupplement={noop}
        onNew={noop}
        onIntentChoice={noop}
        onOpenRecord={noop}
        isLatestTurn
        busy={false}
      />,
    );
    expect(screen.getByRole("button", { name: "提交我的答案" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "直接看答案" })).toBeTruthy();
  });

  test("建议按钮绑定到本轮对象：看解答和提交答案不再按文字重发", () => {
    const onSuggestionClick = vi.fn();
    renderTurn({
      turn: turn({
        exercise: exercise(),
        suggestions: [{ label: "提交我的答案", action: "提交我的答案", target_id: null }],
      }),
      onSuggestionClick,
    });

    fireEvent.click(screen.getByRole("button", { name: "提交我的答案" }));
    expect(onSuggestionClick).toHaveBeenCalledWith(
      expect.objectContaining({ request_id: "turn-1" }),
      expect.objectContaining({ label: "提交我的答案" }),
    );
  });

  test("作答回执只展示本次提交和评价，并能回到题目订正", () => {
    const onJump = vi.fn();
    const onCorrect = vi.fn();
    render(
      <ExerciseAttemptReceipt
        exercise={exercise({ status: "submitted" })}
        attempt={attempt()}
        onJump={onJump}
        onCorrect={onCorrect}
      />,
    );

    expect(screen.getByText("第 1 次作答 · 首次作答")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("答案正确。")).toBeTruthy();
    // 回执不放作答表单，避免和题目主卡重复
    expect(screen.queryByLabelText("最终答案")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "查看题目主卡" }));
    expect(onJump).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "回到题目并订正" }));
    expect(onCorrect).toHaveBeenCalledTimes(1);
  });
});