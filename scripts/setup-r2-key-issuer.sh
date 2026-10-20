#!/usr/bin/env bash
# R2 キーの発行 Job（ops/r2_key_issuer、手順と設計は docs/r2-key-issuer.md）の土台を、一度だけ作る。
# 作るもの: 実行用サービスアカウント r2-key-issuer、Secret の入れ物 3 つ、Cloud Run Job 7 つ（モードごとに引数を固定）、
# Secret と Job への最小の権限。親トークンの値は、ここでは扱わない（scripts/store-cloudflare-parent-token.sh）。
# 加えて、PDF の配置と一致確認の Job 3 つ（アプリのイメージで `python -m app.r2_store` を固定の引数で動かす）:
#   r2-upload-plan（置く予定の表示だけ）/ r2-upload（置く）/ r2-verify（DB の資料と R2 の PDF のハッシュ一致）。
#   置く Job の SA は r2-uploader（r2-ingest-rw だけ読める）。一致確認は、アプリの実行用 SA（docunavi-run）で動かす
#   （アプリが使うのと同じ読み取りキーと DB の接続を、そのまま確かめるため）。
#
# 権限のあるアカウントでログインした状態で、ユーザーが実行する（エージェントは実行しない:
# 書き込みなのでフックが止める。意図どおりに、エージェントが触れない経路にしている）。
#
#   ./scripts/setup-r2-key-issuer.sh <プロジェクト ID> --cloudflare-account <アカウント ID>         # 内容を表示して確認を取る
#   ./scripts/setup-r2-key-issuer.sh <プロジェクト ID> --cloudflare-account <アカウント ID> -y      # 確認なし
#   ./scripts/setup-r2-key-issuer.sh <プロジェクト ID> --cloudflare-account <アカウント ID> --verify  # 権限と Job の点検だけ（変更しない）
#
# 環境変数: R2_BUCKET（既定 docunavi-documents）、REGION（既定 asia-northeast1）、AR_REPO（既定 cloud-run-source-deploy）、
#   RUN_SA（既定 docunavi-run@<プロジェクト ID>.iam.gserviceaccount.com。アプリの実行用。一致確認の Job の SA）、
#   DB_URL_SECRET（既定 database-url）、
#   VIEWER_SA（既定 docunavi-viewer@docunavi-ops.iam.gserviceaccount.com。日常の閲覧専用 ID）、
#   DEPLOYER_SA（既定 agent-deployer@<プロジェクト ID>.iam.gserviceaccount.com。--verify の否定テストで使う）
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
# --- 安全装置（エージェントや自動化が、うっかり本物の gcloud を動かさないための、スクリプト自身の仕組み）---
# 1. 対話端末（TTY）でなければ、gcloud を一度も呼ばずに止まる。人が端末で実行するときだけ動く。
# 2. テスト用に gcloud を差し替えるとき（GCLOUD_BIN）は、絶対パスで、実行でき、2 行目以降に「# STUB-GCLOUD」の行がある偽物だけ受け付ける。
#    差し替えに失敗したときに本物へ落ちない（PATH には頼らない）。
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
repo="${AR_REPO:-cloud-run-source-deploy}"
viewer_sa="${VIEWER_SA:-docunavi-viewer@docunavi-ops.iam.gserviceaccount.com}"
deployer_sa="${DEPLOYER_SA:-agent-deployer@${project}.iam.gserviceaccount.com}"

sa_name="r2-key-issuer"
sa="${sa_name}@${project}.iam.gserviceaccount.com"
parent_secret="cloudflare-issuer-token"   # 親トークン（発行専用）。値は store-cloudflare-parent-token.sh で入れる
out_secrets=(r2-app-read r2-ingest-rw)    # 発行したキー（キー ID と Secret Access Key の JSON）。版の追加で更新する
all_secrets=("$parent_secret" "${out_secrets[@]}")
modes=(issue-app-read issue-ingest-rw rotate-app-read rotate-ingest-rw revoke-stale check revoke-all)
# 閲覧専用の ID に実行を許す Job。revoke-all（全部取り消す）は、本人だけが実行できるようにする
viewer_modes=(issue-app-read issue-ingest-rw rotate-app-read rotate-ingest-rw revoke-stale check)

