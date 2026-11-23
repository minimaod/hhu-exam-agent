import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import TurnComposer from "./TurnComposer";
import type { ComponentProps } from "react";
import type { ProblemView } from "../lib/conversationsApi";

function setup(props: Partial<ComponentProps<typeof TurnComposer>> = {}) {
  const onChange = vi.fn();
  const onSubmit = vi.fn();
  const view = render(
    <TurnComposer
      value=""
      running={false}
      disabled={false}
      disabledReason={null}
      activeProblem={null}
      files={[]}
      sourcePaperId=""
      sourceOptions={[]}
      onChange={onChange}
      onAddFiles={vi.fn()}
      onRemoveFile={vi.fn()}
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

describe("输入区：发送、换行和附件", () => {
  test("Enter 发送，Shift+Enter 换行，长按 Enter 不重复发送", () => {
    const { onSubmit, textarea } = setup({ value: "print(1)" });

    expect(fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true })).toBe(true);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(fireEvent.keyDown(textarea, { key: "Enter" })).toBe(false);
    expect(onSubmit).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(textarea, { key: "Enter", repeat: true });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  test("中文输入法组合期间不发送，组合结束后恢复", () => {
    const { onSubmit, textarea } = setup({ value: "极限" });

    fireEvent.compositionStart(textarea);
    expect(fireEvent.keyDown(textarea, { key: "Enter" })).toBe(true);
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.compositionEnd(textarea);
    fireEvent.keyDown(textarea, { key: "Enter", keyCode: 229 });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(textarea, { key: "Enter" });
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
    expect(screen.getByText("Enter 发送 · Shift+Enter 换行")).toBeTruthy();
  });

  test("加号直接打开唯一选择器，图片和 PDF 走各自入口", () => {
    const onAddFiles = vi.fn();
    const onUploadPdf = vi.fn();
    setup({ onAddFiles, onUploadPdf });
    const picker = screen.getByLabelText("选择附件") as HTMLInputElement;
    const openPicker = vi.spyOn(picker, "click");
    fireEvent.click(screen.getByRole("button", { name: "添加附件" }));
    expect(openPicker).toHaveBeenCalledOnce();
    expect(picker.accept).toContain("application/pdf");
    expect(picker.accept).toContain("image/png");
    expect(screen.queryByRole("button", { name: "补充答案/过程" })).toBeNull();
    expect(screen.queryByLabelText("解法图")).toBeNull();
    const image = new File(["image"], "解法.png", { type: "image/png" });
    const pdf = new File(["%PDF-1.4"], "高数.pdf");
    fireEvent.change(picker, { target: { files: [image, pdf] } });
    expect(onAddFiles).toHaveBeenCalledWith([image], "statement");
    expect(onUploadPdf).toHaveBeenCalledWith(pdf);
    const secondPdf = new File(["%PDF-1.4"], "另一份.pdf", { type: "application/pdf" });
    fireEvent.change(picker, { target: { files: [pdf, secondPdf] } });
    expect(onUploadPdf).toHaveBeenCalledOnce();
    expect(screen.getByText("PDF 每次上传一份，请重新选择。")).toBeTruthy();
  });

  test("已移除追问/补充/纠正/新题输入模式按钮", () => {
    setup({
      activeProblem: {
        id: "p1",
        version: 2,
        status: "explained",
      } as unknown as ProblemView,
    });

    for (const label of ["追问", "补充", "纠正", "新题"]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
  });

  test("特殊输入模式常驻提示并能退出", () => {
    const onResetMode = vi.fn();
    setup({
      mode: "correction",
      targetProblem: { id: "p2", version: 3 } as ProblemView,
      onResetMode,
    });

    expect(screen.getByText("正在纠正第 3 版题干")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "退出模式" }));
    expect(onResetMode).toHaveBeenCalledTimes(1);
  });

  test("统一文件选择器在模型额度用完时仍可上传 PDF", () => {
    const onUploadPdf = vi.fn();
    setup({
      onUploadPdf,
      disabled: true,
      quotaBlocked: true,
      disabledReason: null,
    });

    const trigger = screen.getByRole("button", { name: "添加附件" });
    fireEvent.click(trigger);
    const pdfInput = screen.getByLabelText("选择附件") as HTMLInputElement;
    expect(pdfInput.disabled).toBe(false);

    const pdf = new File(["%PDF-1.4"], "高数讲义.pdf", { type: "application/pdf" });
    fireEvent.change(pdfInput, { target: { files: [pdf] } });
    expect(onUploadPdf).toHaveBeenCalledWith(pdf);
    expect(pdfInput.accept).not.toContain("image/");
  });
});
