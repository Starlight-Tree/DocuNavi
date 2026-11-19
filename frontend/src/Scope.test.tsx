import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import { openKnowledge, goChat } from './test/navigation';
import { AuthProvider } from './auth/AuthContext';
import { MSG_SCOPE_CONFLICT } from './api/chat';
import { fakeAuth } from './test/auth';
import type { FakeAuth } from './test/auth';
import {
  COLLECTIONS_BODY,
  DEFAULT_REPLY,
  DOC_TYPES_BODY,
  TARGETS_BODY,
  errorResponse,
  jsonResponse,
  replyResponse,
} from './test/helpers';
import { openSources } from './test/sources';
import { chooseFacet, facetButton, facetChip, facetOptions, findFacetButton, queryFacetButton } from './test/facet';

const UPDATED = '2026-10-19T05:30:00+00:00';

function summary(id: number, source: string, collection: string | null, target: string | null, docType: string | null) {
  return {
    id,
    source,
    title: null,
    collection,
    target,
    doc_type: docType,
    status: 'ready',
    failure_reason: null,
    pages: 3,
    updated_at: UPDATED,
    chunk_count: 5,
    embedded_count: 5,
  };
}

const DOCUMENTS = {
  configured: true,
  documents: [
    summary(1, 'ザーハード1.pdf', 'zahard', 'zahard', 'setting'),
    summary(2, 'ザーハード2.pdf', 'zahard', 'zahard', 'setting'),
    summary(3, 'cafe_store_guide.pdf', 'cafe', 'minato-coffee', 'store-guide'),
    summary(4, 'field_note.pdf', 'memo', null, null),
  ],
};

type HistoryEntry = {
  messages: Array<{ role: 'user' | 'assistant'; text: string }>;
  collection?: string | null;
  target?: string | null;
  scope_unavailable?: boolean;
};

interface Options {
  chat?: (body: Record<string, unknown>, call: number) => Response | Promise<Response>;
  collections?: () => Response | Promise<Response>;
  targets?: () => Response | Promise<Response>;
  docTypes?: () => Response | Promise<Response>;
  /** 会話の一覧（ログイン中だけ使う）。 */
  threads?: Array<{ thread_id: string; title: string }>;
  /** GET /threads/{id}/messages の応答。 */
  history?: Record<string, HistoryEntry | 'fail'>;
  /** GET /documents の応答（既定は DOCUMENTS）。 */
  documents?: typeof DOCUMENTS;
}

/** /health・/collections・/targets・/doc-types・/documents・/threads・/chat/stream を差し替える。chatBodies は /chat/stream の JSON 本文の一覧。 */
function install(options: Options = {}) {
  const chatBodies: Array<Record<string, unknown>> = [];
  const calls = { collections: 0, targets: 0 };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'mock' });
      if (url.endsWith('/collections')) {
        calls.collections += 1;
        return options.collections ? options.collections() : jsonResponse(COLLECTIONS_BODY);
      }
      if (url.endsWith('/targets')) {
        calls.targets += 1;
        return options.targets ? options.targets() : jsonResponse(TARGETS_BODY);
      }
      if (url.endsWith('/doc-types')) return options.docTypes ? options.docTypes() : jsonResponse(DOC_TYPES_BODY);
      if (url === '/documents') return jsonResponse(options.documents ?? DOCUMENTS);
      const history = /^\/threads\/([^/]+)\/messages$/.exec(url);
      if (history) {
        const entry = options.history?.[decodeURIComponent(history[1] ?? '')];
        if (entry === undefined || entry === 'fail') return errorResponse(503, '会話履歴を操作できませんでした。');
        return jsonResponse(entry);
      }
      if (/^\/threads$/.test(url)) {
        const now = UPDATED;
        return jsonResponse({
          threads: (options.threads ?? []).map((t) => ({ ...t, created_at: now, updated_at: now })),
        });
      }
      const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
      chatBodies.push(body);
      return (options.chat ?? (() => replyResponse()))(body, chatBodies.length);
    }),
  );
  return { chatBodies, calls };
}

function setup(auth: FakeAuth | null = null) {
  const user = userEvent.setup();
  render(
    <AuthProvider client={auth}>
      <App />
    </AuthProvider>,
  );
  return { user };
}

type User = ReturnType<typeof userEvent.setup>;

/** 資料一覧の「全資料に戻す」（一部だけ選んでいるときに、件数の横に出る文字のボタン）。 */
const resetButton = () => screen.queryByRole('button', { name: '全資料に戻す' });
async function resetAll(user: User) {
  await user.click(await screen.findByRole('button', { name: '全資料に戻す' }));
}
/** 資料一覧の件数の行が出るまで待つ（一覧が読めた印）。 */
const summaryReady = () => waitFor(() => expect(document.querySelector('.docs-summary')).not.toBeNull());

/** 入力欄の中の、回答対象のチップ（メニューのボタン。一覧が読めてから出る）。 */
const menuButton = () => screen.findByRole('button', { name: /^回答対象/ });
const openMenu = async (user: User) => {
  await user.click(await menuButton());
  return within(await screen.findByRole('menu', { name: '回答対象を選ぶ' }));
};
const pick = async (user: User, name: string | RegExp) => {
  const menu = await openMenu(user);
  await user.click(menu.getByRole('menuitemradio', { name }));
};
const openThread = async (title: string) =>
  userEvent.setup().click(await screen.findByRole('button', { name: new RegExp(`^${title}`) }));
const input = () => screen.getByRole('textbox', { name: 'メッセージ' });

async function ask(user: User, text: string) {
  await user.type(input(), text);
  await user.click(screen.getByRole('button', { name: '送信' }));
}

