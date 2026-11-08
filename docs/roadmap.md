# ロードマップ

現状・未了・次の順序だけを置く（2026-11-09 に全面改訂）。完了済みの経緯は Git の履歴にある。重要な決定は [decisions/](decisions/README.md)、設計と測定は各文書（下の表のリンク）。
目的は、DB を使うアプリへ育てることと、Cloudflare・R2・Cloud Run などの構成を実際に動かして学ぶこと。計画なので、段階に入るたびに見直す。

## 現状（2026-11-09）

- **構成:** 画面は Cloudflare の Worker `docunavi-web`（`frontend`）、認証は Worker `docunavi-api`（Better Auth、Google ログイン。Hyperdrive 経由で同じ Postgres）、API は Cloud Run の FastAPI。DB は Prisma Postgres `docunavi-prod`（Free・東京・pgvector）。PDF 原本は R2。D1・Vectorize・Vercel・Supabase は撤去済み（2026-11-08）。構成は [architecture.md](architecture.md)、移行の経緯は [cloudflare-migration.md](cloudflare-migration.md)。
- **本番 DB の件数**（2026-11-09、読み取りの SELECT、証拠等級 A）: `documents` 80 行、`chunks` 2,313 行（すべて埋め込み済み）、`user` 2 行、`session` 2 行（行数であり、利用者の人数ではない）。
- **バックアップ** `docunavi-pg-backup-daily`（毎日 03:30 JST、R2 `docunavi-backups`、手順は [pg-backup.md](pg-backup.md)）:
  - **設定の確認は完了**（2026-11-09）: 定時実行、R2 への保存、削除規則 `delete-after-14d`（有効。Cloudflare の API で確認）。初回の定時実行は成功した（実行 `docunavi-pg-backup-4jndj`、53 秒、9,479,225 バイト、18 表、終了コード 0。Job の実行履歴とログで確認。Scheduler 自体の設定は閲覧用の権限では読めず、実行履歴で確認した）。
  - **未確認（上の設定確認とは別）:** Prisma への復元（検証は別の pgvector への復元まで）、規則で実際に期限削除されること（最初の期限は 2026-11-22 ごろ）、失敗の通知（ログのみ）。
- **Prisma Free の使用量**（利用者の報告）: 39.1K / 200K 操作、ストレージ表示 0.0 / 2.0 GiB、請求 $0.00。プロジェクト別の表示との不一致の理由は未確認。上限の 200,000 操作/月に届くと、次の月まで読み書きが止まる（データは残る。利用者が受け入れた、2026-11-08）。
- **試験用の DB を削除した**（2026-11-09）: 本番の前に作った Prisma のプロジェクト `2026-11-08T00:20:12.134Z`（資料 80・チャンク 2,313・利用者 0 行）。Hyperdrive の接続先が `docunavi-prod` であることは、Console では確かめていない（本番 DB に `user`・`session` の行があることからの推定）。
- **速度の改善**（2026-11-09、利用者が一段落とした）: 検索の並列化 `546f755` は push 済みで CI は成功（実行 42515411791）。効果はローカルの pgvector で約 1 ms、本番の時間は未測定。補完モデルの変更（`gemini-3.5-flash-lite`）は、費用が約 25〜27% 増えるため採用しなかった。補完だけの思考レベルを下げる案（案 6）は、効果がなく廃棄した。トレース上の内訳は、回答の LLM が約 78%、検索が約 19%、補完が 0.7〜1.25 秒（追質問のみ）。`CHAT_SMALLTALK_SKIP` は off のまま。
- **`TEST_LOGIN_TOKEN`（決定済み）:** 本番 Worker `docunavi-api` では、検証の間は ON を維持する（利用者の決定）。承認待ちではない。検証が一段落してから OFF にするかを判断し、本番運用に戻る前に OFF を確認する。

## 現在のフェーズの必須残作業

- **監視設定（ログベース指標とアラート）:** 何も作っていない。構造化ログ（`fd44269`）の本番配備を待たず、設定差分（フィルタ案と通知先を含む）を提示し、承認を得てから適用する。フィルタ案は 2 つ: `event="chat_metrics"` と `smalltalk=true` で数える案と、スキップ時だけ出す専用のログを足す案。**未確認:** 構造化ログの配備後に、Cloud Run のログが `jsonPayload` になるか。
- **接続数の見積もり:** Prisma Free の接続上限（プール 10・直接 10）に対して、`--max-instances` × `DATABASE_POOL_MAX_SIZE` が収まるかは未確認。

## 条件待ち・将来対応・受動確認・別の運用確認（現在のフェーズの必須ではない）

