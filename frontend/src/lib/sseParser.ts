/**
 * 增量 SSE 帧解析。
 *
 * 只负责把字节流解码后的文本切成一条条 data 负载；JSON 解析和业务字段时间
 * 校验在 queryApi.ts。单独成模块是为了能直接测试分块边界（中文跨块、CRLF、
 * 一条事件被拆成多块等）。
 */

export type SseParser = {
  /** 送入一段文本，返回其中已经完整的事件负载。 */
  push(chunk: string): string[];
  /** 流结束时调用；若还有未闭合的帧则抛错。 */
  flush(): string[];
};

const FRAME_SEPARATOR = /\r?\n\r?\n/;

/** 从一帧里取出 data 行，按 SSE 规则用换行拼接；没有 data 时返回 null。 */
function parseFrame(frame: string): string | null {
  const dataLines: string[] = [];

  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith(":")) {
      continue; // 注释/保活行
    }
    if (line === "data") {
      dataLines.push("");
      continue;
    }
    if (line.startsWith("data:")) {
      let value = line.slice("data:".length);
      if (value.startsWith(" ")) {
        value = value.slice(1);
      }
      dataLines.push(value);
    }
    // event:/id:/retry: 本接口不使用，忽略。
  }

  return dataLines.length > 0 ? dataLines.join("\n") : null;
}

export function createSseParser(): SseParser {
  let buffer = "";
  let finished = false;

  function extract(): string[] {
    const payloads: string[] = [];

    while (true) {
      const match = FRAME_SEPARATOR.exec(buffer);
      if (!match) {
        break;
      }

      const frame = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);

      const data = parseFrame(frame);
      if (data !== null) {
        payloads.push(data);
      }
    }

    return payloads;
  }

  return {
    push(chunk) {
      if (finished) {
        throw new Error("SSE 解析器已经结束。");
      }
      buffer += chunk;
      return extract();
    },
    flush() {
      finished = true;
      const payloads = extract();
      if (buffer.trim()) {
        buffer = "";
        throw new Error("最后一条 SSE 事件不完整。");
      }
      return payloads;
    },
  };
}
