# ロール別 PDF グループ制御の計画（2026-10-26、利用者の決定）

[roadmap.md](roadmap.md) の項目 ⑧ にあった「計画」を、2026-11-01 に、文面を変えずにこの文書へ移した（段落の区切りだけ足した）。
実装の状態は roadmap.md の ⑧、仕様と運用は [configuration.md](configuration.md) の「資料グループのアクセス制御」。Django Admin（フェーズ 4a）は 2026-11-08 に廃止した（[決定 0003](decisions/0003-django-only-for-operator-admin.md)）。

**目的:** ユーザーロールごとに、表示・検索できる PDF グループ（`documents.collection`）を制御する。見えないグループは、一覧・検索・回答・出典・本文・PDF・関係図・履歴表示のすべてに出さない（全面不可）。

**方針:** 権限の本体は Supabase Postgres と FastAPI。Django は運用者向け管理画面の候補の一つで、導入は必須ではない。

**この項目で、上の 3 番の「資料は全員共通」（2026-10-20）を、グループ単位の絞り込みについて見直す**（利用者のアップロードは作らない点は変わらない）。

**決定済みの設計（2026-10-26）:** ロールは「グループの束」だけで、コード上の `admin` などは持たない／ロールのない利用者は `default` ロール（現行の `zahard` `cafe` `guides` の 3 グループ。既存利用者の挙動を変えない）／ロールの持ち主は Supabase の `sub`（UUID）に紐づく自前の表。外部キーは張らない／権限の解決はキャッシュなしで毎リクエスト（必要になってから 30 秒程度の TTL を検討）／過去の会話は表示するが、現在見えないグループの出典・本文は表示しない／MCP（`docunavi-mcp`）は運用者専用・全グループのまま。利用者には公開しない（docs に明記する）／UI のモックは省略（`GET /collections` の結果に合わせて項目が減るだけで、構成は変えない）。

**フェーズ 1（DB と migration）:** `chatbot.roles`・`chatbot.user_roles`・`chatbot.role_collection_access`・`chatbot.access_audit`、`default` ロールと現行 3 グループの初期データ、`chatbot_app` への SELECT、`anon` / `authenticated` の revoke。**RLS はこのフェーズでは入れない**（別フェーズで、性能の測定と、取り込み・MCP・起動時の検査・評価への影響を調べてから判断する。`documents` `chunks` の所有者は `chatbot_app` で、`FORCE ROW LEVEL SECURITY` が要る。2026-10-26 に本番の読み取りで、RLS が全表で無効であることと、`anon` / `authenticated` に `chatbot` スキーマの権限が無いことを確認した）。一時 Postgres での検証、migration ファイル（`supabase/migrations/`）の作成、コミット。push は別途承認。

**フェーズ 2（FastAPI のアクセス制御）:** `app/access.py` の `visible_collections(user_id)`／検索・回答系に必須の `scope`（`Scope.ALL` を明示しない限り全件を検索しない）／次の 10 経路に制限: 検索の SQL（`search_vector._SEARCH_SQL`）・`GET /documents`・`/documents/{id}/chunks`・`/documents/{id}/file`・`/documents/{id}/graph`・`/documents/{id}/impact`・`GET /collections`・`/chat`・`/chat/stream`・会話履歴の表示／見えない資料は 404、見えないグループの指定は 403、見えるグループが 0 件なら 403「利用できる資料がありません」／エラー文は固定で、非表示グループ名を出さない（今の 409 の文は両方の範囲名を載せるので直す）／`ACCESS_MODE=off\|shadow\|enforce`（`shadow` は絞り込まず、減る件数だけをログに出す）／`cafe` だけのロールで `zahard` の内容が出ないことを確かめる、10 経路の漏れ総当たりテスト／docs の更新（アクセス制御の仕様、MCP が運用者専用であること、`ACCESS_MODE` の意味、運用者が SQL で `user_roles` を更新する手順）。コミット。push は別途承認。

**フェーズ 3（本番適用と段階導入）:** 本番 DB への migration 適用は操作ごとに承認／本番はまず `ACCESS_MODE=shadow` で観察し、問題がなければ `enforce` に切り替える（切り替えも別途承認）／画面・Cloud Run の変更も別途承認。

**フェーズ 4（管理画面。条件付き）:** フェーズ 3 の完了後に検討し、運用者が SQL で `user_roles` を更新する運用が続く場合にだけ着手する。候補は Django Admin と Next.js の管理画面（Django は 2026-11-08 に廃止済み）。Django を採るなら運用者専用の管理コンソールに限り（利用者向け機能は作らない）、試作用の `ops/django-saas` のロールは使い回さない。実装の前に、別途設計と承認を取る。

**暫定の運用:** フェーズ 4 までは、運用者が SQL で `user_roles` を更新する。

**未確認:** 公開環境のサインアップの方針（誰でも Google で入れるか）／運営者が UUID とメールをどう対応づけるか／`lib/collections.ts` の固定の表示名が、一覧の元になっていないか／Data API の公開スキーマに `chatbot` が入っていないこと（`authenticator` の設定を読み切れていない）。
