import { Fragment, type ReactNode } from "react";
import MathText from "./MathText";

type ProtectedToken =
  | { kind: "math"; raw: string; display: boolean }
  | { kind: "code_inline"; code: string }
  | { kind: "code_block"; lang: string; code: string };

const TOKEN_PREFIX = "\uE000TOKEN_";
const TOKEN_SUFFIX = "\uE001";
const TOKEN_RE = /\uE000TOKEN_(\d+)\uE001/g;

// 与 MathText.tsx 保持一致的公式定界规则，额外覆盖围栏/行内代码。
const FENCE_RE = /```([A-Za-z0-9_+-]*)[ \t]*\n([\s\S]*?)(?:\n```(?=[ \t]*(?:\n|$))|$)/g;
const INLINE_CODE_RE = /`([^`\n]+)`/g;
const MATH_RE =
  /(?<!\\)\$\$([\s\S]+?)\$\$|(?<!\\)\\\[([\s\S]+?)\\\]|(?<!\\)\\\(([\s\S]+?)\\\)|(?<!\\|\$)\$(?!\$)([^\s$](?:(?:\\.|[^\\$\n])*?[^\s$])?)\$(?!\d|\$)/g;

function protectSegments(input: string): { masked: string; tokens: ProtectedToken[] } {
  const tokens: ProtectedToken[] = [];
  const push = (token: ProtectedToken): string => {
    const idx = tokens.length;
    tokens.push(token);
    return `${TOKEN_PREFIX}${idx}${TOKEN_SUFFIX}`;
  };

  let text = input.replace(/\r\n?/g, "\n");
  text = text.replace(FENCE_RE, (_m, lang: string, body: string) => {
    const token = push({
      kind: "code_block",
      lang: (lang || "").trim(),
      code: body.replace(/\n$/, ""),
    });
    return `\n\n${token}\n\n`;
  });
  text = text.replace(MATH_RE, (full, b1, b2) =>
    push({
      kind: "math",
      raw: full,
      display: b1 !== undefined || b2 !== undefined,
    }),
  );
  text = text.replace(INLINE_CODE_RE, (_m, code: string) =>
    push({ kind: "code_inline", code }),
  );
  return { masked: text, tokens };
}

function renderToken(token: ProtectedToken, key: string): ReactNode {
  if (token.kind === "math") {
    return <MathText key={key} text={token.raw} />;
  }
  if (token.kind === "code_inline") {
    return (
      <code key={key} className="assistant-inline-code">
        {token.code}
      </code>
    );
  }
  return (
    <pre key={key} className="assistant-code-block">
      {token.lang && <span className="assistant-code-lang">{token.lang}</span>}
      <code>{token.code}</code>
    </pre>
  );
}

const INLINE_SYNTAX_RE =
  /(\uE000TOKEN_\d+\uE001|\*\*(?=\S)(?:[\s\S]*?\S)\*\*|__(?=\S)(?:[\s\S]*?\S)__|\*(?=\S)(?:[^\n*]*?\S)\*)/g;

function renderInline(text: string, tokens: ProtectedToken[], keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let seq = 0;

  for (const match of text.matchAll(INLINE_SYNTAX_RE)) {
    const index = match.index ?? 0;
    if (index > cursor) {
      nodes.push(text.slice(cursor, index));
    }
    const seg = match[0];
    const key = `${keyPrefix}-${seq++}`;
    if (seg.startsWith(TOKEN_PREFIX) && seg.endsWith(TOKEN_SUFFIX)) {
      const tokenIdx = Number(seg.slice(TOKEN_PREFIX.length, -TOKEN_SUFFIX.length));
      const token = tokens[tokenIdx];
      nodes.push(token ? renderToken(token, key) : seg);
    } else if (seg.startsWith("**") || seg.startsWith("__")) {
      const inner = seg.slice(2, -2);
      nodes.push(<strong key={key}>{renderInline(inner, tokens, `${key}-b`)}</strong>);
    } else if (seg.startsWith("*")) {
      const inner = seg.slice(1, -1);
      nodes.push(<em key={key}>{renderInline(inner, tokens, `${key}-i`)}</em>);
    } else {
      nodes.push(seg);
    }
    cursor = index + seg.length;
  }

  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }
  return nodes;
}

function splitTableCells(line: string): string[] {
  let trimmed = line.trim();
  if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
  if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
  return trimmed.split("|").map((cell) => cell.trim());
}

const TABLE_ALIGN_RE = /^\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?$|^\|?\s*:?-{3,}:?\s*\|$/;

function isTableSeparator(line: string): boolean {
  const trimmed = line.trim();
  return trimmed.includes("-") && TABLE_ALIGN_RE.test(trimmed);
}

function onlyBlockToken(line: string, tokens: ProtectedToken[]): ProtectedToken | null {
  const trimmed = line.trim();
  const match = trimmed.match(/^\uE000TOKEN_(\d+)\uE001$/);
  if (!match) return null;
  const token = tokens[Number(match[1])];
  if (!token) return null;
  if (token.kind === "code_block" || (token.kind === "math" && token.display)) {
    return token;
  }
  return null;
}

const ORDERED_ITEM_RE = /^\s*(\d+)\.\s+(.*)$/;
const UNORDERED_ITEM_RE = /^\s*[-*+]\s+(.*)$/;
const HEADING_RE = /^(#{1,4})\s+(.+)$/;
const QUOTE_RE = /^>\s?(.*)$/;

function renderLinesWithBreaks(
  lines: string[],
  tokens: ProtectedToken[],
  keyPrefix: string,
): ReactNode[] {
  return lines.map((line, idx) => (
    <Fragment key={`${keyPrefix}-l-${idx}`}>
      {idx > 0 && <br />}
      {renderInline(line, tokens, `${keyPrefix}-i-${idx}`)}
    </Fragment>
  ));
}

/**
 * 在句末标点处截取评价导语，同时跳过位于 LaTeX 公式或代码片段内部的区间，
 * 避免截断破坏 `$...$` / `$$...$$` 渲染。
 */
export function extractSafeLead(text: string, maxChars = 110): string | null {
  const clean = text.trim();
  if (!clean) return null;

  const protectedRanges: Array<[number, number]> = [];
  for (const re of [FENCE_RE, MATH_RE, INLINE_CODE_RE]) {
    re.lastIndex = 0;
    for (const m of clean.matchAll(re)) {
      const start = m.index ?? 0;
      protectedRanges.push([start, start + m[0].length]);
    }
  }
  const insideProtected = (pos: number) =>
    protectedRanges.some(([start, end]) => pos >= start && pos < end);

  for (let i = 0; i < Math.min(clean.length, maxChars); i++) {
    if ("。！？!?".includes(clean[i]) && !insideProtected(i)) {
      return clean.slice(0, i + 1);
    }
  }
  return null;
}

export default function AssistantMarkdown({ text }: { text: string }) {
  if (!text) return null;
  const { masked, tokens } = protectSegments(text);
  const lines = masked.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }

    const loneToken = onlyBlockToken(line, tokens);
    if (loneToken) {
      blocks.push(renderToken(loneToken, `blk-${i}`));
      i++;
      continue;
    }

    const headingMatch = line.match(HEADING_RE);
    if (headingMatch) {
      const level = headingMatch[1].length;
      const content = renderInline(headingMatch[2], tokens, `h-${i}`);
      if (level === 1) blocks.push(<h3 key={`h-${i}`}>{content}</h3>);
      else if (level === 2) blocks.push(<h4 key={`h-${i}`}>{content}</h4>);
      else blocks.push(<h5 key={`h-${i}`}>{content}</h5>);
      i++;
      continue;
    }

    if (line.includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const headers = splitTableCells(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        rows.push(splitTableCells(lines[i]));
        i++;
      }
      blocks.push(
        <div key={`tbl-${i}`} className="assistant-table-wrap">
          <table className="assistant-table">
            <thead>
              <tr>
                {headers.map((cell, cIdx) => (
                  <th key={cIdx}>{renderInline(cell, tokens, `th-${i}-${cIdx}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, rIdx) => (
                <tr key={rIdx}>
                  {headers.map((_, cIdx) => (
                    <td key={cIdx}>
                      {renderInline(row[cIdx] ?? "", tokens, `td-${i}-${rIdx}-${cIdx}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    if (QUOTE_RE.test(line)) {
      const quoteLines: string[] = [];
      const start = i;
      while (i < lines.length && QUOTE_RE.test(lines[i])) {
        quoteLines.push(lines[i].replace(QUOTE_RE, "$1"));
        i++;
      }
      blocks.push(
        <blockquote key={`q-${start}`} className="assistant-quote">
          {renderLinesWithBreaks(quoteLines, tokens, `q-${start}`)}
        </blockquote>,
      );
      continue;
    }

    if (ORDERED_ITEM_RE.test(line)) {
      const start = i;
      const firstMatch = line.match(ORDERED_ITEM_RE);
      const startNum = firstMatch ? Number(firstMatch[1]) : 1;
      const items: string[] = [];
      while (i < lines.length) {
        const m = lines[i].match(ORDERED_ITEM_RE);
        if (m) {
          items.push(m[2]);
          i++;
        } else if (lines[i].trim() && /^\s{2,}\S/.test(lines[i]) && items.length > 0) {
          items[items.length - 1] += "\n" + lines[i].trim();
          i++;
        } else {
          break;
        }
      }
      blocks.push(
        <ol key={`ol-${start}`} start={startNum > 1 ? startNum : undefined}>
          {items.map((item, idx) => (
            <li key={idx}>
              {renderLinesWithBreaks(item.split("\n"), tokens, `oli-${start}-${idx}`)}
            </li>
          ))}
        </ol>,
      );
      continue;
    }

    if (UNORDERED_ITEM_RE.test(line)) {
      const start = i;
      const items: string[] = [];
      while (i < lines.length) {
        const m = lines[i].match(UNORDERED_ITEM_RE);
        if (m) {
          items.push(m[1]);
          i++;
        } else if (lines[i].trim() && /^\s{2,}\S/.test(lines[i]) && items.length > 0) {
          items[items.length - 1] += "\n" + lines[i].trim();
          i++;
        } else {
          break;
        }
      }
      blocks.push(
        <ul key={`ul-${start}`}>
          {items.map((item, idx) => (
            <li key={idx}>
              {renderLinesWithBreaks(item.split("\n"), tokens, `uli-${start}-${idx}`)}
            </li>
          ))}
        </ul>,
      );
      continue;
    }

    const start = i;
    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !onlyBlockToken(lines[i], tokens) &&
      !HEADING_RE.test(lines[i]) &&
      !QUOTE_RE.test(lines[i]) &&
      !ORDERED_ITEM_RE.test(lines[i]) &&
      !UNORDERED_ITEM_RE.test(lines[i]) &&
      !(lines[i].includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1]))
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    blocks.push(
      <p key={`p-${start}`}>{renderLinesWithBreaks(paraLines, tokens, `p-${start}`)}</p>,
    );
  }

  return <div className="assistant-markdown">{blocks}</div>;
}

export { TOKEN_RE };
