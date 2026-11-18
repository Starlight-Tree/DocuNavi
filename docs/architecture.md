# アーキテクチャ

質問は、入力ガード → LLM（必要なら資料の検索）→ 出力ガードの順に処理されます。API の仕様は [api.md](api.md)、環境変数は [configuration.md](configuration.md) にあります。

## 質問はどう処理されるか

`app/agents/graph.py` の LangGraph は、次の順に進みます。

```mermaid
flowchart LR
    start([START]) --> guard_input
    guard_input -->|拒否| finish([END])
    guard_input -->|通過| agent
    agent -->|ツール呼び出しあり| tools
    tools -->|検索結果| agent
    agent -->|ツール呼び出しなし| guard_output
    guard_output --> finish
```

- `guard_input` が入力を拒否したときは、LLM もツールも呼ばずに END へ進みます。
- `agent` は Gemini（キーが無ければモック）を呼びます。ツール呼び出しがあれば `tools`（資料の検索）へ進み、結果を持って `agent` に戻ります。
- `guard_output` が最終回答を検査して終了します。
- `POST /chat/stream` だけは、`agent` が LLM の応答をチャンクごとに受け（`stream_tokens` が真のとき）、逐次ガードを通した本文をカスタム
  ストリームへ流します。`/chat`（`ainvoke`）の経路は変わりません。ヘッジは「最初のチャンクが届いた試行」を採ります（`hedged_stream`）。

## どこに何があるか

