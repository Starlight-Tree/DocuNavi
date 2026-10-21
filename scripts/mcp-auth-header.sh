#!/usr/bin/env bash
# docunavi-mcp（非公開の Cloud Run）へ送る Authorization ヘッダーを、JSON で標準出力に返す。Claude Code の headersHelper から呼ばれる。
# 標準出力は、そのまま Claude Code が読む。端末で実行すると ID トークンが画面に出るので、手で実行しない。
# 使う ID は、ログイン済みのユーザー自身（日常の閲覧専用のなりすましは外す。run.invoker を持つのはユーザーだけ）。
# トークンはファイルにもログにも書かない。失敗したときは、秘密を含まない理由を標準エラーに出して、非ゼロで終わる。
set -uo pipefail

if ! token=$(env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud auth print-identity-token 2>/dev/null) || [ -z "$token" ]; then
  echo "mcp-auth-header: gcloud の ID トークンを取得できません（gcloud auth login が要る可能性があります）" >&2
  exit 1
fi

printf '{"Authorization":"Bearer %s"}\n' "$token"