# PDF の配置と一致確認（アプリのイメージ）。Job は用途ごとに引数を固定する
uploader_sa="r2-uploader@${project}.iam.gserviceaccount.com"
run_sa="${RUN_SA:-docunavi-run@${project}.iam.gserviceaccount.com}"
db_url_secret="${DB_URL_SECRET:-database-url}"
tools_modes=(upload-plan upload verify)

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src_dir="$here/ops/r2_key_issuer"
src_sha="$(cd "$src_dir" && sha256sum Dockerfile main.py requirements.txt | sha256sum | cut -d' ' -f1)"
image_base="${region}-docker.pkg.dev/${project}/${repo}/r2-key-issuer"
image="${image_base}:${src_sha:0:12}"
tools_image_base="${region}-docker.pkg.dev/${project}/${repo}/docunavi-r2tools"
# タグは、手元のコミットと未コミットの有無から決める（git が無ければ時刻）。同じタグなら作り直さない
tools_tag="$(git -C "$here" rev-parse --short=12 HEAD 2>/dev/null || true)"
if [[ -z "$tools_tag" ]] || [[ -n "$(git -C "$here" status --porcelain -- Dockerfile requirements.txt app storage frontend 2>/dev/null || true)" ]]; then
  tools_tag="local-$(date +%Y%m%d%H%M%S)"
fi
tools_image="${tools_image_base}:${tools_tag}"

verify() {
  local ok=true m got
  echo "== 実行用サービスアカウント（$sa）"
  "$GCLOUD" iam service-accounts describe "$sa" --project "$project" --format='value(email,disabled)' || ok=false
  echo "-- このサービスアカウントを使える人（actAs / TokenCreator）。想定: 本人だけ。agent-deployer・閲覧専用 ID が無いこと"
  "$GCLOUD" iam service-accounts get-iam-policy "$sa" --project "$project" --format='table(bindings.role,bindings.members.flatten())' || true
  echo "-- プロジェクト全体で、iam.serviceAccountUser / TokenCreator / Owner / Editor を持つ主体（想定: 本人だけ）"
  "$GCLOUD" projects get-iam-policy "$project" --format='table(bindings.role,bindings.members.flatten())' \
    --flatten=bindings --filter='bindings.role:(serviceAccountUser OR serviceAccountTokenCreator OR roles/owner OR roles/editor OR roles/iam.serviceAccountAdmin)' || true
  echo "-- $sa がプロジェクトで持つロール（想定: 無し）"
  "$GCLOUD" projects get-iam-policy "$project" --flatten=bindings \
    --filter="bindings.members:serviceAccount:${sa}" --format='value(bindings.role)' || true

  echo "== Secret の権限（想定: $sa に secretAccessor。出力用 2 つには secretVersionAdder も。他の主体は無し）"
  for s in "${all_secrets[@]}"; do
    echo "-- $s"
    "$GCLOUD" secrets get-iam-policy "$s" --project "$project" --format='table(bindings.role,bindings.members.flatten())' || ok=false
  done

  echo "== Job（ソースのハッシュ ${src_sha:0:40}。ラベル source-sha が一致すれば、配備済みと手元のコードが同じ）"
  for m in "${modes[@]}"; do
    echo "-- r2-$m"
    "$GCLOUD" run jobs describe "r2-$m" --region "$region" --project "$project" \
      --format='value(spec.template.spec.template.spec.serviceAccountName,spec.template.spec.template.spec.containers[0].image,spec.template.spec.template.spec.containers[0].args,metadata.labels.source-sha)' || ok=false
    got="$("$GCLOUD" run jobs describe "r2-$m" --region "$region" --project "$project" --format='value(metadata.labels.source-sha)' 2>/dev/null || true)"
    [[ "$got" == "${src_sha:0:40}" ]] && echo "   ソース: 一致" || echo "   ソース: 不一致（配備し直しが必要）"
    "$GCLOUD" run jobs get-iam-policy "r2-$m" --region "$region" --project "$project" --format='table(bindings.role,bindings.members.flatten())' || true
  done

  echo "== PDF の配置・一致確認の Job（SA・引数・イメージ）"
  for m in "${tools_modes[@]}"; do
    echo "-- r2-$m"
    "$GCLOUD" run jobs describe "r2-$m" --region "$region" --project "$project" \
      --format='value(spec.template.spec.template.spec.serviceAccountName,spec.template.spec.template.spec.containers[0].image,spec.template.spec.template.spec.containers[0].command,spec.template.spec.template.spec.containers[0].args)' || ok=false
    "$GCLOUD" run jobs get-iam-policy "r2-$m" --region "$region" --project "$project" --format='table(bindings.role,bindings.members.flatten())' || true
  done
  echo "-- r2-ingest-rw / r2-app-read の権限（想定: ingest-rw を読めるのは $sa と $uploader_sa。app-read を読めるのは $sa と $run_sa）"
  "$GCLOUD" secrets get-iam-policy r2-ingest-rw --project "$project" --format='table(bindings.role,bindings.members.flatten())' || true
  "$GCLOUD" secrets get-iam-policy r2-app-read --project "$project" --format='table(bindings.role,bindings.members.flatten())' || true

  echo "== 否定テスト（拒否されるのが正しい）"
  echo "-- agent-deployer で Job を更新する（拒否されること。通ったら失敗: ラベルを戻して、権限を見直す）"
  if "$GCLOUD" run jobs update r2-check --region "$region" --project "$project" --update-labels=negative-test=1 \
    --impersonate-service-account="$deployer_sa" >/dev/null 2>&1; then
    echo "   失敗: agent-deployer が Job を更新できた。ラベルを外します"
    "$GCLOUD" run jobs update r2-check --region "$region" --project "$project" --remove-labels=negative-test || true
    ok=false
  else
    echo "   拒否された（正しい）"
  fi
  echo "-- 閲覧専用の ID で、引数を上書きして実行する（拒否されること。run.invoker だけでは、引数の上書きはできない）"
  if "$GCLOUD" run jobs execute r2-issue-app-read --region "$region" --project "$project" --args=check \
    --impersonate-service-account="$viewer_sa" >/dev/null 2>&1; then
    echo "   失敗: 引数の上書きが通った（実行されたのは check。読み取りだけ）。run.jobs.runWithOverrides を持つロールを外す"
    ok=false
  else
    echo "   拒否された（正しい）"
  fi
  $ok
}

