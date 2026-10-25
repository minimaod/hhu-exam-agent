import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import ConversationWorkspace from "../components/ConversationWorkspace";
import type {
  ConversationDetail,
  ConversationSummary,
  ConversationTurn,
  ProblemView,
  TurnResultEvent,
  TutoringResult,
} from "../lib/conversationsApi";

const { listConversations, getConversation, streamTurn, revealExerciseSolution } = vi.hoisted(() => ({
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

const B: ConversationSummary = {
  id: "conversation-b",
  title: "会话 B",
  created_at: "2026-10-24T12:00:00+00:00",
  updated_at: "2026-10-24T12:00:00+00:00",
};

function detail(conversation: ConversationSummary): ConversationDetail {
  return {
    conversation,
    turns: [],
    active_problem: null,
    record: null,
  };
}

function problem(id: string, statement: string): ProblemView {
  return {
    id,
    root_problem_id: id,
    version: 1,
    statement_text: statement,
    status: "explained",
    clarity: "clear",
    source: { type: "none", text: null, paper_id: null, paper_title: null },
    attachments: [],
    student_answer_text: null,
    solution_text: null,
    self_reported_understood: false,
    created_at: "2026-10-24T12:00:00+00:00",
    updated_at: "2026-10-24T12:00:00+00:00",
  };
}

function storedTurn(
  requestId: string,
  seq: number,
  item: ProblemView,
): ConversationTurn {
  const tutoring: TutoringResult = {
    kind: "explanation",
    problem: item,
    knowledge_points: [{ name: "知识点", explanation: "说明" }],
    steps: [{ index: 1, title: "步骤", content: "内容" }],
    final_answer: "1",
    error_analysis: null,
    uncertainties: [],
    self_reported_understood: false,
    record_id: null,
  };
  return {
    request_id: requestId,
    seq,
    kind: "problem",
    query: item.statement_text,
    status: "completed",
    result: null,
    tutoring,
    error: null,
    started_at: "2026-10-24T12:00:00+00:00",
    finished_at: "2026-10-24T12:00:01+00:00",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("ConversationWorkspace 异步隔离", () => {
  test("历史题卡片操作携带被点击题目的 ID", async () => {
    const problemA = problem("problem-a", "历史题 A");
    const problemB = problem("problem-b", "当前题 B");
    const detailWithTurns: ConversationDetail = {
      conversation: A,
      turns: [
        storedTurn("request-a", 1, problemA),
        storedTurn("request-b", 2, problemB),
      ],
      active_problem: problemB,
      record: null,
    };
    listConversations.mockResolvedValue({ items: [A], total: 1 });
    getConversation.mockResolvedValue(detailWithTurns);
    streamTurn.mockReturnValue(new Promise<TurnResultEvent>(() => undefined));

    render(
      <ConversationWorkspace
        downloads={{}}
        onDownload={() => undefined}
        onOpenRecord={() => undefined}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /会话 A/ }));
    const recordButtons = await screen.findAllByRole("button", { name: "记录这题" });
    expect(recordButtons.length).toBe(2);
    fireEvent.click(recordButtons[0]);

    await waitFor(() => expect(streamTurn).toHaveBeenCalledTimes(1));
    expect(streamTurn.mock.calls[0][0].problemId).toBe("problem-a");
  });

  test("纠正历史题时携带被点击题目的 ID", async () => {
    const problemA = problem("problem-a", "历史题 A");
    const problemB = problem("problem-b", "当前题 B");
    const detailWithTurns: ConversationDetail = {
      conversation: A,
      turns: [
        storedTurn("request-a", 1, problemA),
        storedTurn("request-b", 2, problemB),
      ],
      active_problem: problemB,
      record: null,
    };
    listConversations.mockResolvedValue({ items: [A], total: 1 });
    getConversation.mockResolvedValue(detailWithTurns);
    streamTurn.mockReturnValue(new Promise<TurnResultEvent>(() => undefined));

    render(
      <ConversationWorkspace
        downloads={{}}
        onDownload={() => undefined}
        onOpenRecord={() => undefined}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /会话 A/ }));
    const correctButtons = await screen.findAllByRole("button", {
      name: "纠正题干",
    });
    expect(correctButtons.length).toBe(2);
    fireEvent.click(correctButtons[0]);
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(streamTurn).toHaveBeenCalledTimes(1));
    const call = streamTurn.mock.calls[0][0];
    expect(call.kind).toBe("correction");
    expect(call.problemId).toBe("problem-a");
  });

  test("切换会话后，A 的迟到结果不会写入 B", async () => {
    listConversations.mockResolvedValue({ items: [A, B], total: 2 });
    getConversation.mockImplementation((id: string) =>
      Promise.resolve(detail(id === A.id ? A : B)),
    );
    const pending = deferred<TurnResultEvent>();
    streamTurn.mockReturnValue(pending.promise);

    render(
      <ConversationWorkspace
        downloads={{}}
        onDownload={() => undefined}
        onOpenRecord={() => undefined}
      />,
    );

    const aButton = await screen.findByRole("button", { name: /会话 A/ });
    fireEvent.click(aButton);
    const textarea = await screen.findByPlaceholderText(
      /输入完整题干|围绕当前题目/,
    );
    const messageScroll = screen.getByTestId("message-scroll");
    const composer = screen.getByTestId("composer");
    expect(messageScroll.contains(composer)).toBe(false);
    fireEvent.change(textarea, { target: { value: "求极限" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(streamTurn).toHaveBeenCalledTimes(1));

    const bButton = screen.getByRole("button", { name: /会话 B/ });
    fireEvent.click(bButton);
    await screen.findByRole("heading", { name: "会话 B" });
    const bTextarea = screen.getByPlaceholderText(
      /输入完整题干|围绕当前题目/,
    ) as HTMLTextAreaElement;
    fireEvent.change(bTextarea, { target: { value: "C 的草稿" } });

    await act(async () => {
      pending.resolve({
        type: "result",
        conversation_id: A.id,
        request_id: "11111111-1111-4111-8111-111111111111",
        kind: "problem",
        result: null,
        tutoring: {
          kind: "intent_clarification",
          message: "A 的迟到结果",
          options: ["query"],
        },
      });
      await Promise.resolve();
    });

    expect(screen.queryByText("A 的迟到结果")).toBeNull();
    expect(screen.getByRole("heading", { name: "会话 B" })).toBeTruthy();
    expect(
      (screen.getByPlaceholderText(/输入完整题干|围绕当前题目/) as HTMLTextAreaElement)
        .value,
    ).toBe("C 的草稿");
  });

  test("切换后揭晓解答的迟到错误不会污染另一会话", async () => {
    const exercise = {
      id: "ex-late-reveal", conversation_id: A.id, problem_id: "p", root_problem_id: "p",
      statement_text: "待揭晓练习", knowledge_point: "极限", status: "pending_answer" as const,
      answered_after_revealed: false, attachments: [], created_at: A.created_at, updated_at: A.updated_at,
    };
    listConversations.mockResolvedValue({ items: [A, B], total: 2 });
    getConversation.mockImplementation((id: string) => Promise.resolve(id === A.id ? {
      ...detail(A), turns: [{ request_id: "reveal-origin", seq: 1, kind: "dynamic", query: "练习", status: "completed",
        result: null, tutoring: null, exercise, error: null, started_at: A.created_at, finished_at: A.updated_at }],
    } : detail(B)));
    let rejectReveal!: (reason: Error) => void;
    revealExerciseSolution.mockReturnValue(new Promise((_, reject) => { rejectReveal = reject; }));
    render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /会话 A/ }));
    fireEvent.click(await screen.findByRole("button", { name: "不会做？查看参考解答" }));
    fireEvent.click(screen.getByRole("button", { name: /会话 B/ }));
    await screen.findByRole("heading", { name: "会话 B" });
    fireEvent.change(screen.getByPlaceholderText(/输入完整题干|围绕当前题目/), { target: { value: "B 的新草稿" } });
    await act(async () => { rejectReveal(new Error("A 的迟到揭晓错误")); });
    expect(screen.queryByText("A 的迟到揭晓错误")).toBeNull();
    expect((screen.getByPlaceholderText(/输入完整题干|围绕当前题目/) as HTMLTextAreaElement).value).toBe("B 的新草稿");
  });

  test("动态轮次渲染 AI 类似题卡片并支持建议操作点击", async () => {
    const dynamicTurn: ConversationTurn = {
      request_id: "dyn-turn-1",
      seq: 1,
      kind: "dynamic",
      query: "我要做一道类似题",
      status: "completed",
      result: null,
      tutoring: null,
      assistant_message: "已为您生成针对性练习：",
      exercise: {
        id: "ex-dom-1",
        conversation_id: A.id,
        problem_id: "prob-1",
        root_problem_id: "prob-1",
        statement_text: "计算极限 lim x->0 sin(3x)/x",
        knowledge_point: "重要极限",
        status: "pending_answer",
        answered_after_revealed: false,
        attachments: [],
        created_at: "2026-10-25T12:00:00+00:00",
        updated_at: "2026-10-25T12:00:00+00:00",
      },
      suggestions: [
        { label: "查看解答", action: "查看解答", target_id: "ex-dom-1" },
        { label: "换一道题", action: "换一道题", target_id: "prob-1" },
      ],
      error: null,
      started_at: "2026-10-25T12:00:00+00:00",
      finished_at: "2026-10-25T12:00:01+00:00",
    };

    const conversationWithExercise: ConversationDetail = {
      conversation: A,
      turns: [dynamicTurn],
      active_problem: null,
      record: null,
    };

    listConversations.mockResolvedValue({ items: [A], total: 1 });
    getConversation.mockResolvedValue(conversationWithExercise);
    streamTurn.mockResolvedValue({
      type: "result",
      conversation_id: A.id,
      request_id: "11111111-1111-4111-8111-111111111111",
      kind: "dynamic",
      result: null,
      tutoring: null,
    });

    render(
      <ConversationWorkspace
        onOpenRecord={vi.fn()}
        onDownload={vi.fn()}
        downloads={{}}
      />,
    );

    const aButton = await screen.findByRole("button", { name: /会话 A/ });
    fireEvent.click(aButton);

    // 验证练习题及建议正确渲染
    await screen.findByText("AI 生成类似题");
    expect(screen.getByText("计算极限 lim x->0 sin(3x)/x")).toBeTruthy();
    expect(screen.getByText("考点：重要极限")).toBeTruthy();

    const sugButton = screen.getByRole("button", { name: "查看解答" });
    expect(sugButton).toBeTruthy();

    // “查看解答”直接绑定到本轮这道练习，不再把内部动作名当自然语言重发
    revealExerciseSolution.mockResolvedValue({
      ...dynamicTurn.exercise!,
      status: "revealed",
      revealed_solution_at: "2026-10-25T12:00:02+00:00",
      reference_solution_text: "用重要极限，结果为 3",
    });
    fireEvent.click(sugButton);
    await waitFor(() => {
      expect(revealExerciseSolution).toHaveBeenCalledWith(A.id, "ex-dom-1");
    });

    // 非练习类建议仍按自然语言发送
    fireEvent.click(screen.getByRole("button", { name: "换一道题" }));
    await waitFor(() => {
      expect(streamTurn).toHaveBeenCalledWith(
        expect.objectContaining({
          conversationId: A.id,
          query: "换一道题",
        }),
      );
    });
  });

  test("练习卡片在揭晓解答后依然展示作答表单并可提交", async () => {
    const dynamicTurn: ConversationTurn = {
      request_id: "dyn-turn-reveal",
      seq: 1,
      kind: "dynamic",
      query: "我要做一道类似题",
      status: "completed",
      result: null,
      tutoring: null,
      assistant_message: "已为您生成针对性练习：",
      exercise: {
        id: "ex-dom-reveal",
        conversation_id: A.id,
        problem_id: "prob-1",
        root_problem_id: "prob-1",
        statement_text: "计算极限 lim x->0 sin(4x)/x",
        knowledge_point: "重要极限",
        status: "pending_answer",
        answered_after_revealed: false,
        attachments: [],
        created_at: "2026-10-25T12:00:00+00:00",
        updated_at: "2026-10-25T12:00:00+00:00",
      },
      suggestions: [],
      error: null,
      started_at: "2026-10-25T12:00:00+00:00",
      finished_at: "2026-10-25T12:00:01+00:00",
    };

    const conversationWithExercise: ConversationDetail = {
      conversation: A,
      turns: [dynamicTurn],
      active_problem: null,
      record: null,
    };

    const revealedExercise = {
      id: "ex-dom-reveal",
      conversation_id: A.id,
      problem_id: "prob-1",
      root_problem_id: "prob-1",
      statement_text: "计算极限 lim x->0 sin(4x)/x",
      knowledge_point: "重要极限",
      reference_solution_text: "令 t=4x，结果为 4",
      status: "revealed" as const,
      revealed_solution_at: "2026-10-25T12:01:00+00:00",
      answered_after_revealed: false,
      attachments: [],
      created_at: "2026-10-25T12:00:00+00:00",
      updated_at: "2026-10-25T12:01:00+00:00",
    };

    const conversationWithExerciseRevealed: ConversationDetail = {
      conversation: A,
      turns: [{ ...dynamicTurn, exercise: revealedExercise }],
      active_problem: null,
      record: null,
    };

    listConversations.mockResolvedValue({ items: [A], total: 1 });
    getConversation.mockResolvedValue(conversationWithExercise);
    revealExerciseSolution.mockImplementation(async () => {
      getConversation.mockResolvedValue(conversationWithExerciseRevealed);
      return revealedExercise;
    });
    streamTurn.mockResolvedValue({
      type: "result",
      conversation_id: A.id,
      request_id: "11111111-1111-4111-8111-111111111111",
      kind: "dynamic",
      result: null,
      tutoring: null,
    });

    render(
      <ConversationWorkspace
        onOpenRecord={vi.fn()}
        onDownload={vi.fn()}
        downloads={{}}
      />,
    );

    const aButton = await screen.findByRole("button", { name: /会话 A/ });
    fireEvent.click(aButton);

    // 点击卡片上的“查看参考解答”
    const revealButton = await screen.findByRole("button", { name: /查看参考解答/ });
    fireEvent.click(revealButton);

    // 验证查看解答接口被调用
    await waitFor(() => {
      expect(revealExerciseSolution).toHaveBeenCalledWith(A.id, "ex-dom-reveal");
    });

    // 验证参考解答已展示，并且提示已看解答后作答
    await screen.findByText("参考解答与推导");
    expect(screen.getByText("令 t=4x，结果为 4")).toBeTruthy();
    expect(screen.getByText(/你已查看参考解答/)).toBeTruthy();

    // 验证作答输入框依然存在，并能输入提交
    const answerInput = screen.getByPlaceholderText(/请输入最终答案/);
    fireEvent.change(answerInput, { target: { value: "4" } });

    const submitAnswerBtn = screen.getByRole("button", { name: /提交作答/ });
    fireEvent.click(submitAnswerBtn);

    await waitFor(() => {
      expect(streamTurn).toHaveBeenCalledWith(
        expect.objectContaining({
          conversationId: A.id,
          exerciseId: "ex-dom-reveal",
          query: "4",
        }),
      );
    });
  });

  test("评价未完成时展示已保存作答和重试提示", async () => {
    const pendingTurn: ConversationTurn = {
      request_id: "dyn-turn-pending-eval",
      seq: 1,
      kind: "exercise",
      query: "1",
      status: "completed",
      result: null,
      tutoring: null,
      assistant_message: "作答已保存，但评价没有完成，请重试。",
      exercise: {
        id: "ex-dom-pending",
        conversation_id: A.id,
        problem_id: "prob-1",
        root_problem_id: "prob-1",
        statement_text: "计算极限 lim x->0 sin(5x)/x",
        knowledge_point: "重要极限",
        status: "pending_answer",
        student_answer_text: "5",
        student_solution_text: null,
        evaluation: null,
        answered_after_revealed: false,
        attachments: [],
        created_at: "2026-10-25T12:00:00+00:00",
        updated_at: "2026-10-25T12:01:00+00:00",
      },
      suggestions: [],
      error: null,
      started_at: "2026-10-25T12:00:00+00:00",
      finished_at: "2026-10-25T12:00:01+00:00",
    };

    listConversations.mockResolvedValue({ items: [A], total: 1 });
    getConversation.mockResolvedValue({
      conversation: A,
      turns: [pendingTurn],
      active_problem: null,
      record: null,
    });

    render(
      <ConversationWorkspace
        onOpenRecord={vi.fn()}
        onDownload={vi.fn()}
        downloads={{}}
      />,
    );

    const aButton = await screen.findByRole("button", { name: /会话 A/ });
    fireEvent.click(aButton);

    await screen.findByText("评价未完成");
    expect(screen.getByText(/作答已经保存，但评价尚未完成/)).toBeTruthy();
    expect(screen.getByText("5")).toBeTruthy();
    expect(screen.getByRole("button", { name: /提交作答/ })).toBeTruthy();
  });

  test("要求补充过程时保留作答表单并允许重新提交", async () => {
    const supplementTurn: ConversationTurn = {
      request_id: "dyn-turn-needs-supplement",
      seq: 1,
      kind: "exercise",
      query: "5",
      status: "completed",
      result: null,
      tutoring: null,
      assistant_message: "已完成作答评价。",
      exercise: {
        id: "ex-dom-supplement",
        conversation_id: A.id,
        problem_id: "prob-1",
        root_problem_id: "prob-1",
        statement_text: "求 y=x^2 的导数",
        knowledge_point: "导数",
        status: "submitted",
        student_answer_text: "5",
        student_solution_text: null,
        evaluation: {
          is_correct: null,
          final_answer_assessment: "只写了答案，过程不足。",
          step_by_step_analysis: "无法判断推导是否掌握。",
          guidance: "请补充求导步骤。",
          needs_supplement: true,
        },
        answered_after_revealed: false,
        attachments: [],
        created_at: "2026-10-25T12:00:00+00:00",
        updated_at: "2026-10-25T12:01:00+00:00",
      },
      suggestions: [],
      error: null,
      started_at: "2026-10-25T12:00:00+00:00",
      finished_at: "2026-10-25T12:00:01+00:00",
    };

    listConversations.mockResolvedValue({ items: [A], total: 1 });
    getConversation.mockResolvedValue({
      conversation: A,
      turns: [supplementTurn],
      active_problem: null,
      record: null,
    });

    render(
      <ConversationWorkspace
        onOpenRecord={vi.fn()}
        onDownload={vi.fn()}
        downloads={{}}
      />,
    );

    const aButton = await screen.findByRole("button", { name: /会话 A/ });
    fireEvent.click(aButton);

    await screen.findByText("待补充过程");
    expect(screen.getByText(/评价认为解题过程还需要补充/)).toBeTruthy();
    const solutionInput = screen.getByLabelText(
      "解题步骤 / 推导思路（选填）",
    ) as HTMLTextAreaElement;
    expect(solutionInput.value).toBe("");
    fireEvent.change(solutionInput, {
      target: { value: "y' = 2x" },
    });
    expect(
      screen.getByRole("button", { name: "补充并重新评价" }),
    ).toBeTruthy();
  });
});

