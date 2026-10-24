# 開発

## 画面とバックエンドの起動

フロントエンドで Supabase のログインを有効にする場合（`frontend-next/.env.local` に `NEXT_PUBLIC_SUPABASE_URL` などがある）、バックエンドも `AUTH_MODE=required`（と `SUPABASE_URL`）で起動します。
`off` で起動すると、ログイン画面は出るのに、`/threads` などが 404 になります（匿名の会話には持ち主がいないため）。

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

フロントエンド（`frontend-next/`）は Vitest です（`cd frontend-next && npm test`。型検査は `npm run typecheck`）。CI の `frontend-next` ジョブが、単体テスト・型検査・ビルドを実行します。

### E2E（Playwright）

`cd frontend-next && npm run test:e2e` は、実ブラウザー（Chromium）から、画面（Next.js）と本物のアプリ（FastAPI。`/chat/stream` を返す）を**別のポート**で立てて、質問を送り、
回答と出典が画面に出ることを確かめます（`frontend-next/e2e/chat.e2e.ts`）。本番（Vercel → Cloud Run）と同じ別オリジンで、確かめるのは、ブラウザー・CORS・ストリームの**つなぎ目**です。回答の中身は pytest / Vitest の役目です。
ログインまわりも 3 シナリオあります（`frontend-next/e2e/auth.e2e.ts`）: ログインして戻ると画面の状態が URL に戻る / チャットが 401 を返すとトークンを 1 回だけ更新して送り直す / 更新できないとログイン画面に戻り、繰り返さない。

- 実 API（Gemini・埋め込み・DB・Supabase・Google）は使いません。`tests/e2e_server.py` が、モックモード・メモリ履歴・`AUTH_MODE=off` に固定し、
  検索を `FakeSearch`、グラフの LLM を `SearchingMockLLM`（どちらも `tests/fakes.py`）に差し替えて、127.0.0.1:8100（`E2E_PORT` で変更）に立てます。`app/` は変えません。
  画面は配信しません。CORS は、画面の 2 つのオリジン（`E2E_WEB_ORIGINS`）だけに許します（許していないオリジンのプリフライトは 400）。
- 画面は 2 通りにビルドし、`next start` で別のポートに立てます。`frontend-next/.next-e2e`（`npm run build:e2e`。`NEXT_PUBLIC_SUPABASE_*` を空にして、ログイン画面が出ない構成。チャット用。127.0.0.1:3100）と、
  `frontend-next/.next-e2e-auth`（`npm run build:e2e:auth`。偽の Supabase の URL `https://auth.e2e.invalid` を入れた、ログイン画面が出る構成。認証用。127.0.0.1:3101）です。どちらも API の向き先は `http://127.0.0.1:8100` に固定です。
  `.next`（`.env.local` の本物の Supabase の設定が入る）は使いません。`playwright.config.ts` の `webServer` が、ビルド（2 つは同じフォルダーなので順に）とサーバーの起動・停止まで行います。
- 認証のシナリオは、本物の supabase-js を、偽の Supabase（`frontend-next/e2e/support/supabase-mock.ts`。ブラウザー側の `page.route`）につなぎます。画面と API が別オリジンなので、`page.route` で返す偽の API の応答には CORS のヘッダーを付けます。
  `.invalid` のホストへの通信と、127.0.0.1 以外へのすべての通信は止めて記録し、テストの最後に空であることを確かめます（実 Supabase・実 Google には届きません）。
  更新の失敗の応答の形は SDK のソースに合わせた推定で、**本物の Supabase での更新・期限切れの挙動は、ここでは確かめていません**（本番に近い環境での 1 時間後の確認は別）。
- 初回だけ、Playwright 用の Chromium を入れます: `cd frontend-next && npx playwright install chromium`（約 120MB。`~/.cache/ms-playwright` に入り、
  Playwright MCP 用の Chromium（`.devcontainer/install-playwright.sh`）とは別の版です。システムのライブラリーは MCP 用の導入で入っています）。
- Python は、リポジトリ直下の `.venv` があればそれを、無ければ PATH の `python` を使います（`E2E_PYTHON` で指定できます）。
- 失敗時の成果物（`frontend-next/test-results/`・`frontend-next/playwright-report/`）は Git 管理外です。
- 今あるのは 4 シナリオ（チャット 1、認証 3）で、CI には入れていません。Vite 版から 2026-10-24 に移しました（移した直後に、新旧とも 4 件通ることを確かめた）。実 API のスモークテストも、まだありません。

