#!/usr/bin/env bash
# scripts/test-postgres-up.sh で起動した一時的な Postgres を止める。起動していなければ何もしない。
#
#   scripts/test-postgres-down.sh           # 停止だけ（取得した .deb と展開物、データは残す。次の起動が速い）
#   scripts/test-postgres-down.sh --purge   # 停止して、/tmp/pg（既定）ごと消す（次の起動で再取得する）
#
# 環境変数（省略可）: TEST_PG_DIR（既定 /tmp/pg）。--purge は、そのディレクトリが `<dir>/data/PG_VERSION` を持つ
# ときだけ消す（誤って別のディレクトリを消さないため）。
set -euo pipefail

dir="${TEST_PG_DIR:-/tmp/pg}"
bin="$dir/root/usr/lib/postgresql/17/bin"
purge=0
case "${1:-}" in
  "") ;;
  --purge) purge=1 ;;
  *) echo "使い方: $0 [--purge]" >&2; exit 2 ;;
esac

export LD_LIBRARY_PATH="$dir/root/usr/lib/$(uname -m)-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

if [ -x "$bin/pg_ctl" ] && "$bin/pg_ctl" -D "$dir/data" status >/dev/null 2>&1; then
  "$bin/pg_ctl" -D "$dir/data" -m fast -w stop >&2
  echo "停止しました。" >&2
else
  echo "起動していません。" >&2
fi

if [ "$purge" = 1 ]; then
  if [ -f "$dir/data/PG_VERSION" ]; then
    rm -rf -- "$dir"
    echo "削除しました: $dir" >&2
  else
    echo "$dir/data/PG_VERSION が無いので、削除しません。" >&2
    exit 1
  fi
fi
