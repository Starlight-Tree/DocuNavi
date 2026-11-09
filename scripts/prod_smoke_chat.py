"""本番の /chat を、テスト専用ユーザーで 1 回だけ叩く（自動テスト用。Better Auth のメール + パスワード → JWT → /chat）。

前提: Worker `docunavi-api` で、自動テスト用ログインが有効（Secret `TEST_LOGIN_TOKEN` が設定されている間だけ。既定は無効）。
手順は docs/development.md の「本番の /chat の自動テスト」。秘密は環境変数で渡す（Infisical の `infisical sm run` など）。値は表示しない。

    TEST_LOGIN_EMAIL / TEST_LOGIN_PASSWORD / TEST_LOGIN_TOKEN   必須（パスワードは 24 文字以上）
    AUTH_WEB_ORIGIN   既定 https://docunavi-web.wataru-apps.workers.dev（Better Auth の公開オリジン）
    API_URL           既定 https://docunavi-xe42miq4da-an.a.run.app（Cloud Run の docunavi）

    python scripts/prod_smoke_chat.py                       # ログインして JWT を取るだけ（/chat は呼ばない）
    python scripts/prod_smoke_chat.py --send                # 質問を 1 回送る（本番の LLM・埋め込みを使う。課金される）
    python scripts/prod_smoke_chat.py --create --send       # 初回だけ: テストユーザーを登録してから送る（本番 DB に 1 行できる）
    python scripts/prod_smoke_chat.py --stream --question "…" [--expect-collections cafe]
        # /chat/stream で 1 回送り、出典の資料グループ（sources）と、回答の文末の引用 [n「語句」] を検査する（課金される）。
        # --expect-collections: 出典がそのグループだけであること（範囲の確認。違えば終了コード 1）。
        # API_URL にタグ付きの URL（0% のリビジョン）を渡すと、昇格の前に新しいリビジョンを試せる。

質問は 1 回だけ（繰り返しの上限は固定）。表示するのは、ステータス・秒数・件数・回答の冒頭だけ。トークン・Cookie・パスワードは出さない。
"""

import argparse
import base64
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_WEB = "https://docunavi-web.wataru-apps.workers.dev"
DEFAULT_API = "https://docunavi-xe42miq4da-an.a.run.app"
TIMEOUT = 60
USER_AGENT = "docunavi-prod-smoke/1.0 (+https://github.com/Starlight-Tree/docunavi)"


# 回答の文末の引用。[1] か [1「根拠の語句」]（docs/api.md）
MARKER = re.compile(r"\[(\d{1,3})(?:「([^」\n]{1,200})」)?\]")
# context の見出し。`[番号] ファイル名`
HEADER = re.compile(r"^\[(\d+)\] (.+)$", re.M)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def parse_stream(raw: str) -> tuple[str, dict, str | None]:
    """SSE 本文（`data: <JSON>` の行）から、回答の本文・message-metadata・エラー文を取り出す。"""
    text, meta, error = [], {}, None
    for line in raw.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            chunk = json.loads(line[6:])
        except ValueError:
            continue
        kind = chunk.get("type")
        if kind == "text-delta":
            text.append(chunk.get("delta", ""))
        elif kind == "message-metadata":
            meta.update(chunk.get("messageMetadata") or {})
        elif kind == "error":
            error = str(chunk.get("errorText") or "error")
    return "".join(text), meta, error


def split_context(context: str) -> dict[int, str]:
    """context（`[番号] ファイル名` の見出し + 抜粋）を、番号ごとの抜粋に分ける。"""
    found = list(HEADER.finditer(context))
    return {
        int(m.group(1)): context[m.end() : (found[i + 1].start() if i + 1 < len(found) else len(context))]
        for i, m in enumerate(found)
    }


def check_citations(text: str, context: str, source_count: int) -> list[tuple[int, str | None, str]]:
    """回答の引用ごとに (番号, 語句, 判定)。判定は 番号のみ / 原文どおり / 原文に無い / 番号が無い。
    画面と同じ規則（番号が実在し、語句が抜粋に空白を除いて含まれるときだけ残す）。"""
    excerpts = split_context(context)
    out = []
    for m in MARKER.finditer(text):
        n, span = int(m.group(1)), m.group(2)
        if n < 1 or n > source_count or n not in excerpts:
            verdict = "番号が無い"
        elif span is None:
            verdict = "番号のみ"
        else:
            verdict = "原文どおり" if _squash(span) in _squash(excerpts[n]) else "原文に無い"
        out.append((n, span, verdict))
    return out


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} が未設定です（値は表示しません）。")
    return value


