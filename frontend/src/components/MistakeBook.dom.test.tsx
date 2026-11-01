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
