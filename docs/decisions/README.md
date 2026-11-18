# 意思決定の記録（decisions/）

重要な決定を、1 件 1 ファイルで残す。**あとから「なぜそうなっているか」を、コードや長い文書を読まずに分かるようにする**ためのもの。
新しく書くより先に、すでに文書にある決定を、出典つきで要約した（2026-11-01）。出典の文書が正本で、食い違ったら出典を信じる。

| 番号 | 決定 | 状態 |
|---|---|---|
| [0001](0001-frontend-on-vercel-api-only-backend.md) | 画面は Vercel（Next.js）、バックエンドは API だけ | 実装済み |
| [0002](0002-role-based-collection-access.md) | ロール別の資料グループ制御（ロールは「グループの束」） | 実装済み（本番は `enforce`） |
| [0003](0003-django-only-for-operator-admin.md) | Django は運用者 1 人の管理画面に限る（試作は別物） | 廃止（2026-11-08。`docunavi-admin` と `ops/access_admin/` を削除。記録として残す） |
| [0004](0004-no-llm-gateway.md) | LLM ゲートウェイを使わず、耐障害性はアプリ内で持つ | 実装済み |
| [0005](0005-go-sse-relay-without-prod-limit.md) | Go の SSE 中継は配備し、本番には同時数の上限を入れない | 配備済み（上限のコードは未配備） |
| [0006](0006-graph-rag-default-off.md) | 検索の GraphRAG は既定 OFF のまま | 置き換え済み（0007） |
| [0007](0007-graph-rag-default-on.md) | 検索の GraphRAG を既定 ON にする（`SEARCH_GRAPH_MAX_EXTRA`=1 のまま） | 実装済み（`main` に入っている。2026-11-18 に `git branch -r --contains` で確認。`SEARCH_GRAPH` の既定は on。本番への配備はロードマップの記録〔2026-11-01〕で、実機の設定は再確認していない） |
| [0008](0008-document-target-and-type.md) | 資料の分類「対象」「資料種別」を足し、`collection` は保存・認可の単位のまま | 実装済み（`main`。スキーマは `db/migrations/0001_schema.sql`、初期分類は `db/seeds/classification.sql`。本番への適用は、そのファイルのコメントに「2026-11-04 適用済み」とあるが、本番では未確認〔2026-11-18〕） |
| [0009](0009-drop-custom-domain-and-worker-front.md) | 独自ドメインと、Cloudflare Worker の前段（段階 3・③）を廃止する | 決定（文書のみ） |
| [0010](0010-physical-page-number-and-display-label.md) | PDF のページを「通し番号」（リンク・照合）と「表示ラベル」（画面の表示）に分ける | 実装済み（`main`。本番 DB に 0002 適用済み。Cloud Run・再取り込みは未実施） |
| [0011](0011-db-connection-pool-limits.md) | DB の接続プールの上限を docunavi=2・docunavi-mcp=1 にする | 実装済み（本番に配備済み） |
| [0012](0012-accept-prisma-free-limit-and-cut-operations.md) | Prisma Free の月 200,000 操作の上限は受け入れ、DB 操作は構成で減らす | 実装済み（2026-11-18 の引き継ぎでは上限で停止中。12-01 まで放置） |
| [0013](0013-pdf-via-same-origin-relay.md) | PDF は、画面の Worker の同一オリジンの中継で iframe に渡す（保存名を資料名にするため） | 承認済み・実装済み（1ac0573）。表示の方式は 0014 が置き換え（中継は有効） |
| [0014](0014-pdfjs-viewer.md) | PDF の表示を iframe から PDF.js に置き換える（出典の根拠を PDF 上で強調するため） | 実装済み（作業ツリー。未コミット・未配備。本番での配信・Cookie 経由の取得は未確認） |

## 書き方

- ファイル名は `NNNN-短い英語の題.md`（番号は連番。欠番を詰めない）。
- 項目: **状態**（提案 / 決定 / 実装済み / 置き換え済み）、**日付と決めた人**、**背景**、**決定**、**結果と限界**（確認していないことは「未確認」と書く）、**出典**。
- 決定を変えるときは、古いファイルを書き換えず、新しいファイルを足して「置き換え」と書く（古い方の状態も「置き換え済み」にする）。
- 測定の数字は、質問の数・上位何件・どの構成かを添える（`evals/CLAUDE.md`）。実機で確認していないことを「確認済み」と書かない。
- 秘密・トークン・実キーの値は書かない。
