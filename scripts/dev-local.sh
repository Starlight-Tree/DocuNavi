#!/usr/bin/env bash
# 標準のローカル開発構成: ローカル Postgres + 認証（Better Auth の Worker）+ API + 画面。開発コンテナの中で動かす。
#   scripts/dev-local.sh check    起動せずに、前提を調べる（読み取りだけ。DB には何も書かない）
#   scripts/dev-local.sh start [--llm real|mock]  Infisical から .dev.vars を作ったあと、Worker(8787)・API(8000)・画面(3000) を起動する（PID と log は $STATE に置く）。
#                                   LLM は既定が real（実 API）。mock は、--llm mock か DEV_LOCAL_LLM=mock と明示したときだけ。キーが取れなくても、自動でモックには戻らない
#   scripts/dev-local.sh autostart [--llm real|mock]  開発コンテナの起動時に自動で呼ばれる（.devcontainer/devcontainer.json の postStartCommand）。start と同じ方法でモードを決める。
#                                   3 つとも起動中で、モードも同じなら何もしない（PID・コマンド・ポート・所有者を確かめる。別モードのプロセスは再利用せず、止めずに失敗する）。
#                                   失敗しても終了コードは 0（コンテナへの接続を妨げない）。結果と理由は $STATE/autostart.log と status に出る
#   scripts/dev-local.sh vars     Infisical（docunavi-local・dev・/）から、workers/api/.dev.vars だけを作り直す（権限 600。値は表示しない）
#   scripts/dev-local.sh register Infisical へ 6 個の値を登録する（ユーザーが自分の端末で実行する。エージェントは実行しない）
#   scripts/dev-local.sh register-gemini  .env の GEMINI_API_KEY を、値を表示せずに Infisical（docunavi-local）へ保存し、取得し直して一致を確かめる（ユーザーが自分の端末で実行する。エージェントは実行しない）
#   scripts/dev-local.sh gemini-key-info  Infisical のキーの所属プロジェクトと課金状態を、可能な範囲で調べる（ユーザーが自分の端末で実行する。値は表示しない）
#   scripts/dev-local.sh ack-gemini       実 API の概算費用・上限・無料枠の有無を表示し、ユーザーが承認したことを記録する（実 API の起動の前提。ユーザーが自分の端末で実行する）
#   scripts/dev-local.sh with-key -- CMD  GEMINI_API_KEY（Infisical）を、CMD のプロセスの環境変数にだけ渡して実行する（実 API のテスト用。引数・ログには出さない）
#   scripts/dev-local.sh status   起動状態、LLM のモード（実 API / モック）と /health
#   scripts/dev-local.sh stop     記録した PID（と、その子プロセス）だけを止める
#   scripts/dev-local.sh login    テスト用ユーザーでログインして、Cookie を $STATE/cookies.txt に保存する（値は表示しない）
#   scripts/dev-local.sh cookie   上の Cookie の値を標準出力に出す（ユーザーが自分の端末で実行する。エージェントは実行しない）
#
# 何をするか: DB・認証・履歴は本物の実装を動かす。LLM は、real（実 API。既定）か mock（固定の返答。明示したときだけ）。
#   real: GEMINI_API_KEY を Infisical（docunavi-local / dev / /）から取り、API のプロセスの環境変数にだけ渡す（画面・Worker・.dev.vars・ログ・引数には出さない）。
#         有料キー（GEMINI_API_KEY_PAID）・Vertex AI・モデルの変更・フォールバックのモデルは使わない（空にする。モデルは app/agents/graph.py の DEFAULT_MODEL）。
#         キーが取れない、または ack-gemini の承認が無いときは、理由を出して止まる（モックには戻らない。.dev.vars も既存のプロセスも変えない）。
#   mock: GEMINI_API_KEY を空にするので、API は MockChatModel（固定の返答）になる。
#   どちらも、.env に実キーや本番の DATABASE_URL があっても、起動時の環境変数が優先される。
#   通常の画面（localhost:3000）で、利用者が実 API に質問する回数に、上限はない。実 API のテストの予算（呼び出し 20 回・質問 10 件）は tests/real_api_budget.py。
# しないこと: DB の作成・初期化・スキーマの適用・削除（足りなければ、手順を示して止まる）。本番 DB への接続。
#   DATABASE_URL のホストは、ローカル（host.docker.internal / localhost / 127.0.0.1 / db）だけを許す。
# ポート: Worker 8787、API 8000、画面 3000。3100・3101・8100 は E2E 専用（frontend/playwright.config.ts）なので使わない。
#   画面の 3000 は、ホストの旧 Compose の frontend（docunavi-frontend）と同じ番号。ホストで `docker compose -p docunavi stop frontend api` してから使う。
# 秘密の正本: Infisical のプロジェクト docunavi-local（環境 dev、パス /）。start が、ログイン済みのユーザー（infisical login）の権限で取り、
#   workers/api/.dev.vars（Git 管理外）を権限 600 で作り直す。.dev.vars は手で書かない（次の start で上書きされる）。取れないときは、理由を出して止まる。
#   キー: BETTER_AUTH_SECRET BETTER_AUTH_URL（= http://localhost:3000） GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET TEST_LOGIN_EMAIL TEST_LOGIN_TOKEN。値は表示しない。
#   実 API のキー GEMINI_API_KEY も同じ保存先（.dev.vars には入れない。API のプロセスだけに渡す）。.env の既存のキーを register-gemini で写す。
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
GEMINI_KEY_SOURCE="Infisical ${INFISICAL_PROJECT_NAME} / ${INFISICAL_ENV} / GEMINI_API_KEY"
LOCAL_DIR="$ROOT/.dev-local" # Git 管理外。承認の記録とキー情報の記録（キーの値は入れない）
ACK_FILE="$LOCAL_DIR/gemini-ack"
KEYINFO_FILE="$LOCAL_DIR/gemini-key-info"
LLM_MODE=""
GKEY=""

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
#   MODE=llmkey GEMINI_API_KEY の値だけを標準出力に出す（呼び出し側が変数に受け、表示しない。無い・不正なら、理由を出して終了コード 1。何も書かない）
infisical_py() {
  local tok
  command -v infisical >/dev/null 2>&1 || die "infisical コマンドがありません。"
  # 未ログインのとき、CLI は終了コード 1 で、対話の選択画面の文字を標準出力に出す。終了コードと、トークンの文字種（英数字と . _ -）の両方で見分ける
  tok="$(infisical user get token --plain --silent </dev/null 2>/dev/null)" || tok=""
  case "$tok" in "" | *[!A-Za-z0-9._-]*) die "Infisical のユーザーログインがありません（または期限切れです）。開発コンテナの端末（vscode ユーザー）で infisical login を実行してください（ブラウザーでの承認が要ります）。" ;; esac
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
if os.environ["MODE"] == "llmkey":
    value = have.get("GEMINI_API_KEY", "")
    if not value:
        die(
            f"Infisical（{name} / {env} / /）に GEMINI_API_KEY がありません（いまあるキー: {' '.join(sorted(have)) or 'なし'}）。\n"
            "  .env の既存のキーを写すには、ユーザーが自分の端末で scripts/dev-local.sh register-gemini を実行してください。自動では登録せず、新しいキーも作りません。"
        )
    if any(c.isspace() for c in value) or value == "your_api_key_here":
        die("Infisical の GEMINI_API_KEY の値が不正です（空白・改行を含む、またはテンプレートのままです）。")
    sys.stdout.write(value)
    sys.exit(0)

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

