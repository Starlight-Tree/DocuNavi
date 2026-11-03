import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { EXAMPLE_QUESTIONS } from './components/MessageList';
import { followUpQuestions } from './lib/followUps';
import { findSourceChunk, findSourcePage } from './lib/sources';
import { installFetch, jsonResponse, replyResponse, sseResponse, replyChunks } from './test/helpers';

afterEach(() => {
  vi.unstubAllGlobals();
});

const CONTEXT =
  '[tsubasa_dorm_rules.pdf]\n第1章 総則\n第2条 寮は3棟で構成する。\n\n[tsubasa_dorm_rules.pdf]\n第5条 入寮の申込みは、2 月 28 日までに行う。';

const LIST = {
  configured: true,
  documents: [
    {
      id: 7,
      source: 'tsubasa_dorm_rules.pdf',
      status: 'ready',
      failure_reason: null,
      pages: 3,
      updated_at: '2026-10-18T00:00:00+00:00',
      chunk_count: 2,
      embedded_count: 2,
    },
  ],
};

const DETAIL = {
  document: LIST.documents[0],
  chunks: [
    { chunk_index: 0, page: 1, page_end: null, kind: 'text', heading: '第1章 総則', content: '第2条 寮は 3 棟で\n構成する。', embedded: true, embedding_model: 'm' },
    { chunk_index: 1, page: 3, page_end: null, kind: 'text', heading: null, content: '第5条 入寮の申込みは、2 月 28 日までに行う。', embedded: true, embedding_model: 'm' },
  ],
};

function installAnswerFetch(list: unknown = LIST) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'gemini' });
    if (url.includes('/chat/stream')) return replyResponse({ reply: '2 月 28 日までです。', context: CONTEXT });
    if (url === '/documents') return jsonResponse(list);
    if (url === '/documents/7/chunks') return jsonResponse(DETAIL);
    return new Response('not found', { status: 404 });
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

async function ask(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByRole('textbox', { name: 'メッセージ' }), '申込みはいつ？');
  await user.click(screen.getByRole('button', { name: '送信' }));
}

describe('findSourcePage', () => {
  const chunks = DETAIL.chunks.map((c) => ({ content: c.content, page: c.page }));

  it('空白の違いを無視して、抜粋の元のチャンクのページを返す', () => {
    expect(findSourcePage(chunks, '第1章 総則\n第2条 寮は3棟で構成する。')).toBe(1);
    expect(findSourcePage(chunks, '第5条 入寮の申込みは、2 月 28 日までに行う。')).toBe(3);
  });

  it('どのチャンクにも当たらなければ null', () => {
    expect(findSourcePage(chunks, '全く別の文')).toBeNull();
    expect(findSourcePage(chunks, '  ')).toBeNull();
  });
});

describe('findSourceChunk', () => {
  const chunks = DETAIL.chunks.map((c) => ({ chunkIndex: c.chunk_index, content: c.content, page: c.page }));

  it('空白の違いを無視して、抜粋の元のチャンクの番号を返す', () => {
    expect(findSourceChunk(chunks, '第1章 総則\n第2条 寮は3棟で構成する。')).toBe(0);
    expect(findSourceChunk(chunks, '第5条 入寮の申込みは、2 月 28 日までに行う。')).toBe(1);
  });

  it('どのチャンクにも当たらなければ null（近そうなチャンクを当てずっぽうに選ばない）', () => {
    expect(findSourceChunk(chunks, '全く別の文')).toBeNull();
    expect(findSourceChunk(chunks, '  ')).toBeNull();
    expect(findSourceChunk(chunks, '第5条')).toBeNull(); // 短い文は、無関係なチャンクにも含まれるので使わない
  });

  it('複数のチャンクに当たるときは、検索が返したページと合うものを選ぶ', () => {
    const twins = [
      { chunkIndex: 0, content: '同じ文言の条文がここにあります。', page: 1 },
      { chunkIndex: 1, content: '同じ文言の条文がここにあります。', page: 4 },
    ];
    expect(findSourceChunk(twins, '同じ文言の条文がここにあります。')).toBe(0);
    expect(findSourceChunk(twins, '同じ文言の条文がここにあります。', 4)).toBe(1);
  });
});

