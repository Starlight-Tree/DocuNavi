// PDF（API の GET /documents/{id}/file）を、この画面と同じオリジンから iframe に渡す中継（ADR 0013）。
// <iframe src> は Authorization を送れず、Blob URL では内蔵ビューアの保存名が UUID になる。同じオリジンの URL にして、
// Content-Disposition の filename*（資料名）を効かせる。
// 流れ: ブラウザーの Cookie だけで /api/auth/token の JWT を取り、API には Bearer だけで GET し、本文は読まずにそのまま返す。
// 権限は API が毎回決める（ここでは判断しない）。失敗時は API の本文を通さず、固定の日本語の HTML を返す。
// 中継先 AUTH_PROXY_TARGET・FILE_PROXY_TARGET はビルド時に埋め込む（next.config.ts の env）。FILE_PROXY_TARGET が空なら 404。
import { encodeRfc5987, pdfFileName } from '../../../../../src/lib/pdfFileName';

const ID_PATTERN = /^[1-9][0-9]{0,18}$/;

const MSG_UNAVAILABLE = 'PDFは利用できません。';
const MSG_EXPIRED = 'ログインの有効期限が切れました。ページを再読み込みしてください。';
const MSG_FORBIDDEN = 'この資料は見られません。';
const MSG_BUSY = 'サーバーが一時的に応答できません。しばらくしてから開き直してください。';

function failure(status: number, message: string): Response {
  const html = `<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>PDF</title></head><body style="font-family:sans-serif;padding:1rem"><p>${message}</p></body></html>`;
  return new Response(html, {
    status,
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' },
  });
}

/** Cookie だけで JWT を取る。認証なしの構成（AUTH_PROXY_TARGET が空）は null（Bearer を付けない）。失敗は Response。 */
async function fetchToken(request: Request, authTarget: string): Promise<string | null | Response> {
  if (!authTarget) return null;
  const cookie = request.headers.get('cookie');
  if (!cookie) return failure(401, MSG_EXPIRED);
  let response: Response;
  try {
    response = await fetch(`${authTarget}/api/auth/token`, { headers: { Cookie: cookie, Accept: 'application/json' }, redirect: 'manual' });
  } catch {
    return failure(502, MSG_BUSY);
  }
  if (response.status === 401 || response.status === 403) return failure(401, MSG_EXPIRED);
  if (!response.ok) return failure(502, MSG_BUSY);
  try {
    const body: unknown = await response.json();
    const token = typeof body === 'object' && body !== null ? (body as { token?: unknown }).token : undefined;
    return typeof token === 'string' && token ? token : failure(401, MSG_EXPIRED);
  } catch {
    return failure(502, MSG_BUSY);
  }
}

export async function GET(request: Request, context: { params: Promise<{ id: string }> }): Promise<Response> {
  const fileTarget = (process.env.FILE_PROXY_TARGET ?? '').replace(/\/+$/, '');
  const authTarget = (process.env.AUTH_PROXY_TARGET ?? '').replace(/\/+$/, '');
  if (!fileTarget) return new Response('Not Found', { status: 404 });
  // 別サイトからの埋め込み・リンクでは、Cookie を使わせない（ヘッダーの無い古いブラウザーは通す）
  if (request.headers.get('sec-fetch-site') === 'cross-site') return failure(403, MSG_FORBIDDEN);

  const { id } = await context.params;
  if (!ID_PATTERN.test(id)) return failure(404, MSG_UNAVAILABLE);

  const token = await fetchToken(request, authTarget);
  if (token instanceof Response) return token;

  const headers: Record<string, string> = { Accept: 'application/pdf' };
  if (token) headers.Authorization = `Bearer ${token}`;
  let upstream: Response;
  try {
    upstream = await fetch(`${fileTarget}/documents/${id}/file`, { headers, redirect: 'manual' });
  } catch {
    return failure(502, MSG_BUSY);
  }
  if (upstream.status === 404 || upstream.status === 409) return failure(404, MSG_UNAVAILABLE);
  if (upstream.status === 401) return failure(401, MSG_EXPIRED);
  if (upstream.status === 403) return failure(403, MSG_FORBIDDEN);
  if (upstream.status !== 200 || !(upstream.headers.get('content-type') ?? '').includes('application/pdf')) {
    // 本文は読まずに捨てる（接続を解放する）
    void upstream.body?.cancel().catch(() => undefined);
    return failure(502, MSG_BUSY);
  }

  const name = pdfFileName(new URL(request.url).searchParams.get('name') ?? '');
  return new Response(upstream.body, {
    status: 200,
    headers: {
      'Content-Type': 'application/pdf',
      'Content-Disposition': `inline; filename="document-${id}.pdf"; filename*=UTF-8''${encodeRfc5987(name)}`,
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
    },
  });
}
