# テスト

| 種類 | 実行 | 確かめること | 課金 | CI |
|---|---|---|---|---|
| バックエンド | `pytest` | 質問の流れ・API・ガード・認証・検索 | なし | あり |
| フロントエンド | `npm test` | 画面の部品と処理（Vitest） | なし | あり |
| E2E | `npm run test:e2e` | ブラウザー・CORS・ストリームのつなぎ目 | なし | なし |
| 実 API の E2E | `npm run test:e2e:real-api` | 主モデルが落ちたときのフォールバック | あり | なし |
| 本番の `/chat` | `prod_smoke_chat.py --send` | 本番の認証つき `/chat` | あり | なし |

`npm` は `frontend/` で実行します（型検査は `npm run typecheck`）。初回は `pip install -r requirements-dev.txt`。課金は、本物の Gemini（実 API の E2E）か本番の LLM（本番の `/chat`）を呼ぶ試験です。実 API の E2E は承認制、本番の `/chat` は先に有効化が要ります（下の節）。

検索・回答の品質の評価は、このテストとは別で、[evals/README.md](../evals/README.md)。最新の結果は CI の実行履歴で見ます（件数はこの文書に書きません）。
確認できていないことは、各節に書いてあります（本物の Better Auth での更新と期限切れ、予算を入れた後の実 API での実行、`docker compose up` の CI での確認）。

## バックエンドとフロントエンド（pytest・Vitest）

`tests/conftest.py` が `GEMINI_API_KEY` を空にしてからアプリを読み込むので、`.env` に実キーがあってもテストは常にモックモードで動き、Gemini API を呼びません。テストは DB も埋め込みの API も使いません。
資料の検索（`app/agents/tools.py` の `search_chunks_async`）は、`tests/conftest.py` が偽の検索（`scripts/bm25_baseline.py`。`storage/pdf/` の PDF を BM25 で引く）に差し替えます。
グラフの「ツール呼び出し → 回答」の流れを通すテストは、偽の LLM（`tests/fakes.py` の `SearchingMockLLM`、画面側は `searching_llm` フィクスチャ）を使います。本物の検索（`app/search/vector.py`）を確かめるテストは、`@pytest.mark.real_search` で差し替えを外し、DB と埋め込みを自分で偽物にします。
RAG の精度は `tests/rag_eval_cases.py`（関連 21 件・無関係 8 件）で回帰テストしています。2-gram 照合では拾えない言い換え（「気温差」→「温度差」など）は既知の取りこぼしです。

## CI

`.github/workflows/ci.yml` は、`main` へのプッシュと PR で、バックエンドのテスト（Python 3.12）、フロントエンドの単体テスト・型検査・ビルド、Go のテスト、認証 Worker の型検査、ワークフローの静的検査、Docker イメージのビルドと起動確認（`/health`・画面・`/chat`）、コミット件名の検査を行います。デプロイは含みません。
**`docker compose up` は CI では確かめていません**（CI は `docker build` したイメージを `docker run` するだけ）。

## E2E（Playwright）

`cd frontend && npm run test:e2e` は、実ブラウザー（Chromium）から、画面（Next.js）と本物のアプリ（FastAPI）を**別のポート**で立てて、質問を送り、回答と出典が画面に出ることを確かめます（`frontend/e2e/chat.e2e.ts`）。
本番と同じ別オリジンで、確かめるのは、ブラウザー・CORS・ストリームの**つなぎ目**です。回答の中身は pytest / Vitest の役目です。
ログインまわりは 3 シナリオです（`frontend/e2e/auth.e2e.ts`）: ログインして戻ると画面の状態が URL に戻る / チャットが 401 を返すとトークンを 1 回だけ更新して送り直す / 更新できないとログイン画面に戻り、繰り返さない。

