import { afterEach, expect, test, vi } from "vitest";
import { apiFetch, CSRF_HEADER, UNAUTHORIZED_EVENT } from "./apiFetch";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("写请求自动带 CSRF 头，GET 不带", async () => {
  const calls: Array<[RequestInfo | URL, RequestInit | undefined]> = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push([input, init]);
    return new Response("{}", { status: 200 });
  });

  await apiFetch("/api/conversations", { method: "POST" });
  await apiFetch("/api/papers", { method: "GET" });

  const postHeaders = new Headers(calls[0][1]?.headers);
  const getHeaders = new Headers(calls[1][1]?.headers);
  expect(postHeaders.get(CSRF_HEADER)).toBe("1");
  expect(getHeaders.get(CSRF_HEADER)).toBeNull();
});

test("401 派发全局未登录事件，skipAuthEvent 可关闭", async () => {
  vi.stubGlobal("fetch", async () => new Response("{}", { status: 401 }));
  const listener = vi.fn();
  window.addEventListener(UNAUTHORIZED_EVENT, listener);

  await apiFetch("/api/conversations");
  expect(listener).toHaveBeenCalledTimes(1);

  listener.mockClear();
  await apiFetch("/api/auth/login", { skipAuthEvent: true });
  expect(listener).not.toHaveBeenCalled();
  window.removeEventListener(UNAUTHORIZED_EVENT, listener);
});