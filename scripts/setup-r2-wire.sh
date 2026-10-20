#!/bin/bash
# アプリの Cloud Run サービスを R2 参照に切り替える Job（ops/r2_wire、手順と設計は docs/r2-rollout.md）の土台を、一度だけ作る。
# 作るもの: 実行用サービスアカウント r2-wirer、Cloud Run Job 3 つ（r2-wire-stage / r2-wire-promote / r2-wire-rollback。モードごとに引数を固定）、
# 最小の権限（サービス docunavi の run.developer、アプリの実行用 SA の serviceAccountUser、Secret r2-app-read の閲覧、イメージのリポジトリの読み取り）。
# 閲覧専用の ID（docunavi-viewer）には、これらの Job を実行する権限を付けない。stage / promote / rollback は、あなたが自分の ID で実行する。
# Job を実行するまで、サービスは何も変わらない（このスクリプトは、サービスを更新しない）。
#
# 権限のあるアカウントでログインした状態で、ユーザーが実行する（エージェントは実行しない:
# 書き込みなのでフックが止める。意図どおりに、エージェントが触れない経路にしている）。
#
# 使い方:
#   ./scripts/setup-r2-wire.sh <プロジェクト ID> --cloudflare-account <アカウント ID>         # 内容を表示して確認を取る
#   ./scripts/setup-r2-wire.sh <プロジェクト ID> --cloudflare-account <アカウント ID> -y      # 確認なし
#   ./scripts/setup-r2-wire.sh <プロジェクト ID> --cloudflare-account <アカウント ID> --verify  # 権限と Job の点検だけ（変更しない）
#
# 環境変数: R2_BUCKET（既定 docunavi-documents）、REGION（既定 asia-northeast1）、SERVICE（既定 docunavi）、AR_REPO（既定 cloud-run-source-deploy）、
#   RUN_SA（既定 docunavi-run@<プロジェクト ID>.iam.gserviceaccount.com。アプリの実行用）
#
# 冪等なので、やり直してもよい（コードを直したときの再配備も、このスクリプトでやり直す）。
set -euo pipefail

# 書き込みなので、本人の ID で動かす（コンテナの既定は docunavi-viewer へのなりすまし。書き込みは 403 になる）
unset CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT

project=""
cf_account=""
assume_yes=false
verify_only=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y | --yes) assume_yes=true ;;
    --verify) verify_only=true ;;
    --cloudflare-account)
      cf_account="${2:-}"
      shift
      ;;
    *) project="$1" ;;
  esac
  shift
done
project="${project:-${GCP_PROJECT_ID:-}}"
cf_account="${cf_account:-${CLOUDFLARE_ACCOUNT_ID:-}}"
if [[ -z "$project" || -z "$cf_account" ]]; then
  echo "使い方: $0 <プロジェクト ID> --cloudflare-account <アカウント ID> [-y | --verify]" >&2
  exit 2
