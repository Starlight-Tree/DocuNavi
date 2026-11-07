#!/usr/bin/env bash
# Infisical 公式 MCP（@infisical/mcp）の起動用。Claude Code のローカル登録（register-infisical-mcp.sh）が、このスクリプトを呼ぶ。
# 資格情報（Universal Auth の Client ID / Secret）は ~/.infisical/identity（ボリューム内・権限 600）から読み、
# 登録（~/.claude/.claude.json）には入れない。Client Secret を発行し直しても、登録はそのまま使える。
# ・秘密の値は MCP の応答でマスクする（INFISICAL_MASK_SECRET_VALUES=true）。
# ・使えるツールは、秘密の一覧（list-secrets。値は伏せ字）だけ（2026-11-08 に絞った）。
#   秘密の値を MCP 経由で入れると、値がエージェントの会話を通る。値の登録は CLI で行う（README の「開発環境の Infisical」）。
# ・MCP にはプロジェクトを絞る設定が無い。届く範囲は、この ID（docunavi-devcontainer）が入っているプロジェクトで決まる。
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
export INFISICAL_ENABLED_TOOLS=list-secrets

exec npx -y @infisical/mcp@0.0.25
