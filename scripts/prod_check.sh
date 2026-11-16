#!/usr/bin/env bash
# 本番の確認を、テスト専用ユーザーで 1 コマンドにまとめる。**ホストのターミナルで、あなたの ID で実行する**
# （本番 Worker の Secret を書く wrangler のログインが、ホストにしか無いため。コンテナでは動かない）。
#
#   ./scripts/prod_check.sh --chat                 # 資格なし 403 → 一時的に資格を付けて 200（scripts/prod_membership_check.sh）
#   ./scripts/prod_check.sh --stream               # /chat/stream で 1 回送り、出典と引用を検査（scripts/prod_smoke_chat.py --stream）
#   ./scripts/prod_check.sh --screen               # 本番の画面（Playwright）で、時刻・太字・色・吹き出しを見て、スクリーンショットを残す
#   ./scripts/prod_check.sh --stream --screen      # 続けて実行できる（--chat、--stream、--screen の順）
#   ./scripts/prod_check.sh --browse <名前>        # 本番の画面を、テスト専用ユーザーでログインして操作・撮影する（scripts/prod_scenarios/<名前>.cjs。何度でも指定できる）
#                                                  #   chat-2turns（2 往復。課金 2 回）、documents（資料一覧。課金なし）、screen-check（時刻・太字の検査。課金 1 回）
#                                                  #   画像の名前を変えるには --browse chat-2turns=readme-chat（.playwright-mcp/readme-chat.png）
#   --scenario-args '<JSON>'   --browse のシナリオへ渡す引数（全シナリオ共通）。--viewport 1280x800、--theme light|dark も指定できる
#   --question "…"   --stream / --screen / --browse の質問（既定: 寮の門限は何時ですか？）。質問は 1 回だけ（実 API・課金）
#   -y               確認を省く（release.sh が、切り替え前の確認のあとに呼ぶ）
#   PREVIEW_DRY_RUN=1 ./scripts/prod_check.sh …   # 書き込みは実行せず、コマンドだけ表示（読み取りは実行する）
#
# 流れ: テスト用ログイン（Worker docunavi-api の TEST_LOGIN_EMAIL / TEST_LOGIN_TOKEN）を一時的に有効にする →
#   （--stream・--screen は）テストユーザーに利用資格を一時的に付ける → 確認 → 資格を外す → テスト用ログインを削除する。
#   後始末は trap で、失敗・中断しても必ず行う。Infisical の値は、開発コンテナからパイプで wrangler に渡す（表示しない）。
# 副作用: 本番 DB の app_members にテストユーザーの行を一時的に足して外す（access_audit に 2 行残る）。本番の LLM を 1 回ずつ呼ぶ（課金）。
# 環境変数: DEVCONTAINER（既定 docunavi-devcontainer）、WRANGLER（既定 wrangler@4.158.0）、AUTH_WORKER（既定 docunavi-api）、
#   MEMBERSHIP_CHECK_EMAIL（既定 e2e-test@docunavi.invalid）
set -euo pipefail

# 書き込みは本人の ID で行う（コンテナの既定は閲覧専用のなりすまし）
unset CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT

DEVCONTAINER="${DEVCONTAINER:-docunavi-devcontainer}"
WRANGLER="${WRANGLER:-wrangler@4.158.0}"
AUTH_WORKER="${AUTH_WORKER:-docunavi-api}"
EMAIL="${MEMBERSHIP_CHECK_EMAIL:-e2e-test@docunavi.invalid}"
DRY_RUN="${PREVIEW_DRY_RUN:-}"

die() {
  echo "エラー: $*" >&2
  exit 2
}

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

confirm() {
  [[ "$assume_yes" == true || -n "$DRY_RUN" ]] && return 0
  local ans
  read -r -p "$1 [y/N] " ans
  [[ "$ans" == y || "$ans" == Y ]] || die "中止しました"
}

