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
SAAS_DB_USER=postgres ../../.venv-django/bin/python manage.py test       # 35 件（テストは DB の作成が要るので postgres で動かす）
../../.venv-django/bin/python bench/boundary.py               # マイグレーションの境界（saas_app で migrate → chatbot が不変）
F=/tmp/django-bench; ../../.venv-django/bin/python bench/make_fixtures.py $F
../../.venv-django/bin/python bench/jwt_django.py $F          # JWT の検証（Django 側）
../../.venv/bin/python bench/jwt_app.py $F                    # JWT の検証（今の app/auth.py。アプリの環境で動かす）
../../.venv-django/bin/python bench/request_latency.py $F     # 1 リクエスト（プロセス内）
../../.venv-django/bin/python bench/http_latency.py $F        # 1 リクエスト（開発サーバー経由の HTTP。起動と停止は自動）
cd ../.. && scripts/test-postgres-down.sh                     # 一時 Postgres を止めて、/tmp/pg を消す
```

## 構成

- `saas/models.py`: `Plan`（人数・PDF 数・月の検索回数の上限は架空の値で、強制しない） `Organization` `Membership`（`external_user_id` = JWT の `sub`。役割は viewer < member < admin < owner） `Subscription`（組織と 1 対 1。trialing / active / canceled。支払いの情報は持たない）。Django のユーザー表は利用者には使わない
- `saas/admin.py`: 管理画面（一覧・絞り込み・検索・組織の中のメンバー編集）
- `saas/management/commands/seed_demo.py`: 架空の組織・メンバー・プラン・契約を入れる（繰り返し実行できる。ローカルの試作 DB だけに使う）
- `saas/jwt_auth.py`: `app/auth.py` と同じ規則の JWT 検証（公開鍵は JWKS のファイル。取得・取り直しは持たない）
- `saas/views.py`: Bearer の JWT で認証する最小の API。役割（owner / admin / member / viewer）で権限を分ける
- `bench/`: 境界の検査と測定のスクリプト。`bootstrap_db.py` が一時 Postgres に `saas`（Django 専用）と `chatbot`（本番の身代わりの簡略な表）を作る

## 戻し方

`rm -rf ops/django-saas .venv-django`、`scripts/test-postgres-down.sh`。製品のファイル（`requirements*.txt` など）は変えていない。
