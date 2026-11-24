/**
 * F019 第三阶段：会话内上传 PDF、按需生成、局部失败重试、阅读区在当前会话追问、
 * 「资料与成果」找回旧资料与删除会话确认弹窗测试。
 */

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import ConversationWorkspace from "./ConversationWorkspace";
import type { ConversationDetail } from "../lib/conversationsApi";
import type { ArtifactDetailView, ConversationMaterialItem } from "../lib/materialsApi";

const cid = "11111111-1111-4111-8111-111111111111";
const mid = "22222222-2222-4222-8222-222222222222";
const oldMid = "33333333-3333-4333-8333-333333333333";
const aid = "44444444-4444-4444-8444-444444444444";
const qid = "55555555-5555-4555-8555-555555555555";
const date = "2026-11-18T08:00:00+00:00";

function makeUsage() {
  return {
    timezone: "Asia/Shanghai",
    reset_at: "2026-11-19T00:00:00+08:00",
    requests: { limit: 20, used: 1, remaining: 19, overridden: false },
    images: { limit: 10, used: 0, remaining: 10, overridden: false },
  };
}

function makeMaterialItem(partial?: {
  artifactSummary?: Record<string, unknown>;
  sufficient?: boolean;
}): ConversationMaterialItem {
  const sufficient = partial?.sufficient ?? true;
  return {
    material: {
      id: mid,
      display_name: "静电场讲义.pdf",
      size_bytes: 204800,
      page_count: 36,
      file_sha256: "a".repeat(64),
      dedicated_conversation_id: null,
      created_at: date,
      updated_at: date,
    },
    readability: {
      material_id: mid,
      page_count: 36,
      readable_pages: sufficient ? 36 : 0,
      total_chars: sufficient ? 8400 : 10,
      scanned_pages: sufficient ? [] : [1, 2],
      formula_warning_pages: [3],
      sufficient,
      summary: sufficient ? "共 36 页，文字清晰可生成" : "文字不足，疑似纯扫描件",
      available_outputs: sufficient ? ["notes", "quiz", "graph"] : [],
    },
    artifacts: partial?.artifactSummary ? [partial.artifactSummary as ConversationMaterialItem["artifacts"][number]] : [],
    attached_at: date,
  };
}

function makeArtifactDetail(partial?: {
  notesStatus?: "none" | "ready" | "failed";
  quizStatus?: "none" | "ready" | "failed";
  graphStatus?: "none" | "ready" | "failed";
}): ArtifactDetailView {
  const notesStatus = partial?.notesStatus ?? "ready";
  const quizStatus = partial?.quizStatus ?? "failed";
  const graphStatus = partial?.graphStatus ?? "none";
  return {
    id: aid,
    material_id: mid,
    file_sha256: "a".repeat(64),
    start_page: 1,
    end_page: 36,
    rule_version: "exampass-v1",
    course_hint: "大学物理",
    title: "静电场与高斯定理",
    is_sample: false,
    status: quizStatus === "failed" ? "failed" : "ready",
    current_step: quizStatus === "failed" ? "quiz" : "completed",
    requested_outputs: ["notes", "quiz"],
    notes_status: notesStatus,
    notes_error_code: null,
    notes_error_message: null,
    quiz_status: quizStatus,
    quiz_error_code: quizStatus === "failed" ? "MODEL_ERROR" : null,
    quiz_error_message: quizStatus === "failed" ? "出题模型临时超时" : null,
    graph_status: graphStatus,
    graph_error_code: null,
    graph_error_message: null,
    reused: false,
    conversation_id: cid,
    skeleton: {
      chapters: [
        {
          id: "ch1",
          label: "静电场",
          kcs: [{ id: "kc1.1", label: "高斯定理", source_refs: ["1"] }],
        },
      ],
    },
    notes_html: notesStatus === "ready" ? "<h2>高斯定理核心结论</h2><p>闭合面电通量等于包围电荷除以真空介电常数。</p>" : "",
    graph_tree: {
      title: "静电场",
      nodes: [{ id: "graph-kc-1", label: "图谱中的高斯定理", summary: "" }],
    },
    coverage_warnings: [],
    questions:
      quizStatus === "ready"
        ? [
            {
              id: qid,
              sort_order: 1,
              kc_id: "kc1.1",
              q_type: "tf",
              stem: "闭合高斯面内净电荷为零时，面上各点场强必为零。",
              options: null,
              points: 5,
              pitfall: "",
              cognitive: "understand",
              difficulty: "basic",
              source_pages: [1],
              revealed_answer: null,
              revealed_explanation: null,
              attempt: null,
            },
          ]
        : [],
    graph_notes: {},
    error_code: quizStatus === "failed" ? "MODEL_ERROR" : null,
    error_message: quizStatus === "failed" ? "出题模型临时超时" : null,
    created_at: date,
    updated_at: date,
  };
}

