# 開発コンテナのイメージ。CLI とシステムのライブラリーは、ここで入れる（再ビルドで層のキャッシュが効く）。
# 入れるもの: gcloud・gh・psql・docker-cli・docker-compose（apt）、Playwright の Chromium に要るライブラリー（apt）、Go（公式の tar）。
# 入れないもの: Chromium 本体（名前付きボリューム playwright-browsers が ~/.cache/ms-playwright を重ねるので、postCreateCommand の
# install-playwright.sh が取得する）、Vercel CLI（入れない。必要なときは npx --yes vercel@63.1.2。README の「開発環境の Vercel CLI」）。
#
# 層の順は、変わりにくいものが上。ツールの版を上げたときに作り直すのは、その層から下だけになる。
# 「Rebuild Container」は層のキャッシュを使う。「Rebuild Without Cache」は使わないので、全部を入れ直す。
# apt と Go の tar は BuildKit の cache mount に残す（層を作り直すときに、取得済みのものを使うため）。
# cache mount はビルドの結果に入らない。prune や、使わない期間が長いときの自動回収で消えても、ビルドは正しく進む（遅くなるだけ）。
#
# gcloud をイメージのビルド時に入れる理由: MCP の headersHelper（scripts/mcp-auth-header.sh）は、コンテナの起動の約 11 秒後に
# Claude Code が実行する。postCreateCommand で入れると、導入が終わる（約 20 秒後）より前にヘルパーが走り、
# gcloud が無いまま失敗して、「認証が必要」の記録が書かれた（2026-10-21 の再ビルドで確認）。
#
# 公式の apt リポジトリから入れる。コミュニティの feature（dhoeric/google-cloud-cli）は鍵の登録に apt-key を使い、
# Debian 13（trixie）ではビルドが失敗するため使わない。gpg --dearmor と signed-by で登録する（gh も同じ形）。
# このイメージにログイン情報は含めない（ログインは README の「開発環境の gcloud（ログインとガード）」と
# 「開発環境の GitHub CLI（gh）」の手順で、ユーザーが行う）。
# ビルドに失敗したら、コンテナの作成も失敗する（postCreateCommand のときと違って、ツールが無いままでは起動しない）。
# 戻し方は、この変更のコミットを git revert して、再ビルドする（ボリュームには触れないので、ログインは残る）。
FROM mcr.microsoft.com/devcontainers/base:trixie

# Debian のイメージは、apt が取得した .deb を自動で消す設定（docker-clean）を持つ。cache mount に .deb を残すため、外す。
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

# apt の前提パッケージと、gcloud・gh の鍵と apt リポジトリの登録。
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    export DEBIAN_FRONTEND=noninteractive \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends apt-transport-https ca-certificates curl gnupg \
    && install -d -m 755 /etc/apt/keyrings \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor --yes -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list

# 日常のツール: gcloud、gh、psql（postgresql-client）、Docker の CLI と Compose（docker compose のプラグイン）。
# Docker は Debian 13 のパッケージで、ソケットもデーモンも含まない。
# 接続先は DOCKER_HOST（devcontainer.json）の読み取り専用プロキシだけ（.devcontainer/README.md の「開発環境の Docker」）。
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    export DEBIAN_FRONTEND=noninteractive \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends google-cloud-cli gh postgresql-client docker-cli docker-compose \
    && gcloud --version \
    && gh --version \
    && psql --version \
    && docker --version \
    && docker compose version

# Playwright の Chromium に要るライブラリーとフォント（`playwright install-deps chromium` が入れるものと同じ 32 件）。
# 一覧の出どころ: playwright-core の依存の定義（debian13 の tools 11 件 + chromium 21 件。arm64 は x64 と同じ一覧）。
# 版は install-playwright.sh の PLAYWRIGHT_VERSION（1.64.0-alpha-1790635538000）に合わせてある。
# 版を上げるときは、この一覧も作り直す（install-deps の依存の定義を読むか、新しい版で `npx playwright install-deps --dry-run chromium`
# を実行して、足りないパッケージを確かめる）。取りこぼしは、postCreateCommand の Chromium の起動確認（--version）で分かる。
# Chromium 本体は、ここでは入れない（上の「入れないもの」）。
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    export DEBIAN_FRONTEND=noninteractive \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends \
        xvfb \
        fonts-noto-color-emoji fonts-unifont xfonts-scalable fonts-liberation fonts-ipafont-gothic \
        fonts-wqy-zenhei fonts-tlwg-loma-otf fonts-freefont-ttf \
        libfontconfig1 libfreetype6 \
        libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 libdbus-1-3 \
        libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 \
        libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2

# Go（ops/sse-relay/ 用。学習目的。docs/target-architecture.md の「Go の SSE 中継」）。公式の配布物を、公式が公開している SHA-256 と照合して入れる。
# 版とハッシュは .devcontainer/install-go.sh と同じ値（arm64 のハッシュだけを固定している。ほかのアーキテクチャではビルドが失敗する）。
# 上げるときは、公開日を確かめてから、両方を書き換える。置き場所は /usr/local/go。
# tar は cache mount（/var/cache/go-dl）に残す。ハッシュが合わなければ、残っていたファイルを消して失敗する（次のビルドで取り直す）。
ARG GO_VERSION=1.26.10
ARG GO_SHA256_ARM64=3f4227ce7b206a513e794f1393adb473a07b46e94d18bd15d1c684bbb7319797
RUN --mount=type=cache,target=/var/cache/go-dl,sharing=locked \
    set -eu; \
    case "$(uname -m)" in \
      aarch64 | arm64) arch=arm64; expected="${GO_SHA256_ARM64}" ;; \
      *) echo "未対応のアーキテクチャです: $(uname -m)（arm64 のハッシュだけを固定している）" >&2; exit 1 ;; \
    esac; \
    archive="go${GO_VERSION}.linux-${arch}.tar.gz"; \
    file="/var/cache/go-dl/${archive}"; \
    if [ ! -f "${file}" ]; then \
      curl -fsSL -o "${file}.part" "https://go.dev/dl/${archive}"; \
      mv "${file}.part" "${file}"; \
    fi; \
    if ! echo "${expected}  ${file}" | sha256sum -c -; then \
      rm -f "${file}"; \
      echo "SHA-256 が一致しません。導入を中止します" >&2; \
      exit 1; \
    fi; \
    rm -rf /usr/local/go; \
    tar -C /usr/local -xzf "${file}"; \
    ln -sf /usr/local/go/bin/go /usr/local/bin/go; \
    ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt; \
    go version
