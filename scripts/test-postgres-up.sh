#!/usr/bin/env bash
# Postgres のテスト（TEST_DATABASE_URL があるときだけ動く）用に、一時的な Postgres 17 を起動する。
# 手順は docs/development.md の「テスト」節と同じ。root なしで Debian のパッケージを展開して使う
# （開発コンテナには Postgres も Docker も無い）。認証は trust で、ループバックだけで待ち受ける。
#
#   TEST_DATABASE_URL=$(scripts/test-postgres-up.sh) pytest
#   scripts/test-postgres-down.sh
#
# 標準出力には接続 URL（1 行）だけを出す。経過は標準エラー。親シェルの環境変数は子プロセスから変えられないので、
# 上のように $(...) で受けて渡す（`make test-db` がこの形で一括実行する）。
#
# ネットワークを使う: 初回だけ `apt-get download` で postgresql-17 / postgresql-client-17 / libpq5 の .deb
# （合計およそ 20MB）を取得する。展開済みなら取得しない。起動済みなら、何もせず URL だけを出す。
#
# 環境変数（省略可）: TEST_PG_DIR（既定 /tmp/pg）、TEST_PG_PORT（既定 55432）。
set -euo pipefail

dir="${TEST_PG_DIR:-/tmp/pg}"
port="${TEST_PG_PORT:-55432}"
bin="$dir/root/usr/lib/postgresql/17/bin"
url="postgresql://postgres@127.0.0.1:$port/postgres"

log() { printf '%s\n' "$*" >&2; }

# 展開した共有ライブラリを見つけさせる（libpq5 など）
export LD_LIBRARY_PATH="$dir/root/usr/lib/$(uname -m)-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

if [ -x "$bin/pg_ctl" ] && "$bin/pg_ctl" -D "$dir/data" status >/dev/null 2>&1; then
  log "起動済み: $dir/data（ポート $port）"
  printf '%s\n' "$url"
  exit 0
fi

# 別のものがそのポートを使っていたら、止めずに失敗する（他のセッションの Postgres かもしれない）
if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
  log "ポート $port は使用中です。TEST_PG_PORT で別のポートを指定してください。"
  exit 1
fi

if [ ! -x "$bin/pg_ctl" ]; then
  log "Postgres 17 を取得します（apt-get download。ネットワークを使います）"
  mkdir -p "$dir/debs" "$dir/root"
  (cd "$dir/debs" && apt-get download postgresql-17 postgresql-client-17 libpq5 >&2)
  for deb in "$dir"/debs/*.deb; do dpkg -x "$deb" "$dir/root"; done
fi

mkdir -p "$dir/sock"
if [ ! -f "$dir/data/PG_VERSION" ]; then
  "$bin/initdb" -D "$dir/data" -U postgres --auth=trust -E UTF8 --locale=C.UTF-8 >"$dir/initdb.log" 2>&1
fi

"$bin/pg_ctl" -D "$dir/data" -l "$dir/pg.log" -w \
  -o "-p $port -c listen_addresses=127.0.0.1 -c unix_socket_directories=$dir/sock" start >&2
log "起動しました（ポート $port）。終わったら scripts/test-postgres-down.sh で止めてください。"
printf '%s\n' "$url"
