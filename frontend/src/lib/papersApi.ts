/**
 * 试卷查询与下载的接口客户端。
 *
 * 页面只调用这里导出的函数，不自己拼请求地址、不直接读 JSON。
 * 这样接口变化、错误文案和响应校验都集中在一个文件里，
 * 以后换成 SSE 或加入自然语言查询时，页面代码不需要跟着改。
 */

export type ExamType = "midterm" | "final";

/** 筛选用的类型值：all 表示不按类型过滤。 */
export type ExamTypeFilter = ExamType | "all";

export type PaperItem = {
  id: string;
  title: string;
  course: string;
  academic_year: string;
  exam_type: ExamType;
  /** 未知时为 null；后端不会用猜测值填空。 */
  exam_date: string | null;
  download_url: string;
};

export type PaperListResponse = {
  items: PaperItem[];
  total: number;
};

export type PaperQuery = {
  course?: string;
  examType?: ExamTypeFilter;
};

const PAPERS_PATH = "/api/papers";

export class PapersApiError extends Error {
  readonly status: number | undefined;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "PapersApiError";
    this.status = status;
  }
}

function isExamType(value: unknown): value is ExamType {
  return value === "midterm" || value === "final";
}

export function isPaperItem(value: unknown): value is PaperItem {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const record = value as Record<string, unknown>;
  return (
    typeof record.id === "string" &&
    typeof record.title === "string" &&
    typeof record.course === "string" &&
    typeof record.academic_year === "string" &&
    isExamType(record.exam_type) &&
    (record.exam_date === null || typeof record.exam_date === "string") &&
    typeof record.download_url === "string"
  );
}

function isPaperListResponse(value: unknown): value is PaperListResponse {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const record = value as Record<string, unknown>;
  return (
    Array.isArray(record.items) &&
    record.items.every(isPaperItem) &&
    typeof record.total === "number" &&
    record.total === record.items.length
  );
}

/** 读取后端错误体里的 detail；取不到时给出状态码文案。 */
export async function readErrorDetail(response: Response): Promise<string> {
  try {
    const payload: unknown = await response.json();
    if (typeof payload === "object" && payload !== null) {
      const detail = (payload as Record<string, unknown>).detail;
      if (typeof detail === "string" && detail.trim()) {
        return detail;
      }
    }
  } catch {
    // 错误体不是 JSON 时继续使用下面的兜底文案。
  }

  return `请求失败（HTTP ${response.status}），请稍后重试。`;
}

export async function fetchPapers(
  query: PaperQuery = {},
  signal?: AbortSignal,
): Promise<PaperListResponse> {
  const params = new URLSearchParams();
  const course = query.course?.trim();
  if (course) {
    params.set("course", course);
  }
  if (query.examType && query.examType !== "all") {
    params.set("exam_type", query.examType);
  }

  const search = params.toString();
  const url = search ? `${PAPERS_PATH}?${search}` : PAPERS_PATH;

  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }

    throw new PapersApiError(
      "无法连接到后端服务。请确认 FastAPI 已在 127.0.0.1:8000 启动。",
    );
  }

  if (!response.ok) {
    throw new PapersApiError(await readErrorDetail(response), response.status);
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new PapersApiError("后端响应不是有效 JSON，无法读取试卷目录。");
  }

  if (!isPaperListResponse(payload)) {
    throw new PapersApiError("后端返回的试卷列表格式不符合约定。");
  }

  return payload;
}

/** 从 Content-Disposition 中取回原文件名（后端用 RFC 5987 编码中文名）。 */
function filenameFromDisposition(
  header: string | null,
  fallback: string,
): string {
  if (!header) {
    return fallback;
  }

  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded?.[1]) {
    try {
      return decodeURIComponent(encoded[1]);
    } catch {
      return fallback;
    }
  }

  const plain = /filename="([^"]+)"/i.exec(header) ?? /filename=([^;]+)/i.exec(header);
  return plain?.[1]?.trim() || fallback;
}

/**
 * 下载一份试卷；失败时抛出可展示的错误。
 *
 * 用 fetch + Blob 而不是普通 <a href>，是因为只有这样才能在失败时
 * 读出后端错误并提示用户；文件名仍取自响应头，保持原文件名。
 */
export async function downloadPaper(item: PaperItem): Promise<void> {
  let response: Response;
  try {
    response = await fetch(item.download_url, {
      method: "GET",
      headers: { Accept: "application/pdf" },
      cache: "no-store",
    });
  } catch {
    throw new PapersApiError("下载请求没有到达后端，请检查服务是否在运行。");
  }

  if (!response.ok) {
    throw new PapersApiError(await readErrorDetail(response), response.status);
  }

  const blob = await response.blob();
  if (blob.size === 0) {
    throw new PapersApiError("后端返回的文件为空，下载未完成。");
  }

  const filename = filenameFromDisposition(
    response.headers.get("content-disposition"),
    `${item.title}.pdf`,
  );

  const objectUrl = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.rel = "noopener";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    // 延迟回收：部分浏览器在点击后立刻 revoke 会导致保存失败。
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }
}
