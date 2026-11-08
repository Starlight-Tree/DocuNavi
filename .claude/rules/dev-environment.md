# 開発環境の方針

2026-10-31 に、Claude Code のログ・メモリーが失われたこと（`~/.claude/projects/` が作り直された。原因は未確定）と、
LiteLLM・Gemini CLI を使った構成の整理を受けて決めた。

## 廃止したもの・入れないもの

- LiteLLM gateway is removed and no longer used. LiteLLM Proxy・Bifrost・claude-code-router は採用しない。LLM の遅延・失敗への対処は `app/agents/resilience.py`
- Gemini CLI is not part of the docunavi runtime. 開発コンテナにも入れない（`devcontainer.json` の `postCreateCommand`・`docker-compose.yml` の `.gemini` ボリューム・`GOOGLE_CLOUD_*` / `GEMINI_MODEL` を戻さない）
- Cloud Code・Gemini Code Assist・Data Agent Kit・Jupyter 系の VS Code 拡張は、`devcontainer.json` の `extensions` に足さない。GCP の操作は原則 gcloud CLI（ガードは CLAUDE.md の「gcloud」の節）
- 画像生成の Cloud Run Job は星庭ジョブ（`hoshiniwa-job`）のリポジトリで管理する。`job/` をこのリポジトリに置かない

## 実 API の経路（2026-11-09）

- エージェントが実 API を呼ぶ確認・測定は、基本的に Vertex AI で行う（`GOOGLE_GENAI_USE_VERTEXAI=true`、`GOOGLE_CLOUD_PROJECT=docunavi-app`、ADC 認証）。理由: Developer API が 504 を返した直後に、Vertex では同じ呼び出しが通った。GCP の無料クレジットがある。
- 実 API の呼び出し前に、質問文・回数・費用を示して承認を得る既存のルールは変わらない。
- 本番の Cloud Run の LLM（Developer API キー）は、この方針に含まない。切り替えは別の決定で、事前に確認する（Vertex の LLM は 2026-11-09 の単発 5 回で Developer API の 2〜3 倍遅かった）。
- ADC が使えるかは、gcloud のガードで確かめられない。呼んで失敗したらユーザーに伝える。

## 再ビルドの可能性

- 開発コンテナの定義は、このリポジトリのファイルだけで完結させる。例外は星庭ジョブの bind mount（ホストの `/Users/wataru/Projects/hoshiniwa-job` が前提。作業ツリーは 2026-11-01 から書き込み可能で、`.git/hooks` と `.git/config` だけ `:ro`）で、残す。
- `devcontainer.json`・`.devcontainer/docker-compose.yml`・`.devcontainer/Dockerfile` に、ホストの絶対パス・別プロジェクトのファイル・外部のアカウント（GCP プロジェクト ID など）を足す変更は、事前にユーザーに確認する
- 再ビルドはホスト側の作業。承認なしに実行しない

## ツールの入れ場所（2026-10-31）

- gcloud・gh・Go・docker-cli・Playwright のライブラリーは、`.devcontainer/Dockerfile`（イメージのビルド時）に入れる。`postCreateCommand` に apt の導入を足さない（作成のたびに毎回走り、キャッシュされない）
- Chromium の本体は、名前付きボリューム `playwright-browsers` のまま（`install-playwright.sh` が取得と起動確認を行う）
- Node の feature はイメージの後に入るので、npm で入れるものは Dockerfile に移せない。Vercel の CLI・MCP・プラグインは入れない（2026-11-08 に本番を Cloudflare に移し、Vercel は廃止。`.devcontainer/README.md` の「開発環境の Vercel（廃止）」）
- `install-gh.sh`・`install-go.sh` は残してある（`postCreateCommand` からは呼ばない）。Go の版とハッシュは `install-go.sh` と Dockerfile の両方にあるので、上げるときは両方を合わせる

## ルール・メモリーの置き場所

- 重要なルールは Auto Memory だけに置かない。プロジェクト固有のルールは `CLAUDE.md` か `.claude/rules/`、ユーザー共通のルールは `~/.claude/CLAUDE.md` に置く
- 機密情報・トークン・認証情報は、メモリー・`CLAUDE.md`・`.claude/rules/`・設定のバックアップ（`*.bak`）に入れない。設定ファイルを置き換えるときは、先に中身を確かめ、秘密があればバックアップに残さない
- `~/.claude`（名前付きボリューム `claude-config`）は、OrbStack の仮想ディスクの中にある。OrbStack の `data.img` は、2026-10-25 の Time Machine バックアップに無かった（理由は未確認。ほかの日付は未調査）。memory は Time Machine から戻せない前提にして、Mac 側のフォルダーへ定期的に写す（手順は `~/.claude/CLAUDE.md` の「バックアップ」。`orb docker volume export` は実在するが、この環境での動作は未確認）（2026-11-01）