| パス | 役割 |
|---|---|
| `app/main.py` | FastAPI アプリ（CORS、例外ハンドラ） |
| `app/api/routes.py` | `/health`、`/chat`、`/chat/stream`、入力検証、LLM・履歴の保存先のエラーの分類、切断時の取り消し |
| `app/api/ui_stream.py` | `/chat/stream` の応答（AI SDK の UI message stream）の組み立て |
| `app/history.py` | 会話履歴の保存先（`DB_BACKEND=pg` なら Postgres〔`app/pg.py`・`app/pg_saver.py`〕、空ならメモリ。旧経路の `d1` はテスト用に残してある）。接続の開閉 |
| `app/agents/graph.py` | LangGraph のグラフ、`SYSTEM_PROMPT`、モック LLM |
| `app/agents/guardrails.py` | 入出力ガードレール（[guardrails.md](guardrails.md)） |
| `app/agents/resilience.py` | LLM 呼び出しのヘッジ付き再試行（完了を待つ `hedged_call` と、最初のチャンクを待つ `hedged_stream`） |
| `app/agents/tools.py` | 資料の検索ツール（`search_documents`）。実体は `app/search/vector.py`（Postgres + pgvector の検索。失敗しても別の経路に戻らない） |
| `app/access.py`・`app/scope.py` | ロール別の資料グループ制御（`ACCESS_MODE`。[configuration.md](configuration.md) の「資料グループのアクセス制御」）。`Scope` は検索できるグループの範囲で、検索は渡し忘れを許さない |
| `app/search/` | 資料の検索。`pipeline.py`（入口。フラグでハイブリッド・リランカー・GraphRAG を重ねる）、`vector.py`（質問の埋め込みと pgvector のコサイン類似度検索、起動時の DB の検査。設計: [search-design.md](search-design.md)）、`keyword.py`（BM25）、`rerank.py`、`graph.py`、`types.py`（`Hit` と `VectorSearchError`） |
| `app/ingestion/` | PDF の取り込み（`python -m app.ingestion`）と埋め込み（`python -m app.ingestion.embed`）。資料ごとの「続けて聞く」の質問は `python -m app.ingestion.suggest`（既定は確認だけ。`--apply` で実 API を 1 資料 1 回呼び、`documents.suggested_questions` に保存。PDF が変わった資料だけ作り直す。2026-11-17 に本番の全 80 資料を作成済み）。取り込みの後（`sync`・`queue_worker`）に `--if-enabled` つきで呼ぶ組み込みを実装済み（2026-11-17。**本番に配備済みで有効**〔2026-11-18 に gcloud の読み取りで確認（A）: 取り込み Job 2 本 `docunavi-ingest-sync`・`docunavi-ingest-queue` は `SUGGEST_ENABLED=true` で、配信中のリビジョン `docunavi-00091-juj` と同じ画像。設定後に Job は一度も実行されておらず、Job 経由の生成は未確認〕。環境変数 `SUGGEST_ENABLED=true` のときだけ動く）。失敗は `documents.suggestions_failure` に残し、資料の側の失敗は 1 時間・6 時間待って 3 回で諦め、API 全体の障害は実行を止める。1 回の上限は 10 資料・10 回・300 秒、24 時間で 30 資料。PDF が入れ替わって再生成を待つ質問は、検索の SQL が隠す（`SUGGESTED_QUESTIONS_SQL`）。列は `db/migrations/0005_suggestion_failures.sql`（本番への適用は 2026-11-17 の記録。DB が Prisma の上限で読めず、2026-11-18 の再確認は未了）。資料の版（`documents.revision`）は、資料群のディレクトリ（R2 なら `<資料群>/`）に並べた `revisions.csv`（`source,revision` の 2 列）から取り込みが入れる（`revisions.py`。一覧があれば一覧が正で、載っていない資料は版なしに戻す。一覧が無ければ版に触れない。形が悪い一覧は何も保存せず終了コード 2。版だけの変更は UPDATE 1 本）。列は `db/migrations/0006_document_revision.sql`（**未適用**。本番は Prisma の上限が解けてから別の承認で。API の資料一覧・資料の詳細と、取り込み（`sync`・`queue_worker`）の SQL が `revision` 列を読み書きするので、**API と取り込み Job の配備より先に適用する**）。`corpus.py` は評価の BM25 基準線用。ページは通し番号（`page`）と PDF のページラベル（`page_label`）を分けて持つ（下の「ページの番号とラベル」） |
| `db/` | `migrations/`（SQL を番号順に。`migrate.py` が適用する。`0002` は `chunks.page_label` `page_end_label`）、`seeds/`、`local-init/` |
| `frontend/` | チャット画面（Next.js + React + TypeScript）。Cloudflare の Worker `docunavi-web`（OpenNext）が配信する（FastAPI は配信しない）。E2E もここ |
| `storage/pdf/<名前>/` | PDF の資料（取り込みの元。検索は Postgres を読む。ディレクトリ名は整理用。いまは zahard・cafe・guides） |
| `scripts/` | 評価の道具。`bm25_baseline.py` は、以前アプリが使った BM25（評価の基準線。アプリは使わない） |
| `tests/` | pytest（モックモード固定） |
| `ops/` | 運用・補助のサービス。`sse-relay/`（Go の SSE 中継）と `sse-fake-upstream/`（その負荷試験用の疑似の上流）、`mcp_search/`（MCP の検索。読み取り専用）と `mcp_probe/`（MCP のつなぎ方の検証用）、`budget_guard/`（予算が上限に届いたら課金を止める）と `budget_guard_disarm/`（その承認トークンの発行）、`r2_key_issuer/`（R2 のキーの発行・取り消し Job）と `r2_wire/`（R2 参照への切り替え Job）、`django-saas/`（Django の学習用の試作。製品には入っていない） |
| `docs/` `.claude/` | 文書（`docs/CLAUDE.md` に正本の表。決定の記録は `docs/decisions/`）と、Claude Code 用の規約（`CLAUDE.md` は領域ごと、`.claude/rules/` は領域別の規則、`.claude/skills/` は手順） |

会話履歴は `thread_id` ごとに保持します。DB を設定しない（`DATABASE_URL` が空）ときはプロセス内メモリ（`MemorySaver`）で、再起動で消え、複数ワーカー間では
共有されません。`DB_BACKEND=pg` と `DATABASE_URL` を設定すると Postgres に保存します（`app/history.py`、`app/pg_saver.py`。起動時に接続を開き、終了時に閉じる。
開けないときは、メモリへ切り替えず起動を失敗させる。[configuration.md](configuration.md) の「会話履歴の保存先」）。

## ページの番号とラベル

表紙・目次のある PDF では、何枚目か（通し番号）と、紙面の番号（ラベル）が違うので、チャンクは両方を持ちます（決定: [decisions/0010](decisions/0010-physical-page-number-and-display-label.md)）。

