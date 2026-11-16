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
import { CLASSIFICATION_NAME_PATTERN } from '../api/classifications';
import { COLLECTION_NAME_PATTERN } from '../api/collections';
import { parseSentAt } from '../lib/sentAt';
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
  /** assistant のみ。この会話の旧グループの範囲（資料グループの名前。null は絞らない）。届かなかった（古いサーバー）ときは無い。 */
  collection?: string | null;
  /** assistant のみ。この会話の質問の対象（対象の識別名。null は絞らない）。届かなかった（古いサーバー）ときは無い。 */
  target?: string | null;
  /** assistant のみ。そのターンの検索で拾ったチャンクの数（null は検索していない）。届かなかったときは無い。 */
  hitCount?: number | null;
  /** user のみ。質問の時刻（ISO 8601）。履歴は API の値、送信直後は端末の時計。無ければ時刻の区切りを出さない。 */
  sentAt?: string;
}

export interface ChatError {
  status: number;
  message: string;
  retryAfterSeconds?: number;
}

export const MSG_HISTORY_FAILED =
  '過去のやり取りを読み込めませんでした。もう一度読み込むか、新しい会話を始めてください。';
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

/** サーバーの質問の対象（信用しない入力）。対象の識別名か null（絞らない）。それ以外・届いていないときは undefined。 */
function parseTarget(value: unknown): string | null | undefined {
  if (value === null) return null;
  return typeof value === 'string' && CLASSIFICATION_NAME_PATTERN.test(value) ? value : undefined;
}

function parseHitCount(value: unknown): number | null | undefined {
  if (value === null) return null;
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : undefined;
}

