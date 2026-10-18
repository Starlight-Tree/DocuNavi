import { useEffect, useRef, useState } from 'react';
import type { ChatMessage } from '../hooks/useChat';
import { followUpQuestions } from '../lib/followUps';
import { renderMarkdown } from '../lib/markdown';
import { parseSources } from '../lib/sources';
import { SourcesPanel } from './SourcesPanel';

interface MessageListProps {
  messages: ChatMessage[];
  /** 回答の本文がまだ 1 文字も届いていない間。待機表示を出す。 */
  waiting: boolean;
  /** 送信から回答の完了までの間。生成中の吹き出しは、コピーを出さず、読み上げも完了まで待たせる。 */
  busy?: boolean;
  /** 再読み込み後など、thread_id は引き継いでいるが、画面に履歴が無い状態。 */
  resumed?: boolean;
  /** 空の画面の質問例を押したときに、その質問を送る。渡さなければ、質問例は出さない。 */
  onExample?: (text: string) => void;
}

/** 最初の画面に出す質問例。登録した資料（ザーハード・入寮規程・洗濯乾燥機の取扱説明書・店舗案内）ごとに 1 つ。 */
export const EXAMPLE_QUESTIONS = [
  'ザーハードの大気の酸素濃度はどれくらい？',
  '入寮の申込みはいつまでにすればよいですか？',
  '洗濯機の排水ホースの先端は、床からどのくらいの高さにすればよいですか。',
  '犬を連れて店に入りたいのですが、店内の席に座れますか。',
] as const;

// 末尾からこの距離（px）以内にいるときだけ、新しい本文に合わせて自動でスクロールする
const STICK_TO_BOTTOM_PX = 80;
const COPIED_LABEL_MS = 2000;

type CopyState = { id: string; ok: boolean } | null;

export function MessageList({
  messages,
  waiting,
  busy = false,
  resumed = false,
  onExample,
}: MessageListProps) {
  const listRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const [copy, setCopy] = useState<CopyState>(null);

  // 新しい発言・本文の追加・待機表示が増えたら、最新が見える位置までスクロールする。
  // 利用者が遡って読んでいるとき（末尾から離れているとき）は、動かさない
  useEffect(() => {
    const list = listRef.current;
    if (list && stickRef.current) list.scrollTop = list.scrollHeight;
  }, [messages, waiting]);

  useEffect(() => {
    if (!copy) return;
    const timer = window.setTimeout(() => setCopy(null), COPIED_LABEL_MS);
    return () => window.clearTimeout(timer);
  }, [copy]);

  const handleScroll = () => {
    const list = listRef.current;
    if (!list) return;
    stickRef.current = list.scrollHeight - list.scrollTop - list.clientHeight <= STICK_TO_BOTTOM_PX;
  };

  const copyText = async (message: ChatMessage) => {
    try {
      await navigator.clipboard.writeText(message.text);
      setCopy({ id: message.id, ok: true });
    } catch {
      // クリップボードが使えない環境（非 HTTPS など）。失敗したことだけを知らせる
      setCopy({ id: message.id, ok: false });
    }
  };

  const lastId = messages[messages.length - 1]?.id;
  // 回答のあとに、その回答の根拠になった資料に答えがある質問を出す（生成中・送信中は出さない）
  const lastMessage = messages[messages.length - 1];
  const followUps =
    onExample && !busy && lastMessage?.role === 'assistant' && lastMessage.context
      ? followUpQuestions(
          [...new Set(parseSources(lastMessage.context).map((source) => source.name))],
          messages.filter((m) => m.role === 'user').map((m) => m.text),
        )
      : [];

  return (
    <div
      ref={listRef}
      className="message-list"
      role="log"
      aria-live="polite"
      aria-busy={busy}
      aria-label="会話"
      tabIndex={0}
      onScroll={handleScroll}
    >
      {messages.length === 0 && !waiting && resumed && (
        <p className="empty-state" data-testid="resumed-notice">
          前回の会話の続きです（これまでのやり取りは画面に表示されません）。
          <br />
          最初からやり直すには、右上の「新しい会話」を押してください。
        </p>
      )}

      {messages.length === 0 && !waiting && !resumed && (
        <div className="empty-state">
          <p>登録された資料（設定資料・規程・取扱説明書・店舗案内など）について質問してみましょう。</p>
          {onExample && (
            <div className="example-chips" role="group" aria-label="質問の例">
              {EXAMPLE_QUESTIONS.map((question) => (
                <button key={question} type="button" className="example-chip" onClick={() => onExample(question)}>
                  {question}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {messages.map((message) => {
        // 生成中の最後の吹き出しは、完了するまでコピーを出さない（途中の本文をコピーさせない）
        const generating = busy && message.role === 'assistant' && message.id === lastId;
        const copied = copy?.id === message.id ? copy : null;
        return (
          <article key={message.id} className={`message message-${message.role}`}>
            <span className="visually-hidden">
              {message.role === 'user' ? 'あなた' : 'アシスタント'}:{' '}
            </span>
            {/* 本文は必ずテキストとして描画する（HTML として解釈しない）。回答だけ Markdown を整形し、利用者の発言はそのまま見せる */}
            {message.role === 'assistant' ? (
              <div className="message-text markdown">{renderMarkdown(message.text)}</div>
            ) : (
              <p className="message-text">{message.text}</p>
            )}
            {message.role === 'assistant' && message.context && !generating && (
              <SourcesPanel context={message.context} />
            )}
            {message.role === 'assistant' && !generating && (
              <div className="message-actions">
                <button
                  type="button"
                  className="btn-link"
                  onClick={() => void copyText(message)}
                  aria-label="この回答をコピー"
                >
                  {copied ? (copied.ok ? 'コピーしました' : 'コピーできませんでした') : 'コピー'}
                </button>
              </div>
            )}
          </article>
        );
      })}

      {followUps.length > 0 && onExample && (
        <div className="follow-ups" role="group" aria-label="続けて聞く">
          <span className="follow-ups-label">続けて聞く</span>
          {followUps.map((question) => (
            <button key={question} type="button" className="example-chip" onClick={() => onExample(question)}>
              {question}
            </button>
          ))}
        </div>
      )}

      {waiting && (
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
