#!/usr/bin/env bash
# タグ push 配備（deploy/<タグ>）のあとの、本番への切り替えを 1 本にまとめる。
# **ホストのターミナルで、あなたの ID で実行する**（書き込みの gcloud・wrangler のログインがホストにあるため。コンテナでは動かない）。
#
#   ./scripts/release.sh <ビルド ID>              # 承認待ちなら承認 → 完了待ち → 確認 → 切り替え → 資格確認 → ピン留め
#   ./scripts/release.sh <ビルド ID> --no-check   # 本番の /chat の自動確認（資格確認）を省く
#   PREVIEW_DRY_RUN=1 ./scripts/release.sh <ビルド ID>   # 書き込みは実行せず、コマンドだけ表示（読み取りは実行する）
#
# 流れ:
#   1. ビルドの状態を見る。承認待ち（PENDING）なら、確認して承認する。完了まで待つ（SUCCESS 以外は止まる）
#   2. 新リビジョンが 0%、本番が別のリビジョンで 100% であることと、タグ付き URL の /health が 200 であることを確かめる
#   3. 計画（切り替え・資格確認の副作用・ピン留め・戻し方）を示し、**ここで 1 回だけ**確認する
#   4. 100% へ切り替える（scripts/preview.sh promote）→ 本番の /health
#   5. 資格確認: 本番 Worker のテスト用ログインを一時的に有効にし、開発コンテナで scripts/prod_membership_check.sh を実行する。
#      終わったら（失敗・中断でも）テスト用ログインの Secret を削除する（trap）
#   6. 通ったら、ピン留めを付け替える（pin-prod = 新イメージ、pin-rollback = 切り替え前のイメージ）
#   7. 新リビジョン・戻し先・戻すコマンドを表示する。失敗したときは、戻すかどうかを聞く（自動では戻さない）
#
# 判断が要るのは切り替えだけ（手順 3）。Secret・トークンの値は表示しない（Infisical の値は、コンテナからパイプで wrangler に渡す）。
# 本番のトラフィックは、この確認のあとにだけ動く。ガード（フック・権限）は外さない。
# 環境変数: PROJECT_ID（既定 docunavi-app）、REGION（既定 asia-northeast1）、SERVICE（既定 docunavi）、AR_REPO（既定 cloud-run-source-deploy）、
#   DEVCONTAINER（既定 docunavi-devcontainer）、WRANGLER（既定 wrangler@4.158.0）、AUTH_WORKER（既定 docunavi-api）
set -euo pipefail

# 書き込みなので、本人の ID で動かす（コンテナの既定は docunavi-viewer へのなりすまし。書き込みは 403 になる）
unset CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ID="${PROJECT_ID:-docunavi-app}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-docunavi}"
AR_REPO="${AR_REPO:-cloud-run-source-deploy}"
DEVCONTAINER="${DEVCONTAINER:-docunavi-devcontainer}"
WRANGLER="${WRANGLER:-wrangler@4.158.0}"
AUTH_WORKER="${AUTH_WORKER:-docunavi-api}"
DRY_RUN="${PREVIEW_DRY_RUN:-}"
IMAGE_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE}"

die() {
  echo "エラー: $*" >&2
  exit 2
}

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# 書き込み。DRY_RUN のときは、実行せずに表示する
w() {
  if [[ -n "$DRY_RUN" ]]; then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

confirm() {
  [[ -n "$DRY_RUN" ]] && return 0
  local ans
  read -r -p "$1 [y/N] " ans
  [[ "$ans" == y || "$ans" == Y ]] || die "中止しました"
}

# ---- 引数（何かを実行する前に、すべて検査する）
build_id=""
do_check=true
for a in "$@"; do
  case "$a" in
    -h | --help) usage 0 ;;
    --no-check) do_check=false ;;
    -*) die "知らない引数です: $a" ;;
    *)
      [[ -z "$build_id" ]] || die "ビルド ID は 1 つだけ指定してください"
      build_id="$a"
      ;;
  esac