#### 実 API の検証（フォールバック）: `npm run test:e2e:real-api`

**本物の Gemini を呼びます（課金）。** 通常の `npm run test:e2e` と CI には入りません（`E2E_REAL_API=1` のときだけ別の構成になります）。
手動で、必要なときだけ実行します（再試行はしません）。確かめるのは、主モデルで始めた会話を、フォールバックのモデル（`gemini-3.5-flash-lite`）で続けられるか、主モデルに戻しても続くかです（`docs/configuration.md` の「主モデルが落ちたとき」）。

- `tests/real_api_server.py`（127.0.0.1:8102）が、本物の Gemini でアプリを立てます。履歴はメモリ（`DATABASE_URL` は空）、認証なし、資料の検索は偽物なので、DB・Supabase・埋め込みの API には触れません。`.env` の `GEMINI_API_KEY` だけを使います（キーが無いと、モックモードなので、テストは理由を付けてスキップします）。
- 本物の 503 は狙って起こせないので、**失敗だけを偽物**にします。主モデルへの要求だけを 503 の応答に差し替え（`tests/real_api_faults.py`。本物と同じ SDK の経路を通ります）、フォールバック先は本物を呼びます。
  オフラインのテスト（`tests/test_real_api_faults.py`）で、差し替えた 503 が `ServerError` → `classify_failure` の経路を通ることと、アプリがフォールバックに切り替わることを確かめてあります。本物の 503 との差は、ネットワークを通らないこと（待ち時間がほぼ 0 で、ヘッダーが無い）です。
- 1 つの会話で 3 ターン: ① 主モデル（検索のツール呼び出しを含む）→ ② 主モデルを 503 にして、同じ会話を続ける（フォールバックが履歴を引き継ぐ）→ ③ 主モデルに戻して続ける。
  結果は `frontend-next/test-results/real-api-fallback.json` に残ります（各ターンのステータス、秒数、Gemini に出た要求のモデル・差し替えの有無・ステータス、エラー。API キーや本文の全体は残しません）。
- 呼び出しは 1 回の実行で通常 6 回前後、最悪で 18 回程度です（1 ターンに LLM を 2 回呼び、遅いと 8 秒でヘッジの追加が最大 3 本）。
- 失敗した応答のヘッダーは、`retry-after` と `x-ratelimit-*` の 7 つだけを JSON に残します（レート制限か容量不足かの切り分け用。キー・Authorization・Cookie・本文は残しません）。
- **確かめられたこと・まだのこと（2026-10-20、実行 2 回）:** 主モデルが本物の 503 を返し続けた回では、フォールバックのモデルが 200 で答えるところまでは確かめた
  （本物の 503 に `retry-after` と `x-ratelimit-*` は付いていなかった）。主モデルが一度も成功しないと、1 ターン目を「検証不能」として記録して止まるので、
  主モデルが作った履歴（thought signature を含む）を別モデルが続けられるか、逆方向も、**この 2 回では確かめられなかった**
  （その後、2026-10-21 に別の方法〔モデルを直接呼ぶ使い捨てのスクリプト〕で確かめた。結果は [configuration.md](configuration.md) の「検証の状況」）。
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
`./app`・`./storage:ro`、`--reload`、ヘルスチェック、`.env` の読み込み）は、開発コンテナに Docker が無いので、ホスト側で次のとおり確かめます。
**2026-10-21 にホスト側（OrbStack）で確かめたのは、`api` が healthy になること、`/health` の応答、`--reload` の反応、`.env` の実キーでの `/chat/stream`（直接 `localhost:8000`）です。**
`localhost:8000/` の 200 と `POST /chat` は、この回では実行していません:

```bash
docker compose up --build -d                       # .env が無ければモックモード
docker compose ps                                  # docunavi-api が healthy になる（ヘルスチェック。数十秒かかることがある）
curl -s localhost:8000/health                      # {"status":"ok",…}。.env に DATABASE_URL があれば "history":"postgres"
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/    # 404（API は画面を配信しない。画面は Vercel の frontend-next）
curl -s -X POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message":"こんにちは","thread_id":"00000000-0000-4000-8000-000000000001"}'   # 返答の JSON（モックなら固定の返答）
docker compose logs --tail 30 api                  # Traceback や起動失敗が無い
touch app/main.py && sleep 3 && docker compose logs --tail 5 api   # --reload が反応する（ホスト↔コンテナの変更通知。Docker Desktop では WATCHFILES_FORCE_POLLING が効く）
docker compose down                                # 終わったら止める
```

