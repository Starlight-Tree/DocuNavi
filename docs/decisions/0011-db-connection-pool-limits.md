# 0011 DB の接続プールの上限を、docunavi=2・docunavi-mcp=1 にする

- 状態: 実装済み（本番に配備済み。2026-11-20 に、配信中のリビジョンの環境変数で確認、証拠等級 A。下の追記）
- 日付と決めた人: 2026-11-09（利用者）

## 背景
本番 DB（Prisma Postgres Free）のプール側の接続上限は 10。Cloud Run の `docunavi`（最大 2 インスタンス）、`docunavi-mcp`、Cloud Run Job が同じ DB につなぐので、既定の `DATABASE_POOL_MAX_SIZE=3` のままだと、重なったときに上限を超えうる。

## 決定
- 本番の `DATABASE_POOL_MAX_SIZE` は、`docunavi`=2、`docunavi-mcp`=1。コードの既定（3）は変えない（[configuration.md](../configuration.md)）。取り込みなどの CLI は 1 接続。
- Hyperdrive（認証 Worker の接続）の接続先は、直接ホスト・上限 5 で余裕が無い。変更しない。
- 利用が少ないので（`chat_metrics` が 30 日で 2 件、2026-11-09）、これ以上の調整はしない。

## 根拠（2026-11-09、証拠等級 A）
- Job の重なりは実行履歴から読んだ（Scheduler の設定は閲覧用の権限で読めない）。当時の最大は日曜 03:00 JST の 3 本（`history-cleanup` `ingest-queue` `ingest-sync`）で、4 本は同時にならない。接続は docunavi 2 × 2 + mcp 1 + Job 3 本（各 1 と仮定）= 8 で、上限 10 に収まる。
- 待ち時間は、本番 DB（読み取り専用）への検索 1 回ぶん（KNN + 軽い問い合わせの 2 回の借り出し。埋め込み API は呼ばない）を、開発コンテナから 3 波ずつ流して測った。同時 2 件までは上限 2 と 3 で差が小さい（中央値 202 ms と 196 ms）。同時 4 件で 379 ms と 262 ms、16 件で 1462 ms と 1199 ms（上限 1 は 3029 ms）。エラーは 0。
- 配備: `docunavi`（`DATABASE_POOL_MAX_SIZE=2`）と `docunavi-mcp`（`=1`）を 0% で配備し、`/health`・環境変数・起動ログを確かめてから 100% へ。本番の `/chat` 1 回（200、`hit_count=2`）と MCP の検索 1 回（200）も確認した。

## 結果と限界
- **未確認:** Cloud Run 上での待ち時間（測ったのは開発コンテナから）、LLM を含む `/chat` の同時負荷、Job の接続数（各 1 は仮定）。
- Job の本数・実行時刻を変えたら、重なりと合計（上の 8）を計算し直す。

## 出典
[configuration.md](../configuration.md) の `DATABASE_POOL_MAX_SIZE`、[mcp-tool-design.md](../mcp-tool-design.md)、[roadmap.md](../roadmap.md) の「接続数」

## 追記（2026-11-20）: 根拠の前提が変わった点

本文の数値の決定（本番の `DATABASE_POOL_MAX_SIZE` は docunavi=2・docunavi-mcp=1。コードの既定は 3）は、リポジトリの記録では変わっていない（`app/pg.py`、[configuration.md](../configuration.md)。`DATABASE_POOL_MAX_SIZE` を grep しても、その後に値を変えた記録は無い、2026-11-20）。配信中のリビジョンの環境変数でも同じ値だった（下の「本番の現在の値」）。本文の「根拠」のうち、次の点は前提が変わった。

