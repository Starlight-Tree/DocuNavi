import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import { AuthProvider } from './auth/AuthContext';
import { MSG_SCOPE_CONFLICT } from './api/chat';
import { MSG_NO_DOCUMENTS_IN_GROUP } from './components/DocumentsView';
import { fakeAuth } from './test/auth';
import type { FakeAuth } from './test/auth';
import { COLLECTIONS_BODY, DEFAULT_REPLY, errorResponse, jsonResponse, replyResponse } from './test/helpers';

const UPDATED = '2026-10-19T05:30:00+00:00';

function summary(id: number, source: string, collection: string | null) {
  return {
    id,
    source,
    title: null,
    collection,
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
    summary(1, 'ザーハード1.pdf', 'zahard'),
    summary(2, 'ザーハード2.pdf', 'zahard'),
    summary(3, 'cafe_store_guide.pdf', 'cafe'),
    summary(4, 'field_note.pdf', 'memo'),
  ],
};

interface Options {
  chat?: (body: Record<string, unknown>, call: number) => Response | Promise<Response>;
  collections?: () => Response | Promise<Response>;
  /** 会話の一覧（ログイン中だけ使う）。 */
  threads?: Array<{ thread_id: string; title: string }>;
  /** GET /threads/{id}/messages の応答。 */
  history?: Record<string, { messages: Array<{ role: 'user' | 'assistant'; text: string }>; collection?: string | null } | 'fail'>;
}

/** /health・/collections・/documents・/threads・/chat/stream を差し替える。chatBodies は /chat/stream の JSON 本文の一覧。 */
function install(options: Options = {}) {
  const chatBodies: Array<Record<string, unknown>> = [];
  const collectionCalls = { count: 0 };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'mock' });
      if (url.endsWith('/collections')) {
        collectionCalls.count += 1;
        return options.collections ? options.collections() : jsonResponse(COLLECTIONS_BODY);
      }
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
  return { chatBodies, collectionCalls };
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

const chips = () => screen.findByRole('group', { name: '検索範囲' });
const chip = async (name: string | RegExp) => within(await chips()).getByRole('button', { name });
const openThread = async (title: string) =>
  userEvent.setup().click(await screen.findByRole('button', { name: new RegExp(`^${title}`) }));
const input = () => screen.getByRole('textbox', { name: 'メッセージ' });

async function ask(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(input(), text);
  await user.click(screen.getByRole('button', { name: '送信' }));
}

describe('検索範囲: 選択と送信', () => {
  it('グループの一覧が読めると、入力欄の上にチップ、入力欄の中に現在の範囲が出る（既定は「すべて」）', async () => {
    install();
    setup();

    expect((await within(await chips()).findAllByRole('button')).map((b) => b.textContent)).toEqual([
      'すべて',
      '設定資料',
      '店舗案内',
      '準備中の資料準備中',
      'memo',
    ]);
    expect(await chip('すべて')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('検索範囲: すべての資料')).toBeInTheDocument();
  });

  it('「すべて」のまま送ると、本文に collection を載せない', async () => {
    const { chatBodies } = install();
    const { user } = setup();
    await chips();

    await ask(user, 'ザーハードの大気は？');

    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: 'ザーハードの大気は？' });
  });

  it('会話が無いうちは、範囲を選ぶとすぐ切り替わり（確認は出ない）、送ると collection が載る。続きの発言にも載る', async () => {
    const { chatBodies } = install({ chat: (_b, call) => replyResponse({ thread_id: 't-1', collection: 'zahard', hit_count: 1, reply: `返答${call}` }) });
    const { user } = setup();

    await user.click(await chip('設定資料'));
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(await chip('設定資料')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('検索範囲: 設定資料')).toBeInTheDocument();

    await ask(user, '大気は？');
    await screen.findByText('返答1');
    await ask(user, '続きは？');
    await screen.findByText('返答2');

    expect(chatBodies[0]).toEqual({ message: '大気は？', collection: 'zahard' });
    expect(chatBodies[1]).toEqual({ message: '続きは？', thread_id: 't-1', collection: 'zahard' });
    expect(screen.getByText('検索範囲: 設定資料')).toBeInTheDocument();
    expect(screen.getByText('この会話で固定')).toBeInTheDocument();
  });

  it('「新しい会話」を押すと、範囲は「すべて」に戻る', async () => {
    install({ chat: () => replyResponse({ thread_id: 't-1', collection: 'zahard', hit_count: 1 }) });
    const { user } = setup();
    await user.click(await chip('設定資料'));
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);

    await user.click(screen.getByRole('button', { name: '新しい会話' }));

    expect(await chip('すべて')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('検索範囲: すべての資料')).toBeInTheDocument();
  });

  it('準備中のグループも選べる。入力欄には「資料はまだありません」と出る', async () => {
    install();
    const { user } = setup();

    await user.click(await chip(/準備中の資料/));

    expect(screen.getByText('検索範囲: 準備中の資料')).toBeInTheDocument();
    expect(screen.getByText('資料はまだありません')).toBeInTheDocument();
  });

  it('未登録のグループは、名前のまま選べる', async () => {
    const { chatBodies } = install();
    const { user } = setup();

    await user.click(await chip('memo'));
    await ask(user, 'メモは？');

    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: 'メモは？', collection: 'memo' });
  });

  it('範囲の選択は、URL にも localStorage にも残さない（再読み込みで「すべて」に戻る）', async () => {
    install();
    const { user } = setup();
    await user.click(await chip('設定資料'));

    expect(window.location.search).toBe('?tab=chat');
    expect(window.localStorage.length).toBe(0);
  });
});

