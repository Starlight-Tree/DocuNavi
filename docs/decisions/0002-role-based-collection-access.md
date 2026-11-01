# 0002 ロール別の資料グループ制御（ロールは「グループの束」）

- 状態: 実装済み（本番は 2026-10-26 から `ACCESS_MODE=enforce`）
- 日付と決めた人: 2026-10-26（利用者）

## 背景
資料は全員共通（2026-10-20 の決定）だったが、ユーザーロールごとに、表示・検索できる PDF グループ（`documents.collection`）を制御したくなった。

## 決定
- 見えないグループは、一覧・検索・回答・出典・本文・PDF・関係図・履歴表示のすべてに出さない（全面不可）。
- 権限の本体は Supabase Postgres と FastAPI。ロールは「グループの束」だけで、コード上の `admin` などは持たない。
- ロールのない利用者は `default` ロール（`zahard` `cafe` `guides` の 3 グループ。既存利用者の挙動を変えない）。
- ロールの持ち主は Supabase の `sub`（UUID）に紐づく自前の表。外部キーは張らない。解決はキャッシュなしで毎リクエスト。
- 検索は `scope` が必須（`Scope.ALL` は呼び出し側が明示）。見えない資料は 404、見えないグループの指定は 403。
- MCP（`docunavi-mcp`）は運用者専用で全グループのまま。利用者には公開しない。
- 段階導入のため `ACCESS_MODE=off|shadow|enforce` を持つ（`shadow` は絞り込まず、減る件数だけをログに出す）。

## 結果と限界
- フェーズ 1〜3 は完了。migration は本番に適用済み（2026-10-26）。運用者向けの割り当て画面は [0003](0003-django-only-for-operator-admin.md)。
- 未確認: 検索が `cafe` 以外を返さないこと（実際の Gemini の埋め込みが要る）、公開環境のサインアップの方針ほか（[access-control-plan.md](../access-control-plan.md) の「未確認」）。

## 出典
[access-control-plan.md](../access-control-plan.md)、[access-admin.md](../access-admin.md)、[configuration.md](../configuration.md) の「資料グループのアクセス制御」
