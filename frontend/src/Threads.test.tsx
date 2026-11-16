import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import App from './App';
import { AuthProvider } from './auth/AuthContext';
import { MSG_NO_THREADS, MSG_THREADS_UNAVAILABLE } from './components/ThreadSidebar';
import { THREAD_ID_STORAGE_KEY } from './lib/storage';
import { fakeAuth } from './test/auth';
import type { FakeAuth } from './test/auth';
import { MSG_HISTORY_FAILED } from './hooks/useChat';
import { DEFAULT_REPLY, deferred, errorResponse, installFetch, jsonResponse, replyResponse } from './test/helpers';
import { openSources } from './test/sources';

const UPDATED = '2026-10-19T05:30:00+00:00';

function thread(id: string, title: string) {
  return { thread_id: id, title, created_at: UPDATED, updated_at: UPDATED };
}

function setup(auth: FakeAuth | null) {
  const user = userEvent.setup();
  render(
    <AuthProvider client={auth}>
      <App />
    </AuthProvider>,
  );
  return { user };
}

type HistoryItem = { role: 'user' | 'assistant'; text: string; sent_at?: string; context?: string; sources?: unknown };

/**
 * 会話の一覧の状態を持つ偽のサーバー。DELETE は一覧から外す。
 * GET /threads/{id}/messages は options.messages[id]（無ければ空）を返す。gate[id] があればそれが解けるまで待つ。
 */
function threadsServer(
  initial: ReturnType<typeof thread>[],
  options: {
    failDelete?: boolean;
    failMessages?: boolean;
    messages?: Record<string, HistoryItem[]>;
    /** 会話ごとの検索範囲（GET /threads/{id}/messages の collection）。無ければ送らない（古い API）。 */
    collections?: Record<string, string | null>;
    gate?: Record<string, Promise<void>>;
  } = {},
) {
  let items = [...initial];
  const log: Array<{ method: string; url: string; authorization: string | null }> = [];
  const handler = async (url: string, method: string, init: RequestInit) => {
    log.push({ method, url, authorization: new Headers(init.headers).get('Authorization') });
    const history = /^\/threads\/([^/]+)\/messages$/.exec(url);
    if (history) {
      const id = decodeURIComponent(history[1] ?? '');
      await options.gate?.[id];
      if (options.failMessages) return errorResponse(503, '会話履歴を操作できませんでした。しばらく待ってから再試行してください。');
      const collection = options.collections?.[id];
      return jsonResponse({
        messages: options.messages?.[id] ?? [],
        ...(collection !== undefined ? { collection } : {}),
      });
    }
    if (method === 'DELETE') {
      if (options.failDelete) return errorResponse(503, '会話履歴を操作できませんでした。しばらく待ってから再試行してください。');
      const id = decodeURIComponent(url.split('/threads/')[1] ?? '');
      items = items.filter((item) => item.thread_id !== id);
      return new Response(null, { status: 204 });
    }
    return jsonResponse({ threads: items });
  };
  return { handler, log, set: (next: typeof items) => (items = next) };
}

const sidebar = () => screen.findByRole('complementary', { name: '会話の一覧' });