describe('出典チップ', () => {
  it('回答の下に、出典ごとのチップを出し、押すと資料を右のパネルに開く（チャットのまま。抽出テキストで根拠を強調する）', async () => {
    installAnswerFetch();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    const chips = await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(chips).toHaveLength(2);

    await user.click(chips[1]!);

    await waitFor(() => expect(window.location.search).toBe('?tab=chat&doc=7&view=text&chunk=1'));
    const panel = await screen.findByRole('complementary', { name: '資料: tsubasa_dorm_rules.pdf' });
    // この資料は元の PDF が無いので、切り替えは出ず、抽出テキストが開く
    expect(within(panel).getByText('資料から抽出したテキストです。')).toBeVisible();
    // 根拠のチャンクだけを強調する
    const marked = panel.querySelectorAll('[data-focus="true"]');
    expect(marked).toHaveLength(1);
    expect(marked[0]).toHaveTextContent('第5条 入寮の申込みは、2 月 28 日までに行う。');
    expect(within(panel).getByText('根拠の箇所')).toBeInTheDocument();
    // 会話は残る
    expect(screen.getByRole('log', { name: '会話' })).toBeVisible();
    expect(screen.getByRole('textbox', { name: 'メッセージ' })).toBeVisible();
  });

  it('根拠のチャンクが見つからなくても資料は開く。何も強調せず、ページも推測しない', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'gemini' });
      if (url.includes('/chat/stream')) {
        return replyResponse({ reply: '回答です。', context: '[tsubasa_dorm_rules.pdf]\n全く別の文章が、ここに入っています。' });
      }
      if (url === '/documents') return jsonResponse(LIST);
      if (url === '/documents/7/chunks') return jsonResponse(DETAIL);
      return new Response('not found', { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    await user.click((await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ }))[0]!);

    const panel = await screen.findByRole('complementary', { name: '資料: tsubasa_dorm_rules.pdf' });
    expect(window.location.search).toBe('?tab=chat&doc=7&view=text');
    expect(panel.querySelector('[data-focus="true"]')).toBeNull();
    expect(within(panel).queryByText('根拠の箇所')).not.toBeInTheDocument();
    expect(within(panel).getAllByRole('listitem')).toHaveLength(2);
  });

  it('資料一覧に無い出典は、開かずに、その旨を出す', async () => {
    installAnswerFetch({ configured: true, documents: [] });
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    await user.click((await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ }))[0]!);

    expect(await screen.findByRole('alert')).toHaveTextContent('資料一覧に見つかりませんでした');
    expect(window.location.search).toBe('?tab=chat');
  });

  it('抜粋は、チップの説明（ヒント）になる。フォーカスで読め、Esc で閉じる。折りたたみの抜粋一覧は出さない', async () => {
    installAnswerFetch();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    const chips = await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
    // ヒントは、チップの説明（aria-describedby）として読み上げられる
    expect(chips[1]).toHaveAccessibleDescription('第5条 入寮の申込みは、2 月 28 日までに行う。');
    expect(screen.getAllByRole('tooltip', { hidden: true })).toHaveLength(2);
    expect(screen.queryByText(/参照した設定資料の抜粋/)).not.toBeInTheDocument();

    // キーボードで来たとき、Esc でヒントだけを閉じる（パネルは開かない）
    await act(async () => chips[1]!.focus());
    await user.keyboard('{Escape}');
    expect(chips[1]!.closest('li')).toHaveAttribute('data-dismissed', 'true');
    expect(window.location.search).toBe('?tab=chat');
    // フォーカスが外れたら、また出せる
    await act(async () => chips[1]!.blur());
    expect(chips[1]!.closest('li')).not.toHaveAttribute('data-dismissed');
  });
});