- **条件待ち:** 独自ドメイン（段階 3、③）、利用者向けの SaaS 管理の要件（⑤）。詳細は下の表。
- **将来対応:** 引用・署名付き URL（段階 4c）。履歴削除ジョブは `--apply` なしで履歴が消えず、戻す時期は未決。`budget-guard` の `DRY_RUN` を `false` に戻す時期は未決（下の「予算ガード」）。④ ⑥ ⑧ と検索の未確認・限界は、下の各表と節。
- **受動確認（起きたときに見る）:** 新しい PDF を取り込んだときの Postgres への書き込み、Better Auth のトークン期限切れ後の更新（どちらも未確認）。バックアップの削除規則 `delete-after-14d` で実際に期限削除されること（最初の期限は 2026-11-22 ごろ。期限が来てから R2 を見る。それまで現在のフェーズは開けておかない）。ほかは下の「受動確認」の表。
- **別の運用確認タスク（今回の必須とは合意していない。完了条件に入れない）:** バックアップの Prisma への復元（検証は別の pgvector への復元まで）、バックアップの失敗の通知（ログのみ）。

## 段階の状態（2026-10-13 の計画の番号のまま）

| 段階 | 内容 | 状態 |
|---|---|---|
| 0 | Cloud Run に 1 コンテナで配備（[deploy.md](deploy.md)） | 済み（2026-10-20）。`--max-instances`・予算アラート・閲覧用の ID（`docunavi-viewer`）も設定済み |
| 1 | Postgres と履歴の保存先 | 済み。2026-11-08 に Supabase から Prisma Postgres へ移した（DB は `DATABASE_URL` だけに依存させていたので移せた） |
| 2 | 認証 | 済み。2026-11-08 から本番は Better Auth |
| 3 | Cloudflare の前段（独自ドメイン・レート制限） | 未実施。独自ドメインの取得待ち（利用者の決定）。ドメインなしで、画面は Worker で配信済み |
| 4a | R2 に原本を置き、取り込む | 済み（2026-10-21。本番は `PDF_STORE=r2`）。資料は全員共通（利用者のアップロードは作らない。作る段で見直す） |
| 4b | 取り込みの自動化 | 済み（2026-10-26。R2 のイベント通知 → Queue → Cloud Run の Job、毎時の突き合わせ）。設計は [r2-ingest-design.md](r2-ingest-design.md) |
| 4c | ハイブリッド検索・リランカー・GraphRAG・引用 | 検索の 3 つは実装・配備済み（2026-11-01、下の「検索」）。引用・署名付き URL は未着手 |
| 5 | フロントの分離 | 済み。Next.js 化（2026-10-25）、Vercel から Worker `docunavi-web` へ（2026-11-08） |

## 実装順序 ①〜⑧ の状態

| 番 | 項目 | 状態 |
|---|---|---|
| ① | Next.js 化 | 完了（2026-10-25、利用者の判断） |
| ② | 本番での切断 → LLM 取り消し | 確認済み（2026-10-25、証拠等級 A）。h2c の本番で、停止ボタンを押すとログに取り消しが出る。HTTP/1.1 のリビジョンに戻すと効かない |
| ③ | Cloudflare Worker（`api.` → Worker → Cloud Run） | 条件待ち（独自ドメイン） |
| ④ | Go の SSE 中継 | 本番に配備済み（2026-10-25。`docunavi-sse-relay`、画面の `/chat/stream` だけが通る）。100 同時接続の試験は済み。**本番の中継に上限は適用しない**（利用者の決定。[decisions/0005](decisions/0005-go-sse-relay-without-prod-limit.md)）。未確認: 中継自身の `: ping`、負荷・本文サイズの上限、レート制限。[ops/sse-relay/README.md](../ops/sse-relay/README.md) |
| ⑤ | Django（SaaS 管理） | 学習用の試作 `ops/django-saas/`（本番には入れない）。本番の SaaS 管理としては条件待ち（利用者向けの要件が固まったとき）。運用者向けの `docunavi-admin` は 2026-11-08 に廃止。[decisions/0003](decisions/0003-django-only-for-operator-admin.md) |
| ⑥ | `--use-http2` + hypercorn（h2c） | 本番に適用済み。既知の問題: 切断の後、hypercorn が SIGTERM に応じないことがある（Cloud Run は約 10 秒で SIGKILL。調べていない） |
| ⑦ | Claude Code 用の Vercel プラグイン | Vercel の廃止（2026-11-08）で不要。入れない（[dev-environment.md](../.claude/rules/dev-environment.md)） |
| ⑧ | ロール別 PDF グループ制御 | フェーズ 1〜3 は完了。本番は `ACCESS_MODE=enforce`（2026-10-26）。ロールの割り当ては SQL（専用の CLI は未作成）。計画は [access-control-plan.md](access-control-plan.md)、仕様は [configuration.md](configuration.md)。**未確認:** `cafe` 以外を返さないこと（実際の埋め込みが要る） |