done
[[ -n "$build_id" ]] || { usage 2; }
[[ "$build_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || die "ビルド ID の形が違います: $build_id"

for c in gcloud curl python3; do command -v "$c" >/dev/null || die "$c が見つかりません"; done
if [[ "$do_check" == true ]]; then
  for c in docker npx; do command -v "$c" >/dev/null || die "$c が見つかりません（資格確認に要ります。省くなら --no-check）"; done
  [[ "$(docker inspect -f '{{.State.Running}}' "$DEVCONTAINER" 2>/dev/null)" == true ]] \
    || die "開発コンテナ $DEVCONTAINER が動いていません（資格確認に要ります。省くなら --no-check）"
fi

# ---- 1. ビルド
IFS=$'\t' read -r b_status b_tag b_commit < <(
  gcloud builds describe "$build_id" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(status,substitutions.TAG_NAME,substitutions.COMMIT_SHA)'
)
[[ "$b_tag" =~ ^deploy/[a-z][a-z0-9-]*$ ]] || die "ビルドのタグが deploy/<小文字・数字・ハイフン> ではありません: ${b_tag:-（なし）}"
run_tag="${b_tag#deploy/}"
echo "ビルド: $build_id  状態: $b_status  タグ: $b_tag  コミット: ${b_commit:0:7}"

if [[ "$b_status" == PENDING ]]; then
  echo "承認待ちです。承認すると、ビルドが走り、新リビジョンが 0% で配備されます（本番は動きません）。"
  confirm "ビルドを承認しますか?"
  w gcloud alpha builds approve "$build_id" --location "$REGION" --project "$PROJECT_ID"
  b_status=WORKING
fi

if [[ -z "$DRY_RUN" ]]; then
  waited=0
  while [[ "$b_status" == WORKING || "$b_status" == QUEUED || "$b_status" == PENDING ]]; do
    ((waited < 1800)) || die "ビルドが 30 分たっても終わりません。状態: $b_status"
    sleep 15
    waited=$((waited + 15))
    b_status="$(gcloud builds describe "$build_id" --region "$REGION" --project "$PROJECT_ID" --format='value(status)')"
  done
fi
if [[ "$b_status" != SUCCESS && -z "$DRY_RUN" ]]; then
  die "ビルドが成功していません。状態: $b_status"
fi

# ---- 2. トラフィックと /health
svc_json="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format=json)"
IFS=' ' read -r cur_rev new_rev new_pct new_url svc_url < <(
  printf '%s' "$svc_json" | python3 -c '
import json, sys
d = json.load(sys.stdin)
tag = sys.argv[1]
traffic = d.get("status", {}).get("traffic", [])
cur = next((t["revisionName"] for t in traffic if t.get("percent") == 100), "-")
new = next((t for t in traffic if t.get("tag") == tag), {})
print(cur, new.get("revisionName", "-"), new.get("percent", 0), new.get("url", "-"), d.get("status", {}).get("url", "-"))
' "$run_tag"
)
[[ "$new_rev" != - ]] || die "タグ $run_tag のリビジョンが見つかりません（配備が終わっていないか、prune で消えた可能性）"
[[ "$cur_rev" != - ]] || die "100% のリビジョンが 1 つに決まりません（戻し先を決められません）"
if [[ "$new_rev" == "$cur_rev" ]]; then
  if [[ -z "$DRY_RUN" ]]; then
    die "$new_rev は既に 100% です（切り替え済み）。戻し先を決められないので止めます"
  fi
  echo "注意（DRY_RUN）: $new_rev は既に 100% です。以降は、切り替え前の状態として表示します。"
elif [[ "$new_pct" != 0 ]]; then
  die "新リビジョン $new_rev が ${new_pct}% です。0% のはずです（想定外のトラフィック）。止めます"
fi

digest_of() {
  local image
  image="$(gcloud run revisions describe "$1" --project "$PROJECT_ID" --region "$REGION" --format='value(spec.containers[0].image)')"
  [[ "$image" == *@sha256:* ]] || die "リビジョン $1 のイメージがダイジェスト指定ではありません: $image"
  echo "${image##*@}"
}
cur_digest="$(digest_of "$cur_rev")"
new_digest="$(digest_of "$new_rev")"

health() { # <URL> → 200 なら 0
  local code="" i
  for i in 1 2 3 4; do
    code="$(curl -s -o /dev/null -m 20 -w '%{http_code}' "$1/health" || true)"
    [[ "$code" == 200 ]] && return 0
    sleep 5
  done
  echo "  /health が $code です: $1" >&2
  return 1
}
health "$new_url" || die "新リビジョンのタグ付き URL の /health が 200 ではありません。切り替えません"
echo "新リビジョン ${new_rev}（0%）の /health: 200"

# ---- 3. 計画と、唯一の確認
rollback_cmd="./scripts/preview.sh rollback $cur_rev"
echo
echo "=== 計画 ==="
echo "切り替え前（戻し先）: $cur_rev  ${cur_digest:0:19}…"
echo "切り替え後          : $new_rev  ${new_digest:0:19}…（タグ ${run_tag}）"
echo "1. 100% を $new_rev へ移す（scripts/preview.sh promote ${run_tag}）→ 本番の /health"
if [[ "$do_check" == true ]]; then
  echo "2. 資格確認: 本番 Worker $AUTH_WORKER のテスト用ログイン（TEST_LOGIN_EMAIL / TEST_LOGIN_TOKEN）を一時的に有効にする。"
  echo "   本番 DB のテストユーザーに資格を一時的に付けて外す（access_audit に 2 行残る）。本番の LLM を 1 回呼ぶ（課金）。終わったら Secret を削除する"
else
  echo "2. 資格確認は省く（--no-check）"
fi
echo "3. ピン留め: pin-prod → ${new_digest:0:19}…、pin-rollback → ${cur_digest:0:19}…（通ったあと）"
echo "戻すコマンド: $rollback_cmd"
echo
confirm "切り替えてよいですか?"

offer_rollback() {
  echo "戻すコマンド: $rollback_cmd" >&2
  if [[ -z "$DRY_RUN" ]]; then
    local ans
    read -r -p "いま戻しますか? [y/N] " ans
    if [[ "$ans" == y || "$ans" == Y ]]; then
      "$HERE/preview.sh" rollback "$cur_rev" -y
    fi
  fi
}

# ---- 4. 切り替え
"$HERE/preview.sh" promote "$run_tag" -y
if [[ -z "$svc_url" || "$svc_url" == - ]]; then svc_url="$new_url"; fi
if ! health "$svc_url"; then
  echo "本番の /health が 200 ではありません。" >&2
  offer_rollback
  exit 1
fi
echo "本番の /health: 200"

# ---- 5. 資格確認（テスト用ログインは、必ず戻す）
login_touched=false
login_off() {
  local k
  [[ "$login_touched" == true ]] || return 0
  for k in TEST_LOGIN_TOKEN TEST_LOGIN_EMAIL; do
    if [[ -n "$DRY_RUN" ]]; then
      echo "+ npx $WRANGLER secret delete $k --name $AUTH_WORKER"
      continue
    fi
    printf 'y\n' | (cd /tmp && npx "$WRANGLER" secret delete "$k" --name "$AUTH_WORKER") >/dev/null 2>&1 \
      || echo "（$k は既にありませんでした、または削除に失敗しました。下の確認を見てください）" >&2
  done
  login_touched=false
  if [[ -z "$DRY_RUN" ]]; then
    if (cd /tmp && npx "$WRANGLER" secret list --name "$AUTH_WORKER" 2>/dev/null) | grep -q 'TEST_LOGIN'; then
      echo "警告: Worker $AUTH_WORKER に TEST_LOGIN_* が残っています。手で削除してください: workers/api/test-login.sh off" >&2
    else
      echo "テスト用ログインを無効にしました（Worker の Secret に TEST_LOGIN_* は無い）"
    fi
  fi
}
trap login_off EXIT

login_on() {
  local k
  login_touched=true
  for k in TEST_LOGIN_EMAIL TEST_LOGIN_TOKEN; do
    if [[ -n "$DRY_RUN" ]]; then
      echo "+ （$DEVCONTAINER で Infisical dev の $k を読み、パイプで）npx $WRANGLER secret put $k --name $AUTH_WORKER"
      continue
    fi
    docker exec -u vscode -e K="$k" "$DEVCONTAINER" bash -lc '
      cd /workspaces/docunavi; set -a; . .dev-local/infisical-ids.env; set +a
      infisical export --projectId "$INFISICAL_PROJECT_ID_DEV" --env dev --path / --format json 2>/dev/null \
        | python3 -c "import sys,json,os; d={e[\"key\"]:e[\"value\"] for e in json.load(sys.stdin)}; sys.stdout.write(d[os.environ[\"K\"]])"' \
      | (cd /tmp && npx "$WRANGLER" secret put "$k" --name "$AUTH_WORKER" >/dev/null)
  done
}

check_rc=0
if [[ "$do_check" == true ]]; then
  echo
  echo "--- 資格確認（テスト用ログインを一時的に有効にします）"
  login_on
  if [[ -n "$DRY_RUN" ]]; then
    echo "+ docker exec -u vscode $DEVCONTAINER bash -lc 'cd /workspaces/docunavi && bash scripts/prod_membership_check.sh'"
  else
    docker exec -u vscode "$DEVCONTAINER" bash -lc 'cd /workspaces/docunavi && bash scripts/prod_membership_check.sh' || check_rc=$?
  fi
  login_off
  trap - EXIT
  if [[ "$check_rc" != 0 ]]; then
    echo "資格確認が合格しませんでした（終了コード ${check_rc}）。" >&2
    offer_rollback
    exit 1
  fi
fi

# ---- 6. ピン留めの付け替え（本番・戻し先のイメージが、クリーンアップで消えないように）
echo
echo "--- ピン留め"
w gcloud artifacts docker tags add "$IMAGE_REPO@$new_digest" "$IMAGE_REPO:pin-prod"
w gcloud artifacts docker tags add "$IMAGE_REPO@$cur_digest" "$IMAGE_REPO:pin-rollback"

# ---- 7. 結果
echo
echo "=== 完了 ==="
echo "本番: ${new_rev}（100%）。戻し先: ${cur_rev}（0%）"
echo "戻すコマンド: $rollback_cmd"
echo "残り: 本番の画面での確認（画面は Worker docunavi-web 側）。メモリ・文書の更新は Claude に頼む"
