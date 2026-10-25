import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import PaperCard from "./PaperCard";
import type { PaperItem } from "../lib/papersApi";

const paper: PaperItem = {
  id: "paper-internal-id",
  title: "高等数学（上）期末试卷",
  course: "高等数学",
  academic_year: "2024-2025",
  exam_type: "final",
  exam_date: null,
  download_url: "/api/papers/paper-internal-id/download",
};

describe("试卷卡片：日期与下载反馈", () => {
  test("不展示内部试卷 ID，未登记日期如实说明", () => {
    const { container } = render(
      <PaperCard paper={paper} download={null} onDownload={vi.fn()} />,
    );
    expect(screen.getByText("日期未登记")).toBeTruthy();
    expect(container.textContent).not.toContain("paper-internal-id");
  });

  test("下载中禁用按钮，重复点击不会再次触发", () => {
    const onDownload = vi.fn();
    render(
      <PaperCard
        paper={paper}
        download={{ phase: "downloading" }}
        onDownload={onDownload}
      />,
    );

    const button = screen.getByRole("button", { name: "正在下载…" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(button);
    expect(onDownload).not.toHaveBeenCalled();
  });

  test("只提示已开始下载，请用户到浏览器确认，不声称已保存", () => {
    render(
      <PaperCard
        paper={paper}
        download={{
          phase: "triggered",
          message: "已开始下载，请在浏览器下载栏确认是否保存成功。",
        }}
        onDownload={vi.fn()}
      />,
    );

    const status = screen.getByRole("status");
    expect(status.textContent).toContain("请在浏览器下载栏确认");
    expect(status.textContent).not.toContain("已保存到");
  });

  test("下载失败显示原因并可重试", () => {
    const onDownload = vi.fn();
    render(
      <PaperCard
        paper={paper}
        download={{ phase: "error", message: "文件不存在或已被移动。" }}
        onDownload={onDownload}
      />,
    );

    expect(screen.getByRole("alert").textContent).toContain("文件不存在");
    fireEvent.click(screen.getByRole("button", { name: "重新下载" }));
    expect(onDownload).toHaveBeenCalledWith(paper);
  });
});