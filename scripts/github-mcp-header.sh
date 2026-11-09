#!/usr/bin/env bash
# GitHub MCP（Actions の起動と監視だけ）へ送る Authorization ヘッダーを、JSON で標準出力に返す。Claude Code の headersHelper から呼ばれる。
# 標準出力は、そのまま Claude Code が読む。端末で実行するとトークンが画面に出るので、手で実行しない（エージェントも実行しない）。
# GitHub App（docunavi-agent-actions）の秘密鍵から JWT を作り、docunavi だけの installation トークン（Actions: write / Metadata: read、1 時間）に換える。
# App の ID と秘密鍵（base64）は Infisical（プロジェクトは GITHUB_MCP_PROJECT_ID、環境 prod）から、ログイン済みのユーザーの権限で取る。
# 秘密鍵はファイルに書かない（プロセス置換で渡す）。トークンも書かない。失敗したときは、秘密を含まない理由を標準エラーに出して、非ゼロで終わる。
set -uo pipefail

OWNER=Starlight-Tree
REPO=docunavi
API=https://api.github.com

fail() { echo "github-mcp-header: $1" >&2; exit 1; }
for c in infisical openssl curl python3; do command -v "$c" >/dev/null 2>&1 || fail "$c が見つかりません"; done
[ -n "${GITHUB_MCP_PROJECT_ID:-}" ] || fail "GITHUB_MCP_PROJECT_ID が設定されていません（headersHelper のコマンドの前に付ける）"

get() { infisical secrets get "$1" --projectId "$GITHUB_MCP_PROJECT_ID" --env prod --plain --silent 2>/dev/null; }
app_id=$(get GITHUB_APP_ID) && [ -n "$app_id" ] || fail "GITHUB_APP_ID を取得できません（Infisical のログイン・プロジェクト・キーを確認）"
pem_b64=$(get GITHUB_APP_PRIVATE_KEY_B64) && [ -n "$pem_b64" ] || fail "GITHUB_APP_PRIVATE_KEY_B64 を取得できません"
pem=$(printf %s "$pem_b64" | base64 -d 2>/dev/null) && [ -n "$pem" ] || fail "秘密鍵の base64 を戻せません"

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
now=$(date +%s)
header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 60)) $((now + 300)) "$app_id" | b64url)
sig=$(printf %s "$header.$payload" | openssl dgst -sha256 -sign <(printf %s "$pem") 2>/dev/null | b64url) && [ -n "$sig" ] || fail "JWT の署名に失敗しました"
jwt="$header.$payload.$sig"

gh_curl() { # $1=認証ヘッダー値 以降=curl の引数（トークンをコマンドラインに出さない）
  local auth=$1; shift
  curl -fsS --max-time 20 -H @<(printf 'Authorization: Bearer %s\n' "$auth") -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' "$@" 2>/dev/null
}
json_get() { python3 -c 'import sys,json; print(json.load(sys.stdin)[sys.argv[1]])' "$1" 2>/dev/null; }

inst=$(gh_curl "$jwt" "$API/repos/$OWNER/$REPO/installation" | json_get id) && [ -n "$inst" ] || fail "App が $OWNER/$REPO にインストールされていません"
token=$(gh_curl "$jwt" -X POST -d "{\"repositories\":[\"$REPO\"],\"permissions\":{\"actions\":\"write\",\"metadata\":\"read\"}}" "$API/app/installations/$inst/access_tokens" | json_get token) && [ -n "$token" ] || fail "installation トークンを作れません"

printf '{"Authorization":"Bearer %s"}\n' "$token"
