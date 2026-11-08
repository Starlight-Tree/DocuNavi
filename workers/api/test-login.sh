#!/usr/bin/env bash
# 本番の自動テスト用ログイン（TEST_LOGIN_TOKEN）を、使うときだけ有効にする。**ホストのターミナルで実行する**（wrangler のログインが要る）。
#
#   bash workers/api/test-login.sh status   # Worker の Secret の名前を表示する（値は出ない）
#   bash workers/api/test-login.sh on       # Infisical の値を Worker の Secret に入れる → 有効
#   bash workers/api/test-login.sh off      # TEST_LOGIN_TOKEN を削除する → 無効（テストユーザーは DB に残す）
#
# 値の原本は Infisical（プロジェクト f451ae41…・環境 dev）。ここから標準入力で wrangler に渡すので、画面にもシェル履歴にも出ない。
# 有効にするのは、scripts/prod_smoke_chat.py を実行する間だけ。終わったら必ず off。
# 使い方と限界は docs/development.md の「本番の /chat の自動テスト」。
set -euo pipefail
cd "$(dirname "$0")"

PROJECT_ID=<INFISICAL_PROJECT_ID>
ENV_NAME=dev

case "${1:-}" in
  status)
    npx wrangler secret list
    ;;
  on)
    infisical sm run --silent --projectId "$PROJECT_ID" --env="$ENV_NAME" -- bash -c '
      set -euo pipefail
      : "${TEST_LOGIN_TOKEN:?TEST_LOGIN_TOKEN が Infisical にありません}" "${TEST_LOGIN_EMAIL:?TEST_LOGIN_EMAIL が Infisical にありません}"
      printf %s "$TEST_LOGIN_EMAIL" | npx wrangler secret put TEST_LOGIN_EMAIL
      printf %s "$TEST_LOGIN_TOKEN" | npx wrangler secret put TEST_LOGIN_TOKEN'
    echo "有効にしました。テストが終わったら: bash workers/api/test-login.sh off"
    ;;
  off)
    # 確認のプロンプトがあってもなくても進むよう y を渡す。Secret が既に無ければ失敗するが、無効という結果は同じ
    printf 'y\n' | npx wrangler secret delete TEST_LOGIN_TOKEN || echo "（TEST_LOGIN_TOKEN は既にありませんでした）"
    echo "無効にしました。パスワードの入口は 400 に戻ります。"
    ;;
  *)
    echo "使い方: bash workers/api/test-login.sh status|on|off" >&2
    exit 2
    ;;
esac
