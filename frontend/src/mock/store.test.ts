import { describe, expect, it } from 'vitest';
import { INGEST_MS, RETRY_MS } from './data';
import { activeThread, createInitialState, docById, mockReducer, pendingUploads, scopeDocs, sendBlock } from './store';
import type { MockAction, MockState } from './store';

const NOW = Date.parse('2026-11-03T05:00:00Z');

function run(state: MockState, ...actions: MockAction[]): MockState {
  return actions.reduce(mockReducer, state);
}

describe('初期データ', () => {
  it('資料は 8 件で、利用可能・取り込み中・エラーを含む', () => {
    const { docs } = createInitialState(NOW);
    expect(docs).toHaveLength(8);
    expect(new Set(docs.map((d) => d.status))).toEqual(new Set(['ready', 'processing', 'failed']));
  });

  it('最初は空の新しい会話で、質問対象は「すべて」。履歴には出ない', () => {
    const state = createInitialState(NOW);
    expect(activeThread(state).messages).toEqual([]);
    expect(activeThread(state).scope).toEqual({ mode: 'all' });
  });

  it('「すべて」は取り込み済みの資料だけを対象にする', () => {
    const state = createInitialState(NOW);
    const { ready } = scopeDocs(state, { mode: 'all' });
    expect(ready.every((d) => d.status === 'ready')).toBe(true);
    expect(ready).toHaveLength(6);
  });
});

describe('質問の送信', () => {
  it('送信すると回答待ちになり、reply で定型の回答と出典が付く', () => {
    let state = run(createInitialState(NOW), { type: 'send', text: '経費の締め日は？', now: NOW });
    expect(activeThread(state).pending).toBe(true);
    expect(sendBlock(state, activeThread(state))).toBe('pending');
    state = run(state, { type: 'reply', threadId: activeThread(state).id, now: NOW });
    const last = activeThread(state).messages.at(-1);
    expect(last?.role).toBe('assistant');
    expect(last?.sources?.[0]?.docName).toBe('経費精算ガイド.pdf');
    expect(last?.text).toContain('モック');
  });

  it('出典は、質問対象の資料の中からだけ選ぶ', () => {
    const base = createInitialState(NOW);
    const state = run(
      base,
      { type: 'setScope', threadId: activeThread(base).id, scope: { mode: 'selected', ids: ['d2'] }, now: NOW },
      { type: 'send', text: '経費の締め日は？', now: NOW },
      { type: 'reply', threadId: activeThread(base).id, now: NOW },
    );
    const sources = activeThread(state).messages.at(-1)?.sources ?? [];
    expect(sources.length).toBeGreaterThan(0);
    expect(sources.every((s) => s.docId === 'd2')).toBe(true);
  });

  it('質問に使える資料がないときは送れない', () => {
    const base = createInitialState(NOW);
    const state = run(base, { type: 'setScope', threadId: activeThread(base).id, scope: { mode: 'selected', ids: ['d7'] }, now: NOW });
    expect(sendBlock(state, activeThread(state))).toBe('waiting');
    expect(run(state, { type: 'send', text: 'こんにちは', now: NOW })).toBe(state);
  });
});

describe('資料の追加と取り込み', () => {
  const files = [{ name: '新しい資料.pdf', size: 1000 }];

  it('追加すると取り込み中になり、時間が過ぎると利用可能になる', () => {
    let state = run(createInitialState(NOW), { type: 'addFiles', files, origin: 'documents', now: NOW });
    const added = state.docs.find((d) => d.name === '新しい資料.pdf');
    expect(added?.status).toBe('processing');
    state = run(state, { type: 'tick', now: NOW + INGEST_MS - 1 });
    expect(docById(state, added?.id ?? '')?.status).toBe('processing');
    state = run(state, { type: 'tick', now: NOW + INGEST_MS });
    expect(docById(state, added?.id ?? '')?.status).toBe('ready');
  });

  it('チャットから追加すると、選択中の質問対象に入り、完了までは質問に使えない', () => {
    const base = createInitialState(NOW);
    const thread = activeThread(base);
    let state = run(
      base,
      { type: 'setScope', threadId: thread.id, scope: { mode: 'selected', ids: ['d1'] }, now: NOW },
      { type: 'addFiles', files, origin: 'chat', now: NOW },
    );
    const added = state.docs.find((d) => d.name === '新しい資料.pdf');
    expect(activeThread(state).scope).toEqual({ mode: 'selected', ids: ['d1', added?.id] });
    expect(scopeDocs(state, activeThread(state).scope).waiting).toHaveLength(1);
    expect(pendingUploads(state, activeThread(state))).toHaveLength(1);
    state = run(state, { type: 'tick', now: NOW + INGEST_MS });
    expect(scopeDocs(state, activeThread(state).scope).ready).toHaveLength(2);
    expect(pendingUploads(state, activeThread(state))).toHaveLength(0);
  });

  it('ファイル名に「エラー」を含むと失敗し、再試行すると利用可能になる', () => {
    let state = run(createInitialState(NOW), { type: 'addFiles', files: [{ name: 'エラー確認.pdf', size: 10 }], origin: 'documents', now: NOW });
    const id = state.docs.find((d) => d.name === 'エラー確認.pdf')?.id ?? '';
    state = run(state, { type: 'tick', now: NOW + INGEST_MS });
    expect(docById(state, id)?.status).toBe('failed');
    state = run(state, { type: 'retry', docId: id, now: NOW + INGEST_MS });
    expect(docById(state, id)?.status).toBe('processing');
    state = run(state, { type: 'tick', now: NOW + INGEST_MS + RETRY_MS });
    expect(docById(state, id)?.status).toBe('ready');
  });

  it('起動時に取り込み中の資料も、時間が来れば利用可能になる', () => {
    const state = run(createInitialState(NOW), { type: 'tick', now: NOW + 13_000 });
    expect(docById(state, 'd7')?.status).toBe('ready');
  });
});

