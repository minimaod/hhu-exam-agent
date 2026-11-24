/** 统一请求入口：同源 Cookie、CSRF 头和 401 通知。 */

export const CSRF_HEADER = "X-HHU-CSRF";
export const UNAUTHORIZED_EVENT = "hhu:unauthorized";

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export type ApiFetchOptions = RequestInit & { skipAuthEvent?: boolean };

export async function apiFetch(
  input: RequestInfo | URL,
  init: ApiFetchOptions = {},
): Promise<Response> {
  const { skipAuthEvent = false, ...requestInit } = init;
  const method = (requestInit.method ?? "GET").toUpperCase();
  const headers = new Headers(requestInit.headers);
  if (UNSAFE.has(method)) {
    headers.set(CSRF_HEADER, "1");
  }
  const response = await fetch(input, { ...requestInit, headers });
  if (!skipAuthEvent && response.status === 401) {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
  return response;
}