import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import ConversationWorkspace from "./ConversationWorkspace";
import type { ConversationDetail, ExerciseView } from "../lib/conversationsApi";

const date = "2026-10-25T08:00:00+00:00";
const cid = "11111111-1111-4111-8111-111111111111";
const eid = "22222222-2222-4222-8222-222222222222";
const pid = "33333333-3333-4333-8333-333333333333";
const bid = "77777777-7777-4777-8777-777777777777";
function fixture(overrides: Partial<ExerciseView> = {}): ConversationDetail {
  const exercise: ExerciseView = {
    id: eid, conversation_id: cid, problem_id: pid, root_problem_id: pid,
    statement_text: "x=[10]; y=x; y += [20]; print(x)", knowledge_point: "列表别名",
    status: "pending_answer", student_answer_text: null, student_solution_text: null,
    answered_after_revealed: false, revealed_solution_at: null, reference_solution_text: null,
    evaluation: null, attachments: [], created_at: date, updated_at: date,
    ...overrides,
  };
  return {
    conversation: { id: cid, title: "请求链路 A", created_at: date, updated_at: date },
    turns: [{ request_id: "44444444-4444-4444-8444-444444444444", seq: 1,
      kind: "dynamic", query: "练一道类似题", status: "completed", result: null, tutoring: null,
      exercise, evaluation: null, error: null, started_at: date, finished_at: date }],
    active_problem: { id: pid, root_problem_id: pid, version: 1, statement_text: "列表题",
      status: "explained", clarity: "clear", source: { type: "none", text: null, paper_id: null, paper_title: null },
      attachments: [], student_answer_text: null, solution_text: null, self_reported_understood: false,
      created_at: date, updated_at: date },
    active_exercise: exercise, record: null,
  };
}

async function open(options: { fail?: boolean; status?: string; retryable?: boolean; detailFailOnReads?: number[]; exerciseOverrides?: Partial<ExerciseView>; awaitForm?: boolean; withClarification?: boolean } = {}) {
  const detail = fixture(options.exerciseOverrides);
  if (options.withClarification) {
    // F008 移除输入模式按钮后，“补充”只剩历史卡片上的明确对象操作；
    // 这里补一条“题目需补充”的辅导轮次，让“补充当前题”按钮真实出现。
    detail.turns.push({
      request_id: "55555555-5555-4555-8555-555555555555",
      seq: 0,
      kind: "problem",
      query: "讲讲这道题",
      status: "completed",
      result: null,
      tutoring: {
        kind: "needs_clarification",
        problem: { ...detail.active_problem!, status: "awaiting_supplement", clarity: "unclear" },
        questions: ["第 2 步的系数是多少？"],
        unclear_points: ["系数被遮挡"],
      },
      exercise: null,
      evaluation: null,
      error: null,
      started_at: date,
      finished_at: date,
    });
  }
  const other = fixture();
  other.conversation = { ...other.conversation, id: bid, title: "请求链路 B" };
  other.turns = [{ ...other.turns[0], exercise: { ...other.active_exercise!, id: "other-exercise", conversation_id: bid } }];
  const requests: Record<string, unknown>[] = [];
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/turns")) {
      const body = JSON.parse(String(init?.body)); requests.push(body);
      if (options.fail) return new Response(JSON.stringify({ detail: "当前题目已经讲解过；如要改题干，请使用纠正。" }), { status: 409 });
      const exercise = body.kind === "exercise" ? {
        ...detail.active_exercise!, student_answer_text: body.student_answer ?? null,
        student_solution_text: body.solution_text ?? null, status: "submitted" as const,
        reference_solution_text: "[10,20]，同一列表原地修改",
        evaluation: { is_correct: true, final_answer_assessment: "答案可核对", step_by_step_analysis: String(body.solution_text), guidance: "继续练习", needs_supplement: false },
      } : null;
      const result = { type: "result", conversation_id: cid, request_id: body.request_id,
        kind: body.kind === "exercise" ? "exercise" : "dynamic", result: null, tutoring: null,
        exercise, evaluation: exercise?.evaluation, assistant_message: "已处理", business_status: options.status ?? "succeeded",
        retryable: options.retryable ?? false, retry_hint: options.retryable ? "重试未完成步骤" : null };
      detail.turns.push({ ...result, seq: 2, query: body.query ?? "", status: "completed", error: null, started_at: date, finished_at: date } as ConversationDetail["turns"][number]);
      if (exercise) { detail.active_exercise = exercise; detail.turns[0].exercise = exercise; }
      return new Response(`data: ${JSON.stringify(result)}\n\n`, { headers: { "Content-Type": "text/event-stream" } });
    }
    if (url.endsWith(cid)) {
      reads += 1;
      if (options.detailFailOnReads?.includes(reads)) {
        return new Response(JSON.stringify({ detail: "本地学习数据暂时不可用，请稍后再试。" }), { status: 503 });
      }
      return Response.json(detail);
    }
    if (url.endsWith(bid)) return Response.json(other);
    return Response.json({ items: [detail.conversation, other.conversation], total: 2 });
  }));
  const mount = () => render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
  let view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /请求链路 A/ }));
  if (options.awaitForm === false) {
    await screen.findByText("x=[10]; y=x; y += [20]; print(x)");
  } else {
    await screen.findByLabelText("最终答案");
  }
  return { requests, detail, reads: () => reads, refresh: async () => {
    view.unmount(); view = mount(); await screen.findAllByText("你的作答");
  } };
}
afterEach(() => { vi.unstubAllGlobals(); });

