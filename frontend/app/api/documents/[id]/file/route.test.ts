import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const FILE = 'https://api.example.test';
const AUTH = 'https://auth.example.test';
const PDF = '%PDF-1.4 test';

type Call = { url: string; init: RequestInit };
let calls: Call[];

function stubFetch(handlers: { token?: () => Response; file?: () => Response }) {
  calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      calls.push({ url, init });
      if (url === `${AUTH}/api/auth/token`) return (handlers.token ?? (() => Response.json({ token: 'jwt-1' })))();
      if (url.startsWith(`${FILE}/documents/`)) return (handlers.file ?? (() => pdfResponse()))();
      throw new Error(`想定外の取得: ${url}`);
    }),
  );
}

const pdfResponse = () => new Response(PDF, { status: 200, headers: { 'Content-Type': 'application/pdf', 'Set-Cookie': 'x=1', 'Content-Disposition': 'inline; filename="from-api.pdf"' } });

async function get(id: string, { query = '', headers = { cookie: 'session=abc' } }: { query?: string; headers?: Record<string, string> } = {}) {
  vi.resetModules();
  const { GET } = await import('./route');
  return GET(new Request(`https://web.example.test/api/documents/${id}/file${query}`, { headers }), { params: Promise.resolve({ id }) });
}

beforeEach(() => {
  vi.stubEnv('FILE_PROXY_TARGET', `${FILE}/`);
  vi.stubEnv('AUTH_PROXY_TARGET', `${AUTH}/`);
});
afterEach(() => vi.unstubAllGlobals());

