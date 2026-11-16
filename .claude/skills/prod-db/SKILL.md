---
name: prod-db
description: 本番の DB・資料に対して、マイグレーションの計画・適用、取り込み系のコマンド（suggest、embed など）、読み取りの確認をするときの手順。「本番の DB にマイグレーション」「本番で suggest を実行」「本番の DB を確認」「本番にコマンドを流して」と頼まれたとき、または Infisical の本番（prod）の環境変数が要るコマンドを動かすときに使う。
---

# 本番の DB を扱う（毎回、組み立て直さない）

## 結論

**`scripts/prod_run.sh` を使う。** ホストのターミナルから 1 行で、開発コンテナの `vscode` ユーザーのログインを使って、Infisical の本番（`INFISICAL_PROJECT_ID_PROD`、env `prod`）の環境変数つきでコマンドを動かす。私（エージェント）が、ホストの Bash から実行してよい（ユーザーの承認の規則は下）。

```
scripts/prod_run.sh .venv/bin/python db/migrate.py                    # マイグレーションの計画だけ（書かない）
scripts/prod_run.sh .venv/bin/python db/migrate.py --apply            # 適用（本番 DB への書き込み）
scripts/prod_run.sh --pg .venv/bin/python -m app.ingestion.suggest    # 確認だけ（API は呼ばない）
scripts/prod_run.sh --pg -e EMBEDDING_USE_VERTEXAI=true -e GOOGLE_CLOUD_PROJECT=docunavi-app .venv/bin/python -m app.ingestion.suggest --apply --max-docs 3 --max-calls 3
```

- `--pg`: アプリのコード（`app.*`）には付ける。`db/migrate.py` は `DIRECT_URL` だけを読むので要らない。
- 出力は Infisical の情報行を除く。秘密の値は表示されない。

## 守ること

- **書き込みは、コマンド全文と影響を示して、ユーザーの承認を得てから実行する**（`db/migrate.py --apply`、`--apply` の付く取り込み系、`member add` など）。読み取り・計画・確認だけは、承認なしでよい。
- **実 API（課金）の実行は、質問・回数・費用の見積もりを示して、別に承認を得る。** 経路は Vertex AI（`EMBEDDING_USE_VERTEXAI=true`、ADC）。コンテナの ADC が通るかは、呼んで確かめる。
- 順序: マイグレーションを先に、Cloud Run の配備と取り込みを後に（逆だと、新しい検索の SQL が列を探して 503）。
- マイグレーションは追加（NULL 可の列）だけにする。適用の前に、`python db/migrate.py` の計画で「未適用」が想定のものだけか確かめ、適用の後にもう一度計画を出して「未適用: なし」を確かめる。
- 本番の DB 接続情報（`DATABASE_URL` `DIRECT_URL`）を、表示・保存・別の経路での取得をしない。

## 失敗したとき

- `No valid login session found`: root で `docker exec` している。`scripts/prod_run.sh`（`-u vscode`）を使う。
- 「ホストに infisical が無い」「コンテナに本番の認証が無い」は、誤りだった判断。ホストの Mac には infisical が無いが、コンテナの `vscode` ユーザーのログインが本番に届く（`prod_check.sh` と同じ経路）。
- コンテナが止まっている・`.dev-local/infisical-ids.env` が無いときは、ユーザーに伝える（再ビルドの可能性に関わる変更は、自分でしない）。

## 関連

- 本番の画面の操作・撮影: `.claude/skills/prod-browse`（`scripts/prod_check.sh --browse`。ホストでユーザーが実行）。
- 配備・切り替え: `docs/deploy.md`、`./scripts/release.sh`。
