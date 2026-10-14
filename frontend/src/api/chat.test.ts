import { describe, expect, it, vi } from 'vitest';
import {
  CHAT_STREAM_PATH,
  ChatApiError,
  chatFetch,
  endpoint,
  fetchHealth,
  isJapaneseMessage,
} from './chat';
import { errorResponse, jsonResponse, replyResponse } from '../test/helpers';

function stubFetch(impl: (url: string, init: RequestInit) => Response | Promise<Response>) {
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    impl(String(input), init ?? {}),
  );
  vi.stubGlobal('fetch', mock);
  return mock;
}

async function failure(promise: Promise<unknown>): Promise<ChatApiError> {
  const error = await promise.then(
    () => {
      throw new Error('エラーになるはずが成功した');
    },
    (caught: unknown) => caught,
  );
  expect(error).toBeInstanceOf(ChatApiError);
  return error as ChatApiError;
}

const post = () => chatFetch(endpoint(CHAT_STREAM_PATH), { method: 'POST', body: '{}' });

describe('endpoint', () => {
  it('既定は相対 URL', () => {
    expect(endpoint(CHAT_STREAM_PATH)).toBe('/chat/stream');
  });

  it('VITE_API_BASE_URL があればその先を呼ぶ（末尾のスラッシュは無視）', () => {
    vi.stubEnv('VITE_API_BASE_URL', 'https://api.example.test/');
    expect(endpoint(CHAT_STREAM_PATH)).toBe('https://api.example.test/chat/stream');
  });
});

describe('chatFetch: 成功', () => {
  it('SSE の応答はそのまま返す（本文の読み取りは AI SDK に任せる）', async () => {
    const fetchMock = stubFetch(() => replyResponse());

    const response = await chatFetch('/chat/stream', { method: 'POST', body: '{"message":"x"}' });

    expect(response.ok).toBe(true);
    expect(await response.text()).toContain('data: [DONE]');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/chat/stream');
    expect(init.method).toBe('POST');
  });
});

