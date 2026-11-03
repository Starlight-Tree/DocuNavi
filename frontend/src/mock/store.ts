import {
  FAILURE_REASON_DEMO,
  INGEST_MS,
  INGEST_STAGGER_MS,
  RETRY_MS,
  TOAST_MS,
  kindOf,
  mockReply,
  seedDocs,
  seedThreads,
  willFailByName,
} from './data';
import type { DialogState, MockDoc, MockMessage, MockThread, PanelTarget, Scope, Toast, View } from './types';

/** UI モックの状態。画面の状態も、資料・会話のデータも、ここだけが持つ（実 API・保存先には触れない）。 */
export interface MockState {
  docs: MockDoc[];
  threads: MockThread[];
  activeId: string;
  view: View;
  panel: PanelTarget | null;
  dialog: DialogState | null;
  toasts: Toast[];
  /** ID の採番用。 */
  seq: number;
}

export interface UploadFile {
  name: string;
  size: number;
}

export type MockAction =
  | { type: 'setView'; view: View }
  | { type: 'newConversation'; now: number }
  | { type: 'askWithDoc'; docId: string; now: number }
  | { type: 'openThread'; id: string }
  | { type: 'send'; text: string; now: number }
  | { type: 'reply'; threadId: string; now: number }
  | { type: 'setScope'; threadId: string; scope: Scope; now: number }
  | { type: 'addFiles'; files: UploadFile[]; origin: 'chat' | 'documents'; now: number }
  | { type: 'tick'; now: number }
  | { type: 'retry'; docId: string; now: number }
  | { type: 'deleteDoc'; docId: string; now: number }
  | { type: 'openPanel'; target: PanelTarget }
  | { type: 'closePanel' }
  | { type: 'openDialog'; dialog: DialogState }
  | { type: 'closeDialog' }
  | { type: 'dismissToast'; id: string }
  | { type: 'reset'; now: number; view: View };

const ALL: Scope = { mode: 'all' };
const NEW_TITLE = '新しい会話';
const TITLE_MAX = 24;

export function createInitialState(now: number, view: View = 'chat'): MockState {
  const docs = seedDocs(now);
  const draft = emptyThread('n0', now, ALL);
  return { docs, threads: [...seedThreads(docs), draft], activeId: draft.id, view, panel: null, dialog: null, toasts: [], seq: 1 };
}

function emptyThread(id: string, now: number, scope: Scope): MockThread {
  return { id, title: NEW_TITLE, scope, messages: [], updatedAt: new Date(now).toISOString(), pending: false };
}

// ---- 参照用の関数（画面とテストが使う） ----

export function activeThread(state: MockState): MockThread {
  return state.threads.find((t) => t.id === state.activeId) ?? state.threads[0] ?? emptyThread('n0', 0, ALL);
}

export function docById(state: MockState, id: string): MockDoc | undefined {
  return state.docs.find((d) => d.id === id);
}

export interface ScopeDocs {
  /** 質問に使える資料（取り込み済み）。 */
  ready: MockDoc[];
  /** 選択に入っているが、取り込み中でまだ使えない資料。 */
  waiting: MockDoc[];
}

/** 質問対象を、質問に使える資料と、取り込みを待つ資料に分ける。「すべて」は取り込み済みの資料だけ。 */
export function scopeDocs(state: MockState, scope: Scope): ScopeDocs {
  if (scope.mode === 'all') return { ready: state.docs.filter((d) => d.status === 'ready'), waiting: [] };
  const picked = scope.ids.flatMap((id) => docById(state, id) ?? []);
  return { ready: picked.filter((d) => d.status === 'ready'), waiting: picked.filter((d) => d.status === 'processing') };
}

export function scopeLabel(state: MockState, scope: Scope): string {
  if (scope.mode === 'all') return 'すべての資料';
  const count = scope.ids.filter((id) => docById(state, id)).length;
  return `選択した資料 ${count}件`;
}

/** 送れるか。生成中でなく、質問に使える資料が 1 件以上あること。 */
export function sendBlock(state: MockState, thread: MockThread): 'pending' | 'no-docs' | 'waiting' | null {
  if (thread.pending) return 'pending';
  const { ready, waiting } = scopeDocs(state, thread.scope);
  if (ready.length > 0) return null;
  return waiting.length > 0 ? 'waiting' : 'no-docs';
}