function makeDetail(materials: ConversationMaterialItem[]): ConversationDetail {
  return {
    conversation: { id: cid, title: "高数与物理复习", created_at: date, updated_at: date },
    turns: [],
    materials,
    active_problem: null,
    active_exercise: null,
    record: null,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  history.replaceState(null, "", location.pathname);
});

test("会话内上传 PDF -> 部分生成失败重试 -> 章节测试留在当前会话追问", async () => {
  const generationBodies: Array<Record<string, unknown>> = [];
  let currentMaterials: ConversationMaterialItem[] = [];
  let currentArtifact = makeArtifactDetail({ notesStatus: "ready", quizStatus: "failed" });
  const streamPayloads: Array<Record<string, unknown>> = [];
  const submittedQuizAnswers: unknown[] = [];
  const checkedRanges: unknown[] = [];

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
          items: [{ id: cid, title: "高数与物理复习", created_at: date, updated_at: date }],
          total: 1,
        }),
        { status: 200 },
      );
    }
    if (url.endsWith(`/api/conversations/${cid}`) && method === "GET") {
      const detail = makeDetail(currentMaterials);
      if (generationBodies.length > 0) {
        detail.turns.push({
          request_id: "99999999-9999-4999-8999-999999999999",
          seq: 1,
          kind: "dynamic",
          query: "生成章节测试",
          status: "completed",
          result: null,
          tutoring: null,
          assistant_message: "历史章节测试生成失败",
          composite_results: [{
            action: "generate_material_artifact",
            status: "failed",
            message: "历史章节测试生成失败",
            details: {
              artifact_id: aid,
              material_id: mid,
              start_page: 9,
              end_page: 16,
              quiz_status: "failed",
            },
          }],
          error: null,
          started_at: date,
          finished_at: date,
        });
      }
      if (generationBodies.length > 0) {
        detail.turns.push({
          ...detail.turns[0],
          request_id: "req-gen",
          seq: 2,
          query: "生成章节笔记",
          assistant_message: "章节笔记已展示在下方。",
          composite_results: [{
            action: "generate_material_artifact", status: "success", message: "章节笔记已展示在下方。",
            details: { artifact_id: aid, material_id: mid, outputs: ["notes"], notes_status: "ready" },
          }],
        });
      }
      return new Response(JSON.stringify(detail), { status: 200 });
    }
    if (url.endsWith(`/api/conversations/${cid}/materials/upload`) && method === "POST") {
      const item = { ...makeMaterialItem(), readability: null };
      currentMaterials = [item];
      return new Response(
        JSON.stringify({
          conversation_id: cid,
          item,
          assistant_notice: "已上传《静电场讲义.pdf》",
          turn_request_id: "req-upload",
        }),
        { status: 201 },
      );
    }
    if (url.endsWith("/api/materials/pdfs/" + mid + "/readability") && method === "POST") {
      checkedRanges.push(JSON.parse(String(init?.body)));
      currentMaterials = [makeMaterialItem()];
      return new Response(JSON.stringify({ reason: "所选 36 页已检查" }), { status: 200 });
    }
    if (url.endsWith(`/api/conversations/${cid}/materials/${mid}/generate`) && method === "POST") {
      const body = JSON.parse(String(init?.body ?? "{}")) as { outputs: string[] };
      generationBodies.push(body);
      if (body.outputs.includes("quiz") && generationBodies.length === 3) {
        currentArtifact = makeArtifactDetail({ notesStatus: "ready", quizStatus: "ready" });
        currentArtifact.questions.push({
          ...currentArtifact.questions[0],
          id: "66666666-6666-4666-8666-666666666666", sort_order: 2, stem: "第二题检验电容的定义。",
        });
      }
      currentMaterials = [
        makeMaterialItem({
          artifactSummary: {
            id: aid,
            material_id: mid,
            start_page: 1,
            end_page: 36,
            course_hint: currentArtifact.course_hint,
            title: "静电场与高斯定理",
            is_sample: false,
            status: currentArtifact.status,
            current_step: currentArtifact.current_step,
            requested_outputs: ["notes", "quiz"],
            notes_status: currentArtifact.notes_status,
            notes_error_code: null,
            notes_error_message: null,
            quiz_status: currentArtifact.quiz_status,
            quiz_error_code: currentArtifact.quiz_error_code,
            quiz_error_message: currentArtifact.quiz_error_message,
            graph_status: currentArtifact.graph_status,
            graph_error_code: null,
            graph_error_message: null,
            question_count: currentArtifact.questions.length,
            conversation_id: cid,
            error_code: currentArtifact.error_code,
            error_message: currentArtifact.error_message,
            created_at: date,
            updated_at: date,
          },
        }),
      ];
      return new Response(
        JSON.stringify({ artifact: currentArtifact, turn_request_id: "req-gen" }),
        { status: 200 },
      );
    }
    if (url.endsWith(`/api/materials/artifacts/${aid}`) && method === "GET") {
      return new Response(JSON.stringify(currentArtifact), { status: 200 });
    }
    if (url.endsWith("/api/materials/artifacts/" + aid + "/questions/" + qid + "/attempt") && method === "POST") {
      const body = JSON.parse(String(init?.body));
      submittedQuizAnswers.push(body);
      currentArtifact.questions[0].attempt = {
        question_id: qid, user_answer: body.user_answer, is_correct: true, score: 5, max_points: 5,
        grading_status: "auto_graded", feedback: null, revealed_solution_at: null,
        answered_after_revealed: false, updated_at: date,
      };
      return new Response(JSON.stringify(currentArtifact), { status: 200 });
    }
    if (url.endsWith(`/api/materials/artifacts/${aid}/questions/${qid}/study`) && method === "POST") {
      return new Response(
        JSON.stringify({
          conversation_id: cid,
          artifact_id: aid,
          question_id: qid,
          mode: "explain",
          prompt: "请围绕我选中的章节测试题目进行讲解或解答我的疑问",
        }),
        { status: 200 },
      );
    }
    if (url.endsWith(`/api/conversations/${cid}/turns`) && method === "POST") {
      streamPayloads.push(JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>);
      const encoder = new TextEncoder();
      const sse =
        `data: ${JSON.stringify({
          type: "result",
          conversation_id: cid,
          request_id: streamPayloads[0]?.request_id,
          seq: 1,
          kind: "dynamic",
          status: "completed",
          business_status: "succeeded",
          reply_status: "complete",
          assistant_message: "闭合高斯面通量为零只说明净电荷为零，场强分布不一定处处为零。",
        })}\n\n`;
      return new Response(encoder.encode(sse), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    return new Response("Not Found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);

  fireEvent.click(await screen.findByRole("button", { name: /高数与物理复习/ }));
  await screen.findByText(/这是新会话/);

  // 1. 通过左下「＋」上传 PDF
  fireEvent.click(screen.getByRole("button", { name: "添加附件" }));
  const pdfInput = screen.getByLabelText("选择附件");
  const file = new File(["%PDF-1.4"], "静电场讲义.pdf", { type: "application/pdf" });
  fireEvent.change(pdfInput, { target: { files: [file] } });

  await screen.findByText("静电场讲义.pdf");
  expect(screen.queryByRole("button", { name: "生成章节笔记" })).toBeNull();
  fireEvent.click(await screen.findByRole("button", { name: "检查文字识别" }));
  await screen.findByRole("button", { name: "生成章节笔记" });
  expect(checkedRanges).toEqual([{ start_page: 1, end_page: 36 }]);
  expect(generationBodies).toHaveLength(0);

  fireEvent.click(screen.getByRole("button", { name: "生成章节笔记" }));
  const resultCard = await screen.findByRole("region", { name: "复习成果" });
  expect(within(resultCard).getByRole("button", { name: "打开复习工作台" })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "高斯定理核心结论" })).toBeNull();
  expect(screen.queryByRole("dialog")).toBeNull();

  // 历史轮次的范围与当前成果不同，重试应读取原成果。随后从资料卡重试。
  fireEvent.click(await screen.findByRole("button", { name: "重试章节测试" }));
  await waitFor(() => expect(generationBodies).toHaveLength(2));
  fireEvent.click((await screen.findAllByRole("button", { name: /章节测试失败 · 重试/ }))[0]);
  await waitFor(() => expect(generationBodies).toHaveLength(3));
  expect(generationBodies.slice(1)).toEqual([
    { outputs: ["quiz"], start_page: 1, end_page: 36, course_hint: "大学物理" },
    { outputs: ["quiz"], start_page: 1, end_page: 36, course_hint: "大学物理" },
  ]);
  fireEvent.click(await screen.findByRole("button", { name: "打开章节测试" }));
  const quiz = await screen.findByRole("dialog", { name: "复习工作台" });
  const group = quiz.querySelector("[data-review-question-group]") as HTMLElement;
  await within(group).findByText(/闭合高斯面内净电荷为零时/);
  expect(within(quiz).getByText("第二题检验电容的定义。")).toBeTruthy();
  const question = within(group).getByText(/闭合高斯面内净电荷为零时/).closest("article")!;
  fireEvent.click(within(question).getByRole("radio", { name: "错误" }));
  fireEvent.click(within(question).getByRole("button", { name: "提交本题" }));
  await within(quiz).findByText(/当前：正确/);
  expect(submittedQuizAnswers).toEqual([{ user_answer: false }]);

  fireEvent.click(within(question).getByRole("button", { name: "问 AI" }));
  const drawer = within(quiz).getByRole("complementary", { name: "问 AI 抽屉" });
  fireEvent.change(within(drawer).getByRole("textbox", { name: "你的问题" }), { target: { value: "为什么通量为零？" } });
  fireEvent.click(within(drawer).getByRole("button", { name: "发送" }));
  await within(drawer).findByText(/场强分布不一定处处为零/);
  expect(streamPayloads).toHaveLength(1);
  expect(streamPayloads[0]).toMatchObject({
    material_artifact_id: aid,
    material_question_id: qid,
    material_kc_id: "kc1.1",
    material_workbench_view: "quiz",
    material_source_pages: [1],
  });
  fireEvent.click(within(quiz).getByRole("button", { name: "返回会话" }));
  const source = await screen.findByRole("button", { name: "来自复习工作台 · 高斯定理" });
  fireEvent.click(source);
  const reopened = await screen.findByRole("dialog", { name: "复习工作台" });
  expect(within(reopened).getByRole("button", { name: "测试" }).getAttribute("aria-pressed")).toBe("true");
});

