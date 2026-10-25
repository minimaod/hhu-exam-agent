import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import ConversationWorkspace from "./ConversationWorkspace";
import type {
  ConversationDetail,
  ConversationSummary,
  ConversationTurn,
  ExerciseAttemptView,
  ExerciseEvaluation,
  ExerciseView,
} from "../lib/conversationsApi";

const { listConversations, getConversation, streamTurn, revealExerciseSolution } =
  vi.hoisted(() => ({
    listConversations: vi.fn(),
    getConversation: vi.fn(),
    streamTurn: vi.fn(),
    revealExerciseSolution: vi.fn(),
  }));

vi.mock("../lib/conversationsApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/conversationsApi")>();
  return {
    ...actual,
    listConversations,
    getConversation,
    streamTurn,
    revealExerciseSolution,
    createRequestId: () => "11111111-1111-4111-8111-111111111111",
  };
});

const A: ConversationSummary = {
  id: "conversation-a",
  title: "会话 A",
  created_at: "2026-10-24T12:00:00+00:00",
  updated_at: "2026-10-24T12:00:00+00:00",
};

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

function exercise(overrides: Partial<ExerciseView> = {}): ExerciseView {
  return {
    id: "exercise-1",
    conversation_id: A.id,
    problem_id: "problem-1",
    root_problem_id: "problem-1",
    statement_text: "计算极限 lim x→0 sin(3x)/x",
    knowledge_point: "重要极限",
    status: "submitted",
    student_answer_text: "3",
    student_solution_text: "使用重要极限。",
    evaluation: evaluation(),
    answered_after_revealed: false,
    attachments: [],
    attempts: [attempt()],
    created_at: "2026-10-25T12:00:00+00:00",
    updated_at: "2026-10-25T12:01:00+00:00",
    ...overrides,
  };
}