describe('回答対象: 選択と送信', () => {
  it('対象の一覧が読めると、入力欄の中のチップのメニューで選べる（既定は「すべての資料」）。チップに現在の対象と件数が出る', async () => {
    install();
    const { user } = setup();

    expect(await menuButton()).toHaveTextContent('すべての資料 · 5件');
    const menu = await openMenu(user);
    expect(menu.getAllByRole('menuitemradio').map((item) => item.textContent)).toEqual([
      '✓すべての資料5件',
      'ザーハード3件',
      'みなとコーヒー1件',
      '準備中の対象準備中（取り込み中の資料だけ）1件',
    ]);
    expect(menu.getByRole('menuitemradio', { name: /^すべての資料/ })).toHaveAttribute('aria-checked', 'true');
    expect(screen.queryByRole('group', { name: '検索範囲' })).not.toBeInTheDocument();
  });

  it('「すべて」のまま送ると、本文に target も collection も載せない', async () => {
    const { chatBodies } = install();
    const { user } = setup();
    await menuButton();

    await ask(user, 'ザーハードの大気は？');

    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: 'ザーハードの大気は？' });
  });

  it('会話が無いうちは、対象を選ぶとすぐ切り替わり（確認は出ない）、送ると target が載る（collection は載せない）。続きの発言にも載る', async () => {
    const { chatBodies } = install({
      chat: (_b, call) => replyResponse({ thread_id: 't-1', target: 'zahard', collection: null, hit_count: 1, reply: `返答${call}` }),
    });
    const { user } = setup();

    await pick(user, /^ザーハード/);
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(await menuButton()).toHaveTextContent('ザーハード · 3件');

    await ask(user, '大気は？');
    await screen.findByText('返答1');
    await ask(user, '続きは？');
    await screen.findByText('返答2');

    expect(chatBodies[0]).toEqual({ message: '大気は？', target: 'zahard' });
    expect(chatBodies[1]).toEqual({ message: '続きは？', thread_id: 't-1', target: 'zahard' });
    // 固定されたことの説明は、チップの横ではなく、選択メニューの中に出る
    expect(screen.queryByText('この会話で固定')).not.toBeInTheDocument();
    expect((await openMenu(user)).getByText('この会話で固定')).toBeInTheDocument();
  });

  it('「新しい会話」を押すと、質問対象は「すべて」に戻る', async () => {
    install({ chat: () => replyResponse({ thread_id: 't-1', target: 'zahard', hit_count: 1 }) });
    const { user } = setup();
    await pick(user, /^ザーハード/);
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);

    await user.click(screen.getByRole('button', { name: '新しい会話' }));

    expect(await menuButton()).toHaveTextContent('すべての資料');
  });

  it('準備中の対象も選べる。チャットの上に、資料がまだ無い旨の案内が出る', async () => {
    install();
    const { user } = setup();

    await pick(user, /^準備中の対象/);

    expect(await menuButton()).toHaveTextContent('準備中の対象');
    expect(screen.getByText(/この対象には、いま検索できる資料がありません/)).toBeInTheDocument();
  });

  it('対象の選択は、URL にも localStorage にも残さない（再読み込みで「すべて」に戻る）', async () => {
    install();
    const { user } = setup();
    await pick(user, /^ザーハード/);

    expect(window.location.search).toBe('?tab=chat');
    expect(window.localStorage.length).toBe(0);
  });

  it('メニューは Esc で閉じてボタンにフォーカスが戻り、↓ で項目を移動できる。回答を作っている間は開けない', async () => {
    install({ chat: () => new Promise<Response>(() => undefined) });
    const { user } = setup();
    const button = await menuButton();

    await user.click(button);
    const menu = await screen.findByRole('menu', { name: '回答対象を選ぶ' });
    expect(within(menu).getByRole('menuitemradio', { name: /^すべての資料/ })).toHaveFocus();
    await user.keyboard('{ArrowDown}');
    expect(within(menu).getByRole('menuitemradio', { name: /^ザーハード/ })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(button).toHaveFocus();

    await ask(user, '大気は？');
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', '回答の生成中は変えられません');
  });
});