describe('削除', () => {
  it('削除すると一覧と質問対象から外れ、空になった選択は「すべて」に戻る', () => {
    const base = createInitialState(NOW);
    const thread = activeThread(base);
    const state = run(
      base,
      { type: 'setScope', threadId: thread.id, scope: { mode: 'selected', ids: ['d1'] }, now: NOW },
      { type: 'openPanel', target: { docId: 'd1' } },
      { type: 'deleteDoc', docId: 'd1', now: NOW },
    );
    expect(docById(state, 'd1')).toBeUndefined();
    expect(activeThread(state).scope).toEqual({ mode: 'all' });
    expect(state.panel).toBeNull();
  });
});

describe('この資料で質問', () => {
  it('その資料だけを選んだ会話を開く。取り込み中・エラーの資料では開かない', () => {
    const base = createInitialState(NOW);
    const state = run(base, { type: 'setView', view: 'documents' }, { type: 'askWithDoc', docId: 'd2', now: NOW });
    expect(state.view).toBe('chat');
    expect(activeThread(state).scope).toEqual({ mode: 'selected', ids: ['d2'] });
    expect(run(base, { type: 'askWithDoc', docId: 'd7', now: NOW })).toBe(base);
    expect(run(base, { type: 'askWithDoc', docId: 'd8', now: NOW })).toBe(base);
  });

  it('会話があるときは、新しい会話を作り、元の会話の質問対象は変えない', () => {
    const base = createInitialState(NOW);
    const first = activeThread(base).id;
    let state = run(base, { type: 'send', text: '契約の更新は？', now: NOW }, { type: 'reply', threadId: first, now: NOW });
    state = run(state, { type: 'askWithDoc', docId: 'd3', now: NOW });
    expect(activeThread(state).id).not.toBe(first);
    expect(state.threads.find((t) => t.id === first)?.scope).toEqual({ mode: 'all' });
  });
});

describe('質問対象の変更', () => {
  it('会話の途中で変えると、会話に変更の記録が残る', () => {
    const base = createInitialState(NOW);
    const id = activeThread(base).id;
    const state = run(
      base,
      { type: 'send', text: '契約の更新は？', now: NOW },
      { type: 'reply', threadId: id, now: NOW },
      { type: 'setScope', threadId: id, scope: { mode: 'selected', ids: ['d2', 'd3'] }, now: NOW },
    );
    expect(activeThread(state).messages.at(-1)).toMatchObject({ role: 'system', text: '質問対象を「選択した資料 2件」に変更しました。' });
  });
});

describe('パネル', () => {
  it('別の会話へ移ると、出典を開いたパネルは閉じ、資料の詳細は残る', () => {
    const base = createInitialState(NOW);
    const withCitation = run(base, { type: 'openPanel', target: { docId: 'd5', citation: 1, focus: { page: 2, para: 0, location: 'p.2' } } });
    expect(run(withCitation, { type: 'newConversation', now: NOW }).panel).toBeNull();
    expect(run(withCitation, { type: 'openThread', id: 't1' }).panel).toBeNull();
    const detail = run(base, { type: 'openPanel', target: { docId: 'd5' } });
    expect(run(detail, { type: 'newConversation', now: NOW }).panel).toEqual({ docId: 'd5' });
  });
});
