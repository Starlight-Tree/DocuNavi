import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { EXAMPLE_QUESTIONS } from './components/MessageList';
import { followUpQuestions } from './lib/followUps';
import { findSourcePage } from './lib/sources';
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

describe('出典チップ', () => {
  it('回答の下に、出典ごとのチップを出し、押すと元PDF をその箇所のページで開く', async () => {
    installAnswerFetch();
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    const chips = await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(chips).toHaveLength(2);

    await user.click(chips[1]!);

    await waitFor(() => expect(window.location.search).toBe('?tab=documents&doc=7&view=pdf&page=3'));
  });

  it('資料一覧に無い出典は、開かずに、その旨を出す', async () => {
    installAnswerFetch({ configured: true, documents: [] });
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    await user.click((await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ }))[0]!);

    expect(await screen.findByRole('alert')).toHaveTextContent('資料一覧に見つかりませんでした');
    expect(window.location.search).toBe('');
  });

  it('抜粋の全文は、折りたたみの中に残る', async () => {
    installAnswerFetch();
    render(<App />);
    await ask(userEvent.setup());

    expect(await screen.findByText(/参照した設定資料の抜粋（2 件）/)).toBeInTheDocument();
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
    await waitFor(() => expect(window.location.search).toBe('?tab=documents&doc=7&view=pdf&page=1'));
  });

  it('sources が無い（古い応答）ときは、ページを出さず、押すと今までどおり突き合わせで開く', async () => {
    installSourcesFetch(undefined);
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    const cards = await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(within(cards[0]!).queryByText(/^p\./)).not.toBeInTheDocument();
    await user.click(cards[1]!);

    await waitFor(() => expect(window.location.search).toBe('?tab=documents&doc=7&view=pdf&page=3'));
  });

  it('sources と出典が合わない（件数が違う）ときは、ずれたページを出さない', async () => {
    installSourcesFetch([{ name: 'tsubasa_dorm_rules.pdf', page: 2, page_end: null }]);
    render(<App />);
    await ask(userEvent.setup());

    const cards = await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
    cards.forEach((card) => expect(within(card).queryByText(/^p\./)).not.toBeInTheDocument());
  });

  it('1 位と類似度が離れた 2 件目（weak）は、カードに出さない。抜粋の一覧には残る', async () => {
    installSourcesFetch([
      { name: 'tsubasa_dorm_rules.pdf', page: 2, page_end: 3, weak: false },
      { name: 'tsubasa_dorm_rules.pdf', page: 1, page_end: null, weak: true },
    ]);
    render(<App />);
    await ask(userEvent.setup());

    const cards = await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ });
    expect(cards).toHaveLength(1);
    expect(within(cards[0]!).getByText('p.2–3')).toBeInTheDocument();
    // LLM が受け取った抜粋は、折りたたみの中に 2 件とも残る
    expect(screen.getByText('参照した設定資料の抜粋（2 件）')).toBeInTheDocument();
  });

  it('sources と出典が合わないときは、weak でも絞らない（ずれた判定を使わない）', async () => {
    installSourcesFetch([{ name: 'tsubasa_dorm_rules.pdf', page: 2, page_end: null, weak: true }]);
    render(<App />);
    await ask(userEvent.setup());

    expect(await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ })).toHaveLength(2);
  });
});