if [[ "$verify_only" == true ]]; then
  verify
  exit $?
fi

echo "プロジェクト:         $project（リージョン $region）"
echo "実行用 SA:            $sa（キーは作らない。プロジェクト全体のロールは付けない）"
echo "Secret の入れ物:      ${all_secrets[*]}（無ければ作成。値は入れない）"
echo "Secret の権限:        $sa に secretAccessor（3 つ）と secretVersionAdder（${out_secrets[*]}）"
echo "イメージ:             $image（Cloud Build で作り、digest を固定して配備）"
echo "Job（引数を固定）:    ${modes[*]/#/r2-}"
echo "Job の権限:           $viewer_sa に roles/run.invoker（${viewer_modes[*]/#/r2-}。revoke-all は付けない。引数の上書きは許さない）"
echo "PDF の配置・確認:     イメージ $tools_image（Cloud Build で、リポジトリ直下の Dockerfile から作る。.env は入らない）"
echo "                      Job r2-upload-plan / r2-upload（SA $uploader_sa。r2-ingest-rw だけ読める）、r2-verify（SA $run_sa。r2-app-read と $db_url_secret を読む）"
echo "                      $run_sa に r2-app-read の secretAccessor、$viewer_sa に上の 3 つの Job の run.invoker"
echo "設定:                 CLOUDFLARE_ACCOUNT_ID=${cf_account} R2_BUCKET=${bucket} GCP_PROJECT_ID=${project}"
echo "注意: agent-deployer には、この SA の serviceAccountUser を付けない（Job の中身を変えられなくするため）。"
if [[ "$assume_yes" != true ]]; then
  read -r -p "実行しますか？ [y/N] " answer
  [[ "$answer" == y || "$answer" == Y ]] || { echo "中止しました。" >&2; exit 1; }
fi

"$GCLOUD" services enable secretmanager.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com --project "$project"

if ! "$GCLOUD" iam service-accounts describe "$sa" --project "$project" >/dev/null 2>&1; then
  "$GCLOUD" iam service-accounts create "$sa_name" --display-name="R2 key issuer (Cloud Run Job)" --project "$project"
  echo "作成しました: $sa（反映を待ちます）"
  for _ in $(seq 1 12); do
    "$GCLOUD" iam service-accounts describe "$sa" --project "$project" >/dev/null 2>&1 && break
    sleep 5
  done
fi

for s in "${all_secrets[@]}"; do
  if ! "$GCLOUD" secrets describe "$s" --project "$project" >/dev/null 2>&1; then
    "$GCLOUD" secrets create "$s" --replication-policy=automatic --project "$project"
  fi
  # 反映の遅れで失敗することがある（docs/deploy.md の D）。数回やり直す
  for attempt in 1 2 3 4 5 6; do
    "$GCLOUD" secrets add-iam-policy-binding "$s" --project "$project" \
      --member="serviceAccount:${sa}" --role=roles/secretmanager.secretAccessor >/dev/null && break
    [[ $attempt -lt 6 ]] && sleep 10
  done
  echo "付与しました: secretAccessor -> $s"
done
for s in "${out_secrets[@]}"; do
  "$GCLOUD" secrets add-iam-policy-binding "$s" --project "$project" \
    --member="serviceAccount:${sa}" --role=roles/secretmanager.secretVersionAdder >/dev/null
  echo "付与しました: secretVersionAdder -> $s"
done

