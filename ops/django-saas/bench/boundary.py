"""マイグレーションの境界の検査: Django が `saas` スキーマだけを作り、`chatbot` に触れないことを確かめる。.venv-django で動かす。

手順: (1) `saas` を作り直す → (2) chatbot と他のスキーマの定義を記録 → (3) saas_app（chatbot に権限が無い）で `manage.py migrate`
→ (4) 定義を取り直して比べる → (5) saas_app が chatbot を読めない・作れないことを確かめる。結果は JSON で標準出力へ。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("BOUNDARY_PG_BASE", "postgresql://postgres@127.0.0.1:55432")
SYSTEM = ("pg_catalog", "information_schema", "pg_toast")


def snapshot(conn) -> dict:
    """chatbot の表・列・制約・索引・権限と、saas 以外のスキーマにある全リレーションの一覧。"""
    q = conn.execute
    return {
        "columns": q(
            "SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'chatbot' ORDER BY table_name, ordinal_position"
        ).fetchall(),
        "constraints": q(
            "SELECT c.relname, k.conname, pg_get_constraintdef(k.oid) FROM pg_constraint k "
            "JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'chatbot' ORDER BY 1, 2"
        ).fetchall(),
        "indexes": q(
            "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'chatbot' ORDER BY 1, 2"
        ).fetchall(),
        "acl": q(
            "SELECT n.nspname, c.relname, pg_get_userbyid(c.relowner), c.relacl::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'chatbot' ORDER BY 1, 2"
        ).fetchall(),
        "rows": q("SELECT (SELECT count(*) FROM chatbot.documents), (SELECT count(*) FROM chatbot.chunks)").fetchall(),
        "other_relations": q(
            "SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname <> ALL(%s) AND n.nspname NOT LIKE 'pg_toast%%' AND n.nspname <> 'saas' ORDER BY 1, 2",
            (list(SYSTEM),),
        ).fetchall(),
    }


def denied(url: str, statement: str) -> str:
    """saas_app で実行して、拒否されたことを確かめる（SQLSTATE を返す。通ってしまったら 'ALLOWED'）。"""
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(statement)
        return "ALLOWED"
    except psycopg.Error as exc:
        return exc.sqlstate or type(exc).__name__


def main() -> None:
    admin = psycopg.connect(f"{BASE}/saas_dev", autocommit=True)
    admin.execute("DROP SCHEMA IF EXISTS saas CASCADE")
    admin.execute("CREATE SCHEMA saas AUTHORIZATION saas_app")
    admin.execute("REVOKE ALL ON SCHEMA saas FROM PUBLIC")
    before = snapshot(admin)

    env = {**os.environ, "SAAS_DB_USER": "saas_app"}
    t0 = time.perf_counter()
    done = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--no-input"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    elapsed = round(time.perf_counter() - t0, 2)
    after = snapshot(admin)

    saas_tables = [
        r[0] for r in admin.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'saas' ORDER BY 1").fetchall()
    ]
    migrations_in = admin.execute("SELECT schemaname FROM pg_tables WHERE tablename = 'django_migrations'").fetchall()
    app_url = f"{BASE}/saas_dev".replace("postgres@", "saas_app@")
    result = {
        "migrate_returncode": done.returncode,
        "migrate_seconds": elapsed,
        "migrate_last_line": (done.stdout.strip().splitlines() or [""])[-1],
        "chatbot_unchanged": {key: before[key] == after[key] for key in before},
        "saas_tables": saas_tables,
        "django_migrations_schema": [r[0] for r in migrations_in],
        "saas_app_denied": {
            "select chatbot.documents": denied(app_url, "SELECT 1 FROM chatbot.documents"),
            "create table chatbot.x": denied(app_url, "CREATE TABLE chatbot.x (id int)"),
            "create table public.x": denied(app_url, "CREATE TABLE public.x (id int)"),
            "create database": denied(app_url, "CREATE DATABASE nope"),
        },
    }
    print(json.dumps(result, indent=1, ensure_ascii=False))
    ok = (
        done.returncode == 0
        and all(result["chatbot_unchanged"].values())
        and "ALLOWED" not in result["saas_app_denied"].values()
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
