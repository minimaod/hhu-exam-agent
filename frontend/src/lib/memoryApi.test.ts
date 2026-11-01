/**
 * F009 学习记忆客户端的行为测试。
 *
 * 覆盖：查询串拼装、字段解析、401/503 错误文案、PATCH/DELETE 的路径拆分
 * 与最小请求体。全部用 fetch 替身，不连真实后端。
 */

import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";

import {
  MemoryApiError,
  correctMemory,
  deleteMemory,
  fetchLearningMemory,
  fetchMemoryCourses,
} from "./memoryApi.ts";

type FetchCall = { url: string; init: RequestInit | undefined };

function stubFetch(t: TestContext, body: unknown, status = 200): FetchCall[] {
  const original = globalThis.fetch;
  const calls: FetchCall[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init });
    return new Response(typeof body === "string" ? body : JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  t.after(() => {
    globalThis.fetch = original;
  });
  return calls;
}

const ENTRY = {
  entry_id: "exercise:11111111-1111-4111-8111-111111111111",
  kind: "exercise",
  course: "Python语言程序设计",
  course_source: "paper",
  knowledge_points: ["循环"],
  knowledge_points_normalized: [true],
  statement_excerpt: "统计完全数",
  verdict: "wrong_independent",
  recorded_at: "2026-11-01T10:00:00+08:00",
  validity: "ok",
  validity_note: null,
  note: null,
  source: {
    conversation_id: "conv-1",
    exercise_id: null,
    problem_id: null,
    question_id: null,
    paper_id: "p1",
    paper_title: "Python 期末",
    number_label: "第 3 题",
  },
  attempts: [
    {
      attempt_id: "a1",
      attempt_no: 1,
      kind: "initial",
      verdict: "wrong_independent",
      label: "独立作答错误",
      is_correct: false,
      answered_after_revealed: false,
      recorded_at: "2026-11-01T10:00:00+08:00",
      excluded: false,
    },
  ],
};

test("摘要查询带课程与范围参数，并解析出计数、汇总和逐条记录", async (t) => {
  const calls = stubFetch(t, {
    status: "ok",
    course: "Python语言程序设计",
    range_text: "近30天",
    range: { days: 30, fallback: false, limit: 50 },
    counts: {
      evidence: 1,
      practices: 1,
      questions: 1,
      attempts: 1,
      excluded: 0,
      pending_course: 0,
      wrong_independent: 1,
      unclear: 0,
      excluded_evaluation: 2,
      earlier_wrong: 0,
      superseded: 0,
      unverified: 0,
    },
    groups: [
      {
        label: "循环",
        slug: "loop",
        normalized: true,
        wrong_count: 1,
        correct_count: 0,
        after_reveal_count: 0,
        insufficient_count: 0,
        no_attempt_count: 0,
        self_report_count: 0,
        excluded_evaluation_count: 2,
        earlier_wrong_count: 0,
        entry_ids: [ENTRY.entry_id],
        latest_at: null,
        suggestion: "先补“循环”",
      },
    ],
    entries: [ENTRY],
    suggestions: ["先补“循环”"],
    pending_course_entries: [],
    message: "",
  });

  const view = await fetchLearningMemory({
    course: "Python语言程序设计",
    days: 30,
    limit: 50,
  });

  assert.equal(calls[0].url, "/api/learning-memory?course=Python%E8%AF%AD%E8%A8%80%E7%A8%8B%E5%BA%8F%E8%AE%BE%E8%AE%A1&days=30&limit=50");
  assert.equal(view.counts.evidence, 1);
  assert.equal(view.counts.excluded_evaluation, 2);
  assert.equal(view.groups[0].wrong_count, 1);
  assert.equal(view.groups[0].excluded_evaluation_count, 2);
  assert.equal(view.entries[0].attempts[0].label, "独立作答错误");
  assert.equal(view.entries[0].source.number_label, "第 3 题");
});

test("课程列表识别待确认条数", async (t) => {
  stubFetch(t, {
    items: [{ course: "Python语言程序设计", label: "Python语言程序设计", evidence: 2 }],
    pending: 3,
  });

  const view = await fetchMemoryCourses();

  assert.equal(view.items[0].evidence, 2);
  assert.equal(view.pending, 3);
});

test("服务不可用时抛出可展示的中文说明", async (t) => {
  stubFetch(t, { detail: "本地学习数据暂时不可用，请稍后再试。" }, 503);

  await assert.rejects(
    () => fetchLearningMemory({ course: null }),
    (error: unknown) => {
      assert.ok(error instanceof MemoryApiError);
      assert.equal(error.status, 503);
      assert.match(error.message, /暂时不可用/);
      return true;
    },
  );
});

test("结构性错误不会被当成空记录", async (t) => {
  stubFetch(t, { status: "ok", counts: "not-an-object", entries: [ENTRY] });

  await assert.rejects(
    () => fetchLearningMemory({ course: "Python" }),
    (error: unknown) => {
      assert.ok(error instanceof MemoryApiError);
      assert.match(error.message, /格式不正确/);
      return true;
    },
  );
});

test("纠正只发改动字段，并按 entry_id 拆成 PATCH 路径", async (t) => {
  const calls = stubFetch(t, ENTRY);

  await correctMemory(ENTRY.entry_id, { note: "看错条件" });

  assert.equal(
    calls[0].url,
    "/api/learning-memory/exercise/11111111-1111-4111-8111-111111111111",
  );
  assert.equal(calls[0].init?.method, "PATCH");
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { note: "看错条件" });
});

test("移除带原因查询参数并返回原样文案", async (t) => {
  const calls = stubFetch(t, {
    removed: true,
    message: "从学习记忆和复习建议中移除，原会话记录保留。",
  });

  const result = await deleteMemory(ENTRY.entry_id, "学生主动移除");

  assert.equal(
    calls[0].url,
    "/api/learning-memory/exercise/11111111-1111-4111-8111-111111111111?reason=%E5%AD%A6%E7%94%9F%E4%B8%BB%E5%8A%A8%E7%A7%BB%E9%99%A4",
  );
  assert.equal(calls[0].init?.method, "DELETE");
  assert.equal(result.removed, true);
  assert.equal(result.message, "从学习记忆和复习建议中移除，原会话记录保留。");
});

test("非法条目编号在发请求前就被拒绝", async (t) => {
  const calls = stubFetch(t, ENTRY);

  await assert.rejects(
    () => deleteMemory("no-colon"),
    (error: unknown) => {
      assert.ok(error instanceof MemoryApiError);
      return true;
    },
  );
  assert.equal(calls.length, 0);
});