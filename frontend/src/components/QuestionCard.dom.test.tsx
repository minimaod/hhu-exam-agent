import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import QuestionCardView, { QuestionResultList } from "./QuestionCard";
import type { QuestionCard } from "../lib/conversationsApi";

function card(overrides: Partial<QuestionCard> = {}): QuestionCard {
  return {
    question_id: "py-2024-2025-s1-final-choice-1",
    build_id: "iv-0123456789abcdef",
    question_type: "choice",
    number_label: "1",
    statement: "下列 Python 表达式的值是？",
    content_sha256: "a".repeat(64),
    options: [
      { label: "A", text: "1" },
      { label: "B", text: "2" },
    ],
    code_lines: [],
    blank_marks: [],
    formulas: [],
    assets: [],
    pages: { statement: [1], answer: [2] },
    knowledge_points: [],
    answer: { state: "original", available: true },
    review: { status: "approved", approved: true, stale: false },
    source: {
      paper_id: "py-2024-2025-s1-final",
      paper_title: "2024-2025 学年第一学期 Python 期末试卷",
      exam_type: "final",
      academic_year: "2024-2025",
      question_number: "1",
      may_show_answer: true,
    },
    content_issues: [],
    selected: true,
    answer_revealed: false,
    answer_detail: null,
    ...overrides,
  };
}