- 実 API（Gemini・埋め込み・DB・Google）は使いません。`tests/e2e_server.py` が、モックモード・メモリ履歴・`AUTH_MODE=off` に固定し、検索を `FakeSearch`、LLM を `SearchingMockLLM`（どちらも `tests/fakes.py`）に差し替えて、127.0.0.1:8100（`E2E_PORT` で変更）に立てます。CORS は画面の 2 つのオリジン（`E2E_WEB_ORIGINS`）だけに許します。
- 画面は 2 通りにビルドし、`next start` で別のポートに立てます。`frontend/.next-e2e`（`npm run build:e2e`。ログインを使わない構成。127.0.0.1:3100）と、`frontend/.next-e2e-auth`（`npm run build:e2e:auth`。ログインを有効にし、認証サーバーの URL に `https://auth.e2e.invalid` を入れた構成。127.0.0.1:3101）です。API の向き先は `http://127.0.0.1:8100` に固定です。
  `.next`（`.env.local` の本物の設定が入る）は使いません。`playwright.config.ts` の `webServer` が、ビルド・起動・停止まで行います。
- 認証のシナリオは、認証サーバーへの通信をブラウザー側の `page.route`（`frontend/e2e/support/auth-mock.ts`）で偽物に差し替えます。画面と API が別オリジンなので、偽の応答には CORS のヘッダーを付けます。
  `.invalid` のホストと 127.0.0.1 以外へのすべての通信は止めて記録し、テストの最後に空であることを確かめます（実際の認証・Google には届きません）。**本物の Better Auth での更新・期限切れの挙動は、ここでは確かめていません。**
- 初回だけ Chromium を入れます: `cd frontend && npx playwright install chromium`（約 120MB。Playwright MCP 用の Chromium とは別の版）。
- Python は、リポジトリ直下の `.venv` があればそれを、無ければ PATH の `python` を使います（`E2E_PYTHON` で指定できます）。失敗時の成果物（`frontend/test-results/`・`frontend/playwright-report/`）は Git 管理外です。
- 今あるのは 4 シナリオ（チャット 1、認証 3）で、CI には入れていません。

### 実 API の検証（フォールバック）: `npm run test:e2e:real-api`

**本物の Gemini を呼びます（課金）。** 通常の `npm run test:e2e` と CI には入りません（`E2E_REAL_API=1` のときだけ別の構成）。手動で、必要なときだけ実行します（再試行はしません）。
確かめるのは、主モデルで始めた会話を、フォールバックのモデル（`gemini-3.5-flash-lite`）で続けられるか、主モデルに戻しても続くかです（[configuration.md](configuration.md) の「主モデルが落ちたとき」）。

- `tests/real_api_server.py`（127.0.0.1:8102）が、本物の Gemini でアプリを立てます。履歴はメモリ、認証なし、資料の検索は偽物なので、DB・埋め込みの API には触れません。キーが無いとモックモードなので、テストは理由を付けてスキップします。
- 本物の 503 は狙って起こせないので、**失敗だけを偽物**にします。主モデルへの要求だけを 503 に差し替え（`tests/real_api_faults.py`）、フォールバック先は本物を呼びます。本物との差は、ネットワークを通らないこと（待ち時間がほぼ 0 で、ヘッダーが無い）です。
- 1 つの会話で 3 ターン: ① 主モデル（検索のツール呼び出しを含む）→ ② 主モデルを 503 にして続ける → ③ 主モデルに戻して続ける。結果は `frontend/test-results/real-api-fallback.json`（各ターンのステータス・秒数・モデル・エラー・予算。キーや本文の全体は残さない）。
- **1 回の実行の予算（`tests/real_api_budget.py`）:** Gemini への外部 API 呼び出し**総数 20 回**（再試行・ヘッジの追加を含む。偽の 503 は数えない）、質問 **10 件**、実行全体 **300 秒**。上限に達したら、送信の前に止めて結果を報告します。環境変数 `REAL_API_MAX_CALLS` `REAL_API_MAX_QUESTIONS` `REAL_API_MAX_SECONDS` で下げられますが、天井は超えられません。
  `LLM_MAX_ATTEMPTS=2`、1 試行 30 秒、全体 60 秒に固定します。概算費用は約 $0.11 以下（トークン数は未測定）。
