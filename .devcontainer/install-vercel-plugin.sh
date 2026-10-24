#!/usr/bin/env bash
# Claude Code 用の Vercel プラグイン（vercel/vercel-plugin。公式）を、このプロジェクトのスコープ（project）で入れる。
# 初回の導入、またはボリュームを消したあとの復旧用。起動のたびに devcontainer.json の postStartCommand が呼ぶ。
# 経緯と方針は docs/roadmap.md の⑦と .devcontainer/README.md の「開発環境の Vercel プラグイン」。
#
# ・入れるのは「知識の注入・スキル」のためだけ。操作（デプロイ・環境変数・ログ）は既存の Vercel MCP を使う。
#   `/vercel-plugin:deploy prod` は使わない（本番は main へのプッシュの Git 連携だけ）。
# ・project スコープなので、有効化の宣言（extraKnownMarketplaces / enabledPlugins）は .claude/settings.json に書かれ、Git に残る。
#   プラグインの本体は ~/.claude/plugins/（永続ボリューム）に入る。
# ・テレメトリは .claude/settings.json の env（VERCEL_PLUGIN_TELEMETRY=off）で止めている。
# ・自動更新はしない（Claude Code の既定がオフ。ここでも有効にしない）。更新は、上流の差分を読んでから手で行う（README）。
# ・上流にタグが無く、版を固定できない。審査したコミットと版を下に残し、導入後の版がずれていたら警告だけ出す。
# ・claude の実体は VS Code の拡張に同梱されていて、コンテナの作成直後はまだ無いことがある。出てくるまで 5 秒おきに待つ
#   （既定は 12 回 = 最大 1 分。環境変数 INSTALL_VERCEL_PLUGIN_WAIT_TRIES で回数を変えられる）。見つからなくても失敗にしない。
# ・何度実行しても同じ（導入済みなら何もしない）。前景で呼ぶ（バックグラウンドの子プロセスは起動処理の終了で止められる）。
# ・失敗しても終了コード 0（コンテナの起動は止めない）。

cd "$(dirname "$0")/.." || exit 0

MARKETPLACE_SOURCE='vercel/vercel-plugin'
PLUGIN='vercel-plugin@vercel'
# 審査したもの（2026-10-25。hooks/*.mjs を読んだ。実行はしていない）。上げるときは、差分を読み直してから書き換える
REVIEWED_VERSION='0.54.1'
REVIEWED_COMMIT='3c88cfc8de5d1e124d23b0ef23f32c5aa127c845'

# コンテナに GitHub の SSH 鍵が無い。セッション内の /plugin や更新は HTTPS に戻らないので、ここでも HTTPS に固定する
export CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1

find_claude() {
  ls -d "$HOME"/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | sort -V | tail -1
}

claude_bin=""
tries="${INSTALL_VERCEL_PLUGIN_WAIT_TRIES:-12}"
case "$tries" in ''|*[!0-9]*) tries=12 ;; esac
for _ in $(seq 1 "$tries"); do
  claude_bin=$(find_claude)
  [ -n "$claude_bin" ] && break
  sleep 5
done

if [ -z "$claude_bin" ]; then
  echo "install-vercel-plugin: claude の実体が見つかりません（VS Code の拡張が未導入）。拡張が入ってから bash .devcontainer/install-vercel-plugin.sh を実行してください。"
  exit 0
fi

if ! "$claude_bin" plugin marketplace list 2>/dev/null | grep -q "$MARKETPLACE_SOURCE"; then
  "$claude_bin" plugin marketplace add "$MARKETPLACE_SOURCE" --scope project ||
    { echo "install-vercel-plugin: マーケットプレイスの追加に失敗しました。手で実行してください: claude plugin marketplace add $MARKETPLACE_SOURCE --scope project"; exit 0; }
fi

if "$claude_bin" plugin list 2>/dev/null | grep -q "$PLUGIN"; then
  echo "install-vercel-plugin: $PLUGIN は導入済みです。"
else
  "$claude_bin" plugin install "$PLUGIN" --scope project ||
    { echo "install-vercel-plugin: 導入に失敗しました。手で実行してください: claude plugin install $PLUGIN --scope project"; exit 0; }
  echo "install-vercel-plugin: $PLUGIN を導入しました。"
fi

# 版の確認（警告だけ。止めない）
if ! "$claude_bin" plugin list 2>/dev/null | grep -A6 "$PLUGIN" | grep -q "$REVIEWED_VERSION"; then
  echo "install-vercel-plugin: 警告: 導入された版が審査した版（$REVIEWED_VERSION、コミット $REVIEWED_COMMIT）と一致しません。フックの差分を読み直してください。"
fi
exit 0
