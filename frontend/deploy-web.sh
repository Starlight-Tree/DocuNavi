#!/usr/bin/env bash
# 画面（OpenNext）を Cloudflare Workers `docunavi-web` へデプロイする、ホスト側のスクリプト（開発コンテナには Cloudflare の資格情報が無い）。
# 使い方: cd <repo>/frontend && ./deploy-web.sh
# 公開してよい値だけをビルドに埋め込む（NEXT_PUBLIC_*）。秘密の値は無い。
set -euo pipefail
cd "$(dirname "$0")"
export NEXT_PUBLIC_AUTH_ENABLED=1
export NEXT_PUBLIC_API_BASE_URL="${NEXT_PUBLIC_API_BASE_URL:-https://docunavi-xe42miq4da-an.a.run.app}"
export NEXT_PUBLIC_STREAM_BASE_URL="${NEXT_PUBLIC_STREAM_BASE_URL:-https://docunavi-sse-relay-xe42miq4da-an.a.run.app}"
# /api/auth/* を中継する先（データ用 Worker）
export AUTH_PROXY_TARGET="${AUTH_PROXY_TARGET:-https://docunavi-api.wataru-apps.workers.dev}"
# PDF の中継（/api/documents/*/file）が API を呼ぶ先（Cloud Run の API。NEXT_PUBLIC_API_BASE_URL と同じ値）
export FILE_PROXY_TARGET="${FILE_PROXY_TARGET:-$NEXT_PUBLIC_API_BASE_URL}"
npm ci --no-audit --no-fund --ignore-scripts
if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then node_modules/.bin/wrangler whoami >/dev/null 2>&1 || node_modules/.bin/wrangler login; fi
npm run build:cf
node_modules/.bin/wrangler deploy
echo "deployed: docunavi-web"
