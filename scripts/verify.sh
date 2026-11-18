#!/usr/bin/env bash
# 変更したら実行する検査を、1 コマンドにまとめる（CI の ci.yml と同じ内容の読み取り検査。書き込み・配備・実 API・DB は使わない）。
#   scripts/verify.sh [py|frontend|worker|go|all]   既定は all。開発コンテナの中で、リポジトリのルートから実行する
#   py        ruff format --check / ruff check / pytest（モックモード固定。実 API・DB は呼ばない）
#   frontend  npm run typecheck / npm test（ビルドと E2E は含まない。`.next-verify` を作らないため）
#   worker    workers/api の npm run typecheck
#   go        ops/sse-relay の go vet / go test -race（go が無ければ飛ばす）
# py の 3 段は全部実行する。py が落ちたら、frontend 以降は実行しない。実行したものと結果は、終わりにまとめて表示する。
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

TARGET="${1:-all}"
case "$TARGET" in py | frontend | worker | go | all) ;; *)
  echo "使い方: scripts/verify.sh [py|frontend|worker|go|all]" >&2
  exit 2
  ;;
esac

RESULTS=()
FAILED=0

run() { # run <名前> <ディレクトリ> <コマンド...>
  local name="$1" dir="$2"
  shift 2
  echo "=== ${name} ($*)"
  if (cd "$ROOT/$dir" && "$@"); then
    RESULTS+=("OK   ${name}")
  else
    RESULTS+=("FAIL ${name}")
    FAILED=1
  fi
}

want() { [ "$TARGET" = all ] || [ "$TARGET" = "$1" ]; }

PY=".venv/bin/python"
if want py; then
  # py の 3 段は互いに独立なので、1 つ落ちても最後まで実行して、全部の結果を出す
  run "ruff format" . "$PY" -m ruff format --check .
  run "ruff check" . "$PY" -m ruff check .
  run "pytest" . "$PY" -m pytest -q
fi
if [ "$FAILED" = 0 ] && want frontend; then
  run "frontend typecheck" frontend npm run typecheck
  [ "$FAILED" = 0 ] && run "frontend test" frontend npm test
fi
if [ "$FAILED" = 0 ] && want worker; then
  run "workers/api typecheck" workers/api npm run typecheck
fi
if [ "$FAILED" = 0 ] && want go; then
  if command -v go >/dev/null 2>&1 && [ -d "$ROOT/ops/sse-relay" ]; then
    run "sse-relay go vet" ops/sse-relay go vet ./...
    [ "$FAILED" = 0 ] && run "sse-relay go test" ops/sse-relay go test -race -count=1 ./...
  else
    RESULTS+=("SKIP go（go コマンドか ops/sse-relay が無い）")
  fi
fi

echo
echo "=== 結果"
printf '%s\n' "${RESULTS[@]}"
exit "$FAILED"
