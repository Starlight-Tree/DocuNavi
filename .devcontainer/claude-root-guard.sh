#!/bin/sh
# /usr/local/bin/claude の実体。root で実行されたら、何もせずに断る。本物の Claude Code は /usr/local/libexec/claude-real。
#
# 理由（2026-11-11 に設定のコピーで再現）: コンテナの環境変数 CLAUDE_CONFIG_DIR=/home/vscode/.claude は root にも効く。
# root の claude は ~/.claude/.claude.json と .credentials.json を root:root 600 で書き直し、vscode ユーザーが読めなくなる
# （MCP の登録が消えたように見える）。HOME を変えても CLAUDE_CONFIG_DIR が優先されるので、防げない。
# 回避の抜け道（環境変数での上書きなど）は作らない。確認したいときは vscode ユーザーで:
#   docker exec -u vscode -e HOME=/home/vscode docunavi-devcontainer claude …
if [ "$(id -u)" = 0 ]; then
  echo "claude: root では実行できません。root の実行は ${CLAUDE_CONFIG_DIR:-~/.claude} の設定ファイルを root 所有に書き換え、vscode ユーザーが読めなくします。" >&2
  echo "claude: vscode ユーザーで実行してください（docker exec -u vscode -e HOME=/home/vscode docunavi-devcontainer claude …）。" >&2
  exit 1
fi
exec /usr/local/libexec/claude-real "$@"
