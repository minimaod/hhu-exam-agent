import { render } from "@testing-library/react";
import { expect, test } from "vitest";
import MathText from "./MathText";

test("保留普通文本并渲染四种公式定界符", () => {
  const { container } = render(
    <MathText text={String.raw`求值 $x^2$，\(\sin x\)，$$\frac{1}{2}$$，\[\sqrt 3\]`} />,
  );
  expect(container.textContent).toContain("求值 ");
  expect(container.querySelectorAll(".katex-mathml")).toHaveLength(4);
  expect(container.querySelectorAll(".katex-display")).toHaveLength(2);
});

test.each([
  "价格 $100 与 $200",
  String.raw`未闭合 \[x^2`,
  String.raw`非法 $\frac{1}{<img src=x onerror=alert(1)>$`,
  String.raw`未知命令 $\invalidcommand{x}$`,
])("保留原文 %s", (text) => {
  const { container } = render(<MathText text={text} />);
  expect(container.textContent).toBe(text);
  expect(container.querySelector("*")).toBeNull();
});

test("题库的独立公式支持无定界符的 LaTeX", () => {
  const { container } = render(<MathText text={String.raw`\frac{1}{2}`} forceMath display />);
  expect(container.querySelector(".katex-display")).toBeTruthy();
});