| | 列・項目 | 使い道 |
|---|---|---|
| 通し番号 | `chunks.page`・`page_end`（1 始まり） | PDF を開くリンク（`#page=N`）、評価の根拠ページの照合 |
| 表示ラベル | `chunks.page_label`・`page_end_label`（PDF の `/PageLabels`。「表紙」「i」「1」「A-1」など） | 画面の出典・チャンクの表示だけ |

- 取り込み（`pdf_parser._page_labels`）がラベルを読み、`chunker` がチャンクに付けます。ラベルが無い・壊れた PDF は `NULL`（表示は通し番号）で、取り込みは止まりません。
- 内容のハッシュにラベルは含めません。再取り込みすると、同じハッシュのチャンクも `UPDATE` でラベルが入ります（再埋め込みはしません）。
- 列は `NULL` 可の追加です。**配備は、マイグレーション（`db/migrate.py --apply`）を先に、Cloud Run と取り込みを後に。** 逆だと検索の SQL が列を名指しして 503 になります。本番の DB には、2026-11-15 に 0002 を適用済みです（Cloud Run の配備と再取り込みは、まだ）。
- 関係図（`app/relations/`）は、ラベルが数字でないページ（表紙・目次）を読みません。

## どこで動くか

本番環境（Cloud Run + Cloudflare Workers〔画面・認証〕+ Cloudflare R2 + Prisma Postgres + Gemini）およびローカル開発環境の構成と通信経路です。
**2026-11-08 から、本番のデータ（資料・チャンク・埋め込み・会話履歴・認証）は、Prisma Postgres（Free・東京。pgvector で検索）に置く。** FastAPI・MCP・取り込み／履歴削除の Job は `DATABASE_URL` で直接つなぐ。認証は、Worker `docunavi-api`（Better Auth）が Hyperdrive 経由で同じ Postgres を使う。
バックアップは、R2 への週 1 回の `pg_dump` です（[pg-backup.md](pg-backup.md)）。
本番の Cloud Run には `GOOGLE_GENAI_USE_VERTEXAI` の設定が無く（2026-10-31 に確認）、Vertex AI は使っていません。LiteLLM などの LLM ゲートウェイは通っていません
（LiteLLM gateway is removed and no longer used.）。Gemini CLI is not part of the docunavi runtime。

```mermaid
flowchart TB
    browser["ブラウザー"]

    subgraph cfweb["Cloudflare Workers（本番フロントエンド）"]
        frontend["Next.js（frontend/）<br/>https://docunavi-web.wataru-apps.workers.dev"]
    end

    subgraph gcp["GCP Cloud Run（本番バックエンド）"]
        sserelay["docunavi-sse-relay<br/>Go SSE 中継（:8080）"]
        api_prod["docunavi<br/>FastAPI + LangGraph（h2c）"]
        mcp["docunavi-mcp<br/>MCP 検索ツール"]
        jobs["Cloud Run Jobs<br/>history-cleanup / ingest-sync / ingest-queue"]
    end

    subgraph host["ローカル開発環境"]
        subgraph compose["docker compose"]
            c_front["frontend :3000<br/>（Next.js dev）"]
            c_api["docunavi-api<br/>（FastAPI。DB につながず、モックモード）"]
        end
        subgraph devc["開発コンテナ（devcontainer）"]
            venv[".venv（uvicorn :8000 / pytest）"]
            dev_tools["Claude Code<br/>Playwright"]
        end
    end

    subgraph cfauth["Cloudflare（認証用 Worker）"]
        authworker["Worker docunavi-api<br/>Better Auth（/api/auth/*）<br/>Hyperdrive 経由で Postgres へ"]
    end

    subgraph data["データ（Prisma Postgres）"]
        pg["Postgres + pgvector<br/>documents / chunks / 履歴 / 認証"]
    end

    subgraph cloudflare["Cloudflare R2（原本ストレージ）"]
        r2["docunavi-documents<br/>PDF 原本"]
    end

    subgraph llm["LLM（モデル生成・埋め込み）"]
        gemini["Gemini API<br/>主: gemini-3.1-flash-lite（コード既定）<br/>副: gemini-3.5-flash-lite<br/>埋め込み: gemini-embedding-2"]
    end

    %% 本番の通信経路
    browser -->|画面表示| frontend
    browser -->|POST /chat/stream| sserelay
    sserelay -->|h2c 中継| api_prod
    browser -->|GET /documents, POST /chat 等| api_prod
    browser -.->|Google ログイン<br/>（画面の Worker が /api/auth/* を中継）| authworker

    api_prod -->|DATABASE_URL| pg
    api_prod -->|JWKS 取得| authworker
    api_prod -->|原本取得| r2
    api_prod -->|推論・埋め込み| gemini

    authworker --> pg
    mcp -->|検索クエリ| pg
    jobs -->|定期バッチ| pg
    jobs -->|取り込み| r2

    %% 開発時の通信経路
    browser -.->|開発時| c_front
    c_front -.->|rewrites| c_api
    devc -.-> gemini
```

