# ロードマップ

2026-10-13 時点の拡張計画です（段階 1・4 は 2026-10-14 に具体化しました）。DB を使うアプリへ育てることと、Cloudflare・R2・Vercel などの構成を実際に動かして学ぶことが目的です。
計画なので、段階に入るたびに見直します。

## 方針

- **1 段ずつ足し、毎回「動く状態」を保つ。** 各段階の終わりに `pytest`・`npm test`・`docker compose up` が通ることを確かめてから次へ進む。
- **先回りしてリファクタしない。** 各段階の直前に要る最小の変更だけを、その段階で行う（下の「先に要る変更」）。
- **「設定が空なら従来どおり動く」を保つ。** モックモードと同じ流儀で、`DATABASE_URL` が空なら今の `MemorySaver` のまま動かす。
- **構成は変えない。** monorepo のまま `app/`・`frontend/`・`storage/chunk/` で進める。足すモジュールは必要になった時に作る（空のディレクトリは作らない）。
- **DB は「普通の Postgres + pgvector」として使う。** `DATABASE_URL` だけに依存し、Supabase の SDK や独自 API を使わない（後から Cloud SQL などへ移せるように）。例外はキーワード検索の索引（段階 4c）で、検索は 1 か所に閉じ込める。
- **学習用の構成であって、必須の構成ではない。** 各段階で「なぜ入れたか」を一言残し、後で取捨選択できるようにする。

## 段階

| 順 | 追加するもの | 学べること | 前提・注意 |
|---|---|---|---|
| 0 | Cloud Run に 1 コンテナでデプロイ（[deploy.md](deploy.md)） | コンテナの配備、シークレット、ログ、予算アラート、権限の分離（閲覧用・デプロイ用の SA） | **課金アカウントが前提**（2026-10-13 時点で未設定）。最初は認証必須で配備し、`--max-instances` と予算アラートを先に設定する |
| 1 | Postgres（第一候補は Supabase。Cloud SQL でも可）と、履歴の保存先の差し替え | 接続プール、マイグレーション、状態の外出し | 現在の `MemorySaver` は、Cloud Run では再起動や複数インスタンスで履歴が失われる。**段階 0 を待たずに進められる**（Supabase の無料枠は GCP の課金が不要。履歴が残る効果は、デプロイしたときに出る） |
| 2 | 認証 | JWT の検証、会話と利用者の紐づけ | **R2 のアップロードより先。** 無認証だと誰でも書き込める |
| 3 | Cloudflare（DNS・独自ドメイン・レート制限） | DNS、プロキシ、WAF | 自分のドメインが要る。ドメインが無ければ後回しでよい。順序はどこでもよい |
| 4a | R2 に原本を置き、手動で取り込む（抽出 → チャンク化 → 埋め込み → pgvector へ upsert） | S3 互換 API、取り込みの冪等性、pgvector | 認証が前提。アップロード資料を利用者ごとに分けるか、全員共通かを先に決める。`page`・`source_path` などは取り込み時にしか取れないので、4a で持たせる |
| 4b | 自動化（R2 のイベント通知 → Queue → Cloud Run の取り込み） | イベント駆動、キュー、リトライ | 取り込みはチャット用サービスと分ける。push と pull は、遅延と認証の手間で選ぶ（下の「段階 4 の設計」） |
| 4c | ハイブリッド検索（日本語の全文索引）、引用、署名付き URL | 検索の品質、参照元の返し方 | 資料が増え、ベクトル検索だけでは取りこぼす質問が出てから。日本語の索引は DB ごとに違う |
| 5 | Vercel でフロントを分離 | CORS、クロスオリジンの認証、環境ごとの API の向き先 | いちばん手間がかかるので最後。Vite のまま静的配信か、Next.js 化するかを先に決める |

Supabase と Cloud SQL の選び方の目安:

| | Supabase | Cloud SQL (Postgres) |
|---|---|---|
| 向くとき | 認証も欲しい、小さく始めたい | GCP 内で完結させたい、IAM で統一したい |
| 注意 | 無料枠はアクセスが無いと一時停止される。リージョンを Cloud Run に合わせる | 常時起動の課金が出る |

料金と無料枠は変わるので、決める前に公式で確認する。

