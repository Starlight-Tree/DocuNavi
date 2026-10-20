#!/usr/bin/env bash
# R2 キーの発行 Job（ops/r2_key_issuer、手順と設計は docs/r2-key-issuer.md）の土台を、一度だけ作る。
# 作るもの: 実行用サービスアカウント r2-key-issuer、Secret の入れ物 3 つ（r2-ingest-rw は空のまま）、Cloud Run Job 6 つ（モードごとに引数を固定）、
# Secret と Job への最小の権限。親トークンの値は、ここでは扱わない（scripts/store-cloudflare-parent-token.sh）。
# 自動ローテーション用に、Cloud Scheduler の Job 3 つ（月 1 回の app-read の入れ替え、週 1 回の古いキーの取り消し、週 1 回の点検）と、
# その実行用 SA r2-scheduler（上の Job のうち 3 つの run.invoker だけ）。
# 取り込み用: SA r2-uploader（r2-ingest-rw だけ読める）と、アプリの実行用 SA に r2-app-read の読み取りを付ける。r2-ingest-rw の発行・入れ替えの Job と、
# 取り込みの Job は作らない（クラウドから PDF を取り込む経路ができたときに、Job と Scheduler を足す）。一致確認の Job r2-verify は作成済みで、このスクリプトは触らない。
# 通知（メール・Cloud Monitoring のアラート）は作らない。PARENT_TOKEN_EXPIRING などは r2-check のログに出るだけ。
#
# 権限のあるアカウントでログインした状態で、ユーザーが実行する（エージェントは実行しない:
# 書き込みなのでフックが止める。意図どおりに、エージェントが触れない経路にしている）。
#
#   ./scripts/setup-r2-key-issuer.sh <プロジェクト ID> --cloudflare-account <アカウント ID>         # 内容を表示して確認を取る
#   ./scripts/setup-r2-key-issuer.sh <プロジェクト ID> --cloudflare-account <アカウント ID> -y      # 確認なし
#   ./scripts/setup-r2-key-issuer.sh <プロジェクト ID> --cloudflare-account <アカウント ID> --verify  # 権限と Job の点検だけ（変更しない）
#
# 環境変数: R2_BUCKET（既定 docunavi-documents）、REGION（既定 asia-northeast1）、AR_REPO（既定 cloud-run-source-deploy）、
#   RUN_SA（既定 docunavi-run@<プロジェクト ID>.iam.gserviceaccount.com。アプリの実行用。r2-app-read だけ読める）、
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
out_secrets=(r2-app-read)                 # 発行したキー（キー ID と Secret Access Key の JSON）。版の追加で更新する。r2-ingest-rw は空の入れ物だけ
all_secrets=("$parent_secret" "${out_secrets[@]}" r2-ingest-rw)
modes=(issue-app-read rotate-app-read rotate-if-due-app-read revoke-stale check revoke-all)
# 閲覧専用の ID に実行を許す Job。revoke-all（全部取り消す）は、本人だけが実行できるようにする
viewer_modes=(issue-app-read rotate-app-read rotate-if-due-app-read revoke-stale check)

# 自動ローテーション（Cloud Scheduler）。名前|cron（JST）|起動する Job。Scheduler は引数を渡さない（Job の固定の引数のまま動く）
scheduler_sa="r2-scheduler@${project}.iam.gserviceaccount.com"
tz="Asia/Tokyo"
schedules=(
  "r2-sched-rotate-app-read|0 4 1 * *|r2-rotate-if-due-app-read"     # 毎月 1 日 04:00。60 日たっていれば入れ替える
  "r2-sched-revoke-stale|30 4 * * 1|r2-revoke-stale"                 # 毎週月曜 04:30。置き換えから 7 日たった古いキーを取り消す
  "r2-sched-check|0 5 * * 1|r2-check"                                # 毎週月曜 05:00。親トークンの残りと、記録したキーの動作を点検する
)

