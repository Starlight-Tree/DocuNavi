# 0003 Django は運用者 1 人の管理画面に限る（試作は別物）

- 状態: 廃止（2026-11-08）。実装・配備は済んでいたが、本番のロール表が D1 に移ったため、`docunavi-admin` と `ops/access_admin/` を削除した。決定の記録としてこの文書は残す
- 日付と決めた人: 2026-10-25（学習用の試作）、2026-10-26（運用者向けの管理画面と認証の例外。利用者）

## 背景
Django（SaaS 管理）は、学習のために測る対象だった。試作 `ops/django-saas/` は、組織・メンバー・役割・プランを Django で作る手間と、`saas` スキーマだけで完結するかを測った。一方、ロールの割り当て（[0002](0002-role-based-collection-access.md)）には運用者向けの画面が要った。

## 決定
- 製品の Django は `ops/access_admin/` だけ。**運用者 1 人専用**で、利用者向けの機能・マルチテナントは作らない。
- 認証の例外: この管理画面に限り、Django 自身の運用者ログイン（`django.contrib.auth`、パスワード + TOTP）を使う。利用者の認証とは連携しない。利用者は今までどおり利用者の認証だけ。
- Django が止まっても、利用者向けの機能は動き続ける。運用者が 2 人以上、または利用者が入るなら、この例外の対象外（決め直す）。
- Django が `migrate` するのは `saas` スキーマだけ。`chatbot` の表は旧マイグレーションのディレクトリだけが変える（Django 側は `managed=False`）。〔2026-11-18 の注: 当時のパス。旧ディレクトリは削除済みで、いまのマイグレーションは `db/migrations/`（Git の履歴に旧パスがある）〕
- 試作 `ops/django-saas/` は本番に入れず、`access_admin` とモデル・ロールを使い回さない。

## 結果と限界
- 試作の測定（疑似データ・各 1 回）: マイグレーションは `saas` だけで完結し `chatbot` は不変、JWT の検証は `app/auth.py` と同程度。実鍵・本番・課金は未測定。
- 本番の SaaS 管理としての Django（組織・課金・利用制限の強制）は、要件が固まるまで条件待ち。
- 未確認: Django 画面の「見えるグループ」の表示、`actor=access_admin` の監査記録（文書は削除済み。Git の履歴）。

## 出典
[target-architecture.md](../target-architecture.md) の「Django」「Django の測定」、[roadmap.md](../roadmap.md) の ⑤⑧、[ops/django-saas/CLAUDE.md](../../ops/django-saas/CLAUDE.md)

## 追記（2026-11-20）: 廃止のあとの状態

本文と状態の欄は、廃止した 2026-11-08 までの記録のまま。そのあとで変わった点と、確かめた範囲は次のとおり。

- **保存先と認証:** 本文の旧認証・旧マイグレーションのパスの記述は当時のもの。本番の保存先は 2026-11-08 に旧 DB から Cloudflare の D1 へ移り（状態の欄の「D1 に移った」はこの時点）、同じ日のうちに Prisma Postgres へ移った（ロードマップの段階 1。切り替えの時刻は未確認）。本番の保存先としての D1（データ用 Worker を含む）は撤去済み。コードは旧経路として残る（`DB_BACKEND` の既定は `d1` のまま。テストの偽 Worker、`PgSaver` の土台、pg の経路と CLI が import する `D1Error` `SyncD1` などに使う。`app/pg.py`、`app/d1.py`、`app/d1_saver.py`、`app/CLAUDE.md`）。利用者の認証は Better Auth（Worker `docunavi-api`）。
- **削除したもの:** 運用者向けの Django（`ops/access_admin/`。パスワード + TOTP のログインと割り当て画面）はコードごと削除した（2026-11-08 の退役のコミット。Git の履歴にある）。本文の「認証の例外」は、対象が無くなり、現在は適用される場面が無い。Cloud 側の `docunavi-admin` のサービスとジョブは、利用者が削除し、Secret・サービスアカウント・イメージは残る、とそのコミットに書かれている（この追記では確認していない）。 <!-- check-docs: ignore -->
- **割り当ての手段:** 画面の代わりに、CLI `python -m app.access_admin`（2026-11-09 に追加。既定は確認だけで、`--apply` で書く。監査は `access_audit` の `actor=access_admin_cli`。詳細は [0002](0002-role-based-collection-access.md) の追記）。
- **未確認の扱い:** 本文の「未確認: Django 画面の『見えるグループ』の表示、`actor=access_admin` の監査記録」は、画面と文書が削除されたので、確かめる対象が無くなった（確認はしていない）。
- **試作と条件待ち:** 試作 `ops/django-saas/` は、リポジトリに残っている（学習用。製品には入っていない。`ops/django-saas/CLAUDE.md`）。本番の SaaS 管理としての Django は、引き続き条件待ち（ロードマップ ⑤。利用者向けの要件が固まるまで）。