# --- LLM のモード（real / mock）と、実 API の前提（キー・承認）----------------------------------------------------------
# 決め方（start と autostart で同じ）: 引数 --llm > 環境変数 DEV_LOCAL_LLM > 既定 real。mock は、明示したときだけ。
resolve_llm_mode() {
  local m="${DEV_LOCAL_LLM:-real}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --llm) [ $# -ge 2 ] || die "--llm には real か mock を指定してください。"; m="$2"; shift 2 ;;
      --llm=*) m="${1#--llm=}"; shift ;;
      *) die "不明な引数: $1（使い方: start|autostart [--llm real|mock]）" ;;
    esac
  done
  case "$m" in
    real | mock) LLM_MODE="$m" ;;
    *) die "LLM のモードは real か mock です（指定: $m）。" ;;
  esac
}

llm_model_name() { # アプリの既定のモデル名（GEMINI_MODEL は空にして渡すので、これが使われる）
  sed -n 's/^DEFAULT_MODEL = "\(.*\)"/\1/p' app/agents/graph.py | head -n1
}

key_fp() { # キーの値の SHA-256 の先頭 12 桁（承認の記録の照合用。値は復元できない）。引数ではなく、標準入力で渡す
  printf '%s' "$1" | sha256sum | cut -c1-12
}

llm_label() { # モード モデル名 → 表示用の 1 行
  if [ "$1" = real ]; then echo "実API（${2:-不明}・キー: ${GEMINI_KEY_SOURCE}）"; else echo "モック（明示指定。固定の返答）"; fi
}

