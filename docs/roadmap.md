# ロードマップ

順序と状態だけを置く（2026-11-18 に圧縮。設計・測定は各文書、完了の経緯は Git の履歴）。決定は [decisions/](decisions/README.md)。
目的は、DB を使うアプリへ育てることと、Cloudflare・R2・Cloud Run などを実際に動かして学ぶこと。日付つきの数字は、その日付のまま。

## 現状（2026-11-18）

- **構成:** 画面は Worker `docunavi-web`、認証は Worker `docunavi-api`（Better Auth）、API は Cloud Run の FastAPI、DB は Prisma Postgres `docunavi-prod`（Free・東京・pgvector）、PDF 原本は R2。[architecture.md](architecture.md)。
- **Prisma Free は上限で止まっている。来月まで放置する**（月 200,000 操作。止まることは利用者が受け入れ済み。[decisions/0012](decisions/0012-accept-prisma-free-limit-and-cut-operations.md)）。2026-11-15 の Console で 197,536 / 200,000（利用者の貼り付け、等級 A）。2026-11-18 の引き継ぎの記録では本番 API は 503（この文書では再確認していない）。更新は **2026-12-01**。それまで放置し、Prisma を呼ぶ確認は増やさない。**未確認:** 認証が止まり画面から使えなくなるか、超過時が課金か停止か。未追跡のローカルメモ `ops/HANDOFF_prisma_usage.md` は記録として引用しない（12-01 後に削除予定）。
- **本番 DB の件数**（2026-11-09、読み取り、等級 A）: `documents` 80、`chunks` 2,313（すべて埋め込み済み）、`user` 2 行（人数ではない）。
- **取り込み:** 同期は週 1 回（日曜 00:00 JST）。`docunavi-ingest-queue-every-5min` は一時停止。「続けて聞く」の質問の自動生成は配備済みで有効（[architecture.md](architecture.md)。Job 経由の実行は未確認）。
- **バックアップ:** 週 1 回（日曜 00:00 JST）、R2 へ。手順・規則・復元は [pg-backup.md](pg-backup.md)。失敗の通知（アラート 2 本）は作成済み。**未確認:** Prisma への復元（検証は別の pgvector まで）、期限削除（最初は 2026-11-22 ごろ）、通知の到達。実行されなかった週は通知できないので、毎週月曜に最新のキーの日付を見る。
- **配備の経路:** `deploy/<タグ名>` の push で Cloud Build（承認必須）が 0% で配備し、100% への切り替えと戻しは `scripts/preview.sh`（利用者）。Worker は手動の `deploy.yml`。[deploy.md](deploy.md)。**未確認:** GitHub のタグ保護（未設定）。
- **`TEST_LOGIN_TOKEN`:** 2026-11-15 の決定は「検証の間は ON を維持」、[testing.md](testing.md) は「使うときだけ入れて終わったら削除」（`scripts/prod_check.sh` が 2026-11-17 から行う）。**現在の状態は未確認。** 本番運用に戻る前に OFF を確認する。
- **監視設定:** ログベース指標 4 つ（`chat_requests_total` `chat_failures_total` `chat_total_ms` `chat_smalltalk_total`）とアラート 2 本を 2026-11-09 に作成（利用者の報告。[deploy.md](deploy.md) の「保留中の運用の準備」6）。**未確認:** 通知の到達、指標の値が増えること。
- **接続数:** プール上限は docunavi=2・docunavi-mcp=1（2026-11-09、利用者の決定。根拠と測定は [decisions/0011](decisions/0011-db-connection-pool-limits.md)）。
- **速度（2026-11-09）:** 時間の約 78% は回答の LLM、約 19% は検索。補完モデルの変更は費用が約 25〜27% 増えるので不採用。

## 日付待ち