- 本番環境では、Cloud Run 上で複数のサービス（FastAPI 本体、Go SSE 中継、MCP 検索、各種バッチ Job）と Cloudflare の Worker `docunavi-web` 上の Next.js 画面が稼働し、Cloudflare の認証用 Worker（Better Auth）、Prisma Postgres（資料・チャンク・履歴・認証）、Cloudflare R2（PDF 原本・バックアップ）、Gemini API（主: gemini-3.1-flash-lite〔コード既定〕、副: gemini-3.5-flash-lite、埋め込み: gemini-embedding-2）と連携しています（デプロイ実績と運用手順は [deploy.md](deploy.md)）。
- ローカル開発でのアプリ実行は 3 通りです。ホストでの `docker compose up`（`docunavi-api`。DB につながず、モックモードで動く。ポートは公開せず、`docunavi-frontend`（next dev）の 3000 番経由で使う。
  8000 番は `docker-compose.api-port.yml` を重ねたときだけ）、開発コンテナの `.venv` での `uvicorn`（8000 番）、そしてローカルの Postgres と認証までそろえる標準構成（`scripts/dev-local.sh`。本番の DB には接続しない。
  [development.md](development.md) の「ローカルの標準構成」）です。開発コンテナには Docker が無いので、compose はホスト側で実行します。
  開発コンテナ自体も Compose ベースで（`.devcontainer/docker-compose.yml`）、2026-11-11 から、開いても `api` `frontend` は起動しません
  （[.devcontainer/README.md](../.devcontainer/README.md) の「開発コンテナの構成（Compose）」）。
- 検索（`chunks`）と会話履歴は、同じ Postgres（`DB_BACKEND=pg` と `DATABASE_URL`）です。本物の LLM では `DATABASE_URL` と `GEMINI_API_KEY` が必須で、
  無いとモックモード（キー未設定）か、起動の失敗になります（[search-design.md](search-design.md)）。
- 秘密情報の置き場所は、実行時の環境変数だけです。値は文書にも画像にも書きません。
  - `GEMINI_API_KEY`、`DATABASE_URL`: ホストのリポジトリ直下の `.env`（Git 管理外。雛形は `.env.example`）。compose が `env_file` で渡します。本番では Secret Manager を利用します。
  - `NEXT_PUBLIC_AUTH_ENABLED`、`NEXT_PUBLIC_AUTH_BASE_URL`: 画面（Worker `docunavi-web`）のビルド時に渡す公開用の値で、秘密ではありません。Cloud Run のイメージには入りません。
  - gh の読み取り専用トークン: 開発コンテナの名前付きボリューム（`~/.gh-config`）。gcloud のログインは残しません（[.devcontainer/README.md](../.devcontainer/README.md)）。

## 検証環境（AlloyDB Express）

**テストと検証のための別スタック `docunavi-gcp-*` です。** PostgreSQL 互換の AlloyDB Express と、本番とは異なる認証サービス・接続構成（Private Service Connect、SSE の中継なし）で、アプリが動くことを確かめます。本番（上の「どこで動くか」。Prisma Postgres）とは独立していて、本番は変わりません。