describe('回答対象: 会話の途中で変える（確認）', () => {
  async function startScopedConversation(user: User) {
    await pick(user, /^ザーハード/);
    await ask(user, '大気は？');
    await screen.findByText('ザーハードの大気は薄い。');
  }
  const server = () =>
    install({
      chat: (_b, call) =>
        replyResponse({
          thread_id: `t-${call}`,
          reply: call === 1 ? 'ザーハードの大気は薄い。' : '店舗の回答です。',
          target: call === 1 ? 'zahard' : 'minato-coffee',
          collection: null,
          hit_count: 1,
        }),
    });

  it('会話があるときに別の対象を選ぶと確認が出て、初期フォーカスは「いまの会話を続ける」。対象はまだ変わらない', async () => {
    server();
    const { user } = setup();
    await startScopedConversation(user);

    await pick(user, /^みなとコーヒー/);

    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText('回答対象を「みなとコーヒー」に変えますか？')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'いまの会話を続ける' })).toHaveFocus();
    expect(await menuButton()).toHaveTextContent('ザーハード');
  });

  it('Esc で閉じると対象は変わらず、フォーカスはメニューのボタンに戻る。会話も残る', async () => {
    server();
    const { user } = setup();
    await startScopedConversation(user);
    await pick(user, /^みなとコーヒー/);
    await screen.findByRole('alertdialog');

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(await menuButton()).toHaveFocus();
    expect(await menuButton()).toHaveTextContent('ザーハード');
    expect(screen.getByText('ザーハードの大気は薄い。')).toBeInTheDocument();
  });

  it('「いまの会話を続ける」でも閉じるだけで、対象は変わらない', async () => {
    server();
    const { user } = setup();
    await startScopedConversation(user);
    await pick(user, /^みなとコーヒー/);

    await user.click(await screen.findByRole('button', { name: 'いまの会話を続ける' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(await menuButton()).toHaveTextContent('ザーハード');
  });

  it('Tab は帯の中の 2 つのボタンを循環し、外へ出ない', async () => {
    server();
    const { user } = setup();
    await startScopedConversation(user);
    await pick(user, /^みなとコーヒー/);
    const dialog = await screen.findByRole('alertdialog');
    const keep = within(dialog).getByRole('button', { name: 'いまの会話を続ける' });
    const confirm = within(dialog).getByRole('button', { name: '新しい会話にする' });

    await user.tab();
    expect(confirm).toHaveFocus();
    await user.tab();
    expect(keep).toHaveFocus();
    await user.tab({ shift: true });
    expect(confirm).toHaveFocus();
  });

  it('「新しい会話にする」で、会話を閉じて新しい対象で始める。入力欄にフォーカスが移り、次の送信は thread_id なし・新しい対象', async () => {
    const { chatBodies } = server();
    const { user } = setup();
    await startScopedConversation(user);
    await pick(user, /^みなとコーヒー/);

    await user.click(await screen.findByRole('button', { name: '新しい会話にする' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(screen.queryByText('ザーハードの大気は薄い。')).not.toBeInTheDocument();
    expect(await menuButton()).toHaveTextContent('みなとコーヒー');
    await waitFor(() => expect(input()).toHaveFocus());

    await ask(user, '営業時間は？');
    await screen.findByText('店舗の回答です。');
    expect(chatBodies[1]).toEqual({ message: '営業時間は？', target: 'minato-coffee' });
  });

  it('いまと同じ対象を選んでも、何も起きない（確認は出ない）', async () => {
    server();
    const { user } = setup();
    await startScopedConversation(user);

    await pick(user, /^ザーハード/);

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });
});

describe('回答対象: 資料ごとの選択（資料一覧のチェックボックス）', () => {
  async function openDocuments(user: User) {
    await openKnowledge(user);
  }
  const rowCheck = (name: string) => screen.findByRole('checkbox', { name: `${name} を回答に使う` });
  const headerCheck = (name: string | RegExp) => screen.findByRole('checkbox', { name });
  const summaryText = async () => {
    await summaryReady();
    return document.querySelector('.docs-summary')?.textContent;
  };
  const sendButton = () => screen.getByRole('button', { name: '送信' });

  it('対象の選択肢が出る（表示名・資料の数・未分類）。選ぶと一覧が絞られ、URL に載る。グループの選択は無い', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);

    expect(await facetOptions(user, '対象')).toEqual([
      'すべて (4)',
      'ザーハード (2)',
      'みなとコーヒー (1)',
      '未分類 (1)',
    ]);
    expect(screen.queryByRole('combobox', { name: 'グループ' })).not.toBeInTheDocument();

    await chooseFacet(user, '対象', 'minato-coffee');

    expect(window.location.search).toBe('?tab=documents&target=minato-coffee');
    expect(screen.getAllByRole('row').slice(1)).toHaveLength(1);
  });

  it('既定は全資料が対象。全部にチェックが付き、「全資料に戻す」は押せない。「全○資料を回答に使う」のボタンは無い', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);

    expect(await summaryText()).toBe('4件の資料');
    for (const name of ['ザーハード1.pdf', 'ザーハード2.pdf', 'cafe_store_guide.pdf', 'field_note.pdf']) {
      expect(await rowCheck(name)).toBeChecked();
    }
    expect(await headerCheck('このページの 4 件を全選択・解除')).toBeChecked();
    expect(resetButton()).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /回答に使う$/ })).not.toBeInTheDocument();
  });

  it('チェックを外すと、入力欄の「回答対象」に即反映される。質問は送らない。次の質問は document_ids を付けて送る（対象は付けない）', async () => {
    const { chatBodies } = install();
    const { user } = setup();
    await openDocuments(user);

    await user.click(await rowCheck('field_note.pdf'));

    expect(await summaryText()).toBe('4件の資料 · 3件を回答対象');
    expect(await rowCheck('field_note.pdf')).not.toBeChecked();
    expect(await headerCheck('このページの 4 件を全選択・解除')).not.toBeChecked();
    expect((await headerCheck('このページの 4 件を全選択・解除')) as HTMLInputElement).toHaveProperty('indeterminate', true);
    expect(await screen.findByRole('button', { name: '全資料に戻す' })).toBeInTheDocument();
    expect(chatBodies).toHaveLength(0);
    // 資料を開かない（チェックは行のクリックに伝わらない）
    expect(window.location.search).toBe('?tab=documents');

    await goChat(user);
    expect(await menuButton()).toHaveTextContent('回答対象：選択した資料 · 3件');

    await ask(user, '営業時間は？');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '営業時間は？', document_ids: [1, 2, 3] });
  });

  it('検索・絞り込みだけでは、選択は変わらない。全選択・解除は表示中の資料だけに効き、その旨が出る', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);

    await chooseFacet(user, '対象', 'zahard');
    // 絞り込んだだけでは何も変わらない
    expect(await summaryText()).toBe('検索結果 2件 · 回答対象 4件');
    expect(resetButton()).not.toBeInTheDocument();

    // 表示中の 2 件だけ解除する
    await user.click(await headerCheck('このページの 2 件を全選択・解除'));
    expect(await summaryText()).toBe('検索結果 2件 · 回答対象 2件');

    // 絞り込みを解除しても、表示していなかった 2 件の選択は変わっていない
    await chooseFacet(user, '対象', '');
    expect(await rowCheck('ザーハード1.pdf')).not.toBeChecked();
    expect(await rowCheck('cafe_store_guide.pdf')).toBeChecked();
    expect(await rowCheck('field_note.pdf')).toBeChecked();
    expect(await headerCheck('このページの 4 件を全選択・解除')).not.toBeChecked();

    // 全体で全選択すると、全部に戻る（保存の形は「絞らない」）
    await user.click(await headerCheck('このページの 4 件を全選択・解除'));
    expect(await summaryText()).toBe('4件の資料');
    expect(resetButton()).not.toBeInTheDocument();
  });

  it('キーワード検索の結果だけを全解除しても、検索を消せば残りの選択は残っている', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);

    await user.type(await screen.findByRole('searchbox'), 'ザーハード');
    await waitFor(() => expect(screen.getAllByRole('row').slice(1)).toHaveLength(2));
    await user.click(await headerCheck('このページの 2 件を全選択・解除'));

    expect(await summaryText()).toBe('検索結果 2件 · 回答対象 2件');
  });

  it('0 件にすると、警告が出て、質問は送れない（送信ボタンは押せず、Enter でも送られない）。全資料に戻すと送れる', async () => {
    const { chatBodies } = install();
    const { user } = setup();
    await openDocuments(user);

    await user.click(await headerCheck('このページの 4 件を全選択・解除'));

    expect(await summaryText()).toBe('4件の資料 · 0件を回答対象');
    expect(within(await screen.findByRole('group', { name: '回答に使う資料' })).getByRole('status')).toHaveTextContent('1 件も選ばれていない');

    await goChat(user);
    expect(await menuButton()).toHaveTextContent('回答対象：選択した資料 · 0件');
    await user.type(input(), 'こんにちは');
    expect(sendButton()).toBeDisabled();
    await user.keyboard('{Enter}');
    expect(chatBodies).toHaveLength(0);
    expect(input()).toHaveValue('こんにちは');
    expect(screen.getByText(/1 件も選ばれていないため、質問は送れません/)).toBeInTheDocument();

    await openDocuments(user);
    await resetAll(user);
    await goChat(user);
    expect(sendButton()).toBeEnabled();
    await user.click(sendButton());
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: 'こんにちは' });
  });

  it('「全資料に戻す」は、選択を外して全資料に戻す（会話が無ければ確認なし）。チップも「すべての資料」に戻る', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);
    await user.click(await rowCheck('field_note.pdf'));

    await resetAll(user);

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(await summaryText()).toBe('4件の資料');
    expect(resetButton()).not.toBeInTheDocument();
    await goChat(user);
    expect(await menuButton()).toHaveTextContent('すべての資料');
  });

  it('会話の途中でも、チェックの付け外しに確認は出ない（質問ごとに適用）。次の質問から選んだ資料だけで探す', async () => {
    const { chatBodies } = install({ chat: () => replyResponse({ thread_id: 't-1', target: null, hit_count: 1 }) });
    const { user } = setup();
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);
    await openDocuments(user);

    await user.click(await rowCheck('field_note.pdf'));
    await user.click(await rowCheck('cafe_store_guide.pdf'));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    await goChat(user);
    await ask(user, '続きは？');
    await waitFor(() => expect(chatBodies).toHaveLength(2));
    expect(chatBodies[1]).toEqual({ message: '続きは？', thread_id: 't-1', document_ids: [1, 2] });
  });

  it('対象を選んでいる会話では、チェックの初期値がその対象の資料になる。「全資料に戻す」は確認を出して新しい会話にする', async () => {
    const { chatBodies } = install({ chat: () => replyResponse({ thread_id: 't-1', target: 'zahard', hit_count: 1 }) });
    const { user } = setup();
    await pick(user, /^ザーハード/);
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);
    await openDocuments(user);

    expect(await summaryText()).toBe('4件の資料 · 2件を回答対象');
    expect(await rowCheck('ザーハード1.pdf')).toBeChecked();
    expect(await rowCheck('cafe_store_guide.pdf')).not.toBeChecked();

    await resetAll(user);

    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: '新しい会話にする' }));
    expect(await summaryText()).toBe('4件の資料');
    await goChat(user);
    expect(await menuButton()).toHaveTextContent('すべての資料');
    expect(chatBodies).toHaveLength(1);
  });

  it('チップのメニューで対象を選び直すと、資料の選択は外れる。選択中でも「すべての資料」を選べる', async () => {
    const { chatBodies } = install();
    const { user } = setup();
    await openDocuments(user);
    await user.click(await rowCheck('field_note.pdf'));
    await goChat(user);
    expect(await menuButton()).toHaveTextContent('選択した資料');

    await pick(user, /^すべての資料/);

    expect(await menuButton()).toHaveTextContent('回答対象：すべての資料');
    await ask(user, '全体の質問');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '全体の質問' });
  });

  it('新しい会話を始めると、資料の選択は外れる', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);
    await user.click(await rowCheck('field_note.pdf'));

    await user.click(screen.getByRole('button', { name: '新しい会話' }));

    await openDocuments(user);
    expect(await summaryText()).toBe('4件の資料');
  });

  it('取り込み中・失敗の資料は、チェックできず、数にも入らない（全選択でも入らない）', async () => {
    install({
      documents: {
        configured: true,
        documents: [
          summary(1, 'ザーハード1.pdf', 'zahard', 'zahard', 'setting'),
          { ...summary(2, 'ザーハード2.pdf', 'zahard', 'zahard', 'setting'), status: 'processing' },
          { ...summary(3, 'cafe_store_guide.pdf', 'cafe', 'minato-coffee', 'store-guide'), status: 'failed' },
        ],
      },
    });
    const { user } = setup();
    await openDocuments(user);

    expect(await summaryText()).toBe('3件の資料');
    expect(await rowCheck('ザーハード2.pdf')).toBeDisabled();
    expect(await rowCheck('cafe_store_guide.pdf')).toBeDisabled();
    expect(resetButton()).not.toBeInTheDocument();
  });
});

