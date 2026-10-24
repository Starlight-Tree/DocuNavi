#!/bin/bash
# Cloud Run のタグ付きプレビュー（docs/deploy.md の「タグ付きプレビュー」）の書き込みを、4 つのサブコマンドにまとめる。
#   build [<コミット>]        イメージをビルドして Artifact Registry に置く（Cloud Build）。<コミット> は省略か HEAD の短縮 SHA だけ
#   deploy <コミット> [<タグ>] そのイメージを --no-traffic --tag で配備する（本番のトラフィックは変わらない）。タグの既定は preview-YYYYMMDD-HHMM
#   promote <タグ>            そのタグのリビジョンへ 100% を移す
#   rollback <リビジョン>      そのリビジョンへ 100% を戻す
#
# 書き込みなので、ユーザーが自分の ID で実行する（エージェントは実行しない。フックが止める）。
# イメージの名前は <コミット> の短縮 SHA から決める（HEAD とのずれを防ぐ）。build はビルドの入力が作業ツリーなので、
# <コミット> が HEAD と一致し、追跡中のファイルに未コミットの変更が無いときだけ動く。
# deploy は --set-env-vars / --set-secrets を付けない（付けると、有料キーの Secret など、いまの設定が消える）。
# Secret・トークンは書かない。画面は Vercel の frontend-next が配信するので、イメージは API だけ（ビルドに渡す設定は無い）。
#
# 使い方:
#   ./scripts/preview.sh build
#   ./scripts/preview.sh deploy "$(git rev-parse --short HEAD)"
#   ./scripts/preview.sh promote preview-20261021-2114        # 実行前に確認を取る。-y で省く
#   ./scripts/preview.sh rollback docunavi-00005-zl7
# 環境変数: PROJECT_ID（既定 docunavi-app）、REGION（既定 asia-northeast1）、SERVICE（既定 docunavi）、AR_REPO（既定 cloud-run-source-deploy）、
#   PREVIEW_DRY_RUN=1（gcloud を実行せず、実行するコマンドを表示する。公開値は伏せる）
set -euo pipefail

# 書き込みなので、本人の ID で動かす（コンテナの既定は docunavi-viewer へのなりすまし。書き込みは 403 になる）
unset CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT

PROJECT_ID="${PROJECT_ID:-docunavi-app}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-docunavi}"
AR_REPO="${AR_REPO:-cloud-run-source-deploy}"
DRY_RUN="${PREVIEW_DRY_RUN:-}"

die() {
  echo "エラー: $*" >&2
  exit 2
}

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

run() {
  if [[ -n "$DRY_RUN" ]]; then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

traffic() {
  run gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='yaml(status.traffic)'
}

short_sha() {
  git rev-parse --short HEAD
}

image_for() {
  echo "${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE}:$1"
}

check_commit() {
  [[ "$1" =~ ^[0-9a-f]{7,40}$ ]] || die "コミットは 16 進の SHA で指定してください: $1"
}

assume_yes=false
args=()
for a in "$@"; do
  case "$a" in
    -y | --yes) assume_yes=true ;;
    -h | --help) usage 0 ;;
    *) args+=("$a") ;;
  esac
done
cmd="${args[0]:-}"
[[ -n "$cmd" ]] || usage 2

confirm() {
  [[ "$assume_yes" == true || -n "$DRY_RUN" ]] && return 0
  read -r -p "$1 [y/N] " ans
  [[ "$ans" == y || "$ans" == Y ]] || die "中止しました"
}

case "$cmd" in
  build)
    commit="${args[1]:-$(short_sha)}"
    check_commit "$commit"
    head_sha="$(short_sha)"
    [[ "$head_sha" == "$commit"* || "$commit" == "$head_sha"* ]] || die "ビルドの入力は作業ツリー（HEAD = $head_sha）です。指定のコミット $commit と一致しません"
    git diff --quiet HEAD -- || die "追跡中のファイルに未コミットの変更があります。コミットしてからビルドしてください"
    image="$(image_for "$head_sha")"
    echo "IMAGE=$image"
    run gcloud builds submit . --project "$PROJECT_ID" --config cloudbuild.yaml \
      --substitutions "_IMAGE=${image}"
    ;;

  deploy)
    commit="${args[1]:-}"
    [[ -n "$commit" ]] || die "deploy にはコミットの SHA が要ります（例: \$(git rev-parse --short HEAD)）"
    check_commit "$commit"
    tag="${args[2]:-preview-$(date +%Y%m%d-%H%M)}"
    [[ "$tag" =~ ^[a-z][a-z0-9-]*$ ]] || die "タグは小文字・数字・ハイフンで、先頭は英字にしてください: $tag"
    image="$(image_for "$commit")"
    echo "IMAGE=$image"
    echo "TAG=$tag"
    echo "--- いまのトラフィック（ロールバック先に、100% のリビジョン名を控える）"
    traffic
    run gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
      --image "$image" --no-traffic --tag "$tag"
    echo "--- 配備後のトラフィック（新しいリビジョンが 0%、本番が 100% のままであること）"
    traffic
    ;;

  promote)
    tag="${args[1]:-}"
    [[ -n "$tag" ]] || die "promote にはタグが要ります"
    [[ "$tag" =~ ^[a-z][a-z0-9-]*$ ]] || die "タグの形が違います: $tag"
    echo "--- いまのトラフィック"
    traffic
    confirm "タグ $tag のリビジョンへ 100% を移します。よろしいですか?"
    run gcloud run services update-traffic "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --to-tags "${tag}=100"
    ;;

  rollback)
    rev="${args[1]:-}"
    [[ -n "$rev" ]] || die "rollback にはリビジョン名が要ります"
    [[ "$rev" =~ ^${SERVICE}-[0-9]{5}-[a-z0-9]{3}$ ]] || die "リビジョン名の形が違います: $rev"
    echo "--- いまのトラフィック"
    traffic
    confirm "リビジョン $rev へ 100% を戻します。よろしいですか?"
    run gcloud run services update-traffic "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --to-revisions="${rev}=100"
    ;;

  *)
    usage 2
    ;;
esac
