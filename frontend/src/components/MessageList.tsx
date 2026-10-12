import { useEffect, useRef } from 'react';
import type { ChatMessage } from '../hooks/useChat';
import { SourcesPanel } from './SourcesPanel';

interface MessageListProps {
  messages: ChatMessage[];
  pending: boolean;
  /** 再読み込み後など、thread_id は引き継いでいるが、画面に履歴が無い状態。 */
  resumed?: boolean;
}

export function MessageList({ messages, pending, resumed = false }: MessageListProps) {
  const listRef = useRef<HTMLDivElement>(null);

  // 新しい発言・待機表示が増えたら、最新が見える位置までスクロールする
  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [messages, pending]);

  return (
    <div
      ref={listRef}
      className="message-list"
      role="log"
      aria-live="polite"
      aria-label="会話"
      tabIndex={0}
    >
      {messages.length === 0 && !pending && resumed && (
        <p className="empty-state" data-testid="resumed-notice">
          前回の会話の続きです（これまでのやり取りは画面に表示されません）。
          <br />
          最初からやり直すには、右上の「新しい会話」を押してください。
        </p>
      )}

      {messages.length === 0 && !pending && !resumed && (
        <p className="empty-state">
          架空の惑星「ザーハード」の設定について質問してみましょう。
          <br />
          例: ザーハードの大気はどんな成分ですか？
        </p>
      )}

      {messages.map((message) => (
        <article key={message.id} className={`message message-${message.role}`}>
          <span className="visually-hidden">
            {message.role === 'user' ? 'あなた' : 'アシスタント'}:{' '}
          </span>
          {/* 本文は必ずテキストとして描画する（HTML として解釈しない） */}
          <p className="message-text">{message.text}</p>
          {message.role === 'assistant' && message.context && (
            <SourcesPanel context={message.context} />
          )}
        </article>
      ))}

      {pending && (
        <div className="message message-assistant message-pending">
          <span className="typing-dots" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span>回答を作成しています…</span>
        </div>
      )}
    </div>
  );
}