`frontend` サービス（`frontend-next` の next dev。`api` が healthy になってから起動する。これは起動順の制御だけで、あとで `api` が落ちても `frontend` は再起動しない。`docker-compose.yml`）も、ホスト側で次のとおり確かめます。上の `api` の確認に加えて:

```bash
docker compose config > /dev/null                  # 構文と変数の展開が通る
docker compose up --build                          # frontend が起動する（初回は npm install。healthy まで数十秒〜1 分）
docker compose ps                                  # docunavi-api と docunavi-frontend が healthy
curl -s -o /dev/null -w '%{http_code}\n' localhost:3000/          # 200（Next.js の画面）。up -d の直後は npm install と最初のコンパイル中で、遅いか 000 になる
curl -s localhost:3000/health                      # api の /health がそのまま返る（next.config.ts の rewrites → api:8000）
touch frontend-next/src/Root.tsx && docker compose logs --tail 5 frontend   # ホットリロードが反応する（効かなければ WATCHPACK_POLLING）
docker compose restart frontend && docker compose ps                  # frontend だけ再起動しても api は動き続ける
```

**Next.js 版の `frontend` の状況（2026-10-25 更新）:** Vite の開発サーバーから置き換えた（2026-10-24）。
**ホスト側（OrbStack）で、起動までは確認済み（2026-10-25）:** `docker compose up --build -d db api frontend` が通り、`ps` で `db`・`api`・`frontend` が healthy（`frontend` は作成の約 16 秒後）。
`localhost:3000/health` が `{"status":"ok","mode":"gemini","history":"postgres"}`（rewrites → `api:8000` が通る）、`localhost:3000/` が 200。
**未確認:** ホットリロード（`WATCHPACK_POLLING`）、`restart frontend` で `api` が動き続けること、`down` の所要時間、ブラウザーでの見た目と SSE の体感、Compose の `frontend` からの Google ログインの戻り。
開発コンテナの中では、`next dev`（`API_PROXY_TARGET` 付き）が `/` で 200、`/health` を API へ転送、`/chat/stream` の SSE を転送する、までを確かめてある。
`frontend_node_modules` は `frontend_next_node_modules` に変えた。Vite 版のボリューム `docunavi_frontend_node_modules` は、この確認の時点でホストに残っていなかった（消す必要は無い）。

**以下の「検証の記録」（2026-10-21〜22）は、Vite 版の `frontend` のものです（当時の記録のまま。パスやポートは今と違う）。**

**検証の記録（2026-10-21、ホスト側。OrbStack の `docker compose`）**

- 確認済み: `docker compose config` が通る / `api` と `frontend` の両方が healthy / `localhost:5173/` が 200 / `localhost:5173/health` が `api` の応答を返す（Vite のプロキシ → `api:8000`）/
  ホットリロード（`frontend/src/main.tsx` の更新で `page reload`）/ `api` の `--reload` / `frontend/package-lock.json` が変わらない（`git status --short` が空）。
- SSE: Vite のプロキシ越しの `/chat/stream` が、チャンクを逐次に届けることを確かめた（画面からの `fetch` と、Python の `http.client` で各チャンクの到着時刻を測るスクリプトの 2 通り。
  実キーの実 API を、短い質問と長い質問で合計 3 回）。プロキシ経由は最初のバイトまで 22.4 秒で 13 チャンクが約 1.25 秒に分かれて届き、`api` に直接つなぐと 31.0 秒で 21 チャンクが約 1.36 秒だった。
  **この 22.4 秒と 31.0 秒の差は、プロキシの効果ではない**（別々の LLM 呼び出しで、バックエンドの待ちがばらつくだけ）。言えるのは、プロキシが SSE をバッファして一括にしない、待ちを増やしていない、まで。