第一候補は Supabase です（学習目的に合い、認証と Storage が同居し、小さく始められる）。無料枠は一時停止するので、デモ以外で使い続けるなら有料プランか Cloud SQL にします。アプリは `DATABASE_URL` だけに依存させるので、後から移せます。最終的な決定は、段階 1 に入るときに行います。

## すでにある継ぎ目

何もしなくてよいもの。

- API の向き先: `frontend/src/api/chat.ts` の `VITE_API_BASE_URL`（空なら同一オリジン）
- CORS: `app/main.py` の `CORS_ORIGINS`（既定は `*`。公開時は実ドメインに絞る）
- フロントが無くても API は動く（`app/frontend.py`）。Vercel に分離しても壊れない
- Cloud Run 向けの作り: `$PORT`、非 root、SIGTERM を直接受ける起動、Vertex AI の ADC

## 先に要る変更

各段階の直前に行う。今はやらない。

| 段階 | 変更 | 根拠 |
|---|---|---|
| 1 | **済み。** `build_graph` に `checkpointer` 引数を足した（既定は `MemorySaver` のまま）。DB 版の保存先は、これに渡す | 引数の追加なので、既存の呼び出しとテストは変わらない。`tests/test_checkpointer_injection.py` |
| 1 | DB 版の checkpointer は接続プールを起動時に開いて終了時に閉じる。`app/main.py` に lifespan を足し、グラフの生成（`app/api/routes.py` の `get_graph`、同期の `lru_cache`）をそこに合わせる | 現状は起動・終了のフックが無い |
| 1 | 接続は、Supabase の session mode（ポート 5432。IPv4 でも届き、prepared statement が使える）から始める。次を確認する: `--max-instances` × プールサイズが接続数の上限に収まるか、開発コンテナと Cloud Run から届くか。transaction mode（6543）は prepared statement が使えない | checkpointer を差し替えるのは段階 1。直接接続は既定で IPv6（IPv4 は有料のアドオン） |
| 1・4b | 秘密情報の置き場所を決める。Supabase の `DATABASE_URL` と Cloudflare の API トークンは Secret Manager に置く。ローカルは `.env`（開発用には、権限を絞った DB ロールを使う） | ベンダーが増えると秘密も増える。開発コンテナには読み取り中心の権限だけを置く（[.devcontainer/README.md](../.devcontainer/README.md)） |
| 2 | `thread_id` は形式の検証しかなく、知っていれば誰でも続きを書ける。履歴が DB に残るので、認証後は利用者と紐づける | 認証が無い今は、UUID の推測困難さだけが守り |
| 4a | コーパスの読み込み（`app/agents/tools.py` の `CHUNK_DIR` と無期限の `lru_cache`）を差し替え可能にし、内容が変わったらキャッシュを捨てる。`search_chunks` の戻り値 `(ファイル名, 本文)` はそのまま使える | アップロードで資料が増減する |
| 3 つ目の外部サービスを入れる前 | 環境変数の読み取りが `graph.py`・`routes.py`・`main.py` に散らばっている。設定を 1 か所（settings）に集約する | 変数群（DB・R2・認証）が増える |
| 5 | Dockerfile の frontend ビルド段が不要になる（ビルド引数か、Dockerfile の分割）。CI の Docker ビルドの確認も合わせる | フロントを別の場所から配信する |

## 段階 4 の設計（2026-10-14）

取り込みの構成案です。4a で手動のものを作り、4b で自動化します。

```text
R2 のイベント通知 → Cloudflare Queue → Cloud Run の取り込み（サービスまたはジョブ）
  → 抽出（必要なら PDF 変換）→ チャンク化 → 埋め込み → Postgres + pgvector に upsert
FastAPI + LangGraph → 検索 → 回答 + 参照元（source_path から署名付き URL）
```

- **取り込みはチャット用サービスと分ける。** 抽出や埋め込みは重く、同じサービスに入れると、応答の遅延と「容量の注意」（`--concurrency`）の計算を崩す。
- **Cloudflare 側は薄い中継にする。** Worker のメモリは 128 MB（isolate ごと）で、queue consumer の CPU 時間も限られる。PDF の抽出や埋め込みは Cloud Run で行う。
- **冪等にする。** イベントは重複して届きうる。`documents` に内容のハッシュと状態を持たせ、`chunks` は `UNIQUE (document_id, chunk_index)` で upsert する。
- **参照元は DB のデータとして持つ。** 回答時に `source_path` から署名付き URL を作る。LLM に URL を書かせない。

