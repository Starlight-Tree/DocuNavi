#!/usr/bin/env bash
# Go を公式の配布物から入れる（手動で実行する。学習用の ops/sse-relay/ のため。docs/target-architecture.md の「Go の SSE 中継」）。
#
# 版は固定し、公式が公開している SHA-256 と照合する（go.dev/dl の一覧にある値）。上げるときは、公開日を確かめてから書き換える。
# 置き場所は /usr/local/go。コンテナを作り直すと消える（恒久化するかは、ops/sse-relay/ の測定結果を見てから決める）。
# 戻し方: sudo rm -rf /usr/local/go /usr/local/bin/go /usr/local/bin/gofmt
set -uo pipefail

GO_VERSION="1.26.10"
GO_SHA256_ARM64="3f4227ce7b206a513e794f1393adb473a07b46e94d18bd15d1c684bbb7319797"

if command -v go >/dev/null 2>&1 && [ "$(go env GOVERSION 2>/dev/null)" = "go${GO_VERSION}" ]; then
  echo "Go は導入済みです: go${GO_VERSION}"
  exit 0
fi

case "$(uname -m)" in
  aarch64 | arm64) ARCH="arm64"; EXPECTED="${GO_SHA256_ARM64}" ;;
  *) echo "未対応のアーキテクチャです: $(uname -m)（arm64 のハッシュだけを固定している）" >&2; exit 1 ;;
esac

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
ARCHIVE="go${GO_VERSION}.linux-${ARCH}.tar.gz"

curl -fsSL -o "${TMP}/${ARCHIVE}" "https://go.dev/dl/${ARCHIVE}" || { echo "取得に失敗しました" >&2; exit 1; }
ACTUAL="$(sha256sum "${TMP}/${ARCHIVE}" | cut -d' ' -f1)"
if [ "${ACTUAL}" != "${EXPECTED}" ]; then
  echo "SHA-256 が一致しません。導入を中止します" >&2
  exit 1
fi

sudo rm -rf /usr/local/go &&
  sudo tar -C /usr/local -xzf "${TMP}/${ARCHIVE}" &&
  sudo ln -sf /usr/local/go/bin/go /usr/local/bin/go &&
  sudo ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt &&
  echo "Go を導入しました: $(go version)"