describe('PDF の中継（GET /api/documents/{id}/file）', () => {
  it('Cookie だけで JWT を取り、API には Bearer だけを付ける。本文はそのまま、ヘッダーは作り直す', async () => {
    stubFetch({});

    const response = await get('7', { query: `?name=${encodeURIComponent('規程 第1版.pdf')}`, headers: { cookie: 'session=abc', authorization: 'Bearer evil', 'x-other': '1' } });

    expect(response.status).toBe(200);
    expect(await response.text()).toBe(PDF);
    expect(response.headers.get('Content-Type')).toBe('application/pdf');
    expect(response.headers.get('Content-Disposition')).toBe(`inline; filename="document-7.pdf"; filename*=UTF-8''${encodeURIComponent('規程 第1版.pdf')}`);
    expect(response.headers.get('Cache-Control')).toBe('no-store');
    expect(response.headers.get('X-Content-Type-Options')).toBe('nosniff');
    expect(response.headers.get('Set-Cookie')).toBeNull();
    expect(calls.map((c) => c.url)).toEqual([`${AUTH}/api/auth/token`, `${FILE}/documents/7/file`]);
    const tokenHeaders = new Headers(calls[0]!.init.headers);
    expect(tokenHeaders.get('Cookie')).toBe('session=abc');
    expect(tokenHeaders.get('Authorization')).toBeNull();
    const fileHeaders = new Headers(calls[1]!.init.headers);
    expect(fileHeaders.get('Authorization')).toBe('Bearer jwt-1');
    expect(fileHeaders.get('Cookie')).toBeNull();
    expect(fileHeaders.get('Range')).toBeNull();
    expect(fileHeaders.get('X-Other')).toBeNull();
  });

  it('保存名は ?name= を整え直す。空・長すぎ・禁止文字・引用符・改行でも、ヘッダーを壊さない', async () => {
    stubFetch({});
    const disposition = async (name: string) => (await get('7', { query: `?name=${encodeURIComponent(name)}` })).headers.get('Content-Disposition');

    expect(await disposition('')).toBe(`inline; filename="document-7.pdf"; filename*=UTF-8''document.pdf`);
    expect(await disposition('a/b"c\r\nSet-Cookie: x')).toBe(`inline; filename="document-7.pdf"; filename*=UTF-8''a_b_c__Set-Cookie_%20x.pdf`);
    expect(await disposition('あ'.repeat(300))).toBe(`inline; filename="document-7.pdf"; filename*=UTF-8''${encodeURIComponent('あ'.repeat(100))}.pdf`);
    const none = await get('7');
    expect(none.headers.get('Content-Disposition')).toContain("filename*=UTF-8''document.pdf");
  });

  it('id が数字でなければ、API を呼ばず 404（固定の HTML）', async () => {
    stubFetch({});
    for (const id of ['abc', '0', '01', '1/../2', '9'.repeat(20)]) {
      const response = await get(id);
      expect(response.status).toBe(404);
      expect(await response.text()).toContain('PDFは利用できません。');
    }
    expect(calls).toEqual([]);
  });

  it('FILE_PROXY_TARGET が空なら 404（何も呼ばない）', async () => {
    vi.stubEnv('FILE_PROXY_TARGET', '');
    stubFetch({});
    expect((await get('7')).status).toBe(404);
    expect(calls).toEqual([]);
  });

  it('別サイトからの要求（Sec-Fetch-Site: cross-site）は 403。同一オリジン・ヘッダー無しは通す', async () => {
    stubFetch({});
    expect((await get('7', { headers: { cookie: 'session=abc', 'sec-fetch-site': 'cross-site' } })).status).toBe(403);
    expect(calls).toEqual([]);
    expect((await get('7', { headers: { cookie: 'session=abc', 'sec-fetch-site': 'same-origin' } })).status).toBe(200);
  });

  it('Cookie が無い・トークンが取れない・期限切れは 401（固定の文）', async () => {
    stubFetch({});
    expect((await get('7', { headers: {} })).status).toBe(401);
    stubFetch({ token: () => new Response('no', { status: 401 }) });
    expect((await get('7')).status).toBe(401);
    stubFetch({ token: () => Response.json({}) });
    expect((await get('7')).status).toBe(401);
    expect(calls.map((c) => c.url)).toEqual([`${AUTH}/api/auth/token`]); // API は呼ばない
  });

  it('トークンの取得が壊れていれば 502', async () => {
    stubFetch({ token: () => new Response('boom', { status: 500 }) });
    expect((await get('7')).status).toBe(502);
    stubFetch({ token: () => new Response('not json', { status: 200 }) });
    expect((await get('7')).status).toBe(502);
  });

  it('API の失敗は、ステータスを対応づけ、API の本文を通さず、固定の日本語の HTML にする', async () => {
    const secret = 'SECRET-DETAIL /srv/app/x.py';
    const cases: Array<[number, number, string]> = [
      [404, 404, 'PDFは利用できません。'],
      [409, 404, 'PDFは利用できません。'],
      [401, 401, 'ログインの有効期限が切れました。'],
      [403, 403, 'この資料は見られません。'],
      [500, 502, 'サーバーが一時的に応答できません。'],
      [503, 502, 'サーバーが一時的に応答できません。'],
    ];
    for (const [upstream, expected, message] of cases) {
      stubFetch({ file: () => new Response(secret, { status: upstream }) });
      const response = await get('7');
      const body = await response.text();
      expect(response.status).toBe(expected);
      expect(response.headers.get('Content-Type')).toContain('text/html');
      expect(response.headers.get('Cache-Control')).toBe('no-store');
      expect(body).toContain(message);
      expect(body).not.toContain('SECRET');
      expect(body).not.toContain('jwt-1');
    }
  });

  it('API が接続できない・PDF でない 200 を返したら 502', async () => {
    stubFetch({ file: () => { throw new Error('connect ECONNREFUSED 10.0.0.1'); } });
    const down = await get('7');
    expect(down.status).toBe(502);
    expect(await down.text()).not.toContain('ECONNREFUSED');
    stubFetch({ file: () => Response.json({ detail: 'x' }) });
    expect((await get('7')).status).toBe(502);
  });

  it('認証なしの構成（AUTH_PROXY_TARGET が空）は、トークンを取らず Bearer も付けない', async () => {
    vi.stubEnv('AUTH_PROXY_TARGET', '');
    stubFetch({});

    const response = await get('7', { headers: {} });

    expect(response.status).toBe(200);
    expect(calls.map((c) => c.url)).toEqual([`${FILE}/documents/7/file`]);
    expect(new Headers(calls[0]!.init.headers).get('Authorization')).toBeNull();
  });
});
