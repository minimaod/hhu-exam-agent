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
  placement?: "all" | "summary" | "warnings";
};

export default function UsageNotice({ usage, error = null, placement = "all" }: UsageNoticeProps) {
  const showSummary = placement !== "warnings";
  const showWarnings = placement !== "summary";
  if (!usage) {
    return error && showWarnings ? <p role="status" className="quota-warning">{error}</p> : null;
  }

  const { requests, images } = usage;
  const blocked = quotaBlocksRequests(usage);
  const imageBlocked = quotaBlocksImages(usage);
  const reason = quotaReason(usage);

  return (
    <>
      {showSummary && (
        <section className="quota-widget" aria-label="今日模型额度">
          <p className="quota-heading">
            <span>{requests.limit === null ? "今日模型请求" : "今日剩余模型请求"}</span>
            <strong>{requests.limit === null ? "不限" : <>{requests.remaining} / {requests.limit}</>}</strong>
          </p>
          {requests.limit !== null && requests.remaining !== null && (
            <progress
              className="quota-meter"
              aria-label="剩余模型请求比例"
              max={Math.max(requests.limit, 1)}
              value={Math.max(0, requests.remaining)}
              data-blocked={blocked}
            />
          )}
          <p className="quota-meta">
            <span>{images.limit === null ? "图片上传不限" : <>图片剩余 {images.remaining}/{images.limit} 张</>}</span>
            <span>{requests.limit === null ? <>已调用 {requests.used} 次</> : "00:00 重置"}</span>
          </p>
        </section>
      )}
      {showWarnings && (
        <>
          {error && <p role="status" className="quota-warning">{error}</p>}
          {blocked && reason && (
            <p role="alert" className="quota-warning">
              {reason}，北京时间 {formatResetTime(usage.reset_at)} 重置。查询目录、下载试卷、
              浏览历史、错题本和学习记忆不受影响。
            </p>
          )}
          {!blocked && imageBlocked && (
            <p role="status" className="quota-warning">
              今天已上传 {usage.images.limit} 张图片，明天 00:00 重置；文字输入仍可用。
            </p>
          )}
        </>
      )}
    </>
  );
}
