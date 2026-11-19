# ロードマップ

順序と状態だけを置く（2026-11-18 に圧縮。設計・測定は各文書、完了の経緯は Git の履歴）。決定は [decisions/](decisions/README.md)。
目的は、DB を使うアプリへ育てることと、Cloudflare・R2・Cloud Run などを実際に動かして学ぶこと。日付つきの数字は、その日付のまま。

## 現状（2026-11-18）

- **構成:** 画面は Worker `docunavi-web`、認証は Worker `docunavi-api`（Better Auth）、API は Cloud Run の FastAPI、DB は Prisma Postgres `docunavi-prod`（Free・東京・pgvector）、PDF 原本は R2。[architecture.md](architecture.md)。
- **PDF の保存名を資料名にする中継**（[decisions/0013](decisions/0013-pdf-via-same-origin-relay.md)）: 実装済み（2026-11-18）。**手動の `deploy.yml` の実行（2026-11-18 21:47 JST）が `docunavi-web` を更新し、中継を含む**（2026-11-20 に GitHub Actions の実行記録とログで確認、等級 A。その後の巻き戻しと、ほかの経路での配備は未確認）。**未確認:** 本番での手動確認（Cloudflare 上で Cookie が付くか、保存名、`#page=N`、CPU・1102）。2026-11-19 の PDF.js の表示（[decisions/0014](decisions/0014-pdfjs-viewer.md)）と画面の変更は、最後の `deploy.yml` の実行が 2026-11-18 なので（`gh run list`、2026-11-20）、本番には出ていないとみられる（未確認）。
- **PDF ビューアの連続スクロール化**（2026-11-20。[decisions/0014](decisions/0014-pdfjs-viewer.md) の表示の続き）: 利用者の指摘（パネルが画面の下端に届かない、ページを下スクロールで進められない、保存がダウンロードのアイコンでない）を受け、全ページを縦に並べて連続スクロールする形へ（見えている範囲の前後 1 ページだけ描く。ツールバーは下部に浮かせ、ページ番号は入力できる。保存はダウンロードのアイコン）。利用者がモックを承認（2026-11-20）。**ローカルの確認（2026-11-20、等級 A）:** Vitest 719 件、型検査、ローカルの実 Chromium（1280px。下端まで届く、連続スクロール、ページ表示の追従、出典からの移動と強調、拡大縮小、暗い表示）、E2E 17 件が通った。**未確認:** Safari・Firefox、本番（**未配備**）、スマホ。
- **Prisma Free（月 200,000 操作）:** 2026-11-15 の Console で 197,536 / 200,000（利用者の貼り付け、等級 A）。2026-11-18 の引き継ぎの記録では本番 API は 503（この文書では再確認していない。リポジトリ内に裏づけは無い）。**現在の状態は未確認。** 止まることは利用者が受け入れ済み（[decisions/0012](decisions/0012-accept-prisma-free-limit-and-cut-operations.md)）で、更新の **2026-12-01** まで放置し、Prisma を呼ぶ確認は増やさない。**未確認:** 認証が止まり画面から使えなくなるか、超過時が課金か停止か。別スタックの検証環境（AlloyDB Express。本番の移行ではない）は [architecture.md](architecture.md#検証環境alloydb-express)。未追跡のローカルメモ `ops/HANDOFF_prisma_usage.md` は記録として引用しない（12-01 後に削除予定）。
- **本番 DB の件数**（2026-11-09、読み取り、等級 A）: `documents` 80、`chunks` 2,313（すべて埋め込み済み）、`user` 2 行（人数ではない）。
- **取り込み:** 同期は週 1 回（日曜 00:00 JST）。`docunavi-ingest-queue-every-5min` は一時停止の記録（[decisions/0012](decisions/0012-accept-prisma-free-limit-and-cut-operations.md)）。**Scheduler の設定**は、2026-11-20 にホストの Claude が読んだ（ホストの報告。コンテナの閲覧用の権限では読めない）: `docunavi-ingest-sync-hourly`（名前は毎時のまま）は cron `0 0 * * 0`（日曜のみ。JST は Job の最終実行からの推定）、`docunavi-ingest-queue-every-5min` は PAUSED（[configuration.md](configuration.md)）。「続けて聞く」の質問の自動生成は配備済みで有効（[architecture.md](architecture.md)。Job 経由の実行は未確認）。
- **バックアップ:** 週 1 回（日曜 00:00 JST）、R2 へ。手順・規則・復元は [pg-backup.md](pg-backup.md)。失敗の通知（アラート 2 本）は作成済みで有効、月次の保持のライフサイクル規則も入れ替え済み（2026-11-20、ホストの報告）。**未確認:** Prisma への復元（検証は別の pgvector まで）、期限削除（最初は 2026-11-22 ごろ）、通知の到達。実行されなかった週は通知できないので、毎週月曜に最新のキーの日付を見る。
- **配備の経路:** `deploy/<タグ名>` の push で Cloud Build（承認必須）が 0% で配備し、100% への切り替えと戻しは `scripts/preview.sh`（利用者）。Worker は手動の `deploy.yml`。[deploy.md](deploy.md)。**未確認:** GitHub のタグ保護（未設定）。
- **`TEST_LOGIN_TOKEN`:** 2026-11-15 の決定は「検証の間は ON を維持」、[testing.md](testing.md) は「使うときだけ入れて終わったら削除」（`scripts/prod_check.sh` が 2026-11-17 から行う）。**現在の状態は未確認。** 本番運用に戻る前に OFF を確認する。
- **監視設定:** ログベース指標 4 つ（`chat_requests_total` `chat_failures_total` `chat_total_ms` `chat_smalltalk_total`）とアラート 2 本を 2026-11-09 に作成（利用者の報告。[deploy.md](deploy.md) の「保留中の運用の準備」6）。**未確認:** 通知の到達、指標の値が増えること。
- **接続数:** プール上限は docunavi=2・docunavi-mcp=1（2026-11-09、利用者の決定。根拠と測定は [decisions/0011](decisions/0011-db-connection-pool-limits.md)）。
- **速度（2026-11-09）:** 時間の約 78% は回答の LLM、約 19% は検索。補完モデルの変更は費用が約 25〜27% 増えるので不採用。

## 日付待ち

| 日付 | 内容 | 場所 |
|---|---|---|
| 2026-11-22 | 履歴削除ジョブの、削除が当たりうる最初の定期実行（03:00 JST、7 日、`--apply`。`--apply` つきの定期実行は 2026-11-15 に 0 件で済んでいる）。DELETE 権限と結果は未確認 | [deploy.md](deploy.md) の「保留中の運用の準備」4 |
| 2026-11-24 以降 | 検証環境の VPC ピアリングの削除を再試行（それまで放置。引き継ぎの記録で、未確認） | [alloydb-test-env.md](alloydb-test-env.md) |
| 2026-12-01 | Prisma Free の更新。上の Prisma の行（現在の状態は未確認）を見直す | [decisions/0012](decisions/0012-accept-prisma-free-limit-and-cut-operations.md) |
| 2026-12-02 | `playwright-auth` の Client Secret の期限切れ（期限前にローテーション） | [.claude/rules/secrets.md](../.claude/rules/secrets.md) |
| 2027-01 中 | Cloudflare の API トークンの作り直し（期限 2027-02-06） | 同上、[deploy.md](deploy.md) |
| 未定 | `budget-guard` の `DRY_RUN` を `false` に戻す時期 | 下の「予算ガード」 |

## 条件待ち・受動確認

- **条件待ち:** 利用者向けの SaaS 管理の要件（⑤）。署名付き URL は作らない方針（認証ヘッダー付きで取る）。
- **判断待ち（未着手。2026-11-20 に利用者の指示で記録）:** 利用者が自分の API キーを持ち込める機能（BYOK）と、接続ごとの利用状況の画面。**今は着手しない。** 回答の生成がサーバー側なので、利用者のキーを API が受け取る・預かる設計になる。接続ごとの記録は、Prisma Free の操作数の予算（[decisions/0012](decisions/0012-accept-prisma-free-limit-and-cut-operations.md)）を使う。始める前に [target-architecture.md](target-architecture.md) を読み、利用者に確認する。
- **受動確認（起きたときにログで見る）:** 新しい PDF の取り込みの書き込み、Better Auth のトークン期限切れ後の更新、有料キーへの切り替えと 60 秒の保持・別モデルへの切り替え（ログ `app.agents.resilience`）、履歴削除で会話が実際に消えること（2026-10-25 に確認、2026-11-15 に `--apply` を戻した。上の 11-22）。

## 段階の状態（2026-10-13 の計画の番号のまま）

| 段階 | 内容 | 状態 |
|---|---|---|
| 0 | Cloud Run に 1 コンテナで配備 | 済み（2026-10-20）。`--max-instances`・予算アラート・閲覧用の ID も設定済み |
| 1 | Postgres と履歴の保存先 | 済み。2026-11-08 に Supabase から Prisma Postgres へ移した（DB は `DATABASE_URL` だけに依存させていた） |
| 2 | 認証 | 済み。2026-11-08 から本番は Better Auth |
| 3 | Cloudflare の前段（独自ドメイン・レート制限） | 廃止（2026-11-09。[decisions/0009](decisions/0009-drop-custom-domain-and-worker-front.md)） |
| 4a | R2 に原本を置き、取り込む | 済み（2026-10-21。`PDF_STORE=r2`）。資料は全員共通（アップロードは作らない。作る段で見直す） |
| 4b | 取り込みの自動化 | 済み（2026-10-26。R2 通知 → Queue → Cloud Run Job）。[r2-ingest-design.md](r2-ingest-design.md) |
| 4c | ハイブリッド検索・リランカー・GraphRAG・引用 | 検索の 3 つは配備済み（下の「検索」）。引用（文末の `[n]` と根拠の語句。[api.md](api.md)）は 2026-11-09 に配備（利用者が実行）。実 API の 5 問（各 1 回、等級 A）のうち 1 問で本文に無い期限が作られた。**限界:** 本文の作り話は直らない。**画面の変更（2026-11-16）:** 本文中の `[n]` `[n「語句」]` を消して見せず、出典は回答の下の「N 件のソース」から開く（`frontend/src/lib/citations.ts` の `stripCitations`。2026-11-18 の `deploy.yml` の実行に含まれる）。原文に無い語句の引用を番号ごと消す処理（`splitCitations`）は、本文の表示に使われなくなった（コードを検索して確認、2026-11-20）。**未確認:** 本番の画面で本文の引用が消えて見えること（2026-11-16 のコミットの記録は、ログイン後の画面を目視していないとしている。リポジトリ内に、その後の目視の記録は無い。2026-11-17 にホストが本番の画面を 1 回見て、本文に `[n]` が出ていないスクリーンショット 1 枚が、Git 管理外のローカルにある。生の文に `[n]` があったかは、これでは分からない）、PDF 上の根拠の強調（PDF.js への置き換えは未配備とみられる。[decisions/0014](decisions/0014-pdfjs-viewer.md)） |
| 5 | フロントの分離 | 済み。Next.js 化（2026-10-25）、Worker へ（2026-11-08） |

## 実装順序 ①〜⑧ の状態

| 番 | 項目 | 状態 |
|---|---|---|
| ① | Next.js 化 | 完了（2026-10-25） |
| ② | 本番での切断 → LLM 取り消し | 確認済み（2026-10-25、等級 A）。h2c の本番（`docunavi-00016-ces`）で停止ボタンを押すとログに取り消しが出る。HTTP/1.1 のリビジョンでは効かない |
| ③ | Cloudflare Worker（`api.` → Worker → Cloud Run） | 廃止（2026-11-09。[decisions/0009](decisions/0009-drop-custom-domain-and-worker-front.md)） |
| ④ | Go の SSE 中継 | 完了（利用者の判断、2026-11-09）。本番に配備済み（2026-10-25。`/chat/stream` だけ）。本文上限・レート制限・詰まった下流の切断は 2026-11-09 に配備し、本番の中継への認証なしの POST で 413・429 を確認した（各 1 回、等級 A。根拠はコミット b959801 の記録: 2 MiB の本文で 413、12 回目の連続 POST で 429、LLM は呼んでいない。`ops/sse-relay/README.md` と `decisions/0005` は「応答コードそのものは未確認」と書いていて、食い違っている）。**本番に同時数の上限は適用しない**（[decisions/0005](decisions/0005-go-sse-relay-without-prod-limit.md)）。**未確認:** 100 本超の負荷、長い回答での逐次表示、2 台のときの数え方。詳細は [ops/sse-relay/README.md](../ops/sse-relay/README.md) |
| ⑤ | Django（SaaS 管理） | 学習用の試作 `ops/django-saas/`（本番には入れない）。本番は条件待ち。`docunavi-admin` は 2026-11-08 に廃止（[decisions/0003](decisions/0003-django-only-for-operator-admin.md)） |
| ⑥ | `--use-http2` + hypercorn（h2c） | 本番に適用済み。SIGTERM で終わらない問題は `python -m app.serve`（Dockerfile の CMD）で直し、2026-11-09 に配備（`app/serve.py`）。**未確認:** 本番での SIGTERM の挙動（約 6 秒で終わるか） |
| ⑦ | （欠番） | 廃止。番号は参照のために残す |
| ⑧ | ロール別 PDF グループ制御 | フェーズ 1〜3 は完了。本番は 2026-10-26 から `ACCESS_MODE=enforce`（コミット dae006b の記録による。切り替えの操作そのものの記録は、この文書からは確認できない）。2026-11-20 に、トラフィック 100% のリビジョン `docunavi-00091-juj` の環境変数でも `ACCESS_MODE=enforce` を確認した（gcloud の読み取り、等級 A。[configuration.md](configuration.md)）。割り当ては `python -m app.access_admin`（既定は確認だけ、`--apply` で書く）。2026-11-09 に本番で範囲の確認を各 1 回行った（テスト用ログインで、Google ログインの利用者ではない）。結果と限界は [decisions/0002](decisions/0002-role-based-collection-access.md) の「追記（2026-11-20）」。計画は [access-control-plan.md](access-control-plan.md)、仕様は [configuration.md](configuration.md) |

ほかに本番にあるもの: MCP `docunavi-mcp`（[mcp-tool-design.md](mcp-tool-design.md)）、別モデルへのフォールバック（実際に切り替わった回は未見）、有料キーへの切り替え（本物の 429 は未見）、予算ガード。

## 検索（設計は [search-design.md](search-design.md)）

- ハイブリッド（BM25 + RRF）・リランカー・GraphRAG は、3 つとも本番で既定 on（2026-11-01。[0007](decisions/0007-graph-rag-default-on.md)）。MCP はベクトル検索のまま。
- 実 API での比較（2026-11-01、64 問、等級 A、`evals/results/pipeline_compare.json`）: 根拠が上位 3 件に入る数は、ベクトルのみ 61、ハイブリッド 61、+ リランカー 63、3 つとも on 63。差は 1〜2 問で、型番を問う質問だけ。
- **限界・未確認:** RRF の重みとリランカーの配合は結果を見る前に固定した値のまま。GraphRAG は本番の資料で辺が 0 本で、効果は評価専用の資料でしか測っていない。チャンクが数千を超えたら、DB の全文索引への移行を測る。

## 予算ガード（2026-11-09 の読み取り。現在値は未確認）

- 月 ¥3,000。`budget-guard` と `budget-guard-disarm` は `DRY_RUN=true`（等級 A）。**この間は、上限に届いても課金を外さない。** 過去に `false` で外れた記録がある（2026-10-31〜11-01）。`false` に戻す時期は未決。条件と確認は [deploy.md](deploy.md) の「保留中の運用の準備」5。`false` への切り替えは、課金の再開が実際に確認できるまで行わない。
- 未確認: 前払い ¥800 が費用に数えられるか、発動時の費用の内訳。

## 方針

- 1 段ずつ足し、毎回「動く状態」を保つ（`pytest`・`npm test`・`docker compose up`）。先回りしてリファクタしない。
- 設定が空なら従来どおり動く（モックモード）。DB は普通の Postgres + pgvector で、`DATABASE_URL` だけに依存する。

## 今はやらないこと

- `app/agents/graph.py` の分割と、`app/api/routes.py` の LLM エラー分類の分離（分ける理由が出るまで保留）。
- 環境変数の読み取りの集約（settings）。変数群がさらに増えたときに行う。
