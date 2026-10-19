# 開発

## 画面とバックエンドの起動

フロントエンドで Supabase のログインを有効にする場合（`frontend/.env.local` に `VITE_SUPABASE_URL` などがある）、バックエンドも `AUTH_MODE=required`（と `SUPABASE_URL`）で起動します。
`off` で起動すると、ログイン画面は出るのに、`/threads` などが 404 になります（匿名の会話には持ち主がいないため）。

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

#### 実 API の検証（フォールバック）: `npm run test:e2e:real-api`

**本物の Gemini を呼びます（課金）。** 通常の `npm run test:e2e` と CI には入りません（`E2E_REAL_API=1` のときだけ別の構成になります）。
手動で、必要なときだけ実行します（再試行はしません）。確かめるのは、主モデルで始めた会話を、フォールバックのモデル（`gemini-3.5-flash-lite`）で続けられるか、主モデルに戻しても続くかです（`docs/configuration.md` の「主モデルが落ちたとき」）。

- `tests/real_api_server.py`（127.0.0.1:8102）が、本物の Gemini でアプリを立てます。履歴はメモリ（`DATABASE_URL` は空）、認証なし、資料の検索は偽物なので、DB・Supabase・埋め込みの API には触れません。`.env` の `GEMINI_API_KEY` だけを使います（キーが無いと、モックモードなので、テストは理由を付けてスキップします）。
- 本物の 503 は狙って起こせないので、**失敗だけを偽物**にします。主モデルへの要求だけを 503 の応答に差し替え（`tests/real_api_faults.py`。本物と同じ SDK の経路を通ります）、フォールバック先は本物を呼びます。
  オフラインのテスト（`tests/test_real_api_faults.py`）で、差し替えた 503 が `ServerError` → `classify_failure` の経路を通ることと、アプリがフォールバックに切り替わることを確かめてあります。本物の 503 との差は、ネットワークを通らないこと（待ち時間がほぼ 0 で、ヘッダーが無い）です。
- 1 つの会話で 3 ターン: ① 主モデル（検索のツール呼び出しを含む）→ ② 主モデルを 503 にして、同じ会話を続ける（フォールバックが履歴を引き継ぐ）→ ③ 主モデルに戻して続ける。
  結果は `frontend/test-results/real-api-fallback.json` に残ります（各ターンのステータス、秒数、Gemini に出た要求のモデル・差し替えの有無・ステータス、エラー。API キーや本文の全体は残しません）。
- 呼び出しは 1 回の実行で通常 6 回前後、最悪で 18 回程度です（1 ターンに LLM を 2 回呼び、遅いと 8 秒でヘッジの追加が最大 3 本）。
- 失敗した応答のヘッダーは、`retry-after` と `x-ratelimit-*` の 7 つだけを JSON に残します（レート制限か容量不足かの切り分け用。キー・Authorization・Cookie・本文は残しません）。
- **確かめられたこと・まだのこと（2026-10-20、実行 2 回）:** 主モデルが本物の 503 を返し続けた回では、フォールバックのモデルが 200 で答えるところまでは確かめた
  （本物の 503 に `retry-after` と `x-ratelimit-*` は付いていなかった）。主モデルが一度も成功しないと、1 ターン目を「検証不能」として記録して止まるので、
  主モデルが作った履歴（thought signature を含む）を別モデルが続けられるか、逆方向も、**まだ確かめられていない**。
  また、1 ターン目に検索の出典（`sources`）が空だった回があった。空になるのは、検索ツールが呼ばれなかった、検索が 0 件だった（偽の検索はキーワードに当たらないと 0 件）、
  複数回検索して最後の検索が 0 件だった、のいずれか（出典は最後の検索の結果で置き換わる）で、どれだったかは記録が無く分かっていない。
  次の実行からは、偽の検索がモデルの検索語の先頭 24 文字・文字数・ハッシュ 8 桁とヒット数を記録し、各ターンの `searches` と最終の `sourcesCount` を結果 JSON に残す。
  出典が空だった回は `notes` に、上のどれかを書く。引き継ぎの検証とは別の件なので、失敗にはしない。

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
