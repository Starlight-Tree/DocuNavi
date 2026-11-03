import { createContext, useContext, useEffect, useMemo, useReducer, useRef } from 'react';
import type { Dispatch, ReactNode } from 'react';
import { REPLY_DELAY_MS } from './data';
import { createInitialState, mockReducer } from './store';
import type { MockAction, MockState } from './store';
import type { View } from './types';

interface MockContextValue {
  state: MockState;
  dispatch: Dispatch<MockAction>;
}

const MockContext = createContext<MockContextValue | null>(null);

const TICK_MS = 500;

export function useMock(): MockContextValue {
  const value = useContext(MockContext);
  if (!value) throw new Error('MockProvider の外では使えません');
  return value;
}

/**
 * モックの状態と、その「時計」。取り込み中→利用可能の遷移、モックの回答の遅延、通知の消去を、ここのタイマーが進める。
 * 通信は一切しない（fetch を持たない）。
 */
export function MockProvider({ initialView = 'chat', children }: { initialView?: View; children: ReactNode }) {
  const [state, dispatch] = useReducer(mockReducer, undefined, () => createInitialState(Date.now(), initialView));

  // 取り込み中の資料か通知があるあいだだけ、時計を進める
  const needsTick = state.toasts.length > 0 || state.docs.some((d) => d.status === 'processing');
  useEffect(() => {
    if (!needsTick) return;
    const timer = window.setInterval(() => dispatch({ type: 'tick', now: Date.now() }), TICK_MS);
    return () => window.clearInterval(timer);
  }, [needsTick]);

  // 回答待ちの会話ごとに、一度だけモックの回答を予約する
  const replies = useRef(new Map<string, number>());
  useEffect(() => {
    for (const thread of state.threads) {
      if (!thread.pending || replies.current.has(thread.id)) continue;
      const id = thread.id;
      const timer = window.setTimeout(() => {
        replies.current.delete(id);
        dispatch({ type: 'reply', threadId: id, now: Date.now() });
      }, REPLY_DELAY_MS);
      replies.current.set(id, timer);
    }
  }, [state.threads]);
  useEffect(() => {
    const pending = replies.current;
    return () => {
      for (const timer of pending.values()) window.clearTimeout(timer);
      pending.clear();
    };
  }, []);

  const value = useMemo(() => ({ state, dispatch }), [state]);
  return <MockContext.Provider value={value}>{children}</MockContext.Provider>;
}
