#!/usr/bin/env bash
# 閲覧専用サービスアカウント docunavi-viewer の作成と IAM の付与を、一度だけ行う。
# サービスアカウントは運用用のプロジェクト（docunavi-ops）に置き、閲覧の対象（docunavi-app）には閲覧ロールだけを付ける。
# 権限のあるアカウントでログインした状態で実行する（ホスト、Cloud Shell、またはログイン済みの開発コンテナで、
# ユーザーが実行する。エージェントは実行しない: 書き込みなのでフックが止める）。
# .devcontainer/README.md の「開発環境の gcloud（ログインとガード）」の手順 2。
#
#   ./scripts/setup-agent-viewer.sh <運用用プロジェクト ID> <対象プロジェクト ID>        # 内容を表示して確認を取る
#   ./scripts/setup-agent-viewer.sh <運用用プロジェクト ID> <対象プロジェクト ID> -y     # 確認なし
#   OPS_PROJECT_ID=<運用用> TARGET_PROJECT_ID=<対象> ./scripts/setup-agent-viewer.sh
#
# 運用用のプロジェクトは先に作っておく（課金は不要）。付与するのは閲覧ロールだけ。冪等なので、やり直してもよい。
# 作成直後は IAM の反映に数分かかり、「does not exist」や「Failed to impersonate」が出ることがある（待って、やり直す）。
# 終わったら、.devcontainer/README.md の手順 3 へ進む。
#
# 書き込みなので、本人の ID で動かす。コンテナの既定（環境変数で docunavi-viewer になりすます）が残っていると、
# 書き込みが 403 になる（初回は SA がまだ無く、なりすましの取得自体に失敗する）ので、下で外す。
set -euo pipefail

unset CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT

assume_yes=false
positional=()
for arg in "$@"; do
  case "$arg" in
    -y | --yes) assume_yes=true ;;
    *) positional+=("$arg") ;;
  esac
done
ops_project="${positional[0]:-${OPS_PROJECT_ID:-}}"
target_project="${positional[1]:-${TARGET_PROJECT_ID:-}}"
if [[ -z "$ops_project" || -z "$target_project" ]]; then
  echo "使い方: $0 <運用用プロジェクト ID> <対象プロジェクト ID> [-y]（または環境変数 OPS_PROJECT_ID と TARGET_PROJECT_ID）" >&2
  exit 2
fi
if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud が見つかりません。ホストまたは Cloud Shell で実行してください。" >&2
  exit 1
fi
# 設定ファイル側（gcloud config set）でなりすましが固定されていないことも確かめる。こちらは外さずに止める
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
# トークンを発行する主体（自分）。サービスアカウントでログインしている場合の書き分け
if [[ "$account" == *.gserviceaccount.com ]]; then member="serviceAccount:$account"; else member="user:$account"; fi

sa_name="docunavi-viewer"
service_account="${sa_name}@${ops_project}.iam.gserviceaccount.com"
# serviceUsageConsumer: なりすまし中の API の利用を対象プロジェクトに数えさせる（--billing-project）ために要る。
# 運用用のプロジェクトに課金を付けずに済み、Cloud Build の閲覧も通る
roles=(roles/cloudbuild.builds.viewer roles/run.viewer roles/logging.viewer roles/serviceusage.serviceUsageConsumer)

echo "運用用プロジェクト:   $ops_project（サービスアカウントを置く。課金は不要）"
echo "対象プロジェクト:     $target_project（ここの閲覧だけを許す）"
echo "サービスアカウント:   $service_account（閲覧専用。無ければ作成）"
echo "付与するロール:       ${roles[*]}（$target_project に対して）"
echo "トークン発行を許可:   $member（roles/iam.serviceAccountTokenCreator を、このサービスアカウントに対して）"
echo "有効化する API:       iamcredentials.googleapis.com（$ops_project）"
echo "注意: roles/logging.viewer は $target_project 内のログをすべて読める。別用途のログがあるなら外す（このスクリプトを編集）。"
if [[ "$assume_yes" != true ]]; then
  read -r -p "実行しますか？ [y/N] " answer
  [[ "$answer" == y || "$answer" == Y ]] || { echo "中止しました。" >&2; exit 1; }
fi

gcloud services enable iamcredentials.googleapis.com --project "$ops_project"

if ! gcloud iam service-accounts describe "$service_account" --project "$ops_project" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$sa_name" --display-name="DocuNavi viewer" --project "$ops_project"
fi

gcloud iam service-accounts add-iam-policy-binding "$service_account" \
  --member="$member" --role=roles/iam.serviceAccountTokenCreator --project "$ops_project" >/dev/null
echo "付与しました: roles/iam.serviceAccountTokenCreator -> $member"

for role in "${roles[@]}"; do
  gcloud projects add-iam-policy-binding "$target_project" \
    --member="serviceAccount:${service_account}" --role="$role" --condition=None >/dev/null
  echo "付与しました: $role"
done

echo "完了しました。日常の ID の固定は、コンテナの環境変数（.devcontainer/devcontainer.json の containerEnv）で行う。"
echo "再ビルド前の暫定: export CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT=$service_account"