describe('出典カードのページ（サーバーが検索で返した値）', () => {
  const SOURCES = [
    { name: 'tsubasa_dorm_rules.pdf', page: 2, page_end: 3 },
    { name: 'tsubasa_dorm_rules.pdf', page: 1, page_end: null },
  ];

  function installSourcesFetch(sources: unknown) {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'gemini' });
      if (url.includes('/chat/stream')) return replyResponse({ reply: '2 月 28 日までです。', context: CONTEXT, sources });
      if (url === '/documents') return jsonResponse(LIST);
      if (url === '/documents/7/chunks') return jsonResponse(DETAIL);
      return new Response('not found', { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('カードに「p.1」と、ページをまたぐなら「p.2–3」を出す', async () => {
    installSourcesFetch(SOURCES);
    render(<App />);
    await ask(userEvent.setup());

    const cards = await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(within(cards[0]!).getByText('p.2–3')).toBeInTheDocument();
    expect(within(cards[1]!).getByText('p.1')).toBeInTheDocument();
  });

  it('押すと、サーバーが返したページで開く（抜粋の突き合わせなら 3 ページになる出典でも）', async () => {
    installSourcesFetch(SOURCES);
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    await user.click((await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ }))[1]!);

    // 抜粋の突き合わせなら 3 ページ（chunks 側の値）になる。サーバーの値（1）が使われる
    await waitFor(() => expect(window.location.search).toBe('?tab=chat&doc=7&view=text&page=1&chunk=1'));
  });

  it('sources が無い（古い応答）ときは、ページを出さず、押すと根拠のチャンクだけ突き合わせて開く（ページは推測して持たせない）', async () => {
    installSourcesFetch(undefined);
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    const cards = await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(within(cards[0]!).queryByText(/^p\./)).not.toBeInTheDocument();
    await user.click(cards[1]!);

    await waitFor(() => expect(window.location.search).toBe('?tab=chat&doc=7&view=text&chunk=1'));
  });

  it('sources と出典が合わない（件数が違う）ときは、ずれたページを出さない', async () => {
    installSourcesFetch([{ name: 'tsubasa_dorm_rules.pdf', page: 2, page_end: null }]);
    render(<App />);
    await ask(userEvent.setup());

    const cards = await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
    cards.forEach((card) => expect(within(card).queryByText(/^p\./)).not.toBeInTheDocument());
  });

  it('1 位と類似度が離れた 2 件目（weak）は、参照資料に出さない', async () => {
    installSourcesFetch([
      { name: 'tsubasa_dorm_rules.pdf', page: 2, page_end: 3, weak: false },
      { name: 'tsubasa_dorm_rules.pdf', page: 1, page_end: null, weak: true },
    ]);
    render(<App />);
    await ask(userEvent.setup());

    const cards = await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(cards).toHaveLength(1);
    expect(within(cards[0]!).getByText('p.2–3')).toBeInTheDocument();
    expect(screen.getByText('1 件')).toBeInTheDocument();
  });

  it('sources と出典が合わないときは、weak でも絞らない（ずれた判定を使わない）', async () => {
    installSourcesFetch([{ name: 'tsubasa_dorm_rules.pdf', page: 2, page_end: null, weak: true }]);
    render(<App />);
    await ask(userEvent.setup());

    expect(await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ })).toHaveLength(2);
  });
});