describe('検索範囲: 会話の途中で変える（確認）', () => {
  async function startScopedConversation(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await chip('設定資料'));
    await ask(user, '大気は？');
    await screen.findByText('ザーハードの大気は薄い。');
  }
  const server = () =>
    install({
      chat: (_b, call) =>
        replyResponse({
          thread_id: `t-${call}`,
          reply: call === 1 ? 'ザーハードの大気は薄い。' : '店舗の回答です。',
          collection: call === 1 ? 'zahard' : 'cafe',
          hit_count: 1,
        }),
    });

  it('会話があるときに別の範囲を選ぶと確認が出て、初期フォーカスは「いまの会話を続ける」。範囲はまだ変わらない', async () => {
    server();
    const { user } = setup();
    await startScopedConversation(user);

    await user.click(await chip('店舗案内'));

    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText('検索範囲を「店舗案内」に変えますか？')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'いまの会話を続ける' })).toHaveFocus();
    expect(await chip('設定資料')).toHaveAttribute('aria-pressed', 'true');
  });

  it('Esc で閉じると範囲は変わらず、フォーカスは押したチップに戻る。会話も残る', async () => {
    server();
    const { user } = setup();
    await startScopedConversation(user);
    await user.click(await chip('店舗案内'));
    await screen.findByRole('alertdialog');

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(await chip('店舗案内')).toHaveFocus();
    expect(await chip('設定資料')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('ザーハードの大気は薄い。')).toBeInTheDocument();
  });

  it('「いまの会話を続ける」でも閉じるだけで、範囲は変わらない', async () => {
    server();
    const { user } = setup();
    await startScopedConversation(user);
    await user.click(await chip('店舗案内'));

    await user.click(await screen.findByRole('button', { name: 'いまの会話を続ける' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(await chip('設定資料')).toHaveAttribute('aria-pressed', 'true');
  });

  it('Tab は帯の中の 2 つのボタンを循環し、外へ出ない', async () => {
    server();
    const { user } = setup();
    await startScopedConversation(user);
    await user.click(await chip('店舗案内'));
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

  it('「新しい会話にする」で、会話を閉じて新しい範囲で始める。入力欄にフォーカスが移り、次の送信は thread_id なし・新しい範囲', async () => {
    const { chatBodies } = server();
    const { user } = setup();
    await startScopedConversation(user);
    await user.click(await chip('店舗案内'));

    await user.click(await screen.findByRole('button', { name: '新しい会話にする' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(screen.queryByText('ザーハードの大気は薄い。')).not.toBeInTheDocument();
    expect(await chip('店舗案内')).toHaveAttribute('aria-pressed', 'true');
    await waitFor(() => expect(input()).toHaveFocus());

    await ask(user, '営業時間は？');
    await screen.findByText('店舗の回答です。');
    expect(chatBodies[1]).toEqual({ message: '営業時間は？', collection: 'cafe' });
  });

  it('いまと同じ範囲を選んでも、何も起きない（確認は出ない）', async () => {
    server();
    const { user } = setup();
    await startScopedConversation(user);

    await user.click(await chip('設定資料'));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  it('回答を作っている間は、チップを選べない', async () => {
    install({ chat: () => new Promise<Response>(() => undefined) });
    const { user } = setup();
    await ask(user, '大気は？');

    expect(await chip('設定資料')).toBeDisabled();
  });
});

describe('検索範囲: 資料一覧とは別のもの', () => {
  async function openDocuments(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole('button', { name: /資料一覧へ/ }));
  }
  const cards = () => screen.findByRole('group', { name: /資料グループ/ });

  it('グループのカードが出る（説明・件数・準備中・未登録）。カードを選ぶと一覧が絞られ、URL に載る', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);

    const group = await cards();
    expect(within(group).getByText('ザーハードに関する設定資料です。')).toBeInTheDocument();
    expect(within(group).getByText('資料 3 件')).toBeInTheDocument();
    expect(within(group).getByText('準備中・資料はまだありません')).toBeInTheDocument();
    expect(within(group).getByText('memo')).toBeInTheDocument();

    await user.click(within(group).getByRole('button', { name: /店舗案内/, pressed: false }));

    expect(window.location.search).toBe('?tab=documents&collection=cafe');
    expect(screen.getAllByRole('row').slice(1)).toHaveLength(1);
  });

  it('カードを選んでも、チャットの検索範囲は変わらない', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);
    const group = await cards();

    await user.click(within(group).getByRole('button', { name: /^設定資料(?!:)/ }));
    await user.click(screen.getByRole('link', { name: 'チャット' }));

    expect(await chip('すべて')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('検索範囲: すべての資料')).toBeInTheDocument();
  });

  it('資料の無いグループを選ぶと、「まだ資料がありません」と出る', async () => {
    install();
    const { user } = setup();
    await openDocuments(user);

    await user.click(within(await cards()).getByRole('button', { name: /^準備中の資料(?!:)/ }));

    expect(screen.getByText(MSG_NO_DOCUMENTS_IN_GROUP)).toBeInTheDocument();
  });

  it('会話が無いとき、「このグループに質問」はすぐチャットへ移り、範囲を切り替えて入力欄にフォーカスを置く', async () => {
    const { chatBodies } = install();
    const { user } = setup();
    await openDocuments(user);

    await user.click(within(await cards()).getByRole('button', { name: '店舗案内: このグループに質問' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(window.location.search).toBe('?tab=chat');
    await waitFor(() => expect(input()).toHaveFocus());
    expect(await chip('店舗案内')).toHaveAttribute('aria-pressed', 'true');

    await ask(user, '営業時間は？');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '営業時間は？', collection: 'cafe' });
  });

  it('「すべて」カードの「すべての資料に質問」で、チャットへ移り、範囲を「すべて」にして入力欄にフォーカスを置く', async () => {
    const { chatBodies } = install();
    const { user } = setup();
    // 先に別のグループを選んでおく（「すべて」へ切り替わることを確かめる）
    await user.click(await chip('設定資料'));
    await openDocuments(user);

    await user.click(within(await cards()).getByRole('button', { name: 'すべて: すべての資料に質問' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(window.location.search).toBe('?tab=chat');
    await waitFor(() => expect(input()).toHaveFocus());
    expect(await chip('すべて')).toHaveAttribute('aria-pressed', 'true');

    await ask(user, '全体の質問');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '全体の質問' });
  });

  it('会話があるとき、「このグループに質問」は資料一覧の上に確認を出す。Esc で閉じるとボタンにフォーカスが戻り、範囲は変わらない', async () => {
    install({ chat: () => replyResponse({ thread_id: 't-1', collection: null, hit_count: 1 }) });
    const { user } = setup();
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);
    await openDocuments(user);
    const button = within(await cards()).getByRole('button', { name: '店舗案内: このグループに質問' });

    await user.click(button);

    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByRole('button', { name: 'いまの会話を続ける' })).toHaveFocus();
    expect(window.location.search).toBe('?tab=documents');

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(button).toHaveFocus();
    expect(window.location.search).toBe('?tab=documents');
    await user.click(screen.getByRole('link', { name: 'チャット' }));
    expect(await chip('すべて')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText(DEFAULT_REPLY)).toBeInTheDocument();
  });

  it('確認で「新しい会話にする」を選ぶと、チャットへ移り、新しい会話が新しい範囲で始まる', async () => {
    install({ chat: () => replyResponse({ thread_id: 't-1', collection: null, hit_count: 1 }) });
    const { user } = setup();
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);
    await openDocuments(user);
    await user.click(within(await cards()).getByRole('button', { name: '店舗案内: このグループに質問' }));

    await user.click(await screen.findByRole('button', { name: '新しい会話にする' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(window.location.search).toBe('?tab=chat');
    expect(await chip('店舗案内')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByText(DEFAULT_REPLY)).not.toBeInTheDocument();
    await waitFor(() => expect(input()).toHaveFocus());
  });
});

describe('検索範囲: 0 件のとき', () => {
  const zero = (collection: string | null, hitCount: number | null) => ({
    chat: (_b: Record<string, unknown>, call: number) =>
      call === 1
        ? replyResponse({ thread_id: 't-1', reply: '見つかりませんでした。', collection, hit_count: hitCount })
        : replyResponse({ thread_id: 't-2', reply: 'すべてから見つけました。', collection: null, hit_count: 2 }),
  });

  it('範囲を指定して 0 件なら案内と「すべての資料で聞き直す」が出る。押すと、直前の質問が範囲なしの新しい会話で送られる', async () => {
    const { chatBodies } = install(zero('cafe', 0));
    const { user } = setup();
    await user.click(await chip('店舗案内'));
    await ask(user, 'ザーハードの大気は？');
    await screen.findByText('見つかりませんでした。');
    expect(screen.getByText('「店舗案内」の中には、該当する資料がありませんでした。')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'すべての資料で聞き直す' }));

    await screen.findByText('すべてから見つけました。');
    expect(chatBodies[1]).toEqual({ message: 'ザーハードの大気は？' });
    expect(screen.queryByText('見つかりませんでした。')).not.toBeInTheDocument();
    expect(screen.getAllByText('ザーハードの大気は？')).toHaveLength(1);
    expect(await chip('すべて')).toHaveAttribute('aria-pressed', 'true');
  });

  it.each([
    ['範囲が「すべて」', null, 0],
    ['検索していない（hit_count が null）', 'cafe', null],
    ['1 件以上拾った', 'cafe', 2],
  ])('案内を出さない: %s', async (_name, collection, hitCount) => {
    install(zero(collection, hitCount));
    const { user } = setup();
    if (collection) await user.click(await chip('店舗案内'));
    await ask(user, '質問');
    await screen.findByText('見つかりませんでした。');

    expect(screen.queryByText(/の中には、該当する資料がありませんでした/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'すべての資料で聞き直す' })).not.toBeInTheDocument();
  });
});

describe('検索範囲: 出典に資料グループを添える', () => {
  it('出典カードに、その資料のグループの表示名が出る（無いものには出ない）', async () => {
    install({
      chat: () =>
        replyResponse({
          thread_id: 't-1',
          context: '[ザーハード1.pdf]\n大気は薄い。\n\n[cafe_store_guide.pdf]\n営業は 9 時から。',
          sources: [
            { name: 'ザーハード1.pdf', page: 1, page_end: null, weak: false, collection: 'zahard' },
            { name: 'cafe_store_guide.pdf', page: 2, page_end: null, weak: false, collection: null },
          ],
          collection: null,
          hit_count: 2,
        }),
    });
    const { user } = setup();
    await ask(user, '質問');

    const group = await screen.findByRole('group', { name: /参照資料/ });
    const [zahard, cafe] = within(group).getAllByRole('button');
    expect(zahard).toHaveTextContent('設定資料 ・ p.1');
    expect(cafe).toHaveTextContent('p.2');
    expect(cafe).not.toHaveTextContent('・');
  });
});

describe('検索範囲: 旧 API・食い違い', () => {
  const sent = async (metadata: Parameters<typeof replyResponse>[0]) => {
    install({ chat: () => replyResponse({ thread_id: 't-1', ...metadata }) });
    const { user } = setup();
    await user.click(await chip('設定資料'));
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);
  };
  const warning = () => screen.queryByText(/検索範囲が、サーバーに反映されなかった可能性があります/);

  it('範囲を送ったのに、応答が範囲を返さない（旧 API）と、警告を 1 つ出す', async () => {
    await sent({});
    expect(warning()).toBeInTheDocument();
    expect(screen.getAllByText(/反映されなかった可能性/)).toHaveLength(1);
  });

  it('応答の範囲が、送った範囲と違うと警告する', async () => {
    await sent({ collection: 'cafe', hit_count: 1 });
    expect(warning()).toBeInTheDocument();
  });

  it('応答の範囲が送った範囲と同じなら、警告は出ない', async () => {
    await sent({ collection: 'zahard', hit_count: 1 });
    expect(warning()).not.toBeInTheDocument();
  });

  it('範囲を指定していないとき（すべて）は、旧 API でも警告しない', async () => {
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
    await user.click(await chip('設定資料'));
    await ask(user, '大気は？');
    await screen.findByText('途中まで');
    expect(warning()).not.toBeInTheDocument();
  });

  it('409（会話の途中の範囲変更）は、サーバーの文ではなく固定文で伝え、再試行は出さず、「新しい会話にする」を出す', async () => {
    install({
      chat: () => errorResponse(409, 'この会話の検索対象は「zahard」です。サーバーの内部の文。'),
    });
    const { user } = setup();
    await user.click(await chip('設定資料'));
    await ask(user, '大気は？');

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(MSG_SCOPE_CONFLICT)).toBeInTheDocument();
    expect(screen.queryByText(/サーバーの内部の文/)).not.toBeInTheDocument();
    expect(within(alert).queryByRole('button', { name: '再試行' })).not.toBeInTheDocument();

    await userEvent.setup().click(within(alert).getByRole('button', { name: '新しい会話にする' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(await chip('すべて')).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('検索範囲: グループの一覧を読めないとき', () => {
  it('取得に失敗しても、「すべて」だけのチップと再試行が出て、質問は今までどおり送れる（範囲は載らない）', async () => {
    const { chatBodies } = install({ collections: () => errorResponse(503, '資料グループの情報がまだ用意されていません。') });
    const { user } = setup();

    const group = await chips();
    expect(within(group).getAllByRole('button').map((b) => b.textContent)).toEqual(['すべて', '再試行']);
    expect(within(group).getByText('グループの一覧を読み込めませんでした。')).toBeInTheDocument();
    expect(screen.queryByText(/検索範囲: /)).not.toBeInTheDocument();

    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '大気は？' });
  });

  it('旧 API（/collections が 404）でも同じ。範囲を選べない', async () => {
    install({ collections: () => errorResponse(404, 'Not Found') });
    setup();

    const group = await chips();
    expect(within(group).queryByRole('button', { name: '設定資料' })).not.toBeInTheDocument();
  });

  it('再試行で取り直せると、チップが増える', async () => {
    let call = 0;
    const { collectionCalls } = install({
      collections: () => {
        call += 1;
        return call === 1 ? errorResponse(503, '失敗') : jsonResponse(COLLECTIONS_BODY);
      },
    });
    const { user } = setup();
    await user.click(await within(await chips()).findByRole('button', { name: '再試行' }));

    expect(await chip('設定資料')).toBeInTheDocument();
    expect(collectionCalls.count).toBe(2);
  });

  it('失敗したときの資料一覧は、資料にある名前（識別名）の絞り込みになり、「このグループに質問」は出ない', async () => {
    install({ collections: () => errorResponse(503, '失敗') });
    const { user } = setup();
    await chips();
    await user.click(screen.getByRole('button', { name: /資料一覧へ/ }));

    const filter = await screen.findByRole('group', { name: '種別で絞り込み' });
    expect(within(filter).getAllByRole('button').map((b) => b.textContent)).toEqual(['すべて', 'cafe', 'memo', 'zahard']);
    expect(screen.queryByRole('button', { name: /このグループに質問/ })).not.toBeInTheDocument();
  });

  it('サーバーが資料の DB を使っていない（configured:false）ときは、範囲の UI を出さない', async () => {
    install({ collections: () => jsonResponse({ configured: false, collections: [] }) });
    const { user } = setup();
    await ask(user, '大気は？');
    await screen.findByText(DEFAULT_REPLY);

    expect(screen.queryByRole('group', { name: '検索範囲' })).not.toBeInTheDocument();
    expect(screen.queryByText(/検索範囲: /)).not.toBeInTheDocument();
  });

  it('グループが 0 件のときも、範囲の UI を出さない', async () => {
    install({ collections: () => jsonResponse({ configured: true, collections: [] }) });
    setup();
    await screen.findByRole('textbox', { name: 'メッセージ' });

    await waitFor(() => expect(screen.queryByRole('group', { name: '検索範囲' })).not.toBeInTheDocument());
  });
});

describe('検索範囲: 保存済みの会話', () => {
  const history = {
    t1: { messages: [{ role: 'user' as const, text: '大気は？' }, { role: 'assistant' as const, text: '薄いです。' }], collection: 'zahard' },
    t2: { messages: [{ role: 'user' as const, text: '昔の質問' }, { role: 'assistant' as const, text: '昔の回答' }] },
    t3: 'fail' as const,
  };
  const threads = [
    { thread_id: 't1', title: '大気は？' },
    { thread_id: 't2', title: '昔の質問' },
    { thread_id: 't3', title: '読めない会話' },
  ];

  it('開くと、その会話の範囲が復元される。続きの発言には thread_id と範囲が載る', async () => {
    const { chatBodies } = install({ threads, history, chat: () => replyResponse({ thread_id: 't1', collection: 'zahard', hit_count: 1 }) });
    const { user } = setup(fakeAuth());
    await openThread('大気は？');
    await screen.findByText('薄いです。');

    expect(await chip('設定資料')).toHaveAttribute('aria-pressed', 'true');
    await ask(user, '続きは？');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '続きは？', thread_id: 't1', collection: 'zahard' });
  });

  it('範囲の項目が無い会話（古い会話・古い API）は「すべて」。範囲は載せない', async () => {
    const { chatBodies } = install({ threads, history, chat: () => replyResponse({ thread_id: 't2' }) });
    const { user } = setup(fakeAuth());
    await openThread('昔の質問');
    await screen.findByText('昔の回答');

    expect(await chip('すべて')).toHaveAttribute('aria-pressed', 'true');
    await ask(user, '続きは？');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '続きは？', thread_id: 't2' });
  });

  it('履歴を読めなかった会話は、範囲が分からない。どのチップも選ばれず、範囲を送らず、返ってきた範囲を取り込む', async () => {
    const { chatBodies } = install({ threads, history, chat: () => replyResponse({ thread_id: 't3', collection: 'cafe', hit_count: 1 }) });
    const { user } = setup(fakeAuth());
    await openThread('読めない会話');
    await screen.findByTestId('history-error');

    for (const button of within(await chips()).getAllByRole('button')) {
      expect(button).toHaveAttribute('aria-pressed', 'false');
    }
    expect(screen.getByText('検索範囲: この会話の設定')).toBeInTheDocument();

    await ask(user, '続きは？');
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies[0]).toEqual({ message: '続きは？', thread_id: 't3' });
    expect(await chip('店舗案内')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('検索範囲: 店舗案内')).toBeInTheDocument();
  });

  it('範囲が分からない会話でチップを選ぶと、確認して新しい会話にする', async () => {
    install({ threads, history });
    const { user } = setup(fakeAuth());
    await openThread('読めない会話');
    await screen.findByTestId('history-error');

    await user.click(await chip('設定資料'));

    expect(await screen.findByRole('alertdialog')).toBeInTheDocument();
  });
});