| 日付 | 内容 | 場所 |
|---|---|---|
| 2026-11-22 | 履歴削除ジョブの初回の定期実行（03:00 JST、7 日、`--apply`）。DELETE 権限と結果は未確認 | [deploy.md](deploy.md) の「保留中の運用の準備」4 |
| 2026-11-24 以降 | 検証環境の VPC ピアリングの削除を再試行（それまで放置。引き継ぎの記録で、未確認） | [alloydb-test-env.md](alloydb-test-env.md) |
| 2026-12-01 | Prisma Free の更新。上の「止まっている」を見直す | [decisions/0012](decisions/0012-accept-prisma-free-limit-and-cut-operations.md) |
| 2026-12-02 | `playwright-auth` の Client Secret の期限切れ（期限前にローテーション） | [.claude/rules/secrets.md](../.claude/rules/secrets.md) |
| 2027-01 中 | Cloudflare の API トークンの作り直し（期限 2027-02-06） | 同上、[deploy.md](deploy.md) |
| 未定 | `budget-guard` の `DRY_RUN` を `false` に戻す時期 | 下の「予算ガード」 |

## 条件待ち・受動確認

- **条件待ち:** 利用者向けの SaaS 管理の要件（⑤）。署名付き URL は作らない方針（認証ヘッダー付きで取る）。
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
| 4c | ハイブリッド検索・リランカー・GraphRAG・引用 | 検索の 3 つは配備済み（下の「検索」）。引用（文末の `[n]` と根拠の語句の強調。[api.md](api.md)）は 2026-11-09 に配備。実 API の 5 問（各 1 回、等級 A）のうち 1 問で本文に無い期限が作られたため、原文に無い語句の引用は番号ごと画面から消す。**限界:** 本文の作り話は直らない。**未確認:** 消える挙動の本番での確認 |
| 5 | フロントの分離 | 済み。Next.js 化（2026-10-25）、Worker へ（2026-11-08） |

## 実装順序 ①〜⑧ の状態

| 番 | 項目 | 状態 |
|---|---|---|
| ① | Next.js 化 | 完了（2026-10-25） |
| ② | 本番での切断 → LLM 取り消し | 確認済み（2026-10-25、等級 A）。h2c の本番（`docunavi-00016-ces`）で停止ボタンを押すとログに取り消しが出る。HTTP/1.1 のリビジョンでは効かない |
| ③ | Cloudflare Worker（`api.` → Worker → Cloud Run） | 廃止（2026-11-09。[decisions/0009](decisions/0009-drop-custom-domain-and-worker-front.md)） |
| ④ | Go の SSE 中継 | 本番に配備済み（2026-10-25。`/chat/stream` だけ）。本文上限・レート制限・詰まった下流の切断を 2026-11-09 に配備し、認証なしの POST で 413・429 を確認（等級 A）。完了（利用者の判断、2026-11-09）。**本番に同時数の上限は適用しない**（[decisions/0005](decisions/0005-go-sse-relay-without-prod-limit.md)）。**未確認:** 100 本超の負荷、長い回答での逐次表示、2 台のときの数え方。詳細は [ops/sse-relay/README.md](../ops/sse-relay/README.md) |
| ⑤ | Django（SaaS 管理） | 学習用の試作 `ops/django-saas/`（本番には入れない）。本番は条件待ち。`docunavi-admin` は 2026-11-08 に廃止（[decisions/0003](decisions/0003-django-only-for-operator-admin.md)） |
| ⑥ | `--use-http2` + hypercorn（h2c） | 本番に適用済み。SIGTERM で終わらない問題は `python -m app.serve`（Dockerfile の CMD）で直し、2026-11-09 に配備（`app/serve.py`）。**未確認:** 本番での SIGTERM の挙動（約 6 秒で終わるか） |
| ⑦ | （欠番） | 廃止。番号は参照のために残す |
| ⑧ | ロール別 PDF グループ制御 | フェーズ 1〜3 は完了。本番は `ACCESS_MODE=enforce`（2026-10-26）。割り当ては `python -m app.access_admin`（既定は確認だけ、`--apply` で書く）。[access-control-plan.md](access-control-plan.md)、[configuration.md](configuration.md)。**確認（2026-11-09、等級 A、各 1 回）:** 本番 DB（読み取り専用）で `Scope.only({"cafe"})` は無関係な質問で 0 件、`cafe` の質問で `cafe` だけ。`cafe-staff` の `/chat/stream` は `zahard` の質問に出典 0 件。**限界:** テスト用ログインで、Google ログインの利用者ではない |

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
