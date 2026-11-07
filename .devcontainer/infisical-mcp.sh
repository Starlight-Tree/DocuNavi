#!/usr/bin/env bash
# Infisical 公式 MCP（@infisical/mcp）の起動用。Claude Code のローカル登録（register-infisical-mcp.sh）が、このスクリプトを呼ぶ。
# 資格情報（Universal Auth の Client ID / Secret）は ~/.infisical/identity（ボリューム内・権限 600）から読み、
# 登録（~/.claude/.claude.json）には入れない。Client Secret を発行し直しても、登録はそのまま使える。
# ・秘密の値は MCP の応答でマスクする（INFISICAL_MASK_SECRET_VALUES=true）。
# ・使えるツールは、一覧・秘密の作成だけ。ただし、この ID は viewer なので、作成は権限で断られる。
#   秘密の値を MCP 経由で入れると、値がエージェントの会話を通る。値の登録は CLI で行う（README の「開発環境の Infisical」）。
# ・標準出力は MCP の通信に使われる。診断は標準エラーに出す。

set -euo pipefail

identity="$HOME/.infisical/identity"
if [ ! -r "$identity" ]; then
  echo "infisical-mcp: $identity がありません（マシン ID の資格情報を置いてください）。" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$identity"
set +a

export INFISICAL_AUTH_METHOD=universal-auth
export INFISICAL_MASK_SECRET_VALUES=true
export INFISICAL_ENABLED_TOOLS=list-projects,list-secrets,create-secret

exec npx -y @infisical/mcp@0.0.25
