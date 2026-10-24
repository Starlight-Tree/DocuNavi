#!/usr/bin/env bash
# Vercel の公式 MCP（vercel。https://mcp.vercel.com）を、このプロジェクトのローカル登録に入れる。初回の登録、または登録が失われたときの復旧用。
# 仕組みは register-supabase-write.sh と同じ。設定ファイルは ~/.claude のボリューム内（~/.claude/.claude.json）にあり、
# 通常の再ビルドでは登録も OAuth のトークン（~/.claude/.credentials.json）も残る。ローカル登録は作業ディレクトリのパスがキーなので、
# フォルダー名を変えると外れる。起動のたびに devcontainer.json の postStartCommand がこのスクリプトを呼び、今のパスに登録し直す。
# ボリュームを削除したときなどは、登録が消え、再認証（/mcp）も要る。README の「開発環境の Vercel MCP」。
#
# ・登録だけで、認証はしない。接続先は公式の https://mcp.vercel.com だけ。
# ・.mcp.json（コミットされる共有の定義）には入れない。
# ・書き込み系のツールは、権限の規則（.claude/settings*.json）で止める。規則が入るまでは、エージェントは読み取り系のツールしか呼ばない。
# ・claude の実体は VS Code の拡張に同梱されていて、コンテナの作成直後はまだ無いことがある。出てくるまで 5 秒おきに待つ
#   （既定は 60 回 = 最大 5 分。環境変数 REGISTER_VERCEL_MCP_WAIT_TRIES で回数を変えられる）。見つからなくても失敗にしない。
# ・何度実行しても同じ（登録済みなら何もしない）。前景で呼ぶ（バックグラウンドの子プロセスは起動処理の終了で止められる）。

cd "$(dirname "$0")/.." || exit 0

NAME=vercel
URL='https://mcp.vercel.com'

find_claude() {
  ls -d "$HOME"/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | sort -V | tail -1
}

claude_bin=""
tries="${REGISTER_VERCEL_MCP_WAIT_TRIES:-60}"
case "$tries" in ''|*[!0-9]*) tries=60 ;; esac
for _ in $(seq 1 "$tries"); do
  claude_bin=$(find_claude)
  [ -n "$claude_bin" ] && break
  sleep 5
done

if [ -z "$claude_bin" ]; then
  echo "register-vercel-mcp: claude の実体が見つかりません（VS Code の拡張が未導入）。拡張が入ってから bash .devcontainer/register-vercel-mcp.sh を実行してください。"
  exit 0
fi

if "$claude_bin" mcp get "$NAME" >/dev/null 2>&1; then
  echo "register-vercel-mcp: $NAME は登録済みです。"
  exit 0
fi

if "$claude_bin" mcp add --scope local --transport http "$NAME" "$URL"; then
  echo "register-vercel-mcp: $NAME を登録しました。/mcp で認証してください（トークンがボリュームに残っていれば不要）。"
else
  echo "register-vercel-mcp: 登録に失敗しました。手で実行してください: claude mcp add --scope local --transport http $NAME '$URL'"
fi
exit 0
