#!/usr/bin/env bash
# 標準のローカル開発構成: ローカル Postgres + 認証（Better Auth の Worker）+ API + 画面。開発コンテナの中で動かす。
#   scripts/dev-local.sh check    起動せずに、前提を調べる（読み取りだけ。DB には何も書かない）
#   scripts/dev-local.sh start    Infisical から .dev.vars を作ったあと、Worker(8787)・API(8000)・画面(3000) を起動する（PID と log は $STATE に置く）
#   scripts/dev-local.sh vars     Infisical（docunavi-local・dev・/）から、workers/api/.dev.vars だけを作り直す（権限 600。値は表示しない）
#   scripts/dev-local.sh register Infisical へ 6 個の値を登録する（ユーザーが自分の端末で実行する。エージェントは実行しない）
#   scripts/dev-local.sh status   起動状態と /health
#   scripts/dev-local.sh stop     記録した PID（と、その子プロセス）だけを止める
#   scripts/dev-local.sh login    テスト用ユーザーでログインして、Cookie を $STATE/cookies.txt に保存する（値は表示しない）
#   scripts/dev-local.sh cookie   上の Cookie の値を標準出力に出す（ユーザーが自分の端末で実行する。エージェントは実行しない）
#
# 何をするか: DB・認証・履歴は本物の実装を動かし、LLM だけを置き換える。GEMINI_API_KEY を空にするので、API はモックモード
#   （app/agents/graph.py の MockChatModel。固定の返答）になる。.env に実キーや本番の DATABASE_URL があっても、起動時の環境変数が優先される。
# しないこと: DB の作成・初期化・スキーマの適用・削除（足りなければ、手順を示して止まる）。本番 DB・実 API・実 LLM への接続。
#   DATABASE_URL のホストは、ローカル（host.docker.internal / localhost / 127.0.0.1 / db）だけを許す。
# ポート: Worker 8787、API 8000、画面 3000。3100・3101・8100 は E2E 専用（frontend/playwright.config.ts）なので使わない。
#   画面の 3000 は、ホストの旧 Compose の frontend（docunavi-frontend）と同じ番号。ホストで `docker compose -p docunavi stop frontend api` してから使う。
# 秘密の正本: Infisical のプロジェクト docunavi-local（環境 dev、パス /）。start が、ログイン済みのユーザー（infisical login）の権限で取り、
#   workers/api/.dev.vars（Git 管理外）を権限 600 で作り直す。.dev.vars は手で書かない（次の start で上書きされる）。取れないときは、理由を出して止まる。
#   キー: BETTER_AUTH_SECRET BETTER_AUTH_URL（= http://localhost:3000） GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET TEST_LOGIN_EMAIL TEST_LOGIN_TOKEN。値は表示しない。
# 環境変数で変えられる: LOCAL_DB_HOST LOCAL_DB_PORT LOCAL_DB_USER LOCAL_DB_PASSWORD LOCAL_DB_NAME DEV_LOCAL_STATE
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ローカル専用の使い捨ての値（docker-compose.pg.yml と同じ。秘密ではない）
DB_HOST="${LOCAL_DB_HOST:-host.docker.internal}"
DB_PORT="${LOCAL_DB_PORT:-5433}"
DB_USER="${LOCAL_DB_USER:-docunavi}"
DB_PASSWORD="${LOCAL_DB_PASSWORD:-docunavi-local-throwaway}"
DB_NAME="${LOCAL_DB_NAME:-docunavi}"
DATABASE_URL_LOCAL="postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"

WORKER_PORT=8787
API_PORT=8000
WEB_PORT=3000
WEB_ORIGIN="http://localhost:${WEB_PORT}"
STATE="${DEV_LOCAL_STATE:-/tmp/docunavi-dev-local}"
DEV_VARS="workers/api/.dev.vars"
REQUIRED_TABLES="user session account verification jwks checkpoints checkpoint_writes user_roles"
REQUIRED_VARS="BETTER_AUTH_SECRET BETTER_AUTH_URL GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET TEST_LOGIN_EMAIL TEST_LOGIN_TOKEN"
INFISICAL_PROJECT_NAME="docunavi-local"
INFISICAL_PROJECT_ID="<INFISICAL_PROJECT_ID>" # 2026-11-11 に作成。ID は秘密ではない
INFISICAL_ENV="dev"