/** この会話から追加して、まだ取り込み中の資料。 */
export function pendingUploads(state: MockState, thread: MockThread): MockDoc[] {
  return state.docs.filter((d) => d.origin === thread.id && d.status === 'processing');
}

/** 履歴に出す会話（発言があるもの）。新しい順。 */
export function historyThreads(state: MockState): MockThread[] {
  return state.threads.filter((t) => t.messages.length > 0).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
}

// ---- reducer ----

function updateThread(state: MockState, id: string, change: (t: MockThread) => MockThread): MockState {
  return { ...state, threads: state.threads.map((t) => (t.id === id ? change(t) : t)) };
}

function toast(state: MockState, text: string, tone: Toast['tone'], now: number): MockState {
  const entry: Toast = { id: `toast${state.seq}`, text, tone, until: now + TOAST_MS };
  return { ...state, toasts: [...state.toasts, entry], seq: state.seq + 1 };
}

/** 空の会話があればそれを使い回し（質問対象だけ置き換える）、なければ新しく作る。 */
function startConversation(state: MockState, scope: Scope, now: number): MockState {
  const current = activeThread(state);
  if (current.messages.length === 0 && !current.pending) {
    return { ...updateThread(state, current.id, (t) => ({ ...t, scope })), view: 'chat' };
  }
  const draft = emptyThread(`n${state.seq}`, now, scope);
  return { ...state, threads: [...state.threads, draft], activeId: draft.id, view: 'chat', seq: state.seq + 1 };
}

/** 別の会話へ移ったら、前の会話の出典を開いたパネルは閉じる（資料の詳細は、会話に関係ないので残す）。 */
function withoutCitationPanel(state: MockState): MockState {
  return state.panel?.focus ? { ...state, panel: null } : state;
}

function systemMessage(state: MockState, text: string): { message: MockMessage; seq: number } {
  return { message: { id: `m${state.seq}`, role: 'system', text }, seq: state.seq + 1 };
}

