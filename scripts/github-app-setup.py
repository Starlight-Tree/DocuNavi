#!/usr/bin/env python3
"""GitHub MCP 用の GitHub App を、manifest 方式で 1 回だけ作る（ホストで実行。標準ライブラリだけ）。

流れ: 手元（127.0.0.1:18791）に小さな受け口を立てる -> ブラウザーで GitHub の「Create GitHub App」を開く -> 利用者が押す
      -> 戻り先で受けた code を App の ID と秘密鍵に換える -> `.github-app.env`（Git 管理外、0600）に書く -> インストールの画面を開く。
秘密鍵は画面にも標準出力にも出さない。`.github-app.env` は、コンテナ側で Infisical に入れたあとに消す。

    python3 scripts/github-app-setup.py
"""

import base64
import json
import os
import secrets
import sys
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

OWNER = "Starlight-Tree"
REPO_URL = f"https://github.com/{OWNER}/docunavi"
APP_NAME = "docunavi-agent-actions"
PORT = int(
    os.environ.get("GITHUB_APP_SETUP_PORT", "18791")
)  # 8765 は、試験でコンテナの転送が残ることがあったので避ける
OUT = Path(__file__).resolve().parent.parent / ".github-app.env"

MANIFEST = {
    "name": APP_NAME,
    "url": REPO_URL,
    "redirect_url": f"http://127.0.0.1:{PORT}/callback",
    "public": False,
    "default_permissions": {"actions": "write", "metadata": "read"},
    "hook_attributes": {"url": "https://example.invalid/unused", "active": False},
}
STATE = secrets.token_urlsafe(24)
done = threading.Event()
failure: list[str] = []


def _page(body: str) -> bytes:
    return f"<!doctype html><meta charset=utf-8><body style='font:16px sans-serif;margin:3rem'>{body}</body>".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 受け取った code などを端末に出さない
        pass

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            form = (
                f"<form id=f method=post action='https://github.com/organizations/{OWNER}/settings/apps/new?state={STATE}'>"
                f"<input type=hidden name=manifest value='{json.dumps(MANIFEST).replace(chr(39), '&#39;')}'>"
                "<button>Create GitHub App</button></form><script>document.getElementById('f').submit()</script>"
            )
            self._send(200, _page(form))
        elif path == "/callback":
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            if params.get("state") != STATE or not params.get("code"):
                failure.append("state が合わないか、code がありません")
                self._send(400, _page("失敗しました。端末を見てください。"))
                done.set()
                return
            try:
                req = urllib.request.Request(
                    f"https://api.github.com/app-manifests/{params['code']}/conversions",
                    method="POST",
                    headers={"Accept": "application/vnd.github+json", "User-Agent": "docunavi-github-app-setup"},
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.load(r)
                pem_b64 = base64.b64encode(data["pem"].encode()).decode()
                fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(f"GITHUB_APP_ID={data['id']}\nGITHUB_APP_PRIVATE_KEY_B64={pem_b64}\n")
                slug = data["slug"]
                self._send(
                    200,
                    _page(
                        "<h2>App を作りました</h2>"
                        f"<p>次に、<a href='https://github.com/apps/{slug}/installations/new'>インストール</a>を開き、"
                        "<b>docunavi だけ</b>を選んで Install を押してください。</p><p>この画面は閉じて構いません。</p>"
                    ),
                )
                print(f"App を作りました（{slug}）。秘密は {OUT.name} に書きました（0600）。")
                print(f"インストール: https://github.com/apps/{slug}/installations/new  （docunavi だけを選ぶ）")
            except Exception as e:  # 秘密を含まない型名だけ出す
                failure.append(type(e).__name__)
                self._send(500, _page("失敗しました。端末を見てください。"))
            done.set()
        else:
            self._send(404, _page("not found"))


def main() -> int:
    if OUT.exists():
        print(
            f"{OUT.name} が既にあります。使い終わっていなければ中身を確かめ、不要なら消してから実行してください。",
            file=sys.stderr,
        )
        return 1
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/"
    print(
        f"ブラウザーで {url} を開きます（開かなければ、手で開く）。GitHub の画面で「Create GitHub App」を押してください。"
    )
    threading.Thread(
        target=webbrowser.open, args=(url,), daemon=True
    ).start()  # 開けない環境でも止まらないよう、別スレッドで
    done.wait(timeout=600)
    server.shutdown()
    if failure or not OUT.exists():
        print("失敗:", failure[0] if failure else "時間切れ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