describe('資料一覧: ページ分けと選択', () => {
  // 60 件。奇数は対象 minato-coffee、偶数は zahard（30 件ずつ）
  const many = {
    configured: true,
    documents: Array.from({ length: 60 }, (_, i) => {
      const n = i + 1;
      return summary(n, `資料 ${String(n).padStart(2, '0')}.pdf`, 'zahard', n % 2 === 0 ? 'zahard' : 'minato-coffee', 'setting');
    }),
  };
  const rows = () => screen.getAllByRole('row').slice(1);
  const pager = () => screen.findByRole('navigation', { name: 'ページ移動' });
  const next = async (user: User) => user.click(within(await pager()).getByRole('button', { name: '次のページ' }));
  const summaryText = async () => {
    await summaryReady();
    return document.querySelector('.docs-summary')?.textContent;
  };
  const rowCheck = (name: string) => screen.findByRole('checkbox', { name: `${name} を回答に使う` });

  it('1 ページは 25 件。下に表示範囲・総件数・‹ ›が出て、表示件数の切り替えは無い', async () => {
    install({ documents: many });
    const { user } = setup();
    await openKnowledge(user);

    await waitFor(() => expect(rows()).toHaveLength(25));
    expect(await pager()).toHaveTextContent('1–25 / 60 件');
    expect(within(await pager()).getByText('1 / 3')).toBeInTheDocument();
    expect(within(await pager()).getByRole('button', { name: '前のページ' })).toBeDisabled();

    await next(user);
    expect(await pager()).toHaveTextContent('26–50 / 60 件');
    await next(user);
    expect(await pager()).toHaveTextContent('51–60 / 60 件');
    expect(rows()).toHaveLength(10);
    expect(within(await pager()).getByRole('button', { name: '次のページ' })).toBeDisabled();

    expect(within(await pager()).queryByRole('combobox')).toBeNull();
  });

  it('ページを移動しても、選択は保たれる。選択済み件数は全ページの合計', async () => {
    install({ documents: many });
    const { user } = setup();
    await openKnowledge(user);
    expect(await summaryText()).toBe('60件の資料');

    await user.click(await rowCheck('資料 01.pdf'));
    expect(await summaryText()).toBe('60件の資料 · 59件を回答対象');

    await next(user);
    await user.click(await rowCheck('資料 30.pdf'));
    expect(await summaryText()).toBe('60件の資料 · 58件を回答対象');

    await user.click(within(await pager()).getByRole('button', { name: '前のページ' }));
    expect(await rowCheck('資料 01.pdf')).not.toBeChecked();
    expect(await rowCheck('資料 01.pdf')).not.toBeChecked();
    expect(await rowCheck('資料 02.pdf')).toBeChecked();
    expect(await summaryText()).toBe('60件の資料 · 58件を回答対象');
  });

  it('表の包みだけがスクロールする。ページ送りは包みの外（下端に固定）、列見出しは包みの中（上に貼り付く）', async () => {
    install({ documents: many });
    const { user } = setup();
    await openKnowledge(user);
    await waitFor(() => expect(rows()).toHaveLength(25));

    const wrap = document.querySelector('.documents-table-wrap') as HTMLElement;
    expect(wrap).not.toBeNull();
    expect(wrap).toContainElement(screen.getByRole('table'));
    expect(wrap).toContainElement(screen.getAllByRole('columnheader')[0] ?? null);
    expect(wrap).not.toContainElement(await pager());
    expect(wrap.parentElement).toBe(document.querySelector('main.documents'));
  });

  it('「選ばれていない」の警告は、固定の領域でなく、表の包み（スクロールする領域）の中に出る。絞り込み中のチップは強調される', async () => {
    install({ documents: many });
    const { user } = setup();
    await openKnowledge(user);
    await waitFor(() => expect(rows()).toHaveLength(25));
    const wrap = document.querySelector('.documents-table-wrap') as HTMLElement;
    const chip = facetChip;
    expect(chip('対象').dataset.active).toBe('false');

    // 見出しのチェックは、そのページだけに効く。3 ページ分を外して、0 件にする
    for (let page = 1; page <= 3; page++) {
      await user.click(await screen.findByRole('checkbox', { name: /^このページの \d+ 件を全選択・解除$/ }));
      if (page < 3) await next(user);
    }
    // 同じ文は、ほかの場所（入力欄の近く）にも出る。ここでは、表の包みの中にあることだけを見る
    expect(await within(wrap).findByText('回答に使う資料が 1 件も選ばれていないため、質問は送れません。')).toBeInTheDocument();

    await chooseFacet(user, '対象', 'zahard');
    expect(chip('対象').dataset.active).toBe('true');
    expect(chip('資料種別').dataset.active).toBe('false');
  });

  it('絞り込みを変えたあとの最初のページ移動でも、表のスクロールを先頭へ戻す', async () => {
    install({ documents: many });
    const { user } = setup();
    await openKnowledge(user);
    await waitFor(() => expect(rows()).toHaveLength(25));
    const wrap = document.querySelector('.documents-table-wrap') as HTMLElement;

    await next(user); // 2 ページ目
    await chooseFacet(user, '対象', 'minato-coffee'); // 条件が変わり、見えるページは 1 に戻る
    wrap.scrollTop = 400;
    await next(user); // 保存しているページ番号は変わらず 2 のまま。それでも、見えるページは 1 → 2 になる
    expect(wrap.scrollTop).toBe(0);
  });

  it('ページ移動・絞り込みを変えると、表のスクロールを先頭へ戻す（選択は保つ）', async () => {
    install({ documents: many });
    const { user } = setup();
    await openKnowledge(user);
    await waitFor(() => expect(rows()).toHaveLength(25));
    const wrap = document.querySelector('.documents-table-wrap') as HTMLElement;
    const scrollDown = () => {
      wrap.scrollTop = 400;
      expect(wrap.scrollTop).toBe(400); // jsdom がスクロール位置を覚えていることの確認（これが崩れたら、下の検証は意味を持たない）
    };

    await user.click(await rowCheck('資料 01.pdf'));
    scrollDown();
    await next(user);
    expect(wrap.scrollTop).toBe(0);

    scrollDown();
    await user.click(within(await pager()).getByRole('button', { name: '前のページ' }));
    expect(wrap.scrollTop).toBe(0);

    scrollDown();
    await user.type(screen.getByRole('searchbox', { name: /資料名で検索/ }), '資料 0');
    expect(wrap.scrollTop).toBe(0);

    scrollDown();
    await chooseFacet(user, '対象', 'zahard');
    expect(wrap.scrollTop).toBe(0);

    // 選択は、スクロールを戻しても変わらない
    await user.clear(screen.getByRole('searchbox', { name: /資料名で検索/ }));
    await chooseFacet(user, '対象', '');
    expect(await rowCheck('資料 01.pdf')).not.toBeChecked();
    expect(await summaryText()).toBe('60件の資料 · 59件を回答対象');
  });

  it('見出しのチェックは、表示中のページだけに効く。別のページの選択は変わらず、「全資料に戻す」で全部に戻る', async () => {
    install({ documents: many });
    const { user } = setup();
    await openKnowledge(user);

    await user.click(await screen.findByRole('checkbox', { name: 'このページの 25 件を全選択・解除' }));
    expect(await summaryText()).toBe('60件の資料 · 35件を回答対象');

    await next(user);
    await user.click(await screen.findByRole('checkbox', { name: 'このページの 25 件を全選択・解除' }));
    expect(await summaryText()).toBe('60件の資料 · 10件を回答対象');

    await resetAll(user);
    expect(await summaryText()).toBe('60件の資料');
    expect(resetButton()).not.toBeInTheDocument();
  });

  it('「選択 ▾」のメニューは無い。「全資料に戻す」は、一部だけ選んでいるときだけ、件数の横に出る', async () => {
    install({ documents: many });
    const { user } = setup();
    await openKnowledge(user);

    expect(await summaryText()).toBe('60件の資料');
    expect(screen.queryByRole('button', { name: '選択' })).not.toBeInTheDocument();
    expect(resetButton()).not.toBeInTheDocument();

    await user.click(await rowCheck('資料 01.pdf'));
    expect(await summaryText()).toBe('60件の資料 · 59件を回答対象');
    expect(await screen.findByRole('button', { name: '全資料に戻す' })).toBeInTheDocument();
    expect(screen.queryByRole('menuitem')).not.toBeInTheDocument();
  });

  it('検索・並び替えは全件に適用して 1 ページ目に戻る。選択は変わらず、見出しのチェックは表示中のページだけに効く', async () => {
    install({ documents: many });
    const { user } = setup();
    await openKnowledge(user);

    await next(user);
    await next(user);
    expect(await pager()).toHaveTextContent('51–60 / 60 件');

    // 絞り込み（対象）で 1 ページ目に戻る。30 件 → 25 件 + 5 件
    await chooseFacet(user, '対象', 'zahard');
    expect(await pager()).toHaveTextContent('1–25 / 30 件');
    expect(await summaryText()).toBe('検索結果 30件 · 回答対象 60件');

    // 表示中の 25 件だけを解除する（見出しのチェックはそのページだけ）。ほかの 35 件の選択は変わらない
    await user.click(await screen.findByRole('checkbox', { name: 'このページの 25 件を全選択・解除' }));
    expect(await summaryText()).toBe('検索結果 30件 · 回答対象 35件');

    await next(user);
    expect(await pager()).toHaveTextContent('26–30 / 30 件');
    await user.click(await screen.findByRole('checkbox', { name: 'このページの 5 件を全選択・解除' }));
    expect(await summaryText()).toBe('検索結果 30件 · 回答対象 30件');

    // 並び替えでも 1 ページ目に戻る
    await user.click(within(screen.getByRole('columnheader', { name: '資料名' })).getByRole('button'));
    expect(await pager()).toHaveTextContent('1–25 / 30 件');
    expect(await summaryText()).toBe('検索結果 30件 · 回答対象 30件');
  });
});

