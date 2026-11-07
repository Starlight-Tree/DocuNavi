#!/usr/bin/env bash
# Infisical の公式 MCP（infisical）を、このプロジェクトのローカル登録に入れる。初回の登録、または登録が失われたときの復旧用。
# 設定は ~/.claude のボリューム内。通常の再ビルドでは残る。ローカル登録は作業ディレクトリの
# パスがキーなので、フォルダー名を変えると外れる。起動のたびに devcontainer.json の postStartCommand が呼び、今のパスに登録し直す）。
#
# ・登録するのは起動スクリプト（.devcontainer/infisical-mcp.sh）だけ。資格情報は登録に入れない（~/.infisical/identity から読む）。
# ・.mcp.json（コミットされる共有の定義）には入れない。
# ・~/.infisical/identity が無ければ、登録しても起動に失敗する。マシン ID の資格情報を置いてから使う。
# ・claude の実体が出てくるまで 5 秒おきに待つ（REGISTER_INFISICAL_MCP_WAIT_TRIES。既定 60 回）。見つからなくても失敗にしない。
# ・何度実行しても同じ（登録済みなら何もしない）。

cd "$(dirname "$0")/.." || exit 0

NAME=infisical
SCRIPT="$(pwd)/.devcontainer/infisical-mcp.sh"

find_claude() {
  ls -d "$HOME"/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | sort -V | tail -1
}

claude_bin=""
tries="${REGISTER_INFISICAL_MCP_WAIT_TRIES:-60}"
case "$tries" in ''|*[!0-9]*) tries=60 ;; esac
for _ in $(seq 1 "$tries"); do
  claude_bin=$(find_claude)
  [ -n "$claude_bin" ] && break
  sleep 5
done

if [ -z "$claude_bin" ]; then
  echo "register-infisical-mcp: claude の実体が見つかりません。拡張が入ってから bash .devcontainer/register-infisical-mcp.sh を実行してください。"
  exit 0
fi

if "$claude_bin" mcp get "$NAME" >/dev/null 2>&1; then
  echo "register-infisical-mcp: $NAME は登録済みです。"
  exit 0
fi

if "$claude_bin" mcp add --scope local "$NAME" -- bash "$SCRIPT"; then
  echo "register-infisical-mcp: $NAME を登録しました。新しいセッションから使えます。"
else
  echo "register-infisical-mcp: 登録に失敗しました。手で実行してください: claude mcp add --scope local $NAME -- bash $SCRIPT"
fi
exit 0
