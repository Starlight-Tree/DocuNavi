import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import { THREAD_ID_STORAGE_KEY } from './lib/storage';
import { chatBody, deferred, errorResponse, installFetch, jsonResponse } from './test/helpers';

const NOT_FOUND = '該当する設定資料は見つかりませんでした。別のキーワードで再検索してください。';

function setup() {
  const user = userEvent.setup();
  const { unmount } = render(<App />);
  const input = screen.getByRole('textbox', { name: 'メッセージ' });
  const sendButton = screen.getByRole('button', { name: '送信' });
  return { user, input, sendButton, unmount };
}

async function sendMessage(
  user: ReturnType<typeof userEvent.setup>,
  text: string,
): Promise<void> {
  await user.type(screen.getByRole('textbox', { name: 'メッセージ' }), text);
  await user.click(screen.getByRole('button', { name: '送信' }));
}

describe('チャット UI: 送受信', () => {
  it('送信すると自分の発言と返答がログに並び、入力欄は空になる', async () => {
    installFetch(() => jsonResponse(chatBody({ reply: '大気は二酸化炭素が主成分です。' })));
    const { user, input } = setup();

    await sendMessage(user, 'ザーハードの大気は？');

    const log = screen.getByRole('log');
    expect(await within(log).findByText('大気は二酸化炭素が主成分です。')).toBeInTheDocument();
    expect(within(log).getByText('ザーハードの大気は？')).toBeInTheDocument();
    expect(input).toHaveValue('');
  });

  it('送信ボタンで送ったあとも、フォーカスは入力欄に戻る', async () => {
    installFetch(() => jsonResponse(chatBody()));
    const { user, input } = setup();

    await user.type(input, '質問');
    await user.click(screen.getByRole('button', { name: '送信' }));

    await screen.findByText(chatBody().reply);
    expect(input).toHaveFocus();
  });

  it('前後の空白は削って送る', async () => {
    const { chatBodies } = installFetch(() => jsonResponse(chatBody()));
    const { user } = setup();

    await sendMessage(user, '   こんにちは  ');

    await screen.findByText(chatBody().reply);
    expect(chatBodies()[0]).toEqual({ message: 'こんにちは' });
  });

  it('空・空白だけのときは送信できない', async () => {
    const { fetchMock } = installFetch(() => jsonResponse(chatBody()));
    const { user, input, sendButton } = setup();
    await waitFor(() => expect(screen.getByText('モック')).toBeInTheDocument());
    fetchMock.mockClear();

    expect(sendButton).toBeDisabled();
    await user.type(input, '   ');
    expect(sendButton).toBeDisabled();
    await user.keyboard('{Enter}');

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
  });

  it('2 回の送信で同じ thread_id を使う（最初は送らない）', async () => {
    const { chatBodies } = installFetch((_body, call) =>
      jsonResponse(chatBody({ thread_id: 'srv-thread-42', reply: `返答${call}` })),
    );
    const { user } = setup();

    await sendMessage(user, '一つ目');
    await screen.findByText('返答1');
    await sendMessage(user, '二つ目');
    await screen.findByText('返答2');

    const [first, second] = chatBodies();
    expect(first).toEqual({ message: '一つ目' });
    expect(second).toEqual({ message: '二つ目', thread_id: 'srv-thread-42' });
  });

  it('返答は HTML として解釈せず、改行を保ったテキストで表示する', async () => {
    const reply = '<img src=x onerror="alert(1)">\n二行目';
    installFetch(() => jsonResponse(chatBody({ reply })));
    const { user } = setup();

    await sendMessage(user, 'x');

    const text = await screen.findByText((_, element) => element?.textContent === reply);
    expect(text.tagName).toBe('P');
    expect(text).toHaveClass('message-text');
    expect(document.querySelector('img')).toBeNull();
  });

  it('動作モードをバッジで表示する', async () => {
    installFetch(() => jsonResponse(chatBody()));
    setup();
    expect(await screen.findByText('モック')).toBeInTheDocument();
  });
});

