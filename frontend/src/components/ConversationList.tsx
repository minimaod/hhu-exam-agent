import { createPortal } from "react-dom";
import type { ConversationSummary } from "../lib/conversationsApi";
import { formatDateTime } from "../lib/display";

type ConversationListProps = {
  conversations: ConversationSummary[];
  currentId: string | null;
  loading: boolean;
  error: string | null;
  creating: boolean;
  createTarget?: HTMLElement | null;
  onCreate: () => void;
  onSelect: (id: string) => void;
  onRequestDelete?: (id: string) => void;
  onRetry: () => void;
};

/** 会话列表与新建入口；只负责展示和上报动作。 */
export default function ConversationList({
  conversations,
  currentId,
  loading,
  error,
  creating,
  createTarget,
  onCreate,
  onSelect,
  onRequestDelete,
  onRetry,
}: ConversationListProps) {
  const createButton = (
    <button type="button" onClick={onCreate} disabled={creating} className={createTarget ? "rail-create" : "primary-button"}>
      {createTarget && <span aria-hidden="true">＋ </span>}
      {creating ? "创建中…" : createTarget ? "新建学习会话" : "新建会话"}
    </button>
  );

  return (
    <section className="history-list">
      {createTarget ? createPortal(createButton, createTarget) : createButton}
      <h2 className="rail-section-title">最近学习会话</h2>

      {loading && (
        <p className="mt-3 text-xs text-slate-500">正在读取会话…</p>
      )}

      {error && !loading && (
        <div className="mt-3 rounded-xl border border-rose-200 bg-rose-50/80 p-3">
          <p className="break-words text-xs leading-5 text-rose-800">{error}</p>
          <button
            type="button"
            onClick={onRetry}
            className="mt-2 rounded-lg border border-rose-300 bg-white px-2.5 py-1 text-xs font-medium text-rose-800 transition hover:bg-rose-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400"
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
              <li key={conversation.id} className="group flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => onSelect(conversation.id)}
                  aria-current={active ? "true" : undefined}
                  className="history-item min-w-0 flex-1"
                >
                  <span className="history-title">
                    {conversation.title}
                  </span>
                  <span className="history-meta">
                    {formatDateTime(conversation.updated_at)}
                  </span>
                </button>
                {onRequestDelete && (
                  <button
                    type="button"
                    aria-label="删除此会话"
                    title={`删除会话：${conversation.title}`}
                    data-testid={`delete-conversation-${conversation.id}`}
                    onClick={() => onRequestDelete(conversation.id)}
                    className="rounded-lg px-2 py-1 text-xs text-slate-400 hover:bg-rose-50 hover:text-rose-700 focus:text-rose-700"
                  >
                    ×
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
