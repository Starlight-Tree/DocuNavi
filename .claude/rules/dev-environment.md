# 開発環境の方針

2026-10-31 に、Claude Code のログ・メモリーが失われたこと（`~/.claude/projects/` が作り直された。原因は未確定）と、
LiteLLM・Gemini CLI を使った構成の整理を受けて決めた。

## 廃止したもの・入れないもの

- LiteLLM gateway is removed and no longer used. LiteLLM Proxy・Bifrost・claude-code-router は採用しない。LLM の遅延・失敗への対処は `app/agents/resilience.py`
- Gemini CLI is not part of the docunavi runtime. 開発コンテナにも入れない（`devcontainer.json` の `postCreateCommand`・`docker-compose.yml` の `.gemini` ボリューム・`GOOGLE_CLOUD_*` / `GEMINI_MODEL` を戻さない）
- Cloud Code・Gemini Code Assist・Data Agent Kit・Jupyter 系の VS Code 拡張は、`devcontainer.json` の `extensions` に足さない。GCP の操作は原則 gcloud CLI（ガードは CLAUDE.md の「gcloud・gh・MCP のガード」の節）
- 画像生成の Cloud Run Job は星庭ジョブ（`HoshiniwaJob`）のリポジトリで管理する。`job/` をこのリポジトリに置かない

## 実 API の経路（2026-11-09 に決め、2026-11-15 に無料キーだけへ改めた）

- **ローカルの実 API の確認・測定は、Gemini Developer API の無料キーだけで行う**（2026-11-15 にユーザーが決定。埋め込みも今のまま）。Vertex AI（`GOOGLE_GENAI_USE_VERTEXAI`・ADC）・有料キー・課金の有効化は、ローカルでは使わない（`docs/development.md` の「LLM のモードと Gemini キー」）。2026-11-09 には「基本的に Vertex AI」としていたが、この決定で置き換わった（そのときの理由: Developer API が 504 を返した直後に、Vertex では同じ呼び出しが通った）。
- 実 API の呼び出し前に、質問文・回数・費用を示して承認を得る既存のルールは変わらない。
- 本番の Cloud Run の LLM（Developer API キー）は、この方針に含まない。切り替えは別の決定で、事前に確認する（Vertex の LLM は 2026-11-09 の単発 5 回で Developer API の 2〜3 倍遅かった）。

## ブラウザー操作（2026-11-18）

- ブラウザーでの確認・実験は、ユーザーが `Claude` プロファイルで開いてある接続済みの Chrome（`mcp__claude-in-chrome__*`）で行う。接続が無いときは、起動せずにユーザーへ頼む（Chrome の起動・終了はユーザーの作業）
- 別プロファイル・別ポートの Chrome（`--user-data-dir`、`--remote-debugging-port`、実行ファイルの直接起動）を、毎回新しく作らない。ホストの `~/.claude/settings.json` の PreToolUse フック（Bash）が止める（2026-11-19 に、`--remote-debugging-pipe` と `open -a` での起動も止めるよう広げた）
- 接続先のブラウザーを切り替えない（`select_browser` `switch_browser`。ホストの `permissions.deny` に入れた）。接続は、Claude プロファイルに入れた拡張の 1 台だけ。複数台が見えたら、選ばずにユーザーへ伝える

## 再ビルドの可能性・ツールの入れ場所

詳細は [devcontainer.md](devcontainer.md)（`.devcontainer/**` を触るときに読み込まれる。2026-11-18 に分けた）。承認の要る点だけ、ここにも残す。

- `devcontainer.json`・`.devcontainer/docker-compose.yml`・`.devcontainer/Dockerfile` に、ホストの絶対パス・別プロジェクトのファイル（星庭ジョブのフォルダーなど）・外部のアカウント（GCP プロジェクト ID など）を足す変更は、事前にユーザーに確認する。定義はこのリポジトリのファイルだけで完結させる
- 再ビルドはホスト側の作業。承認なしに実行しない

## ルール・メモリーの置き場所

- 重要なルールは Auto Memory だけに置かない。プロジェクト固有のルールは `CLAUDE.md` か `.claude/rules/`、ユーザー共通のルールは `~/.claude/CLAUDE.md` に置く
- 機密情報・トークン・認証情報は、メモリー・`CLAUDE.md`・`.claude/rules/`・設定のバックアップ（`*.bak`）に入れない。設定ファイルを置き換えるときは、先に中身を確かめ、秘密があればバックアップに残さない
- `~/.claude`（名前付きボリューム `claude-config`）は、OrbStack の仮想ディスクの中にある。OrbStack の `data.img` は、2026-10-25 の Time Machine バックアップに無かった（理由は未確認。ほかの日付は未調査）。memory は Time Machine から戻せない前提にして、Mac 側のフォルダーへ定期的に写す（手順は `~/.claude/CLAUDE.md` の「バックアップ」。`orb docker volume export` は実在するが、この環境での動作は未確認）（2026-11-01）
