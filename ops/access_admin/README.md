# ops/access_admin — ロール別の資料グループの管理画面（Django Admin）

運用者だけが使う管理画面。利用者へのロールの割り当て（`chatbot.user_roles`）を画面で行う。ロードマップ ⑧ のフェーズ 4a。
設計・運用・TOTP の手順は [docs/access-admin.md](../../docs/access-admin.md)。製品の API（`app/`）とは別のサービスで、`app/` を import しない。
`ops/django-saas` は学習用の試作で、これとは無関係（触らない）。

## 構成

- `config/`: Django プロジェクト。`settings/{base,prod,test}.py`、URL、WSGI、テスト用ランナー（`test_runner.py`）、確認用 DB の作成（`dev_db.py`）
- `accessadmin/`: アプリ。`health.py`（`/healthz` `/readyz`）、`sites.py`（管理サイト）。`services.py` は標準ライブラリだけの純粋な関数（Django を import しない）
- DB の用意は `supabase/migrations/20261026070500_access_admin.sql`。Django の `migrate` が変えるのは `saas` スキーマだけ

## ローカルでの実行（開発コンテナ。外部の Supabase・本番には接続しない）

```bash
cd /workspaces/docunavi
python3 -m venv .venv-django && .venv-django/bin/pip install -r ops/access_admin/requirements.txt   # 製品の依存とは別の venv
scripts/test-postgres-up.sh                                    # 一時 Postgres 17（ポート 55432）
cd ops/access_admin
../../.venv-django/bin/python manage.py test                   # テスト用 DB は、身代わりのスキーマと migration を入れたテンプレートから作る
../../.venv-django/bin/python -m config.dev_db                 # 確認用 DB（access_admin_dev）を作り直す
ADMIN_TEST_DB_USER=access_admin ../../.venv-django/bin/python manage.py migrate     # 本番と同じ DB ロールで saas に Django の表を作る
ADMIN_TEST_DB_USER=access_admin ../../.venv-django/bin/python manage.py runserver 127.0.0.1:8765
cd ../.. && scripts/test-postgres-down.sh
```

依存は `requirements.in` を直して `uv pip compile`（`.txt` は直接編集しない。コマンドは `requirements.in` の先頭）。