- **実行のしかた（承認制）:** 目的を `REAL_API_PURPOSE` に書かないと起動しません。`REAL_API_PURPOSE='…' bash scripts/dev-local.sh with-key -- npm run test:e2e:real-api`（`frontend/` で）。`ack-gemini` の承認が無いとキーを渡しません。質問文・回数・費用を示して承認を得てから実行します。
- **確かめられたこと・まだのこと（2026-10-20、実行 2 回）:** 主モデルが本物の 503 を返し続けた回で、フォールバックが 200 で答えるところまで確認しました。主モデルが作った履歴を別モデルが続けられるかは、この 2 回では確かめられず、2026-10-21 に別の方法で確認しました（[configuration.md](configuration.md) の「検証の状況」）。
  予算の実装は、モックだけで確認しました（`tests/test_real_api_budget.py`）。**予算を入れた後の実 API での実行は、まだしていません。**

## 本番の /chat の自動テスト（テスト専用ユーザー）

本番は `AUTH_MODE=required` で、`/chat` は Better Auth の JWT が要ります（Google ログインだけでは自動で叩けない）。そこで Worker `docunavi-api`（[workers/api/src/index.ts](../workers/api/src/index.ts)）に、**テスト専用のメール + パスワード認証**を、既定で無効のまま足してあります。

- **有効になる条件:** Secret `TEST_LOGIN_TOKEN`（32 文字以上）と `TEST_LOGIN_EMAIL` が両方あるときだけです。どちらかを外すと Google ログインだけに戻ります（パスワードの入口は 400 `EMAIL_PASSWORD_DISABLED`）。
- **有効な間の制限:** 登録もログインも、`TEST_LOGIN_EMAIL` の 1 アドレス**かつ**ヘッダー `x-test-login-token` が `TEST_LOGIN_TOKEN` と一致するときだけです（それ以外は 403）。一般の登録は開きません。メールアドレスは実在しないドメイン（例: `e2e-test@docunavi.invalid`）にします。
- **権限:** ロールを割り当てないので `default` ロール（`zahard` `cafe` `guides`）。Google ログインの一般の利用者と同じです。
- **JWT:** `GET /api/auth/token`（ES256、15 分、`aud=docunavi`）を、Cloud Run の `/chat` に `Authorization: Bearer` で送ります。
- **スクリプト:** [scripts/prod_smoke_chat.py](../scripts/prod_smoke_chat.py)。既定はログインして JWT を取るだけ。`--send` で `/chat` を 1 回だけ叩きます（本番の LLM・埋め込みを使う。課金される）。`--create` は初回だけ。

**運用の決定（2026-11-09）:** テストユーザーは DB に**残し**、`TEST_LOGIN_TOKEN` は**使うときだけ**入れて、終わったら**削除**します（平時は無効）。認証情報の原本は Infisical（`INFISICAL_PROJECT_ID_DEV` のプロジェクト、環境 `dev`）の `TEST_LOGIN_EMAIL` `TEST_LOGIN_PASSWORD` `TEST_LOGIN_TOKEN` です。JWT は保存せず、毎回サインインして取ります。

手順（運用者。Worker の Secret は本番への書き込みなので、エージェントは実行しない。ホストのターミナルで）:

1. 有効にする: `bash workers/api/test-login.sh on`（Infisical の値を `wrangler secret put` に標準入力で渡す。要 `wrangler login --device`）。
2. テストする: `infisical sm run --silent --projectId "$INFISICAL_PROJECT_ID_DEV" --env=dev -- python scripts/prod_smoke_chat.py --send`（ユーザーが無いときだけ `--create`）。
3. **終わったら無効にする:** `bash workers/api/test-login.sh off`（`TEST_LOGIN_TOKEN` を削除。入口は 400 に戻る）。`status` で Secret の名前を確かめられます。
4. テストユーザーを消すとき（復元できない）: 本番 DB の `user` `account` `session` の該当行を SQL で削除します。

**限界:** Workers のレート制限はメモリ上で、インスタンス間では効きません。総当たりへの備えは、長いパスワードと、テストの間だけ有効にすることに頼っています。
**検証の状況:** ローカル（2026-11-08）で、無効時は 400、有効時はトークン無し・違うトークン・別のメールが 403、正しい組み合わせで登録・ログイン・JWT 取得、その JWT をアプリ（`AUTH_MODE=required`）が受理することを確認しました。**本番でも確認しました（2026-11-09）:** 登録・ログイン・JWT 発行・`/chat` 200。
注意: Python 既定の User-Agent は Cloudflare に弾かれる（error code 1010。403 に見える）ので、スクリプトは名前つきの User-Agent を送ります。
