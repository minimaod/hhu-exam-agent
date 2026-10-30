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
  QuestionCard,
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

function questionCard(overrides: Partial<QuestionCard> = {}): QuestionCard {
  return {
    question_id: "q-choice-1", build_id: "iv-0123456789abcdef", question_type: "choice",
    number_label: "1", statement: "原卷 Python 特点题", content_sha256: "a".repeat(64),
    options: [{ label: "A", text: "支持中文" }, { label: "B", text: "执行高效" }],
    code_lines: [], blank_marks: [], formulas: [], assets: [], knowledge_points: [],
    pages: { statement: [3], answer: [11] }, answer: { state: "original", available: true },
    review: { status: "approved", approved: true, stale: false },
    source: { paper_id: "paper", question_number: "1", may_show_answer: true }, content_issues: [],
    ...overrides,
  };
}

describe("会话轮次的展示修正", () => {
  test("后续轮次只显示同版本题目引用，保留已查看事实和回主卡动作", () => {
    const question = questionCard({ selected: true, answer_revealed: true,
      answer_detail: { state: "original", text: "ANSWER-ONCE" } });
    const key = `${question.question_id}:${question.build_id}:${question.content_sha256}`;
    const jump = vi.fn();
    renderTurn({ turn: turn({ request_id: "followup", question }),
      questionOwners: new Map([[key, "owner"]]), questionSnapshots: new Map([[key, question]]),
      onJumpToQuestion: jump });
    expect(screen.queryByText(question.statement)).toBeNull();
    expect(screen.queryByText("ANSWER-ONCE")).toBeNull();
    expect(screen.getByText("答案已查看")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看题目主卡" }));
    expect(jump).toHaveBeenCalledWith(key);
  });

  test("跨轮主卡合并最新答案状态时仍保留原卡动作来源", () => {
    const original = questionCard({ selected: true, source_request_id: "original-search" });
    const key = `${original.question_id}:${original.build_id}:${original.content_sha256}`;
    const onQuestionAction = vi.fn();
    renderTurn({ turn: turn({ request_id: "owner", question: original }),
      questionOwners: new Map([[key, "owner"]]),
      questionSnapshots: new Map([[key, { ...original, source_request_id: "later-search",
        answer_revealed: true, answer_detail: { state: "original", text: "A" } }]]), onQuestionAction });
    fireEvent.click(screen.getByRole("button", { name: "保存到错题本" }));
    expect(onQuestionAction.mock.calls[0][1].source_request_id).toBe("original-search");
    expect(screen.getByText("参考答案（已查看，点击展开）")).toBeTruthy();
  });
  test("检索结果和自动选中同一版本时只显示一张，并可在这张卡提交答案", () => {
    const candidate = questionCard();
    const selected = questionCard({ selected: true });
    const onQuestionAction = vi.fn();
    renderTurn({ turn: turn({ question_results: [candidate], question: selected,
      question_result_status: "ok" }), onQuestionAction });
    expect(screen.getAllByText("原卷 Python 特点题")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "选第 1 道题" })).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "B. 执行高效" }));
    fireEvent.click(screen.getByRole("button", { name: "提交并讲解" }));
    expect(onQuestionAction).toHaveBeenCalledWith("answer", selected, "B");
  });

  test("同号不同版本不会被误合并", () => {
    renderTurn({ turn: turn({ question_results: [questionCard()],
      question: questionCard({ build_id: "iv-fedcba9876543210", statement: "另一版本题面" }) }) });
    expect(screen.getByText("原卷 Python 特点题")).toBeTruthy();
    expect(screen.getByText("另一版本题面")).toBeTruthy();
  });
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

describe("真题讲解正文与讲解卡片不重复展开", () => {
  function explanationTurn(message: string) {
    const problem = {
      id: "problem-1",
      root_problem_id: "root-1",
      version: 1,
      status: "explained",
      clarity: "clear",
      statement_text: "原卷 Python 特点题",
      student_answer_text: null,
      solution_text: null,
      attachments: [],
      source: { type: "none" as const },
      self_reported_understood: false,
      created_at: "2026-10-25T12:00:00+00:00",
      updated_at: "2026-10-25T12:00:00+00:00",
    };
    return turn({
      kind: "explain_question",
      assistant_message: message,
      tutoring: {
        kind: "explanation" as const,
        problem,
        knowledge_points: [{ name: "列表推导", explanation: "先循环再收集" }],
        steps: [{ index: 1, title: "识别形式", content: "这是列表推导式的固定写法" }],
        final_answer: "B",
        error_analysis: null,
        uncertainties: [],
        self_reported_understood: false,
        record_id: null,
      },
    } as Partial<StoredTurn>);
  }

  test("正文已经讲完时，讲解卡片默认收起而不是再展开一遍", () => {
    const body =
      "这道题考的是列表推导式，先看循环范围，再看条件过滤，最后才是取值表达式。" +
      "第一步把 for 的范围写对，第二步补上 if 条件，第三步把结果放进方括号里。" +
      "注意不要在方括号里写 return，也不要把结果拼成字符串。" +
      "最后确认一下变量名，题目里给的变量叫 nums 和 limit，不要自己另起名字，" +
      "也不要漏掉 if 后面的比较符号，很多同学会在这里丢分。";
    renderTurn({ turn: explanationTurn(body) });
    expect(screen.getByText(/正文已经完整讲解，需要时展开讲解卡片/)).toBeTruthy();
    // 卡片里的步骤默认不展开，避免和正文重复。
    expect(screen.queryByText("解题步骤")).toBeNull();
  });

  test("正文很短时仍然直接展示讲解卡片", () => {
    renderTurn({ turn: explanationTurn("已讲解。") });
    expect(screen.getByText("解题步骤")).toBeTruthy();
    expect(screen.queryByText(/正文已经完整讲解/)).toBeNull();
  });
});
