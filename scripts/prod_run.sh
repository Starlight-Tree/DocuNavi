#!/usr/bin/env bash
# 本番の DB・資料を扱うコマンドを、開発コンテナ経由で、Infisical の本番（prod）の環境変数つきで実行する。
# ホストのターミナルで実行する（ホストに infisical は無い。コンテナの vscode ユーザーのログインを使う）。
#
#   scripts/prod_run.sh [--pg] [-e NAME=VALUE]... <コンテナ内で実行するコマンド…>
#
#   --pg           DB_BACKEND=pg を付ける（アプリのコード、app.* を動かすとき）。db/migrate.py には要らない。
#   -e NAME=VALUE  コンテナのコマンドに環境変数を足す（例: EMBEDDING_USE_VERTEXAI=true）。値は秘密を入れない。
#
# 例（コマンドの中身が、読み取りか書き込みかは、コマンド次第。書き込みは、全文を見せて承認を得てから）:
#   scripts/prod_run.sh .venv/bin/python db/migrate.py                    # マイグレーションの計画だけ（書かない）
#   scripts/prod_run.sh .venv/bin/python db/migrate.py --apply            # 適用（本番 DB への書き込み）
#   scripts/prod_run.sh --pg .venv/bin/python -m app.ingestion.suggest    # 質問の生成の確認だけ（API を呼ばない）
#
# 出力から Infisical の情報行（Injecting N Infisical secrets…）は除く。秘密の値はこのスクリプトも表示しない。
# 失敗の典型: root で docker exec すると、Infisical のログインが見えず「No valid login session」になる（-u vscode が要る）。
set -euo pipefail

DEVCONTAINER="${DEVCONTAINER:-docunavi-devcontainer}"

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

pg=""
envs=()
while [ $# -gt 0 ]; do
  case "$1" in
    --pg) pg="DB_BACKEND=pg "; shift ;;
    -e)
      [ $# -ge 2 ] || usage
      case "$2" in
        [A-Za-z_]*=*) envs+=(-e "$2") ;;
        *) echo "-e は NAME=VALUE の形で渡してください。" >&2; exit 2 ;;
      esac
      shift 2
      ;;
    -h|--help) usage ;;
    --) shift; break ;;
    *) break ;;
  esac
done
[ $# -ge 1 ] || usage

cmd=""
for arg in "$@"; do cmd="$cmd $(printf '%q' "$arg")"; done

# macOS の bash 3.2 は、空の配列の展開が set -u で落ちるので、${envs[@]+...} で守る
docker exec -u vscode ${envs[@]+"${envs[@]}"} "$DEVCONTAINER" bash -lc \
  "set -o pipefail; cd /workspaces/docunavi && set -a && . .dev-local/infisical-ids.env && set +a && ${pg}infisical run --silent --projectId \"\$INFISICAL_PROJECT_ID_PROD\" --env prod --${cmd} 2>&1 | awk '!/Injecting [0-9]+ Infisical secrets/'"
