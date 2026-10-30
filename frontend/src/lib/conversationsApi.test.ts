/**
 * 会话客户端的行为测试。
 *
 * 用 Node 内置测试运行器；替换 fetch 并构造 SSE 文本，覆盖历史解析、事件
 * 信封校验、错误事件、HTTP 冲突和提前断流。不需要浏览器或真实后端。
 */

import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";

import {
  ConversationsApiError,
  createConversation,
  createRequestId,
  getConversation,
  listConversations,
  revealExerciseSolution,
  streamTurn,
  type TurnMessageDeltaEvent,
  type TurnProgressEvent,
} from "./conversationsApi.ts";

const CONVERSATION = {
  id: "c4f1f0e2-1111-4222-8333-444455556666",
  title: "我要复习高数",
  created_at: "2026-10-24T12:00:00+00:00",
  updated_at: "2026-10-24T12:00:05+00:00",
};

const PAPER = {
  id: "math-bi-20250116-final",
  title: "2024-2025学年第一学期《高等数学BⅠ》期末试题及答案",
  course: "高等数学BⅠ",
  academic_year: "2024-2025",
  exam_type: "final",
  exam_date: "2025-01-16",
  download_url: "/api/papers/math-bi-20250116-final/download",
};

const RESULT = {
  type: "result",
  kind: "found",
  message: "找到 1 份试卷",
  filters: { course: "高等数学", exam_type: "final" },
  items: [PAPER],
  total: 1,
};

const REQUEST_ID = "11111111-1111-4111-8111-111111111111";

type FetchCall = { url: string; init: RequestInit | undefined };

function installFetch(
  t: TestContext,
  body: string,
  init: ResponseInit = {
    status: 200,
    headers: { "Content-Type": "application/json" },
  },
): FetchCall[] {
  const original = globalThis.fetch;
  const calls: FetchCall[] = [];

  globalThis.fetch = (async (input: RequestInfo | URL, options?: RequestInit) => {
    calls.push({ url: String(input), init: options });
    return new Response(body, init);
  }) as typeof fetch;

  t.after(() => {
    globalThis.fetch = original;
  });

  return calls;
}

function sseBody(events: unknown[]): string {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
}

function envelope(extra: Record<string, unknown>): Record<string, unknown> {
  return {
    conversation_id: CONVERSATION.id,
    request_id: REQUEST_ID,
    ...extra,
  };
}

test("创建会话发送空对象并返回摘要", async (t) => {
  const calls = installFetch(t, JSON.stringify(CONVERSATION), { status: 201 });

  const created = await createConversation();

  assert.equal(created.id, CONVERSATION.id);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/conversations");
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(calls[0].init?.body, "{}");
});

test("会话列表校验 total 与条目一致", async (t) => {
  installFetch(
    t,
    JSON.stringify({ items: [CONVERSATION], total: 2 }),
  );

  await assert.rejects(listConversations(), (error: unknown) => {
    assert.ok(error instanceof ConversationsApiError);
    assert.match(error.message, /格式/);
    return true;
  });
});

test("会话详情解析已完成、失败和中断三种轮次", async (t) => {
  const detail = {
    conversation: CONVERSATION,
    active_problem: null,
    record: null,
    turns: [
      {
        request_id: REQUEST_ID,
        seq: 1,
        kind: "query",
        query: "我要复习高数",
        status: "completed",
        result: RESULT,
        tutoring: null,
        error: null,
        started_at: "2026-10-24T12:00:00+00:00",
        finished_at: "2026-10-24T12:00:05+00:00",
      },
      {
        request_id: "22222222-2222-4222-8222-222222222222",
        seq: 2,
        kind: "query",
        query: "找大学物理期末试卷",
        status: "failed",
        result: null,
        tutoring: null,
        error: { code: "MODEL_TIMEOUT", message: "理解需求超时，请重试。" },
        started_at: "2026-10-24T12:01:00+00:00",
        finished_at: "2026-10-24T12:01:30+00:00",
      },
      {
        request_id: "33333333-3333-4333-8333-333333333333",
        seq: 3,
        kind: "query",
        query: "找线代试卷",
        status: "interrupted",
        result: null,
        tutoring: null,
        error: null,
        started_at: "2026-10-24T12:02:00+00:00",
        finished_at: "2026-10-24T12:02:01+00:00",
      },
    ],
  };
  installFetch(t, JSON.stringify(detail));

  const parsed = await getConversation(CONVERSATION.id);

  assert.equal(parsed.turns.length, 3);
  assert.equal(parsed.turns[0].result?.kind, "found");
  assert.equal(parsed.turns[1].error?.code, "MODEL_TIMEOUT");
  assert.equal(parsed.turns[2].status, "interrupted");
});