die() { echo "dev-local: $*" >&2; exit 1; }
say() { echo "dev-local: $*"; }

vars_value() { # 値を返す（呼び出し側が表示しないこと）
  sed -n "s/^$1=//p" "$DEV_VARS" | head -n1
}

http_code() { curl -s -m 3 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true; }

port_busy() { [ "$(http_code "http://127.0.0.1:$1/")" != "000" ]; }

check_db_host() {
  case "$DB_HOST" in
    host.docker.internal | localhost | 127.0.0.1 | db) ;;
    *) die "DB のホスト '$DB_HOST' はローカルではありません。本番 DB には接続しません。" ;;
  esac
}

check_tools() {
  [ -x .venv/bin/uvicorn ] || die ".venv/bin/uvicorn がありません（pip install -r requirements.txt）。"
  [ -x workers/api/node_modules/.bin/wrangler ] || die "workers/api/node_modules/.bin/wrangler がありません（cd workers/api && npm ci）。"
  [ -d frontend/node_modules ] || die "frontend/node_modules がありません（cd frontend && npm ci）。"
}

check_vars() {
  [ -f "$DEV_VARS" ] || die "$DEV_VARS がありません（Git 管理外。必要な名前: $REQUIRED_VARS）。"
  local missing="" name
  for name in $REQUIRED_VARS; do
    grep -q "^${name}=." "$DEV_VARS" || missing="$missing $name"
  done
  [ -z "$missing" ] || die "$DEV_VARS に足りない項目:$missing（値を自分で入れてください。自動では書きません）。"
  [ "$(vars_value BETTER_AUTH_URL)" = "$WEB_ORIGIN" ] || die "$DEV_VARS の BETTER_AUTH_URL は $WEB_ORIGIN にしてください（画面のオリジン）。"
  [ "$(vars_value TEST_LOGIN_TOKEN | wc -c)" -ge 33 ] || die "TEST_LOGIN_TOKEN は 32 文字以上にしてください。"
}