describe('参照資料（回答の下のコンパクトな一覧）', () => {
  const CONTEXT4 = [1, 2, 3, 4].map((n) => `[tsubasa_dorm_rules.pdf]\n第${n}条 本文${n}`).join('\n\n');
  const SOURCES4 = [1, 2, 3, 4].map((page) => ({ name: 'tsubasa_dorm_rules.pdf', page, page_end: null, weak: false }));
  const CONTEXT7 = [1, 2, 3, 4, 5, 6, 7].map((n) => `[tsubasa_dorm_rules.pdf]\n第${n}条 本文${n}`).join('\n\n');
  const SOURCES7 = [1, 2, 3, 4, 5, 6, 7].map((page) => ({ name: 'tsubasa_dorm_rules.pdf', page, page_end: null, weak: false }));

  function installRefs(context: string, sources: unknown, reply = '回答です。') {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'gemini' });
      if (url.includes('/chat/stream')) return replyResponse({ reply, context, sources });
      if (url === '/documents') return jsonResponse(LIST);
      if (url === '/documents/7/chunks') return jsonResponse(DETAIL);
      return new Response('not found', { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);
  }

  it('「参照資料」の見出しと件数の下に、資料名とページのチップを並べる。大きなカード・サムネイル・番号・更新日は出さない', async () => {
    installRefs(CONTEXT4, SOURCES4);
    const { container } = render(<App />);
    await ask(userEvent.setup());

    const region = await screen.findByRole('region', { name: '参照資料' });
    expect(within(region).getByRole('heading', { name: /参照資料/ })).toHaveTextContent('4 件');
    const chips = within(region).getAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(chips).toHaveLength(4);
    expect(within(chips[0]!).getByText('tsubasa_dorm_rules.pdf')).toBeInTheDocument();
    expect(within(chips[0]!).getByText('p.1')).toBeInTheDocument();
    expect(chips[0]).toHaveAccessibleName('tsubasa_dorm_rules.pdf p.1を右のパネルで開く');
    expect(container.querySelector('.reference-card, .reference-thumb, .source-chip-number, img')).toBeNull();
    expect(region).not.toHaveTextContent('更新');
  });

  it('最初は 5 件まで。残りは「ほか n 件を表示」で開き、「折りたたむ」で戻る', async () => {
    installRefs(CONTEXT7, SOURCES7);
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    const region = await screen.findByRole('region', { name: '参照資料' });
    expect(within(region).getAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ })).toHaveLength(5);
    expect(within(region).getByRole('heading', { name: /参照資料/ })).toHaveTextContent('7 件');

    await user.click(within(region).getByRole('button', { name: 'ほか 2 件を表示' }));
    expect(within(region).getAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ })).toHaveLength(7);

    await user.click(within(region).getByRole('button', { name: '折りたたむ' }));
    expect(within(region).getAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ })).toHaveLength(5);
  });

  it('参照資料の専用ページは無い（「参照資料をすべて見る」を出さない）', async () => {
    installRefs(CONTEXT4, SOURCES4);
    render(<App />);
    await ask(userEvent.setup());

    await screen.findByRole('region', { name: '参照資料' });
    expect(screen.queryByRole('button', { name: /参照資料をすべて見る/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('main', { name: '参照資料' })).not.toBeInTheDocument();
  });

  it('同じ資料・ページ・抜粋の出典は 1 つにまとめる。ページか抜粋が違えば、同じ資料でも別の根拠として残す', async () => {
    // 1・2 番目は完全に同じ。3 番目はページだけ違い、4 番目はページが同じで抜粋が違う
    const context = [
      '[tsubasa_dorm_rules.pdf]\n第5条 入寮の申込みは、2 月 28 日までに行う。',
      '[tsubasa_dorm_rules.pdf]\n第5条 入寮の申込みは、2 月 28 日までに行う。',
      '[tsubasa_dorm_rules.pdf]\n第5条 入寮の申込みは、2 月 28 日までに行う。',
      '[tsubasa_dorm_rules.pdf]\n第6条 退寮の届出は、前月末までに行う。',
    ].join('\n\n');
    const sources = [1, 1, 3, 1].map((page) => ({ name: 'tsubasa_dorm_rules.pdf', page, page_end: null, weak: false }));
    installRefs(context, sources);
    render(<App />);
    await ask(userEvent.setup());

    const region = await screen.findByRole('region', { name: '参照資料' });
    const chips = within(region).getAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(chips.map((chip) => chip.textContent)).toEqual([
      expect.stringContaining('p.1'), // 第5条（p.1）
      expect.stringContaining('p.3'), // 同じ抜粋でも、別のページ
      expect.stringContaining('p.1'), // 同じページでも、別の抜粋（第6条）
    ]);
    expect(chips[0]).toHaveAccessibleDescription('第5条 入寮の申込みは、2 月 28 日までに行う。');
    expect(chips[2]).toHaveAccessibleDescription('第6条 退寮の届出は、前月末までに行う。');
    expect(within(region).getByRole('heading', { name: /参照資料/ })).toHaveTextContent('3 件');
  });

  it('文と出典の対応は作らない: 回答の本文にボタンは入れず、本文中の「[1]」もそのままの文字', async () => {
    installRefs(CONTEXT4, SOURCES4, '申込みは 2 月 28 日までです[1]。');
    render(<App />);
    await ask(userEvent.setup());

    const text = await screen.findByText(/申込みは 2 月 28 日までです\[1\]。/);
    expect(text).toBeVisible();
    expect(within(text).queryAllByRole('button')).toHaveLength(0);
    // 出典は、回答の末尾の「参照資料」にまとまっている
    expect(screen.getByRole('region', { name: '参照資料' })).toBeInTheDocument();
  });

  it('押すと、資料を右のパネルに開く。出典のページは持たせる（チャットのまま）', async () => {
    installRefs(CONTEXT4, SOURCES4);
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    const region = await screen.findByRole('region', { name: '参照資料' });
    await user.click(within(region).getAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ })[3]!);

    await waitFor(() => expect(window.location.search).toBe('?tab=chat&doc=7&view=text&page=4'));
    expect(screen.getByRole('log', { name: '会話' })).toBeVisible();
  });

  it('weak の出典は出さない。件数も合わせる', async () => {
    installRefs(CONTEXT4, SOURCES4.map((source, i) => ({ ...source, weak: i >= 2 })));
    render(<App />);
    await ask(userEvent.setup());

    const region = await screen.findByRole('region', { name: '参照資料' });
    expect(within(region).getByRole('heading', { name: /参照資料/ })).toHaveTextContent('2 件');
    expect(within(region).getAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ })).toHaveLength(2);
  });

  it('資料一覧に無い資料を押したら、その旨を出す', async () => {
    installRefs('[unknown.pdf]\n本文', [{ name: 'unknown.pdf', page: 1, page_end: null, weak: false }]);
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    await user.click(await screen.findByRole('button', { name: /unknown\.pdf/ }));

    expect(await screen.findByRole('alert')).toHaveTextContent('資料一覧に見つかりませんでした');
  });

  it('資料の一覧を読めなかったときは、開けなかった旨を出す（パネルは開かない）', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'gemini' });
      if (url.includes('/chat/stream')) return replyResponse({ reply: '回答です。', context: CONTEXT4, sources: SOURCES4 });
      return new Response('error', { status: 500 });
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    await user.click((await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ }))[0]!);

    expect(await screen.findByRole('alert')).toHaveTextContent('資料を開けませんでした');
    expect(window.location.search).toBe('?tab=chat');
  });
});

