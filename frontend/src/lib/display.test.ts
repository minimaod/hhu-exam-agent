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
  isCoveredText,
  isDuplicateText,
  normalizeText,
} from "./display.ts";

test("内部动作名只显示中文，未知动作不原样泄露", () => {
  assert.equal(actionLabel("query_papers"), "查找试卷");
  assert.equal(actionLabel("introduce"), "介绍可用能力");
  assert.equal(actionLabel("generate_exercise"), "生成练习");
  assert.equal(actionLabel("some_future_action"), "其他操作");
  assert.equal(actionStatusLabel("failed"), "未完成");
  assert.equal(attemptKindLabel("correction"), "订正");
  assert.equal(attemptKindLabel(null), "本次作答");
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
  assert.deepEqual(formatExamDate(null), { text: "日期未登记", known: false });
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