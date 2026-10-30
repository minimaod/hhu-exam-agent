/**
 * F008：流式正文在页面上的呈现。
 *
 * 用可控的 SSE 流手工推帧，验证：
 * 1. 片段到达时立刻显示“正在生成”，且不影响输入区草稿；
 * 2. 收到 result 后临时正文被服务端保存的完整正文替换，不重复显示；
 * 3. 断流时明确标注“已中断、未保存”，不能伪装成成功回复。
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import ConversationWorkspace from "./ConversationWorkspace";
import type { ConversationDetail } from "../lib/conversationsApi";

const cid = "11111111-1111-4111-8111-111111111111";
const date = "2026-10-29T08:00:00+00:00";
const COMPOSER = /输入完整题干|围绕当前题目|输入题目或复习需求/;

function frame(requestId: string, payload: Record<string, unknown>): Uint8Array {
  return new TextEncoder().encode(
    `data: ${JSON.stringify({ conversation_id: cid, request_id: requestId, ...payload })}\n\n`,
  );
}

function detail(assistant: string | null, requestId: string): ConversationDetail {
  return {
    conversation: { id: cid, title: "流式正文", created_at: date, updated_at: date },
    turns: assistant
      ? [
          {
            request_id: requestId,
            seq: 1,
            kind: "dynamic",
            query: "我要复习 Python",
            status: "completed",
            result: null,
            tutoring: null,
            exercise: null,
            evaluation: null,
            error: null,
            started_at: date,
            finished_at: date,
            assistant_message: assistant,
          } as ConversationDetail["turns"][number],
        ]
      : [],
    active_problem: null,
    active_exercise: null,
    record: null,
  };
}

/**
 * 手工可控的 SSE 响应。
 *
 * jsdom 里 `new Response(ReadableStream)` 的时序不稳定，这里只替换
 * `body.getReader()`，客户端真实的分帧、seq 校验和回调逻辑照旧执行。
 */
function manualStream() {
  const queue: Uint8Array[] = [];
  let pending: ((value: ReadableStreamReadResult<Uint8Array>) => void) | null = null;
  let closed = false;

  const deliver = (result: ReadableStreamReadResult<Uint8Array>): boolean => {
    if (!pending) {
      return false;
    }
    const resolve = pending;
    pending = null;
    resolve(result);
    return true;
  };

  const reader = {
    read: async () => {
      if (queue.length > 0) {
        return { value: queue.shift() as Uint8Array, done: false } as const;
      }
      if (closed) {
        return { value: undefined, done: true } as const;
      }
      return new Promise<ReadableStreamReadResult<Uint8Array>>((resolve) => {
        pending = resolve;
      });
    },
    cancel: async () => {},
    releaseLock: () => {},
  };

  const response = {
    ok: true,
    status: 200,
    headers: new Headers({ "Content-Type": "text/event-stream" }),
    body: { getReader: () => reader },
  } as unknown as Response;

  return {
    response,
    send(bytes: Uint8Array) {
      if (!deliver({ value: bytes, done: false })) {
        queue.push(bytes);
      }
    },
    end() {
      closed = true;
      deliver({ value: undefined, done: true });
    },
  };
}

type Harness = {
  push: (payload: Record<string, unknown>) => void;
  close: () => void;
  requests: Record<string, unknown>[];
};

async function open(options: { assistant?: string | null } = {}): Promise<Harness> {
  const requests: Record<string, unknown>[] = [];
  let controller: ReturnType<typeof manualStream> | null = null;
  let turnRequestId = "";
  let detailReads = 0;

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/turns")) {
        const body = JSON.parse(String(init?.body));
        requests.push(body);
        turnRequestId = String(body.request_id);
        controller = manualStream();
        return controller.response;
      }
      if (url.endsWith(cid)) {
        detailReads += 1;
        // 第一轮读取是打开会话（还没有对话）；结果之后的读取才带回保存的正文。
        return Response.json(
          detail(detailReads > 1 ? options.assistant ?? null : null, turnRequestId),
        );
      }
      return Response.json({ items: [detail(null, "").conversation], total: 1 });
    }),
  );

  render(
    <ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />,
  );
  fireEvent.click(await screen.findByRole("button", { name: /流式正文/ }));
  await screen.findByText(/这是新会话/);

  fireEvent.change(screen.getByPlaceholderText(COMPOSER), {
    target: { value: "我要复习 Python" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(controller).not.toBeNull());

  const active = controller as unknown as ReturnType<typeof manualStream>;
  return {
    requests,
    push: (payload) => active.send(frame(turnRequestId, payload)),
    close: () => active.end(),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

test("片段到达即显示，result 后换成保存的完整正文且不重复", async () => {
  const harness = await open({ assistant: "已经确认科目是 Python。" });

  harness.push({ type: "message_delta", seq: 1, delta: "已经确认科目" });
  expect(await screen.findByText(/正在生成回复/)).toBeTruthy();
  expect(await screen.findByText(/已经确认科目/)).toBeTruthy();
  // 输入区草稿不受影响：片段不会清空或覆盖用户正在输入的内容。
  expect((screen.getByPlaceholderText(COMPOSER) as HTMLTextAreaElement).value).toBe(
    "我要复习 Python",
  );

  harness.push({ type: "message_delta", seq: 2, delta: "是 Python。" });
  await screen.findByText(/已经确认科目是 Python。/);

  harness.push({
    type: "result",
    kind: "dynamic",
    assistant_message: "已经确认科目是 Python。",
    business_status: "succeeded",
  });
  harness.close();

  // 最终只保留服务端保存的正文：临时正文区域消失，且不重复显示。
  await waitFor(() => expect(screen.queryByText(/正在生成回复/)).toBeNull());
  await waitFor(() =>
    expect(screen.getAllByText(/已经确认科目是 Python。/, { exact: false })).toHaveLength(1),
  );
  // 成功后输入区清空，说明这一轮真的结束了。
  await waitFor(() =>
    expect((screen.getByPlaceholderText(COMPOSER) as HTMLTextAreaElement).value).toBe(""),
  );
  expect(harness.requests[0].kind).toBe("auto");
  expect(Object.keys(harness.requests[0]).sort()).toEqual(["kind", "query", "request_id"]);
});

test("断流时保留片段但明确标注未保存", async () => {
  const harness = await open();

  harness.push({ type: "message_delta", seq: 1, delta: "正在整理" });
  await screen.findByText(/正在整理/);
  // 直接结束流：没有 result，属于未完成。
  harness.close();

  expect(await screen.findByText(/回复已中断，未保存/)).toBeTruthy();
  expect(screen.queryByText(/正在生成回复/)).toBeNull();
});