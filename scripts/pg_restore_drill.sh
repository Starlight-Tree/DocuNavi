#!/bin/bash
# 復旧訓練: pg_dump のダンプを、使い捨ての pgvector の Postgres（Docker）に戻し、表の件数を確かめる（docs/pg-backup.md の「復旧訓練」）。
#   ./scripts/pg_restore_drill.sh <ダンプのファイル> [--expect documents=80,chunks=2313] [--shred]
# 本番 DB にも R2 にも触れない（ダンプはあらかじめ手元に取っておく）。出力は、表の名前と件数、成否だけ。ダンプの中身・接続文字列は出さない。
#   --expect  名前=件数 のコンマ区切り。一致しなければ失敗（バックアップ直前の本番の件数と突き合わせるとき）
#   --shred   終わったら、ダンプを消す（`account` のトークンと `jwks` の秘密鍵が入っているため。macOS は rm -P、Linux は shred）
# 環境変数: DRILL_IMAGE（既定 pgvector/pgvector:pg17）。終了コード: 0 成功 / 1 検査の失敗 / 2 使い方・前提の不足
set -euo pipefail

IMAGE="${DRILL_IMAGE:-pgvector/pgvector:pg17}"
REQUIRED_TABLES=(documents chunks user account)

die() {
  echo "エラー: $*" >&2
  exit 2
}

dump="" expect="" shred_after=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --expect) expect="${2:-}"; shift 2 ;;
    --shred) shred_after=1; shift ;;
    -h | --help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "知らないオプション: $1" ;;
    *) [[ -z "$dump" ]] || die "ダンプは 1 つだけ"; dump="$1"; shift ;;
  esac
done
[[ -n "$dump" ]] || die "ダンプのファイルを指定してください（使い方は -h）"
[[ -s "$dump" ]] || die "ダンプが無いか空です"
command -v docker > /dev/null || die "docker が要ります"

work="$(mktemp -d)"
name="pg-restore-drill-$$"
cleanup() {
  docker rm -f "$name" > /dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

dump_abs="$(cd "$(dirname "$dump")" && pwd)/$(basename "$dump")"
password="$(openssl rand -hex 12)" # 使い捨て。コンテナと一緒に消える。ポートは公開しない

echo "1/4 使い捨ての Postgres を起動（${IMAGE}）"
docker run -d --rm --name "$name" -e POSTGRES_PASSWORD="$password" -e POSTGRES_DB=drill \
  -v "$dump_abs:/dump/backup.dump:ro" "$IMAGE" > /dev/null
for _ in $(seq 1 60); do
  # 初期化中の一時サーバーは TCP を開けない。TCP で応答する＝本番のサーバーが起動した
  docker exec "$name" pg_isready -h 127.0.0.1 -U postgres -d drill > /dev/null 2>&1 && break
  sleep 1
done
docker exec "$name" pg_isready -h 127.0.0.1 -U postgres -d drill > /dev/null 2>&1 || die "Postgres が起動しませんでした"
docker exec "$name" psql -U postgres -d drill -qc "CREATE EXTENSION IF NOT EXISTS vector" > /dev/null

echo "2/4 復元（Prisma 固有の拡張 prisma_postgres の項目は一覧から外す。--exit-on-error は付けない）"
docker exec "$name" sh -c 'pg_restore -l /dump/backup.dump | grep -v -i prisma_postgres > /tmp/list'
restore_err="$work/restore.err"
if ! docker exec "$name" pg_restore --no-owner -L /tmp/list -U postgres -d drill /dump/backup.dump 2> "$restore_err"; then
  echo "pg_restore が失敗しました（先頭 5 行）:" >&2
  head -5 "$restore_err" >&2
  exit 1
fi
errors="$(grep -c . "$restore_err" || true)"
echo "   pg_restore: 終了コード 0、標準エラー ${errors} 行"

echo "3/4 件数"
counts="$work/counts"
docker exec "$name" psql -U postgres -d drill -At -F '=' -c "
  SELECT t.table_name, (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM public.%I', t.table_name), false, true, '')))[1]::text
  FROM information_schema.tables t WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE' ORDER BY 1" > "$counts"
cat "$counts"
embedded="$(docker exec "$name" psql -U postgres -d drill -At -c "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL" 2> /dev/null || true)"
echo "chunks（embedding あり）=${embedded:-（chunks が無い）}"

echo "4/4 検査"
fail=0
count_of() { awk -F= -v k="$1" '$1 == k { print $2 }' "$counts"; }
for t in "${REQUIRED_TABLES[@]}"; do
  n="$(count_of "$t")"
  if [[ -z "$n" || "$n" -eq 0 ]]; then echo "NG: $t が無いか空です"; fail=1; fi
done
chunks="$(count_of chunks)"
if [[ -n "$chunks" && "$embedded" != "$chunks" ]]; then echo "NG: embedding が入っていない chunks があります（${embedded} / ${chunks}）"; fail=1; fi
if [[ -n "$expect" ]]; then
  IFS=',' read -ra pairs <<< "$expect"
  for pair in "${pairs[@]}"; do
    k="${pair%%=*}" v="${pair#*=}"
    got="$(count_of "$k")"
    if [[ "$got" != "$v" ]]; then echo "NG: $k は $got 件（期待 ${v}）"; fail=1; fi
  done
fi
if [[ "$fail" -eq 0 ]]; then echo "OK: 復元できて、検査に通りました"; fi

if [[ "$shred_after" -eq 1 ]]; then
  if [[ "$(uname)" == "Darwin" ]]; then rm -P "$dump_abs"; else shred -u "$dump_abs"; fi
  echo "ダンプを消しました"
fi
exit "$fail"
