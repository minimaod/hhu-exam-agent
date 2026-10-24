/**
 * 前端查询客户端的行为测试。
 *
 * 用 Node 内置 test runner 运行，不引入额外测试框架；通过替换 fetch 和构造
 * ReadableStream，覆盖分块、中文 UTF-8 跨块、CRLF、坏帧、错误事件与提前断流。
 */

import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";

import {
  QueryApiError,
  streamQuery,
  type ProgressEvent,
  type ResultEvent,
} from "./queryApi.ts";

const PAPER = {
  id: "math-bi-20250116-final",
  title: "2024-2025学年第一学期《高等数学BⅠ》期末试题及答案",
  course: "高等数学BⅠ",
  academic_year: "2024-2025",
  exam_type: "final",
  exam_date: "2025-01-16",
  download_url: "/api/papers/math-bi-20250116-final/download",
};

function sseBody(events: unknown[]): string {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
}

function foundEvents(): unknown[] {
  return [
    {
      type: "progress",
      stage: "understanding",
      message: "正在理解复习需求",
    },
    {
      type: "progress",
      stage: "retrieving",
      message: "正在查找真实试卷",
    },
    {
      type: "result",
      kind: "found",
      message: "为你找到 1 份试卷",
      filters: { course: "高等数学BⅠ", exam_type: "final" },
      items: [PAPER],
      total: 1,
    },
  ];
}

type FetchCall = { url: string; init: RequestInit | undefined };

function installFetch(
  t: TestContext,
  body: string | ReadableStream<Uint8Array>,
  init: ResponseInit = {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
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

function streamOf(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(chunk);
      }
      controller.close();
    },
  });
}

function splitMidCharacter(text: string, character: string): Uint8Array[] {
  const characterIndex = text.indexOf(character);
  assert.notEqual(characterIndex, -1, `测试文本缺少 ${character}`);

  const bytes = new TextEncoder().encode(text);
  const cut = new TextEncoder().encode(text.slice(0, characterIndex)).length + 1;

  return [bytes.slice(0, cut), bytes.slice(cut)];
}

test("正常流返回结果并回调两条进度", async (t) => {
  const calls = installFetch(t, sseBody(foundEvents()));
  const progress: ProgressEvent[] = [];

  const result = await streamQuery("找高数期末试卷", {
    onProgress: (event) => progress.push(event),
  });

  assert.equal(result.kind, "found");
  assert.equal(result.total, 1);
  assert.deepEqual(
    progress.map((event) => event.stage),
    ["understanding", "retrieving"],
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/query");
  assert.equal(calls[0].init?.method, "POST");
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), {
    query: "找高数期末试卷",
  });
});

test("中文跨块与 CRLF 帧边界都能解析", async (t) => {
  const body = sseBody(foundEvents()).replace(/\n/g, "\r\n");
  installFetch(t, streamOf(splitMidCharacter(body, "高")));

  const result = await streamQuery("我要复习高数");

  assert.equal(result.kind, "found");
  assert.equal(result.items[0].course, "高等数学BⅠ");
});

test("error 事件转成带错误码的 QueryApiError", async (t) => {
  installFetch(
    t,
    sseBody([
      { type: "progress", stage: "understanding", message: "正在理解复习需求" },
      { type: "error", code: "MODEL_TIMEOUT", message: "理解需求超时，请重试。" },
    ]),
  );

  await assert.rejects(streamQuery("我要复习高数"), (error: unknown) => {
    assert.ok(error instanceof QueryApiError);
    assert.equal(error.code, "MODEL_TIMEOUT");
    assert.equal(error.message, "理解需求超时，请重试。");
    return true;
  });
});

test("没有终止事件就结束属于未完成", async (t) => {
  installFetch(
    t,
    sseBody([
      { type: "progress", stage: "understanding", message: "正在理解复习需求" },
    ]),
  );

  await assert.rejects(streamQuery("我要复习高数"), (error: unknown) => {
    assert.ok(error instanceof QueryApiError);
    assert.equal(error.code, "STREAM_INCOMPLETE");
    return true;
  });
});

test("最后一条事件被截断时报未完成", async (t) => {
  const body = sseBody(foundEvents());
  const truncated = body.slice(0, body.lastIndexOf("\n\n"));
  installFetch(t, truncated);

  await assert.rejects(streamQuery("我要复习高数"), (error: unknown) => {
    assert.ok(error instanceof QueryApiError);
    assert.equal(error.code, "STREAM_INCOMPLETE");
    return true;
  });
});

test("坏 JSON 帧不会当成成功", async (t) => {
  installFetch(t, "data: {not json}\n\n");

  await assert.rejects(streamQuery("我要复习高数"), (error: unknown) => {
    assert.ok(error instanceof QueryApiError);
    assert.match(error.message, /JSON/);
    return true;
  });
});

test("result 内部不一致会被拒绝", async (t) => {
  const events = foundEvents();
  const result = events[2] as ResultEvent;
  result.total = 2;

  installFetch(t, sseBody(events));

  await assert.rejects(streamQuery("我要复习高数"), (error: unknown) => {
    assert.ok(error instanceof QueryApiError);
    assert.match(error.message, /格式/);
    return true;
  });
});

test("HTTP 错误体里的 detail 会展示给用户", async (t) => {
  installFetch(
    t,
    JSON.stringify({ detail: "自然语言查询尚未配置：缺少 DEEPSEEK_API_KEY。" }),
    {
      status: 503,
      headers: { "Content-Type": "application/json" },
    },
  );

  await assert.rejects(streamQuery("我要复习高数"), (error: unknown) => {
    assert.ok(error instanceof QueryApiError);
    assert.equal(error.status, 503);
    assert.match(error.message, /DEEPSEEK_API_KEY/);
    return true;
  });
});
