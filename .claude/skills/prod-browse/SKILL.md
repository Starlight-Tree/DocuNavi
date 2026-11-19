---
name: prod-browse
description: 本番の画面（docunavi-web）にログインして、操作・スクリーンショット・表示の確認をするときの手順。「本番の画面を撮って」「本番にログインして確認して」「README のスクショを撮り直して」「本番の画面で動作確認」と頼まれたときに使う。
---

# 本番の画面を操作・撮影する

## 前提（毎回、説明し直さない）

- コンテナの Playwright MCP は Google ログインができない。接続済みの Chrome（`mcp__claude-in-chrome__*`。`dev-environment.md` の「ブラウザー操作」）はユーザー本人のセッションなので、本番の操作・撮影には使わない（本番のログインが要る確認は、下のテスト専用ユーザーで行う）。**Cookie・トークンを取り出して使わない。**
- 代わりに、テスト専用ユーザー（`e2e-test@docunavi.invalid`）で、`scripts/prod_check.sh --browse` が、開発コンテナの Playwright でログインして操作する。画面はユーザー本人のものではなく `e2e-test` のもの。
- `prod_check.sh` は**ホストのターミナルで、ユーザーが実行する**（本番 Worker の Secret を書く wrangler のログインがホストにしか無い）。私は実行しない。私がやるのは、シナリオを書くこと、コマンドを組み立てて渡すこと、出力と画像を読んで報告すること。
- 流れ（trap で必ず後始末）: テスト用ログインを一時的に有効 → `e2e-test` に利用資格を一時付与 → 操作 → 資格を外す → テスト用ログインを削除。本番 DB の `access_audit` に 2 行残る。

## 使い方

```
./scripts/prod_check.sh --browse <シナリオ名>[=<画像名>] [--browse …] [--viewport 1280x800] [--theme light|dark] [--scenario-args '<JSON>'] [--question "…"] [-y]
```

- 画像は `docunavi/.playwright-mcp/<画像名>.png`（コンテナの `/workspaces/docunavi/.playwright-mcp/`）。既存の `docs/images/` は触らない。置き換えは、ユーザーが見てから。
- ビューポートは既定 1280×800・ライトテーマ。画像のサイズは全部そろう（`fullPage` は使わない）。
- 既存のシナリオ（`scripts/prod_scenarios/`）: `chat-2turns`（2 往復。課金 2 回）、`documents`（資料一覧。課金なし）、`screen-check`（1 往復と、時刻・太字・色の検査。課金 1 回）。
- 実行前に `PREVIEW_DRY_RUN=1 ./scripts/prod_check.sh …` で、書き込みの計画だけ表示できる。

例（README 用）:
```
./scripts/prod_check.sh --browse chat-2turns=readme-chat --browse documents=readme-documents
```

## 新しい操作が要るとき

1. `scripts/prod_scenarios/<名前>.cjs` を書く（小文字・数字・ハイフンの名前）。形は既存のシナリオに倣う: `module.exports = async ({ page, WEB, shot, log, args }) => { … }`。ログインは実行部（`scripts/prod_browse.cjs`）が済ませている。`shot('名前')` で撮り、`log()` で報告する。
2. **課金（実 API）が起きる操作かを、ユーザーに伝える**（質問を送る回数）。「質問は N 回だけ。再送しない」を守り、シナリオにも再試行を入れない。
3. 画面の要素は、`frontend/e2e/chat.e2e.ts` の `getByRole` に倣う（資料一覧は `/?tab=documents`、チャットは `/?tab=chat`、左下のメニューの「ナレッジ」）。
4. コマンドをユーザーに渡し、出力（`screenshot:` の行と `log` の内容）を貼ってもらう。画像は Read で見て確かめる。

## 守ること

- 出力にメール・パスワード・トークン・Cookie を出さない。会話の削除・ログアウトは、頼まれたときだけ。
- 本番の `e2e-test` の会話は、実行のたびに増える（3 件以上残っている）。消すのは、ユーザーの指示があるときだけ。
- 未確認（2026-11-17 時点）: `--browse` のスクリプトでの通しの実行（`chat-2turns`・`documents`）。確認できたら、この行を更新し、`docs/testing.md` に反映する。
- 失敗したら、後始末（Secret の削除）が済んだかを、出力の「[片付け]」の行で確かめる。残っていたら `bash workers/api/test-login.sh off`。
