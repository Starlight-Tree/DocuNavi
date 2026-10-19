# 開発

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

フロントエンドは Vitest です（`cd frontend && npm test`）。

### E2E（Playwright）

`cd frontend && npm run test:e2e` は、実ブラウザー（Chromium）から、本物のアプリ（FastAPI が画面を配信し、`/chat/stream` を返す）に質問を送り、
回答と出典が画面に出ることを確かめます（`frontend/e2e/chat.e2e.ts`）。確かめるのは、ブラウザー・配信・ストリームの**つなぎ目**で、回答の中身は pytest / Vitest の役目です。
ログインまわりも 3 シナリオあります（`frontend/e2e/auth.e2e.ts`）: ログインして戻ると画面の状態が URL に戻る / チャットが 401 を返すとトークンを 1 回だけ更新して送り直す / 更新できないとログイン画面に戻り、繰り返さない。

- 実 API（Gemini・埋め込み・DB・Supabase・Google）は使いません。`tests/e2e_server.py` が、モックモード・メモリ履歴・`AUTH_MODE=off` に固定し、
  検索を `FakeSearch`、グラフの LLM を `SearchingMockLLM`（どちらも `tests/fakes.py`）に差し替えて、127.0.0.1:8100（`E2E_PORT` で変更）に立てます。`app/` は変えません。
- 画面は 2 通りにビルドします。`frontend/dist-e2e`（`npm run build:e2e`。`VITE_SUPABASE_*` を空にして、ログイン画面が出ない構成。チャット用）と、
  `frontend/dist-e2e-auth`（`npm run build:e2e:auth`。偽の Supabase の URL `https://auth.e2e.invalid` を入れた、ログイン画面が出る構成。認証用）です。`frontend/dist` は使いません
  （`.env.local` の本物の Supabase の設定が入るため）。認証用のサーバーは 127.0.0.1:8101（`E2E_AUTH_PORT` で変更）で、`E2E_DIST=dist-e2e-auth` を渡して立てます。
  `playwright.config.ts` の `webServer` が、ビルドとサーバーの起動・停止まで行います。
- 認証のシナリオは、本物の supabase-js を、偽の Supabase（`frontend/e2e/support/supabase-mock.ts`。ブラウザー側の `page.route`）につなぎます。
  `.invalid` のホストへの通信と、127.0.0.1 以外へのすべての通信は止めて記録し、テストの最後に空であることを確かめます（実 Supabase・実 Google には届きません）。
  更新の失敗の応答の形は SDK のソースに合わせた推定で、**本物の Supabase での更新・期限切れの挙動は、ここでは確かめていません**（本番に近い環境での 1 時間後の確認は別）。
- 初回だけ、Playwright 用の Chromium を入れます: `cd frontend && npx playwright install chromium`（約 120MB。`~/.cache/ms-playwright` に入り、
  Playwright MCP 用の Chromium（`.devcontainer/install-playwright.sh`）とは別の版です。システムのライブラリーは MCP 用の導入で入っています）。
- Python は、リポジトリ直下の `.venv` があればそれを、無ければ PATH の `python` を使います（`E2E_PYTHON` で指定できます）。
- 失敗時の成果物（`frontend/test-results/`・`frontend/playwright-report/`）は Git 管理外です。
- 今あるのは 4 シナリオ（チャット 1、認証 3）で、CI には入れていません。実 API のスモークテストも、まだありません。

テストは DB も埋め込みの API も使いません。資料の検索（`app/agents/tools.py` の `search_chunks_async`）は、`tests/conftest.py` が自動で偽の検索
（`scripts/bm25_baseline.py`。`storage/pdf/` の PDF を BM25 で引く、決まった入力に決まった出力を返す実装）に差し替えます。
アプリのモックモードは検索しない固定の返答なので、グラフの「ツール呼び出し → 回答」の流れを通すテストは、偽の LLM（`tests/fakes.py` の
`SearchingMockLLM`、アプリの画面側は `searching_llm` フィクスチャ）を使います。本物の検索（`app/search_vector.py`）を確かめるテストは、
`@pytest.mark.real_search` で差し替えを外し、DB と埋め込みを自分で偽物にします（`tests/test_search_vector.py`）。