if ! "$GCLOUD" artifacts docker images describe "$image" --project "$project" >/dev/null 2>&1; then
  "$GCLOUD" builds submit "$src_dir" --tag "$image" --project "$project"
fi
digest="$("$GCLOUD" artifacts docker images describe "$image" --project "$project" --format='value(image_summary.digest)')"
[[ "$digest" == sha256:* ]] || { echo "イメージの digest を取得できませんでした。" >&2; exit 1; }

for m in "${modes[@]}"; do
  "$GCLOUD" run jobs deploy "r2-$m" --project "$project" --region "$region" \
    --image "${image_base}@${digest}" \
    --service-account "$sa" \
    --args="$m" \
    --set-env-vars="CLOUDFLARE_ACCOUNT_ID=${cf_account},R2_BUCKET=${bucket},GCP_PROJECT_ID=${project}" \
    --max-retries=0 --tasks=1 --parallelism=1 --task-timeout=300s \
    --labels="source-sha=${src_sha:0:40}"
done
for m in "${viewer_modes[@]}"; do
  "$GCLOUD" run jobs add-iam-policy-binding "r2-$m" --project "$project" --region "$region" \
    --member="serviceAccount:${viewer_sa}" --role=roles/run.invoker >/dev/null
  echo "付与しました: run.invoker -> r2-$m（$viewer_sa）"
done

# --- PDF の配置と一致確認（アプリのイメージ）---
if ! "$GCLOUD" iam service-accounts describe "$uploader_sa" --project "$project" >/dev/null 2>&1; then
  "$GCLOUD" iam service-accounts create r2-uploader --display-name="R2 PDF uploader (Cloud Run Job)" --project "$project"
  echo "作成しました: $uploader_sa（反映を待ちます）"
  for _ in $(seq 1 12); do
    "$GCLOUD" iam service-accounts describe "$uploader_sa" --project "$project" >/dev/null 2>&1 && break
    sleep 5
  done
fi
for attempt in 1 2 3 4 5 6; do
  "$GCLOUD" secrets add-iam-policy-binding r2-ingest-rw --project "$project" \
    --member="serviceAccount:${uploader_sa}" --role=roles/secretmanager.secretAccessor >/dev/null && break
  [[ $attempt -lt 6 ]] && sleep 10
done
echo "付与しました: secretAccessor -> r2-ingest-rw（$uploader_sa）"
"$GCLOUD" secrets add-iam-policy-binding r2-app-read --project "$project" \
  --member="serviceAccount:${run_sa}" --role=roles/secretmanager.secretAccessor >/dev/null
echo "付与しました: secretAccessor -> r2-app-read（$run_sa）"

if ! "$GCLOUD" artifacts docker images describe "$tools_image" --project "$project" >/dev/null 2>&1; then
  "$GCLOUD" builds submit "$here" --tag "$tools_image" --project "$project"
fi
tools_digest="$("$GCLOUD" artifacts docker images describe "$tools_image" --project "$project" --format='value(image_summary.digest)')"
[[ "$tools_digest" == sha256:* ]] || { echo "アプリのイメージの digest を取得できませんでした。" >&2; exit 1; }

r2_env="R2_ACCOUNT_ID=${cf_account},R2_BUCKET=${bucket}"
job_common=(--project "$project" --region "$region" --image "${tools_image_base}@${tools_digest}" --command=python
  --max-retries=0 --tasks=1 --parallelism=1 --task-timeout=300s --set-env-vars="$r2_env")
"$GCLOUD" run jobs deploy r2-upload-plan "${job_common[@]}" --service-account "$uploader_sa" \
  --args="-m,app.r2_store,upload" --set-secrets="R2_CREDENTIALS_JSON=r2-ingest-rw:latest"
"$GCLOUD" run jobs deploy r2-upload "${job_common[@]}" --service-account "$uploader_sa" \
  --args="-m,app.r2_store,upload,--apply" --set-secrets="R2_CREDENTIALS_JSON=r2-ingest-rw:latest"
"$GCLOUD" run jobs deploy r2-verify "${job_common[@]}" --service-account "$run_sa" \
  --args="-m,app.r2_store,verify" --set-secrets="R2_CREDENTIALS_JSON=r2-app-read:latest,DATABASE_URL=${db_url_secret}:latest"
for m in "${tools_modes[@]}"; do
  "$GCLOUD" run jobs add-iam-policy-binding "r2-$m" --project "$project" --region "$region" \
    --member="serviceAccount:${viewer_sa}" --role=roles/run.invoker >/dev/null
  echo "付与しました: run.invoker -> r2-$m（$viewer_sa）"
done

echo "完了しました。次: 親トークンを入れる（./scripts/store-cloudflare-parent-token.sh $project）→ $0 $project --cloudflare-account … --verify"
