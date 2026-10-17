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
  endpoint,
  isJapaneseMessage,
} from '../api/chat';
import type { ChatMetadata } from '../api/chat';
import { useAuth } from '../auth/AuthContext';
import { createAuthedFetch } from '../auth/authFetch';
import { loadThreadId, saveThreadId } from '../lib/storage';

type ChatUIMessage = UIMessage<ChatMetadata>;

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  /** assistant のみ。そのターンで検索された設定資料（未検索なら空文字）。回答の完了の直前に届く。 */
  context?: string;
}

export interface ChatError {
  status: number;
  message: string;
  retryAfterSeconds?: number;
}

const MSG_UNEXPECTED = '予期しないエラーが発生しました。もう一度お試しください。';

function toChatError(error: unknown): ChatError {
  if (error instanceof ChatApiError) {
    const result: ChatError = { status: error.status, message: error.message };
    if (error.retryAfterSeconds !== undefined) {
      result.retryAfterSeconds = error.retryAfterSeconds;
    }
    return result;
  }
  // 本文を送り始めた後の失敗は、サーバーが error チャンクで固定の日本語メッセージを送ってくる。
  // それ以外（パースの失敗など）の英語の文言は、利用者に見せない
  if (error instanceof Error && isJapaneseMessage(error.message)) {
    return { status: 0, message: error.message };
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

export interface UseChat {
  messages: ChatMessage[];
  /** 送信してから、回答が最後まで届く（または失敗・中断する）まで。 */
  pending: boolean;
  /** pending のうち、回答の本文がまだ 1 文字も届いていない間。待機表示に使う。 */
  waiting: boolean;
  error: ChatError | null;
  threadId: string | null;
  /** 直近の応答で分かったバックエンドのモード。まだ応答が無ければ null。 */
  mode: string | null;
  /** 送信を受け付けたら true。送信中・空文字・長すぎる場合は何もせず false。 */
  send: (text: string) => boolean;
  /** 直近のエラーになった発言を、吹き出しを増やさずに再送する。 */
  retry: () => void;
  /** 生成を止める。それまでに届いた本文は残る。 */
  stop: () => void;
  /** 会話（表示と thread_id）を破棄して最初から始める。 */
  reset: () => void;
}

export function useChat(): UseChat {
  const [threadId, setThreadId] = useState<string | null>(() => loadThreadId());

  // state は次の描画まで古いままなので、同じ tick 内の二重送信や最新の thread_id は ref で守る
  const threadIdRef = useRef<string | null>(threadId);
  const pendingRef = useRef(false);
  // 送信・再試行・リセットのたびに進める。リセット前の送信の完了が、新しい送信の pending を落とさないようにする
  const jobRef = useRef(0);

  // ログイン中は、送る直前にアクセストークンを付ける（期限が近ければ更新し、401 なら 1 回だけ更新して送り直す）
  const { client: authClient, expire } = useAuth();
  const authClientRef = useRef(authClient);
  const expireRef = useRef(expire);
  authClientRef.current = authClient;
  expireRef.current = expire;

  const transport = useMemo(
    () =>
      new DefaultChatTransport<ChatUIMessage>({
        api: endpoint(CHAT_STREAM_PATH),
        fetch: createAuthedFetch(
          () => authClientRef.current,
          () => expireRef.current(),
        ),
        headers: { Accept: 'text/event-stream' },
        // 履歴はサーバーが thread_id ごとに持つので、最後の発言だけを送る（最初の送信は thread_id も送らない）
        prepareSendMessagesRequest: ({ messages }) => {
          const body: { message: string; thread_id?: string } = { message: lastUserText(messages) };
          if (threadIdRef.current) body.thread_id = threadIdRef.current;
          return { body };
        },
      }),
    [],
  );
  const chat = useAiChat<ChatUIMessage>({ transport });

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

  // 画面を閉じるとき（アンマウント）は、実行中の送信を止める。サーバーは切断を見て LLM 呼び出しを取り消す
  const stopOnUnmount = chat.stop;
  useEffect(() => () => void stopOnUnmount(), [stopOnUnmount]);

  const begin = useCallback((job: Promise<unknown>) => {
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

  const retry = useCallback(() => {
    if (pendingRef.current || !chat.messages.some((message) => message.role === 'user')) return;
    // 末尾が assistant（途中まで届いた本文）なら、それを捨てて、同じ発言を送り直す
    begin(regenerate());
  }, [begin, chat.messages, regenerate]);

  const stop = useCallback(() => {
    void stopChat();
  }, [stopChat]);

  const reset = useCallback(() => {
    jobRef.current += 1;
    pendingRef.current = false;
    void stopChat();
    setMessages([]);
    clearError();
    threadIdRef.current = null;
    saveThreadId(null);
    setThreadId(null);
  }, [clearError, setMessages, stopChat]);

  return {
    messages,
    pending,
    waiting,
    error: chat.error ? toChatError(chat.error) : null,
    threadId,
    mode,
    send,
    retry,
    stop,
    reset,
  };
}