GitHub Actions（`.github/workflows/ci.yml`）が、`main` へのプッシュと PR で、バックエンドのテスト（Python 3.12）、
フロントエンドのテストとビルド、Docker イメージのビルドと起動確認（`/health`・画面・`/chat`）を行います。デプロイは含みません。
**`docker compose up` は CI では確かめていません**（CI は `docker build` したイメージを `docker run` するだけ）。compose 固有の部分（`docker-compose.yml` のボリューム
`./app`・`./storage:ro`、`--reload`、ヘルスチェック、`.env` の読み込み）は、開発コンテナに Docker が無いので、**ホスト側で次のとおり確かめてください（未実施）**:

```bash
docker compose up --build -d                       # .env が無ければモックモード
docker compose ps                                  # docunavi-api が healthy になる（ヘルスチェック。数十秒かかることがある）
curl -s localhost:8000/health                      # {"status":"ok",…}。.env に DATABASE_URL があれば "history":"postgres"
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/    # 200（画面。frontend/dist はイメージのビルドで入る）
curl -s -X POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message":"こんにちは","thread_id":"00000000-0000-4000-8000-000000000001"}'   # 返答の JSON（モックなら固定の返答）
docker compose logs --tail 30 api                  # Traceback や起動失敗が無い
touch app/main.py && sleep 3 && docker compose logs --tail 5 api   # --reload が反応する（ホスト↔コンテナの変更通知。Docker Desktop では WATCHFILES_FORCE_POLLING が効く）
docker compose down                                # 終わったら止める
```

`.env` に実キーがあると、compose のコンテナは本物の LLM と DB に接続します（`DATABASE_URL` と `GEMINI_API_KEY` が必須。揃わなければ起動に失敗します）。
モックモードだけで確かめるなら、`.env` を一時的に別名にするか、別のディレクトリで実行します。結果（`docker compose ps` と `/health` の出力）を教えてもらえれば、確認済みとして記録します。

アクションのコミット SHA 固定は、Dependabot（`.github/dependabot.yml`）が週次で更新の PR を出します（公開から 7 日待ちます）。手動実行もできます（`workflow_dispatch`）。

依存のバージョンは `requirements.txt` と `requirements-dev.txt` に固定しています（Python 3.12、全プラットフォーム向け）。
どちらも `requirements.in` / `requirements-dev.in` から [uv](https://docs.astral.sh/uv/) で生成したファイルなので、直接は編集しません。
更新するときは `.in` を編集して、次を実行します（`pip install uv` で入ります）。

```bash
uv pip compile requirements.in --universal --python-version 3.12 -o requirements.txt
uv pip compile requirements-dev.in --universal --python-version 3.12 -c requirements.txt -o requirements-dev.txt
```

`requirements-lint.txt` は、CI の `workflow-lint` ジョブ（actionlint と zizmor でワークフローを検査する）だけが使う、ハッシュ付きの固定です。
アプリの依存とは別で、版は `requirements-lint.in` を手で上げて、次で再生成します。手元で同じ検査をするには、`pip install --require-hashes -r requirements-lint.txt` の後に
`actionlint` と `zizmor --offline .` を実行します（`--offline` を外すと、GitHub への問い合わせが要る検査も動きます。CI では `GH_TOKEN` を渡しています）。

```bash
uv pip compile requirements-lint.in --universal --python-version 3.12 --generate-hashes -o requirements-lint.txt
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

次の `make` が、下の手順をスクリプト（`scripts/test-postgres-up.sh` / `scripts/test-postgres-down.sh`）にしたものです。
`make test-db` は起動 → `pytest` 全体 → 停止（失敗しても停止）、`make test-db-up` は起動して接続 URL を表示、`make test-db-down` は停止します
（`scripts/test-postgres-down.sh --purge` なら `/tmp/pg` ごと消します）。初回だけ `apt-get download` でパッケージ（約 20MB）を取得します。
ポートは 55432 で、使用中なら失敗します（`TEST_PG_PORT` で変えられます）。

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
スキップされたまま green にならないよう、別のステップで「スキップが 0 件」であることも確かめます
（対象は `tests/test_history_postgres.py` と、資料の取り込みの `tests/test_ingestion_postgres.py`）。
ただし、この自動テストが動くのは素の Postgres 17（ローカル・CI）です。Supabase の Shared pooler には、手動で一度、起動・往復・再起動をまたいだ続き・
テーブルの所在を確かめました（2026-10-14、LLM はモック。結果は `docs/configuration.md`）。このテスト一式を Supabase に向けた実行と、Cloud SQL は、まだです。
履歴の削除コマンド（`python -m app.history_admin`）のテストも、このファイルの末尾にあります。