test("「资料与成果」找回旧资料引用入当前会话，以及删除会话前影响确认弹窗", async () => {
  let deleted = false;
  let currentMaterials: ConversationMaterialItem[] = [makeMaterialItem()];

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
          items: deleted ? [] : [{ id: cid, title: "期末总复习", created_at: date, updated_at: date }],
          total: deleted ? 0 : 1,
        }),
        { status: 200 },
      );
    }
    if (url.endsWith(`/api/conversations/${cid}`) && method === "GET") {
      if (deleted) return new Response("Not Found", { status: 404 });
      return new Response(JSON.stringify(makeDetail(currentMaterials)), { status: 200 });
    }
    if (url.endsWith("/api/materials/library") && method === "GET") {
      return new Response(
        JSON.stringify({
          items: [
            {
              material: {
                id: oldMid,
                display_name: "高数历年笔记.pdf",
                size_bytes: 102400,
                page_count: 12,
                file_sha256: "b".repeat(64),
                dedicated_conversation_id: null,
                created_at: date,
              },
              readability: null,
              guides: [
                {
                  id: "g-old",
                  material_id: oldMid,
                  start_page: 1,
                  end_page: 12,
                  status: "ready",
                  conversation_id: null,
                  created_at: date,
                },
              ],
              artifacts: [],
              conversation_ids: [],
            },
          ],
        }),
        { status: 200 },
      );
    }
    if (url.endsWith(`/api/conversations/${cid}/materials/${oldMid}/attach`) && method === "POST") {
      const attachedItem: ConversationMaterialItem = {
        material: {
          id: oldMid,
          display_name: "高数历年笔记.pdf",
          size_bytes: 102400,
          page_count: 12,
          file_sha256: "b".repeat(64),
          dedicated_conversation_id: null,
          created_at: date,
          updated_at: date,
        },
        readability: null,
        artifacts: [],
        attached_at: date,
      };
      currentMaterials = [...currentMaterials, attachedItem];
      return new Response(JSON.stringify(attachedItem), { status: 200 });
    }
    if (url.endsWith(`/api/conversations/${cid}/delete-impact`) && method === "GET") {
      return new Response(
        JSON.stringify({
          conversation_id: cid,
          title: "期末总复习",
          linked_material_count: currentMaterials.length,
          artifact_count: 1,
          materials_still_accessible: true,
          summary: "将删除本会话的对话记录，关联个人 PDF 与已生成成果保留。",
        }),
        { status: 200 },
      );
    }
    if (url.endsWith(`/api/conversations/${cid}`) && method === "DELETE") {
      deleted = true;
      return new Response(JSON.stringify({ deleted: true, conversation_id: cid }), { status: 200 });
    }
    return new Response("Not Found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);

  fireEvent.click(await screen.findByRole("button", { name: /期末总复习/ }));
  await screen.findByText("静电场讲义.pdf");

  // 1. 打开「资料与成果」找回旧资料并引用到当前会话
  fireEvent.click(screen.getByRole("button", { name: "资料与成果" }));
  await screen.findByText("高数历年笔记.pdf");
  expect(screen.getByText(/第 1–12 页（可回看）/)).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "引用到当前会话" }));
  await screen.findByText("高数历年笔记.pdf");

  // 2. 删除会话前弹出影响确认弹窗并展示关联保留去向
  fireEvent.click(screen.getByTestId(`delete-conversation-${cid}`));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/确定删除会话「期末总复习」吗/)).toBeTruthy();
  expect(within(dialog).getByText(/关联资料：2 份/)).toBeTruthy();
  expect(within(dialog).getByText("本会话保存到错题本的记录也会被移除。")).toBeTruthy();

  fireEvent.click(within(dialog).getByRole("button", { name: "确认删除会话" }));
  await screen.findByText("今天准备复习哪一门科目？");
});