describe('回答対象: 0 件のとき', () => {
  const zero = (target: string | null, hitCount: number | null) => ({
    chat: (_b: Record<string, unknown>, call: number) =>
      call === 1
        ? replyResponse({ thread_id: 't-1', reply: '見つかりませんでした。', target, collection: null, hit_count: hitCount })
        : replyResponse({ thread_id: 't-2', reply: 'すべてから見つけました。', target: null, collection: null, hit_count: 2 }),
  });

  it('対象を指定して 0 件なら案内と「すべての資料で聞き直す」が出る。押すと、直前の質問が対象なしの新しい会話で送られる', async () => {
    const { chatBodies } = install(zero('minato-coffee', 0));
    const { user } = setup();
    await pick(user, /^みなとコーヒー/);
    await ask(user, 'ザーハードの大気は？');
    await screen.findByText('見つかりませんでした。');
    expect(screen.getByText('「みなとコーヒー」の中には、該当する資料がありませんでした。')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'すべての資料で聞き直す' }));

    await screen.findByText('すべてから見つけました。');
    expect(chatBodies[1]).toEqual({ message: 'ザーハードの大気は？' });
    expect(screen.queryByText('見つかりませんでした。')).not.toBeInTheDocument();
    expect(screen.getAllByText('ザーハードの大気は？')).toHaveLength(1);
    expect(await menuButton()).toHaveTextContent('すべての資料');
  });

  it.each([
    ['対象が「すべて」', null, 0],
    ['検索していない（hit_count が null）', 'minato-coffee', null],
    ['1 件以上拾った', 'minato-coffee', 2],
  ])('案内を出さない: %s', async (_name, target, hitCount) => {
    install(zero(target, hitCount));
    const { user } = setup();
    if (target) await pick(user, /^みなとコーヒー/);
    await ask(user, '質問');
    await screen.findByText('見つかりませんでした。');

    expect(screen.queryByText(/の中には、該当する資料がありませんでした/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'すべての資料で聞き直す' })).not.toBeInTheDocument();
  });
});

