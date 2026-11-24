import { materialPreviewUrl, type MaterialPageView } from "../lib/materialsApi.ts";

type MaterialPageModalProps = {
  materialId: string;
  pageNo: number;
  highlightQuote: string | null;
  loading: boolean;
  error: string | null;
  pageData: MaterialPageView | null;
  onClose: () => void;
};

export default function MaterialPageModal({
  materialId,
  pageNo,
  highlightQuote,
  loading,
  error,
  pageData,
  onClose,
}: MaterialPageModalProps) {
  return (
    <div
      className="material-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="material-page-modal-title"
    >
      <div className="material-modal-card">
        <header className="material-modal-header">
          <div>
            <p className="eyebrow">原文物理页对照</p>
            <h2 id="material-page-modal-title">第 {pageNo} 页</h2>
          </div>
          <div className="material-modal-actions">
            <a
              className="quiet-button"
              href={materialPreviewUrl(materialId, pageNo)}
              target="_blank"
              rel="noopener noreferrer"
            >
              预览原件 PDF（第 {pageNo} 页）
            </a>
            <button type="button" className="quiet-button" onClick={onClose}>
              关闭
            </button>
          </div>
        </header>

        {highlightQuote && (
          <div className="material-quote-banner">
            <span className="material-badge material-badge-source">已校验引文</span>
            <span>{highlightQuote}</span>
          </div>
        )}

        {loading && <p className="page-message">正在读取第 {pageNo} 页文本…</p>}
        {error && <div className="inline-alert">{error}</div>}

        {pageData && !loading && !error && (
          <div className="material-page-body">
            {pageData.warnings.length > 0 && (
              <div className="material-warning-box">
                <strong>可读性提示：</strong>
                {pageData.warnings.join("；")}
              </div>
            )}
            <pre className="material-page-text">
              {pageData.extracted_text || "（该页未提取到可复制文本，建议点击上方预览原件 PDF 查看。）"}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
