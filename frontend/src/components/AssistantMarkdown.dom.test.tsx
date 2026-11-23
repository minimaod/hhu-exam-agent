import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import AssistantMarkdown, { extractSafeLead } from "./AssistantMarkdown";
import ConversationTurn from "./ConversationTurn";
import type { ConversationTurn as StoredTurn } from "../lib/conversationsApi";

const noop = () => undefined;

function makeTurn(overrides: Partial<StoredTurn> = {}): StoredTurn {
  return {
    request_id: "turn-md-1",
    seq: 1,
    kind: "dynamic",
    query: "第二步为什么能换元？",
    status: "completed",
    assistant_message: "因为被积函数在区间 $[0, 1]$ 上连续。",
    result: null,
    tutoring: null,
    exercise: null,
    error: null,
    started_at: "2026-11-15T12:00:00+00:00",
    finished_at: "2026-11-15T12:00:01+00:00",
    ...overrides,
  };
}

describe("AssistantMarkdown 与展示去重", () => {
  test("安全渲染列表、加粗、表格、代码块与 LaTeX 公式，且防止 XSS", () => {
    const md = [
      "### 核心步骤",
      "",
      "**条件检查**：确保 $f(x) = a * b + c_1 | d_2$ 连续。",
      "",
      "1. 第一步：设 $u = x^2$",
      "2. 第二步：换限得到",
      "",
      "$$\\int_0^1 u \\, du = \\frac{1}{2}$$",
      "",
      "| 方法 | 条件 | 说明 |",
      "| :--- | :--- | :--- |",
      "| 换元 | 单调可微 | 同步改上下限 |",
      "",
      "```python",
      "def f(x):",
      "    return x * x",
      "```",
      "",
      "<script>alert('xss')</script>",
    ].join("\n");

    const { container } = render(<AssistantMarkdown text={md} />);
    expect(container.querySelector("h5")?.textContent).toBe("核心步骤");
    expect(container.querySelector("strong")?.textContent).toBe("条件检查");
    expect(container.querySelectorAll("ol > li").length).toBe(2);
    expect(container.querySelector("table.assistant-table")).toBeTruthy();
    expect(container.querySelector("pre.assistant-code-block")?.textContent).toContain("def f(x):");
    expect(container.querySelectorAll(".katex").length).toBeGreaterThanOrEqual(3);
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>alert('xss')</script>");
  });

  test("流式输出未闭合代码块与公式时不报错", () => {
    const partial = "正在推导：\n```python\nprint(1)\n半截公式 $x^2";
    const { container } = render(<AssistantMarkdown text={partial} />);
    expect(container.querySelector("pre.assistant-code-block")?.textContent).toContain("print(1)");
    expect(container.textContent).toContain("半截公式 $x^2");
  });

  test("extractSafeLead 不会在公式内部标点处截断", () => {
    const text = "答案正确，这里满足 $a.b + c$。下一步请继续练习。";
    expect(extractSafeLead(text, 80)).toBe("答案正确，这里满足 $a.b + c$。");

    const insideMathOnly = "满足 $$x + 1。$$" + "说明".repeat(30);
    expect(extractSafeLead(insideMathOnly, 25)).toBeNull();
  });

  test("followup_answer 有正文时不重复渲染追问卡片，旧记录缺正文时兜底展示", () => {
    const withProse = makeTurn({
      assistant_message: "因为 $u = \\sin x$ 在该区间单调可微。",
      tutoring: {
        kind: "followup_answer",
        problem_id: "prob-1",
        answer: "因为 $u = \\sin x$ 在该区间单调可微。",
        related_steps: [2],
      },
    });
    const { rerender } = render(
      <ConversationTurn
        turn={withProse}
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
      />,
    );
    expect(screen.queryByText("追问回答")).toBeNull();

    const legacyWithoutProse = makeTurn({
      assistant_message: "",
      tutoring: {
        kind: "followup_answer",
        problem_id: "prob-1",
        answer: "旧历史兜底回答。",
        related_steps: [1],
      },
    });
    rerender(
      <ConversationTurn
        turn={legacyWithoutProse}
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
      />,
    );
    expect(screen.getByText("追问回答")).toBeTruthy();
    expect(screen.getByText("旧历史兜底回答。")).toBeTruthy();
  });
});
