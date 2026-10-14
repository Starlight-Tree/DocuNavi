# 開発

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

フロントエンドは Vitest です（`cd frontend && npm test`）。

GitHub Actions（`.github/workflows/ci.yml`）が、`main` へのプッシュと PR で、バックエンドのテスト（Python 3.12）、
フロントエンドのテストとビルド、Docker イメージのビルドと起動確認（`/health`・画面・`/chat`）を行います。デプロイは含みません。
アクションのコミット SHA 固定は、Dependabot（`.github/dependabot.yml`）が週次で更新の PR を出します（公開から 7 日待ちます）。手動実行もできます（`workflow_dispatch`）。

依存のバージョンは `requirements.txt` と `requirements-dev.txt` に固定しています（Python 3.12、全プラットフォーム向け）。
どちらも `requirements.in` / `requirements-dev.in` から [uv](https://docs.astral.sh/uv/) で生成したファイルなので、直接は編集しません。
更新するときは `.in` を編集して、次を実行します（`pip install uv` で入ります）。

```bash
uv pip compile requirements.in --universal --python-version 3.12 -o requirements.txt
uv pip compile requirements-dev.in --universal --python-version 3.12 -c requirements.txt -o requirements-dev.txt
```

`tests/conftest.py` が `GEMINI_API_KEY` を空にしてからアプリを読み込むため、`.env` に実キーがあってもテストは常にモックモードで動き、Gemini API を呼びません。

RAG の精度は `tests/rag_eval_cases.py` の評価セット（関連 21 件・無関係 8 件）で回帰テストしています。2-gram 照合では拾えない言い換え（「気温差」→「温度差」など）は既知の取りこぼしです。

## 会話履歴の保存先（Postgres）のテスト

`tests/test_history_store.py` は偽物の接続プールで動くので、DB は要りません。本物の Postgres での検証（`tests/test_history_postgres.py`）は、
**`TEST_DATABASE_URL` を設定したときだけ**動き、無ければスキップされます（`pytest` の既定の実行には含まれません）。
テストごとに専用のスキーマを作って消すので、既存のテーブルには触れません。`.env` の `DATABASE_URL` はテストが空に上書きします
（実際の DB に書き込まないため）。

```bash
TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres pytest tests/test_history_postgres.py
```

開発コンテナには Postgres も Docker も無いので、手元で用意するなら、root なしで Debian のパッケージを展開できます
（2026-10-14 に、Debian 13・aarch64 で確認）。認証は `trust` で、ループバックだけで待ち受けます。終わったら止めてください。

```bash
mkdir -p /tmp/pg/debs /tmp/pg/root /tmp/pg/sock && cd /tmp/pg/debs
apt-get download postgresql-17 postgresql-client-17 libpq5
for f in *.deb; do dpkg -x "$f" /tmp/pg/root; done
export LD_LIBRARY_PATH=/tmp/pg/root/usr/lib/$(uname -m)-linux-gnu
B=/tmp/pg/root/usr/lib/postgresql/17/bin
$B/initdb -D /tmp/pg/data -U postgres --auth=trust -E UTF8 --locale=C.UTF-8
$B/pg_ctl -D /tmp/pg/data -l /tmp/pg/pg.log -w \
  -o "-p 55432 -c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp/pg/sock" start
TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres pytest tests/test_history_postgres.py
$B/pg_ctl -D /tmp/pg/data stop
```

本物の Postgres のテストが確かめていること: 再起動後も履歴が続く、スレッドが混ざらない、初回の同時起動が競合しない、
DB 側から接続を切られても次のリクエストが成功する、取り消し（クライアントの切断）で接続が取り残されない、アプリ経由で `/chat` が保存される。
CI（`backend` ジョブ）は、Postgres 17 のサービスコンテナを起動して `TEST_DATABASE_URL` を渡すので、このテストも実行されます。
スキップされたまま green にならないよう、別のステップで「スキップが 0 件」であることも確かめます。
ただし、この自動テストが動くのは素の Postgres 17（ローカル・CI）です。Supabase の Shared pooler には、手動で一度、起動・往復・再起動をまたいだ続き・
テーブルの所在を確かめました（2026-10-14、LLM はモック。結果は `docs/configuration.md`）。このテスト一式を Supabase に向けた実行と、Cloud SQL は、まだです。
履歴の削除コマンド（`python -m app.history_admin`）のテストも、このファイルの末尾にあります。