describe('回答対象: 出典', () => {
  it('参照資料のチップには、資料名とページだけを出す（対象・グループの名前は、右のパネルで見る）', async () => {
    install({
      chat: () =>
        replyResponse({
          thread_id: 't-1',
          context: '[ザーハード1.pdf]\n大気は薄い。\n\n[cafe_store_guide.pdf]\n営業は 9 時から。',
          sources: [
            { name: 'ザーハード1.pdf', page: 1, page_end: null, weak: false, collection: 'zahard', target: 'zahard' },
            { name: 'cafe_store_guide.pdf', page: 2, page_end: null, weak: false, collection: null, target: null },
          ],
          target: null,
          collection: null,
          hit_count: 2,
        }),
    });
    const { user } = setup();
    await ask(user, '質問');

    const region = await openSources();
    const [zahard, cafe] = within(region).getAllByRole('button', { name: /を右のパネルで開く/ });
    expect(zahard).toHaveTextContent('ザーハード1.pdfp.1');
    expect(cafe).toHaveTextContent('cafe_store_guide.pdfp.2');
    expect(region).not.toHaveTextContent('設定資料');
  });
});

describe('回答対象: 旧 API・食い違い・使えない対象', () => {
  const sent = async (metadata: Parameters<typeof replyResponse>[0]) => {
    install({ chat: () => replyResponse({ thread_id: 't-1', ...metadata }) });
    const { user } = setup();
    await pick(user, /^ザーハード/);
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);
  };
  const warning = () => screen.queryByText(/回答対象が、サーバーに反映されなかった可能性があります/);

  it('対象を送ったのに、応答が対象を返さない（target を知らない旧 API）と、警告を 1 つ出す', async () => {
    await sent({});
    expect(warning()).toBeInTheDocument();
    expect(screen.getAllByText(/反映されなかった可能性/)).toHaveLength(1);
  });

  it('応答の対象が、送った対象と違うと警告する', async () => {
    await sent({ target: 'minato-coffee', collection: null, hit_count: 1 });
    expect(warning()).toBeInTheDocument();
  });

  it('応答の対象が送った対象と同じなら、警告は出ない', async () => {
    await sent({ target: 'zahard', collection: null, hit_count: 1 });
    expect(warning()).not.toBeInTheDocument();
  });

  it('対象を指定していないとき（すべて）は、旧 API でも警告しない', async () => {
    install({ chat: () => replyResponse({ thread_id: 't-1' }) });
    const { user } = setup();
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);
    expect(warning()).not.toBeInTheDocument();
  });

  it('回答が最後まで届く前（messageMetadata が届いていない）は、警告しない', async () => {
    const chunks = [
      { type: 'start', messageId: 'm', messageMetadata: { thread_id: 't-1', mode: 'mock' } },
      { type: 'text-start', id: 'x' },
      { type: 'text-delta', id: 'x', delta: '途中まで' },
    ];
    install({
      chat: () =>
        new Response(chunks.map((c) => `data: ${JSON.stringify(c)}\n\n`).join(''), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream', 'x-vercel-ai-ui-message-stream': 'v1' },
        }),
    });
    const { user } = setup();
    await pick(user, /^ザーハード/);
    await ask(user, '大気は？');
    await screen.findByText('途中まで');
    expect(warning()).not.toBeInTheDocument();
  });

  it('409（会話の途中の対象変更）は、サーバーの文ではなく固定文で伝え、再試行は出さず、「新しい会話にする」を出す', async () => {
    install({
      chat: () => errorResponse(409, 'この会話の質問対象は「zahard」です。サーバーの内部の文。'),
    });
    const { user } = setup();
    await pick(user, /^ザーハード/);
    await ask(user, '大気は？');

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(MSG_SCOPE_CONFLICT)).toBeInTheDocument();
    expect(screen.queryByText(/サーバーの内部の文/)).not.toBeInTheDocument();
    expect(within(alert).queryByRole('button', { name: '再試行' })).not.toBeInTheDocument();

    await userEvent.setup().click(within(alert).getByRole('button', { name: '新しい会話にする' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(await menuButton()).toHaveTextContent('すべての資料');
  });

  it('403（見えない・存在しない・資料が無い対象）は、サーバーの固定文を見せ、再試行は出さず、「新しい会話にする」を出す', async () => {
    install({ chat: () => errorResponse(403, '指定された対象は利用できません。') });
    const { user } = setup();
    await pick(user, /^ザーハード/);
    await ask(user, '大気は？');

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('指定された対象は利用できません。')).toBeInTheDocument();
    expect(within(alert).queryByRole('button', { name: '再試行' })).not.toBeInTheDocument();
    expect(within(alert).getByRole('button', { name: '新しい会話にする' })).toBeInTheDocument();
  });
});

