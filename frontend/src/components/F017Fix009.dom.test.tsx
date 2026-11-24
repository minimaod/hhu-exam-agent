import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import ConversationWorkspace from "./ConversationWorkspace";
import LearningMemory from "./LearningMemory";
import MaterialArtifactReaderModal from "./MaterialArtifactReaderModal";
import type { ConversationDetail, ConversationSummary } from "../lib/conversationsApi";
import type { ArtifactDetailView, ConversationMaterialItem, MaterialProgressItem } from "../lib/materialsApi";

const date = "2026-11-20T08:00:00+00:00";

function makeUsage() {
  return {
    timezone: "Asia/Shanghai",
    reset_at: "2026-11-21T00:00:00+08:00",
    requests: { limit: 30, used: 1, remaining: 29, overridden: false },
    images: { limit: 20, used: 0, remaining: 20, overridden: false },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("F017 Fix-009：学习记忆公式、会话计数与 PDF 成果交互", () => {
  test("1. 学习记忆公式：标题、记录知识点、建议复用 MathText，窄屏与未闭合公式安全", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });

    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/courses")) {
        return new Response(
          JSON.stringify({ items: [{ course: "高等数学", label: "高等数学", evidence: 1 }], pending: 0 }),
          { status: 200 },
        );
      }
      if (url.includes("days=")) {
        return new Response(
          JSON.stringify({
            status: "ready",
            course: "高等数学",
            range_days: 30,
            message: "已汇总",
            counts: {
              evidence: 1,
              practices: 0,
              attempts: 1,
              pending_course: 0,
              excluded: 0,
              excluded_evaluation: 0,
            },
            suggestions: [
              "重点复盘 $\\lim_{x \\to 0} \\frac{\\sin x}{x} = 1$ 的变形条件",
            ],
            groups: [
              {
                label: "重要极限 $\\lim_{x \\to 0} \\frac{\\sin x}{x} = 1$",
                latest_at: date,
                wrong_count: 1,
                correct_count: 0,
                after_reveal_count: 0,
                unclear_count: 0,
                earlier_wrong_count: 0,
                excluded_evaluation_count: 0,
                no_attempt_count: 0,
              },
            ],
            entries: [
              {
                entry_id: "mem-1",
                verdict: "wrong",
                course: "高等数学",
                course_source: "user",
                recorded_at: date,
                validity: "ok",
                statement_excerpt: "计算 $\\lim_{x \\to 0} \\frac{\\tan x - \\sin x}{x^3}$",
                knowledge_points: [
                  "极限 $\\lim_{x \\to 0} \\frac{\\sin x}{x} = 1$",
                  "未闭合 $\\lim_{x \\to 0} 周围可读文字保留",
                ],
                note: null,
                attempts: [],
                source: { conversation_id: "11111111-1111-4111-8111-111111111111" },
              },
            ],
            pending_course_entries: [],
          }),
          { status: 200 },
        );
      }
      return new Response("Not Found", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(<LearningMemory />);

    await screen.findByText(/建议先补/);
    // 知识点卡标题、建议、逐条记录的知识点均渲染了 KaTeX 公式
    const katexNodes = container.querySelectorAll(".katex");
    expect(katexNodes.length).toBeGreaterThanOrEqual(4);
    expect(container.textContent).toContain("重点复盘");
    // 未闭合公式保留周围可读文字
    expect(container.textContent).toContain("周围可读文字保留");
  });

  test("2. 侧栏会话计数：角标与最近学习会话使用同一可见列表（空白会话隐藏、切换、删除、刷新）", async () => {
    const validId = "11111111-1111-4111-8111-111111111111";
    const blankId = "22222222-2222-4222-8222-222222222222";
    const anotherValidId = "33333333-3333-4333-8333-333333333333";
    let listItems: ConversationSummary[] = [
      { id: validId, title: "高数极限复习", created_at: date, updated_at: "2026-11-20T09:00:00+00:00" },
      { id: blankId, title: "新会话", created_at: date, updated_at: date },
      { id: anotherValidId, title: "大学物理复习", created_at: date, updated_at: "2026-11-20T08:30:00+00:00" },
    ];
    const onCountChange = vi.fn();

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/api/usage")) {
        return new Response(JSON.stringify(makeUsage()), { status: 200 });
      }
      if (url.endsWith("/api/papers")) {
        return new Response(JSON.stringify({ items: [] }), { status: 200 });
      }
      if (url.endsWith("/api/conversations") && method === "GET") {
        return new Response(JSON.stringify({ items: listItems, total: listItems.length }), { status: 200 });
      }
      if (url.endsWith(`/api/conversations/${validId}`) && method === "GET") {
        const detail: ConversationDetail = {
          conversation: listItems[0],
          turns: [],
          materials: [],
          active_problem: null,
          active_exercise: null,
          record: null,
        };
        return new Response(JSON.stringify(detail), { status: 200 });
      }
      if (url.endsWith(`/api/conversations/${anotherValidId}`) && method === "GET") {
        const detail: ConversationDetail = {
          conversation: listItems[2],
          turns: [],
          materials: [],
          active_problem: null,
          active_exercise: null,
          record: null,
        };
        return new Response(JSON.stringify(detail), { status: 200 });
      }
      if (url.endsWith(`/api/conversations/${anotherValidId}/delete-impact`)) {
        return new Response(
          JSON.stringify({
            conversation_id: anotherValidId,
            title: "大学物理复习",
            linked_material_count: 0,
            artifact_count: 0,
            materials_still_accessible: true,
          }),
          { status: 200 },
        );
      }
      if (url.endsWith(`/api/conversations/${anotherValidId}`) && method === "DELETE") {
        listItems = listItems.filter((item) => item.id !== anotherValidId);
        return new Response(JSON.stringify({ deleted: true }), { status: 200 });
      }
      return new Response("Not Found", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ConversationWorkspace
        downloads={{}}
        onDownload={vi.fn()}
        onOpenRecord={vi.fn()}
        onCountChange={onCountChange}
      />,
    );

    await screen.findByRole("button", { name: /高数极限复习/ });
    // 空白会话被隐藏，列表中只显示 2 条有效会话，角标计数也必须是 2（而不是 3）
    expect(screen.queryByRole("button", { name: /^新会话/ })).toBeNull();
    await waitFor(() => {
      expect(onCountChange).toHaveBeenLastCalledWith(2);
    });

    // 删除一条有效会话后，只剩 1 条有效会话，角标计数更新为 1
    fireEvent.click(screen.getByTestId(`delete-conversation-${anotherValidId}`));
    fireEvent.click(await screen.findByRole("button", { name: "确认删除会话" }));
    await waitFor(() => {
      expect(onCountChange).toHaveBeenLastCalledWith(1);
    });
  });

  test("3 & 4. 资料处理进度（慢请求/切换后返回/刷新恢复/OCR进度）与从回复直接打开成果", async () => {
    const cidA = "11111111-1111-4111-8111-111111111111";
    const cidB = "22222222-2222-4222-8222-222222222222";
    const mid = "33333333-3333-4333-8333-333333333333";
    const aid = "44444444-4444-4444-8444-444444444444";

    let resolveGenerate!: (value: Response) => void;
    let progressStage: MaterialProgressItem[] = [];
    let detailTurns: ConversationDetail["turns"] = [];

    const artifactReady: ArtifactDetailView = {
      id: aid,
      material_id: mid,
      file_sha256: "a".repeat(64),
      start_page: 1,
      end_page: 12,
      rule_version: "exampass-v1",
      course_hint: "大学物理",
      title: "静电场讲义",
      is_sample: false,
      status: "ready",
      current_step: "completed",
      requested_outputs: ["notes", "quiz"],
      notes_status: "ready",
      notes_error_code: null,
      notes_error_message: null,
      quiz_status: "failed",
      quiz_error_code: "MODEL_ERROR",
      quiz_error_message: "出题超时",
      graph_status: "none",
      graph_error_code: null,
      graph_error_message: null,
      reused: false,
      conversation_id: cidA,
      skeleton: { chapters: [{ id: "c1", label: "静电场", kcs: [{ id: "kc1", label: "高斯定理", source_refs: ["1"] }] }] },
      notes_html: "<h2>静电场笔记</h2><p>核心结论</p>",
      graph_tree: { title: "静电场", nodes: [] },
      coverage_warnings: ["第 5 页存在公式提取告警"],
      questions: [],
      graph_notes: {},
      error_code: null,
      error_message: null,
      created_at: date,
      updated_at: date,
    };

    const makeMatItem = (withArtifact: boolean): ConversationMaterialItem => ({
      material: {
        id: mid,
        display_name: "静电场讲义.pdf",
        size_bytes: 102400,
        page_count: 36,
        file_sha256: "a".repeat(64),
        dedicated_conversation_id: null,
        created_at: date,
        updated_at: date,
      },
      readability: {
        material_id: mid,
        page_count: 36,
        readable_pages: 34,
        total_chars: 6000,
        scanned_pages: [5],
        formula_warning_pages: [5],
        sufficient: true,
        summary: "共 36 页，1 页待识别",
        available_outputs: ["notes", "quiz", "graph"],
      },
      artifacts: withArtifact
        ? [
            {
              id: aid,
              material_id: mid,
              file_sha256: "a".repeat(64),
              start_page: 1,
              end_page: 12,
              rule_version: "exampass-v1",
              course_hint: "大学物理",
              title: "静电场讲义",
              is_sample: false,
              status: "failed",
              current_step: "quiz",
              requested_outputs: ["notes", "quiz"],
              notes_status: "ready",
              notes_error_code: null,
              notes_error_message: null,
              quiz_status: "failed",
              quiz_error_code: "MODEL_ERROR",
              quiz_error_message: "出题超时",
              graph_status: "none",
              graph_error_code: null,
              graph_error_message: null,
              conversation_id: cidA,
              question_count: 0,
              error_code: "MODEL_ERROR",
              error_message: "出题超时",
              created_at: date,
              updated_at: date,
            },
          ]
        : [],
      attached_at: date,
    });

    let hasArtifact = false;

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/api/usage")) {
        return new Response(JSON.stringify(makeUsage()), { status: 200 });
      }
      if (url.endsWith("/api/papers")) {
        return new Response(JSON.stringify({ items: [] }), { status: 200 });
      }
      if (url.endsWith("/api/conversations") && method === "GET") {
        return new Response(
          JSON.stringify({
            items: [
              { id: cidA, title: "大学物理静电场", created_at: date, updated_at: "2026-11-20T09:00:00+00:00" },
              { id: cidB, title: "高数微积分", created_at: date, updated_at: "2026-11-20T08:00:00+00:00" },
            ],
            total: 2,
          }),
          { status: 200 },
        );
      }
      if (url.endsWith(`/api/conversations/${cidA}`) && method === "GET") {
        return new Response(
          JSON.stringify({
            conversation: { id: cidA, title: "大学物理静电场", created_at: date, updated_at: "2026-11-20T09:00:00+00:00" },
            turns: detailTurns,
            materials: [makeMatItem(hasArtifact)],
            active_problem: null,
            active_exercise: null,
            record: null,
          }),
          { status: 200 },
        );
      }
      if (url.endsWith(`/api/conversations/${cidB}`) && method === "GET") {
        return new Response(
          JSON.stringify({
            conversation: { id: cidB, title: "高数微积分", created_at: date, updated_at: "2026-11-20T08:00:00+00:00" },
            turns: [],
            materials: [],
            active_problem: null,
            active_exercise: null,
            record: null,
          }),
          { status: 200 },
        );
      }
      if (url.endsWith(`/api/conversations/${cidA}/materials/progress`) && method === "GET") {
        return new Response(JSON.stringify({ conversation_id: cidA, items: progressStage }), { status: 200 });
      }
      if (url.endsWith(`/api/conversations/${cidB}/materials/progress`) && method === "GET") {
        return new Response(JSON.stringify({ conversation_id: cidB, items: [] }), { status: 200 });
      }
      if (url.endsWith(`/api/conversations/${cidA}/materials/${mid}/generate`) && method === "POST") {
        return await new Promise<Response>((resolve) => {
          resolveGenerate = resolve;
        });
      }
      if (url.endsWith(`/api/materials/artifacts/${aid}`) && method === "GET") {
        return new Response(JSON.stringify(artifactReady), { status: 200 });
      }
      return new Response("Not Found", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);

    // 进入会话 A
    fireEvent.click(await screen.findByRole("button", { name: /大学物理静电场/ }));
    await screen.findByText("静电场讲义.pdf");

    // 模拟后端正在报告真实阶段（生成知识骨架）
    progressStage = [
      {
        task_id: "gen:1",
        conversation_id: cidA,
        material_id: mid,
        material_name: "静电场讲义.pdf",
        kind: "generate",
        start_page: 1,
        end_page: 12,
        outputs: ["notes"],
        stage: "skeleton",
        stage_label: "生成知识骨架",
        ocr_done: 0,
        ocr_total: 0,
        ocr_failed_pages: [],
        updated_at: date,
      },
    ];

    // 所选范围有扫描页，先识别再生成章节笔记。
    fireEvent.click(screen.getByRole("button", { name: "识别并生成章节笔记" }));
    const banner = await screen.findByTestId("material-progress-banner");
    expect(within(banner).getByText(/静电场讲义\.pdf/)).toBeTruthy();
    expect(within(banner).getByText(/章节笔记/)).toBeTruthy();

    // 轮询更新到后端真实阶段“生成知识骨架”
    await waitFor(() => {
      expect(within(screen.getByTestId("material-progress-banner")).getByText(/生成知识骨架/)).toBeTruthy();
    });

    // 切换到会话 B，原会话进度不污染会话 B
    fireEvent.click(screen.getByRole("button", { name: /高数微积分/ }));
    await waitFor(() => {
      expect(screen.queryByTestId("material-progress-banner")).toBeNull();
    });

    // 切回会话 A，仍能看到进度条
    fireEvent.click(screen.getByRole("button", { name: /大学物理静电场/ }));
    await screen.findByTestId("material-progress-banner");

    // 生成结束：有正文的成功/部分失败轮次出现，正文下方直接显示成果操作按钮
    progressStage = [];
    hasArtifact = true;
    detailTurns = [
      {
        request_id: "req-mat-1",
        seq: 1,
        kind: "dynamic",
        query: "生成《静电场讲义》（第 1–12 页）：章节笔记、章节测试",
        status: "completed",
        result: null,
        tutoring: null,
        assistant_message: "已为你生成《静电场讲义》（第 1–12 页）的章节笔记；章节测试暂未成功。",
        composite_results: [
          {
            action: "generate_material_artifact",
            status: "success",
            message: "已为你生成《静电场讲义》（第 1–12 页）的章节笔记；章节测试暂未成功。",
            details: {
              artifact_id: aid,
              material_id: mid,
              material_title: "静电场讲义",
              start_page: 1,
              end_page: 12,
              notes_status: "ready",
              quiz_status: "failed",
              graph_status: "none",
            },
          },
        ],
        error: null,
        started_at: date,
        finished_at: date,
      },
    ];
    resolveGenerate(new Response(JSON.stringify({ artifact: artifactReady, turn_request_id: "req-mat-1" }), { status: 200 }));

    expect(await screen.findByRole("button", { name: "重试章节测试" })).toBeTruthy();
    const card = await screen.findByRole("region", { name: "复习成果" });
    expect(screen.queryByRole("heading", { name: "静电场笔记" })).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "打开复习工作台" }));
    const notes = await screen.findByRole("dialog", { name: "复习工作台" });
    expect(within(notes).getByRole("heading", { name: "静电场笔记" })).toBeTruthy();
    fireEvent.click(within(notes).getByRole("button", { name: "返回会话" }));
    expect(within(notes).queryByText(/资料核对提示|实际纳入与原页引用/)).toBeNull();
  });

  test("5. OCR 进度显示已处理页数与失败页（含刷新恢复）", async () => {
    const cid = "11111111-1111-4111-8111-111111111111";
    const mid = "33333333-3333-4333-8333-333333333333";

    const ocrProgress: MaterialProgressItem[] = [
      {
        task_id: "ocr:99",
        conversation_id: cid,
        material_id: mid,
        material_name: "扫描讲义.pdf",
        kind: "ocr",
        start_page: 1,
        end_page: 4,
        outputs: [],
        stage: "ocr",
        stage_label: "识别扫描页",
        ocr_done: 2,
        ocr_total: 4,
        ocr_failed_pages: [2],
        updated_at: date,
      },
    ];

    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/api/usage")) {
        return new Response(JSON.stringify(makeUsage()), { status: 200 });
      }
      if (url.endsWith("/api/papers")) {
        return new Response(JSON.stringify({ items: [] }), { status: 200 });
      }
      if (url.endsWith("/api/conversations")) {
        return new Response(
          JSON.stringify({
            items: [{ id: cid, title: "OCR 恢复测试", created_at: date, updated_at: "2026-11-20T09:00:00+00:00" }],
            total: 1,
          }),
          { status: 200 },
        );
      }
      if (url.endsWith(`/api/conversations/${cid}`)) {
        return new Response(
          JSON.stringify({
            conversation: { id: cid, title: "OCR 恢复测试", created_at: date, updated_at: "2026-11-20T09:00:00+00:00" },
            turns: [],
            materials: [],
            active_problem: null,
            active_exercise: null,
            record: null,
          }),
          { status: 200 },
        );
      }
      if (url.endsWith(`/api/conversations/${cid}/materials/progress`)) {
        return new Response(JSON.stringify({ conversation_id: cid, items: ocrProgress }), { status: 200 });
      }
      return new Response("Not Found", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: /OCR 恢复测试/ }));
    const banner = await screen.findByTestId("material-progress-banner");
    expect(within(banner).getByText(/扫描讲义\.pdf/)).toBeTruthy();
    expect(within(banner).getByText(/已处理 2\/4 页，失败第 2 页/)).toBeTruthy();
  });

  test("6. 复习阅读区省略逐页报告，保留真实缺页和失败分项提示", () => {
    const artifact: ArtifactDetailView = {
      id: "art-1",
      material_id: "mat-1",
      file_sha256: "b".repeat(64),
      start_page: 1,
      end_page: 10,
      rule_version: "exampass-v1",
      course_hint: "大学物理",
      title: "电介质与电容",
      is_sample: false,
      status: "failed",
      current_step: "quiz",
      requested_outputs: ["notes", "quiz"],
      notes_status: "ready",
      notes_error_code: null,
      notes_error_message: null,
      quiz_status: "failed",
      quiz_error_code: "TIMEOUT",
      quiz_error_message: "模型超时",
      graph_status: "none",
      graph_error_code: null,
      graph_error_message: null,
      reused: false,
      conversation_id: "conv-1",
      skeleton: { chapters: [] },
      notes_html: "<p>笔记正文</p>",
      graph_tree: null,
      coverage_warnings: ["部分页面公式模糊"],
      coverage: {
        input_pages: [], included_pages: [1, 2, 3, 5, 6, 8, 9, 10],
        missing_pages: [4, 7], review_pages: [], excluded_pages: [], partial: true,
      },
      questions: [],
      graph_notes: {},
      error_code: "TIMEOUT",
      error_message: "模型超时",
      created_at: date,
      updated_at: date,
    };

    render(
      <MaterialArtifactReaderModal
        artifact={artifact}
        displayName="物理讲义.pdf"
        pageCount={36}
        onUpdateArtifact={vi.fn()}
        onStartStudy={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getAllByText(/第 1–10 页 · 资料有缺页/).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText(/实际纳入与原页引用|资料核对提示|部分页面公式模糊/)).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: /章节测试/ }));
    expect(screen.getByText("章节测试生成失败")).toBeTruthy();
    expect(screen.getByText("模型超时")).toBeTruthy();
  });

  test("7. 返修：切换会话不中止聊天发起的资料生成，普通聊天结束不清空资料进度", async () => {
    const cidA = "11111111-1111-4111-8111-111111111111";
    const cidB = "22222222-2222-4222-8222-222222222222";
    const mid = "33333333-3333-4333-8333-333333333333";
    let sseAborted = false;
    let resolveChatGen!: (res: Response) => void;
    let progressItems: MaterialProgressItem[] = [];

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/api/usage") || url.endsWith("/api/usage/today")) return Response.json(makeUsage());
      if (url.endsWith("/api/papers")) return Response.json({ items: [] });
      if (url.endsWith("/api/conversations")) {
        return Response.json({
          items: [
            { id: cidA, title: "会话A", created_at: date, updated_at: "2026-11-20T09:00:00+00:00" },
            { id: cidB, title: "会话B", created_at: date, updated_at: "2026-11-20T08:00:00+00:00" },
          ],
          total: 2,
        });
      }
      if (url.endsWith(`/api/conversations/${cidA}`) && method === "GET") {
        return Response.json({
          conversation: { id: cidA, title: "会话A", created_at: date, updated_at: "2026-11-20T09:00:00+00:00" },
          turns: [],
          materials: [],
          active_problem: null,
          active_exercise: null,
          record: null,
        });
      }
      if (url.endsWith(`/api/conversations/${cidB}`) && method === "GET") {
        return Response.json({
          conversation: { id: cidB, title: "会话B", created_at: date, updated_at: "2026-11-20T08:00:00+00:00" },
          turns: [],
          materials: [],
          active_problem: null,
          active_exercise: null,
          record: null,
        });
      }
      if (url.endsWith(`/api/conversations/${cidA}/materials/progress`)) {
        return Response.json({ conversation_id: cidA, items: progressItems });
      }
      if (url.endsWith(`/api/conversations/${cidB}/materials/progress`)) {
        return Response.json({ conversation_id: cidB, items: [] });
      }
      if (url.endsWith(`/api/conversations/${cidA}/turns`) && method === "POST") {
        const body = JSON.parse(String(init?.body ?? "{}")) as { query?: string; request_id?: string };
        if (body.query?.includes("生成笔记")) {
          init?.signal?.addEventListener("abort", () => {
            sseAborted = true;
          });
          const encoder = new TextEncoder();
          const stream = new ReadableStream({
            start(controller) {
              controller.enqueue(
                encoder.encode(
                  `data: ${JSON.stringify({
                    type: "progress",
                    conversation_id: cidA,
                    request_id: body.request_id,
                    stage: "generating_material",
                    message: "正在生成知识骨架",
                  })}\n\n`,
                ),
              );
              progressItems = [
                {
                  task_id: "gen:chat",
                  conversation_id: cidA,
                  material_id: mid,
                  material_name: "高数讲义.pdf",
                  kind: "generate",
                  start_page: 1,
                  end_page: 10,
                  outputs: ["notes"],
                  stage: "skeleton",
                  stage_label: "生成知识骨架",
                  ocr_done: 0,
                  ocr_total: 0,
                  ocr_failed_pages: [],
                  updated_at: date,
                },
              ];
              resolveChatGen = () => {
                controller.enqueue(
                  encoder.encode(
                    `data: ${JSON.stringify({
                      type: "result",
                      conversation_id: cidA,
                      request_id: body.request_id,
                      seq: 1,
                      kind: "dynamic",
                      status: "completed",
                      business_status: "succeeded",
                      reply_status: "complete",
                      assistant_message: "已生成笔记",
                    })}\n\n`,
                  ),
                );
                controller.close();
              };
            },
          });
          return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
        }
        // 普通聊天消息立即完成
        const encoder = new TextEncoder();
        return new Response(
          encoder.encode(
            `data: ${JSON.stringify({
              type: "result",
              conversation_id: cidA,
              request_id: body.request_id,
              seq: 2,
              kind: "dynamic",
              status: "completed",
              business_status: "succeeded",
              reply_status: "complete",
              assistant_message: "好的",
            })}\n\n`,
          ),
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        );
      }
      return new Response("Not Found", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: /会话A/ }));
    const input = await screen.findByLabelText("学习指令");
    fireEvent.change(input, { target: { value: "帮我生成笔记" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await screen.findByTestId("material-progress-banner");

    // 切换到会话 B：不中止聊天发起的资料生成
    fireEvent.click(screen.getByRole("button", { name: /会话B/ }));
    await waitFor(() => {
      expect(screen.queryByTestId("material-progress-banner")).toBeNull();
    });
    expect(sseAborted).toBe(false);

    // 切回会话 A：进度条恢复，且此时发送普通聊天消息结束后不清空资料进度
    fireEvent.click(screen.getByRole("button", { name: /会话A/ }));
    await screen.findByTestId("material-progress-banner");

    const inputAfterReturn = await screen.findByLabelText("学习指令");
    fireEvent.change(inputAfterReturn, { target: { value: "在吗" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText("好的");
    // 普通聊天结束后，正在运行的资料进度依然保留
    expect(screen.getByTestId("material-progress-banner")).toBeTruthy();

    // 结束后台生成流
    progressItems = [];
    resolveChatGen(new Response());
  });

  test("8. 返修：后台 OCR 完成不卡住已切换的新会话", async () => {
    const cidA = "11111111-1111-4111-8111-111111111111";
    const cidB = "22222222-2222-4222-8222-222222222222";
    const mid = "33333333-3333-4333-8333-333333333333";
    let resolveOcr!: (res: Response) => void;
    let resolveDetailB!: (res: Response) => void;
    let detailACallsAfterSwitch = 0;
    let switchedToB = false;

    const itemA: ConversationMaterialItem = {
      material: {
        id: mid,
        display_name: "扫描卷.pdf",
        size_bytes: 1024,
        page_count: 2,
        file_sha256: "c".repeat(64),
        dedicated_conversation_id: null,
        created_at: date,
        updated_at: date,
      },
      readability: {
        material_id: mid,
        page_count: 2,
        readable_pages: 0,
        total_chars: 0,
        scanned_pages: [1, 2],
        formula_warning_pages: [],
        sufficient: false,
        summary: "2 页待识别",
        available_outputs: [],
      },
      artifacts: [],
      attached_at: date,
    };

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/api/usage") || url.endsWith("/api/usage/today")) return Response.json(makeUsage());
      if (url.endsWith("/api/papers")) return Response.json({ items: [] });
      if (url.endsWith("/api/conversations")) {
        return Response.json({
          items: [
            { id: cidA, title: "会话A", created_at: date, updated_at: "2026-11-20T09:00:00+00:00" },
            { id: cidB, title: "会话B", created_at: date, updated_at: "2026-11-20T08:00:00+00:00" },
          ],
          total: 2,
        });
      }
      if (url.endsWith(`/api/conversations/${cidA}`) && method === "GET") {
        if (switchedToB) detailACallsAfterSwitch += 1;
        return Response.json({
          conversation: { id: cidA, title: "会话A", created_at: date, updated_at: "2026-11-20T09:00:00+00:00" },
          turns: [],
          materials: [itemA],
          active_problem: null,
          active_exercise: null,
          record: null,
        });
      }
      if (url.endsWith(`/api/conversations/${cidB}`) && method === "GET") {
        return await new Promise<Response>((resolve) => {
          resolveDetailB = resolve;
        });
      }
      if (url.includes("/materials/progress")) {
        return Response.json({ conversation_id: cidA, items: [] });
      }
      if (url.endsWith(`/api/materials/pdfs/${mid}/ocr`) && method === "POST") {
        return await new Promise<Response>((resolve) => {
          resolveOcr = resolve;
        });
      }
      return new Response("Not Found", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: /会话A/ }));
    fireEvent.click(await screen.findByRole("button", { name: "识别扫描页" }));

    // 切换到 B，此时 B 的详情还在加载中
    switchedToB = true;
    fireEvent.click(screen.getByRole("button", { name: /会话B/ }));

    // A 的 OCR 完成，不应请求 A 的详情也不应作废 B 的详情
    resolveOcr(
      Response.json({
        recognized_pages: [1, 2],
        reused_pages: [],
        skipped_pages: [],
        failures: [],
        readability: itemA.readability,
      }),
    );

    // 随后 B 的详情返回，B 应正常进入就绪状态而非卡在加载中
    resolveDetailB(
      Response.json({
        conversation: { id: cidB, title: "会话B", created_at: date, updated_at: "2026-11-20T08:00:00+00:00" },
        turns: [],
        materials: [],
        active_problem: null,
        active_exercise: null,
        record: null,
      }),
    );

    await screen.findByText(/这是新会话/);
    expect(detailACallsAfterSwitch).toBe(0);
  });

  test("9. 刷新后继续跟踪生成，等轮次完成再重载成果卡", async () => {
    const cid = "11111111-1111-4111-8111-111111111111";
    const requestId = "22222222-2222-4222-8222-222222222222";
    let progressCalls = 0;
    let detailCalls = 0;

    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/api/usage") || url.endsWith("/api/usage/today")) return Response.json(makeUsage());
      if (url.endsWith("/api/papers")) return Response.json({ items: [] });
      if (url.endsWith("/api/conversations")) {
        return Response.json({
          items: [{ id: cid, title: "刷新恢复", created_at: date, updated_at: date }],
          total: 1,
        });
      }
      if (url.endsWith(`/api/conversations/${cid}`)) {
        detailCalls += 1;
        return Response.json({
          conversation: { id: cid, title: "刷新恢复", created_at: date, updated_at: date },
          turns:
            progressCalls >= 3
              ? [
                  {
                    request_id: requestId,
                    seq: 1,
                    kind: "dynamic",
                    query: "生成笔记",
                    status: "completed",
                    result: null,
                    tutoring: null,
                    assistant_message: "刷新后重载的成果完成通知",
                    composite_results: [{
                      action: "generate_material_artifact",
                      status: "success",
                      message: "刷新后重载的成果完成通知",
                      details: { artifact_id: "a1", outputs: ["notes"], notes_status: "ready" },
                    }],
                    error: null,
                    started_at: date,
                    finished_at: date,
                  },
                ]
              : [],
          materials: [],
          active_problem: null,
          active_exercise: null,
          record: null,
        });
      }
      if (url.endsWith("/api/materials/artifacts/a1")) {
        return Response.json({
          id: "a1", material_id: "m1", title: "高数讲义", start_page: 1, end_page: 5,
          notes_status: "ready", notes_html: "<h2>导数复习</h2>", questions: [],
        });
      }
      if (url.endsWith(`/api/conversations/${cid}/materials/progress`)) {
        progressCalls += 1;
        // 生成器结束后，聊天轮次仍在整理并保存回执。
        return Response.json({
          conversation_id: cid,
          chat_turn_request_id: progressCalls < 3 ? requestId : null,
          items:
            progressCalls === 1
              ? [
                  {
                    task_id: "gen:1",
                    conversation_id: cid,
                    material_id: "m1",
                    material_name: "高数.pdf",
                    kind: "generate",
                    start_page: 1,
                    end_page: 5,
                    outputs: ["notes"],
                    stage: "notes",
                    stage_label: "生成章节笔记",
                    ocr_done: 0,
                    ocr_total: 0,
                    ocr_failed_pages: [],
                    updated_at: date,
                  },
                ]
              : [],
        });
      }
      return new Response("Not Found", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: /刷新恢复/ }));
    const card = await screen.findByRole("region", { name: "复习成果" }, { timeout: 3000 });
    fireEvent.click(within(card).getByRole("button", { name: "打开复习工作台" }));
    const notes = await screen.findByRole("dialog", { name: "复习工作台" });
    expect(within(notes).getByRole("heading", { name: "导数复习" })).toBeTruthy();
    fireEvent.click(within(notes).getByRole("button", { name: "返回会话" }));
    expect(detailCalls).toBeGreaterThanOrEqual(2);
  });

  test("10. 返修：在会话 B 重试旧成果绑定当前会话 B 而非原所属会话 A", async () => {
    const cidA = "11111111-1111-4111-8111-111111111111";
    const cidB = "22222222-2222-4222-8222-222222222222";
    const mid = "33333333-3333-4333-8333-333333333333";
    const aid = "44444444-4444-4444-8444-444444444444";
    const generateUrls: string[] = [];

    const oldArtifact: ArtifactDetailView = {
      id: aid,
      material_id: mid,
      file_sha256: "a".repeat(64),
      start_page: 3,
      end_page: 8,
      rule_version: "exampass-v1",
      course_hint: "大学物理",
      title: "旧成果",
      is_sample: false,
      status: "failed",
      current_step: "quiz",
      requested_outputs: ["quiz"],
      notes_status: "ready",
      notes_error_code: null,
      notes_error_message: null,
      quiz_status: "failed",
      quiz_error_code: "ERR",
      quiz_error_message: "失败",
      graph_status: "none",
      graph_error_code: null,
      graph_error_message: null,
      reused: false,
      conversation_id: cidA,
      skeleton: { chapters: [] },
      notes_html: "<h2>既有笔记</h2>",
      graph_tree: null,
      coverage_warnings: [],
      questions: [],
      graph_notes: {},
      error_code: "ERR",
      error_message: "失败",
      created_at: date,
      updated_at: date,
    };

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.endsWith("/api/usage") || url.endsWith("/api/usage/today")) return Response.json(makeUsage());
      if (url.endsWith("/api/papers")) return Response.json({ items: [] });
      if (url.endsWith("/api/conversations")) {
        return Response.json({
          items: [{ id: cidB, title: "会话B", created_at: date, updated_at: date }],
          total: 1,
        });
      }
      if (url.endsWith(`/api/conversations/${cidB}`) && method === "GET") {
        return Response.json({
          conversation: { id: cidB, title: "会话B", created_at: date, updated_at: date },
          turns: [
            {
              request_id: "req-b",
              seq: 1,
              kind: "dynamic",
              query: "生成测试",
              status: "completed",
              result: null,
              tutoring: null,
              assistant_message: "生成失败",
              composite_results: [
                {
                  action: "generate_material_artifact",
                  status: "failed",
                  message: "生成失败",
                  details: {
                    artifact_id: aid,
                    material_id: mid,
                    outputs: ["quiz"],
                    notes_status: "ready",
                    quiz_status: "failed",
                  },
                },
              ],
              error: null,
              started_at: date,
              finished_at: date,
            },
          ],
          materials: [],
          active_problem: null,
          active_exercise: null,
          record: null,
        });
      }
      if (url.endsWith("/api/materials/library")) {
        return Response.json({ items: [{
          material: { id: mid, display_name: "旧讲义.pdf", page_count: 5, size_bytes: 1024, created_at: date },
          artifacts: [oldArtifact], guides: [], conversation_ids: [cidA],
        }] });
      }
      if (url.endsWith(`/api/materials/artifacts/${aid}`)) {
        return Response.json(oldArtifact);
      }
      if (url.includes(`/materials/${mid}/generate`) && method === "POST") {
        generateUrls.push(url);
        const artifact: ArtifactDetailView = generateUrls.length === 1 ? oldArtifact : {
          ...oldArtifact,
          id: "55555555-5555-4555-8555-555555555555",
          title: "OCR 更新后的成果",
          rule_version: "exampass-v1:vision-ocr-v1:updated",
          status: "ready",
          quiz_status: "ready",
          quiz_error_code: null,
          quiz_error_message: null,
          error_code: null,
          error_message: null,
        };
        return Response.json({ artifact, turn_request_id: "req-retry" });
      }
      if (url.includes("/materials/progress")) {
        return Response.json({ conversation_id: cidB, items: [] });
      }
      return new Response("Not Found", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: /会话B/ }));
    fireEvent.click(await screen.findByRole("button", { name: "重试章节测试" }));

    await waitFor(() => {
      expect(generateUrls).toEqual([`/api/conversations/${cidB}/materials/${mid}/generate`]);
    });
    fireEvent.click(screen.getByRole("button", { name: "资料与成果" }));
    fireEvent.click(await screen.findByRole("button", { name: /打开第.*页成果/ }));
    const reader = await screen.findByRole("dialog", { name: "复习工作台" });
    fireEvent.click(within(reader).getByRole("button", { name: "测试" }));
    fireEvent.click(within(reader).getByRole("button", { name: "生成测试" }));
    await within(reader).findByRole("heading", { name: /章节测试 · / });
    expect(within(reader).getByRole("button", { name: "测试" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(within(reader).getByRole("button", { name: "返回会话" }));
    expect(generateUrls).toEqual([
      `/api/conversations/${cidB}/materials/${mid}/generate`,
      `/api/conversations/${cidB}/materials/${mid}/generate`,
    ]);
  });

  test("刷新后从服务端恢复聊天生成轮次并可主动停止", async () => {
    const cid = "11111111-1111-4111-8111-111111111111";
    const requestId = "22222222-2222-4222-8222-222222222222";
    const stopPath = `/api/conversations/${cid}/turns/${requestId}/stop-material`;
    const summary = { id: cid, title: "刷新恢复生成", created_at: date, updated_at: date };
    let stopped = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.endsWith("/api/usage") || url.endsWith("/api/usage/today")) return Response.json(makeUsage());
      if (url.endsWith("/api/conversations")) return Response.json({ items: [summary], total: 1 });
      if (url.endsWith(`/api/conversations/${cid}`)) {
        return Response.json({
          conversation: summary, turns: [], materials: [], active_problem: null,
          active_exercise: null, record: null,
        });
      }
      if (url.endsWith("/materials/progress")) {
        return Response.json({
          conversation_id: cid, items: [], chat_turn_request_id: stopped ? null : requestId,
        });
      }
      if (url.endsWith(stopPath) && init?.method === "POST") {
        stopped = true;
        return new Response(null, { status: 204 });
      }
      return new Response("Not Found", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
    window.localStorage.setItem("hhu-exam-review.current-conversation", cid);

    render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
    await screen.findByText("正在整理资料复习成果…");
    fireEvent.click(await screen.findByRole("button", { name: "停止" }));

    await waitFor(() => expect(stopped).toBe(true));
    await waitFor(() => expect(screen.queryByText("正在整理资料复习成果…")).toBeNull(), { timeout: 2000 });
    expect(screen.queryByRole("button", { name: "停止" })).toBeNull();
  });
});
