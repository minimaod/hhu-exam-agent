/** F012 认证客户端。 */

import { apiFetch } from "./apiFetch.ts";

export type AuthUser = {
  id: string;
  username: string;
  created_at: string;
};

export class AuthApiError extends Error {
  readonly status: number | undefined;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "AuthApiError";
    this.status = status;
  }
}

async function readDetail(response: Response): Promise<string> {
  try {
    const payload: unknown = await response.json();
    if (typeof payload === "object" && payload !== null) {
      const detail = (payload as Record<string, unknown>).detail;
      if (typeof detail === "string" && detail.trim()) {
        return detail;
      }
    }
  } catch {
    // 继续使用兜底文案。
  }
  return `请求失败（HTTP ${response.status}），请稍后重试。`;
}

function isUser(value: unknown): value is AuthUser {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    typeof record.id === "string" &&
    typeof record.username === "string" &&
    typeof record.created_at === "string"
  );
}

export async function fetchMe(signal?: AbortSignal): Promise<AuthUser | null> {
  const response = await apiFetch("/api/me", {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal,
    skipAuthEvent: true,
  });
  if (response.status === 401) {
    return null;
  }
  if (!response.ok) {
    throw new AuthApiError(await readDetail(response), response.status);
  }
  const payload: unknown = await response.json();
  if (!isUser(payload)) {
    throw new AuthApiError("后端返回的用户信息格式不符合约定。");
  }
  return payload;
}

async function authenticate(
  path: string,
  body: Record<string, string>,
): Promise<AuthUser> {
  const response = await apiFetch(path, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
    skipAuthEvent: true,
  });
  if (!response.ok) {
    throw new AuthApiError(await readDetail(response), response.status);
  }
  const payload: unknown = await response.json();
  if (!isUser(payload)) {
    throw new AuthApiError("后端返回的用户信息格式不符合约定。");
  }
  return payload;
}

export function login(username: string, password: string): Promise<AuthUser> {
  return authenticate("/api/auth/login", { username, password });
}

export function register(
  inviteCode: string,
  username: string,
  password: string,
): Promise<AuthUser> {
  return authenticate("/api/auth/register", {
    invite_code: inviteCode,
    username,
    password,
  });
}

export async function logout(): Promise<void> {
  const response = await apiFetch("/api/auth/logout", {
    method: "POST",
    cache: "no-store",
    skipAuthEvent: true,
  });
  if (response.status === 401 || response.status === 204) {
    return;
  }
  if (!response.ok) {
    throw new AuthApiError(await readDetail(response), response.status);
  }
}