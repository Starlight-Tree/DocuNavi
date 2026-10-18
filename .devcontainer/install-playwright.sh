#!/usr/bin/env bash
# Playwright MCP が使う Chromium と、その実行に必要なシステムのライブラリーを入れる（コンテナの作成時に postCreateCommand から呼ぶ）。
#
# 版は @playwright/mcp@0.0.83（.mcp.json）が依存する playwright の alpha 版に合わせて固定する。
# MCP と同じ版でないと、Chromium のリビジョンが合わない（この版では chromium-1247）。
# ブラウザーは ~/.cache/ms-playwright（名前付きボリューム）に入るので、再ビルドしても取得し直さない。
# システムのライブラリーは apt で入れるので、再ビルドのたびに入れ直す（取得済みなら何もしない）。
#
# apt のロックを他の導入（gcloud・gh など）や自動更新が持っていると、ライブラリーの導入が失敗する。
# 数回、間をあけて試し直す。最後に Chromium を実際に起動して（--version）成功を確かめ、
# だめなら理由を stderr に出して終了コード 1 で終わる。
# postCreateCommand の cli-tools は ; で順番に呼ぶので、ここが失敗しても他の導入は先に済んでいる。
# 失敗してもコンテナは使える（ブラウザー確認ができないだけ）。直したら再実行する: bash .devcontainer/install-playwright.sh
set -uo pipefail

PLAYWRIGHT_VERSION="1.64.0-alpha-1790635538000"
BROWSERS_DIR="$HOME/.cache/ms-playwright"
DEPS_ATTEMPTS="${PLAYWRIGHT_DEPS_ATTEMPTS:-8}"
DEPS_WAIT_SECONDS="${PLAYWRIGHT_DEPS_WAIT_SECONDS:-15}"

fail() {
  echo "エラー: Playwright の導入に失敗しました: $1" >&2
  echo "  Playwright MCP のブラウザー確認はできません。直して再実行: bash .devcontainer/install-playwright.sh" >&2
  exit 1
}

install_chromium() {
  # 新規作成直後のボリュームは root 所有で、親の ~/.cache も root 所有で作られることがある
  sudo mkdir -p "$BROWSERS_DIR" &&
    sudo chown "$(id -u):$(id -g)" "$HOME/.cache" &&
    sudo chown -R "$(id -u):$(id -g)" "$BROWSERS_DIR" &&
    PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_DIR" npx -y "playwright@${PLAYWRIGHT_VERSION}" install chromium
}

install_deps() {
  # install-deps は、Chromium に要るライブラリーとフォント（日本語の IPAex ゴシックを含む）を apt で入れる。
  # sudo の PATH に npx が無いので、PATH を渡す
  sudo env "PATH=$PATH" npx -y "playwright@${PLAYWRIGHT_VERSION}" install-deps chromium
}

chromium_path() {
  # 版を固定しているので 1 つだけのはずだが、複数あれば最後（新しい方）を使う
  ls -d "$BROWSERS_DIR"/chromium-*/chrome-linux*/chrome 2>/dev/null | tail -n 1
}

if ! command -v npx >/dev/null 2>&1; then
  fail "npx が見つかりません（node の feature が入っていない可能性があります）"
fi

install_chromium || fail "Chromium の取得に失敗しました（ネットワークまたはボリュームの権限を確認してください）"

for attempt in $(seq 1 "$DEPS_ATTEMPTS"); do
  if install_deps; then
    break
  fi
  if [ "$attempt" -eq "$DEPS_ATTEMPTS" ]; then
    fail "ライブラリーの導入が ${DEPS_ATTEMPTS} 回失敗しました（apt のロックを他の処理が持っている可能性があります）"
  fi
  echo "ライブラリーの導入に失敗しました（${attempt}/${DEPS_ATTEMPTS} 回目）。${DEPS_WAIT_SECONDS} 秒待って試し直します。" >&2
  sleep "$DEPS_WAIT_SECONDS"
done

chrome="$(chromium_path)"
[ -n "$chrome" ] || fail "Chromium の実行ファイルが見つかりません（$BROWSERS_DIR）"
version="$("$chrome" --version 2>&1)" || fail "Chromium が起動しません: $version"

echo "Playwright の Chromium とライブラリーを導入しました（playwright@${PLAYWRIGHT_VERSION}、${version}）。"