export function mockReducer(state: MockState, action: MockAction): MockState {
  switch (action.type) {
    case 'setView':
      return state.view === action.view ? state : { ...state, view: action.view };

    case 'newConversation':
      return withoutCitationPanel(startConversation(state, ALL, action.now));

    case 'askWithDoc': {
      const doc = docById(state, action.docId);
      if (!doc || doc.status !== 'ready') return state;
      return { ...startConversation(state, { mode: 'selected', ids: [doc.id] }, action.now), panel: null };
    }

    case 'openThread':
      return state.threads.some((t) => t.id === action.id)
        ? withoutCitationPanel({ ...state, activeId: action.id, view: 'chat' })
        : state;

    case 'send': {
      const thread = activeThread(state);
      const text = action.text.trim();
      if (!text || sendBlock(state, thread) !== null) return state;
      const user: MockMessage = { id: `m${state.seq}`, role: 'user', text };
      const next = updateThread(state, thread.id, (t) => ({
        ...t,
        title: t.messages.length === 0 ? text.slice(0, TITLE_MAX) : t.title,
        messages: [...t.messages, user],
        pending: true,
        updatedAt: new Date(action.now).toISOString(),
      }));
      return { ...next, seq: state.seq + 1 };
    }

    case 'reply': {
      const thread = state.threads.find((t) => t.id === action.threadId);
      if (!thread || !thread.pending) return state;
      const question = [...thread.messages].reverse().find((m) => m.role === 'user')?.text ?? '';
      const message = mockReply(question, scopeDocs(state, thread.scope).ready, `m${state.seq}`);
      const next = updateThread(state, thread.id, (t) => ({
        ...t,
        messages: [...t.messages, message],
        pending: false,
        updatedAt: new Date(action.now).toISOString(),
      }));
      return { ...next, seq: state.seq + 1 };
    }

    case 'setScope': {
      const thread = state.threads.find((t) => t.id === action.threadId);
      if (!thread) return state;
      const changed = JSON.stringify(thread.scope) !== JSON.stringify(action.scope);
      if (!changed) return { ...state, dialog: null };
      const withScope: MockState = { ...state, dialog: null };
      if (thread.messages.length === 0) return updateThread(withScope, thread.id, (t) => ({ ...t, scope: action.scope }));
      const { message, seq } = systemMessage(state, `質問対象を「${scopeLabel(state, action.scope)}」に変更しました。`);
      return {
        ...updateThread(withScope, thread.id, (t) => ({ ...t, scope: action.scope, messages: [...t.messages, message] })),
        seq,
      };
    }

    case 'addFiles': {
      if (action.files.length === 0) return state;
      const origin = action.origin === 'chat' ? state.activeId : null;
      const added: MockDoc[] = action.files.map((file, i) => {
        const failing = willFailByName(file.name);
        return {
          id: `u${state.seq + i}`,
          name: file.name,
          kind: kindOf(file.name),
          status: 'processing',
          addedAt: new Date(action.now).toISOString(),
          size: file.size,
          processingUntil: action.now + INGEST_MS + i * INGEST_STAGGER_MS,
          willFail: failing,
          failureReason: null,
          origin,
        };
      });
      let next: MockState = { ...state, docs: [...state.docs, ...added], dialog: null, seq: state.seq + added.length };
      if (action.origin === 'chat') {
        // 現在の会話の質問対象に反映する。「すべて」は、取り込みが済んだ資料が自動で入る
        next = updateThread(next, state.activeId, (t) =>
          t.scope.mode === 'selected' ? { ...t, scope: { mode: 'selected', ids: [...t.scope.ids, ...added.map((d) => d.id)] } } : t,
        );
      }
      return toast(next, `${added.length}件の資料を追加しました。取り込みを始めます（モック）。`, 'info', action.now);
    }

    case 'tick': {
      let changed = false;
      let next = state;
      const finished = state.docs.filter((d) => d.status === 'processing' && d.processingUntil !== null && d.processingUntil <= action.now);
      if (finished.length > 0) {
        changed = true;
        const done = new Set(finished.map((d) => d.id));
        next = {
          ...next,
          docs: next.docs.map((d) => {
            if (!done.has(d.id)) return d;
            return d.willFail
              ? { ...d, status: 'failed', processingUntil: null, failureReason: FAILURE_REASON_DEMO }
              : { ...d, status: 'ready', processingUntil: null };
          }),
        };
        for (const d of finished) {
          next = toast(
            next,
            d.willFail ? `「${d.name}」の取り込みに失敗しました。` : `「${d.name}」が利用可能になりました。質問対象に選べます。`,
            d.willFail ? 'error' : 'info',
            action.now,
          );
        }
      }
      if (next.toasts.some((t) => t.until <= action.now)) {
        changed = true;
        next = { ...next, toasts: next.toasts.filter((t) => t.until > action.now) };
      }
      return changed ? next : state;
    }

    case 'retry': {
      const doc = docById(state, action.docId);
      if (!doc || doc.status !== 'failed') return state;
      return {
        ...state,
        docs: state.docs.map((d) =>
          d.id === doc.id ? { ...d, status: 'processing', failureReason: null, willFail: false, processingUntil: action.now + RETRY_MS } : d,
        ),
      };
    }

    case 'deleteDoc': {
      const doc = docById(state, action.docId);
      if (!doc) return { ...state, dialog: null };
      const next: MockState = {
        ...state,
        docs: state.docs.filter((d) => d.id !== doc.id),
        // 選択から外す。空になった会話は「すべて」に戻す
        threads: state.threads.map((t) => {
          if (t.scope.mode !== 'selected') return t;
          const ids = t.scope.ids.filter((id) => id !== doc.id);
          return { ...t, scope: ids.length > 0 ? { mode: 'selected', ids } : ALL };
        }),
        panel: state.panel?.docId === doc.id ? null : state.panel,
        dialog: null,
      };
      return toast(next, `「${doc.name}」を削除しました（モックのデータだけを更新しています）。`, 'info', action.now);
    }

    case 'openPanel':
      return { ...state, panel: action.target };

    case 'closePanel':
      return state.panel === null ? state : { ...state, panel: null };

    case 'openDialog':
      return { ...state, dialog: action.dialog };

    case 'closeDialog':
      return state.dialog === null ? state : { ...state, dialog: null };

    case 'dismissToast':
      return { ...state, toasts: state.toasts.filter((t) => t.id !== action.id) };

    case 'reset':
      return createInitialState(action.now, action.view);
  }
}