# --- Infisical（docunavi-local・dev・/）---------------------------------------------------------------------------------
# ログイン済みのユーザーの権限で、プロジェクトを名前から探す（マシン ID の権限は使わない・広げない）。トークンは環境変数でだけ渡し、表示しない。
#   MODE=id   プロジェクト ID を標準出力に出す
#   MODE=pull REQUIRED の 6 個を取り、OUT に権限 600 で書く（同じディレクトリの一時ファイルから置き換える。値は標準出力・標準エラーに出さない）
infisical_py() {
  local tok
  command -v infisical >/dev/null 2>&1 || die "infisical コマンドがありません。"
  tok="$(infisical user get token --plain --silent 2>/dev/null)" || true
  [ -n "$tok" ] || die "Infisical のユーザーログインがありません。開発コンテナの端末（vscode ユーザー）で infisical login を実行してください（ブラウザーでの承認が要ります）。"
  INF_TOKEN="$tok" PROJECT_NAME="$INFISICAL_PROJECT_NAME" PROJECT_ID="$INFISICAL_PROJECT_ID" INF_ENV="$INFISICAL_ENV" REQUIRED="$REQUIRED_VARS" OUT="$DEV_VARS" MODE="$1" python3 -I - <<'PY'
import json, os, sys, urllib.error, urllib.parse, urllib.request

B = "https://app.infisical.com"
tok = os.environ["INF_TOKEN"]


def die(msg):
    sys.exit("dev-local: " + msg)


def get(path):
    req = urllib.request.Request(B + path, headers={"Authorization": "Bearer " + tok})
    try:
        return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            die(f"Infisical に拒否されました（HTTP {e.code}）。infisical login をやり直してください（プロジェクト {os.environ['PROJECT_NAME']} の権限も確認）。")
        die(f"Infisical の応答が不正です（HTTP {e.code}）。")
    except Exception as e:  # 通信の失敗。URL・トークンは出さない
        die(f"Infisical につながりません（{type(e).__name__}）。")


name = os.environ["PROJECT_NAME"]
want_id = os.environ["PROJECT_ID"]
env = os.environ["INF_ENV"]
allp = get("/api/v1/workspace").get("workspaces", [])
projects = [p for p in allp if p.get("id") == want_id]
if not projects:
    # 「存在しない」と即断しない。何が見えているかを、値を出さずに示す（ログイン中の組織・プロジェクト・ID・環境・パス・権限）
    orgs = get("/api/v1/organization")
    org_names = [o.get("name") for o in orgs.get("organizations", [])] if isinstance(orgs, dict) else []
    same_name = [p["id"] for p in allp if p.get("name") == name]
    die(
        f"Infisical のプロジェクト {name}（ID {want_id}）が、ログイン中のユーザーから見えません。\n"
        f"  確認の順: 1) ログイン中の組織: {org_names or '取得できません'}（別のアカウント・組織でログインしていないか。infisical user get / infisical org）\n"
        f"  2) 見えているプロジェクト: {[p.get('name') for p in allp]}（同じ名前の別 ID: {same_name or 'なし'}）\n"
        f"  3) 環境 {env}・パス / ・自分の権限（プロジェクトの Members に自分がいるか）。\n"
        "  新しい登録・追加・ローテーションは、自動では行いません。理由と影響を示して、ユーザーの承認を得てください。"
    )
if env not in [e["slug"] for e in projects[0].get("environments", [])]:
    die(f"プロジェクト {name} に環境 {env} がありません。")
pid = want_id

if os.environ["MODE"] == "id":
    print(pid)
    sys.exit(0)

q = urllib.parse.urlencode({"workspaceId": pid, "environment": env, "secretPath": "/", "recursive": "false", "include_imports": "false"})
have = {s["secretKey"]: s["secretValue"] for s in get("/api/v3/secrets/raw?" + q).get("secrets", [])}
required = os.environ["REQUIRED"].split()
missing = [k for k in required if not have.get(k)]
if missing:
    die(
        f"Infisical（{name} / {env} / /）に足りないキー: {' '.join(missing)}（いまあるキー: {' '.join(sorted(have)) or 'なし'}）。\n"
        "  環境・パス・権限（読めないキーは出ません）を確認してください。初回なら register で登録します。既存の値がある場合は、上書き・追加の前に承認を得てください。"
    )
bad = [k for k in required if "\n" in have[k] or "\r" in have[k]]
if bad:
    die(f"値に改行があるキー: {' '.join(bad)}（.dev.vars の 1 行形式にできません）。")
out = os.environ["OUT"]
tmp = out + ".tmp"
try:
    os.unlink(tmp)
except FileNotFoundError:
    pass
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    for k in required:
        f.write(f"{k}={have[k]}\n")
os.chmod(tmp, 0o600)
os.replace(tmp, out)
print(f"dev-local: {out} を Infisical（{name} / {env} / /）から作りました（{len(required)} キー、権限 600。値は表示していません）")
PY
}

cmd_vars() {
  infisical_py pull
}

