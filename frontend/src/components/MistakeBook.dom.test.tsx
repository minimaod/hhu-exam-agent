import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import MistakeBook from "./MistakeBook";
import type { MistakeRecordDetail } from "../lib/mistakesApi";

const { getMistakeRecord, listMistakeRecords } = vi.hoisted(() => ({
  getMistakeRecord: vi.fn(), listMistakeRecords: vi.fn(),
}));
vi.mock("../lib/mistakesApi", async (importOriginal) => ({
  ...await importOriginal<typeof import("../lib/mistakesApi")>(), getMistakeRecord, listMistakeRecords,
}));

test("多次纠正后显示记录修订版数，同时保留母题来源版本", async () => {
  const date = "2026-11-01T12:00:00+08:00";
  const detail: MistakeRecordDetail = {
    id: "record-1", title: "循环边界", conversation_id: "c", problem_id: "p", root_problem_id: "p",
    problem_version: 1, statement_text: "统计 1 到 500 的完全数", source: { type: "none" },
    student_answer_text: null, solution_text: null, attachments: [], explanation: null, followups: [],
    feedback: { text: null, self_reported_understood: false, self_reported_at: null, updated_at: null },
    self_reported_understood: false, conversation_has_newer_version: false, newer_problem: null,
    revisions: [1, 2, 3].map((id) => ({ id, request_id: `r${id}`, reason: "correction",
      statement_text: `旧题干 ${id}`, source: { type: "none" }, explanation: null, created_at: date })),
    created_at: date, updated_at: date,
  };
  listMistakeRecords.mockResolvedValue({ items: [], total: 0 });
  getMistakeRecord.mockResolvedValue(detail);
  render(<MistakeBook selectedId="record-1" onSelectRecord={vi.fn()} />);
  expect(await screen.findByText("记录第 4 版 · 来源题干第 1 版")).toBeTruthy();
  expect(screen.getByText("历史版本（3）")).toBeTruthy();
});

test.each([false, true])("详情渲染公式和历史字段，自定义标题保留=%s", async (customTitle) => {
  const date = "2026-11-20T11:33:00+08:00";
  const statement = "求极限 $\\lim_{x \\to 0} \\left(\\frac{\\tan x}{x}\\right)^{1/x^2}$。";
  const legacyTitle = "求极限 $\\lim_{x \\to 0} \\left(\\frac{\\tan x…";
  const explanation = {
    knowledge_points: [{ name: "重要极限 $\\lim_{t\\to0}(1+t)^{1/t}=e$", explanation: "换元后使用基本极限。" }],
    steps: [{ index: 1, title: "把对数内部化为 $1+t$ 形式", content: "先取对数。" }],
    final_answer: "$e^{1/3}$",
    error_analysis: null,
    uncertainties: [],
  };
  const record: MistakeRecordDetail = {
    id: "r-math", title: customTitle ? "复习 $1^\\infty$ 型" : legacyTitle,
    conversation_id: "c", problem_id: "p", root_problem_id: "p", problem_version: 1,
    statement_text: statement, source: { type: "none" }, student_answer_text: null,
    solution_text: null, attachments: [], explanation, followups: [],
    feedback: { text: null, self_reported_understood: false, self_reported_at: null, updated_at: null },
    self_reported_understood: false, conversation_has_newer_version: false, newer_problem: null,
    revisions: [{ id: 1, request_id: "previous", reason: "correction", statement_text: "旧题：$x^2$。",
      source: { type: "none" }, explanation, created_at: date }],
    created_at: date, updated_at: date,
  };
  listMistakeRecords.mockResolvedValue({ items: [], total: 0 });
  getMistakeRecord.mockResolvedValue(record);
  const { container } = render(<MistakeBook selectedId={record.id} onSelectRecord={vi.fn()} />);
  await screen.findByText("讲解");
  const sheet = container.querySelector(".lesson-sheet");
  expect(sheet?.querySelector(".lesson-statement .katex")).toBeTruthy();
  expect(sheet?.querySelectorAll(".lesson-statement")).toHaveLength(1);
  expect(Boolean(sheet?.querySelector("h3 .katex"))).toBe(customTitle);
  expect(sheet?.textContent).not.toContain(legacyTitle);
  expect(screen.getByText(/^重要极限/, { selector: ".font-medium" }).querySelector(".katex")).toBeTruthy();
  expect(sheet?.querySelector(".step-title .katex")).toBeTruthy();
  expect(screen.getByText(/旧题：/).querySelector(".katex")).toBeTruthy();
  expect(screen.getByText("旧讲解最终答案：").querySelector(".katex")).toBeTruthy();
});

test("错题列表用 KaTeX 渲染摘要公式，且标题与题干重复时只展示一次", async () => {
  const date = "2026-11-15T12:00:00+08:00";
  listMistakeRecords.mockResolvedValue({
    items: [
      {
        id: "r-math",
        title: "高等数学 · 偏导数",
        statement_excerpt: "设 $z = f(x,y)$，求偏导数。",
        source: { type: "none" },
        is_model_generated: true,
        problem_version: 1,
        self_reported_understood: false,
        created_at: date,
        updated_at: date,
      },
      {
        id: "r-dup",
        title: "关于元组，以下描述错误的是( ) A. 元组中元素可以是不同类型…",
        statement_excerpt: "关于元组，以下描述错误的是( ) A. 元组中元素可以是不同类型 B. 元组不可变",
        source: { type: "none" },
        is_model_generated: false,
        problem_version: 1,
        self_reported_understood: false,
        created_at: date,
        updated_at: date,
      },
    ],
    total: 2,
  });

  const { container } = render(<MistakeBook selectedId={null} onSelectRecord={vi.fn()} />);
  expect(await screen.findByText("高等数学 · 偏导数")).toBeTruthy();
  expect(container.querySelector(".katex-html")).toBeTruthy();
  expect(container.textContent).not.toContain("$z = f(x,y)$");
  expect(screen.getAllByText(/关于元组，以下描述错误的是/)).toHaveLength(1);
});
