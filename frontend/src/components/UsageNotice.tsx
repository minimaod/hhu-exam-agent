import {
  formatResetTime,
  quotaBlocksImages,
  quotaBlocksRequests,
  quotaReason,
  type UsageSnapshot,
} from "../lib/usageApi";

type UsageNoticeProps = {
  usage: UsageSnapshot | null;
  error?: string | null;
};

export default function UsageNotice({ usage, error = null }: UsageNoticeProps) {
  if (error) {
    return (
      <p role="status" className="mb-1 text-[11px] text-amber-800">
        {error}
      </p>
    );
  }
  if (!usage) {
    return null;
  }
  const blocked = quotaBlocksRequests(usage);
  const imageBlocked = quotaBlocksImages(usage);
  const reason = quotaReason(usage);
  return (
    <div className="mb-1 space-y-1 text-[11px] leading-5">
      <p className="text-slate-500">
        今日剩余模型请求 <strong className="text-ink">{usage.requests.remaining}</strong>
        {" / "}
        {usage.requests.limit} 次
        <span className="mx-1 text-slate-300">·</span>
        图片剩余 {usage.images.remaining} / {usage.images.limit} 张
        <span className="mx-1 text-slate-300">·</span>
        一轮复杂对话可能消耗 3–6 次
      </p>
      {blocked && reason ? (
        <p role="alert" className="rounded-lg border border-amber-300 bg-amber-50 px-2 py-1 text-amber-900">
          {reason}，北京时间 {formatResetTime(usage.reset_at)} 重置。查询目录、下载试卷、
          浏览历史、错题本和学习记忆不受影响。
        </p>
      ) : null}
      {!blocked && imageBlocked ? (
        <p role="status" className="text-amber-800">
          今天已上传 {usage.images.limit} 张图片，明天 00:00 重置；文字输入仍可用。
        </p>
      ) : null}
    </div>
  );
}