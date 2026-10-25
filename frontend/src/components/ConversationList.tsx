import type { ConversationSummary } from "../lib/conversationsApi";
import { formatDateTime } from "../lib/display";

type ConversationListProps = {
  conversations: ConversationSummary[];
  currentId: string | null;
  loading: boolean;
  error: string | null;
  creating: boolean;
  onCreate: () => void;
  onSelect: (id: string) => void;
  onRetry: () => void;
};

/** 会话列表与新建入口；只负责展示和上报动作。 */
export default function ConversationList({
  conversations,
  currentId,
  loading,
  error,
  creating,
  onCreate,
  onSelect,
  onRetry,
}: ConversationListProps) {
  return (
    <aside className="rounded-2xl border border-slate-200/90 bg-white/80 p-4 shadow-card backdrop-blur">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-ink">学习会话</h2>
        <button
          type="button"
          onClick={onCreate}
          disabled={creating}
          className="rounded-lg bg-river px-2.5 py-1 text-[11px] font-medium text-white transition hover:bg-river/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-river focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:bg-slate-400"
        >
          {creating ? "创建中…" : "新建会话"}
        </button>
      </div>

      <p className="mt-1 text-[11px] leading-5 text-slate-500">
        查询历史保存在本地，刷新或重启后端后仍可打开。
      </p>

      {loading && (
        <p className="mt-3 text-xs text-slate-500">正在读取会话…</p>
      )}

      {error && !loading && (
        <div className="mt-3 rounded-xl border border-rose-200 bg-rose-50/80 p-3">
          <p className="break-words text-xs leading-5 text-rose-800">{error}</p>
          <button
            type="button"
            onClick={onRetry}
            className="mt-2 rounded-lg border border-rose-300 bg-white px-2.5 py-1 text-[11px] font-medium text-rose-800 transition hover:bg-rose-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400"
          >
            重新读取
          </button>
        </div>
      )}

      {!loading && !error && conversations.length === 0 && (
        <p className="mt-3 rounded-xl border border-dashed border-slate-300 bg-slate-50/60 p-3 text-xs leading-5 text-slate-500">
          还没有会话。新建一个会话后就可以提交查询。
        </p>
      )}

      {!loading && conversations.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {conversations.map((conversation) => {
            const active = conversation.id === currentId;
            return (
              <li key={conversation.id}>
                <button
                  type="button"
                  onClick={() => onSelect(conversation.id)}
                  aria-current={active ? "true" : undefined}
                  className={`w-full rounded-xl border px-3 py-2 text-left transition focus:outline-none focus-visible:ring-2 focus-visible:ring-river ${
                    active
                      ? "border-river/40 bg-river/10"
                      : "border-transparent hover:border-slate-200 hover:bg-slate-50"
                  }`}
                >
                  <span className="block truncate text-xs font-medium text-ink">
                    {conversation.title}
                  </span>
                  <span className="mt-0.5 block text-[10px] text-slate-400">
                    {formatDateTime(conversation.updated_at)}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </aside>
  );
}