- 未検証: 人の目で見たときの、長い回答の表示の感触 / Google ログインが Compose の `frontend` から戻ってくること（`frontend/.env.local` をマウントするので、画面にはログイン画面が出る。`api` は `AUTH_MODE=off` のまま）/
  `docker compose down` 時の SIGTERM の扱い（`sh -c` 経由。必要なら `init: true`）。
- 停止（`time docker compose down`）: 合計 10.8 秒。`api` は 0.3 秒、`frontend` は 10.2 秒だった。
  `frontend` の 10 秒は Compose の既定の停止猶予（10 秒）にちょうど当たるので、SIGTERM で終わらず、猶予のあとに SIGKILL で止まった可能性が高い（`sh -c` が SIGTERM を子の Vite へ渡さないため、と推測。未確認）。
  開発用なので実害は無く、今回の合格条件には含めない。直すなら `docker-compose.yml` の `frontend` に `init: true`（または `command` を `exec` にする）を足して、`down` が数秒で終わるかを確かめる。

`frontend-next/package-lock.json` と `package.json` が合っていないと、`npm install` がロックを書き換えます（`git status` で確かめる）。
`node_modules` は名前付きボリューム `frontend_next_node_modules` に、`.next` は `frontend_next_cache` にあります。壊れたら `docker compose down -v` で消えます（`-v` は他のボリュームも消す）。

`.env` に実キー（`GEMINI_API_KEY`）があると、compose の `api` は本物の LLM で動き、資料の検索には下の「ローカルの db」が要ります（資料が入っていなければ起動に失敗します）。
モックモードだけで確かめるなら、`.env` を一時的に別名にするか、別のディレクトリで実行します。結果（`docker compose ps` と `/health` の出力）を教えてもらえれば、確認済みとして記録します。

**ローカルの db（2026-10-22 に構成を変更。モックモードの起動はホスト側で確認済み。資料の取り込みと実キーでの動作は未確認）**

- 構成: `db`（`docunavi-db`。`pgvector/pgvector:0.8.7-pg17`）、`api`（`docunavi-api`）、`frontend`（`docunavi-frontend`）。ネットワークは `docunavi-frontend-net`（`frontend` と `api`）と
  `docunavi-backend-net`（`api` と `db`。`internal: true` で、`db` は外へも出られず、ホストからも届かない）。ボリュームは `pg-data`（`db` のデータ）と `frontend_next_node_modules`・`frontend_next_cache`。
  `api` と `db` はホストにポートを公開せず、公開するのは `frontend` の 3000 番だけです。起動順は `db` → `api` → `frontend`（どれも `condition: service_healthy`）。
- **`api` の `DATABASE_URL` は、`.env` の値ではなく compose の `db` に固定されます**（`docker-compose.yml` の `environment` が `env_file` より優先される）。本番の DB には、compose からは届きません。
  パスワードは `LOCAL_DB_PASSWORD`（省略すると `docunavi-local`。ローカルだけの値で、`db` はポートを公開しない）。
- `db` は空で始まります。`pg-data` が空の初回の起動で、`db/local-init/01-bootstrap.sh` が入れ物だけを作ります（`extensions` スキーマと `vector` 型、ロール `chatbot_app`、スキーマ `chatbot`）。テーブルと資料は作りません。
- **このため、`.env` に実キーがある状態で `docker compose up` すると、`api` は「検索できる資料が DB にありません」で起動に失敗し、`frontend` も起動しません**（`api` が healthy にならないため）。
  先に資料を入れるか、モックモード（`.env` を別名にする）で起動します。