describe('続けて聞く', () => {
  it('回答の根拠になった資料に答えがある質問を出し、聞いたばかりの質問は出さない', () => {
    const questions = followUpQuestions(['tsubasa_dorm_rules.pdf'], ['入寮できるのは誰ですか？']);
    expect(questions).toEqual(['寮費はいくらですか？', '門限は何時ですか？', '入寮の申込みはいつまでにすればよいですか？']);
    expect(followUpQuestions(['unknown.pdf'], [])).toEqual([]);
    expect(followUpQuestions(['ザーハード2.pdf'], [])).toHaveLength(3);
  });

  it('回答の下に出し、押すとその質問を送る', async () => {
    const { chatBodies } = installFetch((_body, call) =>
      call === 1 ? replyResponse({ reply: '2 月 28 日までです。', context: CONTEXT }) : replyResponse({ reply: '月額です。' }),
    );
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    const group = await screen.findByRole('group', { name: '続けて聞く' });
    // 回答・参照資料より目立たせない（枠付きの質問例のチップとは別の、控えめな見た目）
    expect(within(group).getByRole('button', { name: '寮費はいくらですか？' })).toHaveClass('follow-up');
    expect(within(group).getByRole('button', { name: '寮費はいくらですか？' })).not.toHaveClass('example-chip');
    await user.click(within(group).getByRole('button', { name: '寮費はいくらですか？' }));

    await screen.findByText('月額です。');
    expect(chatBodies()[1]).toMatchObject({ message: '寮費はいくらですか？' });
  });

  it('出典が無い回答（資料を検索していない）には、出さない', async () => {
    installFetch(() => replyResponse({ reply: 'こんにちは', context: '' }));
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    await screen.findByText('こんにちは');
    expect(screen.queryByRole('group', { name: '続けて聞く' })).not.toBeInTheDocument();
  });
});

describe('質問の例', () => {
  it('押すと、その質問を送る', async () => {
    const { chatBodies } = installFetch(() => sseResponse(replyChunks({ reply: '回答' })));
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole('button', { name: EXAMPLE_QUESTIONS[1] }));

    await screen.findByText('回答');
    expect(chatBodies()[0]).toMatchObject({ message: EXAMPLE_QUESTIONS[1] });
  });

  it('会話が始まったら、出さない', async () => {
    installFetch(() => replyResponse());
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByRole('textbox', { name: 'メッセージ' }), 'こんにちは');
    await user.click(screen.getByRole('button', { name: '送信' }));

    await waitFor(() => expect(screen.queryByRole('group', { name: '質問の例' })).not.toBeInTheDocument());
  });
});
