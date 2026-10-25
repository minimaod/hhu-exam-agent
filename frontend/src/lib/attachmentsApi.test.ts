import assert from "node:assert/strict";
import test from "node:test";

import {
  attachmentUrl,
  validateLocalImage,
} from "./attachmentsApi.ts";

function fileOf(type: string, size: number): File {
  return new File([new Uint8Array(size)], "image", { type });
}

test("本地图片校验只接受允许的格式和大小", () => {
  assert.equal(validateLocalImage(fileOf("image/png", 128)), null);
  assert.equal(validateLocalImage(fileOf("image/jpeg", 128)), null);
  assert.match(validateLocalImage(fileOf("image/bmp", 128)) ?? "", /JPEG/);
  assert.match(validateLocalImage(fileOf("image/png", 7 * 1024 * 1024)) ?? "", /6 MiB/);
  assert.match(validateLocalImage(fileOf("image/png", 0)) ?? "", /空/);
});

test("附件预览地址对会话和附件 ID 编码", () => {
  assert.equal(
    attachmentUrl("conversation/1", "attachment 2"),
    "/api/conversations/conversation%2F1/attachments/attachment%202",
  );
});
