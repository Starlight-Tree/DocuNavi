import { useChat as useAiChat } from '@ai-sdk/react';
import { DefaultChatTransport } from 'ai';
import type { UIMessage } from 'ai';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  CHAT_STREAM_PATH,
  ChatApiError,
  MAX_MESSAGE_LENGTH,
  MAX_THREAD_ID_LENGTH,
  THREAD_ID_PATTERN,
  streamEndpoint,
  isJapaneseMessage,
  parseChatFailure,
  parseChatPhase,
} from '../api/chat';
import type { ChatFailureInfo, ChatMetadata, ChatPhase } from '../api/chat';
import { useAuth } from '../auth/AuthContext';
import { createAuthedFetch } from '../auth/authFetch';
import { COLLECTION_NAME_PATTERN } from '../api/collections';
import { parseSourceRefs } from '../lib/sources';
import type { SourceRef } from '../lib/sources';
import { loadThreadId, saveThreadId } from '../lib/storage';
import { fetchThreadMessages } from '../api/threads';
import { useAuthedJsonFetch } from './useLoaded';

type ChatUIMessage = UIMessage<ChatMetadata>;

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  /** assistant のみ。そのターンで検索された設定資料（未検索なら空文字）。回答の完了の直前に届く。 */
  context?: string;
  /** assistant のみ。context の出典と同じ順のページ。届かなかった（古いサーバー）・形が違うときは無い。 */
  sources?: SourceRef[];
  /** assistant のみ。この会話の検索範囲（資料グループの名前。null は「すべて」）。届かなかった（古いサーバー）ときは無い。 */
  collection?: string | null;
  /** assistant のみ。そのターンの検索で拾ったチャンクの数（null は検索していない）。届かなかったときは無い。 */
  hitCount?: number | null;
}

export interface ChatError {
  status: number;
  message: string;
  retryAfterSeconds?: number;
}

export const MSG_HISTORY_FAILED =
  '過去のやり取りを読み込めませんでした。左の一覧からもう一度選ぶか、そのまま続きを送れます。';
const MSG_UNEXPECTED = '予期しないエラーが発生しました。もう一度お試しください。';

function toChatError(error: unknown, failure: ChatFailureInfo | null): ChatError {
  if (error instanceof ChatApiError) {
    const result: ChatError = { status: error.status, message: error.message };
    if (error.retryAfterSeconds !== undefined) {
      result.retryAfterSeconds = error.retryAfterSeconds;
    }
    return result;
  }
  // レスポンスを始めた後の失敗は、サーバーが error チャンクで固定の日本語メッセージを送ってくる。
  // HTTP で返していたときのステータスと Retry-After は、直前の data-failure で届く。
  // それ以外（パースの失敗など）の英語の文言は、利用者に見せない
  if (error instanceof Error && isJapaneseMessage(error.message)) {
    const result: ChatError = { status: failure?.status ?? 0, message: error.message };
    if (failure?.retryAfterSeconds !== undefined) result.retryAfterSeconds = failure.retryAfterSeconds;
    return result;
  }
  return { status: 0, message: MSG_UNEXPECTED };
}

function userText(message: ChatUIMessage): string {
  return message.parts.map((part) => (part.type === 'text' ? part.text : '')).join('');
}

/** assistant の本文は「最後のテキスト部分」。ツール呼び出しの前置きや、ガードが止める前の部分は無視する。 */
function assistantText(message: ChatUIMessage): string {
  for (let i = message.parts.length - 1; i >= 0; i--) {
    const part = message.parts[i];
    if (part?.type === 'text' && part.text.length > 0) return part.text;
  }
  return '';
}

/** サーバーの検索範囲（信用しない入力）。資料グループの名前か null（すべて）。それ以外・届いていないときは undefined。 */
function parseCollection(value: unknown): string | null | undefined {
  if (value === null) return null;
  return typeof value === 'string' && COLLECTION_NAME_PATTERN.test(value) ? value : undefined;
}

function parseHitCount(value: unknown): number | null | undefined {
  if (value === null) return null;
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : undefined;
}