- 資料を入れる手順（**2026-10-22 JST にホストで実行済み。結果は下の「確認済み（資料の取り込み）」**。最後の埋め込みは Gemini の実 API を呼ぶので課金される）。`api` が起動している必要はなく、`docker compose run --rm api …` でも同じです。
  **入れた後は `docker compose restart api` が要る。** `api` は `uvicorn --reload` で動くため、起動の検査に失敗しても、コンテナは落ちず（再起動もされず）、`unhealthy` のまま止まり、`app/` のファイルが変わるまで待つ:
  1. `docker compose up -d db`
  2. テーブルと資料（課金なし）: `--input` が必須で、資料群ごとに実行する。`for c in zahard cafe guides; do docker compose run --rm api python -m app.ingestion --input storage/pdf/$c --apply; done`
     （`--apply` なしは確認だけ。2026-10-22 に、開発コンテナの `.venv` で確認だけを実行: zahard 3 文書 12 チャンク、cafe 1 文書 30、guides 2 文書 38、合計 80 チャンク）
  3. 埋め込み用の列: `docker compose exec db psql -U postgres -d docunavi -f /migrations/0001_pgvector_embeddings.sql`、続けて `0002`・`0003`・`0005`（`0004` は pgroonga で、ローカルには入れない）
  4. 埋め込み（実 API）: まず `docker compose run --rm api python -m app.ingestion.embed`（確認だけ。件数・費用の見積りを表示）、
     見積りを見てから `docker compose run --rm api python -m app.ingestion.embed --apply`。既定の上限（300 チャンク・10 回）に、80 チャンク（32 件ずつで 3 回）は収まる。
  3 の `psql` は `postgres` ロール（所有者の `chatbot_app` ではない）で実行するので、テーブルの所有者が違っても列は足せます。ただし、この順序と各 SQL がローカルで通ることは、まだ確かめていません。
- **`docker compose down -v` は `pg-data` も消します**（埋め込みの作り直しは実 API を呼ぶ）。`node_modules` だけ消したいときは `docker volume rm docunavi_frontend_next_node_modules`（先に `docker compose down`）。
- `api` の 8000 番を確かめたいとき（ビルド済みの画面配信・`curl`）: `docker compose -f docker-compose.yml -f docker-compose.api-port.yml up -d`。8000 番は `127.0.0.1` にだけ公開されます。
  上のコマンド例の `localhost:8000` は、この上書きを重ねたときの話です（`frontend` の 3000 番経由なら、`localhost:3000/health` などが `api` に届きます）。
- **確認済み（2026-10-22、ホスト側。`.env` を別名にしたモックモード）:** `docker compose config` が通る / 空の `pg-data` からの初回の起動で、初期化スクリプトが通る
  （`CREATE SCHEMA`・`CREATE EXTENSION`・`CREATE ROLE`・`CREATE SCHEMA`・`ALTER ROLE`・`GRANT` がログに出る。Postgres 17.11）/ `db`・`api`・`frontend` の 3 つが healthy になる（`db` → `api` → `frontend` の順）/
  `api` が `internal` の `backend-net` 越しに `db` へ届く（起動ログに「会話履歴の保存先: Postgres」）/ `localhost:5173/health` が `{"status":"ok","mode":"mock","history":"postgres"}` を返す
  （Vite のプロキシ → `api:8000` → `db`）/ `docker compose ps` で公開されているのは `frontend` の 5173 番だけ（`api` と `db` は `8000/tcp`・`5432/tcp` の表示だけで、ホストには出ない）/
  `docker compose down` なしの 2 回目の起動（`pg-data` が残る状態）でも、3 つが healthy になる（`docker compose up -d` のあと 20 秒）。
- **確認済み（資料の取り込み。2026-10-22 JST、ホスト側。`.env` は実キーのまま）:** `docker compose up -d db` → 資料群ごとの `app.ingestion --input storage/pdf/<名前> --apply`（zahard 3 文書 12 チャンク、cafe 1 文書 30、guides 2 文書 38。取り込めなかった文書 0）→
  `db/migrations` の 0001・0002・0003・0005 を `psql -U postgres -d docunavi -v ON_ERROR_STOP=1 -f /migrations/<名前>.sql` で適用（エラーなし。NOTICE は「すでに存在」だけ。0003・0005 の列は、取り込みの時点で作られている）→
  `app.ingestion.embed`（確認だけ: 80 件・3 回・約 $0.0023）→ `--apply`（呼び出し 3 回、保存 80 件）。続けて `docker compose restart api` で、`api` が healthy になった（起動ログに「会話履歴の保存先: Postgres」、`/health` が 200。起動の検査 `search_vector.verify()` を通った）。
  ホストの `docker compose` は、開発コンテナ `docunavi-devcontainer` を同じプロジェクト `docunavi` の「orphan」として警告する（開発コンテナ側の定義はこのリポジトリの `.devcontainer/docker-compose.yml`）。無害だが、**`--remove-orphans` は付けない**（開発コンテナが消える）。
- 未確認: `api` が Gemini に届くこと（質問に実際に答えること。起動の検査は DB だけを見る）/ `db` が `internal` で外へ出られないこと（出られないことの実測はしていない）/
  `docker-compose.api-port.yml` を重ねた起動 / `frontend` の SIGTERM の扱い。
