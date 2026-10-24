"""一時 Postgres（scripts/test-postgres-up.sh、ポート 55432）に、学習用の DB・ロール・スキーマを作る。冪等。

- ロール saas_app: LOGIN。スーパーユーザーでも CREATEDB でもない。`saas` スキーマだけの所有者
- ロール chatbot_app: `chatbot` スキーマの所有者（本番の同名のスキーマ・ロールの身代わり）
- データベース saas_dev: `chatbot`（身代わりの表。実物は pgvector / pgroonga が要るので、簡略化したもの）と、空の `saas`
- データベース saas_template: Django のテスト用 DB の元（`saas` スキーマだけ）

使い方: python bench/bootstrap_db.py [接続 URL。既定 postgresql://postgres@127.0.0.1:55432/postgres]
"""

import sys

import psycopg
from psycopg import sql

ADMIN_URL = sys.argv[1] if len(sys.argv) > 1 else "postgresql://postgres@127.0.0.1:55432/postgres"
ADMIN_DB = ADMIN_URL.rsplit("/", 1)[0]


def run(url: str, statements: list[str], autocommit: bool = True) -> None:
    with psycopg.connect(url, autocommit=autocommit) as conn:
        for statement in statements:
            conn.execute(statement)


def exists(conn, query: str, name: str) -> bool:
    return conn.execute(query, (name,)).fetchone() is not None


def main() -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        if not exists(conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", "saas_app"):
            conn.execute("CREATE ROLE saas_app LOGIN")
        if not exists(conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", "chatbot_app"):
            conn.execute("CREATE ROLE chatbot_app LOGIN")
        for db in ("saas_dev", "saas_template"):
            if not exists(conn, "SELECT 1 FROM pg_database WHERE datname = %s", db):
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(db)))
        conn.execute("GRANT CONNECT ON DATABASE saas_dev TO saas_app, chatbot_app")

    run(
        f"{ADMIN_DB}/saas_dev",
        [
            "CREATE SCHEMA IF NOT EXISTS saas AUTHORIZATION saas_app",
            "CREATE SCHEMA IF NOT EXISTS chatbot AUTHORIZATION chatbot_app",
            "REVOKE ALL ON SCHEMA chatbot FROM PUBLIC",
            "REVOKE ALL ON SCHEMA saas FROM PUBLIC",
            "REVOKE ALL ON SCHEMA public FROM PUBLIC",
            # 身代わりの表（本番の chatbot スキーマの形を簡略にしたもの。外部キー・索引・CHECK 制約を含む）
            """CREATE TABLE IF NOT EXISTS chatbot.documents (
                 id bigserial PRIMARY KEY,
                 name text NOT NULL UNIQUE,
                 collection text NOT NULL DEFAULT 'zahard'
               )""",
            """CREATE TABLE IF NOT EXISTS chatbot.chunks (
                 id bigserial PRIMARY KEY,
                 document_id bigint NOT NULL REFERENCES chatbot.documents(id) ON DELETE CASCADE,
                 ordinal integer NOT NULL CHECK (ordinal >= 0),
                 body text NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS chunks_document_idx ON chatbot.chunks (document_id, ordinal)",
            "ALTER TABLE chatbot.documents OWNER TO chatbot_app",
            "ALTER TABLE chatbot.chunks OWNER TO chatbot_app",
            "ALTER SEQUENCE chatbot.documents_id_seq OWNER TO chatbot_app",
            "ALTER SEQUENCE chatbot.chunks_id_seq OWNER TO chatbot_app",
            """INSERT INTO chatbot.documents (name) VALUES ('stand-in') ON CONFLICT DO NOTHING""",
        ],
    )
    run(
        f"{ADMIN_DB}/saas_template",
        [
            "CREATE SCHEMA IF NOT EXISTS saas AUTHORIZATION saas_app",
            "REVOKE ALL ON SCHEMA public FROM PUBLIC",
        ],
    )
    print("ok: saas_dev (chatbot + saas), saas_template (saas)")


if __name__ == "__main__":
    main()