function toChatMessages(messages: ChatUIMessage[]): ChatMessage[] {
  const result: ChatMessage[] = [];
  for (const message of messages) {
    if (message.role === 'user') {
      result.push({ id: message.id, role: 'user', text: userText(message) });
    } else if (message.role === 'assistant') {
      const text = assistantText(message);
      // 本文がまだ 1 文字も届いていない assistant は、空の吹き出しにせず、待機表示に任せる
      if (!text) continue;
      const item: ChatMessage = { id: message.id, role: 'assistant', text };
      if (message.metadata?.context !== undefined) item.context = message.metadata.context;
      const sources = parseSourceRefs(message.metadata?.sources);
      if (sources) item.sources = sources;
      const collection = parseCollection(message.metadata?.collection);
      if (collection !== undefined) item.collection = collection;
      const hitCount = parseHitCount(message.metadata?.hit_count);
      if (hitCount !== undefined) item.hitCount = hitCount;
      result.push(item);
    }
  }
  return result;
}

function lastUserText(messages: ChatUIMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message?.role === 'user') return userText(message);
  }
  return '';
}

function validThreadId(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    value.length <= MAX_THREAD_ID_LENGTH &&
    THREAD_ID_PATTERN.test(value)
  );
}

/** 保存済みの会話の発言を取り込む状態（resume の間）。 */
export type HistoryStatus = 'idle' | 'loading' | 'error';

export interface UseChat {
  messages: ChatMessage[];
  /** 送信してから、回答が最後まで届く（または失敗・中断する）まで。 */
  pending: boolean;
  /** pending のうち、回答の本文がまだ 1 文字も届いていない間。待機表示に使う。 */
  waiting: boolean;
  /** waiting の間の、サーバーが知らせた段階。まだ何も届いていなければ null。 */
  phase: ChatPhase | null;
  error: ChatError | null;
  threadId: string | null;
  /** 直近の応答で分かったバックエンドのモード。まだ応答が無ければ null。 */
  mode: string | null;
  /**
   * この会話の検索範囲（資料グループの名前。null は「すべて」）。会話ごとに固定で、最初の発言を送ると変えられない。
   * 再読み込み・URL・localStorage には残さない（保存済みの会話は、サーバーから復元する）。
   */
  scope: string | null;
  /** 保存済みの会話を開いたが、検索範囲をまだ（または取得に失敗して）知らないとき true。範囲を送らず、サーバーの会話の範囲に任せる。 */
  scopeUnknown: boolean;
  /** 発言か会話 ID がある（検索範囲を、いまの会話では変えられない）。 */
  hasConversation: boolean;
  /**
   * 検索範囲が、サーバーに反映されなかった可能性があるとき true（直近の回答の messageMetadata が、送った範囲と合わない・
   * 範囲を返さない旧 API）。回答が最後まで届いていないとき・範囲を指定していないときは false。
   */
  scopeMismatch: boolean;
  /** 新しい会話の検索範囲を選ぶ。発言や会話 ID があるときは何もしない（false）。会話が変わるときは `startNew` を使う。 */
  setScope: (collection: string | null) => boolean;
  /** 送信を受け付けたら true。送信中・空文字・長すぎる場合は何もせず false。 */
  send: (text: string) => boolean;
  /** 直近の質問を、検索範囲「すべて」の新しい会話で送り直す（範囲は会話ごとに固定なので、会話を分ける）。 */
  askAgainInAll: () => boolean;
  /** 直近のエラーになった発言を、吹き出しを増やさずに再送する。 */
  retry: () => void;
  /** 生成を止める。それまでに届いた本文は残る。 */
  stop: () => void;
  /** 会話（表示と thread_id）を破棄して最初から始める。検索範囲は「すべて」に戻る。 */
  reset: () => void;
  /** 会話を破棄して、指定した検索範囲（null は「すべて」）で最初から始める。 */
  startNew: (collection: string | null) => void;
  /** 保存済みの会話を開く。以前のやり取りを取得して画面に出し、続きを送れるようにする。 */
  resume: (threadId: string) => void;
  /** resume で以前のやり取りを取得している間（loading）と、取得に失敗したとき（error）。 */
  history: HistoryStatus;
}

