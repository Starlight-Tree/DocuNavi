#!/usr/bin/env bash
# 閲覧専用サービスアカウント agent-viewer の作成と IAM の付与を、一度だけ行う。
# 権限のあるアカウントでログインした状態で実行する（ホスト、Cloud Shell、またはログイン済みの開発コンテナで、
# ユーザーが実行する。エージェントは実行しない: 書き込みなのでフックが止める）。
# README の「開発環境の gcloud（ログインとガード）」の手順 2。
#
#   ./scripts/setup-agent-viewer.sh <プロジェクト ID>        # 内容を表示して確認を取る
#   ./scripts/setup-agent-viewer.sh <プロジェクト ID> -y     # 確認なし
#   GCP_PROJECT_ID=<プロジェクト ID> ./scripts/setup-agent-viewer.sh
#
# 付与するのは閲覧ロールだけ。冪等なので、やり直してもよい。終わったら、README の手順 3（日常の ID を固定する）へ進む。
set -euo pipefail

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

account="$(gcloud config get-value account 2>/dev/null || true)"
if [[ -z "$account" ]]; then
  echo "gcloud にログインしていません。先に gcloud auth login を実行してください。" >&2
  exit 1
fi
# トークンを発行する主体（自分）。サービスアカウントでログインしている場合の書き分け
if [[ "$account" == *.gserviceaccount.com ]]; then member="serviceAccount:$account"; else member="user:$account"; fi

sa_name="agent-viewer"
service_account="${sa_name}@${project}.iam.gserviceaccount.com"
roles=(roles/cloudbuild.builds.viewer roles/run.viewer roles/logging.viewer)

echo "プロジェクト:         $project"
echo "サービスアカウント:   $service_account（閲覧専用。無ければ作成）"
echo "付与するロール:       ${roles[*]}"
echo "トークン発行を許可:   $member（roles/iam.serviceAccountTokenCreator を、このサービスアカウントに対して）"
echo "有効化する API:       iamcredentials.googleapis.com"
echo "注意: roles/logging.viewer はプロジェクト内のログをすべて読める。別用途のログがあるなら外す（このスクリプトを編集）。"
if [[ "$assume_yes" != true ]]; then
  read -r -p "実行しますか？ [y/N] " answer
  [[ "$answer" == y || "$answer" == Y ]] || { echo "中止しました。" >&2; exit 1; }
fi

gcloud services enable iamcredentials.googleapis.com --project "$project"

if ! gcloud iam service-accounts describe "$service_account" --project "$project" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$sa_name" --display-name="Agent read-only" --project "$project"
fi

for role in "${roles[@]}"; do
  gcloud projects add-iam-policy-binding "$project" \
    --member="serviceAccount:${service_account}" --role="$role" --condition=None >/dev/null
  echo "付与しました: $role"
done

gcloud iam service-accounts add-iam-policy-binding "$service_account" \
  --member="$member" --role=roles/iam.serviceAccountTokenCreator --project "$project" >/dev/null
echo "付与しました: roles/iam.serviceAccountTokenCreator -> $member"

echo "完了しました。次: gcloud config set auth/impersonate_service_account $service_account"