describe('回答対象: 対象の一覧を読めないとき', () => {
  it('取得に失敗しても、メニューは出ず、質問は今までどおり送れる（対象は載らない）。再試行が出る', async () => {
    const { chatBodies } = install({ targets: () => errorResponse(503, '資料の分類の情報がまだ用意されていません。') });
    const { user } = setup();

    expect(await screen.findByText('対象の一覧を読み込めませんでした。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^回答対象/ })).not.toBeInTheDocument();
    expect(screen.getByText(/回答対象：/)).toBeInTheDocument(); // 変更の操作の無い、表示だけのチップ

    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '大気は？' });
  });

  it('旧 API（/targets が 404）でも同じ。対象を選べない', async () => {
    install({ targets: () => errorResponse(404, 'Not Found') });
    setup();

    expect(await screen.findByText('対象の一覧を読み込めませんでした。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^回答対象/ })).not.toBeInTheDocument();
  });

  it('再試行で取り直せると、メニューが出る', async () => {
    let call = 0;
    const { calls } = install({
      targets: () => {
        call += 1;
        return call === 1 ? errorResponse(503, '失敗') : jsonResponse(TARGETS_BODY);
      },
    });
    const { user } = setup();
    await user.click(await screen.findByRole('button', { name: '再試行' }));

    expect(await menuButton()).toBeInTheDocument();
    expect(calls.targets).toBe(2);
  });

  it('失敗したときの資料一覧は、資料にある識別名の選択肢になり、「回答に使う」ボタンは出ない', async () => {
    install({ targets: () => errorResponse(503, '失敗') });
    const { user } = setup();
    await screen.findByText('対象の一覧を読み込めませんでした。');
    await openKnowledge(user);

    expect(await facetOptions(user, '対象')).toEqual([
      'すべて (4)',
      'minato-coffee (1)',
      'zahard (2)',
      '未分類 (1)',
    ]);
    expect(screen.queryByRole('button', { name: /回答に使う$/ })).not.toBeInTheDocument();
  });

  it('サーバーが資料の DB を使っていない（configured:false）ときは、対象の UI を出さない', async () => {
    install({ targets: () => jsonResponse({ configured: false, targets: [] }) });
    const { user } = setup();
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);

    expect(screen.queryByRole('button', { name: /^回答対象/ })).not.toBeInTheDocument();
    expect(screen.queryByText('この会話で固定')).not.toBeInTheDocument();
    expect(screen.queryByText('最初の質問で固定')).not.toBeInTheDocument();
  });

  it('対象が 0 件のときも、対象の UI を出さない', async () => {
    install({ targets: () => jsonResponse({ configured: true, targets: [] }) });
    setup();
    await screen.findByRole('textbox', { name: 'メッセージ' });

    await waitFor(() => expect(screen.queryByRole('button', { name: /^回答対象/ })).not.toBeInTheDocument());
  });
});

describe('回答対象: 保存済みの会話', () => {
  const history: Record<string, HistoryEntry | 'fail'> = {
    t1: { messages: [{ role: 'user', text: '大気は？' }, { role: 'assistant', text: '薄いです。' }], target: 'zahard', collection: null },
    t2: { messages: [{ role: 'user', text: '昔の質問' }, { role: 'assistant', text: '昔の回答' }] },
    t3: 'fail',
    t4: { messages: [{ role: 'user', text: '旧グループの質問' }, { role: 'assistant', text: '旧グループの回答' }], collection: 'cafe', target: null },
    t5: { messages: [{ role: 'user', text: '空になった質問' }, { role: 'assistant', text: '空になった回答' }], collection: null, target: null, scope_unavailable: true },
  };
  const threads = [
    { thread_id: 't1', title: '大気は？' },
    { thread_id: 't2', title: '昔の質問' },
    { thread_id: 't3', title: '読めない会話' },
    { thread_id: 't4', title: '旧グループの質問' },
    { thread_id: 't5', title: '空になった質問' },
  ];

  it('開くと、その会話の対象が復元される。続きの発言には thread_id と target が載る', async () => {
    const { chatBodies } = install({ threads, history, chat: () => replyResponse({ thread_id: 't1', target: 'zahard', hit_count: 1 }) });
    const { user } = setup(fakeAuth());
    await openThread('大気は？');
    await screen.findByText('薄いです。');

    expect(await menuButton()).toHaveTextContent('ザーハード · 3件');
    await ask(user, '続きは？');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '続きは？', thread_id: 't1', target: 'zahard' });
  });

  it('対象の項目が無い会話（古い会話・古い API）は「すべて」。対象は載せない', async () => {
    const { chatBodies } = install({ threads, history, chat: () => replyResponse({ thread_id: 't2' }) });
    const { user } = setup(fakeAuth());
    await openThread('昔の質問');
    await screen.findByText('昔の回答');

    expect(await menuButton()).toHaveTextContent('すべての資料');
    await ask(user, '続きは？');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '続きは？', thread_id: 't2' });
  });

  it('履歴を読めなかった会話は、対象が分からない。どの項目も選ばれず、対象を送らず、返ってきた対象を取り込む', async () => {
    const { chatBodies } = install({
      threads,
      history,
      chat: () => replyResponse({ thread_id: 't3', target: 'minato-coffee', collection: null, hit_count: 1 }),
    });
    const { user } = setup(fakeAuth());
    await openThread('読めない会話');
    await screen.findByTestId('history-error');

    const menu = await openMenu(user);
    for (const item of menu.getAllByRole('menuitemradio')) expect(item).toHaveAttribute('aria-checked', 'false');
    await user.keyboard('{Escape}');
    expect(await menuButton()).toHaveTextContent('この会話の対象');

    await ask(user, '続きは？');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '続きは？', thread_id: 't3' });
    expect(await menuButton()).toHaveTextContent('みなとコーヒー');
  });

  it('対象が分からない会話で別の対象を選ぶと、確認して新しい会話にする', async () => {
    install({ threads, history });
    const { user } = setup(fakeAuth());
    await openThread('読めない会話');
    await screen.findByTestId('history-error');

    await pick(user, /^ザーハード/);

    expect(await screen.findByRole('alertdialog')).toBeInTheDocument();
  });

  it('旧グループ（collection）で固定された会話は、旧グループのまま続く。対象に自動で変えず、新しくは選べない項目として示す', async () => {
    const { chatBodies } = install({ threads, history, chat: () => replyResponse({ thread_id: 't4', collection: 'cafe', target: null, hit_count: 1 }) });
    const { user } = setup(fakeAuth());
    await openThread('旧グループの質問');
    await screen.findByText('旧グループの回答');

    expect(await menuButton()).toHaveTextContent('旧グループ: 店舗案内 · 1件');
    const menu = await openMenu(user);
    const legacy = menu.getByRole('menuitemradio', { name: /^旧グループ: 店舗案内/ });
    expect(legacy).toBeDisabled();
    expect(legacy).toHaveAttribute('aria-checked', 'true');
    expect(legacy).toHaveTextContent('この会話で固定（新しくは選べません）');
    await user.keyboard('{Escape}');

    await ask(user, '続きは？');
    await screen.findByText(DEFAULT_REPLY);
    // 旧グループの範囲は、target と一緒に送らない（同時に送ると 422）
    expect(chatBodies[0]).toEqual({ message: '続きは？', thread_id: 't4', collection: 'cafe' });
  });

  it('旧グループの会話で、0 件だったときの案内は、旧グループ名で出る。対象に変えるには、確認して新しい会話にする', async () => {
    install({
      threads,
      history,
      chat: () => replyResponse({ thread_id: 't4', reply: '見つかりません。', collection: 'cafe', target: null, hit_count: 0 }),
    });
    const { user } = setup(fakeAuth());
    await openThread('旧グループの質問');
    await screen.findByText('旧グループの回答');
    await ask(user, '続きは？');

    expect(await screen.findByText('「旧グループ: 店舗案内」の中には、該当する資料がありませんでした。')).toBeInTheDocument();

    await pick(user, /^ザーハード/);
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument();
  });

  it('保存された対象が、いま利用できない会話は、その旨を出す。対象名は出さず、対象を送らず、続きは送れる', async () => {
    const { chatBodies } = install({ threads, history, chat: () => replyResponse({ thread_id: 't5', target: 'gone', collection: null, hit_count: 0 }) });
    const { user } = setup(fakeAuth());
    await openThread('空になった質問');
    await screen.findByText('空になった回答');

    expect(screen.getByText(/この会話の対象は、いま利用できません/)).toBeInTheDocument();
    expect(await menuButton()).toHaveTextContent('この会話の対象');
    await ask(user, '続きは？');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '続きは？', thread_id: 't5' });
    // 返ってきた識別名を、画面に出さない（取り込まない）
    expect(screen.queryByText('gone')).not.toBeInTheDocument();
    expect(await menuButton()).toHaveTextContent('この会話の対象');
  });
});