# 取り込み用の SA（Job は作らない）と、アプリの実行用 SA
uploader_sa="r2-uploader@${project}.iam.gserviceaccount.com"
run_sa="${RUN_SA:-docunavi-run@${project}.iam.gserviceaccount.com}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src_dir="$here/ops/r2_key_issuer"
src_sha="$(cd "$src_dir" && sha256sum Dockerfile main.py requirements.txt | sha256sum | cut -d' ' -f1)"
image_base="${region}-docker.pkg.dev/${project}/${repo}/r2-key-issuer"
image="${image_base}:${src_sha:0:12}"

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

  echo "== Secret の権限（想定: $sa に secretAccessor。r2-app-read には secretVersionAdder も。他の主体は無し。r2-ingest-rw は空）"
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

  echo "== 一致確認の Job r2-verify（作成済み。このスクリプトは作らない・変えない）"
  "$GCLOUD" run jobs describe r2-verify --region "$region" --project "$project" \
    --format='value(spec.template.spec.template.spec.serviceAccountName,spec.template.spec.template.spec.containers[0].image,spec.template.spec.template.spec.containers[0].command,spec.template.spec.template.spec.containers[0].args)' || true
  echo "-- r2-ingest-rw / r2-app-read の権限（想定: ingest-rw を読めるのは $sa と $uploader_sa。app-read を読めるのは $sa と $run_sa）"
  "$GCLOUD" secrets get-iam-policy r2-ingest-rw --project "$project" --format='table(bindings.role,bindings.members.flatten())' || true
  "$GCLOUD" secrets get-iam-policy r2-app-read --project "$project" --format='table(bindings.role,bindings.members.flatten())' || true

  echo "== 自動ローテーション（想定: $scheduler_sa が起動できるのは rotate-if-due-app-read・revoke-stale・check だけ。Job ごとの権限は上に出る）"
  "$GCLOUD" iam service-accounts describe "$scheduler_sa" --project "$project" --format='value(email,disabled)' || ok=false
  echo "-- $scheduler_sa がプロジェクトで持つロール（想定: 無し）"
  "$GCLOUD" projects get-iam-policy "$project" --flatten=bindings \
    --filter="bindings.members:serviceAccount:${scheduler_sa}" --format='value(bindings.role)' || true
  for entry in "${schedules[@]}"; do
    IFS='|' read -r s_name s_cron s_job <<<"$entry"
    echo "-- $s_name（想定: [$s_cron] -> $s_job）"
    "$GCLOUD" scheduler jobs describe "$s_name" --location "$region" --project "$project" \
      --format='value(name,schedule,timeZone,state,httpTarget.uri,httpTarget.oauthToken.serviceAccountEmail,lastAttemptTime,status.code)' || ok=false
  done
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
echo "取り込み用:           SA $uploader_sa（r2-ingest-rw の secretAccessor だけ。Job は作らない）、$run_sa に r2-app-read の secretAccessor"
echo "一致確認:             Job r2-verify は作成済みのまま触らない"
echo "自動ローテーション:   SA $scheduler_sa（プロジェクトのロールは無し。次の Job の run.invoker だけ）と Cloud Scheduler の Job 3 つ（$tz）"
for entry in "${schedules[@]}"; do
  IFS='|' read -r s_name s_cron s_job <<<"$entry"
  echo "                      $s_name  [$s_cron]  -> $s_job"
done
echo "                      Scheduler の Job が 3 つ（無料枠は 3 つまで〔公表値〕）"
echo "通知:                 作らない（メール・アラートなし）。PARENT_TOKEN_EXPIRING などは r2-check のログで読む"
echo "設定:                 CLOUDFLARE_ACCOUNT_ID=${cf_account} R2_BUCKET=${bucket} GCP_PROJECT_ID=${project}"
echo "注意: agent-deployer には、この SA の serviceAccountUser を付けない（Job の中身を変えられなくするため）。"
if [[ "$assume_yes" != true ]]; then
  read -r -p "実行しますか？ [y/N] " answer
  [[ "$answer" == y || "$answer" == Y ]] || { echo "中止しました。" >&2; exit 1; }
fi

"$GCLOUD" services enable secretmanager.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudscheduler.googleapis.com --project "$project"

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

# --- 取り込み用の SA（Job は作らない）---
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

# --- 自動ローテーション（Cloud Scheduler）---
if ! "$GCLOUD" iam service-accounts describe "$scheduler_sa" --project "$project" >/dev/null 2>&1; then
  "$GCLOUD" iam service-accounts create r2-scheduler --display-name="R2 key rotation scheduler (Cloud Scheduler)" --project "$project"
  echo "作成しました: $scheduler_sa（反映を待ちます）"
  for _ in $(seq 1 12); do
    "$GCLOUD" iam service-accounts describe "$scheduler_sa" --project "$project" >/dev/null 2>&1 && break
    sleep 5
  done
fi
for entry in "${schedules[@]}"; do
  IFS='|' read -r s_name s_cron s_job <<<"$entry"
  for attempt in 1 2 3 4 5 6; do  # SA が反映される前は失敗することがある
    "$GCLOUD" run jobs add-iam-policy-binding "$s_job" --project "$project" --region "$region" \
      --member="serviceAccount:${scheduler_sa}" --role=roles/run.invoker >/dev/null && break
    [[ $attempt -lt 6 ]] && sleep 10
  done
  echo "付与しました: run.invoker -> $s_job（$scheduler_sa）"
  s_uri="https://run.googleapis.com/v2/projects/${project}/locations/${region}/jobs/${s_job}:run"
  if "$GCLOUD" scheduler jobs describe "$s_name" --location "$region" --project "$project" >/dev/null 2>&1; then
    s_verb=update
  else
    s_verb=create
  fi
  # 本文（引数）は渡さない＝Job の固定の引数のまま。失敗しても自動では再試行しない（二重に入れ替えない）
  "$GCLOUD" scheduler jobs "$s_verb" http "$s_name" --project "$project" --location "$region" \
    --schedule="$s_cron" --time-zone="$tz" --uri="$s_uri" --http-method=POST \
    --oauth-service-account-email="$scheduler_sa" --attempt-deadline=180s --max-retry-attempts=0
  echo "設定しました: $s_name [$s_cron] -> $s_job"
done

echo "完了しました。次: 親トークンを入れる（./scripts/store-cloudflare-parent-token.sh $project）→ $0 $project --cloudflare-account … --verify"
