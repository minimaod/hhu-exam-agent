import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import TurnComposer from "./TurnComposer";
import type { ComponentProps } from "react";

function setup(props: Partial<ComponentProps<typeof TurnComposer>> = {}) {
  const onChange = vi.fn();
  const onSubmit = vi.fn();
  const view = render(
    <TurnComposer
      value=""
      running={false}
      disabled={false}
      disabledReason={null}
      confirmedCourse={null}
      activeProblem={null}
      mode="auto"
      files={[]}
      studentAnswer=""
      solutionText=""
      sourcePaperId=""
      sourceOptions={[]}
      onChange={onChange}
      onModeChange={vi.fn()}
      onAddFiles={vi.fn()}
      onRemoveFile={vi.fn()}
      onStudentAnswerChange={vi.fn()}
      onSolutionTextChange={vi.fn()}
      onSourceChange={vi.fn()}
      onSubmit={onSubmit}
      onCancel={vi.fn()}
      {...props}
    />,
  );
  const textarea = screen.getByPlaceholderText(
    /输入完整题干|围绕当前题目/,
  ) as HTMLTextAreaElement;
  return { ...view, onChange, onSubmit, textarea };
}

describe("输入区：快捷键、输入法状态和示例", () => {
  test("Enter 保持换行，Ctrl/⌘ + Enter 才发送", () => {
    const { onSubmit, textarea } = setup({ value: "print(1)" });

    expect(fireEvent.keyDown(textarea, { key: "Enter" })).toBe(true);
    expect(onSubmit).not.toHaveBeenCalled();

    expect(
      fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true }),
    ).toBe(false);
    expect(onSubmit).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });
    expect(onSubmit).toHaveBeenCalledTimes(2);
  });

  test("中文输入法组合期间不发送，组合结束后恢复", () => {
    const { onSubmit, textarea } = setup({ value: "极限" });

    fireEvent.compositionStart(textarea);
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.compositionEnd(textarea);
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  test("多行代码原样保留换行", () => {
    const onChange = vi.fn();
    const { textarea } = setup({ onChange });

    fireEvent.change(textarea, {
      target: { value: "def f(x):\n    return x + 1" },
    });
    expect(onChange).toHaveBeenCalledWith("def f(x):\n    return x + 1");
  });

  test("输入区提示区分处理中、读取中和没有会话", () => {
    const running = setup({ running: true, disabled: true });
    expect(screen.getByText("正在处理，请稍候…")).toBeTruthy();
    running.unmount();

    const loading = setup({ disabled: true, disabledReason: "loading" });
    expect(screen.getByText("正在读取会话记录…")).toBeTruthy();
    loading.unmount();

    setup({ disabled: true, disabledReason: "no-conversation" });
    expect(screen.getByText("请先新建或选择会话")).toBeTruthy();
  });

  test("空闲时明确写出发送和换行快捷键", () => {
    setup();
    expect(
      screen.getByText(/Enter 换行，Ctrl\/⌘ \+ Enter 发送/),
    ).toBeTruthy();
  });

  test("固定示例随已确认课程调整，没有课程时不写死高数", () => {
    const withCourse = setup({ confirmedCourse: "高数" });
    expect(screen.getByText("示例（固定）")).toBeTruthy();
    expect(screen.getByRole("button", { name: "查看高数试卷" })).toBeTruthy();
    withCourse.unmount();

    setup();
    expect(screen.getByRole("button", { name: "帮我找期末试卷" })).toBeTruthy();
    expect(screen.queryByText(/高数/)).toBeNull();
  });
});