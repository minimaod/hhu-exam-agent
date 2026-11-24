import katex from "katex";
import { Fragment } from "react";

const MATH_PATTERN =
  /(?<!\\)\$\$([\s\S]+?)\$\$|(?<!\\)\\\[([\s\S]+?)\\\]|(?<!\\)\\\(([\s\S]+?)\\\)|(?<!\\|\$)\$(?!\$)([^\s$](?:(?:\\.|[^\\$\n])*?[^\s$])?)\$(?!\d|\$)/g;

function renderFormula(latex: string, original: string, displayMode: boolean) {
  try {
    const html = katex.renderToString(latex, {
      displayMode,
      throwOnError: false,
      errorColor: "var(--katex-parse-error)",
      trust: false,
      output: "htmlAndMathml",
    });
    // KaTeX 在容错模式下分别标记解析错误和无法识别的命令。
    if (!html.startsWith('<span class="katex-error"') && !html.includes("var(--katex-parse-error)")) {
      return (
        <span
          className={displayMode ? "math-display" : undefined}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      );
    }
  } catch {
    // 渲染异常时也走 React 文本节点，原文始终按文本转义。
  }
  return original;
}

export default function MathText({
  text,
  forceMath = false,
  display = false,
}: {
  text: string;
  /** 已知整段就是公式（如 QuestionCard.formulas）时直接按公式渲染。 */
  forceMath?: boolean;
  display?: boolean;
}) {
  const matches = [...text.matchAll(MATH_PATTERN)];
  if (matches.length === 0) {
    if (forceMath && !/^(?:\$|\\[([])/.test(text) && text.trim()) {
      return renderFormula(text, text, display);
    }
    return text;
  }

  let end = 0;
  return (
    <>
      {matches.map((match) => {
        const start = match.index;
        const preceding = text.slice(end, start);
        end = start + match[0].length;
        const block = match[1] ?? match[2];
        return (
          <Fragment key={start}>
            {preceding}
            {renderFormula(block ?? match[3] ?? match[4], match[0], block !== undefined)}
          </Fragment>
        );
      })}
      {text.slice(end)}
    </>
  );
}