describe("QuestionCardView", () => {
  test("已查看的参考答案默认折叠，候选卡不会重复展示答案正文", () => {
    const revealed = card({ answer_revealed: true, answer_detail: { state: "original", text: "ANSWER-BODY-ONCE" } });
    const { rerender } = render(<QuestionCardView card={revealed} />);
    const summary = screen.getByText("参考答案（已查看，点击展开）");
    expect((summary.closest("details") as HTMLDetailsElement).open).toBe(false);
    expect(screen.getByText("ANSWER-BODY-ONCE")).toBeTruthy();
    rerender(<QuestionCardView card={revealed} compact />);
    expect(screen.queryByText("ANSWER-BODY-ONCE")).toBeNull();
    expect(screen.queryByText("参考答案（已查看，点击展开）")).toBeNull();
  });
  test("点击选项后提交绑定该题的答案；忙碌时不能重复提交", () => {
    const onAction = vi.fn();
    const question = card();
    const { rerender } = render(<QuestionCardView card={question} onAction={onAction} />);
    expect((screen.getByRole("button", { name: "提交并讲解" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("radio", { name: "B. 2" }));
    expect((screen.getByRole("radio", { name: "B. 2" }) as HTMLInputElement).checked).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "提交并讲解" }));
    expect(onAction).toHaveBeenCalledWith("answer", question, "B");
    rerender(<QuestionCardView card={question} busy onAction={onAction} />);
    expect((screen.getByRole("button", { name: "提交并讲解" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("radio", { name: "A. 1" }) as HTMLInputElement).disabled).toBe(true);
  });

  test("题目版本改变后清空选项草稿，不沿用上个版本的答案", () => {
    const { rerender } = render(<QuestionCardView card={card()} onAction={vi.fn()} />);
    fireEvent.click(screen.getByRole("radio", { name: "A. 1" }));
    rerender(<QuestionCardView card={card({ content_sha256: "b".repeat(64) })} onAction={vi.fn()} />);
    expect((screen.getByRole("radio", { name: "A. 1" }) as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole("button", { name: "提交并讲解" }) as HTMLButtonElement).disabled).toBe(true);
  });
  test("展示题面、选项与来源，未开放答案时不出现答案正文", () => {
    render(<QuestionCardView card={card()} />);
    expect(screen.getByText("下列 Python 表达式的值是？")).toBeTruthy();
    expect(screen.getByText("A.")).toBeTruthy();
    expect(screen.getByText("1")).toBeTruthy();
    expect(screen.getByText(/2024-2025 学年第一学期 Python 期末试卷/)).toBeTruthy();
    expect(screen.queryByText("参考答案")).toBeNull();
  });

  test("查看答案、讲解、变式与保存按钮会带出对应动作", () => {
    const onAction = vi.fn();
    render(<QuestionCardView card={card()} busy={false} onAction={onAction} />);
    fireEvent.click(screen.getByRole("button", { name: "讲解这道题" }));
    fireEvent.click(screen.getByRole("button", { name: "查看参考答案" }));
    fireEvent.click(screen.getByRole("button", { name: "生成一道变式" }));
    fireEvent.click(screen.getByRole("button", { name: "保存到错题本" }));
    expect(onAction.mock.calls.map((call) => call[0])).toEqual([
      "explain",
      "view",
      "exercise",
      "record",
    ]);
  });

  test("已开放答案时显示正文，缺失答案时明确说明", () => {
    const { rerender } = render(
      <QuestionCardView
        card={card({
          answer_revealed: true,
          answer_detail: { state: "original", text: "B" },
        })}
      />,
    );
    expect(screen.getByText("参考答案（已查看，点击展开）")).toBeTruthy();
    expect(screen.getByText("B")).toBeTruthy();

    rerender(
      <QuestionCardView
        card={card({ answer: { state: "missing", available: false } })}
      />,
    );
    expect(screen.getByText(/没有提供参考答案/)).toBeTruthy();
  });

  test("编程题答案渲染代码块而不是参考代码占位符", () => {
    render(
      <QuestionCardView
        card={card({
          question_type: "code",
          answer_revealed: true,
          answer_detail: { state: "original", text: "[参考代码]" },
          code_blocks: [
            {
              block_id: "b1",
              method: 1,
              method_label: "方法一：循环",
              kind: "function",
              name: "solve",
              text: "def solve():\n    return 1",
              pages: [3],
              depends_on: [],
            },
            {
              block_id: "b2",
              method: 1,
              method_label: "方法一：循环",
              kind: "statement",
              name: "main",
              text: "print(solve())",
              pages: [3],
              depends_on: ["b1"],
            },
          ],
          code_annotations: [{ role: "说明", text: "保持缩进", page: 3 }],
        })}
      />,
    );
    expect(screen.getByText("方法一：循环")).toBeTruthy();
    const code = screen.getByText(/def solve\(\):/);
    expect(code.textContent).toContain("    return 1");
    expect(screen.getByText("print(solve())")).toBeTruthy();
    expect(screen.getByText(/依赖：b1/)).toBeTruthy();
    expect(screen.queryByText("[参考代码]")).toBeNull();
  });

  test("题面图加载失败时给出原卷核对提示", () => {
    render(
      <QuestionCardView
        card={card({
          assets: [
            {
              asset_id: "asset-1",
              page: 1,
              description: "题面图",
              url: "/api/assets/1",
            },
          ],
        })}
      />,
    );
    fireEvent.error(screen.getByAltText("题面图"));
    expect(screen.getByText(/题图加载失败，可打开原卷第 1 页核对/)).toBeTruthy();
  });

  test("展示题面/答案页码、知识点状态与含答案提醒", () => {
    render(
      <QuestionCardView
        card={card({
          knowledge_points: [
            { slug: "loop", name: "循环", status: "candidate" },
          ],
        })}
      />,
    );
    expect(screen.getByText(/题面第 1 页/)).toBeTruthy();
    expect(screen.getByText(/答案第 2 页/)).toBeTruthy();
    expect(screen.getByText(/循环（候选）/)).toBeTruthy();
    expect(screen.getByText(/原卷可能包含答案页/)).toBeTruthy();
  });
});

describe("QuestionResultList", () => {
  test("按顺序编号并允许直接选第几道或直接讲解", () => {
    const onAction = vi.fn();
    render(
      <QuestionResultList
        cards={[
          card(),
          card({
            question_id: "py-2024-2025-s1-final-choice-2",
            number_label: "2",
            statement: "第二道题",
            selected: false,
          }),
        ]}
        status="ok"
        onAction={onAction}
      />,
    );
    expect(screen.getByText(/找到 2 道已审核真题/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "选第 2 道题" }));
    expect(onAction).toHaveBeenCalledWith(
      "select",
      expect.objectContaining({
        question_id: "py-2024-2025-s1-final-choice-2",
      }),
    );
  });
});

test("来源页链接绑定版本与物理页，查看事实可在刷新后显示", () => {
  render(<QuestionCardView card={card({
    source_url: "/api/conversations/c/questions/q/source?build=iv-old",
    source_views: [{ kind: "answer", page: 2, viewed_at: "2026-10-27T10:00:00+08:00", may_show_answer: true }],
  })} />);
  expect(screen.getByRole("link", { name: "查看题面原页 · 第 1 页" }).getAttribute("href"))
    .toBe("/api/conversations/c/questions/q/source?build=iv-old&kind=statement&page=1#page=1");
  expect(screen.getByRole("link", { name: "查看答案原页 · 第 2 页" }).getAttribute("href"))
    .toContain("kind=answer&page=2#page=2");
  expect(screen.getByText(/已查看原卷来源：答案第 2 页/)).toBeTruthy();
});
