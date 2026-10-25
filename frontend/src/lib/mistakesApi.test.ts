import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";

import {
  MistakesApiError,
  streamRecordCorrection,
} from "./mistakesApi.ts";

const RECORD_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const REQUEST_ID = "11111111-1111-4111-8111-111111111111";

function sseBody(events: object[]): string {
  return events
    .map((event) => `data: ${JSON.stringify(event)}\n\n`)
    .join("");
}

function installFetch(
  t: TestContext,
  body: string,
  status = 200,
): string[] {
  const calls: string[] = [];
  const original = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = original;
  });
  globalThis.fetch = async (input, init) => {
    calls.push(String(input));
    assert.equal(init?.method, "POST");
    return new Response(body, {
      status,
      headers: { "Content-Type": "text/event-stream" },
    });
  };
  return calls;
}

test("纠正记录的不清楚结果被解析", async (t) => {
  installFetch(
    t,
    sseBody([
      {
        type: "progress",
        record_id: RECORD_ID,
        request_id: REQUEST_ID,
        stage: "checking_clarity",
        message: "正在检查题目",
      },
      {
        type: "result",
        record_id: RECORD_ID,
        request_id: REQUEST_ID,
        result: {
          kind: "record_needs_clarification",
          statement_text: "模糊题干",
          questions: ["请补充被积函数"],
          unclear_points: ["被积函数看不清"],
        },
      },
    ]),
  );

  const progress: string[] = [];
  const result = await streamRecordCorrection({
    recordId: RECORD_ID,
    requestId: REQUEST_ID,
    statementText: "模糊题干",
    onProgress: (event) => progress.push(event.message),
  });

  assert.equal(result.kind, "record_needs_clarification");
  assert.deepEqual(progress, ["正在检查题目"]);
});

test("纠正记录的错误事件转成带错误码的异常", async (t) => {
  installFetch(
    t,
    sseBody([
      {
        type: "error",
        record_id: RECORD_ID,
        request_id: REQUEST_ID,
        code: "TUTORING_FAILED",
        message: "讲解没有完成",
      },
    ]),
  );

  await assert.rejects(
    streamRecordCorrection({
      recordId: RECORD_ID,
      requestId: REQUEST_ID,
      statementText: "题干",
    }),
    (error: unknown) => {
      assert.ok(error instanceof MistakesApiError);
      assert.equal(error.code, "TUTORING_FAILED");
      return true;
    },
  );
});