test("扫描 PDF 由用户触发识别，可对照原页并按需生成", async () => {
  const item = makeMaterialItem({ sufficient: false });
  item.material.display_name = "概率论期中试卷.pdf";
  item.material.page_count = 2;
  const pending = item.readability!;
  pending.page_count = 2;
  const recognized = {
    ...pending,
    readable_pages: 2,
    sufficient: true,
    scanned_pages: [],
    ocr_pages: [1, 2],
    summary: "共 2 页由 OCR 转写，请对照原页。",
    available_outputs: ["notes", "quiz", "graph"] as const,
  };
  let didRecognize = false;
  const submittedPages: number[][] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/usage") || url.endsWith("/api/usage/today")) {
      return Response.json(makeUsage());
    }
    if (url.endsWith("/api/papers")) return Response.json({ items: [] });
    if (url.endsWith("/api/conversations")) {
      return Response.json({
        items: [{ id: cid, title: "概率论复习", created_at: date, updated_at: date }],
        total: 1,
      });
    }
    if (url.endsWith(`/api/conversations/${cid}`)) {
      const current = didRecognize
        ? { ...item, readability: { ...recognized, available_outputs: [...recognized.available_outputs] } }
        : item;
      return Response.json(makeDetail([current]));
    }
    if (url.endsWith(`/api/materials/pdfs/${mid}/ocr`)) {
      submittedPages.push(JSON.parse(String(init?.body)).pages);
      didRecognize = true;
      return Response.json({
        recognized_pages: [1, 2],
        reused_pages: [],
        skipped_pages: [],
        failures: [],
        readability: recognized,
      });
    }
    if (url.endsWith(`/api/materials/pdfs/${mid}/pages/2`)) {
      return Response.json({ source: "ocr", extracted_text: "第二页概率题正文" });
    }
    if (url.endsWith(`/api/conversations/${cid}/materials/${mid}/generate`)) {
      return Response.json({ artifact: makeArtifactDetail({ graphStatus: "ready" }), turn_request_id: "req-graph" });
    }
    return new Response("Not Found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: /概率论复习/ }));
  const tools = await screen.findByText("资料工具");
  expect(tools.closest("details")?.open).toBe(false);
  fireEvent.click(tools);
  const startOcr = await screen.findByRole("button", { name: "识别扫描页" });
  expect(screen.getByText(/把|发送给 DeepSeek/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "生成知识图谱" })).toBeNull();
  expect(submittedPages).toEqual([]);

  fireEvent.click(startOcr);
  const generateGraph = await screen.findByRole("button", { name: "生成知识图谱" });
  expect(submittedPages).toEqual([[1, 2]]);
  fireEvent.change(screen.getByLabelText("识别文字页码"), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "查看识别文字" }));
  await screen.findByText("第二页概率题正文");
  expect(screen.getByRole("link", { name: "对照原页" }).getAttribute("href")).toBe(`/api/materials/pdfs/${mid}/file#page=2`);
  fireEvent.click(generateGraph);
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) =>
    String(url).endsWith(`/api/conversations/${cid}/materials/${mid}/generate`)
  )).toBe(true));
});