def _request(opener, url: str, *, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    # Python 既定の User-Agent は、Cloudflare の前段に弾かれる（error code 1010）。用途が分かる名前を付ける
    merged = {"user-agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, data, merged, method="POST" if body is not None else "GET")
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _claims(jwt: str) -> dict:
    part = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _run_stream(opener, api: str, token: str, args) -> int:
    started = time.monotonic()
    status, raw = _request(
        opener,
        f"{api}/chat/stream",
        body={"message": args.question},
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
    )
    elapsed = time.monotonic() - started
    if status != 200:
        print(f"/chat/stream: {status} {elapsed:.2f}s")
        return 1
    text, meta, error = parse_stream(raw.decode("utf-8", "replace"))
    sources = meta.get("sources") or []
    print(
        f"/chat/stream: {status} {elapsed:.2f}s hit_count={meta.get('hit_count')} sources={len(sources)} error={error!r}"
    )
    for n, src in enumerate(sources, 1):
        print(f"  [{n}] {src.get('collection')}: {src.get('name')} p{src.get('page')}")
    print(f"  回答の冒頭: {text[:200]!r}")
    verdicts = check_citations(text, str(meta.get("context") or ""), len(sources))
    for n, span, verdict in verdicts:
        print(f"  引用 [{n}] {verdict}" + (f" 「{span}」" if span else ""))
    if not verdicts:
        print("  引用: なし")
    bad = [v for v in verdicts if v[2] in ("原文に無い", "番号が無い")]
    ok = error is None
    expected = {c.strip() for c in args.expect_collections.split(",") if c.strip()}
    if expected:
        outside = sorted({str(s.get("collection")) for s in sources} - expected)
        print(f"  範囲の確認: 期待 {sorted(expected)}、範囲外の出典 {outside or 'なし'}")
        ok = ok and not outside
    print(f"  画面が消す引用: {len(bad)} 件")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--create", action="store_true", help="サインインできなければ、テストユーザーを登録する")
    parser.add_argument("--send", action="store_true", help="/chat に質問を 1 回送る（実 API・課金）")
    parser.add_argument("--question", default="寮の門限は何時ですか？")
    parser.add_argument(
        "--stream", action="store_true", help="/chat/stream で 1 回送り、出典と引用を検査する（実 API・課金）"
    )
    parser.add_argument(
        "--expect-collections",
        default="",
        help="--stream で、出典がこの資料グループ（カンマ区切り）だけであることを確かめる",
    )
    args = parser.parse_args()

    email, password, gate = _env("TEST_LOGIN_EMAIL"), _env("TEST_LOGIN_PASSWORD"), _env("TEST_LOGIN_TOKEN")
    web = os.environ.get("AUTH_WEB_ORIGIN", DEFAULT_WEB).rstrip("/")
    api = os.environ.get("API_URL", DEFAULT_API).rstrip("/")
    if not (web.startswith("https://") and api.startswith("https://")):
        sys.exit("本番へは https だけです。")

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {"content-type": "application/json", "origin": web, "x-test-login-token": gate}
    creds = {"email": email, "password": password}

    status, _ = _request(opener, f"{web}/api/auth/sign-in/email", body=creds, headers=headers)
    if status == 401 and args.create:
        status, _ = _request(
            opener, f"{web}/api/auth/sign-up/email", body={**creds, "name": "e2e-test"}, headers=headers
        )
        print(f"sign-up: {status}")
        status, _ = _request(opener, f"{web}/api/auth/sign-in/email", body=creds, headers=headers)
    print(f"sign-in: {status}")
    if status != 200:
        print(
            "サインインできません（403 = 無効かトークン違い、400 = テスト用ログインが無効、401 = ユーザー無し/パスワード違い）。"
        )
        return 1

    status, raw = _request(opener, f"{web}/api/auth/token", headers={"origin": web})
    token = json.loads(raw).get("token", "") if status == 200 else ""
    if not token:
        print(f"JWT を取得できません: {status}")
        return 1
    claims = _claims(token)
    print(
        f"jwt: aud={claims.get('aud')} 有効 {claims.get('exp', 0) - claims.get('iat', 0)} 秒 sub={'あり' if claims.get('sub') else 'なし'}"
    )

    if args.stream:
        return _run_stream(opener, api, token, args)

    if not args.send:
        print("（/chat は呼んでいません。--send で 1 回送ります）")
        return 0

    started = time.monotonic()
    status, raw = _request(
        opener,
        f"{api}/chat",
        body={"message": args.question},
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
    )
    elapsed = time.monotonic() - started
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}
    reply = str(payload.get("reply") or payload.get("detail") or "")[:120]
    print(f"/chat: {status} {elapsed:.2f}s hit_count={payload.get('hit_count')} reply={reply!r}")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
