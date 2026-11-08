"""Minimal SQL migration runner for the Prisma Postgres DB. DRAFT - not run yet.

  python db/migrate.py            # plan: list applied / pending (read-only session, writes nothing)
  python db/migrate.py --apply    # apply pending files, one transaction per file

Reads DIRECT_URL from the environment (never printed). Only the direct host db.prisma.io is accepted
(DDL must not go through the PgBouncer pooled host). Files: db/migrations/NNNN_name.sql, applied in name order.
History table schema_migrations(version, checksum, applied_at): an applied file whose checksum changed aborts the run
(edit a new file instead). A session advisory lock prevents two runners at once. Needs psycopg 3.
"""

import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
LOCK_KEY = 7260801  # arbitrary constant for pg_advisory_lock
HISTORY_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
)


def connect(read_only: bool) -> psycopg.Connection:
    url = os.environ.get("DIRECT_URL")
    if not url:
        sys.exit("DIRECT_URL is not set (run via: infisical run ... -- python db/migrate.py)")
    u = urlsplit(url)
    if u.hostname != "db.prisma.io" or u.port != 5432:
        sys.exit("DIRECT_URL must point at the direct host db.prisma.io:5432 (not the pooled host)")
    conn = psycopg.connect(url, connect_timeout=15, autocommit=True)
    if read_only:  # server-side guard for plan mode (conn.read_only is not applied in autocommit mode)
        conn.execute("set default_transaction_read_only = on")
    return conn


def files() -> list[tuple[str, str, str]]:
    out = []
    for p in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        text = p.read_text(encoding="utf-8")
        out.append((p.stem, hashlib.sha256(text.encode()).hexdigest(), text))
    return out


def history(conn: psycopg.Connection) -> dict[str, str] | None:
    """None when schema_migrations does not exist yet."""
    if conn.execute("select to_regclass('public.schema_migrations')").fetchone()[0] is None:
        return None
    return dict(conn.execute("select version, checksum from schema_migrations").fetchall())


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    conn = connect(read_only=not apply)
    conn.execute("select pg_advisory_lock(%s)", (LOCK_KEY,))
    done = history(conn)
    state = done or {}
    for version, checksum, _ in files():
        if version in state and state[version] != checksum:
            sys.exit(f"ABORT: {version} was applied with a different checksum (do not edit applied files)")
    pending = [f for f in files() if f[0] not in state]
    print("applied:", ", ".join(state) or "(none)")
    print("pending:", ", ".join(f[0] for f in pending) or "(none)")
    if done is None:
        n = conn.execute(
            "select count(*) from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE'"
        ).fetchone()[0]
        if n:
            sys.exit(
                f"ABORT: no history table but public already has {n} tables (not an empty DB; decide how to adopt it first)"
            )
    if not apply:
        print("plan only (nothing written). Re-run with --apply to apply.")
        return
    conn.execute(HISTORY_DDL)
    conn.autocommit = False
    for version, checksum, text in pending:
        with conn.transaction():
            conn.execute(text)
            conn.execute("insert into schema_migrations (version, checksum) values (%s, %s)", (version, checksum))
        print("applied", version)
    conn.autocommit = True
    print("DONE")


if __name__ == "__main__":
    main()
