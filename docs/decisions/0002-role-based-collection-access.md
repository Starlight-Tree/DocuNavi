# 0002 ロール別の資料グループ制御（ロールは「グループの束」）

- 状態: 実装済み（本番は 2026-10-26 から `ACCESS_MODE=enforce`。2026-11-20 に、配信中のリビジョンの環境変数でも確認、証拠等級 A。下の追記）
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
[access-control-plan.md](../access-control-plan.md)、[configuration.md](../configuration.md) の「資料グループのアクセス制御」

## 追記（2026-11-20）: 保存先・認証・割り当ての手段の変化と、本番での確認

本文は 2026-10-26 の決定のまま。権限の考え方（グループの束・全面不可・`scope` 必須・MCP は運用者専用）は、コードの上で変わっていない（`app/access.py`、`app/scope.py`。`ops/mcp_search` は `Scope.ALL` を明示）。変わったのは次の点。

- **保存先と利用者 ID:** 本文の「Supabase Postgres」「Supabase の `sub`（UUID）」は当時のもの。本番の DB は 2026-11-08 に Prisma Postgres（`DB_BACKEND=pg`）へ、認証は Better Auth（Worker `docunavi-api`）へ移った。`sub` は Better Auth の `user.id`（英数字・`_`・`-` の 8〜64 文字。`app/auth.py`）。表は `chatbot` スキーマではなく、`db/migrations/0001_schema.sql` の `roles` `role_collection_access` `user_roles` `access_audit`。本文の「外部キーは張らない」と違い、その定義では `user_roles.user_id` が `"user"(id)` への外部キー（`ON DELETE CASCADE`）を持つ。
- **キャッシュ:** 本文の「キャッシュなしで毎リクエスト」は、2026-11-11 から、プロセス内の控え（`ACCESS_CACHE_TTL_SECONDS`。コードの既定は 60 秒、0 で無効）に変わった。Prisma Postgres Free の月の操作数を減らすため（[0012](0012-accept-prisma-free-limit-and-cut-operations.md)。利用者が許容）。割り当ての変更が効くまでに、最大でその秒数かかる（インスタンスごとに別の控え。引けなかった結果は控えない）。本番では環境変数が設定されておらず、コードの既定の 60 秒が効く（下の「本番の現在の設定」）。
- **割り当ての手段:** 運用者向けの割り当て画面（Django の `docunavi-admin`）は 2026-11-08 に廃止した（[0003](0003-django-only-for-operator-admin.md)）。代わりに CLI `python -m app.access_admin`（2026-11-09 に追加。既定は確認だけで、`--apply` で書く。変更は `access_audit` に `actor=access_admin_cli` で残す。見えるグループが増える解除は `--allow-widen` が要る）。2026-11-18 の運用の規則: `user_roles` の変更はこの CLI だけで行い、SQL で直接 `DELETE` しない（最後の 1 行を消すと、見える範囲が `default` まで広がり、DB 側に防ぐ制約は無い）。ロールの作成と、ロールが持つグループの編集は SQL。
- **`default` ロールのグループ:** 本文の「3 グループ」は当時の初期データ。2026-11-09 の本番の読み取りでは、`default` は `cafe` `guides` `synthetic-test` `zahard` の 4 グループだった（証拠等級 A。記録は Git の履歴の `docs/deploy.md`）。グループを足したら `default` にも割り当てる運用（[configuration.md](../configuration.md)）。現在の割り当ては未確認。
- **利用資格（別の関門）:** 「アプリを使ってよいか」は、ロール・グループとは独立の `MEMBERSHIP_MODE` と表 `app_members`（`db/migrations/0003_app_members.sql`。2026-11-16 に追加）で決める。ロールを付け替えても資格は変わらず、資格があっても見える資料が 0 件なら 403（[configuration.md](../configuration.md) の「利用資格（アプリを使ってよい人）」）。
- **確認（2026-11-09、証拠等級 A、各 1 回。ロードマップ ⑧ にあった記録をここへ移した）:** 本文の「未確認: 検索が `cafe` 以外を返さないこと」のうち、検索の層と、ロールを割り当てたユーザーの `/chat/stream` を確かめた。
  - 本番 DB（読み取り専用。埋め込みは Vertex、LLM は呼んでいない）で `Scope.only({"cafe"})` の検索は、`cafe` と無関係な質問 2 問では 0 件（対照の `Scope.ALL` は 5 件ずつ）、`cafe` の質問では 5 件すべてが `cafe` の資料だった。
  - テスト専用ユーザーに `cafe-staff` を割り当てた `/chat/stream`（本番の API・LLM）は、`cafe` の質問で出典が `cafe` だけ、`zahard` の質問で出典 0 件。解除（`--allow-widen`）後は `default` に戻り、`zahard` の質問に出典が付いた。LLM は計 3 回。`user_roles` は 0 行に戻り、`access_audit` に 2 行が残った。
  - 限定ロールの範囲が実際に絞られたので、この時点の本番では `ACCESS_MODE=enforce` が効いていたと読める。
  - **限界:** テスト用ログイン（メール + パスワード）での確認で、Google ログインの利用者では確かめていない（未確認）。
- **本番の現在の設定（2026-11-20、gcloud の読み取り、証拠等級 A）:** Cloud Run `docunavi` は、リビジョン `docunavi-00091-juj` が 100% のトラフィックを受けていて、環境変数は `ACCESS_MODE=enforce`・`MEMBERSHIP_MODE=enforce`・`AUTH_MODE=required`・`DB_BACKEND=pg`。`ACCESS_CACHE_TTL_SECONDS` は設定されていない（コードの既定の 60 秒が効く）。状態の欄の「2026-10-26 から」は、その日に切り替えた記録による（切り替えの時期は、この読み取りでは確かめていない）。
- **未確認のまま:** 公開環境のサインアップの方針ほか（[access-control-plan.md](../access-control-plan.md) の「未確認」）。本番の `user_roles` と `default` ロールの現在の中身（上の「`default` ロールのグループ」）。
