#!/usr/bin/env bash
# Vercel CLI を npm で入れる（コンテナの作成時に postCreateCommand から呼ぶ）。
#
# frontend-next を Vercel へ手動でデプロイするために使う（docs/target-architecture.md の「段階 7」）。
# 版は固定する（公開から 2 週間以上たった版。上げるときは、公開日を確かめてからここを書き換える）。
# ログイン情報（~/.local/share/com.vercel.cli）は入れず、ボリュームにも残さない。ログインはデプロイのたびに
# `vercel login` で行い、終わったら `vercel logout`（.devcontainer/README.md の「開発環境の Vercel CLI」）。
# このスクリプトは失敗しても止まらない（vercel が無いだけで、他の作業はできる）。
set -uo pipefail

VERCEL_VERSION="63.1.2"

if command -v vercel >/dev/null 2>&1 && [ "$(vercel --version 2>/dev/null | tail -1)" = "${VERCEL_VERSION}" ]; then
  echo "Vercel CLI は導入済みです: ${VERCEL_VERSION}"
  exit 0
fi

npm install -g "vercel@${VERCEL_VERSION}" &&
  echo "Vercel CLI を導入しました: $(vercel --version 2>/dev/null | tail -1)" ||
  echo "Vercel CLI の導入に失敗しました（作業は続けられます）" >&2
exit 0
