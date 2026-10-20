#!/usr/bin/env bash
# 閲覧専用サービスアカウント docunavi-viewer の短期アクセストークン（既定で約 1 時間）を発行し、
# リポジトリ直下の .gcloud-token に書き出す。ログインを持たせない代替方式（README の「代替：ログインを持たせない方式」）。
# 通常のログイン方式では使わない。
#
# ホストで実行する（コンテナにはログイン情報を置かない）。
#   ./scripts/issue-agent-token.sh <運用用プロジェクト ID>      # サービスアカウントを置いたプロジェクト（docunavi-ops）
#   GCP_PROJECT_ID=<運用用プロジェクト ID> ./scripts/issue-agent-token.sh
#
# 事前に、サービスアカウントの作成と IAM の付与が要る。手順は README の「開発環境の gcloud（ログインとガード）」。
set -euo pipefail

project="${1:-${GCP_PROJECT_ID:-}}"
if [[ -z "$project" ]]; then
  echo "使い方: $0 <運用用プロジェクト ID>（または環境変数 GCP_PROJECT_ID）" >&2
  exit 2
fi
if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud が見つかりません。ホストで実行してください。" >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$repo_root/.gcloud-token"
service_account="docunavi-viewer@${project}.iam.gserviceaccount.com"

# 自分以外に読ませない。書き込みの途中や失敗で、古い・空のトークンを残さないよう一時ファイル経由にする
umask 077
tmp="$(mktemp "$repo_root/.gcloud-token.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

gcloud auth print-access-token --impersonate-service-account="$service_account" >"$tmp"
if [[ ! -s "$tmp" ]]; then
  echo "トークンが空でした。" >&2
  exit 1
fi

mv "$tmp" "$out"
trap - EXIT
# トークンの中身は表示しない
echo "発行しました: $out（有効期限は約 1 時間。失効したら、もう一度実行する）" >&2