ほかに本番にあるもの: MCP `docunavi-mcp`（読み取り専用の `search_documents`。[mcp-tool-design.md](mcp-tool-design.md)）、別モデルへのフォールバック（`gemini-3.5-flash-lite`、2026-10-21。実際に切り替わった回は未見）、有料キーへの切り替え（配備済み。本物の 429 は未見）、予算ガード（下）。

## 検索（2026-11-01 に実装・配備。設計は [search-design.md](search-design.md)）

- ハイブリッド（アプリ内 BM25 + RRF）、リランカー（語の一致）、GraphRAG（「第N条」の参照先を追加の文脈に）。3 つとも既定 on（GraphRAG は 2026-11-01 に on へ。[0007](decisions/0007-graph-rag-default-on.md)）。環境変数は [configuration.md](configuration.md)。MCP はベクトル検索のまま。
- 実 API での比較（2026-11-01、答えのある 64 問、等級 A。結果は `evals/results/pipeline_compare.json`）: 根拠が上位 3 件に入る数は、ベクトルのみ 61、ハイブリッド 61、ハイブリッド + リランカー 63、3 つとも on 63（k=5 は 62 / 62 / 64 / 64）。差は 1〜2 問で、exact（型番を問う質問）だけに出た。
- **限界・未確認:** RRF の重みとリランカーの配合は、結果を見る前に固定した値のまま（調整するときは別の質問で）。GraphRAG は本番の資料（2026-11-01 時点）で辺が 0 本で、効果は評価専用の資料でしか測っていない。検索のベクトル側は Python で再実装して比べており、本番の SQL は通していない。資料の写しの更新間隔は、本番で 600 秒（`SEARCH_CORPUS_TTL_SECONDS`）。チャンクが数千を超えたら、DB の全文索引への移行を測る。汎用の実体グラフ（LLM で抽出して保存）は未着手。

## 予算ガード（2026-11-01 時点。現在値は再確認していない）

- 月 ¥3,000（50・90・100%、クレジットは差引前、通知先 `budget-alerts`。利用者の報告）。
- `budget-guard` 本体は `DRY_RUN=true`（リビジョン `budget-guard-00004-b7r` を読み取りで確認、等級 A）。**この間は、上限に届いても課金を外さない。** 2026-10-31 14:24 UTC に実際に課金が外れた記録がある（`DRY_RUN=false` のときのログ）。再紐付けの 20 分後の 2026-11-01 00:30:59 UTC にも再発した（原因は未確認）。`false` に戻す時期は未決（原因の特定が先）。
- `budget-guard-disarm`（承認して課金を戻す `POST /disarm`、`ops/budget_guard_disarm/`）は、`imagen-app-2026` に配備済みで `DRY_RUN=true`。動作確認は利用者の報告。`false` への切り替えは、課金の再開が実際に確認できるまで行わない。
- 未確認: 前払い ¥800 が費用に数えられるか、発動時の費用の内訳。

## 受動確認（起きたときにログで見る。能動的には確認しない）

| 項目 | 起きるとき | 見る場所 |
|---|---|---|
| 無料キーが 429 になったときの、有料キーへの切り替えと 60 秒の保持 | 無料枠が尽きたとき | Cloud Run のログの `app.agents.resilience` の WARNING |
| 主モデルが落ちたときの、別モデルへの実際の切り替え（品質、取り消した試行の課金も） | 主モデルの試行が 3 回とも失敗したとき | 同上。どのモデルが答えたか |
| 履歴の削除ジョブで、会話が実際に消えること | 確認済み（2026-10-25。「削除した会話: 10 件」）。本番を Postgres へ移した後は `--apply` なしなので消えない | `run jobs executions list`（`docunavi-history-cleanup`） |

## 方針

- 1 段ずつ足し、毎回「動く状態」を保つ（`pytest`・`npm test`・`docker compose up` が通ること）。先回りしてリファクタしない。
- 設定が空なら従来どおり動く（モックモード）。構成は monorepo のまま。足すモジュールは必要になった時に作る。
- DB は普通の Postgres + pgvector として使い、`DATABASE_URL` だけに依存する。学習用の構成であって、必須の構成ではない。
- 公開に要る守り（`--max-instances`・予算アラート・`CORS_ORIGINS` を実ドメインに絞る）は、配備のときに済ませてある（[deploy.md](deploy.md)）。

## 今はやらないこと

- `app/agents/graph.py`（LLM 設定・モック・グラフが同居）の分割と、`app/api/routes.py` の LLM エラー分類の分離。テストが `build_graph` などを直接 import しているので、分ける理由（複数エージェント化など）が出るまで保留する。
- 環境変数の読み取りの集約（settings）。変数群（DB・R2・認証）がさらに増えたときに行う。
