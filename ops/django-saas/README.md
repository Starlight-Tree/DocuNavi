# Django の学習用の試作（SaaS 管理の土台）

製品には入っていない。組織・メンバー・役割・プランを Django の管理画面とマイグレーションで作ると、どれだけの手間か、`saas` スキーマだけで完結するか、
Supabase の JWT の `sub` だけで利用者を扱えるかを、開発コンテナの中だけで測った。結果は [docs/target-architecture.md](../../docs/target-architecture.md) の「Django の測定」。

## 再現（開発コンテナ内。外部の Supabase・本番・課金には触れない）

```bash
cd /workspaces/docunavi
python3 -m venv .venv-django && .venv-django/bin/pip install -r ops/django-saas/requirements.txt   # 製品の依存とは別の venv
scripts/test-postgres-up.sh                                   # 一時 Postgres 17（ポート 55432）
.venv-django/bin/python ops/django-saas/bench/bootstrap_db.py # DB・ロール・saas / chatbot（身代わり）スキーマ
cd ops/django-saas
SAAS_DB_USER=postgres ../../.venv-django/bin/python manage.py test       # 88 件（テストは DB の作成が要るので postgres で動かす）
../../.venv-django/bin/python manage.py seed_demo               # 架空データ（繰り返し可）。画面は manage.py runserver 127.0.0.1:8765 の /login/
../../.venv-django/bin/python bench/boundary.py               # マイグレーションの境界（saas_app で migrate → chatbot が不変）
F=/tmp/django-bench; ../../.venv-django/bin/python bench/make_fixtures.py $F
../../.venv-django/bin/python bench/jwt_django.py $F          # JWT の検証（Django 側）
../../.venv/bin/python bench/jwt_app.py $F                    # JWT の検証（今の app/auth.py。アプリの環境で動かす）
../../.venv-django/bin/python bench/request_latency.py $F     # 1 リクエスト（プロセス内）
../../.venv-django/bin/python bench/http_latency.py $F        # 1 リクエスト（開発サーバー経由の HTTP。起動と停止は自動）
cd ../.. && scripts/test-postgres-down.sh                     # 一時 Postgres を止めて、/tmp/pg を消す
```

## 構成

Claude Code 向けの規約と、層ごとの責務の表は [CLAUDE.md](CLAUDE.md)。運用者向けのロール割り当て画面（`ops/access_admin/`）は、これとは別物で、2026-11-08 に廃止して削除した。アプリは `saas` ひとつ。

- `config/`: `settings.py`（`saas` スキーマだけを使う。秘密を持たない）・`urls.py`（`/admin/` `/api/` `/`）・`wsgi.py`
- `saas/urls.py`: API の経路（`/api/me/orgs/` `/api/orgs/<id>/` `/api/orgs/<id>/members/`）
- `saas/permissions.py`: 役割の強さ（`rank` `allows`）。HTTP に依存しない
- `saas/migrations/`: 0001〜0003（`saas` スキーマだけ）
- `saas/tests/`: `test_api.py`（API）・`test_web.py`（画面）・`test_members.py`（メンバー管理の規則）・`test_jwt.py`・`test_models_admin.py`、共通の部品は `helpers.py`
- `saas/models.py`: `Plan`（人数・PDF 数・月の検索回数の上限は架空の値で、強制しない） `Organization` `Membership`（`external_user_id` = JWT の `sub`。役割は viewer < member < admin < owner） `Subscription`（組織と 1 対 1。trialing / active / canceled。支払いの情報は持たない） `Usage`（組織と 1 対 1。PDF 数と今月の検索回数の架空の値。画面に出すだけで、更新も強制もしない）。Django のユーザー表は利用者には使わない
- `saas/admin.py`: 管理画面（一覧・絞り込み・検索・組織の中の契約・使用量・メンバー編集）
- `saas/management/commands/seed_demo.py`: 架空の組織・メンバー・プラン・契約・使用量を入れる（繰り返し実行できる。ローカルの試作 DB だけに使う）
- `saas/jwt_auth.py`: `app/auth.py` と同じ規則の JWT 検証（公開鍵は JWKS のファイル。取得・取り直しは持たない）
- `saas/views.py`: Bearer の JWT で認証する最小の API。役割（owner / admin / member / viewer）で権限を分ける
- `saas/web_views.py` `web_urls.py` `templates/saas/`: 利用者向けの画面（ダッシュボード `/dashboard/`、所属組織 `/orgs/`、組織の詳細、メンバー管理〔owner / admin だけ。追加・役割の変更・外す。規則は `saas/member_admin.py`〕）。`<style>` は base テンプレートに置き、外部の CSS・JS は使わない
- `saas/member_admin.py`: メンバー管理の規則（誰が何を付与・変更・外せるか、プランの人数の上限、最後の owner を残す、同時操作の行ロック）。画面（`web_views.members_manage`）はこれを呼ぶだけ。API の追加（owner だけ）は変えていない
- `saas/session_auth.py`: 画面の「いま誰か」は `current_sub(request)` だけ。試作ではセッションの `sub`（`/login/` で選ぶか UUID を入力。`SAAS_DEMO_LOGIN`、既定は DEBUG と同じ）。実際の認証にするときは、この関数を JWT の `sub` に差し替える
- `saas/presenters.py`: 使用量の状態（余裕あり / 上限に近い〔80% 以上〕/ 上限に達した）と契約状態の文言
- `bench/`: 境界の検査と測定のスクリプト。`bootstrap_db.py` が一時 Postgres に `saas`（Django 専用）と `chatbot`（本番の身代わりの簡略な表）を作る

## 戻し方

`rm -rf ops/django-saas .venv-django`、`scripts/test-postgres-down.sh`。製品のファイル（`requirements*.txt` など）は変えていない。