# ---- 引数（何かを実行する前に、すべて検査する）
do_chat=false
do_stream=false
do_screen=false
scenarios=""
scenario_args="{}"
viewport="1280x800"
theme="light"
assume_yes=false
question="寮の門限は何時ですか？"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help) usage 0 ;;
    --chat) do_chat=true ;;
    --stream) do_stream=true ;;
    --screen) do_screen=true ;;
    --browse)
      [[ $# -ge 2 && "$2" =~ ^[a-z0-9-]+(=[a-z0-9-]+)?$ ]] || die "--browse にはシナリオ名（小文字・数字・ハイフン。画像名を変えるなら 名前=画像名）が要ります"
      [[ -f "$(dirname "$0")/prod_scenarios/${2%%=*}.cjs" ]] || die "シナリオがありません: scripts/prod_scenarios/${2%%=*}.cjs"
      scenarios="$scenarios $2"
      shift
      ;;
    --scenario-args)
      [[ $# -ge 2 ]] || die "--scenario-args には JSON が要ります"
      scenario_args="$2"
      shift
      ;;
    --viewport)
      [[ $# -ge 2 && "$2" =~ ^[0-9]{3,4}x[0-9]{3,4}$ ]] || die "--viewport は 1280x800 の形です"
      viewport="$2"
      shift
      ;;
    --theme)
      [[ $# -ge 2 && ( "$2" == light || "$2" == dark ) ]] || die "--theme は light か dark です"
      theme="$2"
      shift
      ;;
    -y | --yes) assume_yes=true ;;
    --question)
      [[ $# -ge 2 && -n "$2" ]] || die "--question には質問文が要ります"
      question="$2"
      shift
      ;;
    *) die "知らない引数です: $1" ;;
  esac
  shift
done
if [[ "$do_chat" == false && "$do_stream" == false && "$do_screen" == false && -z "$scenarios" ]]; then usage 2; fi
python3 -c 'import json,sys; json.loads(sys.argv[1])' "$scenario_args" 2>/dev/null || die "--scenario-args が JSON ではありません"
[[ ${#question} -le 200 ]] || die "質問が長すぎます（200 文字まで）"
[[ "$EMAIL" =~ ^[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+$ ]] || die "MEMBERSHIP_CHECK_EMAIL の形が違います"

for c in docker npx; do command -v "$c" >/dev/null || die "$c が見つかりません"; done
[[ "$(docker inspect -f '{{.State.Running}}' "$DEVCONTAINER" 2>/dev/null)" == true ]] || die "開発コンテナ $DEVCONTAINER が動いていません"

# 開発コンテナで実行する（作業場所と Infisical の ID は、毎回ここで決める。作業フォルダーの違いによる事故を防ぐ）。
# 引数: コンテナで実行するシェルの 1 行。質問は環境変数 QUESTION で渡す（シェルの引用符に混ぜない）。
cx() {
  docker exec -u vscode -e QUESTION="$question" -e SHOT="${SHOT:-}" -e SCENARIO_ARGS="$scenario_args" -e VIEWPORT="$viewport" -e THEME="$theme" "$DEVCONTAINER" bash -lc \
    "set -o pipefail; cd /workspaces/docunavi && set -a && . .dev-local/infisical-ids.env && set +a && $1"
}
# 書き込み（DRY_RUN では表示だけ）
wcx() {
  if [[ -n "$DRY_RUN" ]]; then
    echo "+ （$DEVCONTAINER で）$1"
  else
    cx "$1"
  fi
}
admin_cmd() { # <member のサブコマンド…>。DB_BACKEND=pg が要る。INF の行は消す
  echo "DB_BACKEND=pg infisical run --silent --projectId \"\$INFISICAL_PROJECT_ID_PROD\" --env prod -- .venv/bin/python -m app.access_admin member $* 2>&1 | awk '!/ INF /'"
}

modes=""
[[ "$do_chat" == true ]] && modes="$modes --chat"
[[ "$do_stream" == true ]] && modes="$modes --stream"
[[ "$do_screen" == true ]] && modes="$modes --screen"
for s in $scenarios; do modes="$modes --browse $s"; done

# ---- 計画と確認
echo "=== 本番の確認${modes} ==="
echo "- 本番 Worker $AUTH_WORKER のテスト用ログインを、一時的に有効にする（終わったら必ず削除）"
echo "- 本番 DB のテストユーザーに、利用資格を一時的に付けて外す（access_audit に記録が残る）"
if [[ "$do_chat" == true ]]; then echo "- --chat: 資格なしで 403 → 資格ありで 200（本番の LLM を 1 回呼ぶ）"; fi
if [[ "$do_stream" == true ]]; then echo "- --stream: /chat/stream に 1 回送る（質問: ${question}）"; fi
if [[ "$do_screen" == true ]]; then echo "- --screen: 本番の画面で 1 回送る（質問: ${question}）。スクリーンショットを .playwright-mcp/ に残す"; fi
for s in $scenarios; do
  case "${s%%=*}" in
    chat-2turns) echo "- --browse $s: 本番の画面で 2 往復する（質問: ${question} と、続けて聞く候補の 1 つ。実 API・課金が 2 回）。画像を .playwright-mcp/ に残す。ビューポート ${viewport}、テーマ ${theme}、引数 ${scenario_args}" ;;
    screen-check) echo "- --browse $s: 本番の画面で 1 回送る（質問: ${question}。実 API・課金が 1 回）。画像を .playwright-mcp/ に残す" ;;
    *) echo "- --browse $s: 本番の画面を操作して撮る（scripts/prod_scenarios/${s%%=*}.cjs を読んで、課金（実 API）が無いか確かめてから実行）。画像を .playwright-mcp/ に残す。ビューポート ${viewport}、テーマ ${theme}、引数 ${scenario_args}" ;;
  esac
done
confirm "実行してよいですか?"

# ---- 後始末（trap。失敗・中断でも必ず行う）
login_touched=false
member_added=false

cleanup() {
  local rc=$?
  trap - EXIT
  set +e
  if [[ "$member_added" == true ]]; then
    echo "[片付け] 利用資格を外す"
    wcx "$(admin_cmd remove --email "$EMAIL" --apply) | tail -1"
    member_added=false
  fi
  if [[ "$login_touched" == true ]]; then
    local k n
    for n in 1 2 3; do
      for k in TEST_LOGIN_TOKEN TEST_LOGIN_EMAIL; do
        if [[ -n "$DRY_RUN" ]]; then
          echo "+ npx $WRANGLER secret delete $k --name $AUTH_WORKER"
        else
          printf 'y\n' | (cd /tmp && npx "$WRANGLER" secret delete "$k" --name "$AUTH_WORKER") >/dev/null 2>&1
        fi
      done
      [[ -n "$DRY_RUN" ]] && break
      # 削除が反映されたか確かめる（残っていたら、もう一度消す）
      if ! (cd /tmp && npx "$WRANGLER" secret list --name "$AUTH_WORKER" 2>/dev/null) | grep -q 'TEST_LOGIN'; then
        echo "[片付け] テスト用ログインを無効にしました（Worker の Secret に TEST_LOGIN_* は無い）"
        break
      fi
      if [[ "$n" == 3 ]]; then
        echo "警告: Worker $AUTH_WORKER に TEST_LOGIN_* が残っています。手で削除してください: workers/api/test-login.sh off" >&2
        rc=1
      fi
      sleep 3
    done
    login_touched=false
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ---- テスト用ログインの有効化
login_touched=true
for k in TEST_LOGIN_EMAIL TEST_LOGIN_TOKEN; do
  if [[ -n "$DRY_RUN" ]]; then
    echo "+ （$DEVCONTAINER で Infisical dev の $k を読み、パイプで）npx $WRANGLER secret put $k --name $AUTH_WORKER"
    continue
  fi
  docker exec -u vscode -e K="$k" "$DEVCONTAINER" bash -lc '
    cd /workspaces/docunavi && set -a && . .dev-local/infisical-ids.env && set +a
    infisical export --projectId "$INFISICAL_PROJECT_ID_DEV" --env dev --path / --format json 2>/dev/null \
      | python3 -c "import sys,json,os; d={e[\"key\"]:e[\"value\"] for e in json.load(sys.stdin)}; sys.stdout.write(d[os.environ[\"K\"]])"' \
    | (cd /tmp && npx "$WRANGLER" secret put "$k" --name "$AUTH_WORKER" >/dev/null 2>&1) \
    || die "テスト用ログインを有効にできませんでした: $k"
done
echo "[有効化] テスト用ログインを一時的に有効にしました"

# 反映の待ち（Secret の反映に数秒〜十数秒かかることがある）。ログインだけ試す（/chat は呼ばない。課金なし）
if [[ -z "$DRY_RUN" ]]; then
  ready=false
  for i in 1 2 3 4 5 6; do
    out="$(cx "infisical sm run --silent --projectId \"\$INFISICAL_PROJECT_ID_DEV\" --env=dev -- python3 scripts/prod_smoke_chat.py 2>&1 | awk '!/ INF /'" || true)"
    if grep -q '^sign-in: 200' <<<"$out"; then
      ready=true
      break
    fi
    sleep 10
  done
  [[ "$ready" == true ]] || die "テスト用ログインでサインインできません（反映待ちの上限）。片付けて終わります"
fi

# ---- 確認
failed=0
add_member() {
  [[ "$member_added" == true ]] && return 0
  # 出力を変数に取ってから判定する（grep -q で打ち切ると、pipefail で SIGPIPE が失敗に見える）
  wcx "b=\$($(admin_cmd show --email "$EMAIL")); case \"\$b\" in *'利用資格: なし'*) ;; *) echo '事前に資格が付いています。中断します'; exit 1 ;; esac"
  wcx "$(admin_cmd add --email "$EMAIL" --note prod-check --apply)"
  member_added=true
}
remove_member() {
  [[ "$member_added" == true ]] || return 0
  wcx "$(admin_cmd remove --email "$EMAIL" --apply) | tail -1"
  member_added=false
}

if [[ "$do_chat" == true ]]; then
  echo
  echo "--- --chat（資格なし 403 → 資格あり 200）"
  wcx "bash scripts/prod_membership_check.sh" || failed=1
fi

if [[ "$do_stream" == true || "$do_screen" == true || -n "$scenarios" ]]; then
  add_member
  # 資格の判定は各インスタンスが短時間（既定 60 秒）控える。直前に「資格なし」を控えていなければ待たなくてよい
fi

if [[ "$do_stream" == true ]]; then
  echo
  echo "--- --stream"
  wcx "infisical sm run --silent --projectId \"\$INFISICAL_PROJECT_ID_DEV\" --env=dev -- python3 scripts/prod_smoke_chat.py --stream --question \"\$QUESTION\" 2>&1 | awk '!/ INF /'" || failed=1
fi

if [[ "$do_screen" == true ]]; then
  echo
  echo "--- --screen"
  SHOT="/workspaces/docunavi/.playwright-mcp/prod-screen-check-$(date +%m%d-%H%M).png"
  wcx "mkdir -p .playwright-mcp && cd frontend && NODE_PATH=\$PWD/node_modules infisical sm run --silent --projectId \"\$INFISICAL_PROJECT_ID_DEV\" --env=dev -- node ../scripts/prod_screen_check.cjs 2>&1 | awk '!/ INF /'" || failed=1
  [[ -n "$DRY_RUN" ]] || echo "スクリーンショット: ${SHOT#/workspaces/docunavi/}（開発コンテナ・ホストの docunavi/ 直下）"
fi

for s in $scenarios; do
  echo
  echo "--- --browse $s"
  sname="${s%%=*}"
  sshot=""
  [[ "$s" == *=* ]] && sshot="${s#*=}"
  wcx "cd frontend && NODE_PATH=\$PWD/node_modules SCENARIO=$sname SHOT_NAME=$sshot infisical sm run --silent --projectId \"\$INFISICAL_PROJECT_ID_DEV\" --env=dev -- node ../scripts/prod_browse.cjs 2>&1 | awk '!/ INF /'" || failed=1
done

remove_member
echo
if [[ "$failed" == 0 ]]; then
  echo "結果: 合格"
else
  echo "結果: 不合格（上の出力を見てください）"
fi
exit "$failed"
