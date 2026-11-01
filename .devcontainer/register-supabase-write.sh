#!/usr/bin/env bash
# 書き込み可能な Supabase MCP（supabase。2026-11-01 に supabase-write から改名）を、このプロジェクトのローカル登録に入れる。初回の登録、または登録が失われたときの復旧用。
# 設定ファイルは、devcontainer.json の CLAUDE_CONFIG_DIR により ~/.claude のボリューム内（~/.claude/.claude.json）にある。
# このため、通常の再ビルドでは登録も OAuth のトークン（~/.claude/.credentials.json）も残る。
# ただし、ローカル登録は作業ディレクトリのパスがキーなので、フォルダー名を変える（例: chatbot → docunavi）と、設定が残っていても外れる。
# 起動のたびに devcontainer.json の postStartCommand がこのスクリプトを呼び、今のパスに登録し直す。
# ボリュームを削除したときなどは、登録が消え、再認証（/mcp）も要る可能性がある。
# README の「Supabase MCP」。
#
# ・参照 ID は展開前の ${SUPABASE_PROJECT_REF} のまま登録する（値は ~/.claude/settings.json の env。設定ファイルにもログにも残さない）。
# ・.mcp.json に Supabase の定義は無い（読み取り専用の supabase-read は 2026-11-01 に削除した）。登録はこのローカル登録だけ。
# ・書き込みのツールは、呼ぶたびに利用者の承認が要る（許可リストには入れない）。
# ・claude の実体は VS Code の拡張に同梱されていて、コンテナの作成直後はまだ無いことがある。出てくるまで 5 秒おきに待つ
#   （既定は 60 回 = 最大 5 分。環境変数 REGISTER_SUPABASE_WAIT_TRIES で回数を変えられる）。
#   見つからなくても失敗にしない（コンテナの作成を止めない）。そのときは、このスクリプトを手で実行する。
# ・起動のたびに postStartCommand から、前景で（REGISTER_SUPABASE_WAIT_TRIES=4 = 最大 20 秒）呼ばれる。手で実行してもよい。
#   何度実行しても同じ（登録済みなら何もしない）。バックグラウンドにしないのは、起動処理の終了で子プロセスが止められるため（2026-10-20 に 2 通りの書き方で確認）。

cd "$(dirname "$0")/.." || exit 0

NAME=supabase
URL='https://mcp.supabase.com/mcp?project_ref=${SUPABASE_PROJECT_REF}&features=database,docs'

find_claude() {
  ls -d "$HOME"/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | sort -V | tail -1
}

claude_bin=""
tries="${REGISTER_SUPABASE_WAIT_TRIES:-60}"
case "$tries" in ''|*[!0-9]*) tries=60 ;; esac
for _ in $(seq 1 "$tries"); do
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
