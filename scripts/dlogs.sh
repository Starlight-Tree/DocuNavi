#!/usr/bin/env bash
# コンテナのログを読む。`docker logs` の代わり（読み取り専用のソケットプロキシ越し）。
# `docker logs` と `docker compose logs` は、先に inspect（GET /containers/<名前>/json）を呼ぶ。inspect は環境変数が見えるので、
# プロキシは許可していない。ここでは許可している GET /containers/<名前>/logs だけを呼ぶ。
# 使い方: scripts/dlogs.sh <コンテナ名> [行数。既定 50、最大 2000]
# 手順と限界は .devcontainer/README.md の「開発環境の Docker」。
set -uo pipefail

name="${1:-}"
tail="${2:-50}"

if [[ ! "$name" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
  echo "dlogs: コンテナ名は [a-zA-Z0-9_.-] だけで指定してください" >&2
  exit 2
fi
if [[ ! "$tail" =~ ^[0-9]{1,4}$ ]] || ((10#$tail > 2000)); then
  echo "dlogs: 行数は 0〜2000 の整数で指定してください" >&2
  exit 2
fi
if [[ ! "${DOCKER_HOST:-}" =~ ^tcp://[a-zA-Z0-9_.-]+:[0-9]+$ ]]; then
  echo "dlogs: DOCKER_HOST が tcp://<ホスト>:<ポート> の形ではありません（プロキシ経由の構成のはずです）" >&2
  exit 1
fi

# ログは、TTY なしのコンテナでは「8 バイトのヘッダー（種別 1 + 0 が 3 + 長さ 4）+ 本文」の繰り返しで返る。ヘッダーを外して本文だけを出す。
DLOGS_URL="http://${DOCKER_HOST#tcp://}/containers/${name}/logs?stdout=1&stderr=1&timestamps=1&tail=${tail}" \
  python3 -I -c '
import os, struct, sys, urllib.error, urllib.request

try:
    with urllib.request.urlopen(os.environ["DLOGS_URL"], timeout=10) as r:
        data = r.read()
except urllib.error.HTTPError as e:
    sys.exit(f"dlogs: HTTP {e.code}（拒否されたか、そのコンテナが無い）")
except OSError:
    sys.exit("dlogs: プロキシに接続できません")

out, i = bytearray(), 0
while i + 8 <= len(data) and data[i] in (0, 1, 2) and data[i + 1 : i + 4] == b"\0\0\0":
    n = struct.unpack(">I", data[i + 4 : i + 8])[0]
    out += data[i + 8 : i + 8 + n]
    i += 8 + n
sys.stdout.buffer.write(bytes(out) if i else data)
'
