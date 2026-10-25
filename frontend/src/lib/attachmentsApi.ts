/** 会话图片上传与预览地址。 */

import { readErrorDetail } from "./papersApi.ts";
import type { AttachmentMeta } from "./conversationsApi.ts";

const MAX_IMAGE_BYTES = 6 * 1024 * 1024;
const ALLOWED = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);

export class AttachmentsApiError extends Error {
  readonly status: number | undefined;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "AttachmentsApiError";
    this.status = status;
  }
}

export function attachmentUrl(conversationId: string, attachmentId: string): string {
  return `/api/conversations/${encodeURIComponent(conversationId)}/attachments/${encodeURIComponent(attachmentId)}`;
}

export function validateLocalImage(file: File): string | null {
  if (!ALLOWED.has(file.type)) {
    return "只支持 JPEG、PNG、GIF、WebP 图片。";
  }
  if (file.size <= 0) {
    return "图片内容为空。";
  }
  if (file.size > MAX_IMAGE_BYTES) {
    return "单张图片不能超过 6 MiB。";
  }
  return null;
}

export async function uploadAttachment(
  conversationId: string,
  file: File,
  signal?: AbortSignal,
): Promise<AttachmentMeta> {
  const localError = validateLocalImage(file);
  if (localError) {
    throw new AttachmentsApiError(localError);
  }

  const form = new FormData();
  form.append("file", file, file.name || "problem.png");

  let response: Response;
  try {
    response = await fetch(
      `/api/conversations/${encodeURIComponent(conversationId)}/attachments`,
      {
        method: "POST",
        body: form,
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal,
      },
    );
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new AttachmentsApiError("无法上传图片，请确认后端正在运行。");
  }

  if (!response.ok) {
    throw new AttachmentsApiError(await readErrorDetail(response), response.status);
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new AttachmentsApiError("后端返回的图片信息不是有效 JSON。");
  }
  if (!isAttachmentMeta(payload)) {
    throw new AttachmentsApiError("后端返回的图片信息格式不符合约定。");
  }
  return payload;
}

function isAttachmentMeta(value: unknown): value is AttachmentMeta {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    typeof record.id === "string" &&
    typeof record.mime === "string" &&
    typeof record.width === "number" &&
    typeof record.height === "number" &&
    typeof record.size_bytes === "number" &&
    typeof record.created_at === "string"
  );
}