function turn(overrides: Partial<ConversationTurn>): ConversationTurn {
  return {
    request_id: "turn-1",
    seq: 1,
    kind: "dynamic",
    query: "",
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

describe("练习展示：一张题目主卡 + 每次作答回执", () => {
  test("生成轮保留主卡，作答轮只显示本次回执，订正回到主卡", async () => {
    const shared = exercise();
    const detail: ConversationDetail = {
      conversation: A,
      active_problem: null,
      record: null,
      turns: [
        turn({
          request_id: "turn-generate",
          seq: 1,
          assistant_message: "已生成针对性练习题（待作答）",
          exercise: shared,
          suggestions: [
            { label: "提交我的答案", action: "提交我的答案", target_id: null },
            { label: "直接看答案", action: "直接看答案", target_id: null },
          ],
        }),
        turn({
          request_id: "turn-answer",
          seq: 2,
          kind: "exercise",
          query: "",
          assistant_message: "评价完成。",
          exercise: shared,
        }),
        turn({
          request_id: "turn-retry",
          seq: 3,
          kind: "exercise",
          query: "",
          assistant_message: "已重试评价。",
          exercise: shared,
        }),
      ],
    };

    listConversations.mockResolvedValue({ items: [A], total: 1 });
    getConversation.mockResolvedValue(detail);
    streamTurn.mockResolvedValue({
      type: "result",
      conversation_id: A.id,
      request_id: "11111111-1111-4111-8111-111111111111",
      kind: "exercise",
      result: null,
      tutoring: null,
    });

    render(
      <ConversationWorkspace onOpenRecord={vi.fn()} onDownload={vi.fn()} downloads={{}} />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /会话 A/ }));

    // 只有生成轮展开完整题目卡片
    await screen.findByText("AI 生成类似题");
    expect(screen.getAllByText("AI 生成类似题")).toHaveLength(1);
    expect(screen.getByText("计算极限 lim x→0 sin(3x)/x")).toBeTruthy();
    // 主卡明确告诉学生这次作答和评价在第几轮
    expect(
      screen.getByText(/每次作答与评价按提交顺序显示在各自轮次（第 2 轮）/),
    ).toBeTruthy();

    // 作答轮是紧凑回执：本次答案和评价只出现一次
    expect(screen.getByText("第 1 次作答 · 首次作答")).toBeTruthy();
    expect(screen.getAllByText("答案正确。")).toHaveLength(1);
    expect(screen.getAllByText("（提交练习作答）")).toHaveLength(2);

    // 已经评价过的练习，不再出现失效建议
    expect(screen.queryByText("提交我的答案")).toBeNull();
    expect(screen.queryByText("直接看答案")).toBeNull();

    // 无法对应到具体作答的轮次给出定位入口，不重复大卡片
    expect(
      screen.getByText(/这道练习的题目在上方主卡；本次作答与评价保留在卡片的最新记录中/),
    ).toBeTruthy();

    // 从回执直接进入主卡订正
    fireEvent.click(screen.getByRole("button", { name: "回到题目并订正" }));
    const answerInput = (await screen.findByLabelText("最终答案")) as HTMLInputElement;
    expect(answerInput.value).toBe("");
    await waitFor(() => expect(document.activeElement).toBe(answerInput));

    fireEvent.change(answerInput, { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText("解题步骤 / 推导思路（选填）"), {
      target: { value: "重新推导。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交订正并重新评价" }));

    await waitFor(() => {
      expect(streamTurn).toHaveBeenCalledWith(
        expect.objectContaining({
          conversationId: A.id,
          query: "5",
          exerciseId: "exercise-1",
          studentAnswer: "5",
          solutionText: "重新推导。",
          attemptKind: "correction",
        }),
      );
    });
  });

  test("多次作答各自绑定自己的回执，不串到同一张卡片", async () => {
    const first = attempt();
    const second = attempt({
      id: "attempt-2",
      attempt_no: 2,
      kind: "correction",
      student_answer_text: "5",
      student_solution_text: "重新推导。",
      answered_after_revealed: true,
      evaluation: evaluation({ final_answer_assessment: "订正后答案正确。" }),
      request_id: "turn-correction",
    });
    const shared = exercise({
      attempts: [first, second],
      student_answer_text: "5",
      student_solution_text: "重新推导。",
      evaluation: second.evaluation,
    });
    const detail: ConversationDetail = {
      conversation: A,
      active_problem: null,
      record: null,
      turns: [
        turn({
          request_id: "turn-generate",
          seq: 1,
          assistant_message: "已生成针对性练习题（待作答）",
          exercise: shared,
        }),
        turn({
          request_id: "turn-answer",
          seq: 2,
          kind: "exercise",
          query: "",
          exercise: shared,
        }),
        turn({
          request_id: "turn-correction",
          seq: 3,
          kind: "exercise",
          query: "",
          exercise: shared,
        }),
      ],
    };

    listConversations.mockResolvedValue({ items: [A], total: 1 });
    getConversation.mockResolvedValue(detail);

    render(
      <ConversationWorkspace onOpenRecord={vi.fn()} onDownload={vi.fn()} downloads={{}} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /会话 A/ }));

    // 两次作答各有一张回执，内容不交叉
    const firstReceipt = (await screen.findByText("第 1 次作答 · 首次作答")).closest(
      "div.rounded-2xl",
    ) as HTMLElement;
    expect(firstReceipt.textContent).toContain("最终答案：3");
    expect(firstReceipt.textContent).toContain("答案正确。");

    const secondReceipt = screen
      .getByText("第 2 次作答 · 订正")
      .closest("div.rounded-2xl") as HTMLElement;
    expect(secondReceipt.textContent).toContain("最终答案：5");
    expect(secondReceipt.textContent).toContain("订正后答案正确。");
    expect(secondReceipt.textContent).toContain("已看解答后作答");

    // 主卡不再重复两次作答内容
    const card = document.querySelector("[id^=exercise-card-]") as HTMLElement;
    expect(card.textContent).not.toContain("最终答案：");
    expect(card.textContent).toContain("（第 2、3 轮）");

    // 第二张回执进入订正，表单拿到的是新一次的空输入
    fireEvent.click(screen.getAllByRole("button", { name: "回到题目并订正" })[1]);
    const answerInput = (await screen.findByLabelText("最终答案")) as HTMLInputElement;
    expect(answerInput.value).toBe("");
  });

  test("首轮既生成又作答时，唯一一次作答留在主卡而不是被藏掉", async () => {
    const shared = exercise();
    const detail: ConversationDetail = {
      conversation: A,
      active_problem: null,
      record: null,
      turns: [
        turn({
          request_id: "turn-answer",
          seq: 1,
          kind: "dynamic",
          assistant_message: "已生成并完成评价。",
          exercise: shared,
        }),
      ],
    };

    listConversations.mockResolvedValue({ items: [A], total: 1 });
    getConversation.mockResolvedValue(detail);

    render(
      <ConversationWorkspace onOpenRecord={vi.fn()} onDownload={vi.fn()} downloads={{}} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /会话 A/ }));

    const card = (await screen.findByText("AI 生成类似题")).closest(
      "div.rounded-2xl",
    ) as HTMLElement;
    expect(card.textContent).toContain("最终答案：");
    expect(card.textContent).toContain("作答评价");
    expect(screen.queryByText(/都在上方题目卡片中/)).toBeNull();
  });
});