- `docker compose down`（2026-10-22、ホスト側。`-v` なし）: 3 つのコンテナと 2 つのネットワークが消え、`pg-data` は残る。`db` は 0.2 秒、`api` は 0.4 秒、`frontend` は 10.2 秒。
  `frontend` が Compose の既定の停止猶予（10 秒）にちょうど当たるのは 2026-10-21 の記録と同じで、SIGTERM で終わっていない可能性が高い（未確認。実害はなく、直すなら `init: true` か `exec`）。

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

## ログイン済みの画面を Playwright MCP で確認する

`AUTH_MODE=required` の環境（本番の Cloud Run、Vercel の本番）の質問・会話の一覧は、Supabase のセッションが要ります。
Playwright が起動したブラウザーでは、Google が「安全でない可能性があります」とログインを拒否します（`playwright open` `codegen` も同じ）。
そこで、ホストの Chrome で入ったセッションを、storage state のファイルとして Playwright MCP に渡します（2026-10-24 に eosin で通った）。

1. ホストの Chrome の**シークレットウィンドウ**で、対象の URL にログインする（質問の入力欄が出るまで）。
2. DevTools の Console（貼り付けが止められたら `allow pasting` と入力）で次を実行すると、`auth-eosin.json` がダウンロードされる。
   その後、すぐウィンドウを閉じる。**アプリの「ログアウト」は押さない**（押すとこのセッションも失効する）。開いたままにすると、トークンの更新で写しが切れる。

   ```js
   (() => {
     const items = Object.keys(localStorage)
       .filter(k => /^sb-.*-auth-token$/.test(k))
       .map(k => ({ name: k, value: localStorage.getItem(k) }));
     const state = { cookies: [], origins: [{ origin: location.origin, localStorage: items }] };
     const a = document.createElement('a');
     a.href = URL.createObjectURL(new Blob([JSON.stringify(state)], { type: 'application/json' }));
     a.download = 'auth-eosin.json';
     a.click();
     console.log('keys:', items.map(i => i.name));
   })()
   ```

3. ファイルを Git 管理外の `.playwright-mcp/` に置く（`mv ~/Downloads/auth-eosin.json <リポジトリ>/.playwright-mcp/auth-eosin.json`）。
   ログインした URL が確認したい URL と違う場合は、`origin` を書き換える（同じ Supabase プロジェクトのセッションなので、どの URL でも使える）。
4. `.mcp.json` の playwright の引数に `--isolated --storage-state .playwright-mcp/auth-eosin.json` を足し、`/mcp` で再接続する
   （`--storage-state` は isolated 専用。`@playwright/mcp@0.0.83` の `--help` で確認）。**この変更はコミットしない。**
5. **確認が終わっても、後始末をしない**（2026-10-25、利用者の決定。消して作り直す手戻りが続いたため）。`.mcp.json` の変更、`auth-eosin.json`、Playwright のログイン状態は残す。
   元に戻す・ファイルを削除する・サインアウトするのは、利用者が自分の言葉で頼んだときだけ。作業の区切りや、他の AI の助言の貼り付けでは行わない。
   トークンが切れて Playwright のログインが落ちたときだけ、1 からやり直す。

開発コンテナを作り直した後の扱い: `.playwright-mcp/` と `.mcp.json` はリポジトリ（ホストのフォルダー）にあるので、残る。
ただし、`.mcp.json` に `--storage-state` を足したまま、ファイルを消した・置いていないと、Playwright MCP が起動しない可能性がある（未確認）。
作り直した後で Playwright MCP が使えないときは、まず `git diff .mcp.json` を見て、ファイルが無いのに引数が残っていれば、利用者に伝えて元に戻すか置き直す。セッションは約 1 時間でアクセストークンが切れ、
更新に失敗したら 1 からやり直す。手順そのもの（この節）は、リポジトリと `~/.claude` のメモリ（ボリューム）の両方にあるので、作り直しでは消えない。

ファイルはログインの認証情報（リフレッシュトークン）なので、表示・貼り付け・コミットをしない。
ホストの Chrome の拡張につなぐ `--extension` は、MCP が開発コンテナの中にあり Chrome がホストにあるので、この構成では使えない。
