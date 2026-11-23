import assert from "node:assert/strict";
import test from "node:test";

import {
  actionLabel,
  actionStatusLabel,
  attemptKindLabel,
  classifySuggestion,
  dedupeTexts,
  formatDateTime,
  formatExamDate,
  formatFileSize,
  explanationBody,
  isCoveredText,
  isDuplicateText,
  mathSafeExcerpt,
  memoryValidityLabel,
  memoryVerdictLabel,
  normalizeText,
  stripDuplicatePaperList,
} from "./display.ts";

test("内部动作名只显示中文，未知动作不原样泄露", () => {
  assert.equal(actionLabel("list_paper_questions"), "浏览真题");
  assert.equal(actionLabel("query_papers"), "查找试卷");
  assert.equal(actionLabel("introduce"), "介绍可用能力");
  assert.equal(actionLabel("generate_exercise"), "生成练习");
  assert.equal(actionLabel("some_future_action"), "其他操作");
  assert.equal(actionStatusLabel("failed"), "未完成");
  assert.equal(attemptKindLabel("correction"), "订正");
  assert.equal(attemptKindLabel(null), "本次作答");
});

test("学习记忆动作与证据等级只显示中文说明", () => {
  assert.equal(actionLabel("review_memory"), "查看学习记忆");
  assert.equal(actionLabel("correct_memory"), "纠正学习记忆");
  assert.equal(actionLabel("delete_memory"), "移除学习记忆");
  assert.equal(memoryVerdictLabel("wrong_independent"), "独立作答错误");
  assert.equal(memoryVerdictLabel("after_reveal"), "看答案后作答");
  assert.equal(memoryVerdictLabel("unclear"), "独立性未确认");
  assert.equal(memoryVerdictLabel("something_new"), "记录待核对");
  assert.equal(memoryValidityLabel("superseded"), "已被订正");
  assert.equal(memoryValidityLabel(undefined), "待核对");
});

test("重复文案按全半角、空白和标点归一化后判断", () => {
  assert.equal(normalizeText(" 已生成（练习题）。 "), normalizeText("已生成(练习题)"));
  assert.equal(isDuplicateText("已生成针对性练习题（待作答）", ["已生成针对性练习题(待作答)"]), true);
  assert.equal(isDuplicateText("已保存到错题本。", ["已生成练习题"]), false);
});

test("去重保留自然语言段落，完全相同的结果和动作消息不再重复", () => {
  const { shown, duplicate } = dedupeTexts([
    "已生成针对性练习题（待作答）",
    null,
    "已生成针对性练习题（待作答）",
  ]);
  assert.equal(shown[0], "已生成针对性练习题（待作答）");
  assert.equal(shown[2], "");
  assert.deepEqual(duplicate, [false, false, true]);
});

test("多段回复优先保留自然语言，重复的结果消息被移除", () => {
  const { shown } = dedupeTexts([
    "第一段说明。\n已保存到错题本。",
    null,
    "已保存到错题本。",
  ]);
  assert.equal(shown[0], "第一段说明。\n已保存到错题本。");
  assert.equal(shown[2], "");
});

test("同一段回复内部重复的行只保留一次", () => {
  const { shown } = dedupeTexts(["已保存到错题本。\n已保存到错题本。"]);
  assert.equal(shown[0], "已保存到错题本。");
});

test("动作结果整段被叙事包含时不再重复，但不吞掉叙事外的新结果", () => {
  assert.equal(
    isCoveredText("这道练习题已经完成评价；如需继续练习，请生成新的类似题。", [
      "抱歉，以下操作没有完成：这道练习题已经完成评价；如需继续练习，请生成新的类似题。。依赖未成功步骤的后续动作已跳过。",
    ]),
    true,
  );
  assert.equal(
    isCoveredText("找到 7 份【高等数学】相关试卷。", [
      "好的，不练题了，我帮你查找高等数学期末试卷。",
    ]),
    false,
  );
  assert.equal(isCoveredText("已保存到错题本。", [null, ""]), false);
});

test("缺失或无法解析的时间如实说明", () => {
  assert.equal(formatDateTime(null), "时间未知");
  assert.equal(formatDateTime("not-a-date"), "时间未知");
  assert.match(formatDateTime("2026-10-25T08:00:00+00:00"), /10\/25|\d{2}:\d{2}/);
  assert.deepEqual(formatExamDate(null), { text: "时间待补充", known: false });
  assert.deepEqual(formatExamDate("2024-06-20"), { text: "2024-06-20", known: true });
});

test("建议按钮分类覆盖查看解答、作答、保存和练习", () => {
  assert.equal(classifySuggestion("直接看答案"), "reveal");
  assert.equal(classifySuggestion("查看参考解答"), "reveal");
  assert.equal(classifySuggestion("提交我的答案"), "answer");
  assert.equal(classifySuggestion("保存到错题本"), "save");
  assert.equal(classifySuggestion("先存到错题本"), "save");
  assert.equal(classifySuggestion("再来一道类似题"), "practice");
  assert.equal(classifySuggestion("查看 Python 试卷"), "chat");
});

