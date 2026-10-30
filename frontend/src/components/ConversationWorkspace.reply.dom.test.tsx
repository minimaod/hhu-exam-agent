/**
 * F008 复核修复：正文状态的公开表达与切换会话时的临时正文清理。
 *
 * 覆盖两点：
 * 1. 回复失败/被截断时，页面明确提示“正文没生成完整”，并提供“重新生成回复”；
 *    重做只调用只读正文接口，不重跑工具。
 * 2. 切换会话会清掉上一轮还在流式显示的临时正文，旧文字不能留在新会话页面上。
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import ConversationWorkspace from "./ConversationWorkspace";
import type { ConversationDetail } from "../lib/conversationsApi";

const cid = "11111111-1111-4111-8111-111111111111";
const cid2 = "33333333-3333-4333-8333-333333333333";
const date = "2026-10-30T08:00:00+00:00";
const COMPOSER = /输入完整题干|围绕当前题目|输入题目或复习需求/;

function summary(id: string, title: string) {
  return { id, title, created_at: date, updated_at: date };
}

function detailWith(
  id: string,
  assistant: string | null,
  reply: { status: "complete" | "truncated" | "failed"; retryable: boolean } | null,
): ConversationDetail {
  return {
    conversation: summary(id, id === cid ? "第一个会话" : "第二个会话"),
    turns: assistant
      ? [
          {
            request_id: "22222222-2222-4222-8222-222222222222",
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
            reply_status: reply?.status ?? null,
            reply_retryable: reply?.retryable ?? false,
            business_status: "succeeded",
            composite_results: [],
          } as ConversationDetail["turns"][number],
        ]
      : [],
    active_problem: null,
    active_exercise: null,
    record: null,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

test("回复未生成完整时给出公开提示，并可只重做正文", async () => {
  let regenerated = false;
  const fetchMock = vi.fn(async (url: string) => {
    if (url.endsWith("/reply")) {
      regenerated = true;
      return Response.json({
        request_id: "22222222-2222-4222-8222-222222222222",
        assistant_message: "已经确认科目是 Python，接下来先做两套真题。",
        reply_status: "complete",
        reply_retryable: false,
      });
    }
    if (url.endsWith("/turns")) {
      return new Response("", { status: 202 });
    }
    if (url.endsWith(cid)) {
      return Response.json(
        detailWith(
          cid,
          regenerated
            ? "已经确认科目是 Python，接下来先做两套真题。"
            : "已为您处理完成。",
          regenerated ? { status: "complete", retryable: false } : { status: "failed", retryable: true },
        ),
      );
    }
    return Response.json({ items: [summary(cid, "第一个会话")], total: 1 });
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: /第一个会话/ }));

  expect(await screen.findByText(/回复没有生成成功/)).toBeTruthy();
  // 业务状态仍然是成功：正文不完整不等于操作失败。
  expect(screen.getByText("成功")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "重新生成回复" }));

  await waitFor(() =>
    expect(screen.getByText(/已经确认科目是 Python，接下来先做两套真题。/)).toBeTruthy(),
  );
  expect(screen.queryByText(/回复没有生成成功/)).toBeNull();
  expect(regenerated).toBe(true);
});

test("切换会话会清掉上一轮未保存的流式正文", async () => {
  const requests: Record<string, unknown>[] = [];
  let controller: {
    response: Response;
    send: (bytes: Uint8Array) => void;
    end: () => void;
  } | null = null;
  let turnRequestId = "";
  let detailReads = 0;

  const stream = () => {
    const queue: Uint8Array[] = [];
    let pending: ((value: ReadableStreamReadResult<Uint8Array>) => void) | null = null;
    let closed = false;
    const deliver = (result: ReadableStreamReadResult<Uint8Array>) => {
      if (!pending) return false;
      const resolve = pending;
      pending = null;
      resolve(result);
      return true;
    };
    const reader = {
      read: async () => {
        if (queue.length > 0) return { value: queue.shift() as Uint8Array, done: false } as const;
        if (closed) return { value: undefined, done: true } as const;
        return new Promise<ReadableStreamReadResult<Uint8Array>>((resolve) => {
          pending = resolve;
        });
      },
      cancel: async () => {},
      releaseLock: () => {},
    };
    return {
      response: {
        ok: true,
        status: 200,
        headers: new Headers({ "Content-Type": "text/event-stream" }),
        body: { getReader: () => reader },
      } as unknown as Response,
      send(bytes: Uint8Array) {
        if (!deliver({ value: bytes, done: false })) queue.push(bytes);
      },
      end() {
        closed = true;
        deliver({ value: undefined, done: true });
      },
    };
  };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/turns")) {
        turnRequestId = String(JSON.parse(String(init?.body)).request_id);
        requests.push(JSON.parse(String(init?.body)));
        controller = stream();
        return controller.response;
      }
      if (url.endsWith(cid) || url.endsWith(cid2)) {
        detailReads += 1;
        const id = url.endsWith(cid2) ? cid2 : cid;
        return Response.json(
          detailReads > 2 ? detailWith(id, null, null) : detailWith(id, null, null),
        );
      }
      return Response.json({
        items: [summary(cid, "第一个会话"), summary(cid2, "第二个会话")],
        total: 2,
      });
    }),
  );

  render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: /第一个会话/ }));
  await screen.findByText(/这是新会话/);

  fireEvent.change(screen.getByPlaceholderText(COMPOSER), {
    target: { value: "我要复习 Python" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(controller).not.toBeNull());

  const active = controller as unknown as ReturnType<typeof stream>;
  active.send(
    new TextEncoder().encode(
      `data: ${JSON.stringify({
        type: "message_delta",
        conversation_id: cid,
        request_id: turnRequestId,
        seq: 1,
        delta: "这段文字还没有保存",
      })}\n\n`,
    ),
  );
  await screen.findByText(/这段文字还没有保存/);

  // 切到另一个会话：临时正文必须消失，不能跟着跑到新会话页面上。
  fireEvent.click(screen.getByRole("button", { name: /第二个会话/ }));
  await waitFor(() => expect(screen.queryByText(/这段文字还没有保存/)).toBeNull());
  expect(screen.queryByText(/正在生成回复/)).toBeNull();
  expect(requests).toHaveLength(1);
});