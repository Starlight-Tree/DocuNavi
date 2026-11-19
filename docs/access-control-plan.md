# ロール別 PDF グループ制御の計画（2026-10-26、利用者の決定）

[roadmap.md](roadmap.md) の項目 ⑧ にあった「計画」を、2026-11-01 に、この文書へ移した。2026-11-20 に、実装・配備の状態の記述（完了の印、廃止した Supabase 前提の名前〔`chatbot` スキーマ・`supabase/migrations/`・DB のロール〕、コード上の名前、409 の文、運用の手段）を更新し、「その後の変更」の段落を足した。決定の文（2026-10-26）と、各フェーズの承認の条件は変えていない（フェーズ 4 の着手の条件も元の文言のまま）。
実装の状態は roadmap.md の ⑧、仕様と運用は [configuration.md](configuration.md) の「資料グループのアクセス制御」、その後の変更と本番での確認は [決定 0002](decisions/0002-role-based-collection-access.md) の「追記（2026-11-20）」。Django Admin（フェーズ 4a）は 2026-11-08 に廃止した（[決定 0003](decisions/0003-django-only-for-operator-admin.md)）。

**目的:** ユーザーロールごとに、表示・検索できる PDF グループ（`documents.collection`）を制御する。見えないグループは、一覧・検索・回答・出典・本文・PDF・関係図・履歴表示のすべてに出さない（全面不可）。

**方針:** 権限の本体は Postgres と FastAPI。Django は運用者向け管理画面の候補の一つで、導入は必須ではない。

**この項目で、「資料は全員共通」（2026-10-20 の決定）を、グループ単位の絞り込みについて見直す**（利用者のアップロードは作らない点は変わらない）。

**決定済みの設計（2026-10-26）:** ロールは「グループの束」だけで、コード上の `admin` などは持たない／ロールのない利用者は `default` ロール（現行の `zahard` `cafe` `guides` の 3 グループ。既存利用者の挙動を変えない）／ロールの持ち主は認証の `sub`（UUID）に紐づく自前の表。外部キーは張らない／権限の解決はキャッシュなしで毎リクエスト（必要になってから 30 秒程度の TTL を検討）／過去の会話は表示するが、現在見えないグループの出典・本文は表示しない／MCP（`docunavi-mcp`）は運用者専用・全グループのまま。利用者には公開しない（docs に明記する）／UI のモックは省略（`GET /collections` の結果に合わせて項目が減るだけで、構成は変えない）。

**その後の変更（2026-11-20 にコードと `db/migrations/` で確認。詳細は決定 0002 の追記）:** `sub` は Better Auth の `user.id`（`app/auth.py`）／`user_roles.user_id` は `"user"(id)` への外部キー（`0001_schema.sql`）／権限の解決は、2026-11-11 から、プロセス内の短い TTL の控え（`ACCESS_CACHE_TTL_SECONDS`、コードの既定は 60 秒。本番の Cloud Run では未設定で、この既定が効く。2026-11-20、gcloud の読み取り、A）／本番の `default` は、2026-11-09 の読み取りでは 4 グループ（現在は未確認）。全面不可・`scope` 必須・MCP は運用者専用は変わっていない。

**フェーズ 1（DB と migration。完了）:** 表 `roles`・`user_roles`・`role_collection_access`・`access_audit`、`default` ロールと現行 3 グループの初期データ。**RLS はこのフェーズでは入れない**（別フェーズで、性能の測定と、取り込み・MCP・起動時の検査・評価への影響を調べてから判断する）。一時 Postgres での検証、migration の作成、コミット。push は別途承認。
当時は Supabase の `chatbot` スキーマに作った（マイグレーション `20261026045921`。`chatbot_app` に SELECT だけ、`anon` / `authenticated` は revoke。2026-10-26 の本番の読み取りで、RLS が全表で無効、両ロールに `chatbot` スキーマの権限が無いことを確認した）。Supabase は 2026-11-08 に廃止し、`supabase/` は削除した（旧ファイルは Git の履歴）。いまの表は `db/migrations/0001_schema.sql`（`chatbot` スキーマ・Supabase のロールへの権限・RLS は無い）で、適用は `db/migrate.py`（本番は操作ごとに承認。`db/README.md`）。

