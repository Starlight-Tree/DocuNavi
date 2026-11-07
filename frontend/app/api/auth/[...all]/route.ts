// 認証の API（データ用 Worker の /api/auth/*）を、この画面と同じオリジンから使えるようにする中継。
// next.config.ts の rewrites は 302 をサーバー内でたどり、Set-Cookie をブラウザーへ渡さない（ログインしても未ログインの画面に戻る）。
// そのため、ここで redirect: 'manual' にして、ステータス・Location・Set-Cookie をそのまま返す。
// 中継先 AUTH_PROXY_TARGET はビルド時に埋め込む（next.config.ts の env）。未設定なら 404。

const target = process.env.AUTH_PROXY_TARGET?.replace(/\/+$/, '');

async function proxy(request: Request): Promise<Response> {
  if (!target) return new Response('Not Found', { status: 404 });
  const url = new URL(request.url);
  const headers = new Headers(request.headers);
  headers.delete('host');
  const hasBody = request.method !== 'GET' && request.method !== 'HEAD';
  const upstream = await fetch(`${target}${url.pathname}${url.search}`, {
    method: request.method,
    headers,
    body: hasBody ? request.body : undefined,
    redirect: 'manual',
    // @ts-expect-error Node/Workers の fetch は本文をストリームで送るとき duplex が要る（DOM の型には無い）
    duplex: hasBody ? 'half' : undefined,
  });
  const out = new Headers(upstream.headers);
  // fetch は本文を展開済みなので、元の圧縮・長さの情報を持ち越さない
  out.delete('content-encoding');
  out.delete('content-length');
  return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: out });
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE, proxy as OPTIONS, proxy as HEAD };
