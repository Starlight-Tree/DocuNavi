#!/usr/bin/env bash
# Cloud Run へのデプロイ用サービスアカウント agent-deployer の作成と IAM の付与を、一度だけ行う。
# 日常の ID は閲覧専用の docunavi-viewer（コンテナの既定）。デプロイのときだけ、このサービスアカウントに
# --impersonate-service-account で明示的になりすます（docs/deploy.md）。サービスを公開する権限は付けない。
#
# 権限のあるアカウントでログインした状態で、ユーザーが実行する（エージェントは実行しない:
# 書き込みなのでフックが止める）。事前に、Cloud Run・Cloud Build・Artifact Registry の API の有効化と、
# 実行用サービスアカウントの作成（docs/deploy.md の「準備」）を済ませる。課金が有効でないと API を有効にできない。
#
#   ./scripts/setup-agent-deployer.sh <プロジェクト ID>        # 内容を表示して確認を取る
#   ./scripts/setup-agent-deployer.sh <プロジェクト ID> -y     # 確認なし
#   GCP_PROJECT_ID=<プロジェクト ID> RUN_SA_NAME=<実行用 SA の名前> ./scripts/setup-agent-deployer.sh
#
# 冪等なので、やり直してもよい。
#
# 付与するロールは出発点で、実際に deploy して足りない権限をエラーから足す前提。ソースからのデプロイでは、
# ビルドに使われるサービスアカウント（既定の Compute Engine のものなど）に対する iam.serviceAccountUser が
# 足りないと、権限エラーになることがある。その場合は、エラーに出たサービスアカウントに対して付与する。
set -euo pipefail

# 書き込みなので、本人の ID で動かす（コンテナの既定は docunavi-viewer へのなりすまし。書き込みは 403 になる）
unset CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT

project=""
assume_yes=false
for arg in "$@"; do
  case "$arg" in
    -y | --yes) assume_yes=true ;;
    *) project="$arg" ;;
  esac
done
project="${project:-${GCP_PROJECT_ID:-}}"
if [[ -z "$project" ]]; then
  echo "使い方: $0 <プロジェクト ID> [-y]（または環境変数 GCP_PROJECT_ID）" >&2
  exit 2
fi
if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud が見つかりません。ホストまたは Cloud Shell で実行してください。" >&2
  exit 1
fi
if [[ -n "$(gcloud config get-value auth/impersonate_service_account 2>/dev/null || true)" ]]; then
  echo "auth/impersonate_service_account が設定されています。書き込みが拒否されるので、次を実行してからやり直してください:" >&2
  echo "  gcloud config unset auth/impersonate_service_account" >&2
  exit 1
fi

account="$(gcloud config get-value account 2>/dev/null || true)"
if [[ -z "$account" ]]; then
  echo "gcloud にログインしていません。先に gcloud auth login を実行してください。" >&2
  exit 1
fi
if [[ "$account" == *.gserviceaccount.com ]]; then member="serviceAccount:$account"; else member="user:$account"; fi

sa_name="agent-deployer"
service_account="${sa_name}@${project}.iam.gserviceaccount.com"
run_sa_name="${RUN_SA_NAME:-docunavi-run}"
run_service_account="${run_sa_name}@${project}.iam.gserviceaccount.com"
# run.sourceDeveloper: ソースからのデプロイ（Cloud Run の作成・更新、ビルドの実行、ソース用バケットと
#   Artifact Registry のリポジトリの作成）。サービスの IAM ポリシー（allUsers の付与など）は変えられない。
# logging.viewer: デプロイ中のビルドログの表示
roles=(roles/run.sourceDeveloper roles/logging.viewer)

echo "プロジェクト:         $project"
echo "サービスアカウント:   $service_account（デプロイ用。無ければ作成）"
echo "付与するロール:       ${roles[*]}（プロジェクト）"
echo "実行用 SA への許可:   $service_account に roles/iam.serviceAccountUser（$run_service_account に対して。無ければ飛ばす）"
echo "トークン発行を許可:   $member（roles/iam.serviceAccountTokenCreator を、このサービスアカウントに対して）"
echo "注意: サービスの公開（allUsers への roles/run.invoker）と API の有効化は、付与しない。本人の ID で行う。"
if [[ "$assume_yes" != true ]]; then
  read -r -p "実行しますか？ [y/N] " answer
  [[ "$answer" == y || "$answer" == Y ]] || { echo "中止しました。" >&2; exit 1; }
fi

if ! gcloud iam service-accounts describe "$service_account" --project "$project" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$sa_name" --display-name="Agent deployer (Cloud Run)" --project "$project"
fi

for role in "${roles[@]}"; do
  gcloud projects add-iam-policy-binding "$project" \
    --member="serviceAccount:${service_account}" --role="$role" --condition=None >/dev/null
  echo "付与しました: $role"
done

if gcloud iam service-accounts describe "$run_service_account" --project "$project" >/dev/null 2>&1; then
  gcloud iam service-accounts add-iam-policy-binding "$run_service_account" \
    --member="serviceAccount:${service_account}" --role=roles/iam.serviceAccountUser --project "$project" >/dev/null
  echo "付与しました: roles/iam.serviceAccountUser -> $service_account（$run_service_account に対して）"
else
  echo "実行用サービスアカウント $run_service_account が無いので、serviceAccountUser は飛ばしました。" >&2
  echo "作成してから、このスクリプトをもう一度実行してください（docs/deploy.md の「準備」）。" >&2
fi

gcloud iam service-accounts add-iam-policy-binding "$service_account" \
  --member="$member" --role=roles/iam.serviceAccountTokenCreator --project "$project" >/dev/null
echo "付与しました: roles/iam.serviceAccountTokenCreator -> $member"

echo "完了しました。デプロイは、このサービスアカウントを明示してなりすます（docs/deploy.md）:"
echo "  gcloud run deploy ... --impersonate-service-account=$service_account"
