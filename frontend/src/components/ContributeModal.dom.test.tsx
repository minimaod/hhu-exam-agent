import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ContributeModal from "./ContributeModal.tsx";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ContributeModal", () => {
  it("validates required PDF, course, and year before submitting", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(<ContributeModal onClose={() => {}} />);

    expect(
      screen.getByText(
        "提交后，项目维护者会审核这份试卷。审核通过的题目可在站内公共题库检索。",
      ),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "确认提交" }));
    expect(screen.getByRole("alert").textContent).toContain("请选择一份 PDF 试卷文件");
    expect(fetchMock).not.toHaveBeenCalled();

    const fileInput = screen.getByLabelText("试卷 PDF（必填）") as HTMLInputElement;
    const pdf = new File(["%PDF-1.4\n"], "2024高数.pdf", { type: "application/pdf" });
    fireEvent.change(fileInput, { target: { files: [pdf] } });

    fireEvent.click(screen.getByRole("button", { name: "确认提交" }));
    expect(screen.getByRole("alert").textContent).toContain("请填写学科名称");

    fireEvent.change(screen.getByLabelText("学科（必填）"), {
      target: { value: "高等数学" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认提交" }));
    expect(screen.getByRole("alert").textContent).toContain("请填写试卷年份");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("submits contribution and shows pending review message", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          contribution_id: "c-1",
          course: "高等数学",
          year: "2024",
          status: "pending_review",
          message: "已提交，等待审核",
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ContributeModal onClose={() => {}} />);

    const fileInput = screen.getByLabelText("试卷 PDF（必填）") as HTMLInputElement;
    const pdf = new File(["%PDF-1.4\n"], "2024高数.pdf", { type: "application/pdf" });
    fireEvent.change(fileInput, { target: { files: [pdf] } });
    fireEvent.change(screen.getByLabelText("学科（必填）"), {
      target: { value: "高等数学" },
    });
    fireEvent.change(screen.getByLabelText("年份（必填）"), {
      target: { value: "2024" },
    });

    fireEvent.click(screen.getByRole("button", { name: "确认提交" }));

    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toContain("已提交，等待审核");
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/contributions");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
  });

  it("shows server error message when submission fails", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({ detail: "仅支持有效的 PDF 文件。" }),
        { status: 422, headers: { "Content-Type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ContributeModal onClose={() => {}} />);

    const fileInput = screen.getByLabelText("试卷 PDF（必填）") as HTMLInputElement;
    const fake = new File(["not-a-pdf"], "fake.pdf", { type: "application/pdf" });
    fireEvent.change(fileInput, { target: { files: [fake] } });
    fireEvent.change(screen.getByLabelText("学科（必填）"), {
      target: { value: "大学物理" },
    });
    fireEvent.change(screen.getByLabelText("年份（必填）"), {
      target: { value: "2023" },
    });

    fireEvent.click(screen.getByRole("button", { name: "确认提交" }));

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("仅支持有效的 PDF 文件");
    });
  });
});
