# 開発環境の方針

2026-10-31 に、Claude Code のログ・メモリーが失われたこと（`~/.claude/projects/` が作り直された。原因は未確定）と、
LiteLLM・Gemini CLI を使った構成の整理を受けて決めた。

## 廃止したもの・入れないもの

- LiteLLM gateway is removed and no longer used. LiteLLM Proxy・Bifrost・claude-code-router は採用しない。LLM の遅延・失敗への対処は `app/agents/resilience.py`
- Gemini CLI is not part of the docunavi runtime. 開発コンテナにも入れない（`devcontainer.json` の `postCreateCommand`・`docker-compose.yml` の `.gemini` ボリューム・`GOOGLE_CLOUD_*` / `GEMINI_MODEL` を戻さない）
- Cloud Code・Gemini Code Assist・Data Agent Kit・Jupyter 系の VS Code 拡張は、`devcontainer.json` の `extensions` に足さない。GCP の操作は原則 gcloud CLI（ガードは CLAUDE.md の「gcloud」の節）
- 画像生成の Cloud Run Job は星庭ジョブ（`hoshiniwa-job`）のリポジトリで管理する。`job/` をこのリポジトリに置かない

## 再ビルドの可能性

- 開発コンテナの定義は、このリポジトリのファイルだけで完結させる。例外は星庭ジョブの bind mount（ホストの `/Users/wataru/projects/hoshiniwa-job` が前提。作業ツリーは 2026-11-01 から書き込み可能で、`.git/hooks` と `.git/config` だけ `:ro`）で、残す。
- `devcontainer.json`・`.devcontainer/docker-compose.yml`・`.devcontainer/Dockerfile` に、ホストの絶対パス・別プロジェクトのファイル・外部のアカウント（GCP プロジェクト ID など）を足す変更は、事前にユーザーに確認する
- 再ビルドはホスト側の作業。承認なしに実行しない

## ツールの入れ場所（2026-10-31）

- gcloud・gh・psql・Go・docker-cli・Playwright のライブラリーは、`.devcontainer/Dockerfile`（イメージのビルド時）に入れる。`postCreateCommand` に apt の導入を足さない（作成のたびに毎回走り、キャッシュされない）
- Chromium の本体は、名前付きボリューム `playwright-browsers` のまま（`install-playwright.sh` が取得と起動確認を行う）
- Node の feature はイメージの後に入るので、npm で入れるものは Dockerfile に移せない。Vercel CLI は入れない（2026-10-31 に削除。必要なときは `npx --yes vercel@63.1.2`）
- `install-gh.sh`・`install-psql.sh`・`install-go.sh` は残してある（`postCreateCommand` からは呼ばない）。Go の版とハッシュは `install-go.sh` と Dockerfile の両方にあるので、上げるときは両方を合わせる

## ルール・メモリーの置き場所

- 重要なルールは Auto Memory だけに置かない。プロジェクト固有のルールは `CLAUDE.md` か `.claude/rules/`、ユーザー共通のルールは `~/.claude/CLAUDE.md` に置く
- 機密情報・トークン・認証情報は、メモリー・`CLAUDE.md`・`.claude/rules/`・設定のバックアップ（`*.bak`）に入れない。設定ファイルを置き換えるときは、先に中身を確かめ、秘密があればバックアップに残さない
- `~/.claude`（名前付きボリューム `claude-config`）は、OrbStack の仮想ディスクの中にある。OrbStack の `data.img` は、2026-10-25 の Time Machine バックアップに無かった（理由は未確認。ほかの日付は未調査）。memory は Time Machine から戻せない前提にして、Mac 側のフォルダーへ定期的に写す（手順は `~/.claude/CLAUDE.md` の「バックアップ」。`orb docker volume export` は実在するが、この環境での動作は未確認）（2026-11-01）
