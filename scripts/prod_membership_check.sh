#!/usr/bin/env bash
# 本番の利用資格（MEMBERSHIP_MODE=enforce）の自動確認。テスト専用ユーザーで、断られることと通ることを 1 回ずつ確かめる。
# 開発コンテナの中で実行する（infisical と .venv が要る）。Google ログインは使わない（その経路は、本人が画面で確かめる）。
#
#   bash scripts/prod_membership_check.sh              # ① 資格なしで /chat が 403 → ② 一時的に資格を付ける → ③ /chat が 200 → ④ 資格を外す
#   bash scripts/prod_membership_check.sh --deny-only  # ①だけ（本番の LLM を呼ばない。課金なし）
#
# 前提: 本番の Worker で TEST_LOGIN_TOKEN が有効（docs/testing.md「本番の /chat の自動テスト」。無効だとサインインできず、①で止まる）。
# 書き込み: 本番 DB の app_members に、テストユーザーの行を一時的に 1 行足して、必ず（途中で失敗しても）外す。access_audit に 2 行残る。
# ③は本番の LLM と埋め込みを 1 回呼ぶ（課金）。出力に、メール・ID・トークンは出ない。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

[ -f .dev-local/infisical-ids.env ] && . .dev-local/infisical-ids.env
: "${INFISICAL_PROJECT_ID_PROD:?.dev-local/infisical-ids.env に INFISICAL_PROJECT_ID_PROD がありません}"
: "${INFISICAL_PROJECT_ID_DEV:?.dev-local/infisical-ids.env に INFISICAL_PROJECT_ID_DEV がありません}"
EMAIL="${MEMBERSHIP_CHECK_EMAIL:-e2e-test@docunavi.invalid}"
DENY_ONLY=0
[ "${1:-}" = "--deny-only" ] && DENY_ONLY=1

admin() { # member <サブコマンド…>。DB_BACKEND=pg が要る
  DB_BACKEND=pg infisical run --silent --projectId "$INFISICAL_PROJECT_ID_PROD" --env prod -- \
    .venv/bin/python -m app.access_admin member "$@" 2>&1 | grep -v ' INF '
}
smoke() {
  infisical sm run --silent --projectId "$INFISICAL_PROJECT_ID_DEV" --env=dev -- \
    python3 scripts/prod_smoke_chat.py --send 2>&1 | grep -v ' INF '
}

added=0
cleanup() {
  if [ "$added" = 1 ]; then
    echo "[片付け] 資格を外す"
    admin remove --email "$EMAIL" --apply || echo "警告: 資格を外せませんでした。手で実行: member remove --email <テストユーザー> --apply" >&2
  fi
}
trap cleanup EXIT

fail=0
echo "[事前] テストユーザーの資格"
before=$(admin show --email "$EMAIL")
echo "$before"
case "$before" in *"利用資格: なし"*) ;; *) echo "NG: 事前に資格が付いています。中断します（外さずに終わる）" >&2; exit 1 ;; esac

echo "[①] 資格なし → /chat は 403 のはず"
out=$(smoke); echo "$out"
echo "$out" | grep -q '^/chat: 403' && echo "OK: 403" || { echo "NG: 403 ではありません"; fail=1; }

if [ "$DENY_ONLY" = 0 ] && [ "$fail" = 0 ]; then
  echo "[②] 一時的に資格を付ける"
  admin add --email "$EMAIL" --note membership-check --apply && added=1 || { echo "NG: 資格を付けられません"; fail=1; }
  if [ "$added" = 1 ]; then
    # ①の「資格なし」は、各インスタンスが ACCESS_CACHE_TTL_SECONDS（既定 60 秒）だけ控える。付与は控えを消さないので、待たないと③も 403 になる
    wait_s="${MEMBERSHIP_CACHE_WAIT:-65}"
    echo "[待ち] 判定の控え（TTL 60 秒）が切れるまで ${wait_s} 秒待つ"
    sleep "$wait_s"
    echo "[③] 資格あり → /chat は 200 のはず（本番の LLM を 1 回呼ぶ）"
    out=$(smoke); echo "$out" | cut -c1-120
    echo "$out" | grep -q '^/chat: 200' && echo "OK: 200" || { echo "NG: 200 ではありません"; fail=1; }
  fi
fi

cleanup; added=0
echo "[事後] 資格が外れていること"
after=$(admin show --email "$EMAIL"); echo "$after"
case "$after" in *"利用資格: なし"*) echo "OK" ;; *) echo "NG: 資格が残っています"; fail=1 ;; esac

[ "$fail" = 0 ] && echo "結果: 合格" || echo "結果: 不合格"
exit "$fail"
