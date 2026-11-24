import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import ConversationMaterialPanel from "./ConversationMaterialPanel";
import ConversationWorkspace from "./ConversationWorkspace";
import AssistantMarkdown from "./AssistantMarkdown";
import { explanationBody } from "../lib/display";
import QuestionCardView from "./QuestionCard";
import TurnComposer from "./TurnComposer";
import type { ProblemView, QuestionCard, TutoringResult } from "../lib/conversationsApi";

describe("F017 Fix-005 前端定向回归", () => {
  test("单份讲解正文渲染步骤标题与知识点公式，损坏公式保留可读文字", () => {
    const problem: ProblemView = {
      id: "prob-1",
      root_problem_id: "prob-1",
      version: 1,
      statement_text: "求极限 $\\lim_{x\\to 0}\\frac{\\ln(1+x)}{x}$",
      status: "explained",
      clarity: "clear",
      source: { type: "none", paper_id: null, paper_title: null },
      student_answer_text: null,
      solution_text: null,
      self_reported_understood: false,
      attachments: [],
      created_at: "2026-11-20T00:00:00Z",
      updated_at: "2026-11-20T00:00:00Z",
    };
    const result: Extract<TutoringResult, { kind: "explanation" }> = {
      kind: "explanation",
      problem,
      knowledge_points: [
        { name: "等价无穷小 $\\ln(1+t)\\sim t$", explanation: "当 $t\\to 0$ 时成立，损坏如 $f(x 仍可读。" },
      ],
      steps: [
        { index: 1, title: "替换 $\\ln(1+t)$ 与矩阵 $A$", content: "直接代入即可。" },
      ],
      final_answer: "$1$",
      error_analysis: null,
      uncertainties: [],
      self_reported_understood: false,
      record_id: null,
    };

    const { container } = render(
      <AssistantMarkdown text={explanationBody(result)} />,
    );

    expect(container.querySelectorAll("h5 .katex").length).toBeGreaterThanOrEqual(2);
    expect(container.querySelectorAll(".katex").length).toBeGreaterThanOrEqual(4);
    expect(screen.getByText(/损坏如 \$f\(x 仍可读/)).toBeTruthy();
  });

  test("QuestionCard 缺结构化选项的选择题提示查看原题页并允许文本提交", () => {
    const onAction = vi.fn();
    const card: QuestionCard = {
      question_id: "q-choice-missing-opts",
      build_id: "iv-1",
      content_sha256: "sha-1",
      question_type: "choice",
      number_label: "3",
      statement: "下列极限正确的是（ ）。",
      options: [],
      code_lines: [],
      blank_marks: [],
      formulas: [],
      assets: [],
      pages: { statement: [1], answer: [] },
      knowledge_points: [{ slug: "limit", name: "极限 $A$", status: "verified" }],
      answer: { state: "original", available: true },
      review: { status: "approved", approved: true, stale: false },
      source: { paper_id: "p1", paper_title: "高数试卷", question_number: "3", may_show_answer: false },
      selected: true,
      answer_revealed: false,
      content_issues: [],
      source_views: [],
    };

    const { container } = render(<QuestionCardView card={card} busy={false} onAction={onAction} />);
    expect(container.querySelectorAll("header .katex").length).toBe(1);
    expect(screen.getByText(/结构化选项暂缺/)).toBeTruthy();
    const input = screen.getByLabelText("输入你的答案") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "C" } });
    fireEvent.click(screen.getByRole("button", { name: "提交核对" }));
    expect(onAction).toHaveBeenCalledWith("answer", card, "C");
  });

  test("TurnComposer 加号旁展示引导语，且即时展示 PDF 上传与重试状态", () => {
    const onRetry = vi.fn();
    render(
      <TurnComposer
        value=""
        running={false}
        disabled={false}
        disabledReason={null}
        activeProblem={null}
        files={[]}
        sourcePaperId=""
        sourceOptions={[]}
        pdfUploadStatus={{
          phase: "error",
          fileName: "高数期末.pdf",
          message: "上传《高数期末.pdf》失败：网络超时",
          canRetry: true,
        }}
        onRetryPdfUpload={onRetry}
        onChange={vi.fn()}
        onAddFiles={vi.fn()}
        onUploadPdf={vi.fn()}
        onRemoveFile={vi.fn()}
        onSourceChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByText("可上传题目图片、手写过程图片或 PDF")).toBeTruthy();
    expect(screen.getByText("上传《高数期末.pdf》失败：网络超时")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  test("ConversationMaterialPanel 对未检查页级文字的新资料提供检查文字识别入口", () => {
    const onCheck = vi.fn();
    render(
      <ConversationMaterialPanel
        items={[
          {
            material: {
              id: "mat-scan-1",
              display_name: "高数扫描卷.pdf",
              size_bytes: 102400,
              page_count: 4,
              file_sha256: "sha-scan-1",
              dedicated_conversation_id: null,
              is_exam: true,
              created_at: "2026-11-20T00:00:00Z",
              updated_at: "2026-11-20T00:00:00Z",
            },
            readability: null,
            attached_at: "2026-11-20T00:00:00Z",
            artifacts: [],
          },
        ]}
        generatingKey={null}
        recognizingId={null}
        checkingReadabilityId={null}
        onRecognize={vi.fn()}
        onCheckReadability={onCheck}
        quotaBlocked={false}
        onGenerate={vi.fn()}
        onOpenArtifact={vi.fn()}
        onDetach={vi.fn()}
      />,
    );

    const btn = screen.getByRole("button", { name: "检查文字识别" });
    fireEvent.click(btn);
    expect(onCheck).toHaveBeenCalledWith("mat-scan-1", 1, 4);
  });

  test("60 页 PDF 前 40 页都是扫描件，仍可切到第 41–60 页检查文字", () => {
    const onCheck = vi.fn();
    const checkedFirst40 = Array.from({ length: 40 }, (_, i) => i + 1);
    render(
      <ConversationMaterialPanel
        items={[
          {
            material: {
              id: "mat-60p",
              display_name: "高数教材60页.pdf",
              size_bytes: 204800,
              page_count: 60,
              file_sha256: "sha-60p",
              dedicated_conversation_id: null,
              is_exam: false,
              created_at: "2026-11-20T00:00:00Z",
              updated_at: "2026-11-20T00:00:00Z",
            },
            readability: {
              material_id: "mat-60p",
              page_count: 60,
              readable_pages: 0,
              total_chars: 0,
              scanned_pages: checkedFirst40,
              ocr_pages: [],
              formula_warning_pages: [],
              checked_pages: checkedFirst40,
              sufficient: false,
              summary: "已检查 40/60 页，其中 0 页可读取文字。",
              available_outputs: [],
            },
            attached_at: "2026-11-20T00:00:00Z",
            artifacts: [],
          },
        ]}
        generatingKey={null}
        recognizingId={null}
        checkingReadabilityId={null}
        onRecognize={vi.fn()}
        onCheckReadability={onCheck}
        quotaBlocked={false}
        onGenerate={vi.fn()}
        onOpenArtifact={vi.fn()}
        onDetach={vi.fn()}
      />,
    );

    // 默认页段 1-40 已全部检查，按钮不出现
    expect(screen.queryByRole("button", { name: "检查文字识别" })).toBeNull();

    // 切换到第 41-60 页，按钮应立刻出现并可触发检查
    const startInput = screen.getByLabelText("高数教材60页.pdf 起始页") as HTMLInputElement;
    const endInput = screen.getByLabelText("高数教材60页.pdf 结束页") as HTMLInputElement;
    fireEvent.change(endInput, { target: { value: "60" } });
    fireEvent.change(startInput, { target: { value: "41" } });

    const btn = screen.getByRole("button", { name: "检查文字识别" });
    fireEvent.click(btn);
    expect(onCheck).toHaveBeenCalledWith("mat-60p", 41, 60);
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function makeConvDetail(id: string, title: string, materials: unknown[] = []) {
  return {
    conversation: {
      id,
      title,
      created_at: "2026-11-20T00:00:00Z",
      updated_at: "2026-11-20T00:00:00Z",
    },
    turns: [],
    materials,
    active_problem: null,
    active_exercise: null,
    record: null,
  };
}

describe("ConversationWorkspace PDF 上传刷新失败与切会话并发控制", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    window.localStorage.clear();
  });

  test("PDF 上传成功但刷新详情失败时，先展示资料卡并提示刷新失败与重试", async () => {
    let detailCalls = 0;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/conversations" && (!init?.method || init.method === "GET")) {
        return new Response(
          JSON.stringify({
            items: [makeConvDetail("conv-a", "会话 A").conversation],
            total: 1,
          }),
          { status: 200 },
        );
      }
      if (url === "/api/conversations/conv-a") {
        detailCalls += 1;
        if (detailCalls === 1 || detailCalls >= 3) {
          return new Response(JSON.stringify(makeConvDetail("conv-a", "会话 A")), { status: 200 });
        }
        return new Response(JSON.stringify({ detail: "详情暂时不可用" }), { status: 503 });
      }
      if (url === "/api/conversations/conv-a/materials/upload" && init?.method === "POST") {
        return new Response(
          JSON.stringify({
            conversation_id: "conv-a",
            item: {
              material: {
                id: "mat-1",
                display_name: "高数期末.pdf",
                size_bytes: 2048,
                page_count: 2,
                is_exam: true,
                created_at: "2026-11-20T00:00:00Z",
                updated_at: "2026-11-20T00:00:00Z",
              },
              readability: null,
              artifacts: [],
            },
            assistant_notice: "收到《高数期末》了。",
            turn_request_id: null,
          }),
          { status: 201 },
        );
      }
      if (url === "/api/usage" || url === "/api/papers") {
        return new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 });
      }
      return new Response("{}", { status: 200 });
    }) as typeof fetch;

    render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /会话 A/, hidden: true }));
    await screen.findByPlaceholderText(/输入完整题干/);

    fireEvent.click(screen.getByRole("button", { name: "添加附件" }));
    const pdfInput = screen.getByLabelText("选择附件") as HTMLInputElement;
    const pdf = new File(["%PDF-1.4"], "高数期末.pdf", { type: "application/pdf" });
    fireEvent.change(pdfInput, { target: { files: [pdf] } });

    await screen.findByText(/《高数期末\.pdf》已上传，刷新列表失败/);
    expect(screen.getByText("高数期末.pdf")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByText("已添加《高数期末.pdf》，可以问我某道题");
  });

  test("在会话 A 上传时切换到会话 B，A 上传先完成不会让 B 卡在加载中", async () => {
    const uploadDeferred = deferred<Response>();
    const detailBDeferred = deferred<Response>();

    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/conversations" && (!init?.method || init.method === "GET")) {
        return new Response(
          JSON.stringify({
            items: [
              makeConvDetail("conv-a", "会话 A").conversation,
              makeConvDetail("conv-b", "会话 B").conversation,
            ],
            total: 2,
          }),
          { status: 200 },
        );
      }
      if (url === "/api/conversations/conv-a") {
        return new Response(JSON.stringify(makeConvDetail("conv-a", "会话 A")), { status: 200 });
      }
      if (url === "/api/conversations/conv-b") {
        return detailBDeferred.promise;
      }
      if (url === "/api/conversations/conv-a/materials/upload" && init?.method === "POST") {
        return uploadDeferred.promise;
      }
      if (url === "/api/usage" || url === "/api/papers") {
        return new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 });
      }
      return new Response("{}", { status: 200 });
    }) as typeof fetch;

    render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /会话 A/, hidden: true }));
    await screen.findByPlaceholderText(/输入完整题干/);

    fireEvent.click(screen.getByRole("button", { name: "添加附件" }));
    const pdfInput = screen.getByLabelText("选择附件") as HTMLInputElement;
    fireEvent.change(pdfInput, {
      target: { files: [new File(["%PDF-1.4"], "高数A.pdf", { type: "application/pdf" })] },
    });
    await screen.findByText("正在上传并检查《高数A.pdf》…");

    // 切换到会话 B（B 的详情请求处于挂起中）
    fireEvent.click(screen.getByRole("button", { name: /会话 B/, hidden: true }));
    expect(screen.queryByText("正在上传并检查《高数A.pdf》…")).toBeNull();

    // A 的上传先于 B 的详情返回完成
    uploadDeferred.resolve(
      new Response(
        JSON.stringify({
          conversation_id: "conv-a",
          item: {
            material: {
              id: "mat-a",
              display_name: "高数A.pdf",
              size_bytes: 1024,
              page_count: 2,
              is_exam: true,
              created_at: "2026-11-20T00:00:00Z",
              updated_at: "2026-11-20T00:00:00Z",
            },
            readability: null,
            attached_at: "2026-11-20T00:00:00Z",
            artifacts: [],
          },
          assistant_notice: "收到《高数A》了。",
          turn_request_id: null,
        }),
        { status: 201 },
      ),
    );
    await waitFor(() => {
      // 确保上传完成后仍停留在 B，且未触发第二次对 conv-a 的详情请求污染序号
      expect(screen.queryByText("高数A.pdf")).toBeNull();
    });

    // 随后 B 的详情请求完成，B 必须正常就绪而不是卡在加载中
    detailBDeferred.resolve(
      new Response(JSON.stringify(makeConvDetail("conv-b", "会话 B")), { status: 200 }),
    );
    await screen.findByText(/这是新会话/);
  });
});