describe('会話の一覧', () => {
  it('ログインを使わない構成でも、「会話の履歴」の欄は出し、出せない理由を示す。一覧は取りにも行かない', async () => {
    const server = threadsServer([thread('t1', '最初の質問')]);
    installFetch(() => replyResponse(), server.handler);
    setup(null);

    await screen.findByRole('textbox', { name: 'メッセージ' });
    const list = await sidebar();
    expect(within(list).getByRole('heading', { name: '会話の履歴' })).toBeInTheDocument();
    expect(within(list).getByText(MSG_THREADS_UNAVAILABLE)).toBeInTheDocument();
    // 読み込み中でも 0 件でもない（別の状態として区別できる）
    expect(within(list).queryByRole('status')).not.toBeInTheDocument();
    expect(within(list).queryByText(MSG_NO_THREADS)).not.toBeInTheDocument();
    expect(within(list).queryByRole('list')).not.toBeInTheDocument();
    expect(server.log).toHaveLength(0);
  });

  it('一覧を取っている間は「読み込み中…」を出し、届いたら会話に替わる（0 件の案内は出さない）', async () => {
    const gate = deferred<void>();
    const server = threadsServer([thread('t1', '最初の質問')]);
    installFetch(
      () => replyResponse(),
      async (url, method, init) => {
        if (url === '/threads') await gate.promise;
        return server.handler(url, method, init);
      },
    );
    setup(fakeAuth());

    const list = await sidebar();
    expect(within(list).getByRole('status')).toHaveTextContent('読み込み中');
    expect(within(list).queryByText(MSG_NO_THREADS)).not.toBeInTheDocument();

    gate.resolve();
    expect(await within(list).findByTitle('最初の質問')).toBeInTheDocument();
    expect(within(list).queryByRole('status')).not.toBeInTheDocument();
  });

  it('ログイン中は、トークンを付けて取り、題名と更新日時を新しい順のまま並べる', async () => {
    const server = threadsServer([thread('t2', '二つ目の質問'), thread('t1', '最初の質問')]);
    installFetch(() => replyResponse(), server.handler);
    setup(fakeAuth());

    const list = await sidebar();
    const items = await within(list).findAllByRole('listitem');

    expect(items.map((item) => within(item).getByTitle(/質問/).getAttribute('title'))).toEqual(['二つ目の質問', '最初の質問']);
    expect(items[0]).toHaveTextContent(/\d+\/\d+/);
    expect(server.log[0]).toMatchObject({ method: 'GET', authorization: 'Bearer tok-1' });
  });

  it('会話が無ければ案内を出す', async () => {
    installFetch(() => replyResponse(), threadsServer([]).handler);
    setup(fakeAuth());

    expect(await screen.findByText(MSG_NO_THREADS)).toBeInTheDocument();
  });

  it('取得に失敗したら、固定メッセージと再読み込みを出し、押すと取り直す', async () => {
    let calls = 0;
    installFetch(
      () => replyResponse(),
      () => {
        calls += 1;
        return calls === 1
          ? errorResponse(503, '会話履歴を操作できませんでした。しばらく待ってから再試行してください。')
          : jsonResponse({ threads: [thread('t1', '最初の質問')] });
      },
    );
    const { user } = setup(fakeAuth());

    expect(await within(await sidebar()).findByRole('alert')).toHaveTextContent('会話履歴を操作できませんでした');
    await user.click(screen.getByRole('button', { name: '再読み込み' }));

    expect(await screen.findByTitle('最初の質問')).toBeInTheDocument();
  });

  it('会話を選ぶと続きから始まり、次の質問にその thread_id を付ける', async () => {
    const server = threadsServer([thread('t1', '最初の質問')]);
    const { chatBodies } = installFetch(() => replyResponse({ thread_id: 't1' }), server.handler);
    const { user } = setup(fakeAuth());

    await user.click(await screen.findByTitle('最初の質問'));

    await waitFor(() => expect(screen.queryByTestId('history-loading')).not.toBeInTheDocument());
    expect(screen.getByTitle('最初の質問')).toHaveAttribute('aria-current', 'true');
    expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBe('t1');
    await user.type(screen.getByRole('textbox', { name: 'メッセージ' }), '続きの質問');
    await user.click(screen.getByRole('button', { name: '送信' }));
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies()[0]).toEqual({ message: '続きの質問', thread_id: 't1' });
  });

  it('会話を選ぶと、以前のやり取りをトークン付きで取得して画面に並べる', async () => {
    const server = threadsServer([thread('t1', '最初の質問')], {
      messages: {
        t1: [
          { role: 'user', text: 'ザーハードの大気は？' },
          { role: 'assistant', text: '主に二酸化炭素です。' },
        ],
      },
    });
    const { chatBodies } = installFetch(() => replyResponse({ thread_id: 't1' }), server.handler);
    const { user } = setup(fakeAuth());

    await user.click(await screen.findByTitle('最初の質問'));

    const log = await screen.findByRole('log', { name: '会話' });
    expect(await within(log).findByText('ザーハードの大気は？')).toBeInTheDocument();
    expect(within(log).getByText('主に二酸化炭素です。')).toBeInTheDocument();
    expect(server.log.find((entry) => entry.url === '/threads/t1/messages')).toMatchObject({
      method: 'GET',
      authorization: 'Bearer tok-1',
    });
    // 続きを送ると、その会話に付く。取り込んだ発言は送り直さない（最後の発言だけを送る）
    await user.type(screen.getByRole('textbox', { name: 'メッセージ' }), '続きの質問');
    await user.click(screen.getByRole('button', { name: '送信' }));
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies()).toEqual([{ message: '続きの質問', thread_id: 't1' }]);
    expect(within(log).getByText('ザーハードの大気は？')).toBeInTheDocument();
  });

  it('開き直した会話の回答にも、保存された出典から「参照資料」を出す（出典が無い回答には出さない）', async () => {
    const server = threadsServer([thread('t1', '最初の質問')], {
      messages: {
        t1: [
          { role: 'user', text: 'ザーハードの大気は？' },
          {
            role: 'assistant',
            text: '主に窒素です。',
            context: '[1] ザーハード1.pdf\n大気は主に窒素です。',
            sources: [{ name: 'ザーハード1.pdf', page: 2, page_end: null, weak: false, collection: 'zahard', document_id: 7 }],
          },
          { role: 'user', text: '雑談' },
          { role: 'assistant', text: 'こんにちは。' },
        ],
      },
    });
    installFetch(() => replyResponse({ thread_id: 't1' }), server.handler);
    const { user } = setup(fakeAuth());

    await user.click(await screen.findByTitle('最初の質問'));

    expect(await screen.findAllByRole('button', { name: /^\d+ 件のソース/ })).toHaveLength(1);
    const region = await openSources();
    expect(within(region).getByText(/ザーハード1\.pdf/)).toBeInTheDocument();
    expect(within(region).getByText(/p\.2/)).toBeInTheDocument();
  });

  it('質問の前に時刻の区切りを出す（開き直した会話はサーバーの時刻、送った直後は今の時刻。時刻の無い質問には出さない）', async () => {
    const server = threadsServer([thread('t1', '最初の質問')], {
      messages: {
        t1: [
          { role: 'user', text: 'ザーハードの大気は？', sent_at: '2025-11-14T12:00:00+00:00' },
          { role: 'assistant', text: '主に窒素です。' },
          { role: 'user', text: '時刻が保存される前の質問' },
          { role: 'assistant', text: 'こんにちは。' },
        ],
      },
    });
    installFetch(() => replyResponse({ thread_id: 't1' }), server.handler);
    const { user } = setup(fakeAuth());

    await user.click(await screen.findByTitle('最初の質問'));

    const log = await screen.findByRole('log', { name: '会話' });
    await within(log).findByText('ザーハードの大気は？');
    // 去年の日付なので年も付く。時刻のある質問だけに 1 つ（端末のタイムゾーンで時刻は変わるので、形だけ確かめる）
    expect(log.querySelectorAll('time')).toHaveLength(1);
    expect(log.querySelector('time')).toHaveTextContent(/^2025年11月14日 \d\d:\d\d$/);
    expect(log.querySelector('time')).toHaveAttribute('datetime', '2025-11-14T12:00:00+00:00');

    // 続きを送ると、その質問の前に「今日」の時刻が付く（端末の時計。履歴の質問の時刻は変わらない）
    await user.type(screen.getByRole('textbox', { name: 'メッセージ' }), '続きの質問');
    await user.click(screen.getByRole('button', { name: '送信' }));
    await screen.findByText(DEFAULT_REPLY);
    const times = Array.from(log.querySelectorAll('time'));
    expect(times).toHaveLength(2);
    expect(times[1]).toHaveTextContent(/^今日 \d\d:\d\d$/);
  });

  it('取得している間は読み込み中を出し、届いたら消える', async () => {
    const gate = deferred<void>();
    const server = threadsServer([thread('t1', '最初の質問')], {
      messages: { t1: [{ role: 'user', text: '昔の質問' }] },
      gate: { t1: gate.promise },
    });
    installFetch(() => replyResponse(), server.handler);
    const { user } = setup(fakeAuth());

    await user.click(await screen.findByTitle('最初の質問'));
    expect(await screen.findByTestId('history-loading')).toBeInTheDocument();
    gate.resolve();

    expect(await screen.findByText('昔の質問')).toBeInTheDocument();
    expect(screen.queryByTestId('history-loading')).not.toBeInTheDocument();
  });

  it('取得に失敗したら、固定メッセージを出し、続きは送れる', async () => {
    const server = threadsServer([thread('t1', '最初の質問')], { failMessages: true });
    const { chatBodies } = installFetch(() => replyResponse({ thread_id: 't1' }), server.handler);
    const { user } = setup(fakeAuth());

    await user.click(await screen.findByTitle('最初の質問'));

    expect(await screen.findByTestId('history-error')).toHaveTextContent(MSG_HISTORY_FAILED);
    expect(screen.queryByText(/SECRET|internal/)).not.toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: 'メッセージ' }), '続きの質問');
    await user.click(screen.getByRole('button', { name: '送信' }));
    await screen.findByText(DEFAULT_REPLY);
    expect(chatBodies()[0]).toEqual({ message: '続きの質問', thread_id: 't1' });
  });

  it('取得に失敗したら「再試行」と「新しい会話」を出し、再試行しても会話 ID は変わらない。取れたら履歴が並ぶ', async () => {
    let fail = true;
    const server = threadsServer([thread('t1', '最初の質問')], { messages: { t1: [{ role: 'user', text: '昔の質問' }] } });
    installFetch(
      () => replyResponse({ thread_id: 't1' }),
      (url, method, init) => (fail && /\/threads\/t1\/messages$/.test(url) ? errorResponse(503, '失敗') : server.handler(url, method, init)),
    );
    const { user } = setup(fakeAuth());
    await user.click(await screen.findByTitle('最初の質問'));

    const error = await screen.findByTestId('history-error');
    expect(within(error).getByRole('button', { name: '新しい会話' })).toBeInTheDocument();
    expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBe('t1');

    fail = false;
    await user.click(within(error).getByRole('button', { name: '再試行' }));

    expect(await screen.findByText('昔の質問')).toBeInTheDocument();
    expect(screen.queryByTestId('history-error')).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBe('t1');
  });

  it('取得に失敗したあとの「新しい会話」は、会話 ID を捨てて通常の最初の画面に戻る', async () => {
    const server = threadsServer([thread('t1', '最初の質問')], { failMessages: true });
    installFetch(() => replyResponse({ thread_id: 't1' }), server.handler);
    const { user } = setup(fakeAuth());
    await user.click(await screen.findByTitle('最初の質問'));

    await user.click(within(await screen.findByTestId('history-error')).getByRole('button', { name: '新しい会話' }));

    expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBeNull();
    expect(screen.queryByTestId('history-error')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '何を調べますか？' })).toBeInTheDocument();
  });

  it('再読み込みしたあとも、保存された会話の以前のやり取りを取得して並べる（案内文は出さない）', async () => {
    window.sessionStorage.setItem(THREAD_ID_STORAGE_KEY, 't1');
    const server = threadsServer([thread('t1', '最初の質問')], { messages: { t1: [{ role: 'user', text: '昔の質問' }, { role: 'assistant', text: '昔の回答' }] } });
    installFetch(() => replyResponse({ thread_id: 't1' }), server.handler);
    setup(fakeAuth());

    expect(await screen.findByText('昔の回答')).toBeInTheDocument();
    expect(screen.getByText('昔の質問')).toBeInTheDocument();
    expect(screen.queryByText(/前回の会話の続き/)).not.toBeInTheDocument();
    expect(server.log.filter((entry) => entry.url === '/threads/t1/messages')).toHaveLength(1);
  });

  it('別の会話に移ったあとに届いた、前の会話の発言は取り込まない', async () => {
    const slow = deferred<void>();
    const server = threadsServer([thread('t2', '二つ目の質問'), thread('t1', '最初の質問')], {
      messages: {
        t1: [{ role: 'user', text: '最初の会話の発言' }],
        t2: [{ role: 'user', text: '二つ目の会話の発言' }],
      },
      gate: { t1: slow.promise },
    });
    installFetch(() => replyResponse(), server.handler);
    const { user } = setup(fakeAuth());

    await user.click(await screen.findByTitle('最初の質問')); // 遅い
    await user.click(screen.getByTitle('二つ目の質問'));
    expect(await screen.findByText('二つ目の会話の発言')).toBeInTheDocument();
    slow.resolve();
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(screen.queryByText('最初の会話の発言')).not.toBeInTheDocument();
    expect(screen.getByText('二つ目の会話の発言')).toBeInTheDocument();
  });

  it('回答が届いたあと、新しい会話を一覧に反映する', async () => {
    const server = threadsServer([]);
    installFetch(() => replyResponse({ thread_id: 'fresh-1' }), server.handler);
    const { user } = setup(fakeAuth());
    await screen.findByText(MSG_NO_THREADS);

    server.set([thread('fresh-1', '新しい質問です')]);
    await user.type(screen.getByRole('textbox', { name: 'メッセージ' }), '新しい質問です');
    await user.click(screen.getByRole('button', { name: '送信' }));

    expect(await screen.findByTitle('新しい質問です')).toHaveAttribute('aria-current', 'true');
  });
});