test("试卷没有匹配题时在聊天说明，内部参数错误不打断对话", async () => {
  const detail = makeDetail([]);
  detail.conversation.title = "概率论原卷";
  detail.turns = [{
    request_id: "33333333-3333-4333-8333-333333333332",
    seq: 1,
    kind: "dynamic",
    query: "给出选择题答案",
    status: "completed",
    business_status: "succeeded",
    result: null,
    tutoring: null,
    error: null,
    started_at: date,
    finished_at: date,
    assistant_message: "这份试卷没有选择题。",
    composite_results: [{
      action: "solve_uploaded_exam",
      status: "success",
      message: "这份试卷没有选择题。",
      details: { material_id: mid, source_pages: [1, 2] },
    }, {
      action: "tool:generate_material_artifact",
      status: "failed",
      message: "Value error, 只有指定原题时才填写题号。",
      details: { rejected: true, tool: "generate_material_artifact" },
    }],
  }];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api/usage/today")) return Response.json(makeUsage());
    if (url.endsWith("/api/papers")) return Response.json({ items: [] });
    if (url.endsWith("/api/conversations")) return Response.json({ items: [detail.conversation], total: 1 });
    if (url.endsWith(`/api/conversations/${cid}`)) return Response.json(detail);
    return new Response("Not Found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: /概率论原卷/ }));
  await screen.findByText("这份试卷没有选择题。");
  expect(screen.getByRole("link", { name: "原卷第 2 页" })).toBeTruthy();
  expect(screen.queryByText(/Value error|草稿已保留/)).toBeNull();
});


test.each([
  ["识别并生成", "recognize"],
  ["仅生成部分成果", "partial"],
])("聊天里确认缺页后的 %s 保留原范围和产物类型", async (button, mode) => {
  const requests: unknown[] = [];
  const initial = makeDetail([makeMaterialItem()]);
  initial.turns.push({
    request_id: "87777777-7777-4777-8777-777777777777",
    seq: 1, kind: "dynamic", status: "completed", query: "按原件做章节测试",
    result: null, tutoring: null, assistant_message: "还需要识别第 3 页和第 4 页。",
    composite_results: [{
      action: "generate_material_artifact",
      status: "clarification",
      message: "还有 2 页待识别。",
      details: {
        code: "MATERIAL_COVERAGE_REQUIRED", can_generate_partial: true, missing_pages: [3, 4],
        material_id: mid, start_page: 2, end_page: 23, outputs: ["quiz"], course_hint: "大学物理",
      },
    }],
    error: null, started_at: date, finished_at: date,
  });

  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.endsWith("/api/conversations") && init?.method !== "POST") {
      return new Response(JSON.stringify({ items: [initial.conversation], total: 1 }));
    }
    if (url.endsWith("/api/conversations/" + cid)) return new Response(JSON.stringify(initial));
    if (url.endsWith("/api/conversations/" + cid + "/materials/" + mid + "/generate")) {
      requests.push(JSON.parse(String(init?.body)));
      return new Response(JSON.stringify({
        artifact: makeArtifactDetail({ notesStatus: "none", quizStatus: "ready" }),
      }));
    }
    if (url.includes("/api/usage")) return new Response(JSON.stringify(makeUsage()));
    return new Response(JSON.stringify({ items: [] }));
  }));

  render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: /高数与物理复习/ }));
  await screen.findByText("还需要识别第 3 页和第 4 页。");
  expect(requests).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: button }));
  await waitFor(() => expect(requests).toEqual([{
    outputs: ["quiz"], start_page: 2, end_page: 23, course_hint: "大学物理", coverage_mode: mode,
  }]));
});