test("已完成轮次缺少结果时详情被拒绝", async (t) => {
  installFetch(
    t,
    JSON.stringify({
      conversation: CONVERSATION,
      turns: [
        {
          request_id: REQUEST_ID,
          seq: 1,
          query: "我要复习高数",
          status: "completed",
          result: null,
          error: null,
          started_at: "2026-10-24T12:00:00+00:00",
          finished_at: "2026-10-24T12:00:05+00:00",
        },
      ],
    }),
  );

  await assert.rejects(getConversation(CONVERSATION.id), (error: unknown) => {
    assert.ok(error instanceof ConversationsApiError);
    assert.match(error.message, /会话记录格式/);
    return true;
  });
});

test("提交轮次发送 request_id 并返回最终结果", async (t) => {
  const progress: TurnProgressEvent[] = [];
  const calls = installFetch(
    t,
    sseBody([
      envelope({
        type: "progress",
        stage: "understanding",
        message: "正在理解复习需求",
      }),
      envelope({ type: "result", kind: "query", result: RESULT }),
    ]),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );

  const event = await streamTurn({
    conversationId: CONVERSATION.id,
    requestId: REQUEST_ID,
    query: "找高数期末试卷",
    onProgress: (item) => progress.push(item),
  });

  assert.equal(event.result?.kind, "found");
  assert.equal(progress.length, 1);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    `/api/conversations/${CONVERSATION.id}/turns`,
  );
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), {
    request_id: REQUEST_ID,
    query: "找高数期末试卷",
  });
});

test("其他会话或请求的事件不会被接受", async (t) => {
  installFetch(
    t,
    sseBody([
      {
        type: "result",
        conversation_id: "another-conversation",
        request_id: REQUEST_ID,
        result: RESULT,
      },
    ]),
  );

  await assert.rejects(
    streamTurn({
      conversationId: CONVERSATION.id,
      requestId: REQUEST_ID,
      query: "我要复习高数",
    }),
    (error: unknown) => {
      assert.ok(error instanceof ConversationsApiError);
      assert.match(error.message, /其他会话/);
      return true;
    },
  );
});

test("存储错误事件转成带错误码的异常", async (t) => {
  installFetch(
    t,
    sseBody([
      envelope({
        type: "error",
        code: "STORAGE_UNAVAILABLE",
        message: "本地学习数据暂时不可用，请稍后再试。",
      }),
    ]),
  );

  await assert.rejects(
    streamTurn({
      conversationId: CONVERSATION.id,
      requestId: REQUEST_ID,
      query: "我要复习高数",
    }),
    (error: unknown) => {
      assert.ok(error instanceof ConversationsApiError);
      assert.equal(error.code, "STORAGE_UNAVAILABLE");
      return true;
    },
  );
});

test("HTTP 409 的 detail 会展示给页面", async (t) => {
  installFetch(
    t,
    JSON.stringify({ detail: "该会话已有一轮查询正在执行，请等待完成或先停止。" }),
    { status: 409, headers: { "Content-Type": "application/json" } },
  );

  await assert.rejects(
    streamTurn({
      conversationId: CONVERSATION.id,
      requestId: REQUEST_ID,
      query: "我要复习高数",
    }),
    (error: unknown) => {
      assert.ok(error instanceof ConversationsApiError);
      assert.equal(error.status, 409);
      assert.match(error.message, /正在执行/);
      return true;
    },
  );
});

test("没有最终事件就结束属于未完成", async (t) => {
  installFetch(
    t,
    sseBody([
      envelope({
        type: "progress",
        stage: "understanding",
        message: "正在理解复习需求",
      }),
    ]),
  );

  await assert.rejects(
    streamTurn({
      conversationId: CONVERSATION.id,
      requestId: REQUEST_ID,
      query: "我要复习高数",
    }),
    (error: unknown) => {
      assert.ok(error instanceof ConversationsApiError);
      assert.equal(error.code, "STREAM_INCOMPLETE");
      return true;
    },
  );
});