record_field() { # 記録のファイル 項目名
  [ -f "$1" ] && sed -n "s/^$2=//p" "$1" | head -n1 || true
}

print_gemini_summary() { # key_fp モデル名。実 API の概算費用・上限・無料枠の有無・課金状態（確認できた範囲）
  local fp="$1" model="$2" proj billing
  echo "--- 実 API（Gemini Developer API）の利用条件 ---"
  echo "モデル: ${model}（質問の埋め込み: gemini-embedding-2）。キー: ${GEMINI_KEY_SOURCE}（値は表示しません）"
  if [ -f "$KEYINFO_FILE" ] && [ "$(record_field "$KEYINFO_FILE" key_fp)" = "$fp" ]; then
    proj="$(record_field "$KEYINFO_FILE" project_id)"; billing="$(record_field "$KEYINFO_FILE" billing_enabled)"
    echo "キーの所属プロジェクト: ${proj:-不明} / 課金の有効化（billingEnabled）: ${billing:-不明}（$(record_field "$KEYINFO_FILE" checked_at) に確認）"
  else
    echo "キーの所属プロジェクトと課金状態: **未確認**（gemini-key-info を実行するか、AI Studio https://aistudio.google.com/apikey でキーごとの Plan を確認。この URL は実機で確認していません）"
  fi
  echo "無料枠: あり（公式の料金表、2026-11-11）。無料枠では入力が製品の改善に使われます。「無料だから安全」とは扱いません。"
  echo "有料枠の料金（100 万トークンあたり）: gemini-3.1-flash-lite は入力 \$0.25・出力 \$1.50、gemini-embedding-2 は入力 \$0.20。"
  echo "概算費用（有料枠だった場合。トークン数は未測定の見積もり）: 1 問あたり約 \$0.003〜\$0.005（LLM 2 回、入力 4〜10k・出力約 1k トークンと仮定）。"
  echo "通常の画面（localhost:3000）で質問する回数に、上限はありません。テストの実行 1 回の予算は別（外部 API 呼び出し 20 回・質問 10 件・300 秒。tests/real_api_budget.py）。"
}

# 実 API の前提: キーを取り（GKEY）、承認の記録（キーの指紋とモデル名が一致）があること。満たさなければ、理由を出して終了する。
# .dev.vars の再生成・既存のプロセスの停止・register の呼び出しより前に実行する（失敗しても、何も変えない）。モックには戻らない。
require_gemini() {
  GKEY="$(infisical_py llmkey)" || exit 1
  local fp model
  fp="$(key_fp "$GKEY")"
  model="$(llm_model_name)"
  if [ ! -f "$ACK_FILE" ] || ! grep -qx "key_fp=$fp" "$ACK_FILE" || ! grep -qx "model=$model" "$ACK_FILE"; then
    GKEY=""
    print_gemini_summary "$fp" "$model" >&2
    die "実 API の起動には、ユーザーの承認が要ります。上の内容を確認し、自分の端末で scripts/dev-local.sh ack-gemini を実行してください（承認の記録が無い、またはキー・モデルが変わりました）。モックには戻りません（mock は --llm mock と明示したときだけ）。"
  fi
}

llm_preflight() { [ "$LLM_MODE" != real ] || require_gemini; }

write_llm_mode() { # 起動したモードの記録（キーの値は入れない）。status と autostart の再利用の判定が読む
  local model="" fp=""
  if [ "$LLM_MODE" = real ]; then model="$(llm_model_name)"; fp="$(key_fp "$GKEY")"; fi
  umask 077
  {
    echo "mode=$LLM_MODE"
    echo "model=$model"
    echo "key_source=$([ "$LLM_MODE" = real ] && echo "$GEMINI_KEY_SOURCE" || echo none)"
    echo "key_fp=$fp"
    echo "started_at=$(date '+%Y-%m-%d %H:%M:%S')"
  } >"$STATE/llm-mode"
}

health_mode() { # /health の mode を real / mock に直して返す（取れなければ空）
  curl -s -m 3 "http://127.0.0.1:${API_PORT}/health" 2>/dev/null | sed -n 's/.*"mode" *: *"\([a-z]*\)".*/\1/p' | sed 's/^gemini$/real/'
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

# 記録した PID が、今も「このスクリプトが起動したプロセス」かを確かめる（PID が別のプロセスに再利用されていたら、偽にする）。
# 条件: 生きている / 自分のユーザーのもの / コマンドラインに、起動したコマンドの語（wrangler・uvicorn・next）が入っている
owned_pid() { # 名前 PID
  local pat
  case "$1" in worker) pat="wrangler" ;; api) pat="uvicorn" ;; web) pat="next" ;; *) return 1 ;; esac
  [ -n "${2:-}" ] && kill -0 "$2" 2>/dev/null || return 1
  [ "$(ps -o uid= -p "$2" 2>/dev/null | tr -d ' ')" = "$(id -u)" ] || return 1
  ps -o args= -p "$2" 2>/dev/null | grep -q -- "$pat"
}

