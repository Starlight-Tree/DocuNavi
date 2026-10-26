"""ローカルでの確認用 DB（access_admin_dev）を作り直す。一時 Postgres（scripts/test-postgres-up.sh）が起動していること。

    cd ops/access_admin && ../../.venv-django/bin/python -m config.dev_db

`chatbot` / `auth` の身代わりと supabase/migrations の SQL を入れ、確認用の利用者（example.com のダミー）を入れる。
そのあと、本番と同じ DB ロール（access_admin）で Django を動かせる:

    ADMIN_TEST_DB_USER=access_admin ../../.venv-django/bin/python manage.py migrate
    ADMIN_TEST_DB_USER=access_admin ../../.venv-django/bin/python manage.py runserver 127.0.0.1:8765

一時 Postgres は trust 認証なので、access_admin にパスワードは要らない。外部の Supabase・本番には接続しない。
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

DEV_DB = "access_admin_dev"
SEED_SQL = """
insert into auth.users (id, email, encrypted_password, last_sign_in_at) values
  ('11111111-1111-4111-8111-111111111111', 'operator@example.com', 'x', now() - interval '1 hour'),
  ('22222222-2222-4222-8222-222222222222', 'student-a@example.com', 'x', now() - interval '2 days'),
  ('33333333-3333-4333-8333-333333333333', 'student-b@example.com', 'x', null),
  ('44444444-4444-4444-8444-444444444444', 'cafe-staff@example.com', 'x', now() - interval '5 days');
insert into chatbot.document_collections (name, label) values ('newgroup', '新しいグループ')
  on conflict (name) do nothing;
insert into chatbot.roles (name, label) values ('cafeonly', '店舗だけ'), ('guidesonly', '規程だけ');
insert into chatbot.role_collection_access (role, collection) values ('cafeonly', 'cafe'), ('guidesonly', 'guides');
insert into chatbot.user_roles (user_id, role) values ('44444444-4444-4444-8444-444444444444', 'cafeonly');
"""


def main() -> None:
    import django

    django.setup()
    from config import test_runner

    test_runner.build_database(DEV_DB)
    with test_runner._admin_connect(DEV_DB) as conn:
        conn.execute(SEED_SQL)
    print(f"{DEV_DB} を作り直しました。")


if __name__ == "__main__":
    main()
