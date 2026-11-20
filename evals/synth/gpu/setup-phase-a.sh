#!/usr/bin/env bash
# A 段階（GPU なし）: プロジェクト・請求・API・バケット・サービスアカウント・依頼・重み・イメージ。
# ホストで、自分の権限で実行する:
#   PROJECT=<既存ならその ID> env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT bash evals/synth/gpu/setup-phase-a.sh [請求先ID]
# 何度でも再実行できる（あるものは飛ばす）。PROJECT の既定は日付入りなので、日をまたぐ再実行では必ず指定する。
# 既存の docunavi-app / docunavi-ops には触れない。GPU ジョブは作らない（B 段階）。
set -euo pipefail
cd "$(dirname "$0")/../../.."

PROJECT="${PROJECT:-docunavi-synth-$(date +%y%m%d)}"
FOLDER="${FOLDER:-204792167834}"   # 組織 dev-cloud のフォルダー DocuNavi。組織の外にプロジェクトを作らない（2026-11-20）
REGION=asia-southeast1
BUCKET="$PROJECT-synth"
REPO=synth
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/gen:v1"
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:cu130-nightly}"   # ドライバーは CUDA 13.0 系。ビルド後に digest を記録して固定する
MODEL_REPO=Qwen/Qwen3.6-35B-A3B   # 元の BF16 版（約 72GB）。量子化はしない（事前 FP8 版も、--quantization も使わない）
MODEL_REV=995ad96eacd98c81ed38be0c5b274b04031597b0
BUDGET_AMOUNT="${BUDGET_AMOUNT:-1500JPY}"   # 請求先の通貨に合わせる（この請求先は JPY。約 $10 相当の目安。USD の請求先なら 10USD を渡す）

# 指定（第 1 引数、なければ環境変数 BILLING）を優先する。自動判定は、未指定のときだけ行う
BILLING="${1:-${BILLING:-}}"
if [ -z "$BILLING" ]; then
  FOUND=$(gcloud billing accounts list --filter=open=true --format='value(name)')
  FOUND=${FOUND#billingAccounts/}
  case "$FOUND" in
    *$'\n'*|"") echo "請求先が 1 つに決まりません。ID を第 1 引数で渡してください（まだ何も作っていません）:"
                 echo "  env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud billing accounts list"; exit 1 ;;
  esac
  BILLING=$FOUND
fi
BILLING=${BILLING#billingAccounts/}
# 形式の検査（ID の形だけ。macOS の wc -l は数字の前に空白を付けるので、行数の比較には使わない）
[[ "$BILLING" =~ ^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$ ]] || { echo "請求先 ID の形式が違います: [$BILLING]（まだ何も作っていません）"; exit 1; }
echo "プロジェクト: $PROJECT / 請求先: $BILLING / リージョン: $REGION"

# 各段階は、あれば飛ばす（失敗した所から、同じコマンドで再開できる）
if gcloud projects describe "$PROJECT" >/dev/null 2>&1; then echo "[skip] プロジェクト $PROJECT は存在する"
else gcloud projects create "$PROJECT" --name="docunavi synth" --folder="$FOLDER"; fi

LINKED=$(gcloud billing projects describe "$PROJECT" --format='value(billingAccountName)' 2>/dev/null || true)
if [ "$LINKED" = "billingAccounts/$BILLING" ]; then echo "[skip] 請求先は紐付け済み"
else gcloud billing projects link "$PROJECT" --billing-account="$BILLING"; fi

gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  storage.googleapis.com billingbudgets.googleapis.com --project "$PROJECT"

# 予算（通知だけ。止めはしない）。通貨は請求先と同じでなければ INVALID_ARGUMENT になる。--filter-projects はプロジェクト番号で渡す。
BUDGET_NAME="synth-$PROJECT"
if gcloud billing budgets list --billing-account="$BILLING" --billing-project="$PROJECT" --format='value(displayName)' | grep -Fxq "$BUDGET_NAME"; then
  echo "[skip] 予算 $BUDGET_NAME は存在する"
else
  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
  gcloud billing budgets create --billing-account="$BILLING" --display-name="$BUDGET_NAME" \
    --budget-amount="$BUDGET_AMOUNT" --filter-projects="projects/$PROJECT_NUMBER" \
    --threshold-rule=percent=0.5 --threshold-rule=percent=1.0 --billing-project="$PROJECT"
fi

gcloud artifacts repositories describe "$REPO" --location="$REGION" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" --project "$PROJECT"
gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access \
       --public-access-prevention --project "$PROJECT"
gcloud iam service-accounts describe "synth-runner@$PROJECT.iam.gserviceaccount.com" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud iam service-accounts create synth-runner --display-name="synth job runner" --project "$PROJECT"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:synth-runner@$PROJECT.iam.gserviceaccount.com" --role=roles/storage.objectAdmin

gcloud storage cp evals/synth/out/bench/requests.jsonl "gs://$BUCKET/bench/requests.jsonl"

# 重み（BF16、約 72GB）は、index.json が既にあれば取り直さない
if gcloud storage ls "gs://$BUCKET/models/qwen3.6-35b-a3b/model.safetensors.index.json" >/dev/null 2>&1; then echo "[skip] 重みは配置済み"
else gcloud builds submit --no-source --project "$PROJECT" --config evals/synth/gpu/cloudbuild-weights.yaml \
       --substitutions="_BUCKET=$BUCKET,_REPO=$MODEL_REPO,_REV=$MODEL_REV"; fi
# イメージは、タグがあれば作り直さない
if gcloud artifacts docker images describe "$IMAGE" --project "$PROJECT" >/dev/null 2>&1; then echo "[skip] イメージは作成済み"
else gcloud builds submit evals/synth --project "$PROJECT" --config evals/synth/gpu/cloudbuild-image.yaml \
       --substitutions="_IMAGE=$IMAGE,_VLLM_IMAGE=$VLLM_IMAGE"; fi

echo "--- 結果（B 段階で使う値）"
echo "PROJECT=$PROJECT BUCKET=$BUCKET IMAGE=$IMAGE"
gcloud artifacts docker images list "$REGION-docker.pkg.dev/$PROJECT/$REPO" --include-tags --project "$PROJECT"
gcloud storage ls -l "gs://$BUCKET/models/qwen3.6-35b-a3b/" | tail -3