export function useChat(): UseChat {
  const [threadId, setThreadId] = useState<string | null>(() => loadThreadId());

  // state は次の描画まで古いままなので、同じ tick 内の二重送信や最新の thread_id は ref で守る
  const threadIdRef = useRef<string | null>(threadId);
  const pendingRef = useRef(false);
  // 検索範囲。送信の本文を作る関数が読むので ref でも持つ。null は「すべて」（本文には載せない）
  const [scope, setScopeState] = useState<string | null>(null);
  const [scopeUnknown, setScopeUnknown] = useState<boolean>(() => loadThreadId() !== null);
  const scopeRef = useRef<string | null>(null);
  const applyScope = useCallback((next: string | null, unknown: boolean) => {
    scopeRef.current = next;
    setScopeState(next);
    setScopeUnknown(unknown);
  }, []);
  // 送信・再試行・リセットのたびに進める。リセット前の送信の完了が、新しい送信の pending を落とさないようにする
  const jobRef = useRef(0);
  const [history, setHistory] = useState<HistoryStatus>('idle');
  const historyAbortRef = useRef<AbortController | null>(null);
  const fetchJson = useAuthedJsonFetch();

  // ログイン中は、送る直前にアクセストークンを付ける（期限が近ければ更新し、401 なら 1 回だけ更新して送り直す）
  const { client: authClient, expire } = useAuth();
  const authClientRef = useRef(authClient);
  const expireRef = useRef(expire);
  authClientRef.current = authClient;
  expireRef.current = expire;

  const transport = useMemo(
    () =>
      new DefaultChatTransport<ChatUIMessage>({
        api: streamEndpoint(CHAT_STREAM_PATH),
        fetch: createAuthedFetch(
          () => authClientRef.current,
          () => expireRef.current(),
        ),
        headers: { Accept: 'text/event-stream' },
        // 履歴はサーバーが thread_id ごとに持つので、最後の発言だけを送る（最初の送信は thread_id も送らない）
        prepareSendMessagesRequest: ({ messages }) => {
          const body: { message: string; thread_id?: string; collection?: string } = {
            message: lastUserText(messages),
          };
          if (threadIdRef.current) body.thread_id = threadIdRef.current;
          // 範囲を知っているときだけ送る（「すべて」と、範囲を知らない会話は省略。省略は何も主張せず、会話の範囲がそのまま使われる）
          if (scopeRef.current) body.collection = scopeRef.current;
          return { body };
        },
      }),
    [],
  );
  // サーバーが transient なデータ部分で知らせる、いまの段階と、失敗の付帯情報（履歴には入らない）
  const [phase, setPhase] = useState<ChatPhase | null>(null);
  const [failure, setFailure] = useState<ChatFailureInfo | null>(null);
  const chat = useAiChat<ChatUIMessage>({
    transport,
    onData: (part) => {
      if (part.type === 'data-status') {
        const next = parseChatPhase(part.data);
        if (next) setPhase(next);
      } else if (part.type === 'data-failure') {
        setFailure(parseChatFailure(part.data));
      }
    },
  });

  // サーバーが start チャンクで返した thread_id を、以降の送信と再読み込み後の再開に使う
  useEffect(() => {
    for (let i = chat.messages.length - 1; i >= 0; i--) {
      const message = chat.messages[i];
      const id = message?.role === 'assistant' ? message.metadata?.thread_id : undefined;
      if (validThreadId(id)) {
        if (id !== threadIdRef.current) {
          threadIdRef.current = id;
          setThreadId(id);
          saveThreadId(id);
        }
        return;
      }
    }
  }, [chat.messages]);

  const messages = useMemo(() => toChatMessages(chat.messages), [chat.messages]);
  const pending = chat.status === 'submitted' || chat.status === 'streaming';
  const waiting = pending && messages[messages.length - 1]?.role !== 'assistant';

  const mode = useMemo(() => {
    for (let i = chat.messages.length - 1; i >= 0; i--) {
      const value = chat.messages[i]?.metadata?.mode;
      if (value) return value;
    }
    return null;
  }, [chat.messages]);

  const hasConversation = threadId !== null || chat.messages.length > 0;

  // 直近の回答の messageMetadata（会話の検索範囲）。完了の直前に届く（context が先に届いているときだけ「届き終えた」とみなす）
  const lastMeta = useMemo(() => {
    for (let i = chat.messages.length - 1; i >= 0; i--) {
      const message = chat.messages[i];
      if (message?.role !== 'assistant') continue;
      const metadata = message.metadata;
      if (metadata?.context === undefined) return null;
      return { collection: parseCollection(metadata.collection) };
    }
    return null;
  }, [chat.messages]);

  const scopeMismatch =
    scope !== null && !pending && lastMeta !== null && lastMeta.collection !== scope;

  // 保存済みの会話の範囲が分からないまま回答が届いたら、サーバーが返した会話の範囲を取り込む
  useEffect(() => {
    if (!scopeUnknown || pending || lastMeta === null || lastMeta.collection === undefined) return;
    applyScope(lastMeta.collection, false);
  }, [scopeUnknown, pending, lastMeta, applyScope]);

  // 画面を閉じるとき（アンマウント）は、実行中の送信を止める。サーバーは切断を見て LLM 呼び出しを取り消す
  const stopOnUnmount = chat.stop;
  useEffect(
    () => () => {
      void stopOnUnmount();
      historyAbortRef.current?.abort();
    },
    [stopOnUnmount],
  );

  const begin = useCallback((job: Promise<unknown>) => {
    setPhase(null);
    setFailure(null);
    const token = ++jobRef.current;
    pendingRef.current = true;
    void job
      .catch(() => undefined) // 失敗は chat.error に載る
      .finally(() => {
        if (jobRef.current === token) pendingRef.current = false;
      });
  }, []);

  const { sendMessage, regenerate, stop: stopChat, setMessages, clearError } = chat;

  const send = useCallback(
    (raw: string): boolean => {
      const text = raw.trim();
      if (!text || text.length > MAX_MESSAGE_LENGTH || pendingRef.current) return false;
      begin(sendMessage({ text }));
      return true;
    },
    [begin, sendMessage],
  );

  const setScope = useCallback(
    (collection: string | null): boolean => {
      if (pendingRef.current || threadIdRef.current !== null || chat.messages.length > 0) return false;
      applyScope(collection, false);
      return true;
    },
    [applyScope, chat.messages.length],
  );

  const retry = useCallback(() => {
    if (pendingRef.current || !chat.messages.some((message) => message.role === 'user')) return;
    // 末尾が assistant（途中まで届いた本文）なら、それを捨てて、同じ発言を送り直す
    begin(regenerate());
  }, [begin, chat.messages, regenerate]);

  const stop = useCallback(() => {
    void stopChat();
  }, [stopChat]);

  const startNew = useCallback(
    (collection: string | null) => {
      jobRef.current += 1;
      historyAbortRef.current?.abort();
      setHistory('idle');
      pendingRef.current = false;
      void stopChat();
      setMessages([]);
      clearError();
      threadIdRef.current = null;
      saveThreadId(null);
      setThreadId(null);
      applyScope(collection, false);
    },
    [applyScope, clearError, setMessages, stopChat],
  );

  const reset = useCallback(() => startNew(null), [startNew]);

  const askAgainInAll = useCallback((): boolean => {
    if (pendingRef.current) return false;
    const text = lastUserText(chat.messages).trim();
    if (!text || text.length > MAX_MESSAGE_LENGTH) return false;
    startNew(null);
    begin(sendMessage({ text }));
    return true;
  }, [begin, chat.messages, sendMessage, startNew]);

  const resume = useCallback(
    (id: string) => {
      if (!validThreadId(id)) return;
      const token = ++jobRef.current;
      pendingRef.current = false;
      void stopChat();
      setMessages([]);
      clearError();
      threadIdRef.current = id;
      saveThreadId(id);
      setThreadId(id);
      // 会話の検索範囲は、取得できるまで分からない（分からない間は範囲を送らない）
      applyScope(null, true);

      // 以前のやり取りを取得して並べる。取得の間に別の会話へ移った・新しい会話にした・送信を始めた
      // （どれも jobRef が進む）なら、遅れて届いた結果は捨てる
      historyAbortRef.current?.abort();
      const controller = new AbortController();
      historyAbortRef.current = controller;
      setHistory('loading');
      fetchThreadMessages(fetchJson, id, controller.signal).then(
        (thread) => {
          if (jobRef.current !== token) return;
          applyScope(thread.collection, false);
          setMessages(
            thread.messages.map((item, index) => ({
              id: `history-${id}-${index}`,
              role: item.role,
              parts: [{ type: 'text', text: item.text }],
            })),
          );
          setHistory('idle');
        },
        () => {
          if (jobRef.current === token) setHistory('error');
        },
      );
    },
    [applyScope, clearError, fetchJson, setMessages, stopChat],
  );

  return {
    messages,
    pending,
    waiting,
    phase: waiting ? phase : null,
    error: chat.error ? toChatError(chat.error, failure) : null,
    threadId,
    mode,
    scope,
    scopeUnknown,
    hasConversation,
    scopeMismatch,
    setScope,
    send,
    askAgainInAll,
    retry,
    stop,
    reset,
    startNew,
    resume,
    history,
  };
}