cmd_register() { # ユーザーが自分の端末で実行する。値は read -rs で受け、権限 600 の一時ファイルだけに書く（引数にもチャットにも出さない）
  [ -t 0 ] || die "register は端末で実行してください（値を read -rs で受けます）。"
  local pid tmp gid gsec name
  if [ "${2:-}" != "--overwrite" ] && (cmd_vars) >/dev/null 2>&1; then
    say "すでに Infisical（${INFISICAL_PROJECT_NAME} / ${INFISICAL_ENV} / /）に 6 キーが登録されています。.dev.vars はそこから作り直しました（値は表示していません）。"
    say "再利用してください（start / vars）。Secret の追加・ローテーションは、理由と影響を示してユーザーの承認を得たうえで、register --overwrite で行います。"
    return 0
  fi
  pid="$(infisical_py id)" || exit 1
  umask 077
  tmp="$(mktemp)" || die "一時ファイルを作れません。"
  # EXIT の時点では、関数のローカル変数 tmp は消えている（set -u で unbound になり、削除されない）。パスは今ここで展開して trap に埋め込む
  trap "rm -f '$tmp' '$tmp.err'" EXIT
  say "登録先: Infisical ${INFISICAL_PROJECT_NAME}（ID ${pid}）/ 環境 ${INFISICAL_ENV} / パス /"
  say "BETTER_AUTH_SECRET BETTER_AUTH_URL TEST_LOGIN_EMAIL TEST_LOGIN_TOKEN は、いまの $DEV_VARS の値をそのまま移します。"
  for name in BETTER_AUTH_SECRET BETTER_AUTH_URL TEST_LOGIN_EMAIL TEST_LOGIN_TOKEN; do
    [ -n "$(vars_value "$name")" ] || die "$DEV_VARS に $name がありません（移す元の値がありません）。"
  done
  [ "$(vars_value BETTER_AUTH_URL)" = "$WEB_ORIGIN" ] || die "$DEV_VARS の BETTER_AUTH_URL が $WEB_ORIGIN ではありません。"
  read -rsp "GOOGLE_CLIENT_ID（入力は表示されません）: " gid; echo
  read -rsp "GOOGLE_CLIENT_SECRET（入力は表示されません）: " gsec; echo
  [ -n "$gid" ] && [ -n "$gsec" ] || die "Client ID と Client Secret の両方が要ります。"
  case "$gid$gsec" in *[[:space:]]*) die "値に空白・改行は入れられません。" ;; esac
  {
    printf 'BETTER_AUTH_SECRET=%s\n' "$(vars_value BETTER_AUTH_SECRET)"
    printf 'BETTER_AUTH_URL=%s\n' "$(vars_value BETTER_AUTH_URL)"
    printf 'GOOGLE_CLIENT_ID=%s\n' "$gid"
    printf 'GOOGLE_CLIENT_SECRET=%s\n' "$gsec"
    printf 'TEST_LOGIN_EMAIL=%s\n' "$(vars_value TEST_LOGIN_EMAIL)"
    printf 'TEST_LOGIN_TOKEN=%s\n' "$(vars_value TEST_LOGIN_TOKEN)"
  } >"$tmp"
  unset gid gsec
  infisical secrets set --file "$tmp" --projectId "$pid" --env "$INFISICAL_ENV" --path / --silent >/dev/null 2>"$tmp.err" \
    || { rm -f "$tmp.err"; die "登録に失敗しました（Infisical のログイン・プロジェクトの権限を確認）。"; }
  rm -f "$tmp.err"
  say "登録しました。取得し直して、登録した値と一致するか確かめます（値は表示しません）。"
  cmd_vars
  cmp -s "$tmp" "$DEV_VARS" || die "取得した値が、登録した値と一致しません。.dev.vars を信用しないでください。"
  chmod 600 "$DEV_VARS"
  say "一致を確認しました。$DEV_VARS は Infisical から作った値（権限 600）です。以後、手で書かないでください。"
}

check_db() { # 読み取りだけ。テーブルの有無を調べる
  DATABASE_URL="$DATABASE_URL_LOCAL" REQUIRED_TABLES="$REQUIRED_TABLES" DB_NAME="$DB_NAME" .venv/bin/python - <<'PY' || exit 1
import os, sys
import psycopg
try:
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as c:
        have = {r[0] for r in c.execute("select table_name from information_schema.tables where table_schema='public'")}
except Exception as exc:  # 接続文字列は出さない
    sys.exit(f"dev-local: DB に接続できません（{type(exc).__name__}）。docunavi-db は起動していますか（ホストの 127.0.0.1:5433）。")
missing = [t for t in os.environ["REQUIRED_TABLES"].split() if t not in have]
if missing:
    sys.exit(
        f"dev-local: DB '{os.environ['DB_NAME']}' に表がありません: {' '.join(missing)}\n"
        "  スキーマは自動では作りません。新しい DB なら、手で 1 回だけ db/migrations/0001_schema.sql を適用してください（docker-compose.pg.yml の冒頭）。"
    )
print("dev-local: DB の表: 揃っています（読み取りだけ。何も書いていません）")
PY
}

cmd_check() {
  check_db_host
  check_tools
  check_vars
  check_db
  say "check OK（DB: ${DB_HOST}:${DB_PORT}/${DB_NAME}）"
}

