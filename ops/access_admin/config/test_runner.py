"""テスト用の DB を用意するランナー。

`chatbot` / `auth` スキーマの身代わり（認証の表・資料の表）と、supabase/migrations の実際の SQL（資料グループ・ロール・access_admin）を
入れたテンプレート DB を作り、テスト DB をそこから複製する（Django の migrate は、そのあと `saas` に Django の表だけを作る）。
migrate のあと、`saas` の表の所有者を access_admin にする（本番で access_admin が migrate するのと同じ）。
テストは `SET LOCAL ROLE access_admin` に切り替えて、本番と同じ権限で動かす（accessadmin/tests/base.py）。

必要なもの: 一時 Postgres（scripts/test-postgres-up.sh。接続先は ADMIN_TEST_DB_*）。スーパーユーザーで接続する。
"""

from pathlib import Path

import psycopg
from django.conf import settings
from django.db import connections
from django.test.runner import DiscoverRunner

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "supabase" / "migrations"
# chatbot の表を作る順（実際に本番へ適用するのと同じ順）。pgvector・pgroonga が要る古い migration は使わず、必要な表の身代わりを作る
MIGRATION_FILES = (
    "20261025205258_document_collections.sql",
    "20261026045921_role_collection_access.sql",
    "20261026070500_access_admin.sql",
)
TEMPLATE_NAME = "access_admin_template"

STAND_IN_SQL = """
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'chatbot_app') then create role chatbot_app nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
end
$$;
create schema chatbot;
create schema auth;
-- Supabase の auth.users の身代わり（実物は列がもっと多い。ビューが 3 列だけを出すことを確かめるため、秘密らしい列を足す）
create table auth.users (
  id uuid primary key default gen_random_uuid(),
  email text,
  encrypted_password text,
  last_sign_in_at timestamptz,
  raw_user_meta_data jsonb
);
create table chatbot.documents (id bigserial primary key, source text not null, collection text);
create table chatbot.chunks (id bigserial primary key, document_id bigint not null, content text not null default '');
"""


def _admin_connect(dbname: str = "postgres") -> psycopg.Connection:
    db = settings.DATABASES["default"]
    return psycopg.connect(
        dbname=dbname,
        user=db["USER"],
        password=db["PASSWORD"] or None,
        host=db["HOST"],
        port=db["PORT"],
        autocommit=True,
    )


def build_database(name: str) -> None:
    """身代わりのスキーマと migration を入れた DB を、作り直す（同名があれば消す）。"""
    with _admin_connect() as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{name}"')
    with _admin_connect(name) as conn:
        conn.execute(STAND_IN_SQL)
        for file in MIGRATION_FILES:
            conn.execute((MIGRATIONS_DIR / file).read_text(encoding="utf-8"))


def build_template_database() -> None:
    build_database(TEMPLATE_NAME)


def drop_template_database() -> None:
    with _admin_connect() as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{TEMPLATE_NAME}" WITH (FORCE)')


def hand_saas_tables_to_access_admin() -> None:
    """Django の migrate が作った `saas` の表を、access_admin の持ち物にする（本番では access_admin 自身が作る）。"""
    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'saas'")
        for (table,) in cursor.fetchall():
            cursor.execute(f'ALTER TABLE saas."{table}" OWNER TO access_admin')


class TemplateDatabaseRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        build_template_database()
        config = super().setup_databases(**kwargs)
        hand_saas_tables_to_access_admin()
        return config

    def teardown_databases(self, old_config, **kwargs):
        super().teardown_databases(old_config, **kwargs)
        drop_template_database()