**フェーズ 2（FastAPI のアクセス制御。完了）:** `app/access.py` の `visible_collections(user_id)`／検索・回答系に必須の `scope`（`Scope.ALL` を明示しない限り全件を検索しない）／次の 10 経路に制限: 検索の SQL（`app/search/` の SQL。範囲の条件は `app/scope.py` の `ALLOWED_FILTER_SQL`）・`GET /documents`・`/documents/{id}/chunks`・`/documents/{id}/file`・`/documents/{id}/graph`・`/documents/{id}/impact`・`GET /collections`・`/chat`・`/chat/stream`・会話履歴の表示／見えない資料は 404、見えないグループの指定は 403、見えるグループが 0 件なら 403「利用できる資料がありません」／エラー文は固定で、非表示グループ名を出さない（409 の文も固定文。2026-11-20 にコードで確認）／`ACCESS_MODE=off\|shadow\|enforce`（`shadow` は絞り込まず、減る件数だけをログに出す）／`cafe` だけのロールで `zahard` の内容が出ないことを確かめる、10 経路の漏れ総当たりテスト／docs の更新（アクセス制御の仕様、MCP が運用者専用であること、`ACCESS_MODE` の意味、運用者が `user_roles` を更新する手順。当初は SQL、いまは `python -m app.access_admin`）。コミット。push は別途承認。

**フェーズ 3（本番適用と段階導入）:** 本番 DB への migration 適用は操作ごとに承認／本番はまず `ACCESS_MODE=shadow` で観察し、問題がなければ `enforce` に切り替える（切り替えも別途承認）／画面・Cloud Run の変更も別途承認。本番は、記録上は 2026-10-26 から `enforce`（2026-10-26 の `dae006b` が configuration.md とコミットメッセージに残した記述による。切り替えの操作そのものの記録は、リポジトリからは確認できない）。同じ記述では、`shadow` で観察してから切り替えた（観察の結果は残っておらず、根拠は未確認）。2026-11-20 に、配信中の Cloud Run のリビジョン `docunavi-00091-juj` の環境変数が `ACCESS_MODE=enforce` であることを確かめた（gcloud の読み取り、A）。この読み取りは現在の状態だけで、2026-10-26 から今までずっと `enforce` だったかは未確認。

**フェーズ 4（管理画面。条件付き）:** フェーズ 3 の完了後に検討し、運用者が SQL で `user_roles` を更新する運用が続く場合にだけ着手する（2026-11-09 の `e9c0bf7` 以降は、CLI の `python -m app.access_admin` で更新している。この条件を今どう読むかは、利用者の判断。未確認）。候補は Django Admin と Next.js の管理画面（Django は 2026-11-08 に廃止済み）。Django を採るなら運用者専用の管理コンソールに限り（利用者向け機能は作らない）、試作用の `ops/django-saas` のロールは使い回さない。実装の前に、別途設計と承認を取る。

**暫定の運用:** フェーズ 4 までは、`user_roles` の変更は `python -m app.access_admin` だけで行う（SQL で直接 `DELETE` しない。理由は `app/access_admin.py` の docstring）。既定は確認だけで、`--apply` で書く。利用者は `--email`（`"user".email`）か `--user-id` で選ぶ。ロールの作成とグループの編集は SQL（`docs/configuration.md`）。

**未確認:** 公開環境のサインアップの方針（誰でも Google で入れるか）／`frontend/src/lib/collections.ts` の固定の表示名が、一覧の元になっていないか（2026-11-20 に、このファイルは固定の名前表を持たず、表示名はサーバーの `GET /collections` が持つことを読んだ。画面全体では未確認）／`default` ロールなどの初期データを入れる SQL が、いまの `db/migrations/` に無い（本番の行の投入元は未確認）／Supabase の Data API の公開スキーマに `chatbot` が入っていないこと（`authenticator` の設定を読み切れていなかった。Supabase の廃止で対象外）。
