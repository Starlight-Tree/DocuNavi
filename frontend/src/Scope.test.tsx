import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
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
      if (url === '/documents') return jsonResponse(DOCUMENTS);
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

    expect(await menuButton()).toHaveTextContent('すべての資料 ・ 5 件');
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
    expect(await menuButton()).toHaveTextContent('ザーハード ・ 3 件');

    await ask(user, '大気は？');
    await screen.findByText('返答1');
    await ask(user, '続きは？');
    await screen.findByText('返答2');

    expect(chatBodies[0]).toEqual({ message: '大気は？', target: 'zahard' });
    expect(chatBodies[1]).toEqual({ message: '続きは？', thread_id: 't-1', target: 'zahard' });
    // チップの横に、固定されたことを示すヒントが出る
    expect(screen.getAllByText('この会話で固定')).toHaveLength(1);
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
    expect(screen.getByText('回答の生成中は変えられません')).toBeInTheDocument();
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

describe('回答対象: 資料一覧の絞り込みとは別のもの', () => {
  async function openDocuments(user: User) {
    await user.click(within(await screen.findByRole('navigation', { name: '画面' })).getByRole('link', { name: '資料' }));
  }
  const targetSelect = () => screen.findByRole('combobox', { name: '対象' });
  const askArea = () => screen.findByRole('group', { name: '回答に使う資料' });
  const useAll = (name: string) => screen.findByRole('button', { name });

  it('対象の選択肢が出る（表示名・資料の数・未分類）。選ぶと一覧が絞られ、URL に載る。グループの選択は無い', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);

    const select = await targetSelect();
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'すべて (4)',
      'ザーハード (2)',
      'みなとコーヒー (1)',
      '未分類 (1)',
    ]);
    expect(screen.queryByRole('combobox', { name: 'グループ' })).not.toBeInTheDocument();

    await user.selectOptions(select, 'minato-coffee');

    expect(window.location.search).toBe('?tab=documents&target=minato-coffee');
    expect(screen.getAllByRole('row').slice(1)).toHaveLength(1);
  });

  it('対象を選んで一覧を絞っても、チャットの回答対象は変わらない。長い注記や「現在のチャットの対象」の表示は無い', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);

    await user.selectOptions(await targetSelect(), 'zahard');

    const area = await askArea();
    expect(within(area).getByRole('button', { name: '全3資料を回答に使う' })).toBeEnabled();
    expect(within(area).queryByRole('heading')).not.toBeInTheDocument();
    expect(screen.queryByText(/一覧の絞り込みは/)).not.toBeInTheDocument();
    expect(screen.queryByText(/現在のチャットの/)).not.toBeInTheDocument();

    await user.click(screen.getByRole('link', { name: 'チャット' }));

    expect(await menuButton()).toHaveTextContent('すべての資料');
  });

  it('ボタンは、実際に使う範囲を示す（「全○資料を回答に使う」）。対象が「すべて」でも同じ。資料単位の操作は無い', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);
    const select = await targetSelect();

    // 回答対象は「すべての資料」なので、一覧が「すべて」のときは、すでに適用済み（ボタンは押せない。名前・件数・「適用済み」の表示は出さない）
    let area = await askArea();
    expect(within(area).getByRole('button', { name: '全4資料を回答に使う' })).toBeDisabled();
    expect(within(area).queryByRole('heading')).not.toBeInTheDocument();
    expect(within(area).queryByText('適用済み')).not.toBeInTheDocument();

    await user.selectOptions(select, 'minato-coffee');

    area = await askArea();
    expect(within(area).getByRole('button', { name: '全1資料を回答に使う' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: /この資料/ })).not.toBeInTheDocument();
  });

  it('未分類の資料を選ぶと、ボタンは出ず、「回答の対象に選べません」と出る。一覧に無い対象でも出ない', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);

    await user.selectOptions(await targetSelect(), '_none');
    expect(screen.getByText('未分類の資料は、回答の対象に選べません')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /回答に使う$/ })).not.toBeInTheDocument();

    window.history.replaceState(null, '', '/?tab=documents&target=gone');
    window.dispatchEvent(new PopStateEvent('popstate'));
    await waitFor(() => expect(screen.queryByText('未分類の資料は、回答の対象に選べません')).not.toBeInTheDocument());
    expect(screen.queryByRole('group', { name: '回答に使う資料' })).not.toBeInTheDocument();
  });

  it('会話が無いとき、「全○資料を回答に使う」は、資料一覧に留まったまま回答対象を切り替える。ボタンは押せなくなり、質問は送らない', async () => {
    const { chatBodies } = install();
    const { user } = setup();
    await openDocuments(user);
    await user.selectOptions(await targetSelect(), 'minato-coffee');

    await user.click(await useAll('全1資料を回答に使う'));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(window.location.search).toBe('?tab=documents&target=minato-coffee');
    expect(within(await askArea()).getByRole('button', { name: '全1資料を回答に使う' })).toBeDisabled();
    expect(chatBodies).toHaveLength(0); // 質問は自動で送らない

    await user.click(screen.getByRole('link', { name: 'チャット' }));
    expect(await menuButton()).toHaveTextContent('みなとコーヒー');

    await ask(user, '営業時間は？');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '営業時間は？', target: 'minato-coffee' });
  });

  it('チップで別の対象に変えると、資料一覧のボタンは押せない状態から元に戻る。「すべて」へも切り替えられる', async () => {
    const { chatBodies } = install();
    const { user } = setup();
    await pick(user, /^ザーハード/); // 先に別の対象を選んでおく
    await openDocuments(user);

    const area = await askArea();
    expect(within(area).getByRole('button', { name: '全4資料を回答に使う' })).toBeEnabled();
    await user.click(within(area).getByRole('button', { name: '全4資料を回答に使う' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(window.location.search).toBe('?tab=documents');
    expect(within(await askArea()).getByRole('button', { name: '全4資料を回答に使う' })).toBeDisabled();
    await user.click(screen.getByRole('link', { name: 'チャット' }));
    expect(await menuButton()).toHaveTextContent('すべての資料');

    await ask(user, '全体の質問');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '全体の質問' });
  });

  it('会話があるとき、「全○資料を回答に使う」は資料一覧の上に確認を出す。Esc で閉じるとボタンにフォーカスが戻り、対象は変わらない', async () => {
    install({ chat: () => replyResponse({ thread_id: 't-1', target: null, hit_count: 1 }) });
    const { user } = setup();
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);
    await openDocuments(user);
    await user.selectOptions(await targetSelect(), 'minato-coffee');
    const button = await useAll('全1資料を回答に使う');

    await user.click(button);

    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByRole('button', { name: 'いまの会話を続ける' })).toHaveFocus();
    expect(window.location.search).toBe('?tab=documents&target=minato-coffee');

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(button).toHaveFocus();
    expect(window.location.search).toBe('?tab=documents&target=minato-coffee');
    await user.click(screen.getByRole('link', { name: 'チャット' }));
    expect(await menuButton()).toHaveTextContent('すべての資料');
    expect(screen.getByText(DEFAULT_REPLY)).toBeInTheDocument();
  });

  it('確認で「新しい会話にする」を選ぶと、資料一覧に留まったまま、新しい会話が新しい対象で始まる', async () => {
    install({ chat: () => replyResponse({ thread_id: 't-1', target: null, hit_count: 1 }) });
    const { user } = setup();
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);
    await openDocuments(user);
    await user.selectOptions(await targetSelect(), 'minato-coffee');
    await user.click(await useAll('全1資料を回答に使う'));

    await user.click(await screen.findByRole('button', { name: '新しい会話にする' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(window.location.search).toBe('?tab=documents&target=minato-coffee');
    expect(within(await askArea()).getByRole('button', { name: '全1資料を回答に使う' })).toBeDisabled();
    await user.click(screen.getByRole('link', { name: 'チャット' }));
    expect(await menuButton()).toHaveTextContent('みなとコーヒー');
    expect(screen.queryByText(DEFAULT_REPLY)).not.toBeInTheDocument();
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

    const region = await screen.findByRole('region', { name: '参照資料' });
    const [zahard, cafe] = within(region).getAllByRole('button');
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
    await user.click(within(screen.getByRole('navigation', { name: '画面' })).getByRole('link', { name: '資料' }));

    const select = await screen.findByRole('combobox', { name: '対象' });
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'すべて (4)',
      'minato-coffee (1)',
      'zahard (2)',
      '未分類 (1)',
    ]);
    expect(screen.queryByRole('button', { name: /回答に使う$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('group', { name: '回答に使う資料' })).not.toBeInTheDocument();
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

    expect(await menuButton()).toHaveTextContent('ザーハード ・ 3 件');
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

    expect(await menuButton()).toHaveTextContent('旧グループ: 店舗案内 ・ 1 件');
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
    expect(screen.getByRole('link', { name: 'チャット' })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByRole('combobox', { name: '対象' })).not.toBeInTheDocument();
    expect(window.location.search).toBe('');
  });

  it('入力欄の中に「何を回答に使うか」を出す。対象を変えると追従する。チャットの上の帯と「資料一覧へ」は無い', async () => {
    install();
    const { user } = setup();

    expect(await menuButton()).toHaveTextContent('すべての資料 ・ 5 件');
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).closest('.composer-field')).toContainElement(await menuButton());
    expect(screen.queryByRole('button', { name: /資料一覧へ/ })).not.toBeInTheDocument();
    await pick(user, /^ザーハード/);
    expect(await menuButton()).toHaveTextContent('ザーハード ・ 3 件');
  });

  it('対象とグループの一覧を読めないときは、件数を出さない', async () => {
    install({
      targets: () => errorResponse(503, '資料の分類を取得できませんでした。'),
      collections: () => errorResponse(503, '資料グループを取得できませんでした。'),
    });
    setup();

    expect((await screen.findByText('すべての資料')).closest('.scope-chip-static')).not.toBeNull();
    expect(screen.queryByText(/ 件/)).not.toBeInTheDocument();
  });

  it('資料一覧へ移っても、入力途中の文は、チャットに戻ったときも残る', async () => {
    install();
    const { user } = setup();
    await user.type(input(), '書きかけ');

    await user.click(within(await screen.findByRole('navigation', { name: '画面' })).getByRole('link', { name: '資料' }));

    expect(window.location.search).toBe('?tab=documents');
    expect(await screen.findByRole('combobox', { name: '対象' })).toBeVisible();
    await user.click(screen.getByRole('link', { name: 'チャット' }));
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

  it('閉じたままでも、資料一覧でも再表示のボタンがあり、資料一覧の条件も残る', async () => {
    install();
    const { user } = setup();
    await screen.findByRole('textbox', { name: 'メッセージ' });
    await user.click(sidebarToggle('閉じる'));

    window.history.pushState(null, '', '/?tab=documents&target=zahard');
    window.dispatchEvent(new PopStateEvent('popstate'));

    expect(await screen.findByRole('combobox', { name: '対象' })).toHaveValue('zahard');
    expect(sidebarToggle('開く')).toBeVisible();
    await user.click(sidebarToggle('開く'));
    expect(screen.getByRole('combobox', { name: '対象' })).toHaveValue('zahard');
  });
});
