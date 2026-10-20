#!/usr/bin/env bash
# psql（postgresql-client）を apt で入れる（コンテナの作成時に postCreateCommand から呼ぶ）。
#
# 本番 DB を読み取り専用のロール（readonly）で見るために使う（docs/infra-readonly-role.sql）。
# 接続情報（~/.pgpass）は入れない。パスワードをイメージにもリポジトリにも置かないため、ユーザーが作る。
# このスクリプトは失敗しても止まらない（psql が無いだけで、他の作業はできる）。
set -uo pipefail

if command -v psql >/dev/null 2>&1; then
  echo "psql は導入済みです: $(psql --version 2>/dev/null | head -1)"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y &&
  sudo apt-get install -y --no-install-recommends postgresql-client &&
  echo "psql を導入しました: $(psql --version 2>/dev/null | head -1)" ||
  echo "psql の導入に失敗しました（作業は続けられます）" >&2
exit 0
