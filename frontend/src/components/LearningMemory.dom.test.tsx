import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import LearningMemory from "./LearningMemory";
import type { LearningMemoryView } from "../lib/memoryApi";

const { fetchLearningMemory, fetchMemoryCourses, correctMemory, deleteMemory } =
  vi.hoisted(() => ({
    fetchLearningMemory: vi.fn(),
    fetchMemoryCourses: vi.fn(),
    correctMemory: vi.fn(),
    deleteMemory: vi.fn(),
  }));
vi.mock("../lib/memoryApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/memoryApi")>()),
  fetchLearningMemory,
  fetchMemoryCourses,
  correctMemory,
  deleteMemory,
}));

function makeView(overrides: Partial<LearningMemoryView> = {}): LearningMemoryView {
  const entry = {
    entry_id: "exercise:11111111-1111-4111-8111-111111111111",
    kind: "exercise" as const,
    course: "Python语言程序设计",
    course_source: "paper" as const,
    knowledge_points: ["循环的条件与终止条件"],
    knowledge_points_normalized: [true],
    statement_excerpt: "统计 1 到 500 的完全数",
    verdict: "wrong_independent" as const,
    first_verdict: "wrong_independent" as const,
    recorded_at: "2026-11-01T10:00:00+08:00",
    first_at: "2026-11-01T10:00:00+08:00",
    wrong_at: "2026-11-01T10:00:00+08:00",
    validity: "ok" as const,
    validity_note: null,
    note: null,
    source: {
      conversation_id: "conv-1",
      exercise_id: null,
      problem_id: null,
      question_id: null,
      paper_id: "py-2023-2024-s2-final",
      paper_title: "Python 2023-2024 第二学期期末",
      number_label: "第 3 题",
    },
    attempts: [
      {
        attempt_id: "22222222-2222-4222-8222-222222222222",
        attempt_no: 1,
        kind: "initial",
        verdict: "wrong_independent" as const,
        label: "独立作答错误",
        is_correct: false,
        answered_after_revealed: false,
        recorded_at: "2026-11-01T10:00:00+08:00",
        excluded: false,
      },
    ],
  };
  return {
    status: "ok",
    course: "Python语言程序设计",
    range_text: "近30天（2026-10-03 至 2026-11-01）",
    range: { days: 30, fallback: false, limit: 50 },
    counts: {
      evidence: 1,
      practices: 1,
      questions: 1,
      attempts: 1,
      excluded: 0,
      pending_course: 1,
      wrong_independent: 1,
      unclear: 0,
      excluded_evaluation: 0,
      earlier_wrong: 0,
      superseded: 0,
      unverified: 0,
    },
    groups: [
      {
        label: "循环的条件与终止条件",
        slug: "loop-termination",
        normalized: true,
        wrong_count: 1,
        correct_count: 0,
        after_reveal_count: 0,
        insufficient_count: 0,
        unclear_count: 0,
        no_attempt_count: 0,
        self_report_count: 0,
        excluded_evaluation_count: 0,
        earlier_wrong_count: 0,
        entry_ids: [entry.entry_id],
        latest_at: "2026-11-01T10:00:00+08:00",
        latest_wrong_at: "2026-11-01T10:00:00+08:00",
        suggestion: "先补“循环的条件与终止条件”：独立做错 1 次",
      },
    ],
    entries: [entry],
    suggestions: ["先补“循环的条件与终止条件”：独立做错 1 次"],
    pending_course_entries: [],
    message: "",
    ...overrides,
  };
}

test("学习记忆页只展示证据、建议和逐条记录，不出现掌握率", async () => {
  fetchMemoryCourses.mockResolvedValue({
    items: [
      { course: "Python语言程序设计", label: "Python语言程序设计", evidence: 1 },
    ],
    pending: 1,
  });
  fetchLearningMemory.mockResolvedValue(makeView());

  render(<LearningMemory />);

  expect(await screen.findByText("建议先补")).toBeTruthy();
  expect(screen.getByText("独立作答错误")).toBeTruthy();
  expect(screen.getByText("统计 1 到 500 的完全数")).toBeTruthy();
  expect(screen.getByRole("button", { name: "从学习记忆移除" })).toBeTruthy();
  const text = document.body.textContent ?? "";
  expect(text).not.toMatch(/%|掌握率|正确率/);
  expect(text).toContain("一次做对也不会被当成永久掌握");
});

test("更正只提交改动过的字段并提示原会话不变", async () => {
  fetchMemoryCourses.mockResolvedValue({
    items: [
      { course: "Python语言程序设计", label: "Python语言程序设计", evidence: 1 },
    ],
    pending: 0,
  });
  fetchLearningMemory.mockResolvedValue(makeView());
  correctMemory.mockResolvedValue({});

  render(<LearningMemory />);

  fireEvent.click(await screen.findByRole("button", { name: "更正这条" }));
  fireEvent.change(screen.getByPlaceholderText("例如：这是我当时看错条件"), {
    target: { value: "这是我当时看错条件" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存更正" }));

  await waitFor(() => expect(correctMemory).toHaveBeenCalledOnce());
  const [entryId, change] = correctMemory.mock.calls[0];
  expect(entryId).toBe("exercise:11111111-1111-4111-8111-111111111111");
  expect(change).toEqual({ note: "这是我当时看错条件" });
  expect(
    await screen.findByText("已更新这条学习记忆，原会话记录不变。"),
  ).toBeTruthy();
});

test("留空课程会提交空串，把条目改回待确认", async () => {
  fetchMemoryCourses.mockResolvedValue({
    items: [
      { course: "Python语言程序设计", label: "Python语言程序设计", evidence: 1 },
    ],
    pending: 0,
  });
  fetchLearningMemory.mockResolvedValue(makeView());
  correctMemory.mockResolvedValue({});

  render(<LearningMemory />);

  fireEvent.click(await screen.findByRole("button", { name: "更正这条" }));
  fireEvent.change(screen.getByPlaceholderText("例如：Python语言程序设计"), {
    target: { value: "" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存更正" }));

  await waitFor(() => expect(correctMemory).toHaveBeenCalledOnce());
  expect(correctMemory.mock.calls[0][1]).toEqual({ course: "" });
});

test("移除走确认并原样显示“原会话记录保留”的结果", async () => {
  fetchMemoryCourses.mockResolvedValue({
    items: [
      { course: "Python语言程序设计", label: "Python语言程序设计", evidence: 1 },
    ],
    pending: 0,
  });
  fetchLearningMemory.mockResolvedValue(makeView());
  deleteMemory.mockResolvedValue({
    removed: true,
    message: "从学习记忆和复习建议中移除，原会话记录保留。",
  });
  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

  render(<LearningMemory />);

  fireEvent.click(await screen.findByRole("button", { name: "从学习记忆移除" }));

  await waitFor(() => expect(deleteMemory).toHaveBeenCalledOnce());
  expect(confirmSpy).toHaveBeenCalledOnce();
  expect(
    await screen.findByText("从学习记忆和复习建议中移除，原会话记录保留。"),
  ).toBeTruthy();
  confirmSpy.mockRestore();
});

test("读取失败时如实说明并给出重试", async () => {
  fetchMemoryCourses.mockResolvedValue({ items: [], pending: 0 });
  const { MemoryApiError } = await import("../lib/memoryApi");
  fetchLearningMemory.mockRejectedValueOnce(
    new MemoryApiError("本地学习数据暂时不可用，请稍后再试。"),
  );

  render(<LearningMemory />);

  expect(
    await screen.findByText("本地学习数据暂时不可用，请稍后再试。"),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
});