describe("真实工作区 → streamTurn → fetch 请求链路", () => {
  test("失败后练习与主输入草稿按会话隔离，返回原会话仍可编辑", async () => {
    await open({ fail: true });
    fireEvent.change(screen.getByLabelText("最终答案"), { target: { value: "A 卡片答案" } });
    fireEvent.change(screen.getByLabelText("解题步骤 / 推导思路（选填）"), { target: { value: "A 卡片过程" } });
    fireEvent.change(screen.getByPlaceholderText("我的答案"), { target: { value: "A 主输入答案" } });
    fireEvent.click(screen.getByRole("button", { name: "提交作答并评价" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: /请求链路 B/ }));
    await screen.findByRole("heading", { name: "请求链路 B" });
    expect((screen.getByLabelText("最终答案") as HTMLInputElement).value).toBe("");
    expect((screen.getByPlaceholderText("我的答案") as HTMLTextAreaElement).value).toBe("");
    fireEvent.change(screen.getByPlaceholderText("我的答案"), { target: { value: "B 主输入答案" } });
    fireEvent.click(screen.getByRole("button", { name: /请求链路 A/ }));
    await screen.findByRole("heading", { name: "请求链路 A" });
    expect((screen.getByLabelText("最终答案") as HTMLInputElement).value).toBe("A 卡片答案");
    expect((screen.getByLabelText("解题步骤 / 推导思路（选填）") as HTMLTextAreaElement).value).toBe("A 卡片过程");
    expect((screen.getByPlaceholderText("我的答案") as HTMLTextAreaElement).value).toBe("A 主输入答案");
  });
  test.each(["failed", "partial"])("%s 结果保留草稿，重试只引用原轮次", async (status) => {
    const state = await open({ status, retryable: true });
    fireEvent.change(screen.getByPlaceholderText(/围绕当前题目继续追问/), { target: { value: "保存并出题" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    const button = await screen.findByRole("button", { name: "重试未完成步骤" });
    expect(screen.getByText(status === "failed" ? "未完成" : "部分完成")).toBeTruthy();
    expect((screen.getByPlaceholderText(/围绕当前题目继续追问/) as HTMLTextAreaElement).value).toBe("保存并出题");
    fireEvent.click(button);
    await waitFor(() => expect(state.requests).toHaveLength(2));
    expect(Object.keys(state.requests[1]).sort()).toEqual(["request_id", "retry_of"]);
    expect(state.requests[1].retry_of).toBe(state.requests[0].request_id);
  });

  test("业务拒绝没有直接重试按钮", async () => {
    await open({ status: "failed", retryable: false });
    fireEvent.change(screen.getByPlaceholderText(/围绕当前题目继续追问/), { target: { value: "重交完整评价" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText("未完成");
    expect(screen.queryByRole("button", { name: "重试未完成步骤" })).toBeNull();
  });
  test.each([true, false])("卡片过程独立发送、保存、显示与刷新；填写答案=%s", async (withAnswer) => {
    const state = await open();
    if (withAnswer) fireEvent.change(screen.getByLabelText("最终答案"), { target: { value: "[10,20]" } });
    fireEvent.change(screen.getByLabelText("解题步骤 / 推导思路（选填）"), { target: { value: "y 与 x 引用同一列表，+= 原地修改。" } });
    fireEvent.change(screen.getByPlaceholderText("我的解题过程；也可以添加解法图片"), { target: { value: "主输入区旧过程" } });
    fireEvent.click(screen.getByRole("button", { name: "提交作答并评价" }));
    await waitFor(() => expect(state.requests).toHaveLength(1));
    expect(state.requests[0]).toMatchObject({ kind: "exercise", exercise_id: eid, solution_text: "y 与 x 引用同一列表，+= 原地修改。" });
    expect(state.requests[0].student_answer).toBe(withAnswer ? "[10,20]" : undefined);
    await screen.findAllByText("解题步骤：");
    expect((screen.getByPlaceholderText("我的解题过程；也可以添加解法图片") as HTMLTextAreaElement).value).toBe("主输入区旧过程");
    await state.refresh();
    expect(screen.getAllByText(/y 与 x 引用同一列表/).length).toBeGreaterThan(0);
  });

  test("折叠草稿不改变自然语言操作类型，发送后仍保留", async () => {
    const state = await open();
    fireEvent.change(screen.getByPlaceholderText("我的答案"), { target: { value: "旧答案" } });
    fireEvent.change(screen.getByPlaceholderText("我的解题过程；也可以添加解法图片"), { target: { value: "旧过程" } });
    fireEvent.change(screen.getByPlaceholderText(/围绕当前题目继续追问/), { target: { value: "再来一道类似题" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(state.requests).toHaveLength(1));
    expect(state.requests[0]).toMatchObject({ kind: "auto", query: "再来一道类似题" });
    expect(state.requests[0]).not.toHaveProperty("solution_text");
    expect(state.requests[0]).not.toHaveProperty("student_answer");
    await waitFor(() => expect((screen.getByPlaceholderText(/围绕当前题目继续追问/) as HTMLTextAreaElement).value).toBe(""));
    expect((screen.getByPlaceholderText("我的答案") as HTMLTextAreaElement).value).toBe("旧答案");
  });

  test("显式补充携带字段和目标；409 明显、持续可见且保留草稿", async () => {
    const state = await open({ fail: true, withClarification: true });
    fireEvent.click(screen.getByRole("button", { name: "补充当前题" }));
    fireEvent.change(screen.getByPlaceholderText("我的答案"), { target: { value: "旧答案" } });
    fireEvent.change(screen.getByPlaceholderText("我的解题过程；也可以添加解法图片"), { target: { value: "补充过程" } });
    fireEvent.change(screen.getByPlaceholderText(/围绕当前题目继续追问/), { target: { value: "补充信息" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("当前题目已经讲解过");
    expect(state.requests[0]).toMatchObject({ kind: "supplement", problem_id: pid, student_answer: "旧答案", solution_text: "补充过程" });
    fireEvent.click(within(alert).getByRole("button", { name: "刷新会话记录" }));
    await waitFor(() => expect(state.reads()).toBeGreaterThanOrEqual(3));
    expect(screen.getByRole("alert").textContent).toContain("当前题目已经讲解过");
    expect((screen.getByPlaceholderText("我的答案") as HTMLTextAreaElement).value).toBe("旧答案");
    fireEvent.click(screen.getByRole("button", { name: "切回普通聊天" }));
    expect(screen.getByPlaceholderText(/围绕当前题目继续追问/)).toBeTruthy();
  });

  test("订正提交携带 attempt_kind=correction 且旧尝试仍在卡片里", async () => {
    const attempts = [
      { id: `${eid}-a1`, attempt_no: 1, kind: "initial" as const,
        student_answer_text: "1", student_solution_text: "代换。",
        answered_after_revealed: false,
        evaluation: { is_correct: true, final_answer_assessment: "正确", step_by_step_analysis: "完整", guidance: "继续", needs_supplement: false },
        request_id: null, attachments: [], created_at: date, updated_at: date },
    ];
    const state = await open({
      awaitForm: false,
      exerciseOverrides: {
        status: "submitted", student_answer_text: "1", student_solution_text: "代换。",
        evaluation: attempts[0].evaluation, attempts,
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "再订正一次" }));
    fireEvent.change(screen.getByLabelText("最终答案"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "提交订正并重新评价" }));
    await waitFor(() => expect(state.requests).toHaveLength(1));
    expect(state.requests[0]).toMatchObject({ kind: "exercise", exercise_id: eid, attempt_kind: "correction", student_answer: "2" });
  });

  test("补充提交携带 attempt_kind=supplement 且不展示参考答案", async () => {
    const supplement = { is_correct: null, final_answer_assessment: "缺少推导", step_by_step_analysis: "未看到步骤", guidance: "请补充", needs_supplement: true };
    const state = await open({
      awaitForm: false,
      exerciseOverrides: {
        status: "pending_answer", reference_solution_text: null, revealed_solution_at: null,
        evaluation: supplement,
        attempts: [{ id: `${eid}-a1`, attempt_no: 1, kind: "initial", student_answer_text: "1",
          student_solution_text: null, answered_after_revealed: false, evaluation: supplement,
          request_id: null, attachments: [], created_at: date, updated_at: date }],
      },
    });
    expect(screen.queryByText(/参考解答与推导/)).toBeNull();
    fireEvent.change(screen.getByLabelText("最终答案"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("解题步骤 / 推导思路（选填）"), { target: { value: "补充步骤" } });
    fireEvent.click(screen.getByRole("button", { name: "补充并重新评价" }));
    await waitFor(() => expect(state.requests).toHaveLength(1));
    expect(state.requests[0]).toMatchObject({ attempt_kind: "supplement", solution_text: "补充步骤" });
  });

  test("409 后详情读取失败，告警与草稿仍保留，恢复记录后可继续", async () => {
    const state = await open({ fail: true, detailFailOnReads: [2], withClarification: true });
    fireEvent.click(screen.getByRole("button", { name: "补充当前题" }));
    fireEvent.change(screen.getByPlaceholderText("我的答案"), { target: { value: "要保留的答案" } });
    fireEvent.change(screen.getByPlaceholderText(/围绕当前题目继续追问/), { target: { value: "补充信息" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect((await screen.findByRole("alert")).textContent).toContain("当前题目已经讲解过");
    // 输入区已卸载，但告警仍显示在读取失败卡片上方。
    await screen.findByText("没能读取会话记录");
    expect(screen.getByRole("alert").textContent).toContain("当前题目已经讲解过");
    fireEvent.click(screen.getByRole("button", { name: "刷新会话记录" }));
    await screen.findByPlaceholderText(/围绕当前题目继续追问/);
    expect(state.reads()).toBeGreaterThanOrEqual(3);
    expect(screen.getByRole("alert").textContent).toContain("当前题目已经讲解过");
    expect((screen.getByPlaceholderText("我的答案") as HTMLTextAreaElement).value).toBe("要保留的答案");
    expect((screen.getByPlaceholderText(/围绕当前题目继续追问/) as HTMLTextAreaElement).value).toBe("补充信息");
  });
});

test("真题卡片按钮发送明确的 question kind 与来源轮次", async () => {
  const card = {
    question_id: "py-2024-2025-s1-final-choice-1",
    build_id: "iv-0123456789abcdef",
    question_type: "choice" as const,
    number_label: "1",
    statement: "下列 Python 表达式的值是？",
    content_sha256: "a".repeat(64),
    options: [{ label: "A", text: "1" }, { label: "B", text: "2" }],
    code_lines: [], blank_marks: [], formulas: [], assets: [],
    pages: { statement: [1], answer: [2] },
    knowledge_points: [],
    answer: { state: "original" as const, available: true },
    review: { status: "approved", approved: true, stale: false },
    source: { paper_id: "py-2024-2025-s1-final", paper_title: "Python 期末卷",
      exam_type: "final", academic_year: "2024-2025", question_number: "1", may_show_answer: true },
    content_issues: [], selected: false, answer_revealed: false, answer_detail: null,
    source_request_id: "66666666-6666-4666-8666-666666666666",
  };
  const detail: ConversationDetail = {
    conversation: { id: cid, title: "真题请求链路", created_at: date, updated_at: date },
    turns: [{
      request_id: card.source_request_id, seq: 1, kind: "dynamic", query: "找一道真题",
      status: "completed", result: null, tutoring: null, exercise: null, evaluation: null,
      error: null, started_at: date, finished_at: date,
      question_results: [card], question_result_status: "ok",
    }],
    active_problem: null, active_exercise: null, active_question: card, record: null,
  };
  const requests: Record<string, unknown>[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/turns")) {
      const body = JSON.parse(String(init?.body));
      requests.push(body);
      const result = { type: "result", conversation_id: cid, request_id: body.request_id,
        kind: body.kind === "auto" ? "dynamic" : body.kind, result: null, tutoring: null, question: { ...card, selected: true,
          ...(body.kind === "view_question_answer" ? { answer_revealed: true,
            answer_detail: { state: "original", text: "B" } } : {}) },
        assistant_message: "已选中", business_status: "succeeded", retryable: false };
      detail.turns.push({
        ...result, seq: detail.turns.length + 1, query: body.query ?? "",
        status: "completed", error: null, started_at: date, finished_at: date,
      } as ConversationDetail["turns"][number]);
      return new Response(`data: ${JSON.stringify(result)}\n\n`, { headers: { "Content-Type": "text/event-stream" } });
    }
    if (url.endsWith(cid)) return Response.json(detail);
    return Response.json({ items: [detail.conversation], total: 1 });
  }));

  render(<ConversationWorkspace downloads={{}} onDownload={vi.fn()} onOpenRecord={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: /真题请求链路/ }));
  await screen.findByText("下列 Python 表达式的值是？");
  fireEvent.click(screen.getByRole("button", { name: "选第 1 道题" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0]).toMatchObject({
    kind: "select_question",
    question_id: card.question_id,
    question_source_turn: card.source_request_id,
  });
  expect(requests[0].query).toBe("");

  fireEvent.click(await screen.findByRole("button", { name: "讲解这道题" }));
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1]).toMatchObject({ kind: "explain_question", question_id: card.question_id });

  fireEvent.click(screen.getAllByRole("button", { name: "生成一道变式" })[0]);
  await waitFor(() => expect(requests).toHaveLength(3));
  expect(requests[2]).toMatchObject({
    kind: "auto",
    question_id: card.question_id,
    question_source_turn: card.source_request_id,
    question_action: "exercise",
  });

  fireEvent.click(screen.getAllByRole("button", { name: "保存到错题本" })[0]);
  await waitFor(() => expect(requests).toHaveLength(4));
  expect(requests[3]).toMatchObject({
    kind: "auto",
    question_id: card.question_id,
    question_source_turn: card.source_request_id,
    question_action: "record",
  });

  // 当前轮次已有其他上下文时，仍按被点击的历史卡片提交答案。
  fireEvent.click(screen.getAllByRole("radio", { name: "B. 2" })[0]);
  fireEvent.click(screen.getAllByRole("button", { name: "提交并讲解" })[0]);
  await waitFor(() => expect(requests).toHaveLength(5));
  expect(requests[4]).toMatchObject({ kind: "explain_question", student_answer: "B",
    question_id: card.question_id, question_source_turn: card.source_request_id });
  expect(requests[4]).not.toHaveProperty("exercise_id");
  expect(requests[4]).not.toHaveProperty("solution_text");
  expect(screen.getAllByText("下列 Python 表达式的值是？")).toHaveLength(1);
  expect(screen.getAllByRole("button", { name: "查看题目主卡" }).length).toBeGreaterThan(0);
  const toggle = screen.getByRole("button", { name: "会话列表" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle);
  expect(screen.getByRole("button", { name: "收起会话列表" }).getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "查看参考答案" }));
  await waitFor(() => expect(requests).toHaveLength(6));
  await waitFor(() => expect((screen.getByText("参考答案（已查看，点击展开）").closest("details") as HTMLDetailsElement).open).toBe(true));
  expect(screen.getAllByText("下列 Python 表达式的值是？")).toHaveLength(1);
});

