import type { ChatError } from '../hooks/useChat';

interface ErrorBannerProps {
  error: ChatError;
  disabled: boolean;
  onRetry: () => void;
}

export function ErrorBanner({ error, disabled, onRetry }: ErrorBannerProps) {
  return (
    <div className="error-banner" role="alert">
      <div className="error-text">
        <p>{error.message}</p>
        {error.retryAfterSeconds !== undefined && (
          <p className="error-hint">約 {error.retryAfterSeconds} 秒待ってから再試行してください。</p>
        )}
      </div>
      {/* 入力検証エラー（422）は同じ内容を送り直しても通らないので、再試行は出さない */}
      {error.status !== 422 && (
        <button type="button" className="btn btn-secondary" onClick={onRetry} disabled={disabled}>
          再試行
        </button>
      )}
    </div>
  );
}
