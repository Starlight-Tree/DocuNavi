# db/（Postgres のスキーマと適用）

Postgres（本番は Prisma Postgres）の表は、`db/migrations/` の SQL だけで作る。アプリのコードから DDL を流さない（`app/CLAUDE.md`）。

| 置き場所 | 役割 |
|---|---|
| `migrations/NNNN_名前.sql` | マイグレーション。名前の順に適用する。1 ファイルが 1 トランザクション |
| `migrate.py` | 適用の道具（書式と対象の切り替えは、ファイル冒頭の docstring） |
| `seeds/` | 初期データ（`classification.sql`: 対象・資料種別の語彙など） |

## 適用

- `python db/migrate.py` は、適用済みと未適用を一覧するだけ（何も書かない）。`python db/migrate.py --apply` で、未適用のファイルを順に適用する。
- 本番への適用は、操作ごとに承認を得てから（手順は、スキル `.claude/skills/prod-db/SKILL.md` と `scripts/prod_run.sh`。構成は [docs/architecture.md](../docs/architecture.md)）。

## 適用済みのファイルは変えない

- 適用履歴は `schema_migrations(version, checksum, applied_at)` に残る。適用済みのファイルの内容（チェックサム）が変わっていると、`migrate.py` は `ABORT` して止まる。直したいときは、新しいファイルを足す。
- 例: `0001_schema.sql` の見出しには「DRAFT - not applied」と、D1（`workers/api/migrations/0001_schema.sql`）からの移植を示す古い記述が残っている。適用済みなので書き換えない（現状は、このファイルと `docs/architecture.md`）。