pid_file() { echo "$STATE/$1.pid"; }

killtree() {
  local c
  for c in $(pgrep -P "$1" 2>/dev/null || true); do killtree "$c"; done
  kill "$1" 2>/dev/null || true
}

alive() { [ -f "$(pid_file "$1")" ] && kill -0 "$(cat "$(pid_file "$1")")" 2>/dev/null; }

wait_for() { # 名前 URL 秒
  local i
  for i in $(seq 1 "$3"); do
    [ "$(http_code "$2")" = "200" ] && return 0
    sleep 1
  done
  die "$1 が $3 秒以内に起動しませんでした（log: $STATE/$1.log）。"
}

cmd_start() {
  cmd_vars
  cmd_check
  mkdir -p "$STATE" && chmod 700 "$STATE"
  local n
  for n in worker api web; do alive "$n" && die "$n はすでに起動しています（PID $(cat "$(pid_file "$n")")）。先に stop してください。"; done
  for n in "$WORKER_PORT" "$API_PORT" "$WEB_PORT"; do port_busy "$n" && die "ポート $n が使われています。"; done

  # 認証の Worker（Better Auth）。Hyperdrive の接続先はローカルの Postgres
  # （`cd … && … &` と書くと、& が AND 連結の全体に掛かり、出力を握ったサブシェルが残る。cd は ; で分け、起動は単独のコマンドにする）
  (
    cd workers/api
    CLOUDFLARE_HYPERDRIVE_LOCAL_CONNECTION_STRING_HYPERDRIVE="$DATABASE_URL_LOCAL" WRANGLER_SEND_METRICS=false \
      nohup npx wrangler dev --local --port "$WORKER_PORT" >"$STATE/worker.log" 2>&1 </dev/null &
    echo $! >"$(pid_file worker)"
  )
  wait_for worker "http://127.0.0.1:${WORKER_PORT}/health" 60

  # API。LLM だけを置き換える（キーを空に → モックモード）。.env の本番の値より、ここで渡す値が優先される
  GEMINI_API_KEY= GEMINI_API_KEY_PAID= GOOGLE_GENAI_USE_VERTEXAI= D1_API_URL= D1_API_TOKEN= \
    DB_BACKEND=pg DATABASE_URL="$DATABASE_URL_LOCAL" ACCESS_MODE=off \
    AUTH_MODE=required AUTH_BASE_URL="$WEB_ORIGIN" AUTH_JWKS_URL="http://127.0.0.1:${WORKER_PORT}/api/auth/jwks" \
    nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" >"$STATE/api.log" 2>&1 </dev/null &
  echo $! >"$(pid_file api)"
  wait_for api "http://127.0.0.1:${API_PORT}/health" 60

  # 画面。frontend/.env.local の値（本番の URL など）より、ここで渡す値が優先される
  (
    cd frontend
    NEXT_PUBLIC_AUTH_ENABLED=1 NEXT_PUBLIC_AUTH_BASE_URL= NEXT_PUBLIC_API_BASE_URL= NEXT_PUBLIC_STREAM_BASE_URL= \
      API_PROXY_TARGET="http://127.0.0.1:${API_PORT}" AUTH_PROXY_TARGET="http://127.0.0.1:${WORKER_PORT}" NEXT_TELEMETRY_DISABLED=1 \
      nohup npx next dev -H 127.0.0.1 -p "$WEB_PORT" >"$STATE/web.log" 2>&1 </dev/null &
    echo $! >"$(pid_file web)"
  )
  wait_for web "http://127.0.0.1:${WEB_PORT}/" 90

  say "起動しました: ${WEB_ORIGIN}（API :${API_PORT}、Worker :${WORKER_PORT}）。log: $STATE/*.log"
  say "画面のログインは Google だけです。会話の履歴を見るには、login のあと、docs/development.md の「ローカルの標準構成」の手順で Cookie を入れてください。"
  say "止めるとき: scripts/dev-local.sh stop"
}