describe('資料一覧が最初の画面。チャットの上に、質問の対象と戻る導線を出す', () => {
  it('画面の指定が無い URL は資料一覧を開く。URL は書き換えない', async () => {
    install();
    window.history.replaceState(null, '', '/');
    setup();

    expect(await screen.findByRole('group', { name: /資料グループ/ })).toBeVisible();
    expect(screen.getByRole('link', { name: '資料' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('textbox', { name: 'メッセージ', hidden: true })).not.toBeVisible();
    expect(window.location.search).toBe('');
  });

  it('チャットの上に「何について質問か」を出す。範囲を変えると追従する', async () => {
    install();
    const { user } = setup();

    expect(await screen.findByText('すべての資料（5件）')).toBeVisible();
    await user.click(await chip('設定資料'));
    expect(screen.getByText('設定資料（3件）')).toBeVisible();
    expect(screen.queryByText('すべての資料（5件）')).not.toBeInTheDocument();
  });

  it('グループの一覧を読めないときは、件数を出さない', async () => {
    install({ collections: () => errorResponse(503, '資料グループを取得できませんでした。') });
    setup();

    expect(await screen.findByText('すべての資料')).toBeVisible();
    expect(screen.queryByText(/件）/)).not.toBeInTheDocument();
  });

  it('「資料一覧へ」で資料一覧に戻る。入力途中の文は、チャットに戻ったときも残る', async () => {
    install();
    const { user } = setup();
    await user.type(input(), '書きかけ');

    await user.click(await screen.findByRole('button', { name: /資料一覧へ/ }));

    expect(window.location.search).toBe('?tab=documents');
    expect(await screen.findByRole('group', { name: /資料グループ/ })).toBeVisible();
    await user.click(screen.getByRole('link', { name: 'チャット' }));
    expect(input()).toHaveValue('書きかけ');
  });
});