test("文件大小小于 1 MB 按 KB 显示，达到 1 MB 换算为 MB", () => {
  assert.equal(formatFileSize(2048), "2 KB");
  assert.equal(formatFileSize(3143 * 1024), "3.1 MB");
  assert.equal(formatFileSize(null), "大小未知");
});

test("公式安全截断不会把 LaTeX 公式从中间切开", () => {
  const sample = "设函数 $z = f(x,y)$ 具有一阶偏导数，已知 $f_x(2,1) = 3$，$f_y(2,1) = -1$，求偏导。";
  const clipped = mathSafeExcerpt(sample, 42);
  assert.ok(clipped.endsWith("…"));
  assert.equal((clipped.match(/\$/g) ?? []).length % 2, 0);
  assert.ok(!clipped.includes("$f_y(2"));

  const blockStart = "$$\\lim_{x\\to 0}\\frac{\\sqrt{1+\\tan x}-\\sqrt{1+\\sin x}}{x^3}$$ 后续文字";
  const kept = mathSafeExcerpt(blockStart, 20);
  assert.ok(kept.includes("$$\\lim_{x\\to 0}"));
  assert.ok(kept.includes("$$…"));
});

test("公式安全截断处理开头未闭合的公式，保留转义美元符号后的文字", () => {
  for (const opener of ["$", "$$", "\\[", "\\("]) {
    assert.equal(mathSafeExcerpt(opener + "f_y(2 后续文字很长", 10), "…");
    assert.equal(mathSafeExcerpt(opener + "f_y(2 后续文字很长", 2), "…");
  }
  assert.equal(mathSafeExcerpt("价格 \\$5，后面还有很长一段说明", 12), "价格 \\$5，后面还有…");
});


test("结构化讲解正文带完整推导、答案和原公式", () => {
  const body = explanationBody({
    knowledge_points: [],
    steps: [{ index: 1, title: "取对数", content: "保留 $x^2$。" }],
    final_answer: "B",
    error_analysis: "分母不可以先约掉。",
  });
  assert.match(body, /## 解题步骤/);
  assert.match(body, /保留 \$x\^2\$/);
  assert.match(body, /## 最终答案\n\nB/);
  assert.equal(body.split("分母不可以先约掉。").length, 2);
});

test("stripDuplicatePaperList 精准剥离省略学年的试卷清单，保留解题表格与推荐理由", () => {
  const items = [
    {
      id: "math-bi-2024",
      title: "2024-2025学年第一学期《高等数学BⅠ》期末试题及答案",
    },
    {
      id: "math-aii-2024",
      title: "2024-2025高数AII期末考试试题",
    },
  ];
  const body = [
    "已找到 2 份高等数学期末试卷：",
    "- 2024-2025 高等数学BⅠ 期末试题及答案",
    "2024–2025 第一学期《高等数学BⅠ》期末试题及答案",
    "- 2024-2025高数AII期末考试试题：题型全面且难度适中，推荐优先练习。",
    "",
    "| 方法 | 适用条件 |",
    "| --- | --- |",
    "| 洛必达法则 | 0/0 或 ∞/∞ 型未定式 |",
    "",
    "| 试卷 | 推荐理由 |",
    "| --- | --- |",
    "| 2024–2025 第一学期《高等数学BⅠ》期末试题及答案 | 附原卷答案，方便订正错题 |",
  ].join("\n");

  const cleaned = stripDuplicatePaperList(body, items);
  assert.equal(cleaned.includes("2024-2025 高等数学BⅠ 期末试题及答案"), false);
  assert.equal(cleaned.includes("推荐优先练习"), true);
  assert.equal(cleaned.includes("洛必达法则"), true);
  assert.equal(cleaned.includes("方便订正错题"), true);
  assert.equal(cleaned.split("\n").some((line) =>
    line === "2024–2025 第一学期《高等数学BⅠ》期末试题及答案",
  ), false);
});

test("旧查卷正文的学年和卷名分列时仍由查询卡承载", () => {
  const items = [
    { id: "recent", title: "2024-2025学年第二学期《高等数学AⅡ》期末考试试题" },
    { id: "older", title: "2018级高等数学（上）期末试卷" },
  ];
  const body = [
    "已找到 2 份高数期末试卷。",
    "",
    "| 学年/学期 | 试卷 |",
    "| --- | --- |",
    "| 2024-2025 第二学期 | 《高等数学AⅡ》期末考试试题 |",
    "| 2018-2019 | 2018级高等数学（上）期末试卷 |",
  ].join("\n");
  assert.equal(stripDuplicatePaperList(body, items), "已找到 2 份高数期末试卷。");
});
