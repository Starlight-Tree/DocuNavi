# 0003 Django は運用者 1 人の管理画面に限る（試作は別物）

- 状態: 廃止（2026-11-08）。実装・配備は済んでいたが、本番のロール表が D1 に移ったため、`docunavi-admin` と `ops/access_admin/` を削除した。決定の記録としてこの文書は残す
- 日付と決めた人: 2026-10-25（学習用の試作）、2026-10-26（運用者向けの管理画面と認証の例外。利用者）

## 背景
Django（SaaS 管理）は、学習のために測る対象だった。試作 `ops/django-saas/` は、組織・メンバー・役割・プランを Django で作る手間と、`saas` スキーマだけで完結するかを測った。一方、ロールの割り当て（[0002](0002-role-based-collection-access.md)）には運用者向けの画面が要った。

## 決定
- 製品の Django は `ops/access_admin/` だけ。**運用者 1 人専用**で、利用者向けの機能・マルチテナントは作らない。
- 認証の例外: この管理画面に限り、Django 自身の運用者ログイン（`django.contrib.auth`、パスワード + TOTP）を使う。Supabase Auth とは連携しない。利用者は今までどおり Supabase Auth だけ。
- Django が止まっても、利用者向けの機能は動き続ける。運用者が 2 人以上、または利用者が入るなら、この例外の対象外（決め直す）。
- Django が `migrate` するのは `saas` スキーマだけ。`chatbot` の表は `supabase/migrations/` だけが変える（Django 側は `managed=False`）。
- 試作 `ops/django-saas/` は本番に入れず、`access_admin` とモデル・ロールを使い回さない。

## 結果と限界
- 試作の測定（疑似データ・各 1 回）: マイグレーションは `saas` だけで完結し `chatbot` は不変、JWT の検証は `app/auth.py` と同程度。実鍵・本番・課金は未測定。
- 本番の SaaS 管理としての Django（組織・課金・利用制限の強制）は、要件が固まるまで条件待ち。
- 未確認: Django 画面の「見えるグループ」の表示、`actor=access_admin` の監査記録（[access-admin.md](../access-admin.md)）。

## 出典
[target-architecture.md](../target-architecture.md) の「Django」「Django の測定」、[access-admin.md](../access-admin.md)、[roadmap.md](../roadmap.md) の ⑤⑧、[ops/django-saas/CLAUDE.md](../../ops/django-saas/CLAUDE.md)