alive() { [ -f "$(pid_file "$1")" ] && owned_pid "$1" "$(cat "$(pid_file "$1")")"; }

port_of() { case "$1" in worker) echo "$WORKER_PORT" ;; api) echo "$API_PORT" ;; web) echo "$WEB_PORT" ;; esac; }

listener_pids() { ss -H -ltnp "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 || true; }

descends_from() { # PID ルート: PID がルート自身か、その子孫か
  local p="$1" i=0
  while [ -n "$p" ] && [ "$p" -gt 1 ] 2>/dev/null && [ "$i" -lt 12 ]; do
    [ "$p" = "$2" ] && return 0
    p="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"
    i=$((i + 1))
  done
  return 1
}

# 「起動済みとして再利用してよい」条件。PID が生きている・自分のユーザーのもの・コマンドが想定どおり（alive）に加えて、
# そのポートの待ち受けが、記録した PID の子孫のもの（別のプロセスが同じ番号を使っていない）こと
reusable_proc() {
  alive "$1" || return 1
  local root lp
  root="$(cat "$(pid_file "$1")")"
  for lp in $(listener_pids "$(port_of "$1")"); do
    descends_from "$lp" "$root" && return 0
  done
  return 1
}

wait_for() { # 名前 URL 秒
  local i
  for i in $(seq 1 "$3"); do
    [ "$(http_code "$2")" = "200" ] && return 0
    sleep 1
  done
  die "$1 が $3 秒以内に起動しませんでした（log: $STATE/$1.log）。"
}

cmd_start() {
  llm_preflight # 実 API のときだけ。キー・承認が無ければ、ここで止まる（.dev.vars にも既存のプロセスにも触れない）
  cmd_vars
  cmd_check
  mkdir -p "$STATE" && chmod 700 "$STATE"
  local n
  for n in worker api web; do alive "$n" && die "$n はすでに起動しています（PID $(cat "$(pid_file "$n")")）。先に stop してください。"; done
  for n in "$WORKER_PORT" "$API_PORT" "$WEB_PORT"; do port_busy "$n" && die "ポート $n が使われています。"; done
  # ここから先で失敗したら、このスクリプトが起動した分だけ止める（起動済みのプロセスを止めないよう、上の確認のあとに置く）
  START_OK=0
  trap '[ "$START_OK" = 1 ] || cmd_stop >/dev/null 2>&1' EXIT

  # 認証の Worker（Better Auth）。Hyperdrive の接続先はローカルの Postgres
  # （`cd … && … &` と書くと、& が AND 連結の全体に掛かり、出力を握ったサブシェルが残る。cd は ; で分け、起動は単独のコマンドにする）
  (
    cd workers/api
    CLOUDFLARE_HYPERDRIVE_LOCAL_CONNECTION_STRING_HYPERDRIVE="$DATABASE_URL_LOCAL" WRANGLER_SEND_METRICS=false \
      nohup npx wrangler dev --local --port "$WORKER_PORT" >"$STATE/worker.log" 2>&1 </dev/null 9>&- &
    echo $! >"$(pid_file worker)"
  )
  wait_for worker "http://127.0.0.1:${WORKER_PORT}/health" 60

  # API。LLM は real（キーは API のプロセスの環境変数にだけ。export は組み込みなので、引数には出ない）か mock（キーを空に）。
  # .env の本番の値・有料キー・Vertex AI・モデルの上書きより、ここで渡す値が優先される。LangSmith へも送らない
  (
    if [ "$LLM_MODE" = real ]; then export GEMINI_API_KEY="$GKEY"; else export GEMINI_API_KEY=""; fi
    GEMINI_API_KEY_PAID= GOOGLE_GENAI_USE_VERTEXAI= EMBEDDING_USE_VERTEXAI= GEMINI_MODEL= GEMINI_FALLBACK_MODEL= \
      LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false D1_API_URL= D1_API_TOKEN= \
      DB_BACKEND=pg DATABASE_URL="$DATABASE_URL_LOCAL" ACCESS_MODE=off \
      AUTH_MODE=required AUTH_BASE_URL="$WEB_ORIGIN" AUTH_JWKS_URL="http://127.0.0.1:${WORKER_PORT}/api/auth/jwks" \
      nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" >"$STATE/api.log" 2>&1 </dev/null 9>&- &
    echo $! >"$(pid_file api)"
  )
  write_llm_mode
  GKEY=""
  wait_for api "http://127.0.0.1:${API_PORT}/health" 60
  [ "$(health_mode)" = "$LLM_MODE" ] || die "API の /health の mode（$(health_mode)）が、要求したモード（$LLM_MODE）と違います。起動したプロセスを止めます。"

  # 画面。frontend/.env.local の値（本番の URL など）より、ここで渡す値が優先される。
  # 待ち受けは 0.0.0.0（コンテナの外から届くように。ホストへは .devcontainer/docker-compose.yml が 127.0.0.1:3000 だけに公開する）
  (
    cd frontend
    NEXT_PUBLIC_AUTH_ENABLED=1 NEXT_PUBLIC_AUTH_BASE_URL= NEXT_PUBLIC_API_BASE_URL= NEXT_PUBLIC_STREAM_BASE_URL= \
      API_PROXY_TARGET="http://127.0.0.1:${API_PORT}" AUTH_PROXY_TARGET="http://127.0.0.1:${WORKER_PORT}" NEXT_TELEMETRY_DISABLED=1 \
      nohup npx next dev -H 0.0.0.0 -p "$WEB_PORT" >"$STATE/web.log" 2>&1 </dev/null 9>&- &
    echo $! >"$(pid_file web)"
  )
  wait_for web "http://127.0.0.1:${WEB_PORT}/" 90

  START_OK=1
  say "起動しました: ${WEB_ORIGIN}（API :${API_PORT}、Worker :${WORKER_PORT}）。log: $STATE/*.log"
  say "LLM: $(llm_label "$LLM_MODE" "$(record_field "$STATE/llm-mode" model)")（/health の mode=$(curl -s -m 3 "http://127.0.0.1:${API_PORT}/health" | sed -n 's/.*"mode" *: *"\([a-z]*\)".*/\1/p')）"
  say "画面のログインは Google だけです。会話の履歴を見るには、login のあと、docs/development.md の「ローカルの標準構成」の手順で Cookie を入れてください。"
  say "止めるとき: scripts/dev-local.sh stop"
}

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

autostart_body() { # サブシェルで set -e のまま動かす。失敗は呼び出し側（cmd_autostart）が記録する
  local n up=0 rec_mode rec_model rec_fp want_model="" want_fp=""
  llm_preflight # 実 API のとき、キー・承認が無ければここで止まる（既存のプロセスにも .dev.vars にも触れない）
  for n in worker api web; do alive "$n" && up=$((up + 1)); done
  if [ "$up" = 3 ]; then
    # すでに 3 つとも記録がある。「起動済み」として再利用してよいかを確かめる（PID だけで判断しない）。再利用できなければ、止めずに失敗する
    for n in worker api web; do
      reusable_proc "$n" || die "$n は PID（$(cat "$(pid_file "$n")")）が生きていますが、ポート $(port_of "$n") の待ち受けが、その PID の子孫のものと確認できません。起動済みとして再利用しません（止めていません）。確認して stop → start してください。"
    done
    rec_mode="$(record_field "$STATE/llm-mode" mode)"
    rec_model="$(record_field "$STATE/llm-mode" model)"
    rec_fp="$(record_field "$STATE/llm-mode" key_fp)"
    [ -n "$rec_mode" ] || die "起動中のプロセスのモードが不明です（$STATE/llm-mode が無い。古い方法で起動した可能性）。要求は ${LLM_MODE}。別モードのプロセスを起動済みとして再利用しないため、何もしません（止めていません）。stop → start --llm ${LLM_MODE} してください。"
    [ "$rec_mode" = "$LLM_MODE" ] || die "起動中のプロセスのモードは ${rec_mode}、要求は ${LLM_MODE} です。再利用しません（止めていません）。stop → start --llm ${LLM_MODE} してください。"
    [ "$(health_mode)" = "$LLM_MODE" ] || die "記録は ${rec_mode} ですが、API の /health の mode は $(health_mode) です。再利用しません（止めていません）。"
    if [ "$LLM_MODE" = real ]; then
      want_model="$(llm_model_name)"
      want_fp="$(key_fp "$GKEY")"
      { [ "$rec_model" = "$want_model" ] && [ "$rec_fp" = "$want_fp" ]; } || die "起動中の実 API のモデルまたはキーが、いまの設定と違います。再利用しません（止めていません）。stop → start してください。"
    fi
    GKEY=""
    wait_for web "http://127.0.0.1:${WEB_PORT}/" 90
    say "3 つとも起動中で、モード（$(llm_label "$rec_mode" "$rec_model")）も同じです。何もしません（PID: worker $(cat "$(pid_file worker)") / api $(cat "$(pid_file api)") / web $(cat "$(pid_file web)")）。"
    return 0
  fi
  if [ "$up" -gt 0 ]; then
    say "一部だけ起動中です（${up}/3）。記録した PID の持ち主を確かめて片付け、起動し直します。"
    cmd_stop
  fi
  cmd_start
}

cmd_autostart() { # 終了コードは常に 0。コンテナへの接続を妨げない
  mkdir -p "$STATE" && chmod 700 "$STATE"
  local log="$STATE/autostart.log" rc=0
  exec 9>"$STATE/autostart.lock"
  if ! flock -n 9; then
    echo "$(stamp) スキップ: 別の autostart が実行中です。" >>"$log"
    return 0
  fi
  echo "$(stamp) 開始" >>"$log"
  set +e
  # モードの決め方は start と同じ（--llm > DEV_LOCAL_LLM > 既定 real）。キーが取れなくても、モックには戻らない
  ( set -e; resolve_llm_mode "$@"; echo "$(stamp) LLM のモード（要求）: ${LLM_MODE}"; autostart_body ) >>"$log" 2>&1
  rc=$?
  set -e
  if [ "$rc" = 0 ]; then
    echo "$(stamp) 結果: 成功（${WEB_ORIGIN}）" >>"$log"
  else
    echo "$(stamp) 結果: 失敗（終了コード ${rc}）。理由は上の dev-local: の行。モックには戻していません。Secret の作成・上書き・register はしていません（.dev.vars は取得に成功したときだけ書き換わります）。直したら、scripts/dev-local.sh start。" >>"$log"
  fi
  return 0
}

cmd_status() {
  local n rec_mode rec_model actual
  for n in worker api web; do
    if reusable_proc "$n"; then
      echo "$n: 起動中（PID $(cat "$(pid_file "$n")")、ポート $(port_of "$n") の待ち受けを確認）"
    elif alive "$n"; then
      echo "$n: PID $(cat "$(pid_file "$n")") は生きていますが、ポート $(port_of "$n") の待ち受けを確認できません（起動中か、別のプロセスが使っています）"
    else
      echo "$n: 停止"
    fi
  done
  rec_mode="$(record_field "$STATE/llm-mode" mode)"
  rec_model="$(record_field "$STATE/llm-mode" model)"
  actual="$(health_mode)"
  if [ -n "$rec_mode" ]; then
    echo "LLM:            $(llm_label "$rec_mode" "$rec_model")（/health の mode: ${actual:-不明}$([ "$actual" = "$rec_mode" ] && echo '。記録と一致' || echo '。警告: 記録と違います')）"
  elif [ -n "$actual" ]; then
    echo "LLM:            記録なし（/health の mode: ${actual} → $([ "$actual" = real ] && echo 実API || echo モック)。古い方法で起動した可能性。autostart は再利用しません）"
  else
    echo "LLM:            API につながりません"
  fi
  echo "worker /health: $(http_code "http://127.0.0.1:${WORKER_PORT}/health")"
  echo "api /health:    $(curl -s -m 3 "http://127.0.0.1:${API_PORT}/health" 2>/dev/null || echo 'つながりません')"
  echo "web /:          $(http_code "http://127.0.0.1:${WEB_PORT}/")"
  if [ -f "$STATE/autostart.log" ]; then
    echo "autostart:      $(grep '結果:' "$STATE/autostart.log" | tail -n 1 || true)（詳細: $STATE/autostart.log）"
  else
    echo "autostart:      記録なし（Dev Containers 経由で起動していないか、まだ実行されていません）"
  fi
}

cmd_stop() {
  local n pid
  for n in web api worker; do
    if [ -f "$(pid_file "$n")" ]; then
      pid="$(cat "$(pid_file "$n")")"
      if owned_pid "$n" "$pid"; then
        killtree "$pid"
        say "$n を止めました（PID $pid）"
      else
        say "$n の記録（PID $pid）は、すでに無いか、別のプロセスです。止めずに、記録だけ消します。"
      fi
      rm -f "$(pid_file "$n")"
    fi
  done
  rm -f "$STATE/llm-mode"
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

read_env_gemini_key() { # .env の GEMINI_API_KEY の値だけを標準出力に出す（呼び出し側が変数に受ける。表示しない）
  python3 -I - "$ROOT/.env" <<'PY'
import sys

val = None
try:
    lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
except OSError:
    sys.exit("dev-local: .env を読めません。")
for line in lines:
    s = line.strip()
    if s.startswith("export "):
        s = s[7:].strip()
    if s.startswith("GEMINI_API_KEY="):
        v = s[len("GEMINI_API_KEY="):].strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        val = v
if not val or val == "your_api_key_here" or any(c.isspace() for c in val):
    sys.exit("dev-local: .env に GEMINI_API_KEY（空でない値）がありません、または空白を含んでいます。")
sys.stdout.write(val)
PY
}

cmd_register_gemini() { # ユーザーが自分の端末で実行する。.env の既存のキーを、値を表示せずに Infisical へ写す。新しいキーは作らない。本番の設定は変えない
  [ -t 0 ] || die "register-gemini は端末で実行してください。"
  local pid tmp want got
  if [ "${2:-}" != "--overwrite" ] && (infisical_py llmkey) >/dev/null 2>&1; then
    say "すでに Infisical（${INFISICAL_PROJECT_NAME} / ${INFISICAL_ENV} / /）に GEMINI_API_KEY があります。再利用してください（上書きは --overwrite。理由と影響を示した承認が要ります）。"
    return 0
  fi
  want="$(read_env_gemini_key)" || exit 1
  pid="$(infisical_py id)" || exit 1
  umask 077
  tmp="$(mktemp)" || die "一時ファイルを作れません。"
  trap "rm -f '$tmp' '$tmp.err'" EXIT
  printf 'GEMINI_API_KEY=%s\n' "$want" >"$tmp"
  say "登録先: Infisical ${INFISICAL_PROJECT_NAME}（ID ${pid}）/ 環境 ${INFISICAL_ENV} / パス /（GEMINI_API_KEY だけ。ほかの 6 キーには触れません）"
  infisical secrets set --file "$tmp" --projectId "$pid" --env "$INFISICAL_ENV" --path / --silent >/dev/null 2>"$tmp.err" \
    || die "登録に失敗しました（Infisical のログイン・プロジェクトの権限を確認）。"
  say "登録しました。取得し直して、.env の値と一致するかを確かめます（値は表示しません）。"
  got="$(infisical_py llmkey)" || exit 1
  [ "$got" = "$want" ] || die "取得した値が、.env の値と一致しません。Infisical の GEMINI_API_KEY を信用しないでください。"
  want=""; got=""
  (cmd_vars) >/dev/null 2>&1 || die "登録後に、ほかの 6 キーを取得できませんでした。Infisical の ${INFISICAL_PROJECT_NAME} を確認してください（.dev.vars は書き換えていません）。"
  say "一致を確認しました。ほかの 6 キーも取得できます。次に、gemini-key-info（課金状態の確認）と ack-gemini（承認）を実行してください。"
}

cmd_gemini_key_info() { # ユーザーが自分の端末で実行する。キーの所属プロジェクトと課金状態を、可能な範囲で調べる。キー・トークンは表示も保存もしない
  local key fp
  key="$(infisical_py llmkey)" || exit 1
  fp="$(key_fp "$key")"
  mkdir -p "$LOCAL_DIR" && chmod 700 "$LOCAL_DIR"
  GKEY_IN="$key" FP="$fp" OUT="$KEYINFO_FILE" QUOTA_PROJECT="${GCP_QUOTA_PROJECT:-docunavi-app}" python3 -I - <<'PY'
import json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

key, fp, out, quota = os.environ["GKEY_IN"], os.environ["FP"], os.environ["OUT"], os.environ["QUOTA_PROJECT"]
now = time.strftime("%Y-%m-%d %H:%M")


def save(project_id, number, billing):
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"key_fp={fp}\nproject_id={project_id}\nproject_number={number}\nbilling_enabled={billing}\nchecked_at={now}\n")


def unknown(reason):
    save("", "", "不明")
    print(f"dev-local: キーの所属プロジェクトと課金状態は、CLI では確認できませんでした（{reason}）。")
    print("  AI Studio の画面で確認してください: https://aistudio.google.com/apikey")
    print("  キーごとにプロジェクトと Plan（Free / Paid）が出ます（この URL は実機で確認していません）。確認できるまで、「未確認」として扱います。")
    sys.exit(0)


# ユーザー自身の権限（なりすましの設定を外した環境）で、トークンを取る。トークンは変数に受けるだけで、表示しない
env = {k: v for k, v in os.environ.items() if k not in ("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", "GKEY_IN")}
try:
    r = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, env=env, timeout=30, stdin=subprocess.DEVNULL)
except Exception as e:
    unknown(f"gcloud を実行できません: {type(e).__name__}")
token = r.stdout.strip()
if r.returncode != 0 or not token:
    unknown("gcloud のアクセストークンを取れません（gcloud auth login が必要かもしれません）")


def get(url):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "x-goog-user-project": quota})
    try:
        return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        unknown(f"HTTP {e.code}。権限が足りない可能性があります（応答の本文は出しません）")
    except Exception as e:
        unknown(f"通信の失敗: {type(e).__name__}")


