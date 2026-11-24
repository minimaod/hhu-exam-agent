/** 今日用量读取与额度错误解析。 */

import { apiFetch } from "./apiFetch.ts";

export type QuotaScope = "user" | "site" | "image";

export type QuotaInfo = {
  scope: QuotaScope;
  limit: number;
  used: number;
  reset_at: string;
};

type DailyUsage = {
  limit: number | null;
  used: number;
  remaining: number | null;
};

export type UsageSnapshot = {
  date: string;
  reset_at: string;
  requests: DailyUsage;
  site_exhausted: boolean;
  images: DailyUsage;
  active_turn: boolean;
};

export type QuotaFailure = {
  message: string;
  code: string | null;
  quota: QuotaInfo | null;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function parseQuotaInfo(value: unknown): QuotaInfo | null {
  if (!isRecord(value)) return null;
  const scope = value.scope;
  if (scope !== "user" && scope !== "site" && scope !== "image") return null;
  if (
    typeof value.limit !== "number" ||
    !Number.isInteger(value.limit) ||
    value.limit < 0 ||
    typeof value.used !== "number" ||
    !Number.isInteger(value.used) ||
    value.used < 0 ||
    typeof value.reset_at !== "string" ||
    Number.isNaN(Date.parse(value.reset_at))
  ) {
    return null;
  }
  return {
    scope,
    limit: value.limit,
    used: value.used,
    reset_at: value.reset_at,
  };
}

function parseDailyUsage(value: unknown): DailyUsage | null {
  if (!isRecord(value) || typeof value.used !== "number") return null;
  const { limit, used, remaining } = value;
  if (
    (typeof limit === "number" && typeof remaining === "number") ||
    (limit === null && remaining === null)
  ) {
    return { limit, used, remaining };
  }
  return null;
}

function parseUsage(payload: unknown): UsageSnapshot {
  if (!isRecord(payload)) {
    throw new Error("后端返回的用量格式不符合约定。");
  }
  const requests = parseDailyUsage(payload.requests);
  const images = parseDailyUsage(payload.images);
  if (
    typeof payload.date !== "string" ||
    typeof payload.reset_at !== "string" ||
    !requests || !images ||
    typeof payload.site_exhausted !== "boolean" ||
    typeof payload.active_turn !== "boolean"
  ) {
    throw new Error("后端返回的用量格式不符合约定。");
  }
  return {
    date: payload.date,
    reset_at: payload.reset_at,
    requests,
    site_exhausted: payload.site_exhausted,
    images,
    active_turn: payload.active_turn,
  };
}

export async function fetchUsage(signal?: AbortSignal): Promise<UsageSnapshot> {
  const response = await apiFetch("/api/usage/today", {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    throw new Error(`读取今日用量失败（HTTP ${response.status}）。`);
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new Error("后端返回的用量不是有效 JSON。");
  }
  return parseUsage(payload);
}

export async function parseQuotaFailure(
  response: Response,
): Promise<QuotaFailure | null> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    return null;
  }
  if (!isRecord(payload)) return null;
  const message = typeof payload.detail === "string" ? payload.detail : null;
  if (!message) return null;
  return {
    message,
    code: typeof payload.code === "string" ? payload.code : null,
    quota: parseQuotaInfo(payload.quota),
  };
}

/** 到下一次北京时间零点还剩多少毫秒；用于页面保持打开时恢复输入。 */
export function millisecondsUntilReset(
  resetAt: string,
  now: number = Date.now(),
): number {
  const target = Date.parse(resetAt);
  if (Number.isNaN(target)) return 60_000;
  return Math.max(1_000, target - now);
}

export function formatResetTime(resetAt: string): string {
  const date = new Date(resetAt);
  if (Number.isNaN(date.getTime())) return "北京时间次日 00:00";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function quotaBlocksRequests(usage: UsageSnapshot | null): boolean {
  return Boolean(
    usage && ((usage.requests.remaining !== null && usage.requests.remaining <= 0) || usage.site_exhausted),
  );
}

export function quotaBlocksImages(usage: UsageSnapshot | null): boolean {
  return Boolean(usage && usage.images.remaining !== null && usage.images.remaining <= 0);
}

export function quotaReason(usage: UsageSnapshot): string | null {
  if (usage.requests.remaining !== null && usage.requests.remaining <= 0) {
    return `你今天的 ${usage.requests.limit} 次模型请求已用完`;
  }
  if (usage.site_exhausted) {
    return "全站今天的模型请求额度已用完";
  }
  return null;
}