function toChatMessages(messages: ChatUIMessage[]): ChatMessage[] {
  const result: ChatMessage[] = [];
  for (const message of messages) {
    if (message.role === 'user') {
      const item: ChatMessage = { id: message.id, role: 'user', text: userText(message) };
      const sentAt = parseSentAt(message.metadata?.sent_at);
      if (sentAt) item.sentAt = sentAt;
      result.push(item);
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
      const target = parseTarget(message.metadata?.target);
      if (target !== undefined) item.target = target;
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

/**
 * 会話の質問の範囲。権限（collection）と対象（target）は別のもの: 検索は「見える資料 ∩ 範囲」で絞る。
 * - `all`: 絞らない
 * - `target`: 対象で絞る（新しい会話で選べるのはこれだけ）
 * - `collection`: 旧グループで固定された過去の会話（新しくは選べない。保存済みの会話から復元するときだけ）
 */
export type ChatScope =
  | { kind: 'all' }
  | { kind: 'target'; name: string }
  | { kind: 'collection'; name: string };

export const ALL_SCOPE: ChatScope = { kind: 'all' };

export function sameScope(a: ChatScope, b: ChatScope): boolean {
  return a.kind === b.kind && (a.kind === 'all' || (b.kind !== 'all' && a.name === b.name));
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
   * この会話の質問の範囲（すべて / 対象 / 旧グループ）。会話ごとに固定で、最初の発言を送ると変えられない。
   * 固定されるのは対象の識別名で、資料の集合ではない（資料の分類が変わると、同じ対象で探す資料も変わる）。
   * 再読み込み・URL・localStorage には残さない（保存済みの会話は、サーバーから復元する）。
   */
  scope: ChatScope;
  /** 保存済みの会話を開いたが、範囲をまだ（または取得に失敗して）知らないとき true。範囲を送らず、サーバーの会話の範囲に任せる。 */
  scopeUnknown: boolean;
  /** 保存された範囲（対象か旧グループ）が、いま利用できない（見えない・資料が無い）とサーバーが知らせたとき true。続きは送れるが、保存された範囲のまま探す。 */
  scopeUnavailable: boolean;
  /** 発言か会話 ID がある（範囲を、いまの会話では変えられない）。 */
  hasConversation: boolean;
  /**
   * 範囲が、サーバーに反映されなかった可能性があるとき true（直近の回答の messageMetadata が、送った範囲と合わない・
   * 範囲を返さない旧 API）。回答が最後まで届いていないとき・範囲を指定していないときは false。
   */
  scopeMismatch: boolean;
  /**
   * 画面で選んだ資料（`documents.id`）。null は「資料で絞らない」（会話の範囲のまま）。会話には固定されず、ターンごとに送る。
   * 選んだターンは、会話の範囲に代わって、この資料だけで探す。再読み込み・URL・localStorage には残さない。
   * 会話を変える（新しい会話・保存済みの会話を開く）か、範囲を選び直すと、null に戻る。
   */
  documentIds: number[] | null;
  /** 選んだ資料を置き換える。null は「資料で絞らない」。回答の生成中でも変えられる（次の送信から効く）。 */
  setDocumentIds: (ids: number[] | null) => void;
  /** 新しい会話の範囲を選ぶ。発言や会話 ID があるときは何もしない（false）。会話が変わるときは `startNew` を使う。 */
  setScope: (scope: ChatScope) => boolean;
  /** 送信を受け付けたら true。送信中・空文字・長すぎる場合は何もせず false。 */
  send: (text: string) => boolean;
  /** 直近の質問を、範囲「すべて」の新しい会話で送り直す（範囲は会話ごとに固定なので、会話を分ける）。 */
  askAgainInAll: () => boolean;
  /** 直近のエラーになった発言を、吹き出しを増やさずに再送する。 */
  retry: () => void;
  /** 生成を止める。それまでに届いた本文は残る。 */
  stop: () => void;
  /** 会話（表示と thread_id）を破棄して最初から始める。範囲は「すべて」に戻る。 */
  reset: () => void;
  /** 会話を破棄して、指定した範囲で最初から始める。 */
  startNew: (scope: ChatScope) => void;
  /** 保存済みの会話を開く。以前のやり取りを取得して画面に出し、続きを送れるようにする。 */
  resume: (threadId: string) => void;
  /** 取得に失敗した会話の以前のやり取りを、もう一度取得する（会話 ID はそのまま）。 */
  retryHistory: () => void;
  /** resume で以前のやり取りを取得している間（loading）と、取得に失敗したとき（error）。 */
  history: HistoryStatus;
}

export function useChat(): UseChat {
  const [threadId, setThreadId] = useState<string | null>(() => loadThreadId());

  // state は次の描画まで古いままなので、同じ tick 内の二重送信や最新の thread_id は ref で守る
  const threadIdRef = useRef<string | null>(threadId);
  const pendingRef = useRef(false);
  // 範囲。送信の本文を作る関数が読むので ref でも持つ。「すべて」は本文には載せない
  const [scope, setScopeState] = useState<ChatScope>(ALL_SCOPE);
  const [scopeUnknown, setScopeUnknown] = useState<boolean>(() => loadThreadId() !== null);
  const [scopeUnavailable, setScopeUnavailable] = useState(false);
  const scopeRef = useRef<ChatScope>(ALL_SCOPE);
  // 選んだ資料。送信の本文を作る関数が読むので ref でも持つ。会話の範囲とは別（範囲は会話ごとに固定、こちらはターンごと）
  const [documentIds, setDocumentIdsState] = useState<number[] | null>(null);
  const documentIdsRef = useRef<number[] | null>(null);
  const setDocumentIds = useCallback((ids: number[] | null) => {
    documentIdsRef.current = ids;
    setDocumentIdsState(ids);
  }, []);
  const applyScope = useCallback((next: ChatScope, unknown: boolean, unavailable = false) => {
    scopeRef.current = next;
    setScopeState(next);
    setScopeUnknown(unknown);
    setScopeUnavailable(unavailable);
  }, []);
  // 送信・再試行・リセットのたびに進める。リセット前の送信の完了が、新しい送信の pending を落とさないようにする
  const jobRef = useRef(0);
  // 保存された会話があるときは、起動の直後から取得する（取得が始まるまで、空の案内を一瞬出さない）
  const [history, setHistory] = useState<HistoryStatus>(() => (loadThreadId() !== null ? 'loading' : 'idle'));
  const historyAbortRef = useRef<AbortController | null>(null);
  const fetchJson = useAuthedJsonFetch();

  // ログイン中は、送る直前にアクセストークンを付ける（期限が近ければ更新し、401 なら 1 回だけ更新して送り直す）
  const { client: authClient, expire, denyAccess } = useAuth();
  const authClientRef = useRef(authClient);
  const expireRef = useRef(expire);
  const denyAccessRef = useRef(denyAccess);
  authClientRef.current = authClient;
  expireRef.current = expire;
  denyAccessRef.current = denyAccess;

  const transport = useMemo(
    () =>
      new DefaultChatTransport<ChatUIMessage>({
        api: streamEndpoint(CHAT_STREAM_PATH),
        fetch: createAuthedFetch(
          () => authClientRef.current,
          () => expireRef.current(),
          undefined,
          () => denyAccessRef.current(),
        ),
        headers: { Accept: 'text/event-stream' },
        // 履歴はサーバーが thread_id ごとに持つので、最後の発言だけを送る（最初の送信は thread_id も送らない）
        prepareSendMessagesRequest: ({ messages }) => {
          const body: {
            message: string;
            thread_id?: string;
            collection?: string;
            target?: string;
            document_ids?: number[];
          } = {
            message: lastUserText(messages),
          };
          if (threadIdRef.current) body.thread_id = threadIdRef.current;
          // 範囲を知っているときだけ送る（「すべて」と、範囲を知らない会話は省略。省略は何も主張せず、会話の範囲がそのまま使われる）。
          // 対象と旧グループは同時に送らない（サーバーが 422 にする）
          const current = scopeRef.current;
          // 資料を選んでいるターンは、その資料だけで探す（サーバーは、対象・旧グループと同時には受け付けない）
          const chosen = documentIdsRef.current;
          if (chosen !== null && chosen.length > 0) body.document_ids = chosen;
          else if (current.kind === 'target') body.target = current.name;
          else if (current.kind === 'collection') body.collection = current.name;
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

  // 直近の回答の messageMetadata（会話の範囲）。完了の直前に届く（context が先に届いているときだけ「届き終えた」とみなす）
  const lastMeta = useMemo(() => {
    for (let i = chat.messages.length - 1; i >= 0; i--) {
      const message = chat.messages[i];
      if (message?.role !== 'assistant') continue;
      const metadata = message.metadata;
      if (metadata?.context === undefined) return null;
      return { collection: parseCollection(metadata.collection), target: parseTarget(metadata.target) };
    }
    return null;
  }, [chat.messages]);

  const scopeMismatch =
    scope.kind !== 'all' &&
    !pending &&
    lastMeta !== null &&
    (scope.kind === 'target' ? lastMeta.target !== scope.name : lastMeta.collection !== scope.name);

  // 保存済みの会話の範囲が分からないまま回答が届いたら、サーバーが返した会話の範囲を取り込む。
  // 保存された範囲がいま利用できない会話は、取り込まない（保存された識別名を、画面に出さない）
  useEffect(() => {
    if (!scopeUnknown || scopeUnavailable || pending || lastMeta === null) return;
    if (lastMeta.target === undefined && lastMeta.collection === undefined) return;
    applyScope(
      typeof lastMeta.target === 'string'
        ? { kind: 'target', name: lastMeta.target }
        : typeof lastMeta.collection === 'string'
          ? { kind: 'collection', name: lastMeta.collection }
          : ALL_SCOPE,
      false,
    );
  }, [scopeUnknown, scopeUnavailable, pending, lastMeta, applyScope]);

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
      // 資料を 1 件も選んでいないときは送らない（「選ばない」を「すべて」と読み替えない）
      if (documentIdsRef.current?.length === 0) return false;
      begin(sendMessage({ text, metadata: { sent_at: new Date().toISOString() } }));
      return true;
    },
    [begin, sendMessage],
  );

  const setScope = useCallback(
    (next: ChatScope): boolean => {
      if (pendingRef.current || threadIdRef.current !== null || chat.messages.length > 0) return false;
      applyScope(next, false);
      // 範囲を選び直したら、資料の選択は外す（選んだ資料が、選び直した範囲に勝ってしまわないように）
      setDocumentIds(null);
      return true;
    },
    [applyScope, chat.messages.length, setDocumentIds],
  );

  const retry = useCallback(() => {
    if (pendingRef.current || !chat.messages.some((message) => message.role === 'user')) return;
    if (documentIdsRef.current?.length === 0) return;
    // 末尾が assistant（途中まで届いた本文）なら、それを捨てて、同じ発言を送り直す
    begin(regenerate());
  }, [begin, chat.messages, regenerate]);

  const stop = useCallback(() => {
    void stopChat();
  }, [stopChat]);

  const startNew = useCallback(
    (next: ChatScope) => {
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
      applyScope(next, false);
      setDocumentIds(null);
    },
    [applyScope, clearError, setDocumentIds, setMessages, stopChat],
  );

  const reset = useCallback(() => startNew(ALL_SCOPE), [startNew]);

  const askAgainInAll = useCallback((): boolean => {
    if (pendingRef.current) return false;
    const text = lastUserText(chat.messages).trim();
    if (!text || text.length > MAX_MESSAGE_LENGTH) return false;
    startNew(ALL_SCOPE);
    begin(sendMessage({ text, metadata: { sent_at: new Date().toISOString() } }));
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
      // 会話の範囲は、取得できるまで分からない（分からない間は範囲を送らない）
      applyScope(ALL_SCOPE, true);
      setDocumentIds(null);

      // 以前のやり取りを取得して並べる。取得の間に別の会話へ移った・新しい会話にした・送信を始めた
      // （どれも jobRef が進む）なら、遅れて届いた結果は捨てる
      historyAbortRef.current?.abort();
      const controller = new AbortController();
      historyAbortRef.current = controller;
      setHistory('loading');
      fetchThreadMessages(fetchJson, id, controller.signal).then(
        (thread) => {
          if (jobRef.current !== token) return;
          // 保存された範囲がいま利用できないときは、範囲を知らないまま（識別名を出さず、範囲も送らない）にして、その旨を知らせる
          applyScope(
            thread.scopeUnavailable
              ? ALL_SCOPE
              : thread.target !== null
                ? { kind: 'target', name: thread.target }
                : thread.collection !== null
                  ? { kind: 'collection', name: thread.collection }
                  : ALL_SCOPE,
            thread.scopeUnavailable,
            thread.scopeUnavailable,
          );
          setMessages(
            thread.messages.map((item, index) => ({
              id: `history-${id}-${index}`,
              role: item.role,
              parts: [{ type: 'text', text: item.text }],
              // 回答の出典（保存された検索結果から、サーバーが取り出したもの）。回答の直後と同じ「参照資料」を出す
              ...(item.context !== undefined ? { metadata: { context: item.context, sources: item.sources } } : {}),
              // 質問の時刻（サーバーの値）。無い（古い会話）ときは付けない
              ...(item.sentAt !== undefined ? { metadata: { sent_at: item.sentAt } } : {}),
            })),
          );
          setHistory('idle');
        },
        () => {
          if (jobRef.current === token) setHistory('error');
        },
      );
    },
    [applyScope, clearError, fetchJson, setDocumentIds, setMessages, stopChat],
  );

  // 再読み込みしたあとも、保存された会話の以前のやり取りを表示する。失敗しても、会話 ID は捨てない（再試行か、新しい会話は利用者が選ぶ）
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current) return;
    restoredRef.current = true;
    const id = threadIdRef.current;
    if (id !== null) resume(id);
  }, [resume]);

  const retryHistory = useCallback(() => {
    if (threadIdRef.current !== null) resume(threadIdRef.current);
  }, [resume]);

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
    scopeUnavailable,
    hasConversation,
    scopeMismatch,
    documentIds,
    setDocumentIds,
    setScope,
    send,
    askAgainInAll,
    retry,
    stop,
    reset,
    startNew,
    resume,
    retryHistory,
    history,
  };
}
