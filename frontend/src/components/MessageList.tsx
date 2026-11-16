import { Fragment, useEffect, useRef, useState } from 'react';
import type { ChatPhase } from '../api/chat';
import { MSG_HISTORY_FAILED } from '../hooks/useChat';
import type { ChatMessage, HistoryStatus } from '../hooks/useChat';
import { useCollectionCatalog } from '../hooks/useCollections';
import { classificationItems, useClassificationCatalog } from '../hooks/useClassifications';
import { classificationLabel } from '../lib/classifications';
import { collectionLabel } from '../lib/collections';
import { followUpQuestions } from '../lib/followUps';
import { stripCitations } from '../lib/citations';
import { formatSentAt } from '../lib/sentAt';
import { parseSources } from '../lib/sources';
import { AssistantAnswer } from './AssistantAnswer';

interface MessageListProps {
  messages: ChatMessage[];
  /** 回答の本文がまだ 1 文字も届いていない間。待機表示を出す。 */
  waiting: boolean;
  /** waiting の間の、サーバーが知らせた段階。無ければ（まだ届いていない・古いサーバー）一般的な文言を出す。 */
  phase?: ChatPhase | null;
  /** 送信から回答の完了までの間。生成中の吹き出しは、コピーを出さず、読み上げも完了まで待たせる。 */
  busy?: boolean;
  /** 保存済みの会話のやり取りを取得している間（loading）と、失敗したとき（error）。 */
  history?: HistoryStatus;
  /** 履歴の取得に失敗したときの「再試行」。会話 ID は変えない。渡さなければ、ボタンは出さない。 */
  onRetryHistory?: () => void;
  /** 履歴の取得に失敗したときの「新しい会話」。渡さなければ、ボタンは出さない。 */
  onNewConversation?: () => void;
  /** 空の画面の質問例を押したときに、その質問を送る。渡さなければ、質問例は出さない。 */
  onExample?: (text: string) => void;
  /**
   * 質問対象（対象・旧グループ）を指定して 0 件だった回答の下の「すべての資料で聞き直す」を押したとき。直前の質問を、範囲「すべて」の新しい会話で送る。
   * 渡さなければ、ボタンは出さない。
   */
  onAskAll?: () => void;
}

/** 最初の画面に出す質問例。登録した資料（ザーハード・入寮規程・洗濯乾燥機の取扱説明書・店舗案内）ごとに 1 つ。 */
export const EXAMPLE_QUESTIONS = [
  'ザーハードの大気の酸素濃度はどれくらい？',
  '入寮の申込みはいつまでにすればよいですか？',
  '洗濯機の排水ホースの先端は、床からどのくらいの高さにすればよいですか。',
  '犬を連れて店に入りたいのですが、店内の席に座れますか。',
] as const;

const WAITING_LABEL = '回答を作成しています…';
const PHASE_LABELS: Record<ChatPhase, string> = {
  analyzing: '質問を読み取っています…',
  searching: '資料を検索しています…',
  generating: '回答を生成しています…',
};

// 末尾からこの距離（px）以内にいるときだけ、新しい本文に合わせて自動でスクロールする
const STICK_TO_BOTTOM_PX = 80;
const COPIED_LABEL_MS = 2000;

/** コピーの線画（重なった 2 枚）。アイコンのライブラリは使わず、線だけの SVG を直に置く。 */
function CopyIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="7" y="7" width="9.5" height="9.5" rx="1.8" />
      <path d="M13 7V5.3A1.8 1.8 0 0 0 11.2 3.5H5.3A1.8 1.8 0 0 0 3.5 5.3v5.9A1.8 1.8 0 0 0 5.3 13H7" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4.5 10.5l3.5 3.5 7.5-8" />
    </svg>
  );
}

type CopyState = { id: string; ok: boolean } | null;

