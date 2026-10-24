/**
 * POST /api/query 的客户端。
 *
 * 页面只调用 streamQuery：它负责发请求、增量解码 UTF-8、解析 SSE、校验事件，
 * 并且只有收到一条合法的 result 才返回；error 或提前断流都会抛出 QueryApiError。
 */

import {
  isPaperItem,
  readErrorDetail,
  type ExamType,
  type PaperItem,
} from "./papersApi.ts";
import { createSseParser } from "./sseParser.ts";

const QUERY_PATH = "/api/query";
const MAX_QUERY_LENGTH = 1000;

export type ProgressStage = "understanding" | "retrieving";
export type QueryKind = "found" | "empty" | "clarification" | "unsupported";
export type QueryErrorCode =
  | "MODEL_UNAVAILABLE"
  | "MODEL_AUTH_FAILED"
  | "MODEL_RATE_LIMITED"
  | "MODEL_TIMEOUT"
  | "MODEL_RESPONSE_INVALID"
  | "CATALOG_UNAVAILABLE"
  | "QUERY_FAILED";

export type QueryFilters = {
  course: string | null;
  exam_type: ExamType | null;
};

export type ProgressEvent = {
  type: "progress";
  stage: ProgressStage;
  message: string;
};

export type ResultEvent = {
  type: "result";
  kind: QueryKind;
  message: string;
  filters: QueryFilters | null;
  items: PaperItem[];
  total: number;
};

export type ErrorEvent = {
  type: "error";
  code: QueryErrorCode;
  message: string;
};

export type QueryEvent = ProgressEvent | ResultEvent | ErrorEvent;

export type StreamQueryOptions = {
  signal?: AbortSignal;
  onProgress?: (event: ProgressEvent) => void;
};

export class QueryApiError extends Error {
  readonly code: string | undefined;
  readonly status: number | undefined;

  constructor(
    message: string,
    options: { code?: string; status?: number } = {},
  ) {
    super(message);
    this.name = "QueryApiError";
    this.code = options.code;
    this.status = options.status;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isExamType(value: unknown): value is ExamType {
  return value === "midterm" || value === "final";
}

function isProgressStage(value: unknown): value is ProgressStage {
  return value === "understanding" || value === "retrieving";
}

function isQueryKind(value: unknown): value is QueryKind {
  return (
    value === "found" ||
    value === "empty" ||
    value === "clarification" ||
    value === "unsupported"
  );
}

function isQueryErrorCode(value: unknown): value is QueryErrorCode {
  return (
    value === "MODEL_UNAVAILABLE" ||
    value === "MODEL_AUTH_FAILED" ||
    value === "MODEL_RATE_LIMITED" ||
    value === "MODEL_TIMEOUT" ||
    value === "MODEL_RESPONSE_INVALID" ||
    value === "CATALOG_UNAVAILABLE" ||
    value === "QUERY_FAILED"
  );
}

function isFilters(value: unknown): value is QueryFilters {
  if (!isRecord(value)) {
    return false;
  }
  return (
    (value.course === null || typeof value.course === "string") &&
    (value.exam_type === null || isExamType(value.exam_type))
  );
}

function isResultEvent(value: unknown): value is ResultEvent {
  if (!isRecord(value) || value.type !== "result") {
    return false;
  }
  if (!isQueryKind(value.kind) || typeof value.message !== "string") {
    return false;
  }
  if (!Array.isArray(value.items) || !value.items.every(isPaperItem)) {
    return false;
  }
  if (value.total !== value.items.length) {
    return false;
  }
  if (!(value.filters === null || isFilters(value.filters))) {
    return false;
  }

  if (value.kind === "found") {
    return value.items.length > 0 && value.filters !== null;
  }
  if (value.kind === "empty") {
    return value.items.length === 0 && value.filters !== null;
  }
  return value.items.length === 0 && value.filters === null;
}

function parseQueryEvent(raw: string): QueryEvent {
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    throw new QueryApiError("后端返回的查询事件不是有效 JSON。");
  }

  if (!isRecord(payload)) {
    throw new QueryApiError("后端返回的查询事件格式不符合约定。");
  }

  if (payload.type === "progress") {
    if (
      !isProgressStage(payload.stage) ||
      typeof payload.message !== "string"
    ) {
      throw new QueryApiError("后端返回的进度事件格式不符合约定。");
    }
    return {
      type: "progress",
      stage: payload.stage,
      message: payload.message,
    };
  }

  if (payload.type === "error") {
    if (
      !isQueryErrorCode(payload.code) ||
      typeof payload.message !== "string"
    ) {
      throw new QueryApiError("后端返回的错误事件格式不符合约定。");
    }
    return { type: "error", code: payload.code, message: payload.message };
  }

  if (!isResultEvent(payload)) {
    throw new QueryApiError("后端返回的查询结果格式不符合约定。");
  }
  return payload;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/**
 * 提交一次自然语言查询，返回最终业务结果。
 *
 * 成功只以合法 result 为准；HTTP 200 或连接正常结束都不代表查询成功。
 */
export async function streamQuery(
  query: string,
  options: StreamQueryOptions = {},
): Promise<ResultEvent> {
  const text = query.trim();
  if (!text) {
    throw new QueryApiError("请输入要查询的内容。");
  }
  if (text.length > MAX_QUERY_LENGTH) {
    throw new QueryApiError(`问题最长为 ${MAX_QUERY_LENGTH} 个字符。`);
  }

  let response: Response;
  try {
    response = await fetch(QUERY_PATH, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({ query: text }),
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (isAbortError(error)) {
      throw error;
    }
    throw new QueryApiError(
      "无法连接到后端服务。请确认 FastAPI 已在 127.0.0.1:8000 启动。",
    );
  }

  if (!response.ok) {
    throw new QueryApiError(await readErrorDetail(response), {
      status: response.status,
    });
  }
  if (!response.body) {
    throw new QueryApiError("后端没有返回可读取的查询流。");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  const parser = createSseParser();

  const consume = (payloads: string[]): ResultEvent | null => {
    for (const payload of payloads) {
      const event = parseQueryEvent(payload);
      if (event.type === "progress") {
        options.onProgress?.(event);
        continue;
      }
      if (event.type === "error") {
        throw new QueryApiError(event.message, { code: event.code });
      }
      return event;
    }
    return null;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();

      if (done) {
        const tail = decoder.decode();
        if (tail) {
          const tailResult = consume(parser.push(tail));
          if (tailResult) {
            return tailResult;
          }
        }

        let flushed: string[];
        try {
          flushed = parser.flush();
        } catch {
          throw new QueryApiError("连接提前结束，最后一条事件不完整。", {
            code: "STREAM_INCOMPLETE",
          });
        }

        const flushedResult = consume(flushed);
        if (flushedResult) {
          return flushedResult;
        }

        throw new QueryApiError("连接在返回最终结果前结束，查询未完成。", {
          code: "STREAM_INCOMPLETE",
        });
      }

      const result = consume(parser.push(decoder.decode(value, { stream: true })));
      if (result) {
        return result;
      }
    }
  } catch (error) {
    if (error instanceof QueryApiError || isAbortError(error)) {
      throw error;
    }
    throw new QueryApiError("读取查询结果时连接中断，请重试。");
  } finally {
    try {
      await reader.cancel();
    } catch {
      // 连接可能已经关闭，无需再处理。
    }
  }
}