looked = get("https://apikeys.googleapis.com/v2/keys:lookupKeyName?" + urllib.parse.urlencode({"keyString": key}))
parts = str(looked.get("name", "")).split("/")
number = parts[1] if len(parts) > 1 and parts[0] == "projects" else ""
if not number:
    unknown("キーの所属プロジェクトを特定できません")
info = get(f"https://cloudbilling.googleapis.com/v1/projects/{number}/billingInfo")
enabled = info.get("billingEnabled")
billing = "true" if enabled is True else "false" if enabled is False else "不明"
project_id = str(info.get("projectId", ""))
save(project_id, number, billing)
print(f"dev-local: キーの所属プロジェクト: {project_id or '不明'}（番号 {number}）")
print(f"dev-local: 課金の有効化（billingEnabled）: {billing}")
if billing == "true":
    print("  課金が有効なプロジェクトのキーは、有料枠（従量課金）の可能性が高いです。Plan は AI Studio（https://aistudio.google.com/apikey）で最終確認してください。")
elif billing == "false":
    print("  課金が無効なプロジェクトのキーは、無料枠の可能性が高いです（入力は製品の改善に使われます。「無料だから安全」とは扱いません）。")
print(f"dev-local: 結果を {out} に保存しました（キーの値・トークンは含みません）。")
PY
  key=""
}

