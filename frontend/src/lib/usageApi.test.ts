import assert from "node:assert/strict";
import test from "node:test";

import {
  millisecondsUntilReset,
  parseQuotaInfo,
  quotaBlocksImages,
  quotaBlocksRequests,
  quotaReason,
  type UsageSnapshot,
} from "./usageApi.ts";

const usage: UsageSnapshot = {
  date: "2026-11-10",
  reset_at: "2026-11-11T00:00:00+08:00",
  requests: { limit: 2, used: 2, remaining: 0 },
  site_exhausted: false,
  images: { limit: 20, used: 20, remaining: 0 },
  active_turn: false,
};

test("parseQuotaInfo 接受合法额度并拒绝缺字段", () => {
  assert.deepEqual(
    parseQuotaInfo({
      scope: "user",
      limit: 2,
      used: 2,
      reset_at: "2026-11-11T00:00:00+08:00",
    }),
    {
      scope: "user",
      limit: 2,
      used: 2,
      reset_at: "2026-11-11T00:00:00+08:00",
    },
  );
  assert.equal(parseQuotaInfo({ scope: "user", limit: 2 }), null);
  assert.equal(parseQuotaInfo({ scope: "unknown", limit: 2, used: 1, reset_at: "x" }), null);
});

test("额度耗尽判断区分模型与图片", () => {
  assert.equal(quotaBlocksRequests(usage), true);
  assert.equal(quotaBlocksImages(usage), true);
  assert.equal(quotaReason(usage), "你今天的 2 次模型请求已用完");
  const siteOnly: UsageSnapshot = {
    ...usage,
    requests: { limit: 30, used: 1, remaining: 29 },
    images: { limit: 20, used: 1, remaining: 19 },
    site_exhausted: true,
  };
  assert.equal(quotaBlocksRequests(siteOnly), true);
  assert.equal(quotaBlocksImages(siteOnly), false);
  assert.equal(quotaReason(siteOnly), "全站今天的模型请求额度已用完");
});

test("重置时间换算至少保留 1 秒", () => {
  const now = Date.parse("2026-11-10T23:59:59.500+08:00");
  const ms = millisecondsUntilReset("2026-11-11T00:00:00+08:00", now);
  assert.equal(ms, 1000);
});