データモデルの出発点:

| テーブル | 列 |
|---|---|
| `documents` | `id`, `source_path`, `content_hash`, `status`, `created_at`, `updated_at` |
| `chunks` | `id`, `document_id`（`ON DELETE CASCADE`）, `chunk_index`, `page`, `content`, `embedding vector(N)`, `embedding_model`, `embedding_dimensions`, `created_at` |

- `vector(N)` は、索引を作るために次元を型で固定する。モデルを変えて次元が変わると、列かテーブルの作り直しになるので、`embedding_model` と `embedding_dimensions` を持たせる。
- `tsvector` 列は入れない（次の項）。

**日本語の全文検索（4c）。** Postgres 標準の全文検索（`tsvector`）は英数字の言語向けで、日本語のキーワード検索はほぼ効かない。

| 方法 | Supabase | Cloud SQL |
|---|---|---|
| PGroonga | 使える | 見つからなかった |
| pg_bigm（2-gram） | 未確認 | 使える（`cloudsql.enable_pg_bigm`） |
| アプリ側で分かち書き → `simple` に格納 | 使える | 使える（辞書の依存が増える。未検証） |

索引は `content` 列に後から作れるので、再取り込みは要らない。検索は 1 つのモジュール（または SQL 関数）に閉じ込め、索引の違いを吸収する。

**R2 から Cloud Run への受け渡し（4b）。**

| 方式 | 内容 | 引っかかる点 |
|---|---|---|
| push | Worker が Cloud Run を HTTP で呼ぶ | Cloud Run を認証必須にしているので、Worker から Google の ID トークンを作る手間が出る |
| pull | Cloud Run のジョブが Queue を HTTP で引く | 起こす係として Cloud Scheduler が要り、遅延が出る（最短 1 分間隔の見込み）。Cloudflare の API トークンを Secret Manager に置く |

どちらにするかは 4b で試して決める。今の仮説は pull（認証必須の方針を保てる）。

**確認の状況（2026-10-14）。**

- 確認した（公式ドキュメント）: R2 のイベント通知は Queue に届き、consumer Worker か HTTP pull で読める。Supabase の接続方式（direct / session / transaction）の違い。Supabase の PGroonga。Cloud SQL の pgvector と pg_bigm。
- 未確認: Supabase での pg_bigm、Cloud Scheduler の最短間隔、接続数の上限、IPv6 が開発コンテナと Cloud Run から届くか、PDF 変換（LibreOffice など）をこの環境で動かせるか、Supabase の接続プーラーと LangGraph の Postgres checkpointer の相性。

## 公開前の守り（段階 0 から）

`/chat` は無認証で LLM を呼び、遅いときは 1 リクエストあたり最大 `LLM_MAX_ATTEMPTS` 本の試行が飛びます。公開すると、他人にトークンを使われる経路になります。
コードではなく運用設定で、最初のデプロイから行います。

- `--max-instances` で全体の上限を決める。
- `--concurrency` は、`同時実行数 × LLM_MAX_ATTEMPTS` が SDK の接続プール（既定 100）に収まるようにする（[configuration.md](configuration.md) の「容量の注意」）。
- 予算アラートを設定する（Console の「お支払い → 予算とアラート」）。
- `CORS_ORIGINS` を `*` から実ドメインに絞る。
- 最初は `--no-allow-unauthenticated` で配備し、`gcloud run services proxy` で確かめる。

## 今はやらないこと

- `app/agents/graph.py`（LLM 設定・モック・グラフが同居）の分割。テストが `build_graph`・`create_llm` などをモジュールから直接 import しているため、移動はテストの変更を伴う。分割する理由（複数エージェント化など）が出るまで保留する。
- `app/api/routes.py` の LLM エラー分類の分離。同じ理由で保留する。
- settings の集約やディレクトリの新設の先回り。上の表のとおり、必要になった段階で行う。
- 日本語の全文索引（PGroonga / pg_bigm）とハイブリッド検索を、4c より前に入れること。資料が増えて取りこぼしが出るまで、検索の品質は足さない。
- 取り込みの自動化（4b）を、手動の取り込み（4a）より先に作ること。冪等性とデータモデルを、手動で固めてから自動化する。