- **位置づけ:** この構成を本番に採用するかは未決定です。本番データの継続的な同期も、自動のフェイルオーバーも作っていません（データはダミーで、ローカルを正本に、同じマイグレーションと投入手順で反映します）。待機系（パッシブ）ではなく、検証環境として読んでください。
- **構成:** 画面の Worker `docunavi-gcp-web`、認証の Cloud Run `docunavi-gcp-auth`（Better Auth。画面の Worker が `/api/auth/*` を中継）、API の Cloud Run `docunavi-gcp`（FastAPI + LangGraph）、DB は AlloyDB Express（東京、Private Service Connect のみ。マイグレーション 0001〜0005）。接続は TLS（`verify-ca`）で、ユーザーは用途ごとに分けています。
- **SSE:** Go の中継を通さず、ブラウザーが API に直接つなぎます。Cloud Run を HTTP/2（h2c）にしたうえで、次を確認しました（2026-11-17）。
  - 逐次配信: 1.6 秒の応答が、7 個のチャンクに分かれて届く。
  - 切断: クライアントを切ると、LLM 呼び出しが取り消される（HTTP/1.1 のままだと、最後まで走る。本番 `docunavi` と同じ設定が要る）。
- **起動:** 20 分あけたあとの最初のアクセスでも、DB の応答は 0.3 秒ほどで、再開の遅れは見られませんでした。
- **未検証・制限:** PSC と AlloyDB の課金は Console での確認待ちです。クレジットの期限後に費用が変わるため、この構成を続けるかは別に判断します。アクセス制御は、`ACCESS_MODE` が `off`、`MEMBERSHIP_MODE` が `enforce`（`app_members` にいる人だけが使える）です（2026-11-18 に `docunavi-gcp-00004-dtp` で確認）。
- **画面の配備（2026-11-18）:** ホストで行います（開発コンテナには Cloudflare の資格情報が無い）。`frontend/deploy-web.sh` の既定値は本番なので使わず、環境変数を検証用に置き換えて `npm run build:cf` のあと `wrangler deploy --name docunavi-gcp-web` を実行します（`--name` で本番の `docunavi-web` には触れません）。`NEXT_PUBLIC_AUTH_ENABLED=1`、`NEXT_PUBLIC_API_BASE_URL` と `NEXT_PUBLIC_STREAM_BASE_URL` はどちらも Cloud Run `docunavi-gcp` の URL（中継なし）、`AUTH_PROXY_TARGET` は `docunavi-gcp-auth` の URL です。実行前に `wrangler whoami` でアカウントを確かめます。`frontend/node_modules` は開発コンテナと共有する Linux 用なので、`npm ci` はそこでせず、コピーしてビルドします。値は、配備済みの画面の中身から割り出した推定です（以前の配備の記録は無い）。配備後、`/`・`/api/auth/ok`・`/api/auth/get-session` が 200、ビルドに検証用 API の URL だけが入っていることを確認しました（2026-11-18、A）。ログイン後の画面で、会話の時刻表示が新しい表記になっていることを、ユーザーが確認しました（2026-11-18、A）。
- **埋め込みとおすすめ質問の投入（2026-11-18）:** Cloud Run Job `docunavi-gcp-ingest`（`--args` を `gcloud run jobs update` で変えて `execute --wait`）で、ダミー資料に付けました。経路は Vertex AI（`EMBEDDING_USE_VERTEXAI=true`、`global`、ADC）で、DB は検証用の AlloyDB だけです。埋め込みは 500 件ずつ 5 回で 2006 件（呼び出し 2006 回、概算 約 $0.05）、おすすめ質問（`gemini-3.1-flash-lite`）は 74 資料です。おすすめ質問は 6・7 回目に `ClientError (status=429)` で途中停止し（44 回・15 回で中断。保存はそれぞれ 43・14 件）、8 回目で残り 17 件が通りました（計 76 回の呼び出し。いずれも A）。429 の本文は設計上ログに出さず、Vertex 側の記録も無く、**原因は未確認**です。呼び出しは毎分約 16 回・1 回数百トークンで、このモデルの固定の上限（global の入力トークン 25,000,000／分。`gcloud beta quotas info list` で確認）には遠く、Vertex 側の一時的な混雑が疑わしいという推測にとどまります。`suggest` に待って再試行する処理はありません（429 は実行を止める設計）。Job の `--args` は、投入後も `suggest --apply` のままです。

