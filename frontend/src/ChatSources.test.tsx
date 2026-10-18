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
