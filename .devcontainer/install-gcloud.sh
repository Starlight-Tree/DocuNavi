#!/usr/bin/env bash
# Google Cloud CLI を公式の apt リポジトリから入れる（コンテナの作成時に postCreateCommand から呼ぶ）。
#
# コミュニティの feature（dhoeric/google-cloud-cli）は鍵の登録に apt-key を使うが、Debian 13（trixie）の
# 基底イメージには apt-key が無く、導入に失敗してコンテナの作成ごと止まる。そのため feature は使わず、
# gpg --dearmor と signed-by で登録する。このスクリプトはログインしない（ログインは README の「開発環境の gcloud（ログインとガード）」の手順で、ユーザーが行う）。
#
# 失敗しても開発コンテナの作成は止めない（gcloud が無いだけで、他の作業はできる）。
set -uo pipefail

if command -v gcloud >/dev/null 2>&1; then
  echo "gcloud は導入済みです: $(gcloud --version 2>/dev/null | head -1)"
  exit 0
fi

keyring=/usr/share/keyrings/cloud.google.gpg
list=/etc/apt/sources.list.d/google-cloud-sdk.list

install_gcloud() {
  export DEBIAN_FRONTEND=noninteractive
  sudo apt-get update -y &&
    sudo apt-get install -y --no-install-recommends apt-transport-https ca-certificates curl gnupg &&
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor --yes -o "$keyring" &&
    echo "deb [signed-by=$keyring] https://packages.cloud.google.com/apt cloud-sdk main" | sudo tee "$list" >/dev/null &&
    sudo apt-get update -y &&
    sudo apt-get install -y --no-install-recommends google-cloud-cli
}

if install_gcloud; then
  echo "gcloud を導入しました: $(gcloud --version | head -1)"
else
  echo "gcloud の導入に失敗しました（開発コンテナの作成は続けます）。再実行: bash .devcontainer/install-gcloud.sh" >&2
fi
exit 0