test("request_id 是合法的 UUID", () => {
  assert.match(
    createRequestId(),
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
});

test("动态轮次解析练习题、建议和防泄露参考解答", async (t) => {
  const EXERCISE_VIEW = {
    id: "ex-123",
    conversation_id: CONVERSATION.id,
    problem_id: "prob-456",
    root_problem_id: "prob-456",
    statement_text: "求极限 lim x->0 sin(2x)/x",
    knowledge_point: "重要极限",
    reference_solution_text: null,
    status: "pending_answer",
    student_answer_text: null,
    student_solution_text: null,
    revealed_solution_at: null,
    evaluation: null,
    answered_after_revealed: false,
    attachments: [],
    created_at: "2026-10-25T12:00:00+00:00",
    updated_at: "2026-10-25T12:00:00+00:00",
  };

  installFetch(
    t,
    sseBody([
      envelope({
        type: "result",
        kind: "dynamic",
        assistant_message: "已生成针对性练习题：",
        exercise: EXERCISE_VIEW,
        suggestions: [
          { label: "查看解答", action: "查看解答", target_id: "ex-123" },
          { label: "换一道题", action: "换一道题", target_id: "prob-456" },
        ],
      }),
    ]),
  );

  const event = await streamTurn({
    conversationId: CONVERSATION.id,
    requestId: REQUEST_ID,
    query: "来一道类似题",
  });

  assert.equal(event.kind, "dynamic");
  assert.equal(event.assistant_message, "已生成针对性练习题：");
  assert.ok(event.exercise);
  assert.equal(event.exercise.id, "ex-123");
  // 严格防泄露断言：待作答状态下参考解答必须为 null
  assert.equal(event.exercise.reference_solution_text, null);
  assert.deepEqual(event.suggestions, [
    { label: "查看解答", action: "查看解答", target_id: "ex-123" },
    { label: "换一道题", action: "换一道题", target_id: "prob-456" },
  ]);
});

test("作答轮次接受 exercise 与 evaluating_exercise 的正式契约", async (t) => {
  const EXERCISE_VIEW = {
    id: "ex-123",
    conversation_id: CONVERSATION.id,
    problem_id: "prob-456",
    root_problem_id: "prob-456",
    statement_text: "求极限 lim x->0 sin(2x)/x",
    knowledge_point: "重要极限",
    reference_solution_text: "答案为 2",
    status: "submitted",
    student_answer_text: "2",
    student_solution_text: "换元后使用重要极限",
    revealed_solution_at: null,
    evaluation: {
      is_correct: true,
      final_answer_assessment: "答案正确。",
      step_by_step_analysis: "步骤完整。",
      guidance: "继续保持。",
      needs_supplement: false,
    },
    answered_after_revealed: false,
    attachments: [],
    created_at: "2026-10-25T12:00:00+00:00",
    updated_at: "2026-10-25T12:01:00+00:00",
  };

  installFetch(
    t,
    sseBody([
      envelope({
        type: "progress",
        stage: "evaluating_exercise",
        message: "正在评价",
      }),
      envelope({
        type: "result",
        kind: "exercise",
        exercise: EXERCISE_VIEW,
        evaluation: EXERCISE_VIEW.evaluation,
        assistant_message: "已完成作答评价。",
        exercise_id: "ex-123",
      }),
    ]),
  );

  const stages: string[] = [];
  const event = await streamTurn({
    conversationId: CONVERSATION.id,
    requestId: REQUEST_ID,
    query: "2",
    kind: "exercise",
    exerciseId: "ex-123",
    studentAnswer: "2",
    onProgress: (progress) => stages.push(progress.stage),
  });

  assert.deepEqual(stages, ["evaluating_exercise"]);
  assert.equal(event.kind, "exercise");
  assert.equal(event.exercise_id, "ex-123");
  assert.equal(event.evaluation?.is_correct, true);
});

test("旧版 exercise_answer 结果读取时归一为 exercise", async (t) => {
  installFetch(
    t,
    sseBody([
      envelope({
        type: "result",
        kind: "exercise_answer",
        exercise: {
          id: "ex-legacy",
          conversation_id: CONVERSATION.id,
          problem_id: "prob-456",
          root_problem_id: "prob-456",
          statement_text: "旧练习",
          knowledge_point: "考点",
          status: "submitted",
          student_answer_text: "1",
          student_solution_text: null,
          revealed_solution_at: null,
          evaluation: null,
          answered_after_revealed: false,
          attachments: [],
          created_at: "2026-10-25T12:00:00+00:00",
          updated_at: "2026-10-25T12:00:00+00:00",
        },
        assistant_message: "旧格式",
        exercise_id: "ex-legacy",
      }),
    ]),
  );

  const event = await streamTurn({
    conversationId: CONVERSATION.id,
    requestId: REQUEST_ID,
    query: "1",
  });
  assert.equal(event.kind, "exercise");
});

test("建议项仍为字符串或旧练习附件结构时拒绝解析", async (t) => {
  installFetch(
    t,
    sseBody([
      envelope({
        type: "result",
        kind: "dynamic",
        assistant_message: "已生成",
        exercise: {
          id: "ex-bad",
          conversation_id: CONVERSATION.id,
          problem_id: "prob-456",
          root_problem_id: "prob-456",
          statement_text: "练习",
          knowledge_point: "考点",
          status: "pending_answer",
          answered_after_revealed: false,
          attachments: [
            {
              id: "a-1",
              file_path: "/tmp/a.png",
              content_type: "image/png",
              byte_size: 10,
            },
          ],
          created_at: "2026-10-25T12:00:00+00:00",
          updated_at: "2026-10-25T12:00:00+00:00",
        },
        suggestions: ["查看解答"],
      }),
    ]),
  );

  await assert.rejects(
    streamTurn({
      conversationId: CONVERSATION.id,
      requestId: REQUEST_ID,
      query: "来一道类似题",
    }),
    (error: unknown) => {
      assert.ok(error instanceof ConversationsApiError);
      assert.match(error.message, /格式不符合约定/);
      return true;
    },
  );
});

test("主动查看参考解答接口调用并返回解答", async (t) => {
  const REVEALED = {
    id: "ex-123",
    conversation_id: CONVERSATION.id,
    problem_id: "prob-456",
    root_problem_id: "prob-456",
    knowledge_point: "重要极限",
    status: "revealed",
    statement_text: "求极限 lim x->0 sin(2x)/x",
    reference_solution_text: "令 t=2x，原式 = 2 * lim sin(t)/t = 2",
    student_answer_text: null,
    student_solution_text: null,
    revealed_solution_at: "2026-10-25T12:01:00+00:00",
    evaluation: null,
    answered_after_revealed: false,
    attachments: [],
    created_at: "2026-10-25T12:00:00+00:00",
    updated_at: "2026-10-25T12:01:00+00:00",
  };

  const calls = installFetch(t, JSON.stringify(REVEALED), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

  const res = await revealExerciseSolution(CONVERSATION.id, "ex-123");
  assert.equal(res.id, "ex-123");
  assert.equal(res.reference_solution_text, "令 t=2x，原式 = 2 * lim sin(t)/t = 2");
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    `/api/conversations/${CONVERSATION.id}/exercises/ex-123/reveal`,
  );
  assert.equal(calls[0].init?.method, "POST");
});

test("正文片段按 seq 顺序回调，乱序或重复立即失败", async (t) => {
  const deltas: TurnMessageDeltaEvent[] = [];
  installFetch(
    t,
    sseBody([
      envelope({ type: "progress", stage: "composing_reply", message: "正在组织回复" }),
      envelope({ type: "message_delta", seq: 1, delta: "已经确认科目。" }),
      envelope({ type: "message_delta", seq: 2, delta: "我先找两份试卷。" }),
      envelope({
        type: "result",
        kind: "dynamic",
        assistant_message: "已经确认科目。我先找两份试卷。",
      }),
    ]),
  );

  const event = await streamTurn({
    conversationId: CONVERSATION.id,
    requestId: REQUEST_ID,
    query: "我要复习高数",
    onMessageDelta: (item) => deltas.push(item),
  });

  assert.deepEqual(
    deltas.map((item) => item.seq),
    [1, 2],
  );
  assert.equal(deltas.map((item) => item.delta).join(""), "已经确认科目。我先找两份试卷。");
  assert.equal(event.type, "result");

  // 重复/跳号的片段视为损坏的流，调用方必须丢弃临时正文。
  installFetch(
    t,
    sseBody([
      envelope({ type: "message_delta", seq: 1, delta: "第一段" }),
      envelope({ type: "message_delta", seq: 3, delta: "跳号" }),
      envelope({ type: "result", kind: "dynamic", assistant_message: "第一段跳号" }),
    ]),
  );

  await assert.rejects(
    streamTurn({
      conversationId: CONVERSATION.id,
      requestId: REQUEST_ID,
      query: "我要复习高数",
      onMessageDelta: () => {},
    }),
    (error: unknown) => {
      assert.ok(error instanceof ConversationsApiError);
      assert.equal(error.code, "STREAM_INCOMPLETE");
      return true;
    },
  );
});

test("正文片段格式不合法时按契约错误处理", async (t) => {
  for (const payload of [
    { type: "message_delta", seq: 0, delta: "零号" },
    { type: "message_delta", seq: 1, delta: "" },
    { type: "message_delta", delta: "缺 seq" },
  ]) {
    installFetch(t, sseBody([envelope(payload), envelope({ type: "result", kind: "dynamic" })]));
    await assert.rejects(
      streamTurn({
        conversationId: CONVERSATION.id,
        requestId: REQUEST_ID,
        query: "我要复习高数",
        onMessageDelta: () => {},
      }),
      (error: unknown) => {
        assert.ok(error instanceof ConversationsApiError);
        return true;
      },
    );
  }
});