describe('chatFetch: エラー', () => {
  it('422（配列 detail）: 長すぎる', async () => {
    stubFetch(() =>
      errorResponse(422, [
        {
          type: 'string_too_long',
          loc: ['body', 'message'],
          msg: 'String should have at most 2000 characters',
          input: 'x',
        },
      ]),
    );
    const error = await failure(post());
    expect(error.status).toBe(422);
    expect(error.message).toContain('長すぎ');
    expect(error.message).toContain('2000');
    expect(error.message).not.toContain('[object Object]');
  });

  it('422: 空', async () => {
    stubFetch(() =>
      errorResponse(422, [{ type: 'string_too_short', loc: ['body', 'message'], msg: 'short' }]),
    );
    const error = await failure(post());
    expect(error.message).toContain('入力してください');
  });

  it('422: 会話 ID が不正', async () => {
    stubFetch(() =>
      errorResponse(422, [
        { type: 'string_pattern_mismatch', loc: ['body', 'thread_id'], msg: 'pattern' },
      ]),
    );
    const error = await failure(post());
    expect(error.message).toContain('会話 ID');
    expect(error.message).toContain('新しい会話');
  });

  it('422: 想定外の形でも一般的な文言になり、生の JSON は出さない', async () => {
    stubFetch(() => errorResponse(422, [{ type: 'whatever', loc: ['body', 'other'] }]));
    const error = await failure(post());
    expect(error.message).toContain('入力内容に問題');
    expect(error.message).not.toContain('{');
  });

  it('429: サーバーの文言と Retry-After（秒）を渡す', async () => {
    stubFetch(() =>
      errorResponse(429, 'リクエストが集中しています。しばらく待ってから再試行してください。', {
        'Retry-After': '30',
      }),
    );
    const error = await failure(post());
    expect(error.status).toBe(429);
    expect(error.message).toBe(
      'リクエストが集中しています。しばらく待ってから再試行してください。',
    );
    expect(error.retryAfterSeconds).toBe(30);
  });

  it('429: Retry-After が無い・整数でなければ undefined', async () => {
    stubFetch(() => errorResponse(429, '集中しています'));
    expect((await failure(post())).retryAfterSeconds).toBeUndefined();

    stubFetch(() =>
      errorResponse(429, '集中しています', { 'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT' }),
    );
    expect((await failure(post())).retryAfterSeconds).toBeUndefined();
  });

  it.each([
    [504, 'LLM の応答がタイムアウトしました。しばらく待ってから再試行してください。'],
    [500, 'サーバー側の LLM 設定に問題があります。管理者に連絡してください。'],
    [502, 'LLM 呼び出しに失敗しました。しばらく待ってから再試行してください。'],
    [503, '会話履歴の保存先に接続できませんでした。しばらく待ってから再試行してください。'],
    [500, 'サーバー内部でエラーが発生しました。'],
  ])('%i: サーバーの detail をそのまま使う', async (status, detail) => {
    stubFetch(() => errorResponse(status, detail));
    const error = await failure(post());
    expect(error.status).toBe(status);
    expect(error.message).toBe(detail);
    expect(error.retryAfterSeconds).toBeUndefined();
  });

  it.each([
    [404, 'Not Found'],
    [405, 'Method Not Allowed'],
    [502, 'Bad Gateway'],
    [503, 'Service Unavailable'],
  ])('%i: 英語の detail（FastAPI 既定・ゲートウェイ）は見せず、ステータス別の文言にする', async (status, detail) => {
    stubFetch(() => errorResponse(status, detail));
    const error = await failure(post());
    expect(error.status).toBe(status);
    expect(error.message).toContain(`HTTP ${status}`);
    expect(error.message).not.toContain(detail);
  });

  it('日本語の detail でも、バックエンドが意図して返さないステータスなら使わない', async () => {
    stubFetch(() => errorResponse(404, '見つかりません'));
    const error = await failure(post());
    expect(error.message).toContain('HTTP 404');
  });

  it('ネットワーク失敗（fetch が reject）', async () => {
    stubFetch(() => {
      throw new TypeError('Failed to fetch');
    });
    const error = await failure(post());
    expect(error.status).toBe(0);
    expect(error.message).toContain('サーバーに接続できません');
  });

  it('JSON でない本文（プロキシの HTML エラーページ）はステータス別の文言にする', async () => {
    stubFetch(
      () =>
        new Response('<html><body>Bad Gateway</body></html>', {
          status: 502,
          headers: { 'Content-Type': 'text/html' },
        }),
    );
    const error = await failure(post());
    expect(error.status).toBe(502);
    expect(error.message).toContain('HTTP 502');
    expect(error.message).not.toContain('<html>');
  });

  it('200 でも SSE ではない応答（SPA の index.html など）は想定外としてエラーにする', async () => {
    stubFetch(
      () =>
        new Response('<!doctype html>', { status: 200, headers: { 'Content-Type': 'text/html' } }),
    );
    const error = await failure(post());
    expect(error.message).toContain('想定外');
  });

  it('200 の JSON（旧 /chat の形）も、SSE ではないので想定外にする', async () => {
    stubFetch(() => jsonResponse({ reply: 'r', thread_id: 't', context: '', mode: 'mock' }));
    const error = await failure(post());
    expect(error.message).toContain('想定外');
  });
});

describe('chatFetch: 中断', () => {
  it('AbortSignal を fetch に渡し、中断は ChatApiError にせず AbortError のまま伝える', async () => {
    const controller = new AbortController();
    const fetchMock = stubFetch(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () =>
            reject(new DOMException('aborted', 'AbortError')),
          );
        }),
    );

    const pending = chatFetch('/chat/stream', { method: 'POST', signal: controller.signal });
    const settled = pending.then(
      () => null,
      (caught: unknown) => caught,
    );
    controller.abort();
    const error = await settled;

    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).signal).toBe(controller.signal);
    expect(error).toBeInstanceOf(DOMException);
    expect(error).not.toBeInstanceOf(ChatApiError);
    expect((error as DOMException).name).toBe('AbortError');
  });
});

describe('isJapaneseMessage', () => {
  it('かな・漢字を含む文だけを、サーバーの固定メッセージとして扱う', () => {
    expect(isJapaneseMessage('LLM 呼び出しに失敗しました。')).toBe(true);
    expect(isJapaneseMessage('Internal Server Error')).toBe(false);
    expect(isJapaneseMessage('')).toBe(false);
  });
});

describe('fetchHealth', () => {
  it('GET /health の status と mode を返す', async () => {
    const fetchMock = stubFetch(() => jsonResponse({ status: 'ok', mode: 'gemini' }));
    await expect(fetchHealth()).resolves.toEqual({ status: 'ok', mode: 'gemini' });
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/health');
  });

  it('失敗時は ChatApiError', async () => {
    stubFetch(() => new Response('', { status: 503 }));
    await failure(fetchHealth());
  });
});