describe('参照資料カード（回答の下）と専用ページ', () => {
  const CONTEXT4 = [1, 2, 3, 4].map((n) => `[tsubasa_dorm_rules.pdf]\n第${n}条 本文${n}`).join('\n\n');
  const SOURCES4 = [1, 2, 3, 4].map((page) => ({ name: 'tsubasa_dorm_rules.pdf', page, page_end: null, weak: false }));

  function installRefs(context: string, sources: unknown) {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', mode: 'gemini' });
      if (url.includes('/chat/stream')) return replyResponse({ reply: '回答です。', context, sources });
      if (url === '/documents') return jsonResponse(LIST);
      if (url === '/documents/7/chunks') return jsonResponse(DETAIL);
      return new Response('not found', { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);
  }

  it('カードは最大 3 枚。件数は全体で数え、「参照資料をすべて見る」を出す', async () => {
    installRefs(CONTEXT4, SOURCES4);
    render(<App />);
    await ask(userEvent.setup());

    const group = await screen.findByRole('group', { name: /参照資料/ });
    expect(within(group).getAllByRole('button')).toHaveLength(3);
    expect(screen.getByText('参照資料（4 件）')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '参照資料をすべて見る →' })).toBeInTheDocument();
  });

  it('カードに、サムネイルの代わりの灰色の枠・番号・資料名・ページを出し、更新日は出さない', async () => {
    installRefs(CONTEXT4, SOURCES4);
    const { container } = render(<App />);
    await ask(userEvent.setup());

    const card = (await screen.findAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ }))[0]!;
    expect(within(card).getByText('p.1')).toBeInTheDocument();
    expect(within(card).getByText('資料を開く →')).toBeInTheDocument();
    expect(card.querySelector('.reference-thumb')).not.toBeNull();
    expect(card.querySelector('img')).toBeNull();
    expect(card).not.toHaveTextContent('更新');
    expect(container.querySelector('.reference-excerpt')).toBeNull();
  });

  it('専用ページに、全件を抜粋つきで出す。「チャットに戻る」で、会話と入力欄に戻る', async () => {
    installRefs(CONTEXT4, SOURCES4);
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    await user.click(await screen.findByRole('button', { name: '参照資料をすべて見る →' }));

    const page = screen.getByRole('main', { name: '参照資料' });
    expect(within(page).getByRole('heading', { name: 'この回答の参照資料（4 件）' })).toHaveFocus();
    expect(within(page).getAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ })).toHaveLength(4);
    expect(within(page).getByText(/第4条 本文4/)).toBeInTheDocument();
    expect(screen.getByRole('log', { name: '会話', hidden: true })).not.toBeVisible();

    await user.click(within(page).getByRole('button', { name: '← チャットに戻る' }));

    expect(screen.queryByRole('main', { name: '参照資料' })).not.toBeInTheDocument();
    expect(screen.getByRole('log', { name: '会話' })).toBeVisible();
    expect(screen.getByText('回答です。')).toBeVisible();
    expect(screen.getByRole('textbox', { name: 'メッセージ' })).toHaveFocus();
  });

  it('専用ページのカードを押すと、元の PDF をその箇所のページで開く（資料詳細へのリンクは無い）', async () => {
    installRefs(CONTEXT4, SOURCES4);
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    await user.click(await screen.findByRole('button', { name: '参照資料をすべて見る →' }));

    const page = screen.getByRole('main', { name: '参照資料' });
    expect(within(page).queryByText('資料詳細')).not.toBeInTheDocument();
    await user.click(within(page).getAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ })[3]!);

    await waitFor(() => expect(window.location.search).toBe('?tab=documents&doc=7&view=pdf&page=4'));
  });

  it('専用ページから別のタブへ移ったら、チャットに戻ったときは会話が出る', async () => {
    installRefs(CONTEXT4, SOURCES4);
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    await user.click(await screen.findByRole('button', { name: '参照資料をすべて見る →' }));

    await user.click(screen.getByRole('button', { name: '資料一覧' }));
    await user.click(screen.getByRole('button', { name: 'チャット' }));

    expect(screen.queryByRole('main', { name: '参照資料' })).not.toBeInTheDocument();
    expect(screen.getByRole('log', { name: '会話' })).toBeVisible();
  });

  it('weak の出典は、専用ページにも出さない。件数も合わせる', async () => {
    installRefs(CONTEXT4, SOURCES4.map((source, i) => ({ ...source, weak: i >= 2 })));
    const user = userEvent.setup();
    render(<App />);
    await ask(user);

    expect(await screen.findByText('参照資料（2 件）')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '参照資料をすべて見る →' }));
    expect(screen.getByRole('heading', { name: 'この回答の参照資料（2 件）' })).toBeInTheDocument();
    expect(within(screen.getByRole('main', { name: '参照資料' })).getAllByRole('button', { name: /tsubasa_dorm_rules\.pdf/ })).toHaveLength(2);
  });

  it('資料一覧に無い資料を専用ページから押したら、その旨を出す', async () => {
    installRefs('[unknown.pdf]\n本文', [{ name: 'unknown.pdf', page: 1, page_end: null, weak: false }]);
    const user = userEvent.setup();
    render(<App />);
    await ask(user);
    await user.click(await screen.findByRole('button', { name: '参照資料をすべて見る →' }));

    await user.click(within(screen.getByRole('main', { name: '参照資料' })).getByRole('button', { name: /unknown\.pdf/ }));

    expect(await screen.findByRole('alert')).toHaveTextContent('資料一覧に見つかりませんでした');
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