cmd_ack_gemini() { # ユーザーが自分の端末で実行する。概算費用・上限・無料枠の有無を表示し、承認を記録する（実 API の起動の前提）
  [ -t 0 ] || die "ack-gemini は端末で実行してください（承認の入力を読みます）。"
  local key fp model ans
  key="$(infisical_py llmkey)" || exit 1
  fp="$(key_fp "$key")"
  key=""
  model="$(llm_model_name)"
  print_gemini_summary "$fp" "$model"
  read -rp "この内容を確認し、実 API を使って起動することを承認しますか（承認するときは yes と入力）: " ans
  [ "$ans" = yes ] || die "承認されませんでした。何も記録していません（実 API は起動しません）。"
  mkdir -p "$LOCAL_DIR" && chmod 700 "$LOCAL_DIR"
  umask 077
  {
    echo "key_fp=$fp"
    echo "model=$model"
    echo "billing=$([ -f "$KEYINFO_FILE" ] && [ "$(record_field "$KEYINFO_FILE" key_fp)" = "$fp" ] && record_field "$KEYINFO_FILE" billing_enabled || echo 未確認)"
    echo "approved_at=$(date '+%Y-%m-%d %H:%M:%S')"
  } >"$ACK_FILE"
  say "承認を記録しました（$ACK_FILE。キーの値は含みません）。キーかモデルが変わると、もう一度承認が要ります。"
}