describe('チャット UI: 入力', () => {
  it('IME 変換中の Enter（isComposing / keyCode 229）では送信しない', async () => {
    const { fetchMock } = installFetch(() => jsonResponse(chatBody()));
    const { user, input } = setup();
    await user.type(input, 'にほんご');
    fetchMock.mockClear();

    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(input).toHaveValue('にほんご');

    // 確定後の通常の Enter は送信する
    await user.keyboard('{Enter}');
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await screen.findByText(chatBody().reply);
  });

  it('Shift+Enter は改行で、送信しない', async () => {
    const { fetchMock } = installFetch(() => jsonResponse(chatBody()));
    const { user, input } = setup();
    await user.type(input, '一行目');
    fetchMock.mockClear();

    await user.keyboard('{Shift>}{Enter}{/Shift}');
    await user.type(input, '二行目');

    expect(input).toHaveValue('一行目\n二行目');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('文字数カウンタが入力に追従し、2000 文字で入力を止める', async () => {
    installFetch(() => jsonResponse(chatBody()));
    const { user, input } = setup();

    expect(input).toHaveAttribute('maxlength', '2000');
    expect(screen.getByText('0 / 2000')).toBeInTheDocument();
    await user.type(input, 'あいう');
    expect(screen.getByText('3 / 2000')).toBeInTheDocument();
  });
});

describe('チャット UI: 送信中', () => {
  it('送信中は待機表示を出し、2 通目を送れない（入力は残る）', async () => {
    const gate = deferred<Response>();
    const { fetchMock, chatBodies } = installFetch((_body, call) =>
      call === 1 ? gate.promise : jsonResponse(chatBody({ reply: '二通目の返答' })),
    );
    const { user, input, sendButton } = setup();

    await sendMessage(user, '一通目');
    expect(await screen.findByText('回答を作成しています…')).toBeInTheDocument();
    expect(sendButton).toBeDisabled();

    await user.type(input, '二通目');
    await user.keyboard('{Enter}');
    await user.click(sendButton);

    expect(chatBodies()).toHaveLength(1);
    expect(input).toHaveValue('二通目');
    expect(fetchMock.mock.calls.filter(([url]) => String(url) === '/chat')).toHaveLength(1);

    gate.resolve(jsonResponse(chatBody({ reply: '一通目の返答', thread_id: 'th-1' })));
    await screen.findByText('一通目の返答');
    expect(screen.queryByText('回答を作成しています…')).not.toBeInTheDocument();

    // 待機が終われば、残っていた入力をそのまま送れる
    await user.click(screen.getByRole('button', { name: '送信' }));
    await screen.findByText('二通目の返答');
    expect(chatBodies()[1]).toEqual({ message: '二通目', thread_id: 'th-1' });
  });
});

describe('チャット UI: 二重送信', () => {
  it('再描画を待たずに Enter が 2 回届いても、送信は 1 回だけ', async () => {
    const gate = deferred<Response>();
    const { chatBodies } = installFetch(() => gate.promise);
    const { user, input } = setup();
    await user.type(input, '連打');

    act(() => {
      fireEvent.keyDown(input, { key: 'Enter' });
      fireEvent.keyDown(input, { key: 'Enter' });
    });

    expect(chatBodies()).toHaveLength(1);
    expect(screen.getAllByText('連打')).toHaveLength(1);
    gate.resolve(jsonResponse(chatBody()));
    await screen.findByText(chatBody().reply);
    expect(chatBodies()).toHaveLength(1);
  });
});

describe('チャット UI: エラー', () => {
  it('失敗してもユーザーの発言は残り、再試行で吹き出しを増やさず再送できる', async () => {
    const { chatBodies } = installFetch((_body, call) =>
      call === 1
        ? errorResponse(502, 'LLM 呼び出しに失敗しました。しばらく待ってから再試行してください。')
        : jsonResponse(chatBody({ reply: '再試行で成功しました' })),
    );
    const { user } = setup();

    await sendMessage(user, '壊れる質問');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('LLM 呼び出しに失敗しました。');
    expect(screen.getAllByText('壊れる質問')).toHaveLength(1);

    await user.click(within(alert).getByRole('button', { name: '再試行' }));

    await screen.findByText('再試行で成功しました');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getAllByText('壊れる質問')).toHaveLength(1);
    expect(chatBodies()).toHaveLength(2);
    expect(chatBodies()[1]).toEqual({ message: '壊れる質問' });
  });

  it('429 は待ち時間（秒）を案内する', async () => {
    installFetch(() =>
      errorResponse(429, 'リクエストが集中しています。しばらく待ってから再試行してください。', {
        'Retry-After': '30',
      }),
    );
    const { user } = setup();

    await sendMessage(user, '質問');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('リクエストが集中しています。');
    expect(alert).toHaveTextContent('約 30 秒');
  });

  it('422（配列 detail）は整形した文言で表示する', async () => {
    installFetch(() =>
      errorResponse(422, [
        { type: 'string_too_long', loc: ['body', 'message'], msg: 'too long', input: 'x' },
      ]),
    );
    const { user } = setup();

    await sendMessage(user, '質問');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('長すぎます');
    expect(alert).not.toHaveTextContent('string_too_long');
    // 同じ内容を送り直しても通らないので、再試行は出さない
    expect(within(alert).queryByRole('button', { name: '再試行' })).not.toBeInTheDocument();
  });

  it('ネットワーク失敗を表示する', async () => {
    installFetch(() => {
      throw new TypeError('Failed to fetch');
    });
    const { user } = setup();

    await sendMessage(user, '質問');

    expect(await screen.findByRole('alert')).toHaveTextContent('サーバーに接続できません');
  });

  it('次の送信を始めるとエラー表示は消える', async () => {
    installFetch((_body, call) =>
      call === 1 ? errorResponse(500, 'サーバー内部でエラーが発生しました。') : jsonResponse(chatBody()),
    );
    const { user } = setup();

    await sendMessage(user, '一つ目');
    await screen.findByRole('alert');
    await sendMessage(user, '二つ目');

    await screen.findByText(chatBody().reply);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('チャット UI: 参照した設定資料', () => {
  it('出典の見出しがある context だけ、閉じた <details> で表示する', async () => {
    installFetch(() =>
      jsonResponse(
        chatBody({
          context: '[ザーハード1.txt]\n大気の主成分は二酸化炭素。\n\n[ザーハード2.txt]\n探査団は三名。',
        }),
      ),
    );
    const { user } = setup();

    await sendMessage(user, '大気は？');

    const summary = await screen.findByText(/参照した設定資料/);
    const details = summary.closest('details');
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute('open');
    expect(within(details as HTMLElement).getByText('ザーハード1.txt')).toBeInTheDocument();
    expect(within(details as HTMLElement).getByText('ザーハード2.txt')).toBeInTheDocument();
    expect(
      within(details as HTMLElement).getByText('大気の主成分は二酸化炭素。'),
    ).toBeInTheDocument();

    await user.click(summary);
    expect(details).toHaveAttribute('open');
  });

  it('「該当なし」の定型文は出典として表示しない', async () => {
    installFetch(() => jsonResponse(chatBody({ context: NOT_FOUND })));
    const { user } = setup();

    await sendMessage(user, '無関係な質問');

    await screen.findByText(chatBody().reply);
    expect(screen.queryByText(/参照した設定資料/)).not.toBeInTheDocument();
    expect(screen.queryByText(NOT_FOUND)).not.toBeInTheDocument();
  });

  it('context が空なら出さない。出典はそのターンの返答にだけ付く', async () => {
    installFetch((_body, call) =>
      jsonResponse(
        chatBody({
          reply: `返答${call}`,
          context: call === 1 ? '[ザーハード1.txt]\n本文' : '',
        }),
      ),
    );
    const { user } = setup();

    await sendMessage(user, '一つ目');
    await screen.findByText('返答1');
    await sendMessage(user, '二つ目');
    await screen.findByText('返答2');

    expect(screen.getAllByText(/参照した設定資料/)).toHaveLength(1);
    const first = screen.getByText('返答1').closest('article') as HTMLElement;
    const second = screen.getByText('返答2').closest('article') as HTMLElement;
    expect(within(first).getByText(/参照した設定資料/)).toBeInTheDocument();
    expect(within(second).queryByText(/参照した設定資料/)).not.toBeInTheDocument();
  });
});

describe('チャット UI: 会話の管理', () => {
  it('「新しい会話」で表示と thread_id を破棄し、次は thread_id を送らない', async () => {
    const { chatBodies } = installFetch((_body, call) =>
      jsonResponse(chatBody({ thread_id: `th-${call}`, reply: `返答${call}` })),
    );
    const { user } = setup();

    await sendMessage(user, '一つ目');
    await screen.findByText('返答1');
    expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBe('th-1');

    await user.click(screen.getByRole('button', { name: '新しい会話' }));

    expect(screen.queryByText('一つ目')).not.toBeInTheDocument();
    expect(screen.queryByText('返答1')).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBeNull();

    await sendMessage(user, '二つ目');
    await screen.findByText('返答2');
    expect(chatBodies()[1]).toEqual({ message: '二つ目' });
  });

  it('送信中に「新しい会話」を押すと、遅れて届いた返答は表示しない', async () => {
    const gate = deferred<Response>();
    installFetch(() => gate.promise);
    const { user } = setup();

    await sendMessage(user, '古い会話の質問');
    await screen.findByText('回答を作成しています…');
    await user.click(screen.getByRole('button', { name: '新しい会話' }));

    expect(screen.queryByText('回答を作成しています…')).not.toBeInTheDocument();
    gate.resolve(jsonResponse(chatBody({ reply: '古い会話の返答', thread_id: 'old' })));
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(screen.queryByText('古い会話の返答')).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBeNull();
    // 送信中のまま固まらず、新しい会話の最初の発言を送れる
    await user.type(screen.getByRole('textbox', { name: 'メッセージ' }), '新しい質問');
    expect(screen.getByRole('button', { name: '送信' })).toBeEnabled();
  });

  it('thread_id を sessionStorage に保存し、再読み込み後も使う', async () => {
    const { chatBodies } = installFetch(() => jsonResponse(chatBody({ thread_id: 'keep-me' })));
    const first = setup();
    await sendMessage(first.user, '一つ目');
    await screen.findByText(chatBody().reply);
    expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBe('keep-me');

    // 同じタブでのページ再読み込みに相当する
    first.unmount();
    const second = setup();
    await sendMessage(second.user, '再読み込み後');
    await waitFor(() => expect(chatBodies()).toHaveLength(2));

    expect(chatBodies()[1]).toEqual({ message: '再読み込み後', thread_id: 'keep-me' });
  });

  it('thread_id を引き継いだが履歴が画面に無いときは、そのことを知らせる', async () => {
    installFetch(() => jsonResponse(chatBody({ thread_id: 'keep-me' })));
    const first = setup();
    await sendMessage(first.user, '一つ目');
    await screen.findByText(chatBody().reply);
    first.unmount();

    const second = setup();
    expect(screen.getByTestId('resumed-notice')).toHaveTextContent('前回の会話の続き');

    // 「新しい会話」で通常の案内に戻る
    await second.user.click(screen.getByRole('button', { name: '新しい会話' }));
    expect(screen.queryByTestId('resumed-notice')).not.toBeInTheDocument();
    expect(screen.getByText(/質問してみましょう/)).toBeInTheDocument();
  });

  it('新規の会話では「前回の続き」を出さない', () => {
    installFetch(() => jsonResponse(chatBody()));
    setup();

    expect(screen.queryByTestId('resumed-notice')).not.toBeInTheDocument();
    expect(screen.getByText(/質問してみましょう/)).toBeInTheDocument();
  });

  it('保存されていた thread_id が不正な形式なら使わない', async () => {
    window.sessionStorage.setItem(THREAD_ID_STORAGE_KEY, 'bad id!');
    const { chatBodies } = installFetch(() => jsonResponse(chatBody()));
    const { user } = setup();

    await sendMessage(user, '質問');
    await screen.findByText(chatBody().reply);

    expect(chatBodies()[0]).toEqual({ message: '質問' });
  });

  it('sessionStorage が使えなくても、会話は続けられる', async () => {
    vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => {
      throw new DOMException('The operation is insecure.', 'SecurityError');
    });
    const { chatBodies } = installFetch((_body, call) =>
      jsonResponse(chatBody({ thread_id: 'in-memory', reply: `返答${call}` })),
    );
    const { user } = setup();

    await sendMessage(user, '一つ目');
    await screen.findByText('返答1');
    await sendMessage(user, '二つ目');
    await screen.findByText('返答2');
    await user.click(screen.getByRole('button', { name: '新しい会話' }));

    expect(chatBodies()[1]).toEqual({ message: '二つ目', thread_id: 'in-memory' });
    expect(screen.queryByText('返答1')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