export function MessageList({
  messages,
  waiting,
  phase = null,
  busy = false,
  history = 'idle',
  onRetryHistory,
  onNewConversation,
  onExample,
  onAskAll,
}: MessageListProps) {
  const { state: catalog } = useCollectionCatalog();
  const groups = catalog.status === 'ready' ? catalog.data.collections : undefined;
  const targetItems = classificationItems(useClassificationCatalog().targets);
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
      await navigator.clipboard.writeText(message.role === 'assistant' ? stripCitations(message.text) : message.text);
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
      {messages.length === 0 && !waiting && history === 'loading' && (
        <p className="empty-state" role="status" data-testid="history-loading">
          これまでのやり取りを読み込んでいます…
        </p>
      )}

      {messages.length === 0 && !waiting && history === 'error' && (
        <div className="empty-state" role="alert" data-testid="history-error">
          <p>{MSG_HISTORY_FAILED}</p>
          <div className="history-error-actions">
            {onRetryHistory && (
              <button type="button" className="btn btn-secondary" onClick={onRetryHistory}>
                再試行
              </button>
            )}
            {onNewConversation && (
              <button type="button" className="btn btn-secondary" onClick={onNewConversation}>
                新しい会話
              </button>
            )}
          </div>
        </div>
      )}

      {messages.length === 0 && !waiting && history === 'idle' && (
        <div className="empty-state">
          <h2 className="empty-title">何を調べますか？</h2>
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
        // 質問の前に、その時刻を薄く出す（時刻の無い質問・古い会話・読めない値には出さない）
        const sentLabel = message.role === 'user' && message.sentAt ? formatSentAt(message.sentAt, new Date()) : null;
        return (
          <Fragment key={message.id}>
            {sentLabel && message.sentAt && (
              <time className="message-time" dateTime={message.sentAt}>
                {sentLabel}
              </time>
            )}
            <article className={`message message-${message.role}${message.id === lastId ? ' message-latest' : ''}`}>
              <span className="visually-hidden">
                {message.role === 'user' ? 'あなた' : 'アシスタント'}:{' '}
              </span>
              {/* 本文は必ずテキストとして描画する（HTML として解釈しない）。回答だけ Markdown を整形し、利用者の発言はそのまま見せる */}
              {message.role === 'assistant' ? (
                <AssistantAnswer
                  text={message.text}
                  context={message.context}
                  sources={message.sources}
                  generating={generating}
                  actions={
                    !generating && (
                      <>
                        <button
                          type="button"
                          className="icon-action"
                          onClick={() => void copyText(message)}
                          aria-label="この回答をコピー"
                          title="この回答をコピー"
                        >
                          {copied?.ok ? <CheckIcon /> : <CopyIcon />}
                        </button>
                        {/* 結果は、アイコンの形だけに頼らず、読み上げにも伝える（見える文字は出さない） */}
                        <span role="status" className={copied?.ok === false ? 'icon-action-failed' : 'visually-hidden'}>
                          {copied ? (copied.ok ? 'コピーしました' : 'コピーできませんでした') : ''}
                        </span>
                      </>
                    )
                  }
                >
                  {/* 質問対象（対象か旧グループ）の指定があって、検索で 0 件だった最新の回答だけ。検索していない（hit_count が無い・null）ときは出さない */}
                  {!busy &&
                    message.id === lastId &&
                    (typeof message.target === 'string' || typeof message.collection === 'string') &&
                    message.hitCount === 0 && (
                      <div className="scope-empty" role="status">
                        <p>
                          「
                          {typeof message.target === 'string'
                            ? classificationLabel(message.target, targetItems)
                            : `旧グループ: ${collectionLabel(message.collection as string, groups)}`}
                          」の中には、該当する資料がありませんでした。
                        </p>
                        {onAskAll && (
                          <button type="button" className="btn btn-secondary btn-small" onClick={onAskAll}>
                            すべての資料で聞き直す
                          </button>
                        )}
                      </div>
                    )}
                </AssistantAnswer>
              ) : (
                <p className="message-text message-question">{message.text}</p>
              )}
            </article>
          </Fragment>
        );
      })}

      {followUps.length > 0 && onExample && (
        <div className="follow-ups" role="group" aria-label="続けて聞く">
          {followUps.map((question) => (
            <button key={question} type="button" className="follow-up" onClick={() => onExample(question)}>
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
          <span>{phase ? PHASE_LABELS[phase] : WAITING_LABEL}</span>
        </div>
      )}
    </div>
  );
}
