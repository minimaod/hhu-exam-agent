import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import MaterialWorkspace from "./MaterialWorkspace.tsx";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((complete) => { resolve = complete; });
  return { promise, resolve };
}

function savedGuide(id: string, startPage: number, title: string) {
  return {
    id,
    material_id: "m1",
    file_sha256: "sha1",
    start_page: startPage,
    end_page: startPage + 2,
    rule_version: "guide-v1",
    conversation_id: "c1",
    status: "ready",
    reused: true,
    readability: null,
    items: [{
      id: `item-${id}`,
      category: "concept",
      title,
      source_excerpt: "",
      ai_explanation: "知识点说明",
      practice_prompt: "练习方向",
      verified_citations: [],
    }],
  };
}

function renderWithSavedGuides(delayed: Readonly<Partial<Record<string, Promise<Response>>>>) {
  const material = {
    id: "m1", display_name: "高数.pdf", file_sha256: "sha1", size_bytes: 2048, page_count: 12,
  };
  const firstGuide = savedGuide("g1", 1, "第一页组知识点");
  const secondGuide = savedGuide("g2", 4, "第二页组知识点");
  const responses: Record<string, unknown> = {
    "/api/materials/pdfs": { items: [material] },
    "/api/materials/pdfs/m1": { material, guides: [firstGuide, secondGuide] },
    "/api/materials/guides/g1": firstGuide,
    "/api/materials/guides/g2": secondGuide,
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const pendingResponse = delayed[url];
    if (pendingResponse) return pendingResponse;
    if (url in responses) return jsonResponse(responses[url]);
    return jsonResponse({ detail: `unexpected ${url}` }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  const onStartStudy = vi.fn();
  const { unmount } = render(<MaterialWorkspace onStartStudy={onStartStudy} />);
  return { fetchMock, onStartStudy, unmount, firstGuide };
}

async function openHistory() {
  fireEvent.click(await screen.findByRole("button", { name: /^历史清单/ }));
}

const firstGuideStudyUrl = "/api/materials/guides/g1/items/item-g1/study";
const firstGuideStudyResponse = {
  conversation_id: "c1",
  mode: "explain",
  guide_id: "g1",
  item_id: "item-g1",
  prompt: "请讲解我选中的资料知识点",
};

describe("MaterialWorkspace", () => {
  it("keeps a manually selected guide when the default guide arrives later", async () => {
    const defaultGuide = deferResponse();
    const defaultUrl = "/api/materials/guides/g1";
    const { fetchMock, firstGuide } = renderWithSavedGuides({ [defaultUrl]: defaultGuide.promise });
    await openHistory();
    fireEvent.click(await screen.findByRole("button", { name: "第 4–6 页" }));
    await screen.findByText("第二页组知识点");
    expect(fetchMock.mock.calls.some(([input]) => input === defaultUrl)).toBe(true);

    await act(async () => { defaultGuide.resolve(jsonResponse(firstGuide)); });
    expect(screen.getByText("第二页组知识点")).toBeTruthy();
    expect(screen.queryByText("第一页组知识点")).toBeNull();
  });

  it("shows actual outputs, lets students preview a partial result, and retries the same range", async () => {
    const pdf = { id: "m1", display_name: "大学物理.pdf", page_count: 36, size_bytes: 1024 };
    const failed = {
      id: "a1", material_id: "m1", title: "静电场复习", status: "failed", current_step: "quiz",
      start_page: 1, end_page: 36, course_hint: "物理", is_sample: false,
      notes_html: "<h2>高斯定理笔记</h2><p>对称性与电通量。</p>",
      graph_tree: { nodes: [{ id: "n1", label: "高斯定理", kc_id: "kc1", summary: "电通量", children: [] }] },
      graph_notes: {}, coverage_warnings: [], questions: [], error_message: "[quiz] internal error",
    };
    const ready = { ...failed, status: "ready", current_step: "completed", questions: [{
      id: "q1", q_type: "tf", stem: "高斯定理测试题", source_pages: [], points: 5,
    }] };
    let generated = 0;
    const retry = deferResponse();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/materials/pdfs") return jsonResponse({ items: [pdf] });
      if (url === "/api/materials/pdfs/m1") return jsonResponse({ material: pdf, artifacts: [], guides: [savedGuide("g1", 1, "历史笔记")] });
      if (url === "/api/materials/guides/g1") return jsonResponse(savedGuide("g1", 1, "历史笔记"));
      if (url === "/api/materials/pdfs/m1/artifacts") {
        generated += 1;
        if (generated === 1) return jsonResponse(failed);
        expect(JSON.parse(String(init?.body))).toEqual({start_page: 1, end_page: 36, course_hint: "物理"});
        return retry.promise;
      }
      return jsonResponse({ detail: "unexpected request" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<MaterialWorkspace onStartStudy={vi.fn()} />);
    await screen.findByRole("heading", { name: pdf.display_name });
    expect(screen.queryByText("历史笔记")).toBeNull();
    expect(screen.queryByRole("button", { name: /导入|生成章节复习清单/ })).toBeNull();
    for (const output of ["章节笔记", "章节测试", "知识图谱"]) expect(screen.getByText(output)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "生成复习资料" }));
    await screen.findByText("章节测试生成失败");
    expect(screen.getByText("章节笔记和知识图谱可预览。整套复习资料尚未完成。")).toBeTruthy();
    expect(screen.queryByText(/internal error/)).toBeNull();
    expect(screen.queryByRole("complementary", { name: "原件页码对照" })).toBeNull();
    expect(screen.getByRole("heading", { name: "高斯定理笔记" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /知识图谱/ }));
    expect(screen.getByRole("list", { name: "知识点目录" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /章节测试/ }));
    expect(screen.queryByRole("button", { name: "提交本题" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重试章节测试" }));
    expect(screen.getByText("正在重试…")).toBeTruthy();
    await act(async () => { retry.resolve(jsonResponse(ready)); });
    await screen.findByText("高斯定理测试题");
    expect(screen.getByText("已完成")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /返回资料/ }));
    await openHistory();
    expect(screen.getByText("历史笔记")).toBeTruthy();
  });

  it("does not start a conversation after leaving the material workspace", async () => {
    const pendingStudy = deferResponse();
    const { fetchMock, onStartStudy, unmount } = renderWithSavedGuides({
      [firstGuideStudyUrl]: pendingStudy.promise,
    });
    await openHistory();
    fireEvent.click(await screen.findByRole("button", { name: "继续讲解" }));
    expect(fetchMock.mock.calls.some(([input]) => input === firstGuideStudyUrl)).toBe(true);
    unmount();

    await act(async () => { pendingStudy.resolve(jsonResponse(firstGuideStudyResponse)); });
    expect(onStartStudy).not.toHaveBeenCalled();
  });

  it("does not jump to a previous guide's conversation after selecting a different guide", async () => {
    const pendingStudy = deferResponse();
    const { fetchMock, onStartStudy } = renderWithSavedGuides({
      [firstGuideStudyUrl]: pendingStudy.promise,
    });
    await openHistory();
    fireEvent.click(await screen.findByRole("button", { name: "继续讲解" }));
    expect(fetchMock.mock.calls.some(([input]) => input === firstGuideStudyUrl)).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "第 4–6 页" }));
    await screen.findByText("第二页组知识点");

    await act(async () => { pendingStudy.resolve(jsonResponse(firstGuideStudyResponse)); });
    expect(onStartStudy).not.toHaveBeenCalled();
  });

  it("renders guide items with source/AI/practice badges, citation modal, study jump and delete impact", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();

      if (url === "/api/materials/pdfs" && method === "GET") {
        return jsonResponse({
          items: [
            {
              id: "m1",
              display_name: "高数第一章.pdf",
              file_sha256: "sha1",
              size_bytes: 20480,
              page_count: 12,
              dedicated_conversation_id: "c1",
              created_at: "2026-11-16T00:00:00Z",
              updated_at: "2026-11-16T00:00:00Z",
            },
          ],
        });
      }
      if (url === "/api/materials/pdfs/m1" && method === "GET") {
        return jsonResponse({
          material: {
            id: "m1",
            display_name: "高数第一章.pdf",
            file_sha256: "sha1",
            size_bytes: 20480,
            page_count: 12,
            dedicated_conversation_id: "c1",
            created_at: "2026-11-16T00:00:00Z",
            updated_at: "2026-11-16T00:00:00Z",
          },
          guides: [
            {
              id: "g1",
              material_id: "m1",
              start_page: 1,
              end_page: 5,
              rule_version: "guide-v1",
              conversation_id: "c1",
              status: "ready",
              error_code: null,
              error_message: null,
              created_at: "2026-11-16T00:00:00Z",
              updated_at: "2026-11-16T00:00:00Z",
            },
          ],
        });
      }
      if (url === "/api/materials/guides/g1" && method === "GET") {
        return jsonResponse({
          id: "g1",
          material_id: "m1",
          file_sha256: "sha1",
          start_page: 1,
          end_page: 5,
          rule_version: "guide-v1",
          conversation_id: "c1",
          status: "ready",
          reused: true,
          readability: null,
          items: [
            {
              id: "i1",
              sort_order: 0,
              category: "concept",
              title: "夹逼准则",
              source_excerpt: "若 f(x) <= g(x) <= h(x) 且两端极限相同",
              ai_explanation: "用两个更简单的函数夹住目标函数。",
              practice_prompt: "求数列和极限",
              verified_citations: [{ page_no: 3, quote: "f(x) <= g(x) <= h(x)", verified: true }],
            },
          ],
          error_code: null,
          error_message: null,
          created_at: "2026-11-16T00:00:00Z",
          updated_at: "2026-11-16T00:00:00Z",
        });
      }
      if (url === "/api/materials/pdfs/m1/pages/3" && method === "GET") {
        return jsonResponse({
          material_id: "m1",
          page_no: 3,
          char_count: 120,
          scanned_suspect: false,
          garbled_ratio: 0,
          formula_warning: false,
          warnings: [],
          extracted_text: "第三页正文：若 f(x) <= g(x) <= h(x) 且两端极限相同，则中间极限存在。",
        });
      }
      if (url === "/api/materials/guides/g1/items/i1/study" && method === "POST") {
        return jsonResponse({
          conversation_id: "c1",
          mode: "explain",
          guide_id: "g1",
          item_id: "i1",
          prompt: "请讲解我选中的资料知识点",
        });
      }
      if (url === "/api/materials/pdfs/m1/delete-impact" && method === "GET") {
        return jsonResponse({
          material_id: "m1",
          display_name: "高数第一章.pdf",
          guide_count: 1,
          conversation_id: "c1",
          exercise_count: 2,
          mistake_count: 1,
          attachment_count: 0,
        });
      }
      if (url === "/api/materials/pdfs/m1" && method === "DELETE") {
        return jsonResponse({ deleted: true, material_id: "m1" });
      }
      return jsonResponse({ detail: `unexpected ${method} ${url}` }, 404);
    });

    vi.stubGlobal("fetch", fetchMock);
    const onStartStudy = vi.fn();

    render(<MaterialWorkspace onStartStudy={onStartStudy} />);
    await openHistory();

    await waitFor(() => {
      expect(screen.getByText("夹逼准则")).toBeTruthy();
    });
    expect(screen.getByText("资料原文")).toBeTruthy();
    expect(screen.getByText("AI 解释")).toBeTruthy();
    expect(screen.getByText("模型生成练习方向")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "第 3 页原文" }));
    await waitFor(() => {
      expect(screen.getByText(/第三页正文/)).toBeTruthy();
    });
    expect(screen.getByRole("link", { name: "预览原件 PDF（第 3 页）" }).getAttribute("href")).toBe(
      "/api/materials/pdfs/m1/file#page=3",
    );
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));

    fireEvent.click(screen.getByRole("button", { name: "继续讲解" }));
    await waitFor(() => {
      expect(onStartStudy).toHaveBeenCalledWith("c1", "请讲解我选中的资料知识点", {
        guideId: "g1",
        itemId: "i1",
        mode: "explain",
      });
    });

    fireEvent.click(screen.getByRole("button", { name: "删除资料" }));
    await waitFor(() => {
      expect(screen.getByText(/确认删除个人资料/)).toBeTruthy();
    });
    expect(screen.getByText(/该会话下的练习记录：2 道/)).toBeTruthy();
    expect(screen.getByText(/该会话下的错题记录：1 条/)).toBeTruthy();
  });

  it("ignores stale artifact response when switching materials before generation finishes", async () => {
    let resolveGuideA!: (res: Response) => void;
    const guideAPromise = new Promise<Response>((resolve) => {
      resolveGuideA = resolve;
    });

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url === "/api/materials/pdfs" && method === "GET") {
        return jsonResponse({
          items: [
            {
              id: "mA",
              display_name: "高数A.pdf",
              file_sha256: "shaA",
              size_bytes: 1024,
              page_count: 10,
              dedicated_conversation_id: "cA",
              created_at: "2026-11-16T00:00:00Z",
              updated_at: "2026-11-16T00:00:00Z",
            },
            {
              id: "mB",
              display_name: "物理B.pdf",
              file_sha256: "shaB",
              size_bytes: 1024,
              page_count: 8,
              dedicated_conversation_id: "cB",
              created_at: "2026-11-16T00:00:00Z",
              updated_at: "2026-11-16T00:00:00Z",
            },
          ],
        });
      }
      if (url === "/api/materials/pdfs/mA" && method === "GET") {
        return jsonResponse({
          material: {
            id: "mA",
            display_name: "高数A.pdf",
            file_sha256: "shaA",
            size_bytes: 1024,
            page_count: 10,
            dedicated_conversation_id: "cA",
            created_at: "2026-11-16T00:00:00Z",
            updated_at: "2026-11-16T00:00:00Z",
          },
          guides: [],
        });
      }
      if (url === "/api/materials/pdfs/mB" && method === "GET") {
        return jsonResponse({
          material: {
            id: "mB",
            display_name: "物理B.pdf",
            file_sha256: "shaB",
            size_bytes: 1024,
            page_count: 8,
            dedicated_conversation_id: "cB",
            created_at: "2026-11-16T00:00:00Z",
            updated_at: "2026-11-16T00:00:00Z",
          },
          guides: [],
        });
      }
      if (url === "/api/materials/pdfs/mA/artifacts" && method === "POST") {
        return guideAPromise;
      }
      return jsonResponse({ detail: `unexpected ${method} ${url}` }, 404);
    });

    vi.stubGlobal("fetch", fetchMock);
    render(<MaterialWorkspace onStartStudy={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("heading", { name: "高数A.pdf" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "生成复习资料" }));

    // 生成 A 期间切换到资料 B
    fireEvent.click(screen.getByRole("button", { name: /物理B\.pdf/ }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "物理B.pdf" })).toBeTruthy());

    // A 的迟到响应到达
    resolveGuideA(
      jsonResponse({
        id: "aA",
        title: "高数A迟到成果",
        notes_html: "<h2>高数A迟到知识点</h2>",
        material_id: "mA",
        file_sha256: "shaA",
        start_page: 1,
        end_page: 10,
        rule_version: "guide-v1",
        conversation_id: "cA",
        status: "ready",
        reused: false,
        readability: null,
        items: [
          {
            id: "iA",
            sort_order: 0,
            category: "concept",
            title: "高数A迟到知识点",
            source_excerpt: "A原文",
            ai_explanation: "A解释",
            practice_prompt: "A练习",
            verified_citations: [{ page_no: 3, quote: "A原文", verified: true }],
          },
        ],
        error_code: null,
        error_message: null,
        created_at: "2026-11-16T00:00:00Z",
        updated_at: "2026-11-16T00:00:00Z",
      }),
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "物理B.pdf" })).toBeTruthy();
    });
    expect(screen.queryByText("高数A迟到知识点")).toBeNull();
  });

  it("ignores stale delete impact, citation modal reopen, and study jump responses", async () => {
    let resolveDeleteImpactA!: (res: Response) => void;
    let resolvePageA!: (res: Response) => void;
    let resolveStudyA!: (res: Response) => void;

    const deleteImpactPromiseA = new Promise<Response>((r) => {
      resolveDeleteImpactA = r;
    });
    const pagePromiseA = new Promise<Response>((r) => {
      resolvePageA = r;
    });
    const studyPromiseA = new Promise<Response>((r) => {
      resolveStudyA = r;
    });

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url === "/api/materials/pdfs" && method === "GET") {
        return jsonResponse({
          items: [
            {
              id: "mA",
              display_name: "高数A.pdf",
              file_sha256: "shaA",
              size_bytes: 1024,
              page_count: 10,
              dedicated_conversation_id: "cA",
              created_at: "2026-11-16T00:00:00Z",
              updated_at: "2026-11-16T00:00:00Z",
            },
            {
              id: "mB",
              display_name: "物理B.pdf",
              file_sha256: "shaB",
              size_bytes: 1024,
              page_count: 8,
              dedicated_conversation_id: "cB",
              created_at: "2026-11-16T00:00:00Z",
              updated_at: "2026-11-16T00:00:00Z",
            },
          ],
        });
      }
      if (url === "/api/materials/pdfs/mA" && method === "GET") {
        return jsonResponse({
          material: {
            id: "mA",
            display_name: "高数A.pdf",
            file_sha256: "shaA",
            size_bytes: 1024,
            page_count: 10,
            dedicated_conversation_id: "cA",
            created_at: "2026-11-16T00:00:00Z",
            updated_at: "2026-11-16T00:00:00Z",
          },
          guides: [
            {
              id: "gA",
              material_id: "mA",
              start_page: 1,
              end_page: 5,
              rule_version: "guide-v1",
              conversation_id: "cA",
              status: "ready",
              error_code: null,
              error_message: null,
              created_at: "2026-11-16T00:00:00Z",
              updated_at: "2026-11-16T00:00:00Z",
            },
          ],
        });
      }
      if (url === "/api/materials/guides/gA" && method === "GET") {
        return jsonResponse({
          id: "gA",
          material_id: "mA",
          file_sha256: "shaA",
          start_page: 1,
          end_page: 5,
          rule_version: "guide-v1",
          conversation_id: "cA",
          status: "ready",
          reused: true,
          readability: null,
          items: [
            {
              id: "iA",
              sort_order: 0,
              category: "concept",
              title: "知识点A",
              source_excerpt: "原文A",
              ai_explanation: "解释A",
              practice_prompt: "练习A",
              verified_citations: [{ page_no: 2, quote: "原文A", verified: true }],
            },
          ],
          error_code: null,
          error_message: null,
          created_at: "2026-11-16T00:00:00Z",
          updated_at: "2026-11-16T00:00:00Z",
        });
      }
      if (url === "/api/materials/pdfs/mB" && method === "GET") {
        return jsonResponse({
          material: {
            id: "mB",
            display_name: "物理B.pdf",
            file_sha256: "shaB",
            size_bytes: 1024,
            page_count: 8,
            dedicated_conversation_id: "cB",
            created_at: "2026-11-16T00:00:00Z",
            updated_at: "2026-11-16T00:00:00Z",
          },
          guides: [],
        });
      }
      if (url === "/api/materials/pdfs/mA/pages/2" && method === "GET") {
        return pagePromiseA;
      }
      if (url === "/api/materials/guides/gA/items/iA/study" && method === "POST") {
        return studyPromiseA;
      }
      if (url === "/api/materials/pdfs/mA/delete-impact" && method === "GET") {
        return deleteImpactPromiseA;
      }
      return jsonResponse({ detail: `unexpected ${method} ${url}` }, 404);
    });

    vi.stubGlobal("fetch", fetchMock);
    const onStartStudy = vi.fn();
    render(<MaterialWorkspace onStartStudy={onStartStudy} />);
    await openHistory();

    await waitFor(() => expect(screen.getByText("知识点A")).toBeTruthy());

    // 1. 打开 A 的引文后立即关闭弹窗，A 的迟到响应不应重开弹窗
    fireEvent.click(screen.getByRole("button", { name: "第 2 页原文" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog")).toBeNull();

    // 2. 在 A 点击继续讲解与删除资料，趁响应未回切到 B
    fireEvent.click(screen.getByRole("button", { name: "继续讲解" }));
    fireEvent.click(screen.getByRole("button", { name: "删除资料" }));
    fireEvent.click(screen.getByRole("button", { name: /物理B\.pdf/ }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "物理B.pdf" })).toBeTruthy());

    // 3. A 的三个迟到响应同时到达
    resolvePageA(
      jsonResponse({
        material_id: "mA",
        page_no: 2,
        char_count: 50,
        scanned_suspect: false,
        garbled_ratio: 0,
        formula_warning: false,
        warnings: [],
        extracted_text: "迟到页文本A",
      }),
    );
    resolveStudyA(
      jsonResponse({
        conversation_id: "cA",
        mode: "explain",
        guide_id: "gA",
        item_id: "iA",
        prompt: "请讲解我选中的资料知识点",
      }),
    );
    resolveDeleteImpactA(
      jsonResponse({
        material_id: "mA",
        display_name: "高数A.pdf",
        guide_count: 1,
        conversation_id: "cA",
        exercise_count: 0,
        mistake_count: 0,
        attachment_count: 0,
      }),
    );

    await waitFor(() => expect(screen.getByRole("heading", { name: "物理B.pdf" })).toBeTruthy());
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(/确认删除个人资料/)).toBeNull();
    expect(onStartStudy).not.toHaveBeenCalled();
  });

  it("ignores late quiz attempt responses after switching to another material", async () => {
    const matA = {
      id: "mA", display_name: "物理A.pdf", file_sha256: "shaA", size_bytes: 2048, page_count: 36,
    };
    const matB = {
      id: "mB", display_name: "高数B.pdf", file_sha256: "shaB", size_bytes: 1024, page_count: 10,
    };
    const artA = {
      id: "artA",
      material_id: "mA",
      file_sha256: "shaA",
      start_page: 1,
      end_page: 36,
      rule_version: "exampass-sample-v1",
      course_hint: "",
      title: "静电场复习样例",
      is_sample: true,
      status: "ready",
      current_step: "completed",
      reused: false,
      conversation_id: "cA",
      skeleton: null,
      notes_html: "<h2>静电场</h2><p>内容A</p>",
      graph_tree: { title: "静电场", nodes: [{ id: "n1", label: "静电场", summary: "", children: [] }] },
      coverage_warnings: [],
      questions: [
        {
          id: "qA1",
          sort_order: 1,
          kc_id: "kc1.1",
          q_type: "tf",
          stem: "测试题目A",
          options: null,
          pitfall: "",
          cognitive: "remember",
          difficulty: "basic",
          points: 5,
          source_pages: [3],
          attempt: null,
          revealed_answer: null,
          revealed_explanation: null,
        },
      ],
      graph_notes: {},
      error_code: null,
      error_message: null,
      created_at: "2026-11-18T00:00:00Z",
      updated_at: "2026-11-18T00:00:00Z",
    };

    const pendingAttemptA = deferResponse();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/materials/pdfs") return jsonResponse({ items: [matA, matB] });
      if (url === "/api/materials/pdfs/mA") {
        return jsonResponse({ material: matA, guides: [], artifacts: [artA] });
      }
      if (url === "/api/materials/artifacts/artA") return jsonResponse(artA);
      if (url === "/api/materials/artifacts/artA/questions/qA1/attempt") {
        return pendingAttemptA.promise;
      }
      if (url === "/api/materials/pdfs/mB") {
        return jsonResponse({ material: matB, guides: [], artifacts: [] });
      }
      return jsonResponse({ detail: `unexpected ${url}` }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MaterialWorkspace onStartStudy={vi.fn()} />);
    await screen.findByRole("heading", { name: "物理A.pdf" });
    fireEvent.click(screen.getByRole("button", { name: "打开复习资料" }));
    fireEvent.click(await screen.findByRole("tab", { name: /章节测试/ }));
    await screen.findByText("测试题目A");

    fireEvent.click(screen.getByRole("button", { name: "提交本题" }));
    fireEvent.click(screen.getByRole("button", { name: /返回资料/ }));
    fireEvent.click(screen.getByRole("button", { name: /高数B\.pdf/ }));
    await screen.findByRole("heading", { name: "高数B.pdf" });

    await act(async () => {
      pendingAttemptA.resolve(
        jsonResponse({
          ...artA,
          questions: [
            {
              ...artA.questions[0],
              attempt: {
                question_id: "qA1",
                user_answer: true,
                is_correct: true,
                score: 5,
                max_points: 5,
                grading_status: "auto_graded",
                feedback: "判断正确。",
                revealed_solution_at: "2026-11-18T00:01:00Z",
                answered_after_revealed: false,
                updated_at: "2026-11-18T00:01:00Z",
              },
              revealed_answer: true,
              revealed_explanation: "解析A",
            },
          ],
        }),
      );
    });

    expect(screen.getByRole("heading", { name: "高数B.pdf" })).toBeTruthy();
    expect(screen.queryByText("测试题目A")).toBeNull();
    expect(screen.queryByText("静电场复习样例")).toBeNull();
  });
});
