import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import App from './App';
import { AuthProvider, MSG_SESSION_EXPIRED } from './auth/AuthContext';
import { THREAD_ID_STORAGE_KEY } from './lib/storage';
import { fakeAuth } from './test/auth';
import type { FakeAuth } from './test/auth';
import { DEFAULT_REPLY, errorResponse, installFetch, replyResponse } from './test/helpers';

const UNAUTHORIZED = 'ログインが必要です。ログインし直してください。';

function setup(auth: FakeAuth | null) {
  const user = userEvent.setup();
  render(
    <AuthProvider client={auth}>
      <App />
    </AuthProvider>,
  );
  return { user };
}

async function ask(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(await screen.findByRole('textbox', { name: 'メッセージ' }), text);
  await user.click(screen.getByRole('button', { name: '送信' }));
}

function authorization(init: RequestInit): string | null {
  return new Headers(init.headers).get('Authorization');
}

describe('ログイン: 画面', () => {
  it('ログインを使わない構成では、ログイン画面もログアウトも出さず、トークンも付けない', async () => {
    const { chatInits } = installFetch(() => replyResponse());
    const { user } = setup(null);

    await ask(user, '質問');

    await screen.findByText(DEFAULT_REPLY);
    expect(screen.queryByRole('button', { name: 'Google でログイン' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'ログアウト' })).not.toBeInTheDocument();
    expect(authorization(chatInits()[0]!)).toBeNull();
  });

  it('ログインしていなければログイン画面を出し、チャットは出さない', async () => {
    installFetch(() => replyResponse());
    setup(fakeAuth({ user: null }));

    expect(await screen.findByRole('button', { name: 'Google でログイン' })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'メッセージ' })).not.toBeInTheDocument();
  });

  it('「Google でログイン」で Google のログインを始める', async () => {
    installFetch(() => replyResponse());
    const auth = fakeAuth({ user: null });
    const { user } = setup(auth);

    await user.click(await screen.findByRole('button', { name: 'Google でログイン' }));

    expect(auth.signInWithGoogle).toHaveBeenCalledTimes(1);
  });

  it('ログインを始められなかったら、案内を出す', async () => {
    installFetch(() => replyResponse());
    const auth = fakeAuth({ user: null });
    auth.signInWithGoogle.mockRejectedValueOnce(new Error('secret detail'));
    const { user } = setup(auth);

    await user.click(await screen.findByRole('button', { name: 'Google でログイン' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('ログインを始められませんでした');
    expect(alert).not.toHaveTextContent('secret detail');
  });

  it('ログイン中は、メールアドレスとログアウトを出し、チャットの要求にトークンを付ける', async () => {
    const { chatInits } = installFetch(() => replyResponse());
    const { user } = setup(fakeAuth());

    expect(await screen.findByText('a@example.com')).toBeInTheDocument();
    await ask(user, '質問');

    await screen.findByText(DEFAULT_REPLY);
    expect(authorization(chatInits()[0]!)).toBe('Bearer tok-1');
  });

  it('起動直後（セッションの確認中）は、ログイン画面を一瞬出さずに待つ', async () => {
    installFetch(() => replyResponse());
    const auth = fakeAuth();
    let release!: () => void;
    auth.getUser.mockImplementationOnce(
      () => new Promise((resolve) => (release = () => resolve({ email: 'a@example.com' }))),
    );
    setup(auth);

    expect(screen.getByRole('status')).toHaveTextContent('読み込み中');
    expect(screen.queryByRole('button', { name: 'Google でログイン' })).not.toBeInTheDocument();
    release();
    expect(await screen.findByRole('textbox', { name: 'メッセージ' })).toBeInTheDocument();
  });

  it('ログアウトすると、端末のセッションと会話の ID を消してログイン画面に戻る', async () => {
    installFetch(() => replyResponse({ thread_id: 'thread-9' }));
    const auth = fakeAuth();
    const { user } = setup(auth);
    await ask(user, '質問');
    await screen.findByText(DEFAULT_REPLY);
    await waitFor(() => expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBe('thread-9'));

    await user.click(screen.getByRole('button', { name: 'ログアウト' }));

    expect(await screen.findByRole('button', { name: 'Google でログイン' })).toBeInTheDocument();
    expect(auth.signOut).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBeNull();
  });

  it('別のタブでログアウトされたら、ログイン画面に戻る', async () => {
    installFetch(() => replyResponse());
    const auth = fakeAuth();
    setup(auth);
    await screen.findByRole('textbox', { name: 'メッセージ' });

    auth.emit(null);

    expect(await screen.findByRole('button', { name: 'Google でログイン' })).toBeInTheDocument();
  });
});

describe('ログイン: トークンの更新', () => {
  it('401 でも、更新に成功すれば送り直して回答が届く（ログイン画面に戻らない）', async () => {
    const { chatInits } = installFetch((_body, call) =>
      call === 1 ? errorResponse(401, UNAUTHORIZED) : replyResponse(),
    );
    const auth = fakeAuth();
    const { user } = setup(auth);

    await ask(user, '質問');

    expect(await screen.findByText(DEFAULT_REPLY)).toBeInTheDocument();
    expect(auth.refreshAccessToken).toHaveBeenCalledTimes(1);
    expect(authorization(chatInits()[0]!)).toBe('Bearer tok-1');
    expect(authorization(chatInits()[1]!)).toBe('Bearer tok-2');
    expect(screen.queryByRole('button', { name: 'Google でログイン' })).not.toBeInTheDocument();
  });

  it('更新できなければ、期限切れの案内つきでログイン画面に戻る', async () => {
    installFetch(() => errorResponse(401, UNAUTHORIZED));
    const auth = fakeAuth({ refreshed: null });
    const { user } = setup(auth);

    await ask(user, '質問');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(MSG_SESSION_EXPIRED);
    expect(within(document.body).getByRole('button', { name: 'Google でログイン' })).toBeInTheDocument();
    expect(auth.signOut).toHaveBeenCalledTimes(1);
  });

  it('期限切れで戻ったときは、会話の ID を残す（同じ人が入り直せば続きを読める）', async () => {
    installFetch((_body, call) =>
      call === 1 ? replyResponse({ thread_id: 'thread-7' }) : errorResponse(401, UNAUTHORIZED),
    );
    const auth = fakeAuth({ refreshed: null });
    const { user } = setup(auth);
    await ask(user, '1 つ目');
    await screen.findByText(DEFAULT_REPLY);
    await waitFor(() => expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBe('thread-7'));

    await ask(user, '2 つ目');

    await screen.findByText(MSG_SESSION_EXPIRED);
    expect(window.sessionStorage.getItem(THREAD_ID_STORAGE_KEY)).toBe('thread-7');
  });
});
