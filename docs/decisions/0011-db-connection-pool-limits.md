# 0011 DB の接続プールの上限を、docunavi=2・docunavi-mcp=1 にする

- 状態: 実装済み（本番に配備済み）
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
