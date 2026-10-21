# 開発コンテナのイメージ。gcloud だけをここで入れる（ほかの CLI は postCreateCommand の install-*.sh）。
#
# gcloud をイメージのビルド時に入れる理由: MCP の headersHelper（scripts/mcp-auth-header.sh）は、コンテナの起動の約 11 秒後に
# Claude Code が実行する。postCreateCommand で入れると、導入が終わる（約 20 秒後）より前にヘルパーが走り、
# gcloud が無いまま失敗して、「認証が必要」の記録が書かれた（2026-10-21 の再ビルドで確認）。
#
# 公式の apt リポジトリから入れる。コミュニティの feature（dhoeric/google-cloud-cli）は鍵の登録に apt-key を使い、
# Debian 13（trixie）ではビルドが失敗するため使わない。gpg --dearmor と signed-by で登録する。
# このイメージにログイン情報は含めない（ログインは README の「開発環境の gcloud（ログインとガード）」の手順で、ユーザーが行う）。
# ビルドに失敗したら、コンテナの作成も失敗する（postCreateCommand のときと違って、gcloud 無しでは起動しない）。
FROM mcr.microsoft.com/devcontainers/base:trixie

RUN export DEBIAN_FRONTEND=noninteractive \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends apt-transport-https ca-certificates curl gnupg \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor --yes -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends google-cloud-cli \
    && rm -rf /var/lib/apt/lists/* \
    && gcloud --version

# Docker の CLI と Compose（docker compose のプラグイン）。Debian 13 のパッケージで、ソケットもデーモンも含まない。
# 接続先は DOCKER_HOST（devcontainer.json）の読み取り専用プロキシだけ（.devcontainer/README.md の「開発環境の Docker」）。
RUN export DEBIAN_FRONTEND=noninteractive \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends docker-cli docker-compose \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version \
    && docker compose version