cmd_with_key() { # with-key -- コマンド…: 実 API のテスト用。GEMINI_API_KEY を、そのコマンドのプロセスの環境変数にだけ渡す
  shift
  [ "${1:-}" = "--" ] && shift
  [ $# -gt 0 ] || die "使い方: scripts/dev-local.sh with-key -- <コマンド>"
  require_gemini
  # 有料キー・Vertex AI は空にする（.env の値で、課金の経路が変わらないように）。export は組み込みなので、キーは引数に出ない
  export GEMINI_API_KEY="$GKEY" GEMINI_API_KEY_PAID= GOOGLE_GENAI_USE_VERTEXAI= EMBEDDING_USE_VERTEXAI=
  GKEY=""
  exec "$@"
}

case "${1:-}" in
  check) cmd_check ;;
  start) shift; resolve_llm_mode "$@"; cmd_start ;;
  autostart) shift; cmd_autostart "$@" ;;
  vars) cmd_vars ;;
  register) cmd_register "$@" ;;
  register-gemini) cmd_register_gemini "$@" ;;
  gemini-key-info) cmd_gemini_key_info ;;
  ack-gemini) cmd_ack_gemini ;;
  with-key) cmd_with_key "$@" ;;
  status) cmd_status ;;
  stop) cmd_stop ;;
  login) cmd_login ;;
  cookie) cmd_cookie ;;
  *) sed -n '2,/^set -euo/{/^set -euo/!p}' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
