import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import ConversationTurn from "./ConversationTurn";
import ConversationWorkspace from "./ConversationWorkspace";
import ExerciseAttemptReceipt from "./ExerciseAttemptReceipt";
import ExerciseCard, { type ExerciseDraftImage } from "./ExerciseCard";
import MistakeBook from "./MistakeBook";
import type {
  ConversationDetail,
  ConversationSummary,
  ConversationTurn as StoredTurn,
  ExerciseAttemptView,
  ExerciseEvaluation,
  ExerciseView,
} from "../lib/conversationsApi";
import type { MistakeRecordDetail } from "../lib/mistakesApi";

const {
  listConversations,
  getConversation,
  streamTurn,
  uploadAttachment,
  createMistakeRecord,
  listMistakeRecords,
  getMistakeRecord,
} = vi.hoisted(() => ({
  listConversations: vi.fn(),
  getConversation: vi.fn(),
  streamTurn: vi.fn(),
  uploadAttachment: vi.fn(),
  createMistakeRecord: vi.fn(),
  listMistakeRecords: vi.fn(),
  getMistakeRecord: vi.fn(),
}));

vi.mock("../lib/attachmentsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/attachmentsApi")>()),
  uploadAttachment,
}));

vi.mock("../lib/mistakesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/mistakesApi")>()),
  createMistakeRecord,
  listMistakeRecords,
  getMistakeRecord,
}));

vi.mock("../lib/conversationsApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/conversationsApi")>();
  return {
    ...actual,
    listConversations,
    getConversation,
    streamTurn,
    createRequestId: () => "11111111-1111-4111-8111-111111111111",
  };
});

const date = "2026-11-15T10:00:00+08:00";

function makeExercise(overrides: Partial<ExerciseView> = {}): ExerciseView {
  return {
    id: "ex-1",
    conversation_id: "conv-1",
    problem_id: "prob-1",
    root_problem_id: "root-1",
    statement_text: String.raw`计算 $\lim_{x\to 0}\frac{\sin 2x}{x}$`,
    knowledge_point: "等价无穷小",
    status: "pending_answer",
    reference_solution_text: null,
    student_answer_text: null,
    student_solution_text: null,
    revealed_solution_at: null,
    answered_after_revealed: false,
    evaluation: null,
    attachments: [],
    attempts: [],
    created_at: date,
    updated_at: date,
    ...overrides,
  };
}

function makeEvaluation(
  overrides: Partial<ExerciseEvaluation> = {},
): ExerciseEvaluation {
  return {
    is_correct: false,
    final_answer_assessment: "答案有误。",
    step_by_step_analysis: "未提供推导过程，暂无法定位具体错误步骤。",
    guidance: "请补充关键步骤。",
    needs_supplement: true,
    ...overrides,
  };
}

function makeAttempt(
  overrides: Partial<ExerciseAttemptView> = {},
): ExerciseAttemptView {
  return {
    id: "att-1",
    attempt_no: 1,
    kind: "initial",
    student_answer_text: "2",
    student_solution_text: "",
    answered_after_revealed: false,
    evaluation: makeEvaluation(),
    request_id: "turn-ans",
    attachments: [],
    created_at: date,
    updated_at: date,
    ...overrides,
  };
}