cmd_status() {
  local n
  for n in worker api web; do
    if alive "$n"; then echo "$n: 起動中（PID $(cat "$(pid_file "$n")")）"; else echo "$n: 停止"; fi
  done
  echo "worker /health: $(http_code "http://127.0.0.1:${WORKER_PORT}/health")"
  echo "api /health:    $(curl -s -m 3 "http://127.0.0.1:${API_PORT}/health" 2>/dev/null || echo 'つながりません')"
  echo "web /:          $(http_code "http://127.0.0.1:${WEB_PORT}/")"
}

cmd_stop() {
  local n pid
  for n in web api worker; do
    if [ -f "$(pid_file "$n")" ]; then
      pid="$(cat "$(pid_file "$n")")"
      killtree "$pid"
      rm -f "$(pid_file "$n")"
      say "$n を止めました（PID $pid）"
    fi
  done
  sleep 2
  for n in "$WORKER_PORT" "$API_PORT" "$WEB_PORT"; do
    port_busy "$n" && say "警告: ポート $n がまだ使われています（このスクリプトが起動したものではない可能性があります。止めていません）。"
  done
  return 0
}

cmd_login() { # テスト用ユーザーでサインイン（無ければ登録）して、Cookie を $STATE/cookies.txt に保存する。値は表示しない
  [ "$(http_code "$WEB_ORIGIN/api/auth/get-session")" = "200" ] || die "画面（$WEB_ORIGIN）につながりません。先に start してください。"
  mkdir -p "$STATE" && chmod 700 "$STATE"
  umask 077
  local pwf="$STATE/test-login-password" hdr="$STATE/.hdr" jar="$STATE/cookies.txt" email code
  [ -f "$pwf" ] || head -c 24 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=' >"$pwf"
  email="$(vars_value TEST_LOGIN_EMAIL)"
  printf 'origin: %s\ncontent-type: application/json\nx-test-login-token: %s\n' "$WEB_ORIGIN" "$(vars_value TEST_LOGIN_TOKEN)" >"$hdr"
  trap 'rm -f "$hdr"' RETURN
  body() { printf '{"email":"%s","password":"%s"%s}' "$email" "$(cat "$pwf")" "$1"; }
  code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' -c "$jar" -H @"$hdr" -d "$(body '')" "$WEB_ORIGIN/api/auth/sign-in/email")"
  if [ "$code" != "200" ]; then
    curl -s -m 10 -o /dev/null -H @"$hdr" -d "$(body ',"name":"local-dev"')" "$WEB_ORIGIN/api/auth/sign-up/email" || true
    code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' -c "$jar" -H @"$hdr" -d "$(body '')" "$WEB_ORIGIN/api/auth/sign-in/email")"
  fi
  [ "$code" = "200" ] || die "ログインに失敗しました（HTTP $code）。Worker の log: $STATE/worker.log"
  chmod 600 "$jar"
  if [ "$(curl -s -m 10 -o /dev/null -w '%{http_code}' -b "$jar" "$WEB_ORIGIN/api/auth/token")" != "200" ]; then
    say "警告: アクセストークンの発行に失敗しました。DB の jwks の行が、いまの BETTER_AUTH_SECRET と別の秘密で暗号化されている可能性があります（Worker の log: $STATE/worker.log）。"
    say "  対処（どちらもユーザーの判断）: LOCAL_DB_NAME に別の DB を使う、または、ローカルの DB の jwks の行を消す（Worker が作り直す）。"
  fi
  say "ログインしました。Cookie を $jar に保存しました（値は表示していません）。"
  say "ブラウザーに入れる: docs/development.md の「ローカルの標準構成」。値は、自分の端末で scripts/dev-local.sh cookie を実行して取る。"
}

cmd_cookie() { # Cookie の値だけを出す（ユーザー用。エージェントは実行しない）
  [ -f "$STATE/cookies.txt" ] || die "先に login してください。"
  awk -F'\t' '$6=="better-auth.session_token"{print $7}' "$STATE/cookies.txt"
}

case "${1:-}" in
  check) cmd_check ;;
  start) cmd_start ;;
  vars) cmd_vars ;;
  register) cmd_register "$@" ;;
  status) cmd_status ;;
  stop) cmd_stop ;;
  login) cmd_login ;;
  cookie) cmd_cookie ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
