#!/usr/bin/env bash
# Cloudflare の親トークン（R2 キーの発行専用）を、Secret Manager の cloudflare-issuer-token に新しい版として入れる。
# 値は、画面に出ない入力で受け取り、標準入力で gcloud に渡す（コマンドの引数・ファイル・履歴・ログに残さない）。
# 古い有効な版は無効にする。ユーザーが自分の端末で実行する（エージェントは実行しない・値を見ない）。
#
#   ./scripts/store-cloudflare-parent-token.sh <プロジェクト ID>
#
# 親トークンは Cloudflare の画面（アカウント API トークン）で作る。権限は「Account API Tokens: Edit」だけから始める
# （足りなければ、Job の check の出力に従う）。有効期限は必ず付ける（docs/r2-key-issuer.md の「親トークンの期限」）。
set -euo pipefail

# 書き込みなので、本人の ID で動かす
unset CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT

project="${1:-${GCP_PROJECT_ID:-}}"
if [[ -z "$project" ]]; then
  echo "使い方: $0 <プロジェクト ID>" >&2
  exit 2
fi
secret="cloudflare-issuer-token"

# 安全装置（scripts/setup-r2-key-issuer.sh と同じ考え方）: 対話端末でなければ、gcloud を呼ばずに止まる。
# テスト用の差し替え（GCLOUD_BIN）は、絶対パスで実行でき '# STUB-GCLOUD' の行を持つ偽物だけ。
GCLOUD="gcloud"
stub=false
if [[ -n "${GCLOUD_BIN:-}" ]]; then
  if [[ "$GCLOUD_BIN" == /* && -f "$GCLOUD_BIN" && -x "$GCLOUD_BIN" ]] && grep -qx '# STUB-GCLOUD' "$GCLOUD_BIN"; then
    GCLOUD="$GCLOUD_BIN"
    stub=true
  else
    echo "GCLOUD_BIN は、絶対パスの実行できる偽物だけ使えます。何も実行せず終了します。" >&2
    exit 2
  fi
fi
if [[ "$stub" != true && ! ( -t 0 && -t 1 ) ]]; then
  echo "対話端末ではないので、何も実行しません（gcloud は呼んでいません）。あなた自身の端末で実行してください。" >&2
  exit 3
fi

read -r -s -p "Cloudflare の親トークン（入力は画面に出ません）: " token
echo
if [[ -z "$token" ]]; then
  echo "空です。中止しました。" >&2
  exit 1
fi
length=${#token}

new="$(printf '%s' "$token" | "$GCLOUD" secrets versions add "$secret" --data-file=- --project "$project" \
  --format='value(name)' 2>/dev/null | awk -F/ '{print $NF}')"
unset token
if [[ -z "$new" ]]; then
  echo "保存に失敗しました（Secret ${secret} があるか、権限があるかを確かめてください）。" >&2
  exit 1
fi

# 新しい版より前の有効な版は無効にする（古い親トークンを残さない）
while read -r v; do
  [[ -n "$v" && "$v" != "$new" ]] && "$GCLOUD" secrets versions disable "$v" --secret="$secret" --project "$project" >/dev/null
done < <("$GCLOUD" secrets versions list "$secret" --project "$project" --filter='state=ENABLED' --format='value(name)' | awk -F/ '{print $NF}')

echo "保存しました: ${secret} 版 ${new}（${length} 文字。値は表示していません）。古い版は無効にしました。"