test("生成记录共用一张成果卡，工作台按知识点跳转、对照原页并保留共享批注", async () => {
  const detail = makeDetail([makeMaterialItem()]);
  const artifact = makeArtifactDetail({ notesStatus: "ready", quizStatus: "none", graphStatus: "ready" });
  artifact.coverage = {
    input_pages: [], included_pages: [2, 3], missing_pages: [], review_pages: [3], excluded_pages: [1], partial: false,
  };
  artifact.notes_html = '<h2>静电场</h2><h3 data-kc="kc1.1">高斯定理核心结论<span data-slides="2"></span></h3><div class="tldr">闭合曲面积分。</div><p>完整推导的内容。</p>';
  artifact.coverage_warnings = ["扫描核对提示"];
  artifact.graph_tree = {
    title: "静电场",
    nodes: [{ id: "node-1", kc_id: "kc1.1", label: "真空中的高斯定理", summary: "电通量与净电荷", source_refs: ["2-3", "3"] }],
  };
  let reads = 0;
  const addTurn = (seq: number, output: "notes" | "graph") => detail.turns.push({
    request_id: "21111111-1111-4111-8111-11111111111" + seq,
    seq, kind: "dynamic", status: "completed", result: null, tutoring: null,
    query: output === "notes" ? "生成章节笔记" : "生成知识图谱",
    assistant_message: output === "notes" && seq === 1
      ? "已为你准备好章节笔记。点成果卡即可打开。"
      : output === "notes" ? "章节笔记已展示在下方。" : "知识图谱已展示在下方。",
    composite_results: [{
      action: "generate_material_artifact", status: "success", message: "知识图谱已展示在下方。",
      details: {
        artifact_id: aid, material_id: mid, graph_status: "ready", notes_status: "ready", outputs: [output],
      },
    }],
    error: null, started_at: date, finished_at: date,
  });

  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api/conversations")) return Response.json({ items: [detail.conversation], total: 1 });
    if (url.endsWith("/api/conversations/" + cid)) return Response.json(detail);
    if (url.endsWith("/api/conversations/" + cid + "/materials/" + mid + "/generate")) {
      addTurn(1, "notes");
      addTurn(2, "graph");
      addTurn(3, "notes");
      addTurn(4, "graph");
      return Response.json({ artifact, turn_request_id: detail.turns[0].request_id });
    }
    if (url.endsWith("/api/materials/artifacts/" + aid)) {
      reads += 1;
      return Response.json(artifact);
    }
    if (url.endsWith("/api/materials/pdfs/" + mid + "/pages/3")) {
      return Response.json({ page_no: 3, extracted_text: "原页里的高斯定理", warnings: [] });
    }
    if (url.endsWith("/kc-notes/kc1.1")) {
      artifact.kc_notes = { "kc1.1": "复习通量" };
      return Response.json({ node_id: "node-1", note_text: "复习通量" });
    }
    if (url.includes("/api/usage")) return Response.json(makeUsage());
    return Response.json({ items: [] });
  }));

  const openWorkspace = () => render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
  const workspace = openWorkspace();
  fireEvent.click(await screen.findByRole("button", { name: /高数与物理复习/ }));
  fireEvent.click(await screen.findByRole("button", { name: "生成知识图谱" }));
  const card = await screen.findByRole("region", { name: "复习成果" });
  expect(screen.getAllByRole("region", { name: "复习成果" })).toHaveLength(1);
  expect(screen.queryByText("闭合曲面积分。")).toBeNull();
  expect(screen.queryByText(/点成果卡即可打开/)).toBeNull();
  const queuedFrames = new Map<number, FrameRequestCallback>();
  let frameId = 0;
  const requestFrame = vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
    queuedFrames.set(++frameId, callback);
    return frameId;
  });
  const cancelFrame = vi.spyOn(window, "cancelAnimationFrame").mockImplementation((id) => {
    queuedFrames.delete(id);
  });
  try {
    fireEvent.click(await within(card).findByRole("button", { name: "笔记" }));
    const dialog = await screen.findByRole("dialog", { name: "复习工作台" });
    const workbench = within(dialog);
    expect(workbench.getByText("闭合曲面积分。")).toBeTruthy();
    expect(workbench.queryByRole("complementary", { name: "原页对照" })).toBeNull();
    fireEvent.click(workbench.getByRole("button", { name: "原页" }));
    const original = workbench.getByRole("complementary", { name: "原页对照" });
    expect(within(original).getByRole("img", { name: "资料第 2 页原件" })).toBeTruthy();
    fireEvent.click(within(original).getByRole("button", { name: "下一页" }));
    fireEvent.click(workbench.getByRole("button", { name: "查看提取文本" }));
    expect(queuedFrames.size).toBeGreaterThan(0);
    act(() => {
      const callbacks = [...queuedFrames.values()];
      queuedFrames.clear();
      callbacks.forEach((callback) => callback(0));
    });
    expect(workbench.getByRole("img", { name: "资料第 3 页原件" })).toBeTruthy();
    await workbench.findByText("原页里的高斯定理");
  } finally {
    requestFrame.mockRestore();
    cancelFrame.mockRestore();
  }
  const workbench = within(screen.getByRole("dialog", { name: "复习工作台" }));
  fireEvent.click(workbench.getByRole("button", { name: "关闭" }));
  fireEvent.click(workbench.getByRole("button", { name: "速览" }));
  expect(workbench.queryByText("完整推导的内容。")).toBeNull();
  expect(workbench.getByText("闭合曲面积分。")).toBeTruthy();
  fireEvent.click(workbench.getByRole("button", { name: "速览" }));

  fireEvent.click(workbench.getByRole("button", { name: "在图谱中定位" }));
  expect(workbench.getByRole("button", { name: "高斯定理" }).getAttribute("aria-pressed")).toBe("true");
  const graphDetail = workbench.getByRole("complementary", { name: "知识点详情" });
  fireEvent.change(within(graphDetail).getByLabelText("知识点批注"), { target: { value: "复习通量" } });
  fireEvent.click(within(graphDetail).getByRole("button", { name: "保存批注" }));
  await within(graphDetail).findByText("我的批注 · 已保存");
  fireEvent.click(within(graphDetail).getByRole("button", { name: "读这一节笔记" }));
  expect(workbench.getByLabelText<HTMLTextAreaElement>("知识点批注").value).toBe("复习通量");
  await waitFor(() => expect(location.hash).toContain("kc=kc1.1"));

  workspace.unmount();
  openWorkspace();
  const reopened = await screen.findByRole("dialog", { name: "复习工作台" });
  expect(within(reopened).getByText("闭合曲面积分。")).toBeTruthy();
  expect(within(reopened).getByLabelText<HTMLTextAreaElement>("知识点批注").value).toBe("复习通量");
  expect(reads).toBeGreaterThanOrEqual(2);
});