describe("F017 体验修复前端回归", () => {
  test("1. 题卡支持上传与粘贴过程图、缩略图预览与移除，uploading 禁提交且仅图片可提交", () => {
    const onAddImages = vi.fn();
    const onRemoveImage = vi.fn();
    const onSubmit = vi.fn();
    const pngFile = new File(["png"], "step.png", { type: "image/png" });

    const readyImage: ExerciseDraftImage = {
      localId: "local-1",
      file: pngFile,
      previewUrl: "blob:step",
      status: "ready",
      uploadedId: "att-ready-1",
    };
    const uploadingImage: ExerciseDraftImage = {
      ...readyImage,
      localId: "local-2",
      status: "uploading",
      uploadedId: undefined,
    };

    const { rerender } = render(
      <ExerciseCard
        exercise={makeExercise()}
        draftImages={[uploadingImage]}
        onAddImages={onAddImages}
        onRemoveImage={onRemoveImage}
        onSubmitAnswer={onSubmit}
      />,
    );

    // 粘贴图片触发 onAddImages
    fireEvent.paste(screen.getByLabelText("解题步骤 / 推导思路（选填）"), {
      clipboardData: { items: [{ kind: "file", getAsFile: () => pngFile }] },
    });
    expect(onAddImages).toHaveBeenCalledWith("ex-1", [pngFile]);

    // uploading 状态禁用提交按钮
    const submitUploading = screen.getByRole("button", {
      name: "图片上传中...",
    }) as HTMLButtonElement;
    expect(submitUploading.disabled).toBe(true);

    // 就绪状态：放大预览弹窗与仅图片提交
    rerender(
      <ExerciseCard
        exercise={makeExercise()}
        draftImages={[readyImage]}
        onAddImages={onAddImages}
        onRemoveImage={onRemoveImage}
        onSubmitAnswer={onSubmit}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "预览待提交过程图 1" }));
    expect(screen.getByRole("dialog", { name: "图片预览" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "关闭预览" }));
    expect(screen.queryByRole("dialog", { name: "图片预览" })).toBeNull();

    const submitReady = screen.getByRole("button", {
      name: "提交作答并评价",
    }) as HTMLButtonElement;
    expect(submitReady.disabled).toBe(false);
    fireEvent.click(submitReady);
    expect(onSubmit).toHaveBeenCalledWith("ex-1", "", "", undefined, [
      { id: "att-ready-1", role: "solution" },
    ]);

    fireEvent.click(screen.getByRole("button", { name: "移除过程图 1" }));
    expect(onRemoveImage).toHaveBeenCalledWith("ex-1", "local-1");
  });

  test("2. 两道练习过程图按 exerciseId 隔离，互不混用；评价卡片直接加入错题本不重复调用", async () => {
    URL.createObjectURL = vi.fn(() => "blob:ex-img");
    URL.revokeObjectURL = vi.fn();
    const conv: ConversationSummary = {
      id: "conv-1",
      title: "极限复习",
      created_at: date,
      updated_at: date,
    };
    const ex1 = makeExercise({
      id: "ex-1",
      problem_id: "prob-1",
      root_problem_id: "root-1",
      statement_text: "第一道极限题",
      status: "submitted",
      evaluation: makeEvaluation({ is_correct: false, needs_supplement: false }),
      attempts: [
        makeAttempt({
          id: "att-ex1",
          evaluation: makeEvaluation({
            is_correct: false,
            needs_supplement: false,
            step_by_step_analysis: "符号求错。",
          }),
        }),
      ],
    });
    const ex2 = makeExercise({
      id: "ex-2",
      problem_id: "prob-2",
      root_problem_id: "root-2",
      statement_text: "第二道极限题",
      status: "pending_answer",
    });

    const detail: ConversationDetail = {
      conversation: conv,
      active_problem: null,
      record: null,
      records_by_root: {},
      turns: [
        {
          request_id: "t1",
          seq: 1,
          kind: "dynamic",
          query: "练一题",
          status: "completed",
          result: null,
          tutoring: null,
          exercise: ex1,
          error: null,
          started_at: date,
          finished_at: date,
        },
        {
          request_id: "t2",
          seq: 2,
          kind: "dynamic",
          query: "再来一题",
          status: "completed",
          result: null,
          tutoring: null,
          exercise: ex2,
          error: null,
          started_at: date,
          finished_at: date,
        },
      ],
    };

    listConversations.mockResolvedValue({ items: [conv], total: 1 });
    getConversation.mockResolvedValue(detail);
    uploadAttachment.mockResolvedValue({
      id: "up-ex2",
      mime: "image/png",
      width: 50,
      height: 50,
      size_bytes: 100,
      created_at: date,
    });
    let resolveSave!: (value: MistakeRecordDetail) => void;
    createMistakeRecord.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );

    const onLearningChanged = vi.fn();
    render(
      <ConversationWorkspace
        downloads={{}}
        onDownload={vi.fn()}
        onOpenRecord={vi.fn()}
        onLearningChanged={onLearningChanged}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /极限复习/ }));
    await screen.findByText("第二道极限题");

    // 仅给 ex-2 选图，ex-1 无任何草稿预览
    const fileInputs = screen.getAllByLabelText("选择过程图");
    const ex2File = new File(["img2"], "ex2.png", { type: "image/png" });
    fireEvent.change(fileInputs[0], { target: { files: [ex2File] } });
    expect(await screen.findByAltText("ex2.png")).toBeTruthy();
    expect(screen.getAllByAltText("ex2.png")).toHaveLength(1);

    // 点击 ex-1 评价卡片上的「加入错题本」，防止连续双击产生重复请求
    const saveBtn = screen.getByRole("button", { name: "加入错题本" });
    fireEvent.click(saveBtn);
    fireEvent.click(saveBtn);
    expect(createMistakeRecord).toHaveBeenCalledTimes(1);

    resolveSave({
      id: "rec-new-1",
      title: "极限计算",
      conversation_id: "conv-1",
      problem_id: "prob-1",
      root_problem_id: "root-1",
      problem_version: 1,
      statement_text: "第一道极限题",
      student_answer_text: "2",
      solution_text: null,
      source: { type: "none" },
      is_model_generated: true,
      attachments: [],
      explanation: null,
      followups: [],
      feedback: {
        text: null,
        self_reported_understood: false,
        self_reported_at: null,
        updated_at: null,
      },
      self_reported_understood: false,
      revisions: [],
      conversation_has_newer_version: false,
      newer_problem: null,
      created_at: date,
      updated_at: date,
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "已加入 · 查看" })).toBeTruthy(),
    );
    expect(onLearningChanged).toHaveBeenCalledTimes(1);
  });

  test("3. 回执顶部不再截断 LaTeX 题干，且历史超长评价正文与复合保存条不重复展示", () => {
    const ex = makeExercise({
      status: "submitted",
      statement_text: String.raw`计算 $\lim_{x\to 0}\frac{\sin(2x)-2x}{x^3}$ 完整题干`,
      attempts: [makeAttempt()],
    });
    render(
      <ExerciseAttemptReceipt
        exercise={ex}
        attempt={makeAttempt()}
        onJump={vi.fn()}
        onCorrect={vi.fn()}
        mistakeRecord={{ id: "rec-1", problem_version: 1 }}
        onOpenRecord={vi.fn()}
      />,
    );

    expect(screen.getByText("本题")).toBeTruthy();
    expect(screen.getByText("第 1 次作答 · 首次作答")).toBeTruthy();
    expect(screen.queryByText(/\\lim/)).toBeNull();
    expect(screen.getByRole("button", { name: "已加入 · 查看" })).toBeTruthy();

    // 历史超长评价正文折叠；复合保存结果不再重复渲染一条记录状态
    const longMsg =
      "请先补充推导步骤，便于精准定位错因。接下来是一大段旧版系统自动附加的重复详细分析和逐行推导内容，例如从无穷小等价替换到泰勒展开的逐项对照说明以及常见符号漏写的原因归纳与反思建议，专门凑足超过一百四十个字符以上用来测试超长历史评价正文的自动折叠逻辑是否确实按预期生效，从而确保页面主卡片的简洁清晰。";
    const turn: StoredTurn = {
      request_id: "turn-long",
      seq: 2,
      kind: "exercise",
      query: "",
      status: "completed",
      result: null,
      tutoring: {
        kind: "record_created",
        record: {
          id: "rec-1",
          title: "极限",
          statement_excerpt: "极限",
          source: { type: "none" },
          problem_version: 1,
          self_reported_understood: false,
          created_at: date,
          updated_at: date,
        },
      },
      composite_results: [
        {
          action: "record_mistake",
          status: "success",
          message: "已把这道题保存到错题本。",
        },
      ],
      exercise: ex,
      assistant_message: longMsg,
      error: null,
      started_at: date,
      finished_at: date,
    };

    render(
      <ConversationTurn
        turn={turn}
        downloads={{}}
        onDownload={vi.fn()}
        onRetry={vi.fn()}
        onFeedback={vi.fn()}
        onRecord={vi.fn()}
        onCorrect={vi.fn()}
        onSupplement={vi.fn()}
        onNew={vi.fn()}
        onIntentChoice={vi.fn()}
        onOpenRecord={vi.fn()}
        exerciseRole="attempt"
        exerciseAttempt={makeAttempt()}
        recordsByRoot={{ "root-1": { id: "rec-1", problem_version: 1 } }}
        busy={false}
      />,
    );

    expect(screen.getByText("查看完整文字回复")).toBeTruthy();
    expect(screen.queryByText("已把这道题保存到错题本。")).toBeNull();
    expect(screen.getAllByRole("button", { name: "已加入错题本 · 查看" })).toHaveLength(1);
  });

  test("4. 错题本详情以真实自主练习为主体：单一徽章、状态一致、关联练习默认折叠", async () => {
    const onOpenConversation = vi.fn();
    const detail: MistakeRecordDetail = {
      id: "rec-1",
      title: "极限计算",
      conversation_id: "conv-1",
      problem_id: "prob-1",
      root_problem_id: "root-1",
      problem_version: 1,
      statement_text: "计算极限 lim x→0 sin(2x)/x",
      student_answer_text: "1",
      solution_text: null,
      source: { type: "none" },
      is_model_generated: true,
      attachments: [],
      explanation: null,
      followups: [],
      feedback: {
        text: null,
        self_reported_understood: false,
        self_reported_at: null,
        updated_at: null,
      },
      self_reported_understood: false,
      revisions: [],
      conversation_has_newer_version: false,
      newer_problem: null,
      primary_exercise: makeExercise({
        id: "ex-primary",
        status: "pending_answer",
        student_answer_text: "1",
        evaluation: makeEvaluation(),
        attachments: [
          {
            id: "att-img-1",
            role: "student_solution",
            mime: "image/png",
            width: 100,
            height: 100,
            url: "/api/conversations/conv-1/attachments/att-img-1",
          },
        ],
        attempts: [
          makeAttempt({
            student_answer_text: "1",
            attachments: [
              {
                id: "att-img-1",
                role: "student_solution",
                mime: "image/png",
                width: 100,
                height: 100,
                url: "/api/conversations/conv-1/attachments/att-img-1",
              },
            ],
          }),
        ],
      }),
      exercises: [
        makeExercise({
          id: "ex-related",
          statement_text: "计算极限 lim x→0 tan(3x)/x",
          status: "submitted",
        }),
      ],
      created_at: date,
      updated_at: date,
    };

    listMistakeRecords.mockResolvedValue({
      items: [
        {
          id: "rec-1",
          title: "极限计算",
          statement_excerpt: "计算极限 lim x→0 sin(2x)/x",
          source: { type: "none" },
          is_model_generated: true,
          problem_version: 1,
          self_reported_understood: false,
          created_at: date,
          updated_at: date,
        },
      ],
      total: 1,
    });
    getMistakeRecord.mockResolvedValue(detail);

    render(
      <MistakeBook
        selectedId="rec-1"
        onSelectRecord={vi.fn()}
        onOpenConversation={onOpenConversation}
      />,
    );

    await screen.findAllByText("计算极限 lim x→0 sin(2x)/x");
    // 只显示模型生成 · 非真题，不再出现“未关联来源”
    expect(screen.getAllByText("模型生成 · 非真题").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("未关联来源")).toBeNull();

    // 作答状态依据真实作答与评价计算，不显示“未提交”
    expect(screen.getByText("已作答 · 待补充过程")).toBeTruthy();
    expect(screen.queryByText("未提交")).toBeNull();
    expect(screen.getAllByText("考点：等价无穷小").length).toBeGreaterThanOrEqual(1);

    // 关联类似练习默认折叠
    const relatedDetails = screen
      .getByText("关联类似练习（1）")
      .closest("details") as HTMLDetailsElement;
    expect(relatedDetails.open).toBe(false);

    // 点击查看解析：未解锁时安全提示、绝不泄题
    fireEvent.click(screen.getByRole("button", { name: "查看解析" }));
    expect(screen.getByText(/参考解答尚未解锁/)).toBeTruthy();

    // 点击打开原会话
    fireEvent.click(screen.getByRole("button", { name: "打开原会话" }));
    expect(onOpenConversation).toHaveBeenCalledWith("conv-1");
  });
});