fi
# --- 安全装置（setup-r2-key-issuer.sh と同じ）---
# 1. 対話端末（TTY）でなければ、gcloud を一度も呼ばずに止まる。人が端末で実行するときだけ動く。
# 2. テスト用に gcloud を差し替えるとき（GCLOUD_BIN）は、絶対パスで、実行でき、2 行目以降に「# STUB-GCLOUD」の行がある偽物だけ受け付ける。
GCLOUD="gcloud"
stub=false
if [[ -n "${GCLOUD_BIN:-}" ]]; then
  if [[ "$GCLOUD_BIN" == /* && -f "$GCLOUD_BIN" && -x "$GCLOUD_BIN" ]] && grep -qx '# STUB-GCLOUD' "$GCLOUD_BIN"; then
    GCLOUD="$GCLOUD_BIN"
    stub=true
  else
    echo "GCLOUD_BIN は、絶対パスの実行できる偽物（'# STUB-GCLOUD' の行を持つファイル）だけ使えます。何も実行せず終了します。" >&2
    exit 2
  fi
fi
if [[ "$stub" != true && ! ( -t 0 && -t 1 ) ]]; then
  echo "対話端末ではないので、何も実行しません（gcloud は呼んでいません）。" >&2
  echo "あなた自身の端末（コンテナの外のホストか Cloud Shell）で実行してください: $0 $project --cloudflare-account <ID>" >&2
  exit 3
fi
if [[ "$stub" != true ]] && ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud が見つかりません。ホストまたは Cloud Shell で実行してください。" >&2
  exit 1
fi
if [[ -n "$("$GCLOUD" config get-value auth/impersonate_service_account 2>/dev/null || true)" ]]; then
  echo "auth/impersonate_service_account が設定されています。書き込みが拒否されるので、次を実行してからやり直してください:" >&2
  echo "  gcloud config unset auth/impersonate_service_account" >&2
  exit 1
fi

bucket="${R2_BUCKET:-docunavi-documents}"
region="${REGION:-asia-northeast1}"
service="${SERVICE:-docunavi}"
repo="${AR_REPO:-cloud-run-source-deploy}"
run_sa="${RUN_SA:-docunavi-run@${project}.iam.gserviceaccount.com}"
secret="r2-app-read"

sa_name="r2-wirer"
sa="${sa_name}@${project}.iam.gserviceaccount.com"
modes=(stage promote rollback)

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src_dir="$here/ops/r2_wire"
src_sha="$(cd "$src_dir" && sha256sum Dockerfile main.py requirements.txt | sha256sum | cut -d' ' -f1)"
image_base="${region}-docker.pkg.dev/${project}/${repo}/r2-wire"
image="${image_base}:${src_sha:0:12}"

verify() {
  local ok=true
  echo "== 実行用 SA（想定: 存在する）"
  "$GCLOUD" iam service-accounts describe "$sa" --project "$project" --format='value(email,disabled)' || ok=false
  echo "== Job（想定: 3 つ。引数が固定、実行用 SA が $sa、イメージが digest 固定）"
  for m in "${modes[@]}"; do
    "$GCLOUD" run jobs describe "r2-wire-$m" --region "$region" --project "$project" \
      --format='value(name,spec.template.spec.template.spec.serviceAccountName,spec.template.spec.template.spec.containers[0].args,spec.template.spec.template.spec.containers[0].image)' || ok=false
  done
  echo "== サービス $service の権限（想定: $sa に roles/run.developer。viewer 以外の追加の主体は無い）"
  "$GCLOUD" run services get-iam-policy "$service" --region "$region" --project "$project" \
    --format='table(bindings.role,bindings.members.flatten())' || ok=false
  echo "== $run_sa の権限（想定: $sa に roles/iam.serviceAccountUser）"
  "$GCLOUD" iam service-accounts get-iam-policy "$run_sa" --project "$project" \
    --format='table(bindings.role,bindings.members.flatten())' || ok=false
  echo "== Secret $secret の権限（想定: $run_sa に secretAccessor、$sa に secretmanager.viewer）"
  "$GCLOUD" secrets get-iam-policy "$secret" --project "$project" \
    --format='table(bindings.role,bindings.members.flatten())' || ok=false
  echo "== イメージのリポジトリ $repo の権限（想定: $sa に roles/artifactregistry.reader。サービスの更新時に、Cloud Run が呼び出し側の読み取りを確かめる）"
  "$GCLOUD" artifacts repositories get-iam-policy "$repo" --location "$region" --project "$project" \
    --format='table(bindings.role,bindings.members.flatten())' || ok=false
  echo "== Job を実行できる主体（想定: あなただけ。docunavi-viewer・$sa には無い）"
  for m in "${modes[@]}"; do
    "$GCLOUD" run jobs get-iam-policy "r2-wire-$m" --region "$region" --project "$project" \
      --format='table(bindings.role,bindings.members.flatten())' || ok=false
  done
  $ok
}

if [[ "$verify_only" == true ]]; then
  verify
  exit $?
fi

echo "プロジェクト:         $project（リージョン $region）"
echo "実行用 SA:            $sa（キーは作らない。プロジェクト全体のロールは付けない）"
echo "権限:                 サービス $service に roles/run.developer、$run_sa に roles/iam.serviceAccountUser、Secret $secret に roles/secretmanager.viewer（値は読めない）、"
echo "                      イメージのリポジトリ $repo に roles/artifactregistry.reader（サービスの更新時に、Cloud Run が呼び出し側の読み取りを確かめる）"
echo "イメージ:             $image（Cloud Build で作り、digest を固定して配備）"
echo "Job（引数を固定）:    ${modes[*]/#/r2-wire-}"
echo "Job を実行できる主体: あなたの ID だけ（docunavi-viewer には run.invoker を付けない）"
echo "設定:                 R2_ACCOUNT_ID=${cf_account} R2_BUCKET=${bucket} GCP_PROJECT_ID=${project} SERVICE_NAME=${service}"
echo "このスクリプトはサービスを更新しない。サービスが変わるのは、あなたが r2-wire-stage / r2-wire-promote / r2-wire-rollback を実行したときだけ。"
if [[ "$assume_yes" != true ]]; then
  read -r -p "実行しますか？ [y/N] " answer
  [[ "$answer" == y || "$answer" == Y ]] || { echo "中止しました。" >&2; exit 1; }
fi

"$GCLOUD" services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com --project "$project"

if ! "$GCLOUD" iam service-accounts describe "$sa" --project "$project" >/dev/null 2>&1; then
  "$GCLOUD" iam service-accounts create "$sa_name" --display-name="R2 wiring (Cloud Run Job)" --project "$project"
  echo "作成しました: $sa（反映を待ちます）"
  for _ in $(seq 1 12); do
    "$GCLOUD" iam service-accounts describe "$sa" --project "$project" >/dev/null 2>&1 && break
    sleep 5
  done
fi

# 反映の遅れで失敗することがある（docs/deploy.md の D）。数回やり直す
retry() {
  local attempt
  for attempt in 1 2 3 4 5 6; do
    "$@" >/dev/null && return 0
    [[ $attempt -lt 6 ]] && sleep 10
  done
  return 1
}
retry "$GCLOUD" run services add-iam-policy-binding "$service" --project "$project" --region "$region" \
  --member="serviceAccount:${sa}" --role=roles/run.developer
echo "付与しました: run.developer -> サービス $service（$sa）"
retry "$GCLOUD" iam service-accounts add-iam-policy-binding "$run_sa" --project "$project" \
  --member="serviceAccount:${sa}" --role=roles/iam.serviceAccountUser
echo "付与しました: serviceAccountUser -> $run_sa（$sa）"
retry "$GCLOUD" secrets add-iam-policy-binding "$secret" --project "$project" \
  --member="serviceAccount:${sa}" --role=roles/secretmanager.viewer
echo "付与しました: secretmanager.viewer -> $secret（$sa）"
retry "$GCLOUD" artifacts repositories add-iam-policy-binding "$repo" --project "$project" --location "$region" \
  --member="serviceAccount:${sa}" --role=roles/artifactregistry.reader
echo "付与しました: artifactregistry.reader -> リポジトリ $repo（$sa）"

if ! "$GCLOUD" artifacts docker images describe "$image" --project "$project" >/dev/null 2>&1; then
  "$GCLOUD" builds submit "$src_dir" --tag "$image" --project "$project"
fi
digest="$("$GCLOUD" artifacts docker images describe "$image" --project "$project" --format='value(image_summary.digest)')"
[[ "$digest" == sha256:* ]] || { echo "イメージの digest を取得できませんでした。" >&2; exit 1; }

for m in "${modes[@]}"; do
  "$GCLOUD" run jobs deploy "r2-wire-$m" --project "$project" --region "$region" \
    --image "${image_base}@${digest}" \
    --service-account "$sa" \
    --args="$m" \
    --set-env-vars="GCP_PROJECT_ID=${project},REGION=${region},SERVICE_NAME=${service},R2_ACCOUNT_ID=${cf_account},R2_BUCKET=${bucket}" \
    --max-retries=0 --tasks=1 --parallelism=1 --task-timeout=900s \
    --labels="source-sha=${src_sha:0:40}"
done

echo "完了しました。次: $0 $project --cloudflare-account … --verify（点検）。サービスはまだ変わっていません。"
echo "      切り替えの手順は docs/r2-rollout.md（アプリのイメージの配備 → r2-wire-stage → r2-verify → r2-wire-promote）。"