test("资料进度请求限流后退避，生成完成后结束轮询", async () => {
  const detail = makeDetail([makeMaterialItem()]);
  let progressCalls = 0;
  let progressStatus = 200;
  let completeGeneration!: (response: Response) => void;
  const pendingGeneration = new Promise<Response>((resolve) => {
    completeGeneration = resolve;
  });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api/conversations")) {
      return Response.json({ items: [detail.conversation], total: 1 });
    }
    if (url.endsWith(`/api/conversations/${cid}`)) return Response.json(detail);
    if (url.endsWith(`/api/conversations/${cid}/materials/${mid}/generate`)) {
      return pendingGeneration;
    }
    if (url.endsWith(`/api/conversations/${cid}/materials/progress`)) {
      progressCalls += 1;
      return Response.json({ items: [], chat_turn_request_id: null }, { status: progressStatus });
    }
    if (url.includes("/api/usage")) return Response.json(makeUsage());
    return Response.json({ items: [] });
  }));

  vi.useFakeTimers();
  let unmount: (() => void) | undefined;
  const advance = (milliseconds: number) => act(async () => {
    await vi.advanceTimersByTimeAsync(milliseconds);
  });
  try {
    await act(async () => {
      ({ unmount } = render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /高数与物理复习/ }));
    });
    await advance(250);
    expect(progressCalls).toBe(1);

    progressStatus = 429;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "生成知识图谱" }));
    });
    await advance(250);
    expect(progressCalls).toBe(2);
    await advance(2_399);
    expect(progressCalls).toBe(2);
    await advance(1);
    expect(progressCalls).toBe(3);
    await advance(4_799);
    expect(progressCalls).toBe(3);

    progressStatus = 200;
    await advance(1);
    expect(progressCalls).toBe(4);
    await advance(1_199);
    expect(progressCalls).toBe(4);
    await advance(1);
    expect(progressCalls).toBe(5);

    progressStatus = 429;
    await act(async () => {
      completeGeneration(Response.json({
        artifact: makeArtifactDetail({ notesStatus: "none", quizStatus: "none", graphStatus: "ready" }),
      }));
    });
    expect(progressCalls).toBe(6);

    progressStatus = 200;
    await advance(1_200);
    expect(progressCalls).toBe(7);
    await advance(60_000);
    expect(progressCalls).toBe(7);
  } finally {
    unmount?.();
    vi.useRealTimers();
  }
});
