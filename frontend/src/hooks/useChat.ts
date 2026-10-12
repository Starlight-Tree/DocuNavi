import { useCallback, useEffect, useRef, useState } from 'react';
import { ChatApiError, isAbortError, MAX_MESSAGE_LENGTH, sendChat } from '../api/chat';
import { loadThreadId, saveThreadId } from '../lib/storage';

export interface ChatMessage {
  id: number;
  role: 'user' | 'assistant';
  text: string;
  /** assistant のみ。そのターンで検索された設定資料（未検索なら空文字）。 */
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
  return { status: 0, message: MSG_UNEXPECTED };
}

export interface UseChat {
  messages: ChatMessage[];
  pending: boolean;
  error: ChatError | null;
  threadId: string | null;
  /** 直近の応答で分かったバックエンドのモード。まだ応答が無ければ null。 */
  mode: string | null;
  /** 送信を受け付けたら true。送信中・空文字・長すぎる場合は何もせず false。 */
  send: (text: string) => boolean;
  /** 直近のエラーになった発言を、吹き出しを増やさずに再送する。 */
  retry: () => void;
  /** 会話（表示と thread_id）を破棄して最初から始める。 */
  reset: () => void;
}

export function useChat(): UseChat {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ChatError | null>(null);
  const [threadId, setThreadId] = useState<string | null>(() => loadThreadId());
  const [mode, setMode] = useState<string | null>(null);

  // state は次の描画まで古いままなので、同じ tick 内の二重送信や最新の thread_id は ref で守る
  const pendingRef = useRef(false);
  const threadIdRef = useRef<string | null>(threadId);
  const lastUserTextRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // reset() のたびに進める。中断済みのリクエストの結果が、新しい会話に混ざらないようにする
  const generationRef = useRef(0);
  const nextIdRef = useRef(1);

  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  const run = useCallback(async (text: string) => {
    const generation = generationRef.current;
    const controller = new AbortController();
    abortRef.current = controller;
    pendingRef.current = true;
    setPending(true);
    setError(null);

    try {
      const response = await sendChat({
        message: text,
        threadId: threadIdRef.current,
        signal: controller.signal,
      });
      if (generation !== generationRef.current) return;

      // 以降はサーバーが返した thread_id を使う
      threadIdRef.current = response.thread_id;
      setThreadId(response.thread_id);
      saveThreadId(response.thread_id);
      setMode(response.mode);
      setMessages((prev) => [
        ...prev,
        {
          id: nextIdRef.current++,
          role: 'assistant',
          text: response.reply,
          context: response.context,
        },
      ]);
    } catch (caught) {
      if (generation !== generationRef.current || isAbortError(caught)) return;
      setError(toChatError(caught));
    } finally {
      if (generation === generationRef.current) {
        pendingRef.current = false;
        abortRef.current = null;
        setPending(false);
      }
    }
  }, []);

  const send = useCallback(
    (raw: string): boolean => {
      const text = raw.trim();
      if (!text || text.length > MAX_MESSAGE_LENGTH || pendingRef.current) return false;

      lastUserTextRef.current = text;
      setMessages((prev) => [...prev, { id: nextIdRef.current++, role: 'user', text }]);
      void run(text);
      return true;
    },
    [run],
  );

  const retry = useCallback(() => {
    const text = lastUserTextRef.current;
    if (!text || pendingRef.current) return;
    void run(text);
  }, [run]);

  const reset = useCallback(() => {
    generationRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    pendingRef.current = false;
    threadIdRef.current = null;
    lastUserTextRef.current = null;
    saveThreadId(null);
    setMessages([]);
    setPending(false);
    setError(null);
    setThreadId(null);
  }, []);

  return { messages, pending, error, threadId, mode, send, retry, reset };
}
