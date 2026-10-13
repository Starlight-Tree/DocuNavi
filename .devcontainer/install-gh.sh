#!/usr/bin/env bash
# GitHub CLI（gh）を公式の apt リポジトリから入れる（コンテナの作成時に postCreateCommand から呼ぶ）。
#
# feature（ghcr.io/devcontainers/features/github-cli）ではなくスクリプトにしているのは、feature の導入に
# 失敗するとコンテナの作成ごと止まるため（gcloud のコミュニティ feature が trixie で実際にそうなった）。
# このスクリプトは失敗しても止まらない（gh が無いだけで、他の作業はできる）。
# ログインはしない。README の「開発環境の GitHub CLI（gh）」の手順で、ユーザーが行う。
set -uo pipefail

if command -v gh >/dev/null 2>&1; then
  echo "gh は導入済みです: $(gh --version 2>/dev/null | head -1)"
  exit 0
fi

keyring=/etc/apt/keyrings/githubcli-archive-keyring.gpg
list=/etc/apt/sources.list.d/github-cli.list

install_gh() {
  export DEBIAN_FRONTEND=noninteractive
  sudo apt-get update -y &&
    sudo apt-get install -y --no-install-recommends ca-certificates curl &&
    sudo mkdir -p -m 755 /etc/apt/keyrings &&
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee "$keyring" >/dev/null &&
    sudo chmod go+r "$keyring" &&
    echo "deb [arch=$(dpkg --print-architecture) signed-by=$keyring] https://cli.github.com/packages stable main" |
    sudo tee "$list" >/dev/null &&
    sudo apt-get update -y &&
    sudo apt-get install -y --no-install-recommends gh
}

if install_gh; then
  echo "gh を導入しました: $(gh --version | head -1)"
else
  echo "gh の導入に失敗しました（開発コンテナの作成は続けます）。再実行: bash .devcontainer/install-gh.sh" >&2
fi
exit 0
