"""読み取り API の検査用の使い捨て DB を作る（ローカルの Postgres 専用。本番・開発用の DB には使わない）。

  SETUP_ADMIN_URL=postgresql://…@host:5433/postgres  python ops/read-api/testdata/setup_db.py readapi_cmp
  python ops/read-api/testdata/setup_db.py readapi_cmp --drop          # 後片付け（その名前の DB を消す）

`db/migrations/*.sql`（名前順）→ `db/seeds/classification.sql` → `ops/read-api/testdata/seed.sql` を、新しい DB に流す。
接続先のホストは、ローカル（localhost・127.0.0.1・host.docker.internal・db）だけ。URL とパスワードは表示しない。
"""

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[3]
LOCAL_HOSTS = {"localhost", "127.0.0.1", "host.docker.internal", "db"}


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    drop = "--drop" in sys.argv
    if len(args) != 1 or not re.fullmatch(r"readapi_[a-z0-9_]{1,40}", args[0]):
        print("使い方: setup_db.py readapi_<名前> [--drop]（DB 名は readapi_ で始める）")
        return 2
    name = args[0]
    admin = os.environ.get("SETUP_ADMIN_URL", "")
    parts = urlsplit(admin)
    if parts.hostname not in LOCAL_HOSTS:
        print("SETUP_ADMIN_URL のホストがローカルではありません。何もしません。")
        return 2
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        if drop:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
            print(f"{name} を消しました")
            return 0
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = urlunsplit(parts._replace(path=f"/{name}"))
    files = sorted((ROOT / "db" / "migrations").glob("*.sql")) + [
        ROOT / "db" / "seeds" / "classification.sql",
        ROOT / "ops" / "read-api" / "testdata" / "seed.sql",
    ]
    with psycopg.connect(target, autocommit=True, connect_timeout=5) as conn:
        for path in files:
            conn.execute(path.read_text(encoding="utf-8"))
            print(f"適用: {path.relative_to(ROOT)}")
    print(f"{name} を作りました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
