#!/usr/bin/env python3
"""Infisical Agent Vault の設定（Bundle・Service・ルール・Proxy・付与）を、API で冪等に行う。

エージェントに Prisma のトークンを持たせず、Prisma の使用量 API だけを、プロキシ経由で呼べるようにする。

  Bundle   prisma-usage
  Service  prisma-usage（api.prisma.io、Bearer、GET のみ、パスは使用量 API の 1 本だけ）
  Proxy    docunavi-proxy（trafficPolicy = service-hosts。Service のホスト以外は 403）
  付与     Machine Identity `docunavi-agent` に Bundle を付与

秘密値は、このファイルにも、コマンド引数にも、ファイルにも、画面にも出さない。
  - 認証情報（Universal Auth の Machine Identity だけ）: 環境変数を優先し、無ければ macOS Keychain
    （service `docunavi-infisical`、account は下の名前）から読む。
      INFISICAL_UNIVERSAL_AUTH_CLIENT_ID / INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET
    Keychain への保存（値はプロンプトで入力。引数に書かない）:
      security add-generic-password -s docunavi-infisical -a INFISICAL_UNIVERSAL_AUTH_CLIENT_ID -w
      security add-generic-password -s docunavi-infisical -a INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET -w
  - Prisma の Service Token は扱わない。Service は「値なし」の Bearer で作る。値は、作成後にダッシュボードで利用者が入れる。
  - Proxy Token は、API の応答から取り、環境変数 INFISICAL_AGENT_VAULT_PROXY_TOKEN として docker run の子プロセスにだけ渡す
    （`-e NAME` の形なので argv に載らない）。出力にも、ファイルにも出さない。

使い方（ホストの、自分のターミナルで）:
  # 認証情報は Keychain（または環境変数）から読むので、毎回の入力は要らない
  python3 ops/setup_agent_vault.py --project-id <Agent Vault の Project ID>            # 計画だけ（何も変更しない）
  python3 ops/setup_agent_vault.py --project-id <ID> --apply                           # Bundle・Service・付与・Proxy の設定
  python3 ops/setup_agent_vault.py --project-id <ID> --apply --start-proxy             # さらに Proxy Token を発行し、ホストの Docker で起動

環境変数（任意）: INFISICAL_API_URL（既定 https://app.infisical.com）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("INFISICAL_API_URL", "https://app.infisical.com").rstrip("/")

BUNDLE = "prisma-usage"
SERVICE = "prisma-usage"
PROXY = "docunavi-proxy"
AGENT_IDENTITY = "docunavi-agent"
HOST = "api.prisma.io"
DEFAULT_DB_ID = "db_gn96qorjqcfu12zt2mm3gczo"
CONTAINER = "agent-vault-proxy"
PROXY_PORT = "127.0.0.1:17323:17323"
PROXY_TOKEN_ENV = "INFISICAL_AGENT_VAULT_PROXY_TOKEN"


class ApiError(RuntimeError):
    pass


def die(msg: str) -> None:
    print(f"エラー: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _request(method: str, path: str, token: str | None, body: dict | None = None, query: dict | None = None) -> dict:
    url = API + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "docunavi-ops-setup/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
    except urllib.error.HTTPError as e:
        # 応答の本文は検査メッセージ（送った値に秘密は無い）。先頭だけ出す。
        raw_err = ""
        try:
            raw_err = (e.read() or b"").decode("utf-8", "replace")[:600]
        except Exception:
            pass
        # 診断: トークンの値は出さない（長さ・Authorization の有無だけ）。
        print(
            f"[診断] {method} {url}\n"
            f"[診断] Authorization: {'Bearer <' + str(len(token)) + '文字>' if token else 'なし'}"
            f" / Content-Type: {'あり' if data is not None else 'なし'}\n"
            f"[診断] ステータス: HTTP {e.code}\n"
            f"[診断] 応答本文: {raw_err or '(空)'}",
            file=sys.stderr,
        )
        qs = ",".join(sorted(query)) if query else "-"
        raise ApiError(f"{method} {path} (query: {qs}) -> HTTP {e.code} {raw_err}".strip()) from None
    except urllib.error.URLError as e:
        raise ApiError(f"{method} {path} -> 接続できません: {e.reason}") from None
    return json.loads(raw) if raw else {}


KEYCHAIN_SERVICE = "docunavi-infisical"
CRED_NAMES = ("INFISICAL_UNIVERSAL_AUTH_CLIENT_ID", "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET")


def _credential(name: str) -> tuple[str, str]:
    """環境変数を優先し、無ければ macOS Keychain から読む。値は表示しない。(値, 出どころ) を返す。"""
    v = "".join(os.environ.get(name, "").split())
    if v:
        return v, "環境変数"
    if sys.platform == "darwin" and shutil.which("security"):
        # -w は値だけを標準出力に出す。子プロセスの出力として受け取るだけで、引数にも画面にも出さない。
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return "".join(r.stdout.split()), "Keychain"
    return "", ""


def login() -> str:
    """Universal Auth の Machine Identity でログインし、アクセストークンを返す（値は表示しない）。"""
    (cid, cid_src), (sec, sec_src) = (_credential(n) for n in CRED_NAMES)
    if not (cid and sec):
        missing = [n for n, v in zip(CRED_NAMES, (cid, sec)) if not v]
        cmds = "\n".join(
            f"  security add-generic-password -s {KEYCHAIN_SERVICE} -a {n} -w    # 値は、プロンプトで入力（引数に書かない）"
            for n in missing
        )
        die(
            "認証情報がありません（環境変数にも Keychain にも）: " + ", ".join(missing) + "\n"
            "Keychain に保存してください（ホストのターミナルで）:\n" + cmds + "\n"
            "（すでにある項目を直すときは、先に `security delete-generic-password -s "
            f"{KEYCHAIN_SERVICE} -a <名前>`）。`user get token` の出力は使いません"
        )
    print(
        f"[診断] 認証情報の出どころ: Client ID={cid_src} / Client Secret={sec_src}（値は表示しません）", file=sys.stderr
    )
    res = _request("POST", "/api/v1/auth/universal-auth/login", None, {"clientId": cid, "clientSecret": sec})
    tok = res.get("accessToken")
    if not tok:
        die("Universal Auth のログインに失敗しました（accessToken が無い）")
    print(f"[診断] ログイン成功。アクセストークン長 {len(tok)}（値は表示しません）", file=sys.stderr)
    # Machine Identity のトークンで、認証が通るかだけを確かめる（/users/me は人間のユーザー用なので使わない）。
    me = _request("GET", "/api/v1/identities/details", tok)
    ident = me.get("identity") or {}
    print(f"[診断] 認証 OK: identity 名={ident.get('name', '?')} / 組織 ID={ident.get('orgId', '?')}", file=sys.stderr)
    return tok


def service_payload(db_id: str) -> dict:
    return {
        "type": "http",
        "name": SERVICE,
        "hostPattern": HOST,
        "allowedMethods": ["GET"],
        # 区切りごとの前方一致。使用量 API の 1 本だけに絞る（クエリ startDate/endDate は path に含まれない）
        "allowedPathPrefixes": [f"/v1/databases/{db_id}/usage"],
        # 値なしの Bearer。Prisma の Service Token は、作成後にダッシュボードで利用者が入れる
        # 値は持たない。トークンは各メンバーが自分で入れる（API は「値なし」の bearer を 422 で拒む）。
        "credential": {"type": "bearer", "memberProvidedFields": ["credential-value"]},
    }


def find_bundle(tok: str, project_id: str) -> dict | None:
    res = _request("GET", "/api/v1/agent-vault/access-bundles", tok, query={"projectId": project_id})
    for b in res.get("accessBundles", []):
        if b.get("name") == BUNDLE:
            return b
    return None


def find_proxy(tok: str, project_id: str) -> dict | None:
    res = _request("GET", "/api/v1/agent-vault/proxies", tok, query={"projectId": project_id})
    for p in res.get("proxies", []):
        if p.get("name") == PROXY:
            return p
    return None


def find_agent_identity(tok: str, project_id: str) -> dict | None:
    res = _request(
        "GET", "/api/v1/agent-vault/members", tok, query={"projectId": project_id, "actorType": "machineIdentity"}
    )
    for m in res.get("members", []):
        if m.get("type") == "machineIdentity" and m.get("name") == AGENT_IDENTITY:
            return m
    return None


def start_proxy_container(proxy_token: str, domain: str | None) -> None:
    """Proxy Token を、環境変数だけで docker run に渡す（argv にもファイルにも出さない）。"""
    subprocess.run(["docker", "rm", "-f", CONTAINER], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        CONTAINER,
        "-p",
        PROXY_PORT,
        "-v",
        "agent-vault-proxy:/etc/infisical/agent-vault",
        "-e",
        PROXY_TOKEN_ENV,  # 値は付けない。呼び出し元の環境から取る
        "infisical/cli",
        "agent-vault",
        "proxy",
    ]
    if domain:
        cmd += ["--domain", domain]
    env = dict(os.environ)
    env[PROXY_TOKEN_ENV] = proxy_token
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        # docker の出力にはトークンは出ないが、念のため先頭だけ
        die("Proxy のコンテナを起動できませんでした: " + (r.stderr.strip().splitlines() or ["(出力なし)"])[0][:200])
    print(f"  Proxy のコンテナ `{CONTAINER}` を起動しました（{PROXY_PORT}）。Proxy Token は表示していません")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project-id", required=True, help="Agent Vault の Project ID（秘密ではない）")
    ap.add_argument("--database-id", default=DEFAULT_DB_ID, help="使用量 API の対象の DB ID")
    ap.add_argument("--apply", action="store_true", help="実際に変更する。省略時は計画だけ")
    ap.add_argument(
        "--start-proxy", action="store_true", help="Proxy Token を発行（既存なら再発行）し、ホストの Docker で起動する"
    )
    ap.add_argument("--domain", default=None, help="proxy に渡す --domain（US Cloud 以外のとき）")
    a = ap.parse_args()

    tok = login()
    mode = "適用" if a.apply else "計画のみ（変更しません）"
    print(f"モード: {mode} / 対象: Agent Vault Project {a.project_id}")

    # 1. Bundle
    bundle = find_bundle(tok, a.project_id)
    if bundle:
        print(f"[1] Bundle `{BUNDLE}`: 既にあります（{bundle['id']}）")
    elif a.apply:
        bundle = _request(
            "POST",
            "/api/v1/agent-vault/access-bundles",
            tok,
            {"name": BUNDLE, "description": "Prisma の使用量 API（GET のみ）", "projectId": a.project_id},
        )["accessBundle"]
        print(f"[1] Bundle `{BUNDLE}`: 作成しました（{bundle['id']}）")
    else:
        print(f"[1] Bundle `{BUNDLE}`: 作成します")

    # 2. Service とルール
    want = service_payload(a.database_id)
    if bundle:
        detail = _request("GET", f"/api/v1/agent-vault/access-bundles/{bundle['id']}", tok)["accessBundle"]
        have = next((s for s in detail.get("services", []) if s.get("name") == SERVICE), None)
        if have:
            drift = [k for k in ("hostPattern", "allowedMethods", "allowedPathPrefixes") if have.get(k) != want[k]]
            if drift and a.apply:
                _request(
                    "PATCH",
                    f"/api/v1/agent-vault/access-bundles/{bundle['id']}/services/{have['id']}",
                    tok,
                    {k: want[k] for k in drift},
                )
                print(f"[2] Service `{SERVICE}`: ルールを修正しました（{', '.join(drift)}）")
            elif drift:
                print(f"[2] Service `{SERVICE}`: ルールが違います（{', '.join(drift)}）。修正します")
            else:
                print(f"[2] Service `{SERVICE}`: 既にあり、ルールも一致しています")
        elif a.apply:
            _request("POST", f"/api/v1/agent-vault/access-bundles/{bundle['id']}/services", tok, want)
            print(
                f"[2] Service `{SERVICE}`: 作成しました（値なし）。Prisma の Service Token は、ダッシュボードで入力してください"
            )
        else:
            print(f"[2] Service `{SERVICE}`: 作成します（{HOST}、GET のみ、{want['allowedPathPrefixes'][0]}）")
    else:
        print(f"[2] Service `{SERVICE}`: Bundle の作成後に作成します")

    # 3. docunavi-agent への付与
    ident = find_agent_identity(tok, a.project_id)
    if not ident and a.apply:
        # Project 管理の Machine Identity を、この Project の中に作る（Project の Admin 権限で足りる。組織の Identity 作成権限は要らない）。
        # 認証方法（Universal Auth など）は付けない。エージェントは Agent Vault の Session で使うだけ。
        created = _request(
            "POST",
            f"/api/v1/projects/{a.project_id}/identities",
            tok,
            {"name": AGENT_IDENTITY, "roles": [{"role": "member"}]},
        )
        print(f"[3] Identity: `{AGENT_IDENTITY}` を、この Project の中に Member で作成しました")
        ident = find_agent_identity(tok, a.project_id)
        if not ident:
            die(
                f"`{AGENT_IDENTITY}` を作成しましたが、メンバー一覧に見つかりません。応答の形を確認してください: {sorted(created)}"
            )
    if not ident:
        print(
            f"[3] Identity: `{AGENT_IDENTITY}` が、この Project にいません。--apply で、Project の中に Member で作成します"
            "（Project の Admin 権限を使う。組織の Identity 作成権限は使わない）"
        )
    elif not bundle:
        print(f"[3] 付与: Bundle の作成後に `{AGENT_IDENTITY}` へ付与します")
    else:
        members = _request("GET", f"/api/v1/agent-vault/access-bundles/{bundle['id']}/members", tok).get("members", [])
        if any(m.get("id") == ident["id"] for m in members):
            print(f"[3] 付与: `{AGENT_IDENTITY}` には付与済みです")
        elif a.apply:
            _request(
                "POST",
                f"/api/v1/agent-vault/access-bundles/{bundle['id']}/members",
                tok,
                {"machineIdentityIds": [ident["id"]]},
            )
            print(f"[3] 付与: `{AGENT_IDENTITY}` に Bundle `{BUNDLE}` を付与しました")
        else:
            print(f"[3] 付与: `{AGENT_IDENTITY}` に Bundle `{BUNDLE}` を付与します")

    # 4. Proxy（service-hosts）
    proxy = find_proxy(tok, a.project_id)
    proxy_token: str | None = None
    if proxy:
        policy = proxy.get("trafficPolicy")
        if policy in ("service-hosts", "bundle-hosts"):
            print(f"[4] Proxy `{PROXY}`: 既にあり、ポリシーは {policy} です")
        elif a.apply:
            _request("PATCH", f"/api/v1/agent-vault/proxies/{proxy['id']}", tok, {"trafficPolicy": "service-hosts"})
            print(f"[4] Proxy `{PROXY}`: ポリシーを service-hosts にしました（それまで: {policy}）")
        else:
            print(f"[4] Proxy `{PROXY}`: ポリシーが {policy} です。service-hosts にします")
    elif a.apply:
        res = _request(
            "POST",
            "/api/v1/agent-vault/proxies",
            tok,
            {
                "projectId": a.project_id,
                "name": PROXY,
                "description": "ホストの Docker（127.0.0.1:17323）",
                "trafficPolicy": "service-hosts",
            },
        )
        proxy, proxy_token = res["proxy"], res.get("proxyToken")
        print(f"[4] Proxy `{PROXY}`: 作成しました（service-hosts）。Proxy Token は表示しません")
    else:
        print(f"[4] Proxy `{PROXY}`: 作成します（service-hosts）")

    # 5. Proxy の起動（Proxy Token は、環境変数で docker run にだけ渡す）
    if a.start_proxy:
        if not a.apply:
            print("[5] Proxy の起動: --apply と一緒に指定してください")
        else:
            if proxy_token is None:
                # 既存の Proxy。再発行すると、これまでのトークンは使えなくなる
                proxy_token = _request(
                    "POST", f"/api/v1/agent-vault/proxies/{proxy['id']}/token-auth/proxy-token", tok
                ).get("proxyToken")
                print("[5] Proxy Token を再発行しました（以前のトークンは無効）")
            if not proxy_token:
                die("Proxy Token を取得できませんでした")
            start_proxy_container(proxy_token, a.domain)
            proxy_token = None
    elif proxy_token:
        # 作成時にしか出ないトークンを、使わずに捨てることになる。次回 --start-proxy で再発行すればよい
        print(
            "[5] Proxy Token は使わずに破棄しました。起動は --start-proxy を付けて、もう一度実行してください（再発行します）"
        )

    print("完了" if a.apply else "計画は以上です。--apply を付けると変更します")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ApiError as e:
        die(str(e))