describe('チャットが最初の画面。回答対象は入力欄の中のチップにある', () => {
  it('画面の指定が無い URL はチャットを開く。URL は書き換えない', async () => {
    install();
    window.history.replaceState(null, '', '/');
    setup();

    expect(await screen.findByRole('textbox', { name: 'メッセージ' })).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'ナレッジ' })).not.toBeInTheDocument(); // 最初の画面はチャット
    expect(queryFacetButton('対象')).not.toBeInTheDocument();
    expect(window.location.search).toBe('');
  });

  it('入力欄の中に「何を回答に使うか」を出す。対象を変えると追従する。チャットの上の帯と「資料一覧へ」は無い', async () => {
    install();
    const { user } = setup();

    expect(await menuButton()).toHaveTextContent('すべての資料 · 5件');
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).closest('.composer-field')).toContainElement(await menuButton());
    expect(screen.queryByRole('button', { name: /資料一覧へ/ })).not.toBeInTheDocument();
    await pick(user, /^ザーハード/);
    expect(await menuButton()).toHaveTextContent('ザーハード · 3件');
  });

  it('対象とグループの一覧を読めないときは、件数を出さない', async () => {
    install({
      targets: () => errorResponse(503, '資料の分類を取得できませんでした。'),
      collections: () => errorResponse(503, '資料グループを取得できませんでした。'),
    });
    setup();

    expect((await screen.findByText('すべての資料')).closest('.scope-chip-static')).not.toBeNull();
    expect(screen.queryByText(/· \d+件/)).not.toBeInTheDocument();
  });

  it('資料一覧へ移っても、入力途中の文は、チャットに戻ったときも残る', async () => {
    install();
    const { user } = setup();
    await user.type(input(), '書きかけ');

    await openKnowledge(user);

    expect(window.location.search).toBe('?tab=documents');
    expect(await findFacetButton('対象')).toBeVisible();
    await goChat(user);
    expect(input()).toHaveValue('書きかけ');
  });
});

describe('左サイドバーの開閉', () => {
  const sidebarToggle = (name: '閉じる' | '開く') => screen.getByRole('button', { name: `サイドバーを${name}` });

  it('閉じると再表示のボタンが残り、会話・入力途中の文・回答対象は変わらない。開くと元に戻る', async () => {
    install({ chat: () => replyResponse({ thread_id: 't-1', target: 'zahard', hit_count: 1 }) });
    const { user } = setup();
    await pick(user, /^ザーハード/);
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);
    await user.type(input(), '書きかけ');

    await user.click(sidebarToggle('閉じる'));

    // 見えなくするのは CSS（このテストは CSS を読まない）。見た目は e2e で確かめる
    expect(document.querySelector('.ws-shell')).toHaveAttribute('data-side', 'closed');
    expect(screen.getByRole('button', { name: 'サイドバーを開く' })).toHaveFocus();
    expect(screen.getByText(DEFAULT_REPLY)).toBeVisible();
    expect(input()).toHaveValue('書きかけ');
    expect(await menuButton()).toHaveTextContent('ザーハード');

    await user.click(sidebarToggle('開く'));

    expect(document.querySelector('.ws-shell')).not.toHaveAttribute('data-side');
    expect(screen.getByRole('button', { name: 'サイドバーを閉じる' })).toHaveFocus();
    expect(input()).toHaveValue('書きかけ');
    expect(await menuButton()).toHaveTextContent('ザーハード');
    expect(screen.queryByRole('button', { name: 'サイドバーを開く' })).not.toBeInTheDocument();
  });

  it('PC 幅では、閉じるとアイコンだけのレールが残り、各アイコンに名前と選択中の表示がある。開くボタンはレールの先頭のロゴの位置（メインの左上には出ない）。開いているときのロゴは、チャットへ移るリンクのまま', async () => {
    vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === '(min-width: 1024px)', media: query, addEventListener: () => {}, removeEventListener: () => {} }));
    try {
      install();
      const { user } = setup();
      await screen.findByRole('textbox', { name: 'メッセージ' });
      expect(screen.queryByRole('navigation', { name: 'サイドバー（縮小表示）' })).not.toBeInTheDocument();

      await user.click(sidebarToggle('閉じる'));

      const rail = screen.getByRole('navigation', { name: 'サイドバー（縮小表示）' });
      expect(within(rail).getByRole('button', { name: 'サイドバーを開く' })).toHaveFocus();
      // 帯は常設。ただし、再表示のボタンはレールにあり、帯には出ない
      expect(document.querySelector('.ws-main-bar')?.querySelector('button[aria-label="サイドバーを開く"]')).toBeNull();
      expect(within(rail).getByRole('link', { name: 'チャット' })).toHaveAttribute('aria-current', 'page');
      expect(within(rail).getByRole('button', { name: '新しい会話' })).toBeInTheDocument();
      expect(within(rail).getByRole('link', { name: 'ナレッジ' })).not.toHaveAttribute('aria-current');

      await user.click(within(rail).getByRole('link', { name: 'ナレッジ' }));
      expect(within(rail).getByRole('link', { name: 'ナレッジ' })).toHaveAttribute('aria-current', 'page');
      expect(within(rail).getByRole('link', { name: 'チャット' })).not.toHaveAttribute('aria-current');

      // 閉じているときのロゴの位置は、開くボタン（レールに「DocuNavi」のリンクは無い）。項目のクリックは、開閉しない
      expect(within(rail).queryByRole('link', { name: 'DocuNavi' })).not.toBeInTheDocument();
      await user.click(within(rail).getByRole('link', { name: 'チャット' }));
      expect(screen.getByRole('navigation', { name: 'サイドバー（縮小表示）' })).toBeInTheDocument();
      expect(within(rail).getByRole('link', { name: 'チャット' })).toHaveAttribute('aria-current', 'page');

      await user.click(within(rail).getByRole('button', { name: 'サイドバーを開く' }));
      expect(screen.queryByRole('navigation', { name: 'サイドバー（縮小表示）' })).not.toBeInTheDocument();
      expect(sidebarToggle('閉じる')).toHaveFocus();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('閉じたままでも、資料一覧でも再表示のボタンがあり、資料一覧の条件も残る', async () => {
    install();
    const { user } = setup();
    await screen.findByRole('textbox', { name: 'メッセージ' });
    await user.click(sidebarToggle('閉じる'));

    window.history.pushState(null, '', '/?tab=documents&target=zahard');
    window.dispatchEvent(new PopStateEvent('popstate'));

    expect(await findFacetButton('対象')).toHaveAttribute('data-value', 'zahard');
    expect(sidebarToggle('開く')).toBeVisible();
    await user.click(sidebarToggle('開く'));
    expect(facetButton('対象')).toHaveAttribute('data-value', 'zahard');
  });
});