- **Job の実行時刻:** 根拠の「日曜 03:00 JST の 3 本（`history-cleanup` `ingest-queue` `ingest-sync`）」は、2026-11-09 の実行履歴。その後、[0012](0012-accept-prisma-free-limit-and-cut-operations.md) の操作数の削減（2026-11-10〜11）で、毎時の同期を週 1 回（日曜 00:00 JST）にし、`docunavi-ingest-queue-every-5min` を一時停止し、バックアップを毎日から週 1 回（日曜 00:00 JST。[pg-backup.md](../pg-backup.md)）にした。文書の上では、日曜 00:00 JST に同期とバックアップ、日曜 03:00 JST に履歴掃除（[deploy.md](../deploy.md) の「D. 会話履歴の削除」）。本文の「Job の本数・実行時刻を変えたら、重なりと合計を計算し直す」の再計算は、記録が無い（未確認）。実行の実績は、`docunavi-history-cleanup` の最後の実行が 2026-11-14T18:00:27Z（2026-11-15 03:00 JST。日曜）で完了、削除は 0 件（2026-11-20、gcloud の読み取り、証拠等級 A。[deploy.md](../deploy.md) の「D. 会話履歴の削除」の訂正と同じ）。`docunavi-ingest-sync` の最終実行は 2026-11-14 15:00 UTC（日曜 00:00 JST）で、週 1 回の記録と時刻が合う（[configuration.md](../configuration.md) の 2026-11-20 の記録、A）。一方、`docunavi-ingest-queue` の最終実行は 2026-11-15 15:27 UTC で、一時停止の記録より後（理由は未確認。configuration.md）。Scheduler の設定は、コンテナの閲覧用の権限では読めない。2026-11-20 にホストの Claude が読んだ結果（ホストの報告。私は再確認していない）では、`docunavi-pg-backup-daily` と `docunavi-ingest-sync-hourly` はどちらも cron `0 0 * * 0`（日曜。`JST` は Job の最終実行からの推定）、`docunavi-ingest-queue-every-5min` は PAUSED（[pg-backup.md](../pg-backup.md)、[roadmap.md](../roadmap.md)）。履歴掃除の週 1 回（日曜 03:00 JST）は、Scheduler の設定の読み取りではなく、[deploy.md](../deploy.md) の手順の記述による。この前提で数えた重なり（計算。実測ではない）: 日曜 00:00 JST は同期とバックアップの 2 本（各 1 接続と仮定）で、docunavi 2 × 2 + mcp 1 + Job 2 本 = 7、日曜 03:00 JST は履歴掃除 1 本で 6。どちらも上限 10 に収まる。`docunavi-mcp` の最大インスタンス数が 1 であることは、この追記では未確認（上と同じ前提）。
- **操作数の上限:** 接続数とは別に、Prisma Free の月 200,000 operations が本番 DB の制約として問題になった（2026-11-15 の Console で 197,536。利用者の貼り付け、証拠等級 A）。止まることは利用者が受け入れ済み（0012）。上限に届いたあとの挙動は、0012 のとおり未確認。
- **Hyperdrive:** `workers/api/wrangler.jsonc` は引き続き Hyperdrive を束縛し、`workers/api/src/index.ts` はリクエストごとに `max: 1` のプールを作る。本文の「直接ホスト・上限 5」は 2026-11-09 の値で、Cloudflare 側の設定なのでリポジトリからは見えず、現在は未確認。
- **本番の現在の値（2026-11-20、gcloud の読み取り、証拠等級 A）:** `docunavi` は、リビジョン `docunavi-00091-juj` が 100% を受けていて、`DATABASE_POOL_MAX_SIZE=2`・`DB_BACKEND=pg`。`docunavi-mcp` は、リビジョン `docunavi-mcp-00004-qif` が 100% を受けていて、`DATABASE_POOL_MAX_SIZE=1`・`DB_BACKEND=pg`（[mcp-tool-design.md](../mcp-tool-design.md) の 2026-11-09 の記録と同じ）。どちらも、本文の決定の値と同じ。`docunavi` の最大インスタンス数 2 は、[configuration.md](../configuration.md) の同日の確認（A）による。`docunavi-mcp` の最大インスタンス数と、Job の接続数（CLI は設定に関係なく 1 接続）は、この追記では確認していない。
- **対象外:** 検証環境（[architecture.md](../architecture.md) の「検証環境（AlloyDB Express）」）は別の DB で、この決定の対象外。