describe('会話の削除', () => {
  it('確認してから削除し、一覧から消える', async () => {
    const server = threadsServer([thread('t2', '二つ目の質問'), thread('t1', '最初の質問')]);
    installFetch(() => replyResponse(), server.handler);
    const { user } = setup(fakeAuth());

    await user.click(await screen.findByRole('button', { name: '「二つ目の質問」を削除' }));
    expect(server.log.some((entry) => entry.method === 'DELETE')).toBe(false); // まだ消さない
    await user.click(screen.getByRole('button', { name: '削除する' }));

    await waitFor(() => expect(screen.queryByTitle('二つ目の質問')).not.toBeInTheDocument());
    expect(screen.getByTitle('最初の質問')).toBeInTheDocument();
    expect(server.log.find((entry) => entry.method === 'DELETE')).toMatchObject({
      url: '/threads/t2',
      authorization: 'Bearer tok-1',
    });
  });

  it('「やめる」を押せば、削除しない', async () => {
    const server = threadsServer([thread('t1', '最初の質問')]);
    installFetch(() => replyResponse(), server.handler);
    const { user } = setup(fakeAuth());

    await user.click(await screen.findByRole('button', { name: '「最初の質問」を削除' }));
    await user.click(screen.getByRole('button', { name: 'やめる' }));

    expect(screen.getByRole('button', { name: '「最初の質問」を削除' })).toBeInTheDocument();
    expect(server.log.some((entry) => entry.method === 'DELETE')).toBe(false);
  });

  it('いま開いている会話を消したら、画面も最初の状態に戻る', async () => {
    const server = threadsServer([thread('t1', '最初の質問')]);
    installFetch(() => replyResponse(), server.handler);
    const { user } = setup(fakeAuth());
    await user.click(await screen.findByTitle('最初の質問'));
    await waitFor(() => expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBe('t1'));

    await user.click(screen.getByRole('button', { name: '「最初の質問」を削除' }));
    await user.click(screen.getByRole('button', { name: '削除する' }));

    await waitFor(() => expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBeNull());
    expect(await screen.findByText(MSG_NO_THREADS)).toBeInTheDocument();
  });

  it('削除に失敗したら、固定メッセージを出し、会話は残す', async () => {
    const server = threadsServer([thread('t1', '最初の質問')], { failDelete: true });
    installFetch(() => replyResponse(), server.handler);
    const { user } = setup(fakeAuth());
    await user.click(await screen.findByTitle('最初の質問'));

    await user.click(screen.getByRole('button', { name: '「最初の質問」を削除' }));
    await user.click(screen.getByRole('button', { name: '削除する' }));

    expect(await within(await sidebar()).findByRole('alert')).toHaveTextContent('会話履歴を操作できませんでした');
    expect(screen.getByTitle('最初の質問')).toBeInTheDocument();
    expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBe('t1'); // 開いていた会話はそのまま
  });
});
