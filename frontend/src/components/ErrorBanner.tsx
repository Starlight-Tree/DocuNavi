import type { ChatError } from '../hooks/useChat';

interface ErrorBannerProps {
  error: ChatError;
  disabled: boolean;
  onRetry: () => void;
  /** 質問対象の食い違い（409）・使えない対象（403）のとき、送り直しても通らないので、代わりに新しい会話にする。 */
  onNewConversation?: (() => void) | undefined;
}

export function ErrorBanner({ error, disabled, onRetry, onNewConversation }: ErrorBannerProps) {
  return (
    <div className="error-banner" role="alert">
      <div className="error-text">
        <p>{error.message}</p>
        {error.retryAfterSeconds !== undefined && (
          <p className="error-hint">約 {error.retryAfterSeconds} 秒待ってから再試行してください。</p>
        )}
      </div>
      {/* 質問対象の食い違い（409）・使えない対象（403）は、同じ内容を送り直しても通らない。新しい会話にして、聞き直す */}
      {(error.status === 409 || error.status === 403) && onNewConversation && (
        <button type="button" className="btn btn-secondary" onClick={onNewConversation}>
          新しい会話にする
        </button>
      )}
      {/* 入力検証エラー（422）・対象の食い違い（409）・使えない対象（403）は同じ内容を送り直しても通らないので、再試行は出さない */}
      {error.status !== 422 && error.status !== 409 && error.status !== 403 && (
        <button type="button" className="btn btn-secondary" onClick={onRetry} disabled={disabled}>
          再試行
        </button>
      )}
    </div>
  );
}