describe("ConversationWorkspace 流式草稿查卷去重路径", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    window.localStorage.clear();
  });

  test("查卷流式片段等实际结果返回再显示，避免闪现重复清单", async () => {
    const streamGate = deferred<void>();
    const encoder = new TextEncoder();

    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/conversations" && (!init?.method || init.method === "GET")) {
        return new Response(
          JSON.stringify({
            items: [makeConvDetail("conv-stream", "查卷会话").conversation],
            total: 1,
          }),
          { status: 200 },
        );
      }
      if (url === "/api/conversations/conv-stream" && (!init?.method || init.method === "GET")) {
        return new Response(
          JSON.stringify(makeConvDetail("conv-stream", "查卷会话")),
          { status: 200 },
        );
      }
      if (url === "/api/conversations/conv-stream/turns" && init?.method === "POST") {
        const body = JSON.parse(String(init?.body ?? "{}"));
        const reqId = String(body.request_id || "req-1");
        const sse = new ReadableStream({
          async start(controller) {
            const send = (event: string, data: Record<string, unknown>) => {
              controller.enqueue(
                encoder.encode(`event: ${event}\ndata: ${JSON.stringify({ type: event, ...data })}\n\n`),
              );
            };
            send("progress", {
              conversation_id: "conv-stream",
              request_id: reqId,
              stage: "retrieving",
              message: "正在检索试卷资料",
            });
            send("message_delta", {
              conversation_id: "conv-stream",
              request_id: reqId,
              seq: 1,
              delta:
                "已找到 1 份高数期末试卷。\n| 试卷名称 | 学年 |\n| --- | --- |\n| 2024-2025高数AII期末考试试题 | 2024-2025 |\n- 2024-2025 高数AII 期末考试试题（[下载](/api/papers/m1/download)）\n",
            });
            await streamGate.promise;
            send("result", {
              conversation_id: "conv-stream",
              request_id: reqId,
              kind: "dynamic",
              assistant_message: "已找到 1 份高数期末试卷。",
              result: {
                type: "result",
                kind: "found",
                message: "找到 1 份【高等数学】试卷（期末）。",
                filters: { course: "高等数学", exam_type: "final" },
                total: 1,
                items: [
                  {
                    id: "m1",
                    title: "2024-2025高数AII期末考试试题",
                    course: "高等数学",
                    academic_year: "2024-2025",
                    exam_type: "final",
                    exam_date: null,
                    download_url: "/api/papers/m1/download",
                  },
                ],
              },
              suggestions: [],
              composite_results: [],
              business_status: "succeeded",
              reply_status: "complete",
            });
            controller.close();
          },
        });
        return new Response(sse, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      if (url === "/api/usage" || url === "/api/papers") {
        return new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 });
      }
      return new Response("{}", { status: 200 });
    }) as typeof fetch;

    render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /查卷会话/, hidden: true }));
    const textarea = await screen.findByPlaceholderText(/输入完整题干/);
    fireEvent.change(textarea, { target: { value: "查高数期末真题" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await screen.findByText("正在检索试卷资料");
    expect(screen.queryByTestId("assistant-draft")).toBeNull();
    expect(screen.queryByText("2024-2025高数AII期末考试试题")).toBeNull();
    expect(screen.queryByText("试卷名称")).toBeNull();

    streamGate.resolve();
    await waitFor(() => expect(screen.queryByText("正在检索试卷资料")).toBeNull());
  });
});
