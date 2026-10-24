/**
 * 健康检查接口客户端。
 *
 * 页面只依赖这个函数，不直接拼接请求地址；这样以后增加接口或统一处理
 * 错误时，改动集中在一处。开发环境下 /api 由 Vite 代理到 FastAPI。
 */

const HEALTH_PATH = "/api/health";

const EXPECTED_STATUS = "ok";
const EXPECTED_SERVICE = "hhu-exam-review";

export type HealthResponse = {
  status: typeof EXPECTED_STATUS;
  service: typeof EXPECTED_SERVICE;
};

export class HealthCheckError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "HealthCheckError";
  }
}

function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const record = value as Record<string, unknown>;
  return record.status === EXPECTED_STATUS && record.service === EXPECTED_SERVICE;
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  let response: Response;

  try {
    response = await fetch(HEALTH_PATH, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }

    throw new HealthCheckError(
      "无法连接到后端服务。请确认 FastAPI 已在 127.0.0.1:8000 启动。",
    );
  }

  if (!response.ok) {
    throw new HealthCheckError(
      `后端返回了 HTTP ${response.status}，健康检查未通过。`,
    );
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new HealthCheckError("后端响应不是有效 JSON，无法确认服务状态。");
  }

  if (!isHealthResponse(payload)) {
    throw new HealthCheckError(
      "后端响应格式不符合约定（需要 status=ok、service=hhu-exam-review）。",
    );
  }

  return payload;
}
