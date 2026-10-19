#!/usr/bin/env bash
# 書き込み可能な Supabase MCP（supabase-write）を、このプロジェクトのローカル登録に入れる。初回の登録、または登録が失われたときの復旧用。
# 設定ファイルは、devcontainer.json の CLAUDE_CONFIG_DIR により ~/.claude のボリューム内（~/.claude/.claude.json）にある。
# このため、通常の再ビルドでは登録も OAuth のトークン（~/.claude/.credentials.json）も残り、このスクリプトは要らない。
# ボリュームを削除したときなどは、登録が消え、再認証（/mcp）も要る可能性がある。
# README の「Supabase MCP（読み取り専用の共有）」。
#
# ・参照 ID は展開前の ${SUPABASE_PROJECT_REF} のまま登録する（値は ~/.claude/settings.json の env。設定ファイルにもログにも残さない）。
# ・共有の .mcp.json の supabase（read_only=true）は変えない。別名にするのは、同じ名前だと共有側が隠れるため。
# ・書き込みのツールは、呼ぶたびに利用者の承認が要る（許可リストには入れない）。
# ・claude の実体は VS Code の拡張に同梱されていて、コンテナの作成直後はまだ無いことがある。出てくるまで待つ（最大 5 分）。
#   見つからなくても失敗にしない（コンテナの作成を止めない）。そのときは、このスクリプトを手で実行する。
# ・いまは手で実行する（起動時の自動実行は未設定。設けるなら、作成を止めない形で別に決める）。何度実行しても同じ（登録済みなら何もしない）。

cd "$(dirname "$0")/.." || exit 0

NAME=supabase-write
URL='https://mcp.supabase.com/mcp?project_ref=${SUPABASE_PROJECT_REF}&features=database,docs'

find_claude() {
  ls -d "$HOME"/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | sort -V | tail -1
}

claude_bin=""
for _ in $(seq 1 60); do
  claude_bin=$(find_claude)
  [ -n "$claude_bin" ] && break
  sleep 5
done

if [ -z "$claude_bin" ]; then
  echo "register-supabase-write: claude の実体が見つかりません（VS Code の拡張が未導入）。拡張が入ってから bash .devcontainer/register-supabase-write.sh を実行してください。"
  exit 0
fi

if "$claude_bin" mcp get "$NAME" >/dev/null 2>&1; then
  echo "register-supabase-write: $NAME は登録済みです。"
  exit 0
fi

if "$claude_bin" mcp add --scope local --transport http "$NAME" "$URL"; then
  echo "register-supabase-write: $NAME を登録しました。/mcp で認証してください（トークンがボリュームに残っていれば不要）。"
else
  echo "register-supabase-write: 登録に失敗しました。手で実行してください: claude mcp add --scope local --transport http $NAME '$URL'"
fi
exit 0