```mermaid
flowchart LR
    B["ブラウザー"] --> W["Cloudflare Worker<br/>docunavi-gcp-web"]
    W -->|"/api/auth/*"| AU["Cloud Run<br/>docunavi-gcp-auth"]
    B -->|"/chat ほか（SSE も直接）"| A["Cloud Run<br/>docunavi-gcp（h2c）"]
    A --> G["Gemini"]
    A -->|PSC| DB[("AlloyDB Express")]
    AU -->|PSC| DB
```

## ログインの流れ

2026-11-08 から、認証は Better Auth（認証用 Worker `docunavi-api` の `/api/auth/*`。Google ログイン、15 分の JWT、JWKS）です。それ以前の Supabase Auth の流れは Git の履歴にあります。

`AUTH_MODE=required` のときの流れです（仕様は [configuration.md](configuration.md) の「ログイン（認証）」）。ブラウザーが Better Auth でログインして
アクセストークン（JWT）を受け取り、API にはそのトークンだけを送ります。バックエンドは、Better Auth が公開する鍵（`{AUTH_BASE_URL}/api/auth/jwks`）で署名を検証します。
`/api/auth/*` は、画面の Worker `docunavi-web` が認証用 Worker へ中継するので、Cookie は画面と同じオリジンに置かれます。PDF も、画面の Worker の `/api/documents/{id}/file` が、その Cookie から取った JWT で Cloud Run の API を呼んで中継します（iframe は Authorization を送れないため。保存名を資料名にできる。[ADR 0013](decisions/0013-pdf-via-same-origin-relay.md)。実装済み・配備は未実施）。

```mermaid
sequenceDiagram
    participant B as ブラウザー（frontend）
    participant W as 認証用 Worker（Better Auth）
    participant G as Google
    participant A as バックエンド（FastAPI）

    B->>W: Google でログイン（画面の Worker が中継）
    W->>G: 認証
    G-->>W: 本人確認の結果
    W-->>B: セッション → アクセストークン（JWT）
    B->>A: POST /chat/stream<br/>Authorization: Bearer トークン
    A->>W: JWKS の公開鍵（起動時に取得。10 分ごとに更新）
    A->>A: 署名・exp・iss・aud・sub を検証
    A-->>B: 回答（履歴のキーは「sub:thread_id」）
```

- **信頼の境界。** バックエンドが信じるのは、検証に通ったトークンの `sub` だけです。クライアントが送る `thread_id` や、画面の状態は信じません。
- **秘密と公開の区別。** 画面に渡すのは認証の有効化フラグと認証のオリジンだけです（公開してよい値）。Gemini のキーと DB の接続文字列は、バックエンドの環境変数にだけあります。
- **401 のとき。** 画面は、トークンの更新を 1 回試し、だめならログインを促す固定の文を出します。ログイン前の画面（`?tab=documents` など）は、ログイン後に元へ戻ります。
- **確認の状況。** Better Auth での確認は、2026-11-08 の本番の移行時のもので、この文書では独立に確かめていません。

## 画面はどう配信されるか

画面（`frontend/`、Next.js）は Cloudflare の Worker `docunavi-web`（OpenNext）が配信します。FastAPI は画面を配信せず、API だけを持ちます（2026-10-24 に、Vite の画面の配信を外した）。
ブラウザーは、Worker の画面から API を呼ぶ別オリジンの構成なので、API は `CORS_ORIGINS` に画面のオリジンを持ちます。
Cloud Run の `/` は 404 です。ローカルの開発では、Next.js の開発サーバーが `next.config.ts` の rewrites で API を同じオリジンに見せます。構成の決定は [target-architecture.md](target-architecture.md) です。
本番への画面（と、認証用の Worker `docunavi-api`）のデプロイは、`main` へのプッシュでは行われません。ユーザーが GitHub の Actions で手動の `.github/workflows/deploy.yml` を実行し、Environment の承認を経て出ます。

## ガードレールはどう働くか

ルールベース（LLM を使わない）で、入力は拒否と伏せ字、出力は `SYSTEM_PROMPT` の漏洩の検査（ストリーミング中は逐次）を行います。
典型的な手口を安く弾くためのもので、プロンプトインジェクションを完全に防ぐものではありません。詳細は [guardrails.md](guardrails.md